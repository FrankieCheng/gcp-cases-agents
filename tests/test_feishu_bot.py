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

"""Offline tests for the Feishu bot (mocked lark API, support client and agent runner)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import config, feishu_bot
from app.feishu_bot import FeishuBot, build_draft_card, strip_mentions
from app.support_client import CloudSupportClient

BOT_OPEN_ID = "ou_bot0000001"
OWNER = "ou_owner000001"
OTHER = "ou_other000001"
DRAFT_ID = "draft-case-abc123def0"


class _SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _pending_draft(draft_id: str = DRAFT_ID, status: str = "PENDING_CONFIRMATION") -> dict:
    return {
        "draft_id": draft_id,
        "draft_type": "CREATE_CASE",
        "status": status,
        "project_id": "gcp-prod-core-01",
        "payload": {
            "display_name": "Cloud SQL 连接超时",
            "description": "生产 Cloud SQL 实例连接频繁超时，影响业务。",
            "severity": "S2",
            "classification_display_name": "Cloud SQL > Connectivity",
            "mode": "LIVE",
            "test_case": True,
        },
    }


def _make_bot(support=None, turn_runner=None) -> tuple[FeishuBot, MagicMock, MagicMock]:
    api = MagicMock()
    support = support or MagicMock()
    runner = turn_runner or MagicMock(return_value={"reply": "ok", "new_draft_ids": []})
    bot = FeishuBot(
        api,
        runner,
        executor=_SyncExecutor(),
        support_client_getter=lambda: support,
        error_mapper=lambda exc: SimpleNamespace(status_code=400, detail=str(exc)),
        bot_open_id=BOT_OPEN_ID,
        default_project="gcp-prod-core-01",
        allowed_chat_ids=[],
        patch_delay_seconds=0,
    )
    return bot, api, support


def _msg_event(
    text: str = "@_user_1 帮我提一个 Cloud SQL 连接超时的 S2 工单",
    chat_type: str = "group",
    mentions: list | None = None,
    message_id: str = "om_1",
    message_type: str = "text",
    sender: str = OWNER,
):
    if mentions is None:
        mentions = [SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=BOT_OPEN_ID), mentioned_type="bot")]
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender), sender_type="user"),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="oc_chat1",
                chat_type=chat_type,
                message_type=message_type,
                content=json.dumps({"text": text}),
                mentions=mentions,
            ),
        )
    )


def _card_event(action: str, operator: str, draft_id: str = DRAFT_ID):
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=operator),
            action=SimpleNamespace(value={"action": action, "draft_id": draft_id}),
            context=SimpleNamespace(open_message_id="om_card1", open_chat_id="oc_chat1"),
        )
    )


def test_strip_mentions() -> None:
    assert strip_mentions("@_user_1  帮我 @_user_23 提工单 @_all ") == "帮我 提工单"
    assert strip_mentions("@_user_1") == ""


def test_group_message_without_bot_mention_is_ignored() -> None:
    bot, api, _ = _make_bot()
    other_mention = [SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=OTHER), mentioned_type="user")]
    bot.handle_message_event(_msg_event(mentions=other_mention))
    bot.handle_message_event(_msg_event(mentions=[], message_id="om_2"))
    bot._turn_runner.assert_not_called()
    api.reply_text.assert_not_called()


def _no_mention_event(message_id: str, sender: str = OWNER, text: str = "S2，生产环境", parent_id: str = ""):
    ev = _msg_event(text=text, mentions=[], message_id=message_id, sender=sender)
    ev.event.message.parent_id = parent_id
    return ev


def test_group_followup_without_mention_continues_conversation() -> None:
    bot, _api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(message_id="om_a"))  # @bot starts the conversation
    bot.handle_message_event(_no_mention_event("om_b"))  # same user, no @
    assert bot._turn_runner.call_count == 2
    user_id, session_id, text, *_ = bot._turn_runner.call_args.args
    assert (user_id, session_id, text) == (f"feishu:{OWNER}", f"feishu-oc_chat1-{OWNER}", "S2，生产环境")


def test_group_followup_ignored_for_other_user_mentions_or_disabled() -> None:
    bot, _api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(message_id="om_a"))
    bot.handle_message_event(_no_mention_event("om_b", sender=OTHER))  # different user
    at_other = _msg_event(
        message_id="om_c",
        mentions=[SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=OTHER), mentioned_type="user")],
    )
    bot.handle_message_event(at_other)  # owner talking to someone else
    assert bot._turn_runner.call_count == 1

    disabled, _api2, support2 = _make_bot()
    disabled._followup_seconds = 0
    support2.list_pending_drafts.return_value = []
    disabled.handle_message_event(_msg_event(message_id="om_d"))
    disabled.handle_message_event(_no_mention_event("om_e"))
    assert disabled._turn_runner.call_count == 1


def test_group_reply_to_bot_message_is_handled() -> None:
    bot, api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot._followup_seconds = 0  # only the reply-to-bot rule applies
    api.reply_markdown.return_value = "om_bot_reply"
    bot.handle_message_event(_msg_event(message_id="om_a"))
    bot.handle_message_event(_no_mention_event("om_b", sender=OTHER, parent_id="om_bot_reply"))
    assert bot._turn_runner.call_count == 2
    assert bot._turn_runner.call_args.args[0] == f"feishu:{OTHER}"


def test_markdown_reply_falls_back_to_text_and_strips_at_tags() -> None:
    bot, api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    api.reply_markdown.side_effect = RuntimeError("card rejected")
    bot.handle_message_event(_msg_event(text="查看工单", chat_type="p2p", mentions=[]))
    api.reply_text.assert_called_once_with("om_1", "ok")
    card = feishu_bot.build_markdown_card('**粗体** <at user_id="all">所有人</at>')
    content = card["elements"][0]["content"]
    assert "<at" not in content and "**粗体**" in content


def test_group_turn_gets_recent_group_context_note() -> None:
    bot, _api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot._reply_classifier = MagicMock(return_value=True)
    bot.handle_message_event(_msg_event(message_id="om_a", text="@_user_1 查一下工单 84920101"))
    bot.handle_message_event(_no_mention_event("om_b", sender=OTHER, text="这个工单是什么级别"))
    note = bot._turn_runner.call_args.kwargs["context_note"]
    assert "飞书群聊" in note and "84920101" in note and "不要执行" in note


def test_classifier_uses_group_context_to_decide() -> None:
    classifier = MagicMock(return_value=True)
    bot, _api, support = _make_bot()
    bot._reply_classifier = classifier
    support.list_pending_drafts.return_value = []
    runner_reply = {"reply": "请问严重级别是？", "new_draft_ids": []}
    bot._turn_runner.return_value = runner_reply

    bot.handle_message_event(_msg_event(message_id="om_a"))  # @bot, bot asks a question
    # A different member answers the bot's question without @: classifier sees the context.
    bot.handle_message_event(_no_mention_event("om_b", sender=OTHER, text="S2，影响生产"))
    assert bot._turn_runner.call_count == 2
    context, speaker, text = classifier.call_args.args
    assert text == "S2，影响生产"
    assert speaker.startswith("成员-")
    assert any(s.startswith("成员-") and "Cloud SQL" in t for s, t in context)
    assert any(s == feishu_bot.BOT_SPEAKER and "严重级别" in t for s, t in context)


def test_classifier_rejects_chitchat_even_in_window() -> None:
    bot, _api, support = _make_bot()
    bot._reply_classifier = MagicMock(return_value=False)
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(message_id="om_a"))
    bot.handle_message_event(_no_mention_event("om_b", text="中午一起吃饭吗"))
    assert bot._turn_runner.call_count == 1


def test_classifier_not_called_when_bot_not_engaged() -> None:
    classifier = MagicMock(return_value=True)
    bot, _api, _support = _make_bot()
    bot._reply_classifier = classifier
    bot.handle_message_event(_no_mention_event("om_x", text="大家好"))
    classifier.assert_not_called()
    bot._turn_runner.assert_not_called()


def test_classifier_error_falls_back_to_window() -> None:
    bot, _api, support = _make_bot()
    bot._reply_classifier = MagicMock(side_effect=RuntimeError("vertex down"))
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(message_id="om_a"))
    bot.handle_message_event(_no_mention_event("om_b"))  # owner in window -> handled
    bot.handle_message_event(_no_mention_event("om_c", sender=OTHER))  # other user -> ignored
    assert bot._turn_runner.call_count == 2


def test_p2p_message_is_handled_and_session_identity() -> None:
    bot, api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(text="查看工单", chat_type="p2p", mentions=[]))
    bot._turn_runner.assert_called_once()
    user_id, session_id, text, project, _sel = bot._turn_runner.call_args.args
    assert user_id == f"feishu:{OWNER}"
    assert session_id == f"feishu-oc_chat1-{OWNER}"
    assert text == "查看工单"
    assert project == "gcp-prod-core-01"
    api.reply_markdown.assert_called_once_with("om_1", "ok")
    note = bot._turn_runner.call_args.kwargs["context_note"]
    assert "飞书私聊" in note and "群聊最近消息" not in note


def test_non_text_message_gets_hint() -> None:
    bot, api, _ = _make_bot()
    bot.handle_message_event(_msg_event(message_type="audio"))
    bot._turn_runner.assert_not_called()
    api.reply_text.assert_called_once()
    assert "文本" in api.reply_text.call_args.args[1]


def test_duplicate_message_id_is_processed_once() -> None:
    bot, _api, support = _make_bot()
    support.list_pending_drafts.return_value = []
    bot.handle_message_event(_msg_event(message_id="om_dup"))
    bot.handle_message_event(_msg_event(message_id="om_dup"))
    assert bot._turn_runner.call_count == 1
    assert bot._turn_runner.call_args.args[2] == "帮我提一个 Cloud SQL 连接超时的 S2 工单"


def test_new_draft_sends_card_with_draft_id_and_records_owner() -> None:
    support = MagicMock()
    support.list_pending_drafts.return_value = []
    support.get_draft.return_value = _pending_draft()
    support.claim_draft_owner.return_value = True
    runner = MagicMock(return_value={"reply": "已生成草稿", "new_draft_ids": [DRAFT_ID]})
    bot, api, _ = _make_bot(support=support, turn_runner=runner)

    bot.handle_message_event(_msg_event())

    support.claim_draft_owner.assert_called_once_with(DRAFT_ID, OWNER)
    api.reply_card.assert_called_once()
    message_id, card = api.reply_card.call_args.args
    assert message_id == "om_1"
    buttons = [a for el in card["elements"] if el["tag"] == "action" for a in el["actions"]]
    assert {b["value"]["action"] for b in buttons} == {"confirm", "cancel"}
    assert all(b["value"]["draft_id"] == DRAFT_ID for b in buttons)
    card_text = json.dumps(card, ensure_ascii=False)
    assert "确认提交" in card_text and "取消" in card_text and "test_case=true" in card_text


def test_preexisting_draft_does_not_get_card() -> None:
    support = MagicMock()
    support.list_pending_drafts.return_value = [_pending_draft()]
    runner = MagicMock(return_value={"reply": "x", "new_draft_ids": [DRAFT_ID]})
    bot, api, _ = _make_bot(support=support, turn_runner=runner)
    bot.handle_message_event(_msg_event())
    support.claim_draft_owner.assert_not_called()
    api.reply_card.assert_not_called()


def test_non_owner_card_click_is_rejected() -> None:
    support = MagicMock()
    support.get_draft_owner.return_value = OWNER
    support.get_draft.return_value = _pending_draft()
    bot, api, _ = _make_bot(support=support)

    for action in ("confirm", "cancel"):
        resp = bot.handle_card_action(_card_event(action, OTHER))
        assert resp["toast"]["type"] == "warning"
        assert "只有发起人" in resp["toast"]["content"]
        assert "card" not in resp
    support.execute_confirmed_draft.assert_not_called()
    support.cancel_draft.assert_not_called()
    api.patch_card.assert_not_called()


def test_owner_confirm_executes_once() -> None:
    support = MagicMock()
    support.get_draft_owner.return_value = OWNER
    support.get_draft.return_value = _pending_draft()
    support.execute_confirmed_draft.return_value = {
        "draft_type": "CREATE_CASE",
        "case": {"name": "projects/gcp-prod-core-01/cases/123"},
    }
    bot, api, _ = _make_bot(support=support)

    resp = bot.handle_card_action(_card_event("confirm", OWNER))

    assert resp["toast"]["content"] == "正在提交…"
    assert resp["card"]["type"] == "raw"
    support.execute_confirmed_draft.assert_called_once_with(DRAFT_ID, confirmed=True)
    api.patch_card.assert_called_once()
    msg_id, card = api.patch_card.call_args.args
    assert msg_id == "om_card1"
    assert "projects/gcp-prod-core-01/cases/123" in json.dumps(card, ensure_ascii=False)


def test_confirm_error_is_surfaced_on_card() -> None:
    support = MagicMock()
    support.get_draft_owner.return_value = OWNER
    support.get_draft.return_value = _pending_draft()
    support.execute_confirmed_draft.side_effect = PermissionError("403 caller lacks permission")
    bot, api, _ = _make_bot(support=support)
    bot.handle_card_action(_card_event("confirm", OWNER))
    card = api.patch_card.call_args.args[1]
    assert "403 caller lacks permission" in json.dumps(card, ensure_ascii=False)
    assert card["header"]["template"] == "red"


def test_already_confirmed_draft_is_not_executed_again() -> None:
    support = MagicMock()
    support.get_draft_owner.return_value = OWNER
    support.get_draft.return_value = _pending_draft(status="CONFIRMED")
    bot, _api, _ = _make_bot(support=support)
    resp = bot.handle_card_action(_card_event("confirm", OWNER))
    assert "cannot be confirmed again" in resp["toast"]["content"]
    support.execute_confirmed_draft.assert_not_called()


def test_owner_cancel() -> None:
    support = MagicMock()
    support.get_draft_owner.return_value = OWNER
    support.get_draft.return_value = _pending_draft()
    support.cancel_draft.return_value = _pending_draft(status="CANCELLED")
    bot, _api, _ = _make_bot(support=support)
    resp = bot.handle_card_action(_card_event("cancel", OWNER))
    support.cancel_draft.assert_called_once_with(DRAFT_ID)
    support.execute_confirmed_draft.assert_not_called()
    assert resp["toast"]["type"] == "success"
    assert resp["card"]["data"]["header"]["template"] == "grey"


def test_chat_text_confirm_never_executes_draft(tmp_path: Path) -> None:
    """Text like '确认 draft-...' goes through the agent turn only; nothing is executed."""
    support = MagicMock()
    support.list_pending_drafts.return_value = []
    bot, _api, _ = _make_bot(support=support)
    bot.handle_message_event(_msg_event(text=f"@_user_1 确认 {DRAFT_ID}"))
    support.execute_confirmed_draft.assert_not_called()
    support.cancel_draft.assert_not_called()


def test_draft_owner_persisted_in_db(tmp_path: Path) -> None:
    config.set_support_mode("MOCK")
    client = CloudSupportClient(db_path=tmp_path / "owner.db")
    draft = client.create_case_draft(
        project_id="gcp-prod-core-01",
        display_name="Owner persistence",
        description="Verify draft owner column is stored server-side.",
    )
    assert client.get_draft_owner(draft["draft_id"]) is None
    assert client.claim_draft_owner(draft["draft_id"], OWNER) is True
    assert client.claim_draft_owner(draft["draft_id"], OTHER) is False
    # Re-opening runs the migration again safely.
    reopened = CloudSupportClient(db_path=tmp_path / "owner.db")
    assert reopened.get_draft_owner(draft["draft_id"]) == OWNER


def test_end_to_end_with_shared_agent_turn(tmp_path: Path) -> None:
    from app.fast_api_app import run_agent_turn
    from app.support_client import set_support_client

    config.set_support_mode("MOCK")
    client = CloudSupportClient(db_path=tmp_path / "e2e.db")
    set_support_client(client)
    try:
        api = MagicMock()
        bot = FeishuBot(
            api,
            run_agent_turn,
            executor=_SyncExecutor(),
            support_client_getter=lambda: client,
            bot_open_id=BOT_OPEN_ID,
            default_project="gcp-prod-core-01",
            allowed_chat_ids=[],
            patch_delay_seconds=0,
        )
        bot.handle_message_event(_msg_event())
        api.reply_card.assert_called_once()
        card = api.reply_card.call_args.args[1]
        draft_id = next(
            a["value"]["draft_id"] for el in card["elements"] if el["tag"] == "action" for a in el["actions"]
        )
        assert client.get_draft_owner(draft_id) == OWNER
        resp = bot.handle_card_action(_card_event("confirm", OWNER, draft_id))
        assert resp["toast"]["content"] == "正在提交…"
        assert client.get_draft(draft_id)["status"] == "CONFIRMED"
        replay = bot.handle_card_action(_card_event("confirm", OWNER, draft_id))
        assert "cannot be confirmed again" in replay["toast"]["content"]
    finally:
        set_support_client(None)


def test_card_json_is_plain_text_for_user_content() -> None:
    draft = _pending_draft()
    draft["payload"]["display_name"] = "<at id=all></at> spam"
    card = build_draft_card(draft, owner_open_id=OWNER)
    assert card["elements"][0]["text"]["tag"] == "plain_text"


def test_bot_does_not_start_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.setenv("FEISHU_APP_SECRET", "x")
    started = MagicMock()
    monkeypatch.setattr(feishu_bot.threading, "Thread", started)
    assert feishu_bot.start_feishu_bot_if_configured(turn_runner=MagicMock()) is None
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    assert feishu_bot.start_feishu_bot_if_configured(turn_runner=MagicMock()) is None
    started.assert_not_called()
