# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FastAPI BFF & API Gateway for Google Cloud Support Case Management Agent."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from app import config
from app.agent import app as adk_app
from app.agent import run_deterministic_agent_turn
from app.case_notifier import get_case_update_monitor
from app.security import (
    SecurityHeadersAndMethodMiddleware,
    SessionData,
    get_or_create_session,
    get_session_from_request,
    invalidate_session,
    require_csrf,
    resolve_stored_upload_path,
    validate_and_store_upload,
)
from app.support_client import get_support_client

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Google Cloud Support Case Management Agent",
    description="Enterprise BFF & Web Portal for Google Cloud Support API v2 Lifecycle Management",
    version="1.0.0",
)

app.add_middleware(SecurityHeadersAndMethodMiddleware)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_adk_session_service = InMemorySessionService()
_adk_runner = Runner(
    app=adk_app,
    session_service=_adk_session_service,
    auto_create_session=True,
)


# -----------------------------------------------------------------------------
# Request Models (Input Validation)
# -----------------------------------------------------------------------------
class ModeSwitchRequest(BaseModel):
    mode: str = Field(..., pattern="^(MOCK|LIVE|mock|live)$")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    project_id: str = Field(default="gcp-prod-core-01", max_length=64)
    selected_case_name: str | None = Field(default=None, max_length=128)


class CaseDraftRequest(BaseModel):
    project_id: str = Field(..., min_length=5, max_length=64)
    display_name: str = Field(..., min_length=4, max_length=200)
    description: str = Field(..., min_length=10, max_length=8000)
    severity: str = Field(default="S3", pattern="^(S1|S2|S3|S4|P1|P2|P3|P4)$")
    classification_id: str = Field(default="", max_length=256)
    classification_display_name: str = Field(default="", max_length=256)
    subscriber_emails: str = Field(default="", max_length=512)
    time_zone: str = Field(default="Asia/Shanghai", max_length=64)


class EscalationDraftRequest(BaseModel):
    case_name: str = Field(..., min_length=10, max_length=128)
    reason: str = Field(
        default="BUSINESS_IMPACT",
        pattern="^(RESOLUTION_TIME|TECHNICAL_EXPERTISE|BUSINESS_IMPACT)$",
    )
    justification: str = Field(..., min_length=10, max_length=2000)


class DraftConfirmRequest(BaseModel):
    confirmed: bool = Field(default=True)
    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=8000)
    severity: str | None = Field(default=None, max_length=8)
    subscriber_emails: str | None = Field(default=None, max_length=512)


class CommentCreateRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


class IMWebhookRequest(BaseModel):
    platform: str = Field(default="feishu", max_length=32)
    user_id: str = Field(default="im-operator", max_length=64)
    project_id: str = Field(default="gcp-prod-core-01", max_length=64)
    text: str = Field(..., min_length=1, max_length=4000)


def _handle_domain_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    try:
        from google.api_core import exceptions as gexc
    except ImportError:  # pragma: no cover
        gexc = None
    if gexc is not None and isinstance(exc, gexc.GoogleAPICallError):
        code = int(exc.code or 500)
        logger.error("Support API error %s: %s", code, exc.message)
        if 400 <= code < 500:
            return HTTPException(
                status_code=code, detail=f"Google Cloud Support API: {exc.message}"
            )
        return HTTPException(status_code=502, detail="Google Cloud Support API is unavailable.")
    logger.error("Unhandled internal error: %s", type(exc).__name__)
    return HTTPException(status_code=500, detail="An internal server error occurred.")


