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

"""Comprehensive end-to-end tests for all real-world customer case submission & reply scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import config
from app.agent import guard_tool_execution, run_deterministic_agent_turn
from app.case_notifier import CaseUpdateMonitor, set_case_update_monitor
from app.feishu_bot import FeishuBot
from app.support_client import CloudSupportClient, set_support_client
from app.tools import (
    prepare_case_draft,
    prepare_followup_case_draft,
    update_support_case,
)

BOT_OPEN_ID = "ou_bot_case_agent"
OWNER_OPEN_ID = "ou_customer_sre01"


class _SyncExecutor:
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)


@pytest.fixture()
def scenario_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated MOCK support environment with FeishuBot + CaseUpdateMonitor wired end-to-end."""
    monkeypatch.setenv(
        "ALLOWED_PROJECTS",
        "gcp-prod-core-01,gcp-analytics-data-02,gcp-ai-sandbox-03",
    )
    monkeypatch.setenv("FORCE_TEST_CASE", "true")
    monkeypatch.setenv("SUPPORT_API_MODE", "MOCK")
    monkeypatch.setenv("DEFAULT_SUBSCRIBER_EMAILS", "")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    config.set_support_mode("MOCK")

    client = CloudSupportClient(db_path=tmp_path / "scenarios.db")
    set_support_client(client)

    monitor = CaseUpdateMonitor(
        support_client_getter=lambda: client,
        poll_interval_seconds=0,
    )
    set_case_update_monitor(monitor)

    api_mock = MagicMock()
    _seq = {"n": 0}

    def _next_mid(*_a: Any, **_k: Any) -> str:
        _seq["n"] += 1
        return f"om_bot_msg_{_seq['n']}"

    api_mock.reply_markdown.side_effect = _next_mid
    api_mock.reply_text.side_effect = _next_mid
    api_mock.reply_card.side_effect = _next_mid
    api_mock.send_card.side_effect = _next_mid

    def _turn_runner(
        user_id: str,
        session_id: str,
        text: str,
        project: str,
        selected_case: str | None,
        context_note: str | None = None,
        inline_attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        res = run_deterministic_agent_turn(
            message=text,
            active_project=project,
            selected_case_name=selected_case,
        )
        res["new_draft_ids"] = [
            d["draft_id"] for d in res.get("pending_drafts", []) if d.get("draft_id")
        ]
        return res

    bot = FeishuBot(
        api_mock,
        _turn_runner,
        executor=_SyncExecutor(),
        support_client_getter=lambda: client,
        error_mapper=lambda exc: SimpleNamespace(status_code=400, detail=str(exc)),
        bot_open_id=BOT_OPEN_ID,
        default_project="gcp-prod-core-01",
        patch_delay_seconds=0,
        reply_classifier=None,
    )
    monitor.attach_feishu_runtime(api_mock, bot)
    monitor.poll_once("gcp-prod-core-01")
    monitor.poll_once("gcp-analytics-data-02")

    yield client, bot, monitor, api_mock

    set_case_update_monitor(None)
    set_support_client(None)


def _feishu_event(
    text: str = "",
    *,
    msg_id: str = "om_u_1",
    chat_id: str = "oc_group_1",
    chat_type: str = "group",
    open_id: str = OWNER_OPEN_ID,
    msg_type: str = "text",
    raw_content: dict[str, Any] | None = None,
    at_bot: bool = True,
    parent_id: str = "",
) -> Any:
    mentions = []
    payload_text = text
    if at_bot and chat_type != "p2p" and msg_type in ("text", "post"):
        mentions = [
            SimpleNamespace(
                key="@_user_1",
                id=SimpleNamespace(open_id=BOT_OPEN_ID),
                mentioned_type="bot",
            )
        ]
        if msg_type == "text":
            payload_text = f"@_user_1 {text}"
    if raw_content is not None:
        content_str = json.dumps(raw_content, ensure_ascii=False)
    else:
        content_str = json.dumps({"text": payload_text}, ensure_ascii=False)
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=open_id), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id=msg_id,
                parent_id=parent_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type=msg_type,
                content=content_str,
                mentions=mentions,
            ),
        )
    )


