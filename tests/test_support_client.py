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

"""Unit tests for CloudSupportClient (SQLite Mock, HITL Drafts, RBAC, PII Masking)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.support_client import CloudSupportClient, mask_email, set_support_client


@pytest.fixture()
def isolated_client(tmp_path: Path) -> CloudSupportClient:
    db_file = tmp_path / "test_support_cases.db"
    config.set_support_mode("MOCK")
    client = CloudSupportClient(db_path=db_file)
    set_support_client(client)
    return client


def test_mask_email_pii() -> None:
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("sre-oncall@example.com") == "s***@example.com"
    assert mask_email("invalid-email") == "***"


def test_search_classifications(isolated_client: CloudSupportClient) -> None:
    sql_results = isolated_client.search_classifications("Cloud SQL PostgreSQL timeout")
    assert len(sql_results) >= 1
    assert "Cloud SQL" in sql_results[0]["display_name"]

    gke_results = isolated_client.search_classifications("GKE Cluster Autoscaler")
    assert len(gke_results) >= 1
    assert "Kubernetes" in gke_results[0]["display_name"]


def test_case_draft_and_hitl_confirmation_lifecycle(
    isolated_client: CloudSupportClient,
) -> None:
    draft = isolated_client.create_case_draft(
        project_id="gcp-prod-core-01",
        display_name="[TEST] Cloud SQL read replica lag spike",
        description="Read replica in us-central1-b shows replication lag > 120 seconds.",
        severity="S2",
        classification_id="",
        classification_display_name="",
        subscriber_emails="dba@example.com,oncall@example.com",
        time_zone="Asia/Shanghai",
    )
    assert draft["status"] == "PENDING_CONFIRMATION"
    assert draft["draft_type"] == "CREATE_CASE"
    assert draft["payload"]["masked_subscriber_emails"] == [
        "d***@example.com",
        "o***@example.com",
    ]

    # Attempting to confirm with confirmed=False MUST raise PermissionError
    with pytest.raises(PermissionError, match="HITL Guardrail"):
        isolated_client.confirm_and_create_case(draft["draft_id"], confirmed=False)

    # Confirming with confirmed=True creates the case
    created = isolated_client.confirm_and_create_case(
        draft["draft_id"],
        confirmed=True,
        overrides={"severity": "S1"},
    )
    assert created["name"].startswith("projects/gcp-prod-core-01/cases/")
    assert created["severity"] == "S1"
    assert created["priority"] == "P1"
    assert created["state"] == "OPEN"
    assert created["test_case"] is True
    assert created["subscriber_emails"] == ["d***@example.com", "o***@example.com"]

    # Re-confirming an already confirmed draft MUST fail
    with pytest.raises(ValueError, match="cannot be confirmed again"):
        isolated_client.confirm_and_create_case(draft["draft_id"], confirmed=True)


def test_comments_escalation_and_close_lifecycle(
    isolated_client: CloudSupportClient,
) -> None:
    cases = isolated_client.list_cases("gcp-prod-core-01", "OPEN")
    assert len(cases) >= 1
    target_case_name = cases[0]["name"]

    # Add comment
    cmt = isolated_client.add_comment(
        case_name=target_case_name,
        body="Uploaded additional tcpdump capture from GKE node.",
        author="engineer@example.com",
    )
    assert cmt["author"] == "e***@example.com"

    # Prepare escalation draft
    esc_draft = isolated_client.create_escalation_draft(
        case_name=target_case_name,
        reason="BUSINESS_IMPACT",
        justification="Production checkout pipeline experiencing elevated latency.",
    )
    assert esc_draft["status"] == "PENDING_CONFIRMATION"
    assert esc_draft["draft_type"] == "ESCALATE_CASE"

    # Unconfirmed escalation must fail
    with pytest.raises(PermissionError, match="HITL Guardrail"):
        isolated_client.confirm_and_escalate_case(esc_draft["draft_id"], confirmed=False)

    # Confirmed escalation succeeds
    escalated_case = isolated_client.confirm_and_escalate_case(
        esc_draft["draft_id"], confirmed=True
    )
    assert escalated_case["escalated"] is True
    assert escalated_case["escalation_reason"] == "BUSINESS_IMPACT"

    # Close case
    closed_case = isolated_client.close_case(target_case_name)
    assert closed_case["state"] == "CLOSED"
    assert closed_case["escalated"] is False


def test_rbac_project_allowlist_enforcement(
    isolated_client: CloudSupportClient,
) -> None:
    with pytest.raises(PermissionError, match="not in ALLOWED_PROJECTS"):
        isolated_client.list_cases("gcp-unauthorized-project-99", "ALL")

    with pytest.raises(PermissionError, match="not in ALLOWED_PROJECTS"):
        isolated_client.create_case_draft(
            project_id="gcp-unauthorized-project-99",
            display_name="Unauthorized test case",
            description="Should be blocked by RBAC project allowlist.",
            severity="S3",
        )

    with pytest.raises(PermissionError, match="not in ALLOWED_PROJECTS"):
        isolated_client.get_case("projects/gcp-unauthorized-project-99/cases/84920101")


def test_close_draft_lifecycle_and_execute_dispatch(
    isolated_client: CloudSupportClient,
) -> None:
    open_cases = isolated_client.list_cases("gcp-prod-core-01", "OPEN")
    assert len(open_cases) >= 1
    target_case_name = open_cases[0]["name"]

    # Close draft: does not close the case by itself
    close_draft = isolated_client.create_close_draft(
        case_name=target_case_name, reason="Issue resolved after config fix."
    )
    assert close_draft["status"] == "PENDING_CONFIRMATION"
    assert close_draft["draft_type"] == "CLOSE_CASE"
    assert isolated_client.get_case(target_case_name)["state"] != "CLOSED"

    # Unconfirmed close MUST raise PermissionError and leave the case open
    with pytest.raises(PermissionError, match="HITL Guardrail"):
        isolated_client.confirm_and_close_case(close_draft["draft_id"], confirmed=False)
    assert isolated_client.get_case(target_case_name)["state"] != "CLOSED"

    # Confirmed close succeeds
    closed = isolated_client.confirm_and_close_case(close_draft["draft_id"], confirmed=True)
    assert closed["state"] == "CLOSED"
    assert isolated_client.get_draft(close_draft["draft_id"])["status"] == "CONFIRMED"

    # Second confirm MUST fail
    with pytest.raises(ValueError, match="cannot be confirmed again"):
        isolated_client.confirm_and_close_case(close_draft["draft_id"], confirmed=True)

    # execute_confirmed_draft dispatches by draft_type
    create_draft = isolated_client.create_case_draft(
        project_id="gcp-prod-core-01",
        display_name="[TEST] Dispatch create",
        description="Dispatch test for execute_confirmed_draft create path.",
        severity="S3",
    )
    with pytest.raises(PermissionError, match="HITL Guardrail"):
        isolated_client.execute_confirmed_draft(create_draft["draft_id"], confirmed=False)
    created = isolated_client.execute_confirmed_draft(create_draft["draft_id"], confirmed=True)
    assert created["draft_type"] == "CREATE_CASE"
    assert created["case"]["state"] == "OPEN"
    new_case_name = created["case"]["name"]

    esc_draft = isolated_client.create_escalation_draft(
        case_name=new_case_name,
        reason="BUSINESS_IMPACT",
        justification="Dispatch test for escalation path.",
    )
    escalated = isolated_client.execute_confirmed_draft(esc_draft["draft_id"], confirmed=True)
    assert escalated["draft_type"] == "ESCALATE_CASE"
    assert escalated["case"]["escalated"] is True

    close_draft_2 = isolated_client.create_close_draft(case_name=new_case_name, reason="")
    closed_2 = isolated_client.execute_confirmed_draft(close_draft_2["draft_id"], confirmed=True)
    assert closed_2["draft_type"] == "CLOSE_CASE"
    assert closed_2["case"]["state"] == "CLOSED"


def test_live_classification_ranking_uses_real_catalog_only():
    """LIVE ranking must return entries from the real catalog (never mock IDs) and handle Chinese terms."""
    from app.support_client import DEFAULT_CLASSIFICATIONS, _rank_live_classifications

    catalog = [
        {"id": "REAL-GCE-1", "display_name": "Compute Engine > Connect to a VM"},
        {"id": "REAL-SQL-1", "display_name": "Other Google Cloud Products > Cloud SQL for PostgreSQL > Connectivity"},
        {"id": "REAL-GKE-1", "display_name": "Google Kubernetes Engine > Cluster Autoscaler"},
    ]
    mock_ids = {c["id"] for c in DEFAULT_CLASSIFICATIONS}

    vm = _rank_live_classifications(catalog, "生产环境虚拟机无法 SSH 连接")
    assert vm and vm[0]["id"] == "REAL-GCE-1"

    sql = _rank_live_classifications(catalog, "Cloud SQL PostgreSQL connection timeout")
    assert sql and sql[0]["id"] == "REAL-SQL-1"

    assert _rank_live_classifications(catalog, "你好") == []
    for result in vm + sql:
        assert result["id"] not in mock_ids
