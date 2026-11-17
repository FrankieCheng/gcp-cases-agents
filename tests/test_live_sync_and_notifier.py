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

"""Comprehensive tests for LIVE Support API v2 sync, Feishu case update notifier, and security."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gcp_exceptions

from app import config, support_client
from app.agent import run_deterministic_agent_turn
from app.case_notifier import (
    CaseUpdateMonitor,
    build_case_update_card,
    send_feishu_webhook_card,
    set_case_update_monitor,
    validate_feishu_webhook_url,
)
from app.fast_api_app import app
from app.feishu_bot import FeishuBot
from app.support_client import CloudSupportClient, set_support_client

BOT_OPEN_ID = "ou_bot0000001"
OWNER_OPEN_ID = "ou_owner000001"


class _SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite DB and restore MOCK mode after each test."""
    monkeypatch.setenv(
        "ALLOWED_PROJECTS",
        "gcp-prod-core-01,gcp-analytics-data-02,gcp-ai-sandbox-03",
    )
    monkeypatch.setenv("FORCE_TEST_CASE", "true")
    monkeypatch.delenv("FEISHU_NOTIFY_CHAT_IDS", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK_SECRET", raising=False)
    config.set_support_mode("MOCK")
    support_client._live_catalog = []
    support_client._live_catalog_loaded_at = 0.0
    db_file = tmp_path / "live_sync_test.db"
    client = CloudSupportClient(db_path=db_file)
    set_support_client(client)
    monitor = CaseUpdateMonitor(support_client_getter=lambda: client)
    set_case_update_monitor(monitor)
    yield client, monitor
    config.set_support_mode("MOCK")
    support_client._live_catalog = []
    support_client._live_catalog_loaded_at = 0.0


def _fake_remote_case(
    name: str = "projects/gcp-prod-core-01/cases/9123456789",
    display_name: str = "[LIVE] Cloud SQL connection spike",
    description: str = "Production Cloud SQL PostgreSQL connections timing out.",
    state_name: str = "NEW",
    priority_name: str = "P2",
    cls_id: str = "cls-cloud-sql-01",
    cls_display: str = "Database > Cloud SQL > Connectivity",
    escalated: bool = False,
    test_case: bool = True,
    subscribers: list[str] | None = None,
    tz: str = "Asia/Shanghai",
    create_dt: datetime | None = None,
    update_dt: datetime | None = None,
) -> SimpleNamespace:
    c_dt = create_dt or datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
    u_dt = update_dt or datetime(2026, 11, 8, 12, 5, 0, tzinfo=UTC)
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        description=description,
        state=SimpleNamespace(name=state_name),
        priority=SimpleNamespace(name=priority_name),
        classification=SimpleNamespace(id=cls_id, display_name=cls_display),
        escalated=escalated,
        test_case=test_case,
        subscriber_email_addresses=subscribers if subscribers is not None else ["sre@example.com"],
        time_zone=tz,
        create_time=c_dt,
        update_time=u_dt,
    )


def _fake_remote_comment(
    case_name: str,
    comment_short_id: str,
    body: str,
    google_support: bool,
    actor_display_name: str = "Google Cloud Support TSE",
    actor_email: str = "",
    create_dt: datetime | None = None,
) -> SimpleNamespace:
    c_dt = create_dt or datetime(2026, 11, 8, 12, 10, 0, tzinfo=UTC)
    return SimpleNamespace(
        name=f"{case_name}/comments/{comment_short_id}",
        body=body,
        create_time=c_dt,
        creator=SimpleNamespace(
            display_name=actor_display_name,
            email=actor_email,
            google_support=google_support,
        ),
    )


def test_support_mode_symmetric_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Bug #1 fix: SUPPORT_API_MODE=LIVE in env can still be toggled to MOCK and back."""
    monkeypatch.setenv("SUPPORT_API_MODE", "LIVE")
    config._RUNTIME_MODE_OVERRIDE = None
    assert config.get_support_mode() == "LIVE"

    config.set_support_mode("MOCK")
    assert config.get_support_mode() == "MOCK"

    config.set_support_mode("LIVE")
    assert config.get_support_mode() == "LIVE"

    config.set_support_mode("MOCK")