def test_s1_duplicate_detection_and_in_place_draft_revision(scenario_env) -> None:
    """Scenarios 1, 2 & 3: Duplicate active-case alert, S1 24x7 tip, and in-place draft card revision."""
    client, bot, _, api_mock = scenario_env

    # 1. Create S1 Cloud SQL connectivity draft -> hits duplicate #84920101 + S1 tip
    bot.handle_message_event(
        _feishu_event(
            "帮我提一个 S1 工单：生产环境 Cloud SQL PostgreSQL 主备切换后连接超时完全不可用",
            msg_id="om_s1_create",
        )
    )
    assert api_mock.reply_card.call_count == 1
    first_card = api_mock.reply_card.call_args.args[1]
    card_body = first_card["elements"][0]["text"]["content"]
    assert "#84920101" in card_body  # Duplicate warning on card
    md_reply = api_mock.reply_markdown.call_args.args[1]
    assert "防重提醒" in md_reply
    assert "7×24" in md_reply

    action_elem = next(e for e in first_card["elements"] if e.get("tag") == "action")
    draft_id = action_elem["actions"][0]["value"]["draft_id"]

    # 2. User revises the pending draft BEFORE clicking confirm: downgrade to S2 + add CC + UTC
    bot.handle_message_event(
        _feishu_event(
            f"把草稿 {draft_id} 改成 S2，抄送邮箱加上 oncall@company.com，时区 UTC",
            msg_id="om_s1_revise",
            at_bot=False,  # follow-up in group without @bot!
        )
    )
    # No duplicate second draft card created; original card patched in-place!
    assert api_mock.reply_card.call_count == 1
    assert api_mock.patch_card.call_count == 1
    patched_card = api_mock.patch_card.call_args.args[1]
    patched_body = patched_card["elements"][0]["text"]["content"]
    assert "S2 (P2)" in patched_body
    assert "o***@company.com" in patched_body
    revised_draft = client.get_draft(draft_id)
    assert revised_draft["payload"]["time_zone"] == "UTC"
    assert revised_draft["payload"]["subscriber_emails"] == ["oncall@company.com"]


def test_s4_unauthorized_project_guardrail_across_all_new_tools(scenario_env) -> None:
    """Scenario 4: Unauthorized project access is blocked across all new and existing tools."""
    _ = scenario_env
    for tool_fn, bad_args in [
        (
            prepare_case_draft,
            {
                "project_id": "evil-project-999",
                "display_name": "Test",
                "description": " Unauthorized project test ",
                "severity": "S3",
                "classification_id": "id",
                "classification_display_name": "name",
                "subscriber_emails": "",
                "time_zone": "UTC",
            },
        ),
        (
            update_support_case,
            {"case_name": "projects/evil-project-999/cases/84920101", "severity": "S1"},
        ),
        (
            prepare_followup_case_draft,
            {
                "reference_case_name": "projects/evil-project-999/cases/84920103",
                "followup_description": "Reopen attempt on unauthorized project",
            },
        ),
    ]:
        res = guard_tool_execution(tool_fn, bad_args)
        assert res is not None
        assert res["status"] == "blocked_by_security_guardrail"
        assert res["reason"] == "PROJECT_NOT_ALLOWED"


def test_s5_update_active_case_priority_and_append_subscribers(scenario_env) -> None:
    """Scenario 5: Update severity (S3 -> S1) and append CC emails without overwriting existing CCs."""
    client, bot, _, _ = scenario_env
    case_name = "projects/gcp-prod-core-01/cases/84920102"
    before = client.get_case(case_name)
    assert before["severity"] == "S3"
    assert len(before["subscriber_emails"]) == 1  # p***@example.com

    bot.handle_message_event(
        _feishu_event(
            "把工单 84920102 的严重级别调高到 S1，并把 netops@example.com 加到订阅邮箱",
            msg_id="om_upd_prio",
        )
    )
    after = client.get_case(case_name)
    assert after["severity"] == "S1"
    assert after["priority"] == "P1"
    assert len(after["subscriber_emails"]) == 2
    assert any(e.startswith("n***@") for e in after["subscriber_emails"])


