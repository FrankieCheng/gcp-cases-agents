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
# ruff: noqa: RUF001

"""Google Cloud Support Case Update Monitor and Feishu Interactive Card Notifier."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from app import config
from app.support_client import get_support_client

logger = logging.getLogger(__name__)

_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_\-]{4,64}$")
_CHAT_ID_RE = re.compile(r"^o[a-z]_[A-Za-z0-9_\-]{4,64}$")
_SYNTHETIC_COMMENT_PREFIXES = ("cmt-esc-", "cmt-close-", "cmt-att-")

ALLOWED_FEISHU_WEBHOOK_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})
ALLOWED_FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"

STATE_LABELS_ZH = {
    "NEW": "新建待分配 (NEW)",
    "OPEN": "处理中 (OPEN)",
    "IN_PROGRESS_GOOGLE_SUPPORT": "Google 工程师处理中 (IN_PROGRESS_GOOGLE_SUPPORT)",
    "ACTION_REQUIRED": "需客户补充反馈 (ACTION_REQUIRED)",
    "SOLUTION_PROVIDED": "已提供解决方案 (SOLUTION_PROVIDED)",
    "CLOSED": "已关闭 (CLOSED)",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block all HTTP redirects on outbound webhook calls to prevent redirect-based SSRF."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise PermissionError(f"Outbound webhook redirect (HTTP {code}) is prohibited.")


def validate_feishu_webhook_url(url: str) -> str:
    """Validate that a Feishu incoming webhook URL uses HTTPS and an allowlisted Feishu host."""
    clean = (url or "").strip()
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https":
        raise ValueError("FEISHU_WEBHOOK_URL must use https:// scheme.")
    if parsed.username or parsed.password:
        raise ValueError("FEISHU_WEBHOOK_URL must not contain embedded credentials.")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_FEISHU_WEBHOOK_HOSTS:
        raise PermissionError(
            f"Webhook host '{host}' is not in allowlist {sorted(ALLOWED_FEISHU_WEBHOOK_HOSTS)}."
        )
    if not parsed.path.startswith(ALLOWED_FEISHU_WEBHOOK_PATH_PREFIX):
        raise ValueError(
            f"Webhook path must start with '{ALLOWED_FEISHU_WEBHOOK_PATH_PREFIX}'."
        )
    return clean


def _compute_feishu_webhook_sign(secret: str, timestamp: int) -> str:
    """Compute Feishu custom-bot HMAC-SHA256 signature."""
    string_to_sign = f"{timestamp}\n{secret}".encode()
    mac = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def send_feishu_webhook_card(
    webhook_url: str,
    card: dict[str, Any],
    webhook_secret: str = "",
) -> None:
    """Send an interactive card to a validated Feishu incoming webhook URL."""
    safe_url = validate_feishu_webhook_url(webhook_url)
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }
    clean_secret = (webhook_secret or "").strip()
    if clean_secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _compute_feishu_webhook_sign(clean_secret, ts)

    body_bytes = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        safe_url,
        data=body_bytes,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if data.get("code", 0) not in (0, None):
                raise RuntimeError(
                    f"Feishu webhook error: code={data.get('code')} msg={data.get('msg')}"
                )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Feishu webhook HTTP error {exc.code}.") from exc


def _is_synthetic_comment(comment_id: str) -> bool:
    cid = (comment_id or "").strip()
    if not cid:
        return True
    if cid.endswith("-init") and cid.startswith("cmt-"):
        return True
    return cid.startswith(_SYNTHETIC_COMMENT_PREFIXES)


def diff_case_updates(
    previous: dict[str, Any],
    current_case: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute structured update events between a saved watch snapshot and current case state."""
    events: list[dict[str, Any]] = []

    prev_state = str(previous.get("last_state") or "")
    curr_state = str(current_case.get("state") or "")
    if prev_state and curr_state and prev_state != curr_state:
        events.append(
            {
                "type": "STATE_CHANGED",
                "old_state": prev_state,
                "new_state": curr_state,
                "summary": (
                    f"状态变更: {STATE_LABELS_ZH.get(prev_state, prev_state)} → "
                    f"{STATE_LABELS_ZH.get(curr_state, curr_state)}"
                ),
            }
        )

    prev_prio = str(previous.get("last_priority") or "")
    curr_prio = str(current_case.get("priority") or "")
    if prev_prio and curr_prio and prev_prio != curr_prio:
        events.append(
            {
                "type": "PRIORITY_CHANGED",
                "old_priority": prev_prio,
                "new_priority": curr_prio,
                "summary": f"优先级变更: {prev_prio} → {curr_prio} (当前严重级别 {current_case.get('severity', '')})",
            }
        )

    prev_esc = bool(previous.get("last_escalated", False))
    curr_esc = bool(current_case.get("escalated", False))
    if not prev_esc and curr_esc:
        events.append(
            {
                "type": "ESCALATED_CHANGED",
                "summary": "工单已进入升级处理状态 (Escalated)",
            }
        )

    seen_ids = set(previous.get("seen_comment_ids") or [])
    for cmt in current_case.get("comments") or []:
        cid = str(cmt.get("comment_id") or "").strip()
        if not cid or cid in seen_ids or _is_synthetic_comment(cid):
            continue
        author_type = str(cmt.get("author_type") or "CUSTOMER")
        author = str(cmt.get("author") or ("Google Cloud Support" if author_type == "GOOGLE_SUPPORT" else "Customer"))
        body = str(cmt.get("body") or "").strip()
        role_label = (
            "Google Cloud Support 工程师回复"
            if author_type == "GOOGLE_SUPPORT"
            else "工单新增评论"
        )
        events.append(
            {
                "type": "NEW_COMMENT",
                "comment_id": cid,
                "author": author,
                "author_type": author_type,
                "body": body,
                "create_time": str(cmt.get("create_time") or ""),
                "summary": f"{role_label} ({author}): {body[:180]}{'…' if len(body) > 180 else ''}",
            }
        )

    if "seen_attachment_ids" in previous:
        seen_att_ids = set(previous.get("seen_attachment_ids") or [])
        for att in current_case.get("attachments") or []:
            aid = str(att.get("attachment_id") or "").strip()
            if not aid or aid in seen_att_ids:
                continue
            fname = str(att.get("filename") or att.get("original_filename") or "attachment")
            size_b = int(att.get("size_bytes") or 0)
            events.append(
                {
                    "type": "NEW_ATTACHMENT",
                    "attachment_id": aid,
                    "filename": fname,
                    "size_bytes": size_b,
                    "summary": f"新增工单附件: {fname} ({size_b} bytes)",
                }
            )

    return events