def test_live_mode_create_case_payload_and_new_state(isolated_env) -> None:
    """Verify LIVE mode classification search and create_case payload without hitting real GCP."""
    client, _ = isolated_env
    config.set_support_mode("LIVE")

    fake_case_client = MagicMock()
    fake_case_client.search_case_classifications.return_value = [
        SimpleNamespace(
            id="100H41Q3-CLOUD-SQL-LIVE",
            display_name="Database > Cloud SQL > PostgreSQL Connectivity",
        )
    ]
    fake_case_client.create_case.return_value = _fake_remote_case(
        name="projects/gcp-prod-core-01/cases/9876543210",
        state_name="NEW",
        priority_name="P1",
        cls_id="100H41Q3-CLOUD-SQL-LIVE",
        cls_display="Database > Cloud SQL > PostgreSQL Connectivity",
        subscribers=[],
    )
    fake_case_client.get_case.return_value = fake_case_client.create_case.return_value

    fake_cmt_client = MagicMock()
    fake_cmt_client.list_comments.return_value = []
    fake_att_client = MagicMock()
    fake_att_client.list_attachments.return_value = []

    with (
        patch("google.cloud.support_v2.CaseServiceClient", return_value=fake_case_client),
        patch("google.cloud.support_v2.CommentServiceClient", return_value=fake_cmt_client),
        patch("google.cloud.support_v2.CaseAttachmentServiceClient", return_value=fake_att_client),
    ):
        cls_list = client.search_classifications("Cloud SQL PostgreSQL timeout")
        assert cls_list[0]["id"] == "100H41Q3-CLOUD-SQL-LIVE"

        draft = client.create_case_draft(
            project_id="gcp-prod-core-01",
            display_name="Production Cloud SQL timeout",
            description="Connections from GKE pods to Cloud SQL time out after 10s.",
            severity="S1",
            classification_id=cls_list[0]["id"],
            classification_display_name=cls_list[0]["display_name"],
            subscriber_emails="",
            time_zone="Asia/Shanghai",
        )
        assert draft["payload"]["mode"] == "LIVE"
        assert draft["payload"]["test_case"] is True
        assert draft["payload"]["subscriber_emails"] == []

        created = client.confirm_and_create_case(draft["draft_id"], confirmed=True)
        assert created["name"] == "projects/gcp-prod-core-01/cases/9876543210"
        assert created["state"] == "NEW"
        assert created["priority"] == "P1"
        assert created["severity"] == "S1"

        fake_case_client.create_case.assert_called_once()
        call_kwargs = fake_case_client.create_case.call_args.kwargs
        assert call_kwargs["parent"] == "projects/gcp-prod-core-01"
        sent_case = call_kwargs["case"]
        assert sent_case.display_name == "Production Cloud SQL timeout"
        assert sent_case.classification.id == "100H41Q3-CLOUD-SQL-LIVE"
        assert sent_case.test_case is True
        assert list(sent_case.subscriber_email_addresses) == []


