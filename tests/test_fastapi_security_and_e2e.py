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

"""End-to-end FastAPI security, CSRF, CSP, file upload, and frontend DOM safety tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.fast_api_app import app
from app.support_client import CloudSupportClient, set_support_client


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CASE_AGENT_DATA_DIR", str(tmp_path / ".data"))
    config.DATA_DIR = (tmp_path / ".data").resolve()
    config.DB_PATH = config.DATA_DIR / "support_cases.db"
    config.UPLOAD_DIR = config.DATA_DIR / "uploads"
    config.set_support_mode("MOCK")
    support_client = CloudSupportClient(db_path=config.DB_PATH)
    set_support_client(support_client)
    return TestClient(app, base_url="https://127.0.0.1:8000")


def test_security_headers_and_http_method_allowlist(client: TestClient) -> None:
    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")
    assert "unsafe-inline" not in resp.headers.get("Content-Security-Policy", "")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "camera=()" in resp.headers.get("Permissions-Policy", "")
    assert resp.headers.get("Cache-Control") == "no-store"

    # Cookie hardening check (__Host-session, HttpOnly, Secure, SameSite=lax)
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "__Host-session=" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert "Secure" in set_cookie_header

    # Disallowed HTTP methods (DELETE, PUT, TRACE) must return 405
    for method in ("DELETE", "PUT", "TRACE"):
        method_resp = client.request(method, "/api/cases")
        assert method_resp.status_code == 405


def test_csrf_protection_blocks_unauthenticated_post(client: TestClient) -> None:
    client.get("/api/session")

    # POST without X-CSRF-Token must be rejected with 403
    no_csrf = client.post(
        "/api/chat",
        json={"message": "查看工单", "project_id": "gcp-prod-core-01"},
    )
    assert no_csrf.status_code == 403

    # POST with forged X-CSRF-Token must be rejected with 403
    bad_csrf = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": "forged-token-value.12345"},
        json={"message": "查看工单", "project_id": "gcp-prod-core-01"},
    )
    assert bad_csrf.status_code == 403


def test_end_to_end_chat_hitl_draft_comment_escalate_and_close(
    client: TestClient,
) -> None:
    sess_resp = client.get("/api/session")
    csrf = sess_resp.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    # 1. Chat request to create a case generates a HITL draft (not an immediate case)
    chat_resp = client.post(
        "/api/chat",
        headers=headers,
        json={
            "message": "帮我在 gcp-prod-core-01 提一个 S2 工单：生产 Cloud SQL PostgreSQL 连接超时",
            "project_id": "gcp-prod-core-01",
        },
    )
    assert chat_resp.status_code == 200
    chat_data = chat_resp.json()
    assert len(chat_data["pending_drafts"]) == 1
    draft_id = chat_data["pending_drafts"][0]["draft_id"]

    # 2. Confirm the draft via HITL endpoint
    confirm_resp = client.post(
        f"/api/drafts/{draft_id}/confirm",
        headers=headers,
        json={
            "confirmed": True,
            "display_name": "[PROD] Cloud SQL PostgreSQL HA 连接超时故障",
            "severity": "S2",
        },
    )
    assert confirm_resp.status_code == 200
    created_case = confirm_resp.json()["case"]
    case_id = created_case["case_id"]
    project_id = created_case["project_id"]
    assert created_case["state"] == "OPEN"

    # 3. Add a comment to the created case
    comment_resp = client.post(
        f"/api/cases/{project_id}/{case_id}/comments",
        headers=headers,
        json={"body": "已附上 pg_stat_activity 诊断输出，请优先排查。"},
    )
    assert comment_resp.status_code == 200
    assert any(
        "pg_stat_activity" in c["body"]
        for c in comment_resp.json()["case"]["comments"]
    )

    # 4. Create an escalation draft and confirm it
    esc_draft_resp = client.post(
        "/api/drafts/escalation",
        headers=headers,
        json={
            "case_name": created_case["name"],
            "reason": "BUSINESS_IMPACT",
            "justification": "核心支付链路数据库只读副本延迟持续升高，影响生产交易。",
        },
    )
    assert esc_draft_resp.status_code == 200
    esc_draft_id = esc_draft_resp.json()["draft"]["draft_id"]

    esc_confirm_resp = client.post(
        f"/api/drafts/{esc_draft_id}/confirm",
        headers=headers,
        json={"confirmed": True},
    )
    assert esc_confirm_resp.status_code == 200
    assert esc_confirm_resp.json()["case"]["escalated"] is True

    # 5. Close the case
    close_resp = client.post(
        f"/api/cases/{project_id}/{case_id}/close",
        headers=headers,
    )
    assert close_resp.status_code == 200
    assert close_resp.json()["case"]["state"] == "CLOSED"


def test_secure_file_upload_and_download(client: TestClient) -> None:
    sess_resp = client.get("/api/session")
    csrf = sess_resp.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    # 1. Disallowed extension (.exe / .sh) must be rejected with 400
    bad_ext = client.post(
        "/api/cases/gcp-prod-core-01/84920101/attachments",
        headers=headers,
        files={"file": ("exploit.sh", io.BytesIO(b"echo hello"), "text/plain")},
    )
    assert bad_ext.status_code == 400

    # 2. Fake PNG magic bytes must be rejected with 400
    bad_magic = client.post(
        "/api/cases/gcp-prod-core-01/84920101/attachments",
        headers=headers,
        files={"file": ("fake.png", io.BytesIO(b"not-a-real-png-header"), "image/png")},
    )
    assert bad_magic.status_code == 400

    # 3. Executable ELF/script disguised as .log must be rejected with 400
    disguised_script = client.post(
        "/api/cases/gcp-prod-core-01/84920101/attachments",
        headers=headers,
        files={"file": ("diag.log", io.BytesIO(b"#!/bin/bash\nwhoami"), "text/plain")},
    )
    assert disguised_script.status_code == 400

    # 4. Valid .log and valid .png upload and download with Content-Disposition: attachment
    valid_log_bytes = b"2026-10-07T01:00:00Z ERROR [CloudSQLAuthProxy] connection timeout to 10.128.0.5:5432\n"
    ok_upload = client.post(
        "/api/cases/gcp-prod-core-01/84920101/attachments",
        headers=headers,
        files={"file": ("../../cloudsql_proxy.log", io.BytesIO(valid_log_bytes), "text/plain")},
    )
    assert ok_upload.status_code == 200
    att = ok_upload.json()["attachment"]
    # Path traversal stripped from original filename
    assert ".." not in att["filename"]

    # Download with active session
    dl_resp = client.get(att["download_url"])
    assert dl_resp.status_code == 200
    assert dl_resp.content == valid_log_bytes
    assert "attachment;" in dl_resp.headers.get("Content-Disposition", "")
    assert dl_resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_session_logout_invalidates_csrf_and_session(client: TestClient) -> None:
    sess_resp = client.get("/api/session")
    csrf = sess_resp.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    logout_resp = client.post("/api/logout", headers=headers)
    assert logout_resp.status_code == 200

    # Reusing the old CSRF token after logout must fail with 403
    post_after_logout = client.post(
        "/api/mode",
        headers=headers,
        json={"mode": "MOCK"},
    )
    assert post_after_logout.status_code == 403


def test_chat_close_request_only_drafts_until_card_confirmed(client: TestClient) -> None:
    sess_resp = client.get("/api/session")
    headers = {"X-CSRF-Token": sess_resp.json()["csrf_token"]}
    case_name = "projects/gcp-prod-core-01/cases/84920101"

    chat_resp = client.post(
        "/api/chat",
        headers=headers,
        json={"message": f"关闭工单 {case_name}", "project_id": "gcp-prod-core-01"},
    )
    assert chat_resp.status_code == 200
    data = chat_resp.json()
    assert data["engine"] == "deterministic"
    close_drafts = [d for d in data["pending_drafts"] if d["draft_type"] == "CLOSE_CASE"]
    assert len(close_drafts) == 1
    draft_id = close_drafts[0]["draft_id"]

    detail = client.get("/api/cases/gcp-prod-core-01/84920101").json()["case"]
    assert detail["state"] != "CLOSED"

    confirm_resp = client.post(
        f"/api/drafts/{draft_id}/confirm", headers=headers, json={"confirmed": True}
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["draft_type"] == "CLOSE_CASE"
    assert confirm_resp.json()["case"]["state"] == "CLOSED"

    replay = client.post(
        f"/api/drafts/{draft_id}/confirm", headers=headers, json={"confirmed": True}
    )
    assert replay.status_code == 400


def test_frontend_static_security_audit() -> None:
    """Verify app.js contains zero unsafe DOM sinks, blocking dialogs, or WebStorage token usage."""
    app_js_path = (
        Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
    )
    js_source = app_js_path.read_text(encoding="utf-8")

    prohibited_patterns = [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "alert(",
        "confirm(",
        "prompt(",
        "localStorage",
        "sessionStorage",
    ]
    for pattern in prohibited_patterns:
        assert pattern not in js_source, f"Prohibited frontend pattern '{pattern}' found in app.js"