def test_s6_to_s9_all_case_reply_modes_including_quote_and_rich_post(scenario_env) -> None:
    """Scenarios 6, 7, 8 & 9: Explicit reply, quote-reply without ID/keywords, active-case follow-up, and text+image post reply."""
    client, bot, monitor, api_mock = scenario_env
    case_name = "projects/gcp-prod-core-01/cases/84920101"
    client.subscribe_case(case_name, "oc_group_1", owner_open_id=OWNER_OPEN_ID)
    client.record_case_watch_snapshot(client.get_case(case_name))

    # Scenario 6: Explicit case ID reply -> no self-echo on poll
    bot.handle_message_event(
        _feishu_event(
            "给工单 84920101 回复：我们已将连接池 idle timeout 调整为 30 秒",
            msg_id="om_rep_explicit",
        )
    )
    c_after_6 = client.get_case(case_name)
    assert "30 秒" in c_after_6["comments"][-1]["body"]
    assert monitor.poll_once("gcp-prod-core-01")["notifications_sent"] == 0

    # Simulate Google Support TSE asking a diagnostic question & changing state to ACTION_REQUIRED
    with client._get_conn() as conn:
        conn.execute(
            "UPDATE cases SET state = 'ACTION_REQUIRED', update_time = '2026-11-10T15:00:00Z' WHERE case_name = ?",
            (case_name,),
        )
        conn.execute(
            "INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cmt-tse-ask-901",
                case_name,
                "Please confirm your PostgreSQL minor version and attach connection pool metrics.",
                "Google Cloud Support TSE",
                "GOOGLE_SUPPORT",
                "2026-11-10T15:00:00Z",
            ),
        )
        conn.commit()

    poll_res = monitor.poll_once("gcp-prod-core-01")
    assert poll_res["notifications_sent"] == 1
    notif_card_msg_id = api_mock.send_card.return_value or f"om_bot_msg_{api_mock.send_card.call_count + 1}"
    # Grab exact message_id registered on bot
    notif_msg_ids = [mid for mid, cname in bot._message_cases.items() if cname == case_name]
    assert notif_msg_ids
    notif_card_msg_id = notif_msg_ids[-1]

    # Scenario 7: User quote-replies the TSE notification card with pure natural answer
    # (NO @bot, NO case ID, NO word "评论"/"回复"!) -> posts comment & transitions back to IN_PROGRESS_GOOGLE_SUPPORT!
    bot.handle_message_event(
        _feishu_event(
            "数据库小版本是 PostgreSQL 15.6，Private Service Connect 已启用",
            msg_id="om_quote_natural",
            at_bot=False,
            parent_id=notif_card_msg_id,
        )
    )
    c_after_7 = client.get_case(case_name)
    assert "PostgreSQL 15.6" in c_after_7["comments"][-1]["body"]
    assert c_after_7["state"] == "IN_PROGRESS_GOOGLE_SUPPORT"
    assert monitor.poll_once("gcp-prod-core-01")["notifications_sent"] == 0

    # Scenario 8: Next message in conversation without quoting and without case ID ("补充：发生在 us-central1-a")
    bot.handle_message_event(
        _feishu_event(
            "补充：故障主要集中在 us-central1-a 可用区的只读副本",
            msg_id="om_active_followup",
            at_bot=False,
        )
    )
    c_after_8 = client.get_case(case_name)
    assert "us-central1-a" in c_after_8["comments"][-1]["body"]

    # Scenario 9: Quote-replying with rich-text `post` containing BOTH text AND screenshot!
    valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-metrics-chart"
    api_mock.download_message_resource.return_value = (valid_png, None)
    att_before = len(c_after_8["attachments"])
    cmt_before = len(c_after_8["comments"])

    post_reply = {
        "content": [
            [
                {"tag": "text", "text": "回复：附上最新的连接池活跃连接数监控图 "},
                {"tag": "img", "image_key": "img_v3_metrics_001"},
            ]
        ]
    }
    bot.handle_message_event(
        _feishu_event(
            msg_id="om_quote_post_combo",
            msg_type="post",
            raw_content=post_reply,
            at_bot=False,
            parent_id=notif_card_msg_id,
        )
    )
    c_after_9 = client.get_case(case_name)
    assert len(c_after_9["attachments"]) == att_before + 1
    # 1 synthetic attachment log comment + 1 customer text comment = +2 comments
    assert len(c_after_9["comments"]) == cmt_before + 2
    assert any("活跃连接数监控图" in c["body"] for c in c_after_9["comments"][-2:])


def test_s10_s11_solution_provided_reject_vs_accept_close(scenario_env) -> None:
    """Scenarios 10 & 11: Rejecting a solution resumes investigation; accepting generates HITL close card."""
    client, bot, _, api_mock = scenario_env
    case_name = "projects/gcp-prod-core-01/cases/84920102"
    with client._get_conn() as conn:
        conn.execute("UPDATE cases SET state = 'SOLUTION_PROVIDED' WHERE case_name = ?", (case_name,))
        conn.commit()

    # Customer says solution didn't fix it -> resumes IN_PROGRESS_GOOGLE_SUPPORT
    bot.handle_message_event(
        _feishu_event(
            "给工单 84920102 回复：按方案扩容后今天还是报错 GCE_STOCKOUT，未解决请继续排查",
            msg_id="om_sol_reject",
        )
    )
    assert client.get_case(case_name)["state"] == "IN_PROGRESS_GOOGLE_SUPPORT"

    # Put back in SOLUTION_PROVIDED, now customer accepts solution and asks to close
    with client._get_conn() as conn:
        conn.execute("UPDATE cases SET state = 'SOLUTION_PROVIDED' WHERE case_name = ?", (case_name,))
        conn.commit()

    cards_before = api_mock.reply_card.call_count
    bot.handle_message_event(
        _feishu_event(
            "工单 84920102 方案有效，问题已解决，请关闭工单",
            msg_id="om_sol_accept_close",
        )
    )
    # Still not closed until human clicks confirm!
    assert client.get_case(case_name)["state"] == "SOLUTION_PROVIDED"
    assert api_mock.reply_card.call_count == cards_before + 1
    close_card = api_mock.reply_card.call_args.args[1]
    close_draft_id = next(
        e for e in close_card["elements"] if e.get("tag") == "action"
    )["actions"][0]["value"]["draft_id"]

    confirm_ev = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=OWNER_OPEN_ID),
            action=SimpleNamespace(value={"action": "confirm", "draft_id": close_draft_id}),
            context=SimpleNamespace(open_message_id="om_close_card_1", open_chat_id="oc_group_1"),
        )
    )
    bot.handle_card_action(confirm_ev)
    assert client.get_case(case_name)["state"] == "CLOSED"