def test_live_mode_list_and_get_case_sync_into_empty_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify LIVE mode list_cases and get_case sync remote GCP state into a fresh SQLite DB."""
    monkeypatch.setenv("ALLOWED_PROJECTS", "demo-core-prod")
    config.set_support_mode("LIVE")
    fresh_client = CloudSupportClient(db_path=tmp_path / "empty_container.db")

    remote_open = _fake_remote_case(
        name="projects/demo-core-prod/cases/10000001",
        display_name="New case from Cloud Console",
        state_name="NEW",
        priority_name="P0",  # P0 must safely map to S1
    )
    remote_escalated = _fake_remote_case(
        name="projects/demo-core-prod/cases/10000002",
        display_name="Escalated GKE case",
        state_name="IN_PROGRESS_GOOGLE_SUPPORT",
        priority_name="P2",
        escalated=True,
    )
    remote_closed = _fake_remote_case(
        name="projects/demo-core-prod/cases/10000003",
        display_name="Closed BigQuery case",
        state_name="CLOSED",
        priority_name="P3",
    )

    fake_case_client = MagicMock()
    fake_case_client.list_cases.return_value = [remote_open, remote_escalated, remote_closed]
    fake_case_client.get_case.return_value = remote_open

    fake_comment_client = MagicMock()
    fake_comment_client.list_comments.return_value = [
        _fake_remote_comment(
            "projects/demo-core-prod/cases/10000001",
            "cmt-cust-1",
            "Initial customer log snippet",
            google_support=False,
            actor_display_name="",
            actor_email="frankie@example.com",
            create_dt=datetime(2026, 11, 8, 12, 1, 0, tzinfo=UTC),
        ),
        _fake_remote_comment(
            "projects/demo-core-prod/cases/10000001",
            "cmt-tse-2",
            "Hello! Google Support TSE investigating now.",
            google_support=True,
            actor_display_name="Google Cloud Support",
            create_dt=datetime(2026, 11, 8, 12, 4, 0, tzinfo=UTC),
        ),
    ]

    fake_att_client = MagicMock()
    fake_att_client.list_attachments.return_value = [
        SimpleNamespace(
            name="projects/demo-core-prod/cases/10000001/attachments/att-remote-77",
            filename="tcpdump_summary.txt",
            mime_type="text/plain",
            size_bytes=2048,
            create_time=datetime(2026, 11, 8, 12, 3, 0, tzinfo=UTC),
        )
    ]

    try:
        with (
            patch("google.cloud.support_v2.CaseServiceClient", return_value=fake_case_client),
            patch("google.cloud.support_v2.CommentServiceClient", return_value=fake_comment_client),
            patch(
                "google.cloud.support_v2.CaseAttachmentServiceClient",
                return_value=fake_att_client,
            ),
        ):
            open_cases = fresh_client.list_cases("demo-core-prod", state_filter="OPEN")
            assert {c["name"] for c in open_cases} == {
                "projects/demo-core-prod/cases/10000001",
                "projects/demo-core-prod/cases/10000002",
            }
            p0_case = next(c for c in open_cases if c["name"].endswith("10000001"))
            assert p0_case["severity"] == "S1"

            esc_cases = fresh_client.list_cases("demo-core-prod", state_filter="ESCALATED")
            assert [c["name"] for c in esc_cases] == ["projects/demo-core-prod/cases/10000002"]

            closed_cases = fresh_client.list_cases("demo-core-prod", state_filter="CLOSED")
            assert [c["name"] for c in closed_cases] == ["projects/demo-core-prod/cases/10000003"]

            full_case = fresh_client.get_case("projects/demo-core-prod/cases/10000001")
            assert len(full_case["comments"]) == 2
            assert full_case["comments"][0]["author_type"] == "CUSTOMER"
            assert full_case["comments"][0]["author"] == "f***@example.com"
            assert full_case["comments"][1]["author_type"] == "GOOGLE_SUPPORT"
            assert full_case["comments"][1]["comment_id"] == "cmt-tse-2"
            assert len(full_case["attachments"]) == 1
            assert full_case["attachments"][0]["attachment_id"] == "att-remote-77"

            # Verify NotFound maps to KeyError (404 in FastAPI)
            fake_case_client.get_case.side_effect = gcp_exceptions.NotFound("Case not found")
            with pytest.raises(KeyError):
                fresh_client.get_case("projects/demo-core-prod/cases/99999999")
    finally:
        config.set_support_mode("MOCK")


def test_live_mode_add_comment_canonical_id_and_no_self_echo(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify customer add_comment uses canonical remote ID, avoids duplicates, and suppresses self-echo."""
    client, monitor = isolated_env
    monkeypatch.setenv("FEISHU_NOTIFY_CHAT_IDS", "oc_ops_group")
    monitor._notify_chat_ids = ["oc_ops_group"]
    case_name = "projects/gcp-prod-core-01/cases/84920101"

    # Seed baseline first so CaseUpdateMonitor watches existing cases
    api_mock = MagicMock()
    api_mock.send_card.return_value = "om_notif_init"
    monitor.attach_feishu_runtime(api_mock, None)
    seed_summary = monitor.poll_once("gcp-prod-core-01")
    assert seed_summary["seeded_cases"] >= 1
    assert seed_summary["notifications_sent"] == 0

    config.set_support_mode("LIVE")
    remote_case = _fake_remote_case(
        name=case_name,
        state_name="IN_PROGRESS_GOOGLE_SUPPORT",
        priority_name="P2",
        update_dt=datetime(2026, 11, 8, 15, 0, 0, tzinfo=UTC),
    )
    created_remote_cmt = _fake_remote_comment(
        case_name,
        "remote-cmt-555",
        "Uploaded pg_stat_activity snapshot.",
        google_support=False,
        actor_display_name="Customer Engineer",
        create_dt=datetime(2026, 11, 8, 15, 0, 0, tzinfo=UTC),
    )

    fake_case_client = MagicMock()
    fake_case_client.list_cases.return_value = [remote_case]
    fake_case_client.get_case.return_value = remote_case

    fake_cmt_client = MagicMock()
    fake_cmt_client.create_comment.return_value = created_remote_cmt
    fake_cmt_client.list_comments.return_value = [created_remote_cmt]

    fake_att_client = MagicMock()
    fake_att_client.list_attachments.return_value = []

    with (
        patch("google.cloud.support_v2.CaseServiceClient", return_value=fake_case_client),
        patch("google.cloud.support_v2.CommentServiceClient", return_value=fake_cmt_client),
        patch("google.cloud.support_v2.CaseAttachmentServiceClient", return_value=fake_att_client),
    ):
        saved_cmt = client.add_comment(case_name, "Uploaded pg_stat_activity snapshot.")
        assert saved_cmt["comment_id"] == "remote-cmt-555"

        refreshed = client.get_case(case_name)
        matching = [c for c in refreshed["comments"] if c["comment_id"] == "remote-cmt-555"]
        assert len(matching) == 1

        # Poll should NOT notify on the customer's own comment
        poll_res = monitor.poll_once("gcp-prod-core-01")
        assert poll_res["notifications_sent"] == 0
        api_mock.send_card.assert_not_called()


