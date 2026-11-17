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

"""Comprehensive Cloud SRE / Ops real-world scenario tests."""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from google.api_core import exceptions as gcp_exceptions

from app import config, support_client
from app.agent import run_deterministic_agent_turn
from app.feishu_bot import FeishuBot, build_draft_card
from app.security import validate_and_store_raw_bytes
from app.support_client import CloudSupportClient, set_support_client
from app.tools import (
    get_ops_shift_handover,
    inspect_case_attachment,
    prepare_case_draft,
    prepare_escalation_draft,
    revise_case_draft,
    search_support_cases,
)

BOT_ID = "ou_bot_ops01"
SRE_NIGHT = "ou_sre_night01"
SRE_DAY = "ou_sre_day02"
SRE_BYSTANDER = "ou_dev_bystander03"
SRE_LEAD = "ou_sre_lead99"
WAR_ROOM_CHAT = "oc_warroom_prod01"


class _SyncExecutor:
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)


@pytest.fixture()
def isolated_sre_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CloudSupportClient:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_DIR", upload_dir)
    monkeypatch.setenv(
        "ALLOWED_PROJECTS",
        "gcp-prod-core-01,gcp-analytics-data-02,gcp-ai-sandbox-03",
    )
    monkeypatch.setenv("FORCE_TEST_CASE", "true")
    config.set_support_mode("MOCK")
    client = CloudSupportClient(db_path=tmp_path / "sre_ops.db")
    set_support_client(client)
    yield client
    set_support_client(None)


def test_sre_diagnostic_file_formats_accepted(isolated_sre_env: CloudSupportClient) -> None:
    """SREs regularly attach YAML manifests, HAR captures, CSV metrics, SQL, conf, gzip, and zip bundles."""
    yaml_meta = validate_and_store_raw_bytes(
        b"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\n",
        "deployment.yaml",
    )
    assert yaml_meta["mime_type"] == "application/x-yaml"

    har_payload = json.dumps({"log": {"version": "1.2", "entries": []}}).encode("utf-8")
    har_meta = validate_and_store_raw_bytes(har_payload, "lb-502-capture.har")
    assert har_meta["mime_type"] == "application/json"

    csv_meta = validate_and_store_raw_bytes(
        b"timestamp,latency_ms,status\n2026-11-13T01:00:00Z,1520,503\n",
        "latency.csv",
    )
    assert csv_meta["mime_type"] == "text/csv"

    sql_meta = validate_and_store_raw_bytes(
        b"SELECT job_id, total_slot_ms FROM region-us.INFORMATION_SCHEMA.JOBS;\n",
        "slow_query.sql",
    )
    assert sql_meta["mime_type"] == "text/plain"

    gz_bytes = gzip.compress(b"Nov 13 01:02:03 gke-node kubelet: pleg is not healthy\n")
    gz_meta = validate_and_store_raw_bytes(gz_bytes, "kubelet.log.gz")
    assert gz_meta["mime_type"] == "application/gzip"

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pods.txt", "checkout-7d9f8b-abcde 0/1 CrashLoopBackOff\n")
        zf.writestr("events.json", json.dumps({"items": []}))
    zip_meta = validate_and_store_raw_bytes(zip_buf.getvalue(), "cluster-dump.zip")
    assert zip_meta["mime_type"] == "application/zip"


