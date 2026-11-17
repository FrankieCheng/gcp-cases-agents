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

"""Google ADK FunctionTools for Google Cloud Support Case Lifecycle Management."""

from __future__ import annotations

from typing import Any

from app.support_client import get_support_client


def search_case_classifications(query: str) -> dict[str, Any]:
    """Search valid Google Cloud Support case classifications matching a product or issue.

    Always call this tool before drafting a new support case so you can populate a valid
    classification_id and classification_display_name.

    Args:
        query: Product or symptom keywords (e.g., 'Cloud SQL PostgreSQL timeout', 'GKE autoscaler', 'Vertex AI 429').

    Returns:
        A dictionary with status and a list of matching classification objects (id, display_name).
    """
    try:
        items = get_support_client().search_classifications(query=query)
        return {
            "status": "success",
            "query": query,
            "count": len(items),
            "classifications": items,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def prepare_case_draft(
    project_id: str,
    display_name: str,
    description: str,
    severity: str,
    classification_id: str,
    classification_display_name: str,
    subscriber_emails: str,
    time_zone: str,
) -> dict[str, Any]:
    """Create a pending support case draft for human-in-the-loop (HITL) user confirmation.

    This does NOT create the case. The user must review the draft card in the portal and click
    the confirm button; only that human action calls cases.create.

    Args:
        project_id: Target GCP project ID from ALLOWED_PROJECTS (e.g., 'gcp-prod-core-01').
        display_name: Concise summary title of the issue (4-200 characters).
        description: Detailed technical description including symptoms, timestamps, and error messages.
        severity: Case severity level ('S1', 'S2', 'S3', or 'S4').
        classification_id: Classification ID returned by search_case_classifications.
        classification_display_name: Human-readable classification hierarchy name.
        subscriber_emails: Comma-separated subscriber emails or empty string ''.
        time_zone: User IANA time zone (e.g., 'Asia/Shanghai' or 'UTC').

    Returns:
        A dictionary containing the created draft metadata (`draft_id`, `status`, `payload`).
    """
    try:
        draft = get_support_client().create_case_draft(
            project_id=project_id,
            display_name=display_name,
            description=description,
            severity=severity,
            classification_id=classification_id,
            classification_display_name=classification_display_name,
            subscriber_emails=subscriber_emails,
            time_zone=time_zone,
        )
        similar = draft.get("payload", {}).get("similar_open_cases") or []
        diag_tips = draft.get("payload", {}).get("diagnostic_tips") or []
        redacted = draft.get("payload", {}).get("redacted_secrets") or []
        dup_hint = ""
        if similar:
            ids = ", ".join(f"{s['case_id']} ({s['display_name'][:40]})" for s in similar[:2])
            dup_hint = f" 注意：检测到同项目已有相似未关闭工单 [{ids}]，请提醒用户避免重复提单。"
        redact_hint = ""
        if redacted:
            redact_hint = f" 🛡️ 安全提示：已自动脱敏文本中的敏感凭据（{', '.join(redacted)}）。"
        return {
            "status": "pending_confirmation",
            "message": (
                f"已生成工单确认草稿 `{draft['draft_id']}`。请在确认卡片核对工单信息并点击「确认提交工单」。"
                f"{dup_hint}{redact_hint}"
            ),
            "draft": draft,
            "similar_open_cases": similar,
            "diagnostic_tips": diag_tips,
            "redacted_secrets": redacted,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def list_support_cases(project_id: str, state_filter: str) -> dict[str, Any]:
    """List Google Cloud Support cases under an authorized GCP project.

    Args:
        project_id: The GCP project ID (e.g., 'gcp-prod-core-01').
        state_filter: Filter by case status ('ALL', 'OPEN', 'ESCALATED', or 'CLOSED').

    Returns:
        A dictionary containing the list of matching support cases.
    """
    try:
        cases = get_support_client().list_cases(
            project_id=project_id,
            state_filter=state_filter,
        )
        return {
            "status": "success",
            "project_id": project_id,
            "state_filter": state_filter,
            "count": len(cases),
            "cases": cases,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def get_support_case_details(case_name: str) -> dict[str, Any]:
    """Fetch complete details, comments history, and attachments for a support case.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.

    Returns:
        A dictionary containing the full case object, comments, and attachments.
    """
    try:
        case_obj = get_support_client().get_case(case_name=case_name)
        return {
            "status": "success",
            "case": case_obj,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def add_case_comment(case_name: str, body: str) -> dict[str, Any]:
    """Add a comment or diagnostic update to an existing Google Cloud Support case.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.
        body: The comment text to post to the support case.

    Returns:
        A dictionary with the newly created comment and updated case summary.
    """
    try:
        comment = get_support_client().add_comment(case_name=case_name, body=body)
        redacted = comment.get("redacted_secrets") or []
        redact_note = (
            f"（已自动脱敏敏感凭据：{', '.join(redacted)}）" if redacted else ""
        )
        return {
            "status": "success",
            "message": f"已向工单 {case_name} 追加评论。{redact_note}",
            "comment": comment,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def prepare_escalation_draft(
    case_name: str,
    reason: str,
    justification: str,
) -> dict[str, Any]:
    """Create a pending escalation draft for human-in-the-loop (HITL) user confirmation.

    This does NOT escalate the case. The user must click the confirm button on the draft card;
    only that human action calls cases.escalate.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.
        reason: Escalation reason enum ('RESOLUTION_TIME', 'TECHNICAL_EXPERTISE', or 'BUSINESS_IMPACT').
        justification: Detailed justification explaining business impact or technical urgency (at least 10 chars).

    Returns:
        A dictionary containing the pending escalation draft (`draft_id`, `status`, `payload`).
    """
    try:
        draft = get_support_client().create_escalation_draft(
            case_name=case_name,
            reason=reason,
            justification=justification,
        )
        return {
            "status": "pending_confirmation",
            "message": (
                f"已生成工单升级确认草稿 `{draft['draft_id']}`。请核对升级原因与业务影响说明后点击「确认升级工单」。"
            ),
            "draft": draft,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def prepare_close_draft(case_name: str, reason: str) -> dict[str, Any]:
    """Create a pending close draft for human-in-the-loop (HITL) user confirmation.

    This does NOT close the case. The user must click the confirm button on the draft card;
    only that human action calls cases.close.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.
        reason: Short reason for closing (e.g., 'Issue resolved after DB failover').

    Returns:
        A dictionary containing the pending close draft (`draft_id`, `status`, `payload`).
    """
    try:
        draft = get_support_client().create_close_draft(case_name=case_name, reason=reason)
        return {
            "status": "pending_confirmation",
            "message": (
                f"已生成关闭工单确认草稿 `{draft['draft_id']}`。请在确认卡片中点击「确认关闭工单」。"
            ),
            "draft": draft,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def subscribe_case_updates(case_name: str) -> dict[str, Any]:
    """Subscribe the current IM conversation to automatic Feishu notifications when a support case updates.

    Notifications are pushed when Google Cloud Support engineers post a reply, or when case state,
    priority, or escalation status changes.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.

    Returns:
        A dictionary confirming the subscription request for the active conversation.
    """
    try:
        case_obj = get_support_client().get_case(case_name=case_name)
        canonical = str(case_obj.get("name") or case_name)
        return {
            "status": "success",
            "subscription_action": "subscribe",
            "case_name": canonical,
            "message": f"已为当前会话开启工单 `{canonical}` 的更新飞书通知（新回复、状态流转、优先级/升级变更）。",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def unsubscribe_case_updates(case_name: str) -> dict[str, Any]:
    """Unsubscribe the current IM conversation from automatic Feishu notifications for a support case.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.

    Returns:
        A dictionary confirming that notifications were disabled for the active conversation.
    """
    try:
        from app import config

        config.validate_project_access(case_name)
        canonical = config.normalize_case_name(case_name)
        return {
            "status": "success",
            "subscription_action": "unsubscribe",
            "case_name": canonical,
            "message": f"已取消当前会话对工单 `{canonical}` 的飞书更新通知订阅。",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def update_support_case(
    case_name: str,
    severity: str = "",
    subscriber_emails: str = "",
    append_subscribers: bool = True,
) -> dict[str, Any]:
    """Update the priority/severity or subscriber (CC) email addresses of an active support case.

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.
        severity: Optional new severity ('S1', 'S2', 'S3', 'S4'), or '' to keep unchanged.
        subscriber_emails: Optional comma-separated emails to add or set, or '' to keep unchanged.
        append_subscribers: If True (default), merges new emails with existing subscribers; if False, replaces the subscriber list.

    Returns:
        A dictionary containing the updated support case summary.
    """
    try:
        sev_arg = severity.strip() if severity and severity.strip() else None
        sub_arg: str | None
        if subscriber_emails and subscriber_emails.strip():
            sub_arg = subscriber_emails.strip()
        elif not append_subscribers and sev_arg is None:
            sub_arg = ""
        else:
            sub_arg = None

        if sev_arg is None and sub_arg is None:
            return {
                "status": "error",
                "error": "请至少提供要修改的严重级别（S1-S4）或订阅邮箱地址。",
            }

        updated = get_support_client().update_case(
            case_name=case_name,
            severity=sev_arg,
            subscriber_emails=sub_arg,
            append_subscribers=append_subscribers,
        )
        return {
            "status": "success",
            "message": (
                f"已更新工单 `{updated['name']}`（当前严重级别: {updated['severity']} / {updated['priority']}，"
                f"订阅邮箱: {', '.join(updated['subscriber_emails']) or '无'}）。"
            ),
            "case": updated,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def revise_case_draft(
    draft_id: str,
    display_name: str = "",
    description: str = "",
    severity: str = "",
    subscriber_emails: str = "",
    time_zone: str = "",
) -> dict[str, Any]:
    """Modify an existing pending CREATE_CASE draft in place before the user clicks confirm.

    Use this when a draft card was already generated and the user asks to tweak its title,
    description, severity (S1-S4), subscriber emails, or time zone before submitting.

    Args:
        draft_id: Pending draft ID (e.g., 'draft-case-xxxxxxxxxx').
        display_name: New title if changing, else ''.
        description: New detailed description if changing, else ''.
        severity: New severity ('S1'-'S4') if changing, else ''.
        subscriber_emails: Comma-separated subscriber emails if changing, else ''.
        time_zone: New IANA time zone (e.g., 'Asia/Shanghai', 'UTC') if changing, else ''.

    Returns:
        A dictionary with status 'draft_revised' and the updated draft metadata.
    """
    try:
        sub_arg = subscriber_emails.strip() if subscriber_emails and subscriber_emails.strip() else None
        revised = get_support_client().revise_case_draft(
            draft_id=draft_id,
            display_name=display_name,
            description=description,
            severity=severity,
            subscriber_emails=sub_arg,
            time_zone=time_zone,
        )
        return {
            "status": "draft_revised",
            "message": f"已就地更新待确认草稿 `{revised['draft_id']}`，请在确认卡片核对后点击「确认提交」。",
            "draft": revised,
            "diagnostic_tips": revised.get("payload", {}).get("diagnostic_tips") or [],
            "redacted_secrets": revised.get("payload", {}).get("redacted_secrets") or [],
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def prepare_followup_case_draft(
    reference_case_name: str,
    followup_description: str,
    severity: str = "",
) -> dict[str, Any]:
    """Create a linked follow-up CREATE_CASE draft when a CLOSED case recurs or needs reopening.

    Google Cloud Support API v2 does not allow commenting on permanently CLOSED cases. This tool
    clones the original case's classification, project, and subscribers, links the original case ID,
    and generates a HITL confirmation draft.

    Args:
        reference_case_name: Full resource name of the closed/previous case ('projects/<p>/cases/<id>').
        followup_description: New recurrence symptoms, timestamps, or follow-up question.
        severity: Optional severity override ('S1'-'S4'), or '' to inherit original severity.

    Returns:
        A dictionary with status 'pending_confirmation' and the new follow-up draft.
    """
    try:
        draft = get_support_client().create_followup_case_draft(
            reference_case_name=reference_case_name,
            followup_description=followup_description,
            severity=severity,
        )
        return {
            "status": "pending_confirmation",
            "message": (
                f"原工单 `{reference_case_name}` 已关闭，已为您自动继承分类并生成关联续单草稿 "
                f"`{draft['draft_id']}`。请在确认卡片核对并点击「确认提交」。"
            ),
            "draft": draft,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def inspect_case_attachment(case_name: str, attachment_ref: str = "") -> dict[str, Any]:
    """Read or inspect an attachment on a support case (including diagnostic scripts/logs uploaded by Google Support TSE).

    Args:
        case_name: Full resource name in format 'projects/<project_id>/cases/<case_id>'.
        attachment_ref: Optional filename (or partial filename, e.g. 'fix.yaml') or attachment_id; pass '' for the latest attachment.

    Returns:
        A dictionary containing attachment metadata and sanitized text preview or archive entry listing.
    """
    try:
        info = get_support_client().inspect_attachment(
            case_name=case_name, attachment_ref=attachment_ref
        )
        return {"status": "success", "attachment": info}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def search_support_cases(
    query: str = "",
    project_id: str = "",
    state_filter: str = "ALL",
    severity_filter: str = "",
) -> dict[str, Any]:
    """Search Google Cloud Support cases across all authorized projects (or a specific project) by keyword, state, or severity.

    Args:
        query: Keywords to match across case ID, title, description, classification, and comments (or '' for all).
        project_id: Optional single GCP project ID, or '' to search across all ALLOWED_PROJECTS.
        state_filter: 'ALL', 'OPEN', 'ESCALATED', or 'CLOSED'.
        severity_filter: Optional severity filter ('S1', 'S2', 'S3', 'S4'), or ''.

    Returns:
        A dictionary with matching cases across projects.
    """
    try:
        cases = get_support_client().search_cases(
            query=query,
            project_id=project_id,
            state_filter=state_filter,
            severity_filter=severity_filter,
        )
        return {
            "status": "success",
            "query": query,
            "project_id": project_id or "ALL_ALLOWED",
            "count": len(cases),
            "cases": cases,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def get_ops_shift_handover(project_id: str = "") -> dict[str, Any]:
    """Generate an SRE On-Call Shift Handover report and SLA stale-case alert radar across authorized projects.

    Categorizes all open cases into high-priority/escalated, customer-blocked (ACTION_REQUIRED),
    pending verification (SOLUTION_PROVIDED), and SLA-overdue stale cases (S1>=1h, S2>=4h, S3>=24h, S4>=72h).

    Args:
        project_id: Optional single GCP project ID, or '' to inspect all ALLOWED_PROJECTS.

    Returns:
        A structured shift handover summary dictionary.
    """
    try:
        report = get_support_client().get_ops_shift_handover(project_id=project_id)
        return {"status": "success", "handover": report}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# Tools exposed to the model. Deliberately contains NO tool that creates, escalates, or closes a
# case: those actions only run via the human-facing /api/drafts/{id}/confirm endpoint.
ALL_TOOLS = [
    search_case_classifications,
    prepare_case_draft,
    revise_case_draft,
    prepare_followup_case_draft,
    list_support_cases,
    search_support_cases,
    get_ops_shift_handover,
    get_support_case_details,
    inspect_case_attachment,
    update_support_case,
    add_case_comment,
    prepare_escalation_draft,
    prepare_close_draft,
    subscribe_case_updates,
    unsubscribe_case_updates,
]

# Tool names that must never be callable by the model (enforced again in before_tool_callback).
HUMAN_ONLY_TOOL_NAMES = frozenset(
    {
        "confirm_and_create_case",
        "confirm_and_escalate_case",
        "confirm_and_close_case",
        "close_support_case",
        "execute_confirmed_draft",
    }
)