def test_live_mode_update_escalate_close_and_attachment_upload(isolated_env) -> None:
    """Verify LIVE mode update_case FieldMask, escalate_case, close_case, and media upload."""
    client, _ = isolated_env
    case_name = "projects/gcp-prod-core-01/cases/84920102"
    esc_draft = client.create_escalation_draft(
        case_name=case_name,
        reason="TECHNICAL_EXPERTISE",
        justification="Production GKE cluster autoscaler remains blocked for 2 hours.",
    )

    config.set_support_mode("LIVE")
    fake_case_client = MagicMock()
    fake_case_client.get_case.return_value = _fake_remote_case(
        name=case_name,
        state_name="IN_PROGRESS_GOOGLE_SUPPORT",
        priority_name="P3",
    )
    fake_case_client.update_case.return_value = _fake_remote_case(
        name=case_name,
        state_name="IN_PROGRESS_GOOGLE_SUPPORT",
        priority_name="P1",
        subscribers=["lead@example.com"],
    )
    fake_case_client.escalate_case.return_value = _fake_remote_case(
        name=case_name,
        state_name="IN_PROGRESS_GOOGLE_SUPPORT",
        priority_name="P1",
        escalated=True,
    )
    fake_case_client.close_case.return_value = _fake_remote_case(
        name=case_name,
        state_name="CLOSED",
        priority_name="P1",
        escalated=True,
    )
    fake_cmt_client = MagicMock()
    fake_cmt_client.list_comments.return_value = []
    fake_att_client = MagicMock()
    fake_att_client.list_attachments.return_value = []

    with (
        patch("google.cloud.support_v2.CaseServiceClient", return_value=fake_case_client),
        patch("google.cloud.support_v2.CommentServiceClient", return_value=fake_cmt_client),
        patch("google.cloud.support_v2.CaseAttachmentServiceClient", return_value=fake_att_client),
        patch.object(
            client,
            "_upload_live_attachment",
            return_value={
                "name": f"{case_name}/attachments/live-att-900",
                "createTime": "2026-11-08T15:10:00Z",
            },
        ) as mock_upload,
    ):
        updated = client.update_case(
            case_name,
            severity="S1",
            subscriber_emails="lead@example.com",
        )
        assert updated["severity"] == "S1"
        update_kwargs = fake_case_client.update_case.call_args.kwargs
        assert list(update_kwargs["request"].update_mask.paths) == [
            "priority",
            "subscriber_email_addresses",
        ]

        escalated = client.confirm_and_escalate_case(esc_draft["draft_id"], confirmed=True)
        assert escalated["escalated"] is True
        fake_case_client.escalate_case.assert_called_once()

        att = client.add_attachment_metadata(
            case_name=case_name,
            original_filename="kubectl_describe.txt",
            stored_filename="stored_kubectl.txt",
            mime_type="text/plain",
            size_bytes=16,
            content_bytes=b"node-pool-status",
        )
        assert att["attachment_id"] == "live-att-900"
        mock_upload.assert_called_once()

        closed = client.close_case(case_name)
        assert closed["state"] == "CLOSED"
        fake_case_client.close_case.assert_called_once()
        assert fake_case_client.close_case.call_args.kwargs["request"].name == case_name


def test_case_update_monitor_cold_start_and_console_created_case(isolated_env) -> None:
    """Verify cold-start silent seeding and notification for externally created cases."""
    client, _ = isolated_env
    monitor = CaseUpdateMonitor(
        support_client_getter=lambda: client,
        notify_chat_ids=["oc_notify_room"],
    )

    api_mock = MagicMock()
    api_mock.send_card.return_value = "om_new_case_card"
    monitor.attach_feishu_runtime(api_mock, None)

    # 1. Cold start: seeds existing cases silently without spamming historical notifications
    first = monitor.poll_once("gcp-prod-core-01")
    assert first["seeded_cases"] >= 2
    assert first["notifications_sent"] == 0
    api_mock.send_card.assert_not_called()

    # 2. Simulate a brand-new case appearing externally (e.g., created in Google Cloud Console)
    external_case_name = "projects/gcp-prod-core-01/cases/9988776655"
    with client._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_name, case_id, project_id, display_name, description,
                severity, priority, state, classification_id, classification_display_name,
                time_zone, subscriber_emails_json, escalated, escalation_reason,
                escalation_justification, test_case, creator_name, create_time, update_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_case_name,
                "9988776655",
                "gcp-prod-core-01",
                "Console-created VPC BGP flap",
                "Cloud Router BGP session flapping in asia-east1.",
                "S2",
                "P2",
                "NEW",
                "cls-net-1",
                "Networking > Cloud Router",
                "Asia/Shanghai",
                "[]",
                0,
                "",
                "",
                1,
                "GCP Console",
                "2026-11-08T16:00:00Z",
                "2026-11-08T16:00:00Z",
            ),
        )
        conn.commit()

    second = monitor.poll_once("gcp-prod-core-01")
    assert second["updated_cases"] == 1
    assert second["notifications_sent"] == 1
    api_mock.send_card.assert_called_once()
    sent_chat_id, sent_card = api_mock.send_card.call_args.args
    assert sent_chat_id == "oc_notify_room"
    assert external_case_name in json.dumps(sent_card, ensure_ascii=False)