def test_s12_closed_case_reply_and_reopen_creates_linked_followup_draft(scenario_env) -> None:
    """Scenario 12: Replying to or reopening a CLOSED case creates a linked follow-up CREATE_CASE draft."""
    client, bot, _, api_mock = scenario_env
    closed_case = "projects/gcp-analytics-data-02/cases/84920103"
    assert client.get_case(closed_case)["state"] == "CLOSED"

    # Attempt to reply to a CLOSED case -> automatically offers & creates linked follow-up draft!
    bot.handle_message_event(
        _feishu_event(
            f"给工单 {closed_case} 回复：今天凌晨 01:30 BigQuery Slot Contention 又复发了",
            msg_id="om_closed_reply",
        )
    )
    assert api_mock.reply_card.call_count == 1
    followup_card = api_mock.reply_card.call_args.args[1]
    card_text = followup_card["elements"][0]["text"]["content"]
    assert "关联历史工单: projects/gcp-analytics-data-02/cases/84920103" in card_text
    assert "[Follow-up #84920103]" in card_text

    draft_id = next(
        e for e in followup_card["elements"] if e.get("tag") == "action"
    )["actions"][0]["value"]["draft_id"]
    confirm_ev = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=OWNER_OPEN_ID),
            action=SimpleNamespace(value={"action": "confirm", "draft_id": draft_id}),
            context=SimpleNamespace(open_message_id="om_fup_card", open_chat_id="oc_group_1"),
        )
    )
    bot.handle_card_action(confirm_ev)
    new_case_name = client.get_draft(draft_id)["result_case_name"]
    new_case = client.get_case(new_case_name)
    assert new_case["state"] == "OPEN"
    assert new_case["project_id"] == "gcp-analytics-data-02"
    assert "84920103" in new_case["description"]


def test_s13_s14_tse_remote_attachment_notification_and_rich_case_details(scenario_env) -> None:
    """Scenarios 13 & 14: Remote TSE attachment triggers Feishu card; case details show latest TSE reply."""
    client, bot, monitor, api_mock = scenario_env
    case_name = "projects/gcp-prod-core-01/cases/84920101"
    client.subscribe_case(case_name, "oc_group_1", owner_open_id=OWNER_OPEN_ID)
    client.record_case_watch_snapshot(client.get_case(case_name))

    # Simulate Google Support TSE uploading a diagnostic PDF externally
    with client._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO attachments (
                attachment_id, case_name, original_filename,
                stored_filename, mime_type, size_bytes, create_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "att-tse-rca-001",
                case_name,
                "cloudsql_ha_rca_report.pdf",
                "",
                "application/pdf",
                128400,
                "2026-11-10T15:10:00Z",
            ),
        )
        conn.execute(
            "UPDATE cases SET update_time = '2026-11-10T15:10:00Z' WHERE case_name = ?",
            (case_name,),
        )
        conn.commit()

    poll_res = monitor.poll_once("gcp-prod-core-01")
    assert poll_res["notifications_sent"] == 1
    sent_card = api_mock.send_card.call_args.args[1]
    assert "cloudsql_ha_rca_report.pdf" in json.dumps(sent_card, ensure_ascii=False)

    # Scenario 14: Query case details -> displays latest TSE comment AND attachment filename!
    bot.handle_message_event(
        _feishu_event("查看工单 84920101 详情", msg_id="om_detail_rich")
    )
    detail_md = api_mock.reply_markdown.call_args.args[1]
    assert "cloudsql_ha_rca_report.pdf" in detail_md
    assert "tcp_keepalives_idle" in detail_md