# -----------------------------------------------------------------------------
# Web Portal Entrypoint
# -----------------------------------------------------------------------------
@app.get("/")
async def serve_portal(request: Request) -> Response:
    """Serve the Google Cloud Support Case Management Web Portal."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        return Response(content="Portal assets missing.", status_code=404)
    response = FileResponse(str(index_path), media_type="text/html; charset=utf-8")
    get_or_create_session(request, response)
    return response


# -----------------------------------------------------------------------------
# Session, Auth & Mode Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/session")
async def get_session_info(request: Request, response: Response) -> dict[str, Any]:
    """Initialize or retrieve active session metadata, CSRF token, mode, and project allowlist."""
    sess = get_or_create_session(request, response)
    allowed_projects = config.get_allowed_projects()
    client = get_support_client()
    return {
        "authenticated": True,
        "user_id": sess.user_id,
        "csrf_token": sess.csrf_token,
        "mode": config.get_support_mode(),
        "force_test_case": config.is_force_test_case(),
        "allowed_projects": allowed_projects,
        "default_project": allowed_projects[0] if allowed_projects else "gcp-prod-core-01",
        "classifications": client.search_classifications(""),
        "pending_drafts": client.list_pending_drafts(),
    }


@app.post("/api/logout")
async def logout_session(
    request: Request,
    response: Response,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Invalidate current session on server and clear session cookie."""
    invalidate_session(request, response)
    return {"status": "logged_out", "message": "Session invalidated successfully."}


