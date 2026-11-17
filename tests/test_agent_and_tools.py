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

"""Unit tests for Google ADK Agent, FunctionTools, and `guard_tool_execution` callback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.agent import guard_tool_execution, root_agent, run_deterministic_agent_turn
from app.support_client import CloudSupportClient, get_support_client, set_support_client
from app.tools import (
    HUMAN_ONLY_TOOL_NAMES,
    get_support_case_details,
    list_support_cases,
    prepare_case_draft,
)

SEEDED_CASE = "projects/gcp-prod-core-01/cases/84920101"


@pytest.fixture(autouse=True)
def setup_isolated_db(tmp_path: Path) -> None:
    config.set_support_mode("MOCK")
    client = CloudSupportClient(db_path=tmp_path / "agent_test.db")
    set_support_client(client)


def _named_tool(name: str) -> SimpleNamespace:
    """Stand-in for an ADK tool object as passed to before_tool_callback."""
    return SimpleNamespace(name=name)


def test_agent_has_no_execution_tools() -> None:
    """The model's toolset must contain only read/draft tools, never create/escalate/close."""
    assert root_agent.name == "cloud_case_agent"
    assert root_agent.before_tool_callback is guard_tool_execution
    tool_names = {getattr(t, "name", getattr(t, "__name__", "")) for t in root_agent.tools}
    assert {
        "search_case_classifications",
        "prepare_case_draft",
        "prepare_escalation_draft",
        "prepare_close_draft",
    } <= tool_names
    assert tool_names.isdisjoint(HUMAN_ONLY_TOOL_NAMES)
    assert "close_support_case" not in tool_names


def test_guard_tool_execution_blocks_unauthorized_project() -> None:
    res = guard_tool_execution(
        list_support_cases,
        {"project_id": "gcp-rogue-project-999", "state_filter": "ALL"},
        tool_context=None,
    )
    assert res is not None
    assert res["status"] == "blocked_by_security_guardrail"
    assert res["reason"] == "PROJECT_NOT_ALLOWED"

    res_case = guard_tool_execution(
        get_support_case_details,
        {"case_name": "projects/gcp-rogue-project-999/cases/12345678"},
        tool_context=None,
    )
    assert res_case is not None
    assert res_case["reason"] == "PROJECT_NOT_ALLOWED"


@pytest.mark.parametrize("tool_name", sorted(HUMAN_ONLY_TOOL_NAMES))
def test_guard_blocks_human_only_tools_even_with_valid_confirmed_draft(tool_name: str) -> None:
    """Previously the guard trusted a model-supplied confirmed=True. Now it must always block."""
    draft_resp = prepare_case_draft(
        project_id="gcp-prod-core-01",
        display_name="Vertex AI prediction endpoint 502 error",
        description="Online prediction endpoint returns 502 Bad Gateway under burst traffic.",
        severity="S2",
        classification_id="",
        classification_display_name="",
        subscriber_emails="ml-ops@example.com",
        time_zone="UTC",
    )
    draft_id = draft_resp["draft"]["draft_id"]

    res = guard_tool_execution(
        _named_tool(tool_name),
        {"draft_id": draft_id, "confirmed": True, "case_name": SEEDED_CASE},
        tool_context=None,
    )
    assert res is not None
    assert res["reason"] == "HUMAN_CONFIRMATION_ONLY"
    assert get_support_client().get_draft(draft_id)["status"] == "PENDING_CONFIRMATION"


def test_guard_fails_closed_on_unregistered_tool() -> None:
    res = guard_tool_execution(_named_tool("delete_everything"), {}, tool_context=None)
    assert res is not None
    assert res["reason"] == "TOOL_NOT_ALLOWED"


def test_deterministic_turn_creates_draft_but_chat_cannot_confirm() -> None:
    # 1. Incomplete prompt triggers clarification question without creating a draft
    clarify_turn = run_deterministic_agent_turn(
        message="我要提单",
        active_project="gcp-prod-core-01",
    )
    assert "请补充以下信息" in clarify_turn["reply"]
    assert len(clarify_turn["pending_drafts"]) == 0

    # 2. Detailed fault description searches classification and creates a HITL draft
    draft_turn = run_deterministic_agent_turn(
        message="帮我在项目 gcp-prod-core-01 提一个 S2 工单：生产环境 Cloud SQL PostgreSQL 实例连接超时 connection timeout",
        active_project="gcp-prod-core-01",
    )
    assert len(draft_turn["pending_drafts"]) == 1
    draft = draft_turn["pending_drafts"][0]
    assert draft["draft_type"] == "CREATE_CASE"
    executed_tools = [tc["tool"] for tc in draft_turn["tool_calls"]]
    assert "search_case_classifications" in executed_tools
    assert "prepare_case_draft" in executed_tools

    # 3. Typing "confirm <draft_id>" in chat must NOT execute the draft
    confirm_turn = run_deterministic_agent_turn(
        message=f"确认提交 {draft['draft_id']}",
        active_project="gcp-prod-core-01",
    )
    assert "确认按钮" in confirm_turn["reply"]
    assert confirm_turn["tool_calls"] == []
    assert get_support_client().get_draft(draft["draft_id"])["status"] == "PENDING_CONFIRMATION"


def test_deterministic_close_intent_creates_draft_and_respects_negation() -> None:
    before = get_support_client().get_case(SEEDED_CASE)["state"]
    assert before != "CLOSED"

    # Negated request must not create a draft or close the case
    neg_turn = run_deterministic_agent_turn(
        message=f"不要关闭工单 {SEEDED_CASE}",
        active_project="gcp-prod-core-01",
    )
    assert neg_turn["pending_drafts"] == []
    assert neg_turn["tool_calls"] == []

    # Close request only creates a CLOSE_CASE draft
    close_turn = run_deterministic_agent_turn(
        message=f"关闭工单 {SEEDED_CASE}",
        active_project="gcp-prod-core-01",
    )
    assert len(close_turn["pending_drafts"]) == 1
    assert close_turn["pending_drafts"][0]["draft_type"] == "CLOSE_CASE"
    assert get_support_client().get_case(SEEDED_CASE)["state"] == before