def _select_card_header(
    case: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[str, str]:
    """Choose Feishu card header color template and title based on event urgency."""
    event_types = {e.get("type") for e in events}
    state = str(case.get("state") or "")
    has_tse_reply = any(
        e.get("type") == "NEW_COMMENT" and e.get("author_type") == "GOOGLE_SUPPORT"
        for e in events
    )
    if "ESCALATED_CHANGED" in event_types:
        return "red", "工单升级状态更新 · Escalated"
    if state == "ACTION_REQUIRED":
        return "orange", "工单需您补充反馈 · Action Required"
    if state == "SOLUTION_PROVIDED":
        return "green", "工单已提供解决方案 · Solution Provided"
    if state == "CLOSED":
        return "grey", "工单已关闭 · Closed"
    if has_tse_reply:
        return "blue", "Google Cloud Support 工程师新回复"
    return "blue", "Google Cloud Support 工单动态更新"


def build_case_update_card(
    case: dict[str, Any],
    events: list[dict[str, Any]],
    owner_open_id: str | None = None,
) -> dict[str, Any]:
    """Build a Feishu interactive notification card. All untrusted case/comment text is plain_text."""
    template, title_prefix = _select_card_header(case, events)
    case_name = str(case.get("name") or case.get("case_name") or "")
    case_id = str(case.get("case_id") or (case_name.split("/")[-1] if "/" in case_name else ""))
    state = str(case.get("state") or "")
    header_lines = [
        f"工单资源名: {case_name}",
        f"标题: {str(case.get('display_name') or '')[:200]}",
        f"项目: {case.get('project_id', '')} | 严重级别: {case.get('severity', '')} ({case.get('priority', '')})",
        f"当前状态: {STATE_LABELS_ZH.get(state, state)}",
        f"更新时间: {case.get('update_time', '')}",
    ]
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(header_lines)}},
    ]

    if owner_open_id and _OPEN_ID_RE.match(owner_open_id):
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"关注人：<at id={owner_open_id}></at>"},
            }
        )

    elements.append({"tag": "hr"})

    change_lines: list[str] = []
    for ev in events[:6]:
        ev_type = ev.get("type")
        if ev_type == "NEW_COMMENT":
            tag = (
                "【Google Support 工程师】"
                if ev.get("author_type") == "GOOGLE_SUPPORT"
                else "【新增评论】"
            )
            body = str(ev.get("body") or "").strip()
            snippet = body[:500] + ("…" if len(body) > 500 else "")
            change_lines.append(f"{tag} {ev.get('author', '')} ({ev.get('create_time', '')}):\n{snippet}")
        else:
            change_lines.append(f"• {ev.get('summary', '')}")

    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "\n\n".join(change_lines) or "工单状态已更新。",
            },
        }
    )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "提示：可直接「引用回复」本卡片发送文字、报错截图或日志文件以回复工程师，也可直接要求改优先级、升级或关闭工单。",
                }
            ],
        }
    )

    suffix = f"#{case_id}" if case_id else ""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": f"{title_prefix} {suffix}".strip(),
            },
        },
        "elements": elements,
    }


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