@app.post("/api/mode")
async def switch_mode(
    body: ModeSwitchRequest,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Switch between MOCK and LIVE Support API modes."""
    try:
        new_mode = config.set_support_mode(body.mode)
        return {
            "status": "success",
            "mode": new_mode,
            "force_test_case": config.is_force_test_case(),
        }
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


# -----------------------------------------------------------------------------
# Conversational Agent Endpoint
#   CHAT_ENGINE=llm (default): Google ADK Runner + Gemini.
#   CHAT_ENGINE=deterministic: keyword engine (offline tests / no credentials).
#   If the LLM call fails, the keyword engine is used and the response is labelled
#   engine="deterministic_fallback" so the degradation is visible.
# -----------------------------------------------------------------------------
def _chat_engine() -> str:
    engine = os.getenv("CHAT_ENGINE", "llm").strip().lower()
    return engine if engine in ("llm", "deterministic") else "llm"


async def _run_llm_turn(
    user_id: str,
    session_id: str,
    message: str,
    project_id: str,
    selected_case_name: str | None,
    context_note: str | None = None,
    inline_attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    allowed_projects_str = ", ".join(config.get_allowed_projects())
    att_notes: list[str] = []
    multimodal_parts: list[types.Part] = []

    for att in inline_attachments or []:
        if not isinstance(att, dict):
            continue
        fname = str(att.get("original_filename") or "attachment")
        mime_t = str(att.get("mime_type") or "application/octet-stream")
        raw_data = att.get("content_bytes")
        size_b = int(att.get("size_bytes") or (len(raw_data) if isinstance(raw_data, bytes) else 0))
        att_notes.append(f"{fname} ({mime_t}, {size_b} bytes)")
        if isinstance(raw_data, bytes) and raw_data:
            if mime_t.startswith("image/") or mime_t == "application/pdf":
                multimodal_parts.append(types.Part.from_bytes(data=raw_data, mime_type=mime_t))
            elif (
                mime_t.startswith("text/")
                or mime_t in ("application/json", "application/x-yaml", "application/xml")
            ):
                from app.security import redact_sensitive_secrets

                raw_excerpt = raw_data.decode("utf-8", errors="replace")[:4000]
                excerpt, _ = redact_sensitive_secrets(raw_excerpt)
                att_notes.append(f"[诊断文件 {fname} 内容摘录(已脱敏)]:\n{excerpt}")

    att_header = ""
    if att_notes:
        att_header = (
            "[本轮随消息上传并已安全暂存的附件（确认提单后将自动上传至 Google Cloud Support）: "
            + "; ".join(att_notes)
            + "]\n"
        )

    contextual_prompt = (
        f"[允许操作的 Projects: {allowed_projects_str}] "
        f"[当前工作区 Project: {project_id}] "
        f"[当前选中工单: {selected_case_name or '未选中'}]\n"
        + (f"{context_note}\n" if context_note else "")
        + att_header
        + f"用户指令: {message}"
    )
    parts: list[types.Part] = [types.Part.from_text(text=contextual_prompt), *multimodal_parts]
    content = types.Content(role="user", parts=parts)
    reply_parts: list[str] = []
    tool_calls_trace: list[dict[str, Any]] = []
    new_draft_ids: list[str] = []
    revised_draft_ids: list[str] = []
    subscription_actions: list[dict[str, str]] = []
    active_case_name: str | None = selected_case_name
    async for event in _adk_runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if not (event.content and event.content.parts):
            continue
        for part in event.content.parts:
            if part.function_call:
                tool_calls_trace.append(
                    {"tool": part.function_call.name, "args": dict(part.function_call.args or {})}
                )
            elif part.function_response:
                resp = part.function_response.response or {}
                for trace in reversed(tool_calls_trace):
                    if trace["tool"] == part.function_response.name and "result" not in trace:
                        trace["result"] = {"status": resp.get("status")}
                        break
                draft = resp.get("draft") if isinstance(resp, dict) else None
                if (
                    resp.get("status") == "pending_confirmation"
                    and isinstance(draft, dict)
                    and draft.get("draft_id")
                ):
                    new_draft_ids.append(str(draft["draft_id"]))
                elif (
                    isinstance(resp, dict)
                    and resp.get("status") == "draft_revised"
                    and isinstance(draft, dict)
                    and draft.get("draft_id")
                ):
                    revised_draft_ids.append(str(draft["draft_id"]))
                if isinstance(resp, dict):
                    if isinstance(resp.get("case"), dict) and resp["case"].get("name"):
                        active_case_name = str(resp["case"]["name"])
                    elif isinstance(resp.get("comment"), dict) and resp["comment"].get("case_name"):
                        active_case_name = str(resp["comment"]["case_name"])
                    elif resp.get("case_name"):
                        active_case_name = str(resp["case_name"])
                if (
                    isinstance(resp, dict)
                    and resp.get("status") == "success"
                    and resp.get("subscription_action") in ("subscribe", "unsubscribe")
                    and resp.get("case_name")
                ):
                    subscription_actions.append(
                        {
                            "action": str(resp["subscription_action"]),
                            "case_name": str(resp["case_name"]),
                        }
                    )
            elif part.text and event.author != "user":
                reply_parts.append(part.text)
    return {
        "reply": "\n".join(reply_parts).strip() or "（Agent 未返回文本回复）",
        "tool_calls": tool_calls_trace,
        "pending_drafts": get_support_client().list_pending_drafts(project_id),
        "new_draft_ids": new_draft_ids,
        "revised_draft_ids": revised_draft_ids,
        "subscription_actions": subscription_actions,
        "active_case_name": active_case_name,
    }


def _deterministic_new_draft_ids(result: dict[str, Any]) -> list[str]:
    # The deterministic engine only returns drafts created in the current turn.
    return [d["draft_id"] for d in result.get("pending_drafts", []) if d.get("draft_id")]


async def run_agent_turn(
    user_id: str,
    session_id: str,
    message: str,
    project_id: str,
    selected_case_name: str | None,
    context_note: str | None = None,
    inline_attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shared agent turn used by the web chat and IM bots (same agent, guard and fallback).

    `context_note` (IM channel / group context) is only added to the LLM prompt.
    """
    if _chat_engine() == "llm":
        try:
            result = await _run_llm_turn(
                user_id,
                session_id,
                message,
                project_id,
                selected_case_name,
                context_note,
                inline_attachments=inline_attachments,
            )
            result["engine"] = "llm"
            return result
        except Exception as exc:
            logger.warning(
                "LLM chat turn failed (%s); using deterministic fallback engine.",
                type(exc).__name__,
            )
            result = run_deterministic_agent_turn(
                message=message,
                active_project=project_id,
                selected_case_name=selected_case_name,
            )
            result["engine"] = "deterministic_fallback"
            result["new_draft_ids"] = _deterministic_new_draft_ids(result)
            return result

    result = run_deterministic_agent_turn(
        message=message,
        active_project=project_id,
        selected_case_name=selected_case_name,
    )
    result["engine"] = "deterministic"
    result["new_draft_ids"] = _deterministic_new_draft_ids(result)
    return result


async def _run_chat_turn(
    sess: SessionData,
    message: str,
    project_id: str,
    selected_case_name: str | None,
) -> dict[str, Any]:
    return await run_agent_turn(
        sess.user_id, sess.session_id, message, project_id, selected_case_name
    )


@app.post("/api/chat")
async def chat_with_agent(
    body: ChatRequest,
    sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Process a user message via the Google ADK agent (drafts only; humans confirm on the card)."""
    try:
        config.validate_project_access(body.project_id)
        if body.selected_case_name:
            config.validate_project_access(body.selected_case_name)
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc

    turn_result = await _run_chat_turn(
        sess, body.message, body.project_id, body.selected_case_name
    )
    return {
        "status": "success",
        "engine": turn_result["engine"],
        "reply": turn_result["reply"],
        "tool_calls": turn_result["tool_calls"],
        "pending_drafts": turn_result.get("pending_drafts", []),
        "updated_case": turn_result.get("updated_case"),
        "mode": config.get_support_mode(),
    }


# -----------------------------------------------------------------------------
# Support Cases & HITL Draft REST Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/classifications")
async def search_classifications_api(
    q: str = Query(default="", max_length=200),
) -> dict[str, Any]:
    """Search Google Cloud Support case classifications."""
    try:
        items = get_support_client().search_classifications(q)
        return {"status": "success", "classifications": items}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.get("/api/cases")
async def list_cases_api(
    project_id: str = Query(default="gcp-prod-core-01", max_length=64),
    state_filter: str = Query(default="ALL", max_length=32),
) -> dict[str, Any]:
    """List support cases for an authorized GCP project."""
    try:
        cases = get_support_client().list_cases(
            project_id=project_id,
            state_filter=state_filter,
        )
        return {
            "status": "success",
            "project_id": project_id,
            "state_filter": state_filter,
            "cases": cases,
        }
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.get("/api/cases/{project_id}/{case_id}")
async def get_case_details_api(project_id: str, case_id: str) -> dict[str, Any]:
    """Get full details, comments, and attachments for a specific support case."""
    try:
        case_name = config.normalize_case_name(project_id, case_id)
        case_obj = get_support_client().get_case(case_name)
        return {"status": "success", "case": case_obj}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/drafts/case")
async def create_case_draft_api(
    body: CaseDraftRequest,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Create a pending HITL confirmation draft for a new support case."""
    try:
        draft = get_support_client().create_case_draft(
            project_id=body.project_id,
            display_name=body.display_name,
            description=body.description,
            severity=body.severity,
            classification_id=body.classification_id,
            classification_display_name=body.classification_display_name,
            subscriber_emails=body.subscriber_emails,
            time_zone=body.time_zone,
        )
        return {"status": "pending_confirmation", "draft": draft}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/drafts/escalation")
async def create_escalation_draft_api(
    body: EscalationDraftRequest,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Create a pending HITL confirmation draft for escalating an existing case."""
    try:
        draft = get_support_client().create_escalation_draft(
            case_name=body.case_name,
            reason=body.reason,
            justification=body.justification,
        )
        return {"status": "pending_confirmation", "draft": draft}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/drafts/{draft_id}/confirm")
async def confirm_draft_api(
    draft_id: str,
    body: DraftConfirmRequest,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Execute a pending create / escalate / close draft after the user clicks confirm on the card.

    This endpoint is the ONLY path that executes drafts; the agent's tools can only create them.
    """
    try:
        overrides = {
            k: v
            for k, v in {
                "display_name": body.display_name,
                "description": body.description,
                "severity": body.severity,
                "subscriber_emails": body.subscriber_emails,
            }.items()
            if v is not None
        }
        result = get_support_client().execute_confirmed_draft(
            draft_id=draft_id,
            confirmed=body.confirmed,
            overrides=overrides or None,
        )
        return {
            "status": "success",
            "draft_type": result["draft_type"],
            "case": result["case"],
        }
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/drafts/{draft_id}/cancel")
async def cancel_draft_api(
    draft_id: str,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Cancel a pending confirmation draft."""
    try:
        cancelled = get_support_client().cancel_draft(draft_id)
        return {"status": "cancelled", "draft": cancelled}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/cases/{project_id}/{case_id}/comments")
async def add_comment_api(
    project_id: str,
    case_id: str,
    body: CommentCreateRequest,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Add a comment to an existing support case."""
    try:
        case_name = config.normalize_case_name(project_id, case_id)
        client = get_support_client()
        comment = client.add_comment(case_name=case_name, body=body.body)
        updated_case = client.get_case(case_name)
        return {
            "status": "success",
            "comment": comment,
            "case": updated_case,
        }
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/cases/{project_id}/{case_id}/close")
async def close_case_api(
    project_id: str,
    case_id: str,
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Close an existing support case."""
    try:
        case_name = config.normalize_case_name(project_id, case_id)
        closed_case = get_support_client().close_case(case_name)
        return {"status": "success", "case": closed_case}
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/cases/{project_id}/{case_id}/attachments")
async def upload_attachment_api(
    project_id: str,
    case_id: str,
    file: UploadFile = File(...),
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Validate and store a diagnostic attachment for a support case."""
    try:
        case_name = config.normalize_case_name(project_id, case_id)
        config.validate_project_access(case_name)
        stored_info = await validate_and_store_upload(file)
        stored_path = resolve_stored_upload_path(stored_info["stored_filename"])
        content_bytes = stored_path.read_bytes()
        client = get_support_client()
        attachment = client.add_attachment_metadata(
            case_name=case_name,
            original_filename=stored_info["original_filename"],
            stored_filename=stored_info["stored_filename"],
            mime_type=stored_info["mime_type"],
            size_bytes=stored_info["size_bytes"],
            content_bytes=content_bytes,
        )
        updated_case = client.get_case(case_name)
        return {
            "status": "success",
            "attachment": attachment,
            "case": updated_case,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.get("/api/attachments/{attachment_id}/download")
async def download_attachment_api(
    attachment_id: str,
    request: Request,
) -> Response:
    """Serve an uploaded diagnostic attachment to authorized users with strict download headers."""
    # Require an active session (via cookie or signed token)
    sess = get_session_from_request(request)
    if sess is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required to download support attachments.",
        )

    try:
        record = get_support_client().get_attachment_record(attachment_id)
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc

    safe_dl_name = record["original_filename"].replace('"', "")
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_dl_name}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if not record.get("stored_filename") and config.get_support_mode() == "LIVE":
        try:
            raw_bytes = get_support_client().download_live_attachment_bytes(
                record["case_name"], attachment_id
            )
        except Exception as exc:
            raise _handle_domain_exception(exc) from exc
        return Response(
            content=raw_bytes,
            media_type=record["mime_type"],
            headers=headers,
        )

    file_path = resolve_stored_upload_path(record["stored_filename"])
    return FileResponse(
        path=str(file_path),
        media_type=record["mime_type"],
        headers=headers,
    )


@app.post("/api/cases/sync")
async def sync_cases_and_notify_api(
    project_id: str | None = Query(default=None, max_length=64),
    _sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Trigger an immediate case update poll and dispatch Feishu notifications if updates exist."""
    try:
        if project_id:
            config.validate_project_access(project_id)
        summary = get_case_update_monitor().poll_once(project_id=project_id)
        return {"status": "success", **summary}
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc


@app.post("/api/webhook/im")
async def im_webhook_endpoint(
    body: IMWebhookRequest,
    sess: SessionData = Depends(require_csrf),
) -> dict[str, Any]:
    """Standard webhook endpoint for enterprise IM (Feishu / Slack / Google Chat) integration."""
    try:
        config.validate_project_access(body.project_id)
    except Exception as exc:
        raise _handle_domain_exception(exc) from exc

    turn = await _run_chat_turn(sess, body.text, body.project_id, None)
    return {
        "status": "success",
        "platform": body.platform,
        "engine": turn["engine"],
        "reply": turn["reply"],
        "pending_drafts": turn.get("pending_drafts", []),
    }


@app.on_event("startup")
async def _start_feishu_bot() -> None:
    """Start the Feishu long-connection bot (daemon thread) only when its credentials are set."""
    import asyncio

    from app import feishu_bot

    bot = feishu_bot.start_feishu_bot_if_configured(
        turn_runner=run_agent_turn,
        main_loop=asyncio.get_running_loop(),
        error_mapper=_handle_domain_exception,
    )
    if bot is None and os.getenv("FEISHU_WEBHOOK_URL", "").strip():
        get_case_update_monitor().start_background()


if __name__ == "__main__":
    import uvicorn

    # Strictly listen on 127.0.0.1 for local testing and BFF execution (never 0.0.0.0)
    uvicorn.run(app, host="127.0.0.1", port=8000)