def test_sre_prohibited_keyfiles_and_sa_json_blocked(isolated_sre_env: CloudSupportClient) -> None:
    """Accidental upload of private keys, .tfstate, .env, SA JSON keys, or zip with keys must be blocked."""
    # 1. Direct prohibited extensions
    for bad_name in ("server.key", "cert.pem", "prod.tfstate", ".env", "prod.env", "keystore.p12"):
        with pytest.raises(HTTPException) as exc_info:
            validate_and_store_raw_bytes(b"dummy-secret-content", bad_name)
        assert exc_info.value.status_code == 400

    # 2. GCP Service Account JSON key disguised as normal .json
    pem_hdr = "-----BEGIN " + "PRIVATE KEY-----"
    pem_ftr = "-----END " + "PRIVATE KEY-----"
    sa_key_json = json.dumps(
        {
            "type": "service_account",
            "project_id": "gcp-prod-core-01",
            "private_key_id": "123456",
            "private_key": f"{pem_hdr}\nFAKE_TEST_KEY_MATERIAL_ONLY\n{pem_ftr}\n",
            "client_email": "prod-sa@gcp-prod-core-01.iam.gserviceaccount.com",
        }
    ).encode("utf-8")
    with pytest.raises(HTTPException) as exc_sa:
        validate_and_store_raw_bytes(sa_key_json, "gcp-creds.json")
    assert exc_sa.value.status_code == 400
    assert "私钥" in exc_sa.value.detail or "Service Account" in exc_sa.value.detail

    # 3. K8s Secret YAML containing raw PEM private key
    rsa_hdr = ("-----BEGIN RSA " + "PRIVATE KEY-----").encode("utf-8")
    yaml_with_key = (
        b"apiVersion: v1\nkind: Secret\nstringData:\n  tls.key: |\n    "
        + rsa_hdr
        + b"\n    FAKE_TEST_RSA_BODY\n"
    )
    with pytest.raises(HTTPException) as exc_yaml:
        validate_and_store_raw_bytes(yaml_with_key, "tls-secret.yaml")
    assert exc_yaml.value.status_code == 400

    # 4. ZIP bundle hiding a .pem or .sh script
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("logs/app.log", "ok")
        zf.writestr("keys/id_rsa.pem", "secret")
    with pytest.raises(HTTPException) as exc_zip:
        validate_and_store_raw_bytes(zip_buf.getvalue(), "diag.zip")
    assert exc_zip.value.status_code == 400