class CaseUpdateMonitor:
    """Polls Google Cloud Support cases across ALLOWED_PROJECTS and pushes Feishu notifications."""

    def __init__(
        self,
        *,
        support_client_getter: Callable[[], Any] = get_support_client,
        feishu_api: Any = None,
        feishu_bot: Any = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        notify_chat_ids: list[str] | None = None,
        auto_notify_bot_chats: bool | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._support = support_client_getter
        self._api = feishu_api
        self._bot = feishu_bot
        self._webhook_url = (
            webhook_url
            if webhook_url is not None
            else os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        )
        self._webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
        )
        self._notify_chat_ids = (
            list(notify_chat_ids)
            if notify_chat_ids is not None
            else _env_list("FEISHU_NOTIFY_CHAT_IDS")
        )
        if auto_notify_bot_chats is None:
            raw_auto = os.getenv("FEISHU_AUTO_NOTIFY_BOT_CHATS", "true").strip().lower()
            self._auto_notify_bot_chats = raw_auto not in ("false", "0", "no")
        else:
            self._auto_notify_bot_chats = auto_notify_bot_chats

        if poll_interval_seconds is None:
            try:
                poll_interval_seconds = float(
                    os.getenv("CASE_POLL_INTERVAL_SECONDS", "60")
                )
            except ValueError:
                poll_interval_seconds = 60.0
        self.poll_interval_seconds = max(0.0, poll_interval_seconds)

        self._poll_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline_projects: set[str] = set()
        self._bot_chats_cache: tuple[float, list[str]] = (0.0, [])

    def attach_feishu_runtime(self, feishu_api: Any, feishu_bot: Any = None) -> None:
        """Bind active Feishu API/Bot instances once the long-connection thread starts."""
        self._api = feishu_api
        self._bot = feishu_bot

    def _discover_bot_chats(self) -> list[str]:
        if self._api is None or not hasattr(self._api, "list_bot_chats"):
            return []
        now = time.monotonic()
        cached_at, cached_ids = self._bot_chats_cache
        if cached_ids and now - cached_at < 300.0:
            return cached_ids
        try:
            chat_ids = [
                cid
                for cid in (self._api.list_bot_chats() or [])
                if _CHAT_ID_RE.match(cid)
            ]
        except Exception as exc:
            logger.warning("Failed to list Feishu bot chats for notifications: %s", type(exc).__name__)
            return cached_ids
        if self._bot is not None and getattr(self._bot, "allowed_chat_ids", None):
            allowed = self._bot.allowed_chat_ids
            chat_ids = [cid for cid in chat_ids if cid in allowed]
        else:
            env_allowed = set(_env_list("FEISHU_ALLOWED_CHAT_IDS"))
            if env_allowed:
                chat_ids = [cid for cid in chat_ids if cid in env_allowed]
        self._bot_chats_cache = (now, chat_ids)
        return chat_ids

    def resolve_targets(self, case_name: str) -> list[tuple[str, str]]:
        """Resolve `(chat_id, owner_open_id)` notification recipients for `case_name`."""
        support = self._support()
        targets: dict[str, str] = {}

        for sub in support.list_case_subscriptions(case_name=case_name, active_only=True):
            cid = str(sub.get("chat_id") or "").strip()
            if cid and _CHAT_ID_RE.match(cid):
                targets[cid] = str(sub.get("owner_open_id") or "").strip()

        for cid in self._notify_chat_ids:
            if _CHAT_ID_RE.match(cid) and cid not in targets:
                targets[cid] = ""

        if not targets and self._auto_notify_bot_chats:
            for cid in self._discover_bot_chats():
                targets.setdefault(cid, "")

        return list(targets.items())

    def _deliver_update(
        self, case: dict[str, Any], events: list[dict[str, Any]]
    ) -> int:
        """Deliver interactive card notifications to resolved Feishu chats and/or webhook."""
        case_name = str(case.get("name") or case.get("case_name") or "")
        summary_text = "; ".join(str(e.get("summary") or "") for e in events[:3])
        delivered = 0

        if self._api is not None and hasattr(self._api, "send_card"):
            for chat_id, owner_open_id in self.resolve_targets(case_name):
                card = build_case_update_card(case, events, owner_open_id=owner_open_id or None)
                try:
                    msg_id = self._api.send_card(chat_id, card)
                    delivered += 1
                    if self._bot is not None and hasattr(self._bot, "register_notification_message"):
                        self._bot.register_notification_message(
                            chat_id=chat_id,
                            message_id=msg_id,
                            case_name=case_name,
                            summary=summary_text,
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to send Feishu case update card to chat %s: %s",
                        chat_id,
                        type(exc).__name__,
                    )

        if self._webhook_url:
            card = build_case_update_card(case, events, owner_open_id=None)
            try:
                send_feishu_webhook_card(
                    self._webhook_url, card, webhook_secret=self._webhook_secret
                )
                delivered += 1
            except Exception as exc:
                logger.error("Failed to send Feishu webhook case update: %s", type(exc).__name__)

        return delivered

    def poll_once(self, project_id: str | None = None) -> dict[str, Any]:
        """Run one deterministic sync-and-notify pass over `project_id` or all ALLOWED_PROJECTS."""
        with self._poll_lock:
            support = self._support()
            projects = (
                [config.validate_project_access(project_id)]
                if project_id
                else config.get_allowed_projects()
            )
            checked_cases = 0
            seeded_cases = 0
            notified_events: list[dict[str, Any]] = []
            notifications_sent = 0

            for proj in projects:
                try:
                    summaries = list(support.list_cases(project_id=proj, state_filter="ALL"))
                except Exception as exc:
                    logger.warning(
                        "CaseUpdateMonitor failed listing cases for %s: %s",
                        proj,
                        type(exc).__name__,
                    )
                    continue

                seen_names = {str(s.get("name") or "").strip() for s in summaries}
                if hasattr(support, "list_case_subscriptions"):
                    try:
                        for sub in support.list_case_subscriptions(active_only=True):
                            sub_case = str(sub.get("case_name") or "").strip()
                            if (
                                sub_case
                                and sub_case.startswith(f"projects/{proj}/cases/")
                                and sub_case not in seen_names
                            ):
                                summaries.append({"name": sub_case})
                                seen_names.add(sub_case)
                    except Exception as exc:
                        logger.debug("Subscription lookup skipped during poll: %s", type(exc).__name__)

                for summary in summaries:
                    case_name = str(summary.get("name") or "").strip()
                    if not case_name:
                        continue
                    checked_cases += 1
                    watch = support.get_case_watch_state(case_name)

                    # Optimization: skip full comment/attachment fetch for already-watched CLOSED
                    # cases whose state and update_time have not changed.
                    if (
                        watch is not None
                        and summary.get("state") == "CLOSED"
                        and watch.get("last_state") == "CLOSED"
                        and summary.get("update_time") == watch.get("last_update_time")
                    ):
                        continue

                    try:
                        full_case = support.get_case(case_name)
                    except Exception as exc:
                        logger.warning(
                            "CaseUpdateMonitor failed fetching %s: %s",
                            case_name,
                            type(exc).__name__,
                        )
                        continue

                    if watch is None:
                        # Cold-start / newly discovered case without prior baseline:
                        # If we already completed initial cold-start baseline for this project AND
                        # this is a brand-new active case created externally (e.g. in GCP Console),
                        # notify once as a new case event; otherwise seed silently on restart.
                        if proj in self._baseline_projects and full_case.get("state") != "CLOSED":
                            new_events = [
                                {
                                    "type": "CASE_CREATED_EXTERNAL",
                                    "summary": (
                                        f"检测到新工单已创建（状态: "
                                        f"{STATE_LABELS_ZH.get(str(full_case.get('state') or ''), str(full_case.get('state') or ''))}）"
                                    ),
                                }
                            ]
                            sent = self._deliver_update(full_case, new_events)
                            notifications_sent += sent
                            notified_events.append(
                                {
                                    "case_name": case_name,
                                    "events": new_events,
                                    "delivered": sent,
                                }
                            )
                        support.record_case_watch_snapshot(full_case)
                        seeded_cases += 1
                        continue

                    events = diff_case_updates(watch, full_case)
                    support.record_case_watch_snapshot(full_case)
                    if events:
                        sent = self._deliver_update(full_case, events)
                        notifications_sent += sent
                        notified_events.append(
                            {
                                "case_name": case_name,
                                "events": events,
                                "delivered": sent,
                            }
                        )

                self._baseline_projects.add(proj)

            return {
                "projects_polled": projects,
                "checked_cases": checked_cases,
                "seeded_cases": seeded_cases,
                "updated_cases": len(notified_events),
                "notifications_sent": notifications_sent,
                "updates": notified_events,
            }

    def start_background(self) -> threading.Thread | None:
        """Start the background polling daemon if `poll_interval_seconds > 0`."""
        if self.poll_interval_seconds <= 0:
            logger.info("CaseUpdateMonitor disabled (CASE_POLL_INTERVAL_SECONDS <= 0).")
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        self._stop_event.clear()

        def _loop() -> None:
            logger.info(
                "CaseUpdateMonitor started (interval=%ss).",
                self.poll_interval_seconds,
            )
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as exc:
                    logger.error("CaseUpdateMonitor loop error: %s", type(exc).__name__)
                self._stop_event.wait(self.poll_interval_seconds)

        self._thread = threading.Thread(
            target=_loop,
            name="case-update-monitor",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop_event.set()


_monitor_lock = threading.Lock()
_monitor_instance: CaseUpdateMonitor | None = None


def get_case_update_monitor() -> CaseUpdateMonitor:
    """Return the singleton `CaseUpdateMonitor` instance."""
    global _monitor_instance
    with _monitor_lock:
        if _monitor_instance is None:
            _monitor_instance = CaseUpdateMonitor()
        return _monitor_instance


def set_case_update_monitor(monitor: CaseUpdateMonitor | None) -> None:
    """Override the singleton `CaseUpdateMonitor` (used in unit tests)."""
    global _monitor_instance
    with _monitor_lock:
        _monitor_instance = monitor