def test_end_to_end_feishu_create_tse_reply_notify_and_quote_reply_comment(isolated_env) -> None:
    """Verify full closed loop: Feishu create -> auto-subscribe -> TSE reply -> card notify -> user quote-reply adds comment."""
    client, monitor = isolated_env
    api_mock = MagicMock()
    api_mock.reply_card.return_value = "om_draft_card_1"
    api_mock.reply_text.return_value = "om_reply_txt_1"
    api_mock.send_card.return_value = "om_tse_update_card_99"

    def _turn_runner(
        user_id: str,
        session_id: str,
        message: str,
        project_id: str,
        selected_case_name: str | None,
        context_note: str | None = None,
    ) -> dict:
        inferred_case = selected_case_name
        if not inferred_case and context_note:
            matches = re.findall(r"projects/[a-z0-9\-]+/cases/\d+", context_note)
            if matches:
                inferred_case = matches[-1]
        res = run_deterministic_agent_turn(
            message=message,
            active_project=project_id,
            selected_case_name=inferred_case,
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
        allowed_chat_ids=[],
        patch_delay_seconds=0,
    )
    monitor.attach_feishu_runtime(api_mock, bot)

    # Seed pre-existing mock cases first
    monitor.poll_once("gcp-prod-core-01")

    # Step 1: User @mentions bot in Feishu group to report an issue
    create_msg_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_user_1",
                chat_id="oc_chat1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "@_user_1 帮我提一个 Cloud SQL 连接超时的 S2 工单"}),
                mentions=[
                    SimpleNamespace(
                        key="@_user_1",
                        id=SimpleNamespace(open_id=BOT_OPEN_ID),
                        mentioned_type="bot",
                    )
                ],
            ),
        )
    )
    bot.handle_message_event(create_msg_event)
    assert api_mock.reply_card.call_count == 1
    sent_draft_card = api_mock.reply_card.call_args.args[1]
    action_elem = next(e for e in sent_draft_card["elements"] if e.get("tag") == "action")
    draft_id = action_elem["actions"][0]["value"]["draft_id"]

    # Step 2: User clicks "Confirm" on the Feishu card
    card_confirm_event = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=OWNER_OPEN_ID),
            action=SimpleNamespace(value={"action": "confirm", "draft_id": draft_id}),
            context=SimpleNamespace(open_message_id="om_draft_card_1", open_chat_id="oc_chat1"),
        )
    )
    resp = bot.handle_card_action(card_confirm_event)
    assert resp["toast"]["content"] == "正在提交…"

    confirmed_draft = client.get_draft(draft_id)
    created_case_name = confirmed_draft["result_case_name"]
    assert created_case_name.startswith("projects/gcp-prod-core-01/cases/")

    # Auto-subscription should be active for (created_case_name, oc_chat1, OWNER_OPEN_ID)
    subs = client.list_case_subscriptions(created_case_name)
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "oc_chat1"
    assert subs[0]["owner_open_id"] == OWNER_OPEN_ID

    # Immediate poll right after creation produces 0 notifications (snapshot already seeded)
    assert monitor.poll_once("gcp-prod-core-01")["notifications_sent"] == 0

    # Step 3: Simulate Google Support TSE replying & transitioning state -> ACTION_REQUIRED, priority -> P1
    with client._get_conn() as conn:
        conn.execute(
            "UPDATE cases SET state = ?, priority = ?, severity = ?, update_time = ? WHERE case_name = ?",
            ("ACTION_REQUIRED", "P1", "S1", "2026-11-08T17:00:00Z", created_case_name),
        )
        conn.execute(
            """
            INSERT INTO comments (comment_id, case_name, author, author_type, body, create_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cmt-tse-live-901",
                created_case_name,
                "Google Cloud Support TSE",
                "GOOGLE_SUPPORT",
                "Hi team, please share pg_stat_activity output during the spike.",
                "2026-11-08T17:00:00Z",
            ),
        )
        conn.commit()

    # Step 4: CaseUpdateMonitor polls and dispatches Feishu interactive card notification
    poll_summary = monitor.poll_once("gcp-prod-core-01")
    assert poll_summary["updated_cases"] == 1
    assert poll_summary["notifications_sent"] == 1
    api_mock.send_card.assert_called_once()
    notif_chat_id, notif_card = api_mock.send_card.call_args.args
    assert notif_chat_id == "oc_chat1"
    notif_json = json.dumps(notif_card, ensure_ascii=False)
    assert f"<at id={OWNER_OPEN_ID}></at>" in notif_json
    assert "ACTION_REQUIRED" in notif_json
    assert "pg_stat_activity" in notif_json

    # Re-polling without new updates is idempotent (zero duplicate notifications)
    assert monitor.poll_once("gcp-prod-core-01")["notifications_sent"] == 0

    # Step 5: User quote-replies directly to the notification card WITHOUT @bot and WITHOUT case number!
    quote_reply_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_user_quote_2",
                parent_id="om_tse_update_card_99",
                chat_id="oc_chat1",
                chat_type="group",
                message_type="text",
                content=json.dumps(
                    {"text": "回复：已上传 pg_stat_activity 诊断结果，请协助继续排查"}
                ),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(quote_reply_event)

    updated_case = client.get_case(created_case_name)
    customer_replies = [
        c for c in updated_case["comments"] if "pg_stat_activity 诊断结果" in c["body"]
    ]
    assert len(customer_replies) == 1


def test_subscribe_and_unsubscribe_tools_and_rbac(isolated_env) -> None:
    """Verify explicit subscribe/unsubscribe chat commands in Feishu and RBAC enforcement."""
    client, _ = isolated_env
    api_mock = MagicMock()

    def _turn_runner(
        user_id, session_id, message, project_id, selected_case_name, context_note=None
    ):
        res = run_deterministic_agent_turn(
            message=message,
            active_project=project_id,
            selected_case_name=selected_case_name,
        )
        res["new_draft_ids"] = []
        return res

    bot = FeishuBot(
        api_mock,
        _turn_runner,
        executor=_SyncExecutor(),
        support_client_getter=lambda: client,
        error_mapper=lambda exc: SimpleNamespace(status_code=400, detail=str(exc)),
        bot_open_id=BOT_OPEN_ID,
        default_project="gcp-prod-core-01",
        allowed_chat_ids=[],
        patch_delay_seconds=0,
    )

    # Subscribe to 84920101 via Feishu DM
    sub_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_sub_1",
                chat_id="oc_dm_1",
                chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "订阅工单 84920101"}),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(sub_event)
    subs = client.list_case_subscriptions("projects/gcp-prod-core-01/cases/84920101")
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "oc_dm_1"

    # Unsubscribe from 84920101
    unsub_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_unsub_2",
                chat_id="oc_dm_1",
                chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "取消订阅 84920101"}),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(unsub_event)
    assert client.list_case_subscriptions("projects/gcp-prod-core-01/cases/84920101") == []

    # RBAC check: subscribing to an unauthorized project must fail safely
    evil_res = run_deterministic_agent_turn(
        message="订阅工单 projects/unauthorized-proj-99/cases/84920101",
        active_project="gcp-prod-core-01",
    )
    assert "Access denied" in evil_res["reply"] or "ALLOWED_PROJECTS" in evil_res["reply"]


def test_build_case_update_card_xss_and_open_id_sanitization() -> None:
    """Ensure untrusted case fields/comments use plain_text and open_ids are strictly validated."""
    malicious_case = {
        "name": "projects/gcp-prod-core-01/cases/84920101",
        "project_id": "gcp-prod-core-01",
        "display_name": "<script>alert('xss')</script> [SQL]",
        "state": "ACTION_REQUIRED",
        "severity": "S1",
        "priority": "P1",
        "escalated": True,
        "update_time": "2026-11-08T18:00:00Z",
    }
    events = [
        {
            "type": "NEW_COMMENT",
            "comment_id": "c1",
            "author": "TSE <img src=x onerror=alert(1)>",
            "author_type": "GOOGLE_SUPPORT",
            "create_time": "2026-11-08T18:00:00Z",
            "body": "Check <at id=all></at> & <script>evil()</script>",
        }
    ]
    # Invalid owner_open_id must NEVER be rendered into lark_md <at>
    card_bad_id = build_case_update_card(
        malicious_case,
        events,
        owner_open_id="ou_bad<script>",
    )
    for elem in card_bad_id["elements"]:
        if elem.get("tag") == "div" and "text" in elem:
            assert elem["text"]["tag"] == "plain_text"

    # Valid owner_open_id renders in its own lark_md element while untrusted text stays plain_text
    card_valid = build_case_update_card(
        malicious_case,
        events,
        owner_open_id="ou_validUser_01",
    )
    for elem in card_valid["elements"]:
        if elem.get("tag") == "div" and "text" in elem:
            txt_obj = elem["text"]
            content = txt_obj.get("content", "")
            if "<script>" in content or "onerror=" in content:
                assert txt_obj["tag"] == "plain_text"
            if txt_obj["tag"] == "lark_md":
                assert content == "关注人：<at id=ou_validUser_01></at>"


def test_ssrf_webhook_validation_and_hmac_signature() -> None:
    """Verify SSRF allowlist rejects non-Feishu/HTTP URLs and signs webhook payloads properly."""
    for bad_url in [
        "http://open.feishu.cn/open-apis/bot/v2/hook/abc",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost:8080/open-apis/bot/v2/hook/abc",
        "https://evil.example.com/open-apis/bot/v2/hook/abc",
        "https://user:pass@open.feishu.cn/open-apis/bot/v2/hook/abc",
        "https://open.feishu.cn/other/path",
    ]:
        with pytest.raises((ValueError, PermissionError)):
            validate_feishu_webhook_url(bad_url)

    valid_url = "https://open.feishu.cn/open-apis/bot/v2/hook/12345678-aaaa-bbbb"
    assert validate_feishu_webhook_url(valid_url) == valid_url

    captured: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps({"code": 0, "msg": "success"}).encode("utf-8")

    class _FakeOpener:
        def open(self, req, timeout=15):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp()

    with patch("urllib.request.build_opener", return_value=_FakeOpener()):
        send_feishu_webhook_card(
            valid_url,
            {"header": {"title": {"tag": "plain_text", "content": "test"}}, "elements": []},
            webhook_secret="my_webhook_secret",
        )
    ts = captured["body"]["timestamp"]
    expected_str = f"{ts}\nmy_webhook_secret"
    expected_sign = base64.b64encode(
        hmac.new(expected_str.encode(), digestmod=hashlib.sha256).digest()
    ).decode("ascii")
    assert captured["body"]["sign"] == expected_sign


def test_fastapi_cases_sync_endpoint_csrf_and_execution(isolated_env) -> None:
    """Verify POST /api/cases/sync enforces CSRF + RBAC and returns poll summary."""
    _, _ = isolated_env
    with TestClient(app, base_url="https://127.0.0.1:8000") as tc:
        # Without CSRF token -> 403
        no_csrf = tc.post("/api/cases/sync")
        assert no_csrf.status_code == 403

        boot = tc.get("/api/session")
        assert boot.status_code == 200
        csrf_token = boot.json()["csrf_token"]

        # Unauthorized project -> 403
        bad_proj = tc.post(
            "/api/cases/sync?project_id=evil-project-999",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert bad_proj.status_code == 403

        # Authorized sync -> 200
        ok_resp = tc.post(
            "/api/cases/sync?project_id=gcp-prod-core-01",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert ok_resp.status_code == 200
        body = ok_resp.json()
        assert body["status"] == "success"
        assert body["projects_polled"] == ["gcp-prod-core-01"]


def test_download_live_attachment_retries_transient_401_and_404_raises_key_error(
    isolated_env,
) -> None:
    """Verify media.download retries transient 401 (read-after-write ACL lag) and maps 404 to KeyError."""
    import io
    import urllib.error

    client, _ = isolated_env
    case_name = "projects/gcp-prod-core-01/cases/84920101"

    class _OkResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b"diag-payload"

    err_401 = urllib.error.HTTPError(
        url="https://cloudsupport.googleapis.com/v2/test",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"acl lag"),
    )
    err_404 = urllib.error.HTTPError(
        url="https://cloudsupport.googleapis.com/v2/test",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"missing"),
    )

    with (
        patch.object(client, "_get_google_access_token", side_effect=["tok1", "tok2", "tok3"]),
        patch("app.support_client._invalidate_live_oauth_token") as mock_inv,
        patch("time.sleep") as mock_sleep,
        patch("urllib.request.urlopen", side_effect=[err_401, _OkResp()]),
    ):
        data = client.download_live_attachment_bytes(case_name, "att_live_123")
        assert data == b"diag-payload"
        mock_inv.assert_called_once()
        mock_sleep.assert_called_once()

    with (
        patch.object(client, "_get_google_access_token", return_value="tok3"),
        patch("urllib.request.urlopen", side_effect=err_404),
    ):
        with pytest.raises(KeyError):
            client.download_live_attachment_bytes(case_name, "att_missing_999")


def test_feishu_post_and_staged_image_auto_upload_on_confirm(isolated_env) -> None:
    """Verify Feishu standalone image + rich-text post screenshot stage onto draft and upload on confirm."""
    client, _ = isolated_env
    valid_png_1 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-screen-1"
    valid_png_2 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-screen-2"

    api_mock = MagicMock()
    api_mock.reply_markdown.return_value = "om_bot_md"
    api_mock.reply_card.return_value = "om_draft_card_img"
    api_mock.download_message_resource.side_effect = [
        (valid_png_1, None),  # extensionless Feishu image_key -> sniffed as .png
        (valid_png_2, None),
    ]

    captured_inline: list[list[dict[str, Any]]] = []

    def _turn_runner(
        user_id: str,
        session_id: str,
        text: str,
        project: str,
        selected_case: str | None,
        context_note: str | None = None,
        inline_attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        captured_inline.append(inline_attachments or [])
        if "S3" not in text:
            return {"reply": "已识别截图报错，请告知严重级别（S1-S4）。", "new_draft_ids": []}
        d = client.create_case_draft(
            project_id=project,
            display_name="Cloud SQL 连接超时截图报障",
            description=text,
            severity="S3",
        )
        return {"reply": f"已生成草稿 {d['draft_id']}", "new_draft_ids": [d["draft_id"]]}

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

    # Turn 1: User sends a standalone screenshot in DM (no extension provided by Feishu)
    img_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_img_1",
                chat_id="oc_dm_1",
                chat_type="p2p",
                message_type="image",
                content=json.dumps({"image_key": "img_v3_02abc123"}),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(img_event)
    assert len(captured_inline) == 1
    assert captured_inline[0][0]["mime_type"] == "image/png"
    assert captured_inline[0][0]["original_filename"].endswith(".png")

    # Turn 2: User sends a rich-text `post` with S3 severity + another inline screenshot
    post_content = {
        "zh_cn": {
            "title": "补充日志截图",
            "content": [
                [
                    {"tag": "text", "text": "Cloud SQL 连接偶发超时，按 S3 提交 "},
                    {"tag": "img", "image_key": "img_v3_02def456"},
                ]
            ],
        }
    }
    post_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_post_2",
                chat_id="oc_dm_1",
                chat_type="p2p",
                message_type="post",
                content=json.dumps(post_content, ensure_ascii=False),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(post_event)
    assert api_mock.reply_card.call_count == 1
    draft_card = api_mock.reply_card.call_args.args[1]
    card_plain = draft_card["elements"][0]["text"]["content"]
    assert "待随单上传附件 (2)" in card_plain

    action_elem = next(e for e in draft_card["elements"] if e.get("tag") == "action")
    draft_id = action_elem["actions"][0]["value"]["draft_id"]

    # Confirm the card -> creates the case AND auto-uploads both staged PNGs!
    confirm_event = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=OWNER_OPEN_ID),
            action=SimpleNamespace(value={"action": "confirm", "draft_id": draft_id}),
            context=SimpleNamespace(open_message_id="om_draft_card_img", open_chat_id="oc_dm_1"),
        )
    )
    bot.handle_card_action(confirm_event)

    confirmed_draft = client.get_draft(draft_id)
    created_case_name = confirmed_draft["result_case_name"]
    created_case = client.get_case(created_case_name)
    assert len(created_case["attachments"]) == 2
    patched_card = api_mock.patch_card.call_args.args[1]
    patched_json = json.dumps(patched_card, ensure_ascii=False)
    assert "已随单同步上传 2 个附件" in patched_json


def test_feishu_quote_reply_image_uploads_directly_to_existing_case(isolated_env) -> None:
    """Verify quote-replying a notification/confirmed card with an image uploads directly to that case."""
    client, _ = isolated_env
    target_case = "projects/gcp-prod-core-01/cases/84920101"
    valid_jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF-console-photo"

    api_mock = MagicMock()
    api_mock.download_message_resource.return_value = (valid_jpg, None)
    turn_mock = MagicMock()

    bot = FeishuBot(
        api_mock,
        turn_mock,
        executor=_SyncExecutor(),
        support_client_getter=lambda: client,
        error_mapper=lambda exc: SimpleNamespace(status_code=400, detail=str(exc)),
        bot_open_id=BOT_OPEN_ID,
        default_project="gcp-prod-core-01",
        patch_delay_seconds=0,
        reply_classifier=None,
    )
    bot.register_notification_message("oc_ops_group", "om_notif_card_88", target_case, "TSE 回复")

    before_count = len(client.get_case(target_case)["attachments"])
    quote_img_event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
            ),
            message=SimpleNamespace(
                message_id="om_reply_img_99",
                parent_id="om_notif_card_88",
                chat_id="oc_ops_group",
                chat_type="group",
                message_type="image",
                content=json.dumps({"image_key": "img_v3_02quote99"}),
                mentions=[],
            ),
        )
    )
    bot.handle_message_event(quote_img_event)

    turn_mock.assert_not_called()
    after_atts = client.get_case(target_case)["attachments"]
    assert len(after_atts) == before_count + 1
    assert after_atts[-1]["mime_type"] == "image/jpeg"
    assert "已将附件" in api_mock.reply_text.call_args.args[1]


def test_feishu_image_upload_security_rejects_executable_and_surfaces_scope_hint(
    isolated_env,
) -> None:
    """Verify spoofed executable bytes are blocked and missing im:resource permission gives clear guidance."""
    client, _ = isolated_env
    api_mock = MagicMock()
    api_mock.download_message_resource.side_effect = [
        (b"MZ\x90\x00spoofed-pe-binary", "diag.png"),
        PermissionError("请开通 im:resource 权限"),
    ]
    turn_mock = MagicMock()

    def _err_mapper(exc: Exception) -> SimpleNamespace:
        detail = getattr(exc, "detail", None) or str(exc)
        code = getattr(exc, "status_code", 403 if isinstance(exc, PermissionError) else 400)
        return SimpleNamespace(status_code=code, detail=detail)

    bot = FeishuBot(
        api_mock,
        turn_mock,
        executor=_SyncExecutor(),
        support_client_getter=lambda: client,
        error_mapper=_err_mapper,
        bot_open_id=BOT_OPEN_ID,
        default_project="gcp-prod-core-01",
        patch_delay_seconds=0,
        reply_classifier=None,
    )

    for idx in (1, 2):
        ev = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(open_id=OWNER_OPEN_ID), sender_type="user"
                ),
                message=SimpleNamespace(
                    message_id=f"om_bad_img_{idx}",
                    chat_id="oc_dm_sec",
                    chat_type="p2p",
                    message_type="image",
                    content=json.dumps({"image_key": f"img_v3_bad_{idx}"}),
                    mentions=[],
                ),
            )
        )
        bot.handle_message_event(ev)

    turn_mock.assert_not_called()
    assert api_mock.reply_text.call_count == 2
    assert "Executable binaries" in api_mock.reply_text.call_args_list[0].args[1]
    assert "im:resource" in api_mock.reply_text.call_args_list[1].args[1]