def test_automatic_secret_redaction_in_draft_revise_and_comment(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """Inline Bearer tokens, API keys, AWS keys, and DB URI passwords must be auto-redacted."""
    fake_oauth = "ya29." + "test_dummy_oauth_fixture_9876543210"
    fake_gcp_key = "AIza" + "TestDummyFixtureKey0000000000000000"
    fake_aws_key = "AKIA" + "TESTDUMMYFIXTURE"
    raw_desc = (
        "排查 Cloud SQL 连接报错，复现命令：\n"
        f"curl -H 'Authorization: Bearer {fake_oauth}' "
        "https://sqladmin.googleapis.com/v1/projects/gcp-prod-core-01/instances\n"
        "应用侧连接串：postgresql://app_user:MySuperSecretPass!2026@10.24.0.8:5432/orders_db\n"
        f"客户端 Key: {fake_gcp_key} 以及 {fake_aws_key}"
    )
    res = prepare_case_draft(
        project_id="gcp-prod-core-01",
        display_name="Cloud SQL 连接鉴权异常排查",
        description=raw_desc,
        severity="S2",
        classification_id="",
        classification_display_name="",
        subscriber_emails="sre@example.com",
        time_zone="Asia/Shanghai",
    )
    assert res["status"] == "pending_confirmation"
    draft = res["draft"]
    saved_desc = draft["payload"]["description"]
    assert "MySuperSecretPass" not in saved_desc
    assert fake_oauth not in saved_desc
    assert fake_gcp_key not in saved_desc
    assert fake_aws_key not in saved_desc
    assert "[REDACTED_PASSWORD]" in saved_desc
    assert "[REDACTED_BEARER_TOKEN]" in saved_desc
    assert len(draft["payload"]["redacted_secrets"]) >= 3

    # Verify Feishu draft card displays the redaction notice
    card_json = json.dumps(build_draft_card(draft, owner_open_id=SRE_NIGHT), ensure_ascii=False)
    assert "安全自动脱敏" in card_json

    # Also verify add_comment scrubs secrets before saving/sending
    cmt = isolated_sre_env.add_comment(
        "projects/gcp-prod-core-01/cases/84920101",
        "补充 Redis 连接串 redis://default:RedisPass888@10.0.0.9:6379/0 供参考",
    )
    assert "RedisPass888" not in cmt["body"]
    assert "[REDACTED_PASSWORD]" in cmt["body"]
    assert "数据库连接串密码" in cmt.get("redacted_secrets", [])


def test_product_diagnostic_readiness_and_s1_follow_the_sun_tips(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """S1 sparse Chinese GKE case gets actionable non-blocking readiness & Follow-the-Sun tips."""
    res = prepare_case_draft(
        project_id="gcp-prod-core-01",
        display_name="生产环境 GKE 业务完全中断",
        description="我们的生产容器服务突然全部访问不了，请求紧急协助排查！",
        severity="S1",
        classification_id="100H41Q3DTMN0TBKCKD0SGRFDLO7AT35499N0PR995I62SR991762R3581J62VAI",
        classification_display_name="Google Kubernetes Engine > Cluster Autoscaler & Node Pools",
        subscriber_emails="",
        time_zone="Asia/Shanghai",
    )
    tips = res["diagnostic_tips"]
    joined = "\n".join(tips)
    assert "时间窗" in joined
    assert "Region" in joined
    assert "GKE" in joined
    assert "7×24" in joined or "全球接力" in joined

    # Revise draft adding timestamp, region, cluster name, subscriber email, and English summary
    rev = revise_case_draft(
        draft_id=res["draft"]["draft_id"],
        description=(
            "2026-11-13 06:15 UTC 起 asia-east1-b 区域的 GKE cluster prod-gke-01 (node pool default-pool) "
            "所有 Pod 无法调度。English Summary: Production GKE cluster prod-gke-01 in asia-east1-b "
            "experiencing total pod scheduling outage since 06:15 UTC."
        ),
        subscriber_emails="noc-24x7@example.com",
    )
    assert rev["status"] == "draft_revised"
    assert rev["diagnostic_tips"] == []


def test_duplicate_escalation_blocked_and_live_classification_retry(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """Escalating an already-escalated case must fail fast with actionable guidance; LIVE classification stale ID auto-retries."""
    # Case 84920104 in gcp-ai-sandbox-03 is seeded as escalated=1
    with isolated_sre_env._get_conn() as conn:
        conn.execute(
            "UPDATE cases SET escalated = 1 WHERE case_name = ?",
            ("projects/gcp-prod-core-01/cases/84920101",),
        )
        conn.commit()

    esc_res = prepare_escalation_draft(
        case_name="projects/gcp-prod-core-01/cases/84920101",
        reason="BUSINESS_IMPACT",
        justification="核心交易链路持续受损超过 2 小时，请求升级。",
    )
    assert esc_res["status"] == "error"
    assert "already escalated" in esc_res["error"]

    # Test LIVE create_case auto-retry when classification ID is stale
    draft = isolated_sre_env.create_case_draft(
        project_id="gcp-prod-core-01",
        display_name="Cloud SQL 连接池耗尽排查",
        description="2026-11-13 05:00 UTC 在 us-central1 实例 prod-pg-01 连接池被打满。",
        severity="S3",
    )
    config.set_support_mode("LIVE")
    try:
        fake_case_client = MagicMock()
        created_proto = SimpleNamespace(
            name="projects/gcp-prod-core-01/cases/99887766",
            display_name="Cloud SQL 连接池耗尽排查",
            description="...",
            priority=SimpleNamespace(name="P3"),
            state=SimpleNamespace(name="NEW"),
            classification=SimpleNamespace(id="real-cls-2", display_name="Cloud SQL > Connect"),
            time_zone="Asia/Shanghai",
            subscriber_email_addresses=[],
            escalated=False,
            test_case=True,
            creator=SimpleNamespace(display_name="SRE", email="sre@example.com"),
            create_time=None,
            update_time=None,
        )
        fake_case_client.create_case.side_effect = [
            gcp_exceptions.InvalidArgument("Invalid classification ID provided."),
            created_proto,
        ]
        with (
            patch.object(support_client, "_make_live_client", return_value=fake_case_client),
            patch.object(
                isolated_sre_env,
                "_resolve_live_classification",
                side_effect=[
                    ("stale-cls-1", "Old Classification"),
                    ("real-cls-2", "Cloud SQL > Connect"),
                ],
            ),
        ):
            created_case = isolated_sre_env.confirm_and_create_case(draft["draft_id"], confirmed=True)
        assert created_case["case_id"] == "99887766"
        assert fake_case_client.create_case.call_count == 2
    finally:
        config.set_support_mode("MOCK")


def test_inspect_case_attachment_local_and_live_remote_tse_script(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """SRE can read TSE-uploaded YAML/log attachments right in chat with secret sanitization."""
    case_name = "projects/gcp-prod-core-01/cases/84920101"
    raw_yaml = (
        b"# Recommended by Google Cloud Support TSE\n"
        b"tcp_keepalives_idle: 30\n"
        b"dsn: postgres://user:TempPass123@10.1.2.3:5432/app\n"
    )
    meta = validate_and_store_raw_bytes(raw_yaml, "tse_recommended_sysctl.yaml")
    isolated_sre_env.add_attachment_metadata(
        case_name=case_name,
        original_filename=meta["original_filename"],
        stored_filename=meta["stored_filename"],
        mime_type=meta["mime_type"],
        size_bytes=meta["size_bytes"],
    )

    res = inspect_case_attachment(case_name=case_name, attachment_ref="sysctl.yaml")
    assert res["status"] == "success"
    att = res["attachment"]
    assert att["preview_available"] is True
    assert "tcp_keepalives_idle: 30" in att["content_preview"]
    assert "TempPass123" not in att["content_preview"]
    assert "[REDACTED_PASSWORD]" in att["content_preview"]

    # Deterministic fallback turn
    turn = run_deterministic_agent_turn(
        f"帮我查看附件 {case_name} 里的 sysctl.yaml",
        active_project="gcp-prod-core-01",
    )
    assert "tcp_keepalives_idle: 30" in turn["reply"]

    # Also verify LIVE remote TSE attachment download when stored_filename is empty
    with isolated_sre_env._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO attachments (attachment_id, case_name, original_filename, stored_filename, mime_type, size_bytes, create_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("att-remote-tse-01", case_name, "rca_summary.txt", "", "text/plain", 48, "2026-11-13T06:00:00Z"),
        )
        conn.commit()

    config.set_support_mode("LIVE")
    try:
        with (
            patch.object(isolated_sre_env, "_sync_live_case_details", return_value=None),
            patch.object(
                isolated_sre_env,
                "download_live_attachment_bytes",
                return_value=b"Root cause: PSC NAT subnet exhaustion in us-central1.",
            ),
        ):
            live_res = inspect_case_attachment(case_name=case_name, attachment_ref="rca_summary")
        assert live_res["status"] == "success"
        assert "PSC NAT subnet exhaustion" in live_res["attachment"]["content_preview"]
    finally:
        config.set_support_mode("MOCK")


def test_cross_project_search_and_ops_shift_handover_sla_radar(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """Cross-project search finds cases across all projects & shift handover flags stale/blocked cases."""
    # 1. Search across ALL projects for '429' (in gcp-ai-sandbox-03) and 'ARP/PSC' (inside comment of 84920101)
    s_429 = search_support_cases(query="429", project_id="")
    assert s_429["status"] == "success"
    assert any(c["case_id"] == "84920104" for c in s_429["cases"])

    s_psc = search_support_cases(query="tcp_keepalives_idle", project_id="")
    assert s_psc["status"] == "success"
    assert any(c["case_id"] == "84920101" for c in s_psc["cases"])

    # 2. Mark 84920102 as ACTION_REQUIRED and test shift handover SLA radar
    with isolated_sre_env._get_conn() as conn:
        conn.execute(
            "UPDATE cases SET state = 'ACTION_REQUIRED' WHERE case_name = ?",
            ("projects/gcp-prod-core-01/cases/84920102",),
        )
        conn.commit()

    handover = get_ops_shift_handover(project_id="")
    assert handover["status"] == "success"
    rep = handover["handover"]
    assert rep["total_open_cases"] >= 3
    assert any(x["case_id"] == "84920101" for x in rep["high_priority_or_escalated"])
    assert any(x["case_id"] == "84920102" for x in rep["action_required"])
    assert len(rep["stale_cases"]) >= 2

    det = run_deterministic_agent_turn("生成交接班巡检报告", active_project="gcp-prod-core-01")
    assert "交接班巡检简报" in det["reply"]
    assert "84920102" in det["reply"]


def test_war_room_draft_delegation_and_sre_admin_co_approval(
    isolated_sre_env: CloudSupportClient,
) -> None:
    """War room shift handover allows @colleague delegation and SRE Lead co-approval while blocking random bystanders."""
    sent_cards: list[tuple[str, dict[str, Any]]] = []
    patched_cards: list[tuple[str, dict[str, Any]]] = []
    replies: list[str] = []

    api = MagicMock()
    api.reply_markdown.side_effect = lambda mid, txt: (replies.append(txt) or "om_md")
    api.reply_text.side_effect = lambda mid, txt: (replies.append(txt) or "om_txt")

    def _reply_card(mid: str, card: dict[str, Any]) -> str:
        cid = f"om_card_{len(sent_cards) + 1}"
        sent_cards.append((cid, card))
        return cid

    api.reply_card.side_effect = _reply_card
    api.patch_card.side_effect = lambda cid, card: patched_cards.append((cid, card))

    def _runner(
        user_id: str,
        session_id: str,
        message: str,
        project_id: str,
        selected_case: str | None,
        context_note: str | None = None,
        inline_attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        out = run_deterministic_agent_turn(
            message, active_project=project_id, selected_case_name=selected_case
        )
        out["new_draft_ids"] = [d["draft_id"] for d in out.get("pending_drafts", [])]
        return out

    bot = FeishuBot(
        api,
        _runner,
        executor=_SyncExecutor(),
        support_client_getter=lambda: isolated_sre_env,
        bot_open_id=BOT_ID,
        default_project="gcp-prod-core-01",
        admin_open_ids=[SRE_LEAD],
        patch_delay_seconds=0,
    )

    # 1. Night-shift SRE creates a draft card in War Room
    ev_create = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=SRE_NIGHT), sender_type="user"),
            message=SimpleNamespace(
                message_id="om_w1",
                chat_id=WAR_ROOM_CHAT,
                chat_type="group",
                message_type="text",
                content=json.dumps(
                    {
                        "text": "@_user_1 在 gcp-prod-core-01 提交工单：2026-11-13 06:00 UTC us-central1 GKE 节点池扩容失败，严重级别 S2"
                    }
                ),
                mentions=[
                    SimpleNamespace(
                        key="@_user_1",
                        id=SimpleNamespace(open_id=BOT_ID),
                        mentioned_type="bot",
                    )
                ],
                parent_id="",
            ),
        )
    )
    bot.handle_message_event(ev_create)
    assert len(sent_cards) == 1
    card_msg_id, card_body = sent_cards[0]
    draft_id = next(
        a["value"]["draft_id"]
        for el in card_body["elements"]
        if el.get("tag") == "action"
        for a in el["actions"]
    )

    def _card_click(operator_id: str, action: str = "confirm", target_draft: str = draft_id) -> dict[str, Any]:
        return bot.handle_card_action(
            SimpleNamespace(
                event=SimpleNamespace(
                    operator=SimpleNamespace(open_id=operator_id),
                    action=SimpleNamespace(value={"action": action, "draft_id": target_draft}),
                    context=SimpleNamespace(open_message_id=card_msg_id, open_chat_id=WAR_ROOM_CHAT),
                )
            )
        )

    # 2. Unrelated bystander clicks confirm -> blocked
    bystander_resp = _card_click(SRE_BYSTANDER)
    assert "只有发起人" in bystander_resp["toast"]["content"]
    assert isolated_sre_env.get_draft(draft_id)["status"] == "PENDING_CONFIRMATION"

    # 3. Day-shift SRE initially cannot click before handover
    before_handover = _card_click(SRE_DAY)
    assert "只有发起人" in before_handover["toast"]["content"]

    # 4. Night-shift SRE delegates the draft in group: "@bot 把草稿转交给 @SRE_DAY 代确认"
    ev_delegate = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=SRE_NIGHT), sender_type="user"),
            message=SimpleNamespace(
                message_id="om_w2",
                chat_id=WAR_ROOM_CHAT,
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "@_user_1 我下班交接，把草稿转交给 @_user_2 代确认"}),
                mentions=[
                    SimpleNamespace(
                        key="@_user_1",
                        id=SimpleNamespace(open_id=BOT_ID),
                        mentioned_type="bot",
                    ),
                    SimpleNamespace(
                        key="@_user_2",
                        id=SimpleNamespace(open_id=SRE_DAY),
                        mentioned_type="user",
                    ),
                ],
                parent_id="",
            ),
        )
    )
    bot.handle_message_event(ev_delegate)
    assert any("授权代确认" in json.dumps(c, ensure_ascii=False) for _, c in patched_cards)

    # 5. Now Day-shift SRE clicks confirm -> succeeds!
    day_resp = _card_click(SRE_DAY)
    assert day_resp["toast"]["content"] == "正在提交…"
    assert isolated_sre_env.get_draft(draft_id)["status"] == "CONFIRMED"

    # 6. SRE Lead (admin_open_ids) can directly approve an urgent close/escalation draft when owner is offline
    close_draft = isolated_sre_env.create_close_draft(
        "projects/gcp-prod-core-01/cases/84920102", "Resolved by node pool resize"
    )
    isolated_sre_env.claim_draft_owner(close_draft["draft_id"], SRE_NIGHT, chat_id=WAR_ROOM_CHAT)
    lead_resp = _card_click(SRE_LEAD, action="confirm", target_draft=close_draft["draft_id"])
    assert lead_resp["toast"]["content"] == "正在提交…"
    assert isolated_sre_env.get_draft(close_draft["draft_id"])["status"] == "CONFIRMED"
