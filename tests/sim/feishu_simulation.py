"""Feishu bot end-to-end simulation with the REAL agent (Gemini via Vertex) and MOCK Support data.

Runs the production FeishuBot logic (message routing, group-context classifier, HITL cards,
owner-only confirm/cancel) against simulated Feishu events and a fake Feishu API that records
everything the bot would send. No Feishu traffic, no real Support cases.

Usage (from repo root):
  GOOGLE_APPLICATION_CREDENTIALS=<sa adc.json> uv run python <this file> [scenario ...]
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import traceback
import zlib
from types import SimpleNamespace

DATA_DIR = tempfile.mkdtemp(prefix="feishu-sim-")
os.environ.update(
    {
        "SUPPORT_API_MODE": "MOCK",
        "CHAT_ENGINE": "llm",
        "CASE_AGENT_DATA_DIR": DATA_DIR,
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-core-prod"),
        "GOOGLE_CLOUD_LOCATION": "global",
    }
)
os.environ.pop("FEISHU_APP_ID", None)
os.environ.pop("FEISHU_APP_SECRET", None)
sys.path.insert(0, os.getcwd())

from app import feishu_bot  # noqa: E402
from app.fast_api_app import run_agent_turn  # noqa: E402
from app.support_client import get_support_client  # noqa: E402

BOT = "ou_botsim000001"
U1 = "ou_user1sim0001"
U2 = "ou_user2sim0002"
GROUP = "oc_groupsim0001"
DM = "oc_dmsim0000001"
PROJECT = "gcp-prod-core-01"


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.patched: list[dict] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"om_bot{self._n:04d}"

    def reply_text(self, message_id: str, text: str) -> str:
        mid = self._id()
        self.sent.append({"id": mid, "reply_to": message_id, "type": "text", "text": text})
        return mid

    def reply_markdown(self, message_id: str, text: str) -> str:
        json.dumps(feishu_bot.build_markdown_card(text))
        mid = self._id()
        self.sent.append({"id": mid, "reply_to": message_id, "type": "rich", "text": text})
        return mid

    def reply_card(self, message_id: str, card: dict) -> str:
        json.dumps(card)  # must be serialisable
        mid = self._id()
        self.sent.append({"id": mid, "reply_to": message_id, "type": "card", "card": card})
        return mid

    def patch_card(self, message_id: str, card: dict) -> None:
        self.patched.append({"id": message_id, "card": card})

    def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str = "image"
    ) -> tuple[bytes, str]:
        # Generate a valid 16x16 RGB PNG so Vertex AI Gemini multimodal decoder accepts it cleanly
        w, h = 16, 16
        raw_scanlines = b"".join(b"\x00" + (b"\xd9\x30\x25" * w) for _ in range(h))

        def _chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw_scanlines))
            + _chunk(b"IEND", b"")
        )
        return png_bytes, f"{file_key}.png"


class SyncExecutor:
    def submit(self, fn, *a, **k):
        fn(*a, **k)


_msg_n = 0


def msg(text: str, sender: str, chat: str, *, at_bot: bool = False, at_other: bool = False,
        parent_id: str = "", message_type: str = "text"):
    global _msg_n
    _msg_n += 1
    mentions = []
    if at_bot:
        mentions.append(SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=BOT), mentioned_type="bot"))
        text = "@_user_1 " + text
    if at_other:
        mentions.append(SimpleNamespace(key="@_user_2", id=SimpleNamespace(open_id="ou_someone"), mentioned_type="user"))
        text = "@_user_2 " + text
    return SimpleNamespace(event=SimpleNamespace(
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender), sender_type="user"),
        message=SimpleNamespace(
            message_id=f"om_user{_msg_n:04d}", chat_id=chat,
            chat_type="p2p" if chat == DM else "group", message_type=message_type,
            content=json.dumps({"text": text}), mentions=mentions, parent_id=parent_id,
        ),
    ))


def click(action: str, draft_id: str, operator: str, card_msg_id: str, chat: str = GROUP):
    return SimpleNamespace(event=SimpleNamespace(
        operator=SimpleNamespace(open_id=operator),
        action=SimpleNamespace(value={"action": action, "draft_id": draft_id}),
        context=SimpleNamespace(open_message_id=card_msg_id, open_chat_id=chat),
    ))


def card_draft_id(entry: dict) -> str | None:
    for el in entry["card"]["elements"]:
        if el.get("tag") == "action":
            return el["actions"][0]["value"]["draft_id"]
    return None


def card_text(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


class Sim:
    def __init__(self) -> None:
        self.api = FakeApi()
        self.bot = feishu_bot.FeishuBot(
            self.api, run_agent_turn, executor=SyncExecutor(), bot_open_id=BOT,
            default_project=PROJECT, allowed_chat_ids=[], patch_delay_seconds=0,
        )
        self.support = get_support_client()

    def say(self, ev) -> list[dict]:
        before = len(self.api.sent)
        t0 = time.monotonic()
        self.bot.handle_message_event(ev)
        out = self.api.sent[before:]
        m = ev.event.message
        who = "U1" if ev.event.sender.sender_id.open_id == U1 else "U2"
        print(f"\n  [{who}{' @bot' if any(x.id.open_id == BOT for x in m.mentions) else ''}] "
              f"{json.loads(m.content)['text']}   ({time.monotonic() - t0:.1f}s)")
        for o in out:
            if o["type"] == "card":
                print(f"    <BOT CARD> {o['card']['header']['title']['content']} draft={card_draft_id(o)}")
            else:
                print("    <BOT> " + o["text"].replace("\n", "\n          ")[:900])
        if not out:
            print("    (bot stayed silent)")
        return out

    def cards(self, out: list[dict]) -> list[dict]:
        return [o for o in out if o["type"] == "card"]


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}: {name}{(' - ' + detail) if detail and not cond else ''}")


def converse_until_card(sim: Sim, first, followups: list) -> tuple[list[dict], list[dict]]:
    """Send the first message, then follow-ups until a card appears. Returns (all_out, cards)."""
    all_out = sim.say(first)
    cards = sim.cards(all_out)
    for f in followups:
        if cards:
            break
        out = sim.say(f)
        all_out += out
        cards = sim.cards(out)
    return all_out, cards


# ----------------------------------------------------------------------------- scenarios
def s_dm_create_confirm(sim: Sim) -> None:
    print("\n=== S1 DM: multi-turn create -> owner confirms -> replay blocked")
    out, cards = converse_until_card(
        sim,
        msg("我们生产环境的 GKE 集群节点一直 NotReady，帮我开个工单", U1, DM),
        [msg("S2，就用默认项目，今天早上 9 点开始，影响线上下单服务", U1, DM),
         msg("信息就这些，请直接生成工单草稿", U1, DM)],
    )
    check("S1 bot replied in DM", bool(out))
    check("S1 create draft card sent", len(cards) == 1)
    if not cards:
        return
    draft_id = card_draft_id(cards[0])
    check("S1 owner recorded = requester", sim.support.get_draft_owner(draft_id) == U1)
    check("S1 card shows project/severity/LIVE-or-MOCK note",
          PROJECT in card_text(cards[0]["card"]) and "严重级别" in card_text(cards[0]["card"]))
    resp = sim.bot.handle_card_action(click("confirm", draft_id, U1, cards[0]["id"], DM))
    check("S1 confirm returns 正在提交… toast", resp["toast"]["content"] == "正在提交…")
    patched = sim.api.patched[-1]["card"] if sim.api.patched else {}
    check("S1 card patched to success with case name",
          patched.get("header", {}).get("template") == "green" and "projects/" in card_text(patched),
          card_text(patched)[:300])
    check("S1 draft CONFIRMED", sim.support.get_draft(draft_id)["status"] == "CONFIRMED")
    replay = sim.bot.handle_card_action(click("confirm", draft_id, U1, cards[0]["id"], DM))
    check("S1 replay rejected", "cannot be confirmed again" in replay["toast"]["content"])


def s_group_followups(sim: Sim) -> None:
    print("\n=== S2 Group: @ start, follow-ups without @, chit-chat ignored, non-owner click rejected")
    first = sim.say(msg("帮忙提交一个case", U1, GROUP, at_bot=True))
    check("S2 bot replied to @", bool(first))
    out = sim.say(msg("Cloud SQL 实例 prod-mysql-01 连接超时，S2，从昨晚 10 点开始，影响支付服务", U1, GROUP))
    check("S2 owner follow-up without @ handled", bool(out))
    cards = sim.cards(first + out)
    if not cards:
        out2 = sim.say(msg("就这些信息，帮我生成草稿吧", U1, GROUP))
        cards = sim.cards(out2)
    chit = sim.say(msg("中午大家想吃什么？", U2, GROUP))
    check("S2 other member chit-chat ignored", not chit)
    at_other = sim.say(msg("你那边先看下网络", U1, GROUP, at_other=True))
    check("S2 owner @-ing someone else ignored", not at_other)
    check("S2 create card sent in group", len(cards) >= 1)
    if cards:
        draft_id = card_draft_id(cards[-1])
        resp = sim.bot.handle_card_action(click("confirm", draft_id, U2, cards[-1]["id"]))
        check("S2 non-owner confirm rejected", "只有发起人" in resp["toast"]["content"])
        check("S2 draft still pending after non-owner click",
              sim.support.get_draft(draft_id)["status"] == "PENDING_CONFIRMATION")
        txt = sim.say(msg(f"确认 {draft_id}", U1, GROUP, at_bot=True))
        check("S2 chat text '确认 draft' does not execute",
              sim.support.get_draft(draft_id)["status"] == "PENDING_CONFIRMATION", str(txt)[:200])
        reply = " ".join(o.get("text", "") for o in txt)
        check("S2 reply points to the in-chat card (no portal detour)",
              "卡片" in reply and "Portal" not in reply and "门户" not in reply, reply[:200])
        resp = sim.bot.handle_card_action(click("cancel", draft_id, U1, cards[-1]["id"]))
        check("S2 owner cancel works",
              resp["toast"]["type"] == "success"
              and sim.support.get_draft(draft_id)["status"] == "CANCELLED")


def s_group_other_member_supplement(sim: Sim) -> None:
    print("\n=== S3 Group: another member supplements while bot is engaged")
    sim.say(msg("帮我查一下工单 84920101 的最新进展", U1, GROUP, at_bot=True))
    out = sim.say(msg("顺便问下，这个工单现在是什么严重级别？", U2, GROUP))
    check("S3 other member's question to bot (context) handled", bool(out))
    reply = " ".join(o.get("text", "") for o in out)
    check("S3 answer uses group context (84920101 is S2)", "S2" in reply, reply[:200])


def s_escalate_close(sim: Sim) -> None:
    print("\n=== S4 Escalate + close via @ (cards, owner cancel)")
    out, cards = converse_until_card(
        sim,
        msg("升级工单 projects/gcp-prod-core-01/cases/84920101，主备切换后订单失败率 8%，业务影响很大", U1, GROUP, at_bot=True),
        [msg("原因是业务影响，请直接生成升级草稿", U1, GROUP)],
    )
    esc = [c for c in cards if "升级" in c["card"]["header"]["title"]["content"]]
    check("S4 escalation card sent", bool(esc), str([o.get("text", "")[:120] for o in out]))
    out, cards = converse_until_card(
        sim,
        msg("关闭工单 projects/gcp-prod-core-01/cases/84920102，问题已经解决了", U2, GROUP, at_bot=True),
        [msg("确认要关闭，请生成关闭草稿", U2, GROUP)],
    )
    cl = [c for c in cards if "关闭" in c["card"]["header"]["title"]["content"]]
    check("S4 close card sent", bool(cl), str([o.get("text", "")[:120] for o in out]))
    if cl:
        did = card_draft_id(cl[0])
        check("S4 close card owned by U2", sim.support.get_draft_owner(did) == U2)
        r = sim.bot.handle_card_action(click("confirm", did, U1, cl[0]["id"]))
        check("S4 U1 cannot confirm U2's close card", "只有发起人" in r["toast"]["content"])


def s_query(sim: Sim) -> None:
    print("\n=== S5 Query cases in DM")
    out = sim.say(msg("列出当前项目的所有工单", U2, DM))
    check("S5 list mentions seeded case", any("84920101" in o.get("text", "") for o in out))
    out = sim.say(msg("查看工单 84920102 的详情", U2, DM))
    check("S5 details reply", any("84920102" in o.get("text", "") for o in out))


def s_misc(sim: Sim) -> None:
    print("\n=== S6 Misc: unsupported media, empty @, no-engagement silence")
    out = sim.say(msg("", U1, DM, message_type="audio"))
    check("S6 unsupported media gets hint", any("文本" in o.get("text", "") for o in out))
    out = sim.say(msg("", U1, GROUP, at_bot=True))
    check("S6 empty @ gets greeting", bool(out))
    fresh = Sim()
    out = fresh.say(msg("今天天气不错", U2, "oc_othergroup01"))
    check("S6 unrelated group message ignored", not out)


def s_error_surface(sim: Sim) -> None:
    print("\n=== S7 Support API error surfaces on card")
    from google.api_core import exceptions as gexc

    draft = sim.support.create_case_draft(PROJECT, "错误映射测试", "用于验证 Support API 错误在卡片上的展示。")
    sim.support.claim_draft_owner(draft["draft_id"], U1)
    orig = sim.support.execute_confirmed_draft
    sim.support.execute_confirmed_draft = lambda *a, **k: (_ for _ in ()).throw(
        gexc.BadRequest("You are not eligible to create a case with this channel."))
    try:
        sim.bot.handle_card_action(click("confirm", draft["draft_id"], U1, "om_errcard"))
    finally:
        sim.support.execute_confirmed_draft = orig
    patched = sim.api.patched[-1]["card"]
    check("S7 error card red with Google API detail",
          patched["header"]["template"] == "red" and "not eligible" in card_text(patched))


def s_multimodal_revise_and_reply(sim: Sim) -> None:
    print("\n=== S8 Multimodal post image + revise draft + quote-reply + closed follow-up")
    global _msg_n
    _msg_n += 1
    post_ev = SimpleNamespace(event=SimpleNamespace(
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=U1), sender_type="user"),
        message=SimpleNamespace(
            message_id=f"om_user{_msg_n:04d}",
            chat_id=DM,
            chat_type="p2p",
            message_type="post",
            content=json.dumps({
                "title": "Cloud Run 503 报错截图",
                "content": [[
                    {"tag": "text", "text": f"在 {PROJECT} 项目提交工单，Cloud Run checkout-api 持续返回 503，从今天 10:00 开始，严重级别 S3，附上报错截图，请直接生成工单草稿"},
                    {"tag": "img", "image_key": "img_v3_sim_503"},
                ]],
            }),
            mentions=[],
            parent_id="",
        ),
    ))
    before = len(sim.api.sent)
    sim.bot.handle_message_event(post_ev)
    out = sim.api.sent[before:]
    reply_txt = " ".join(o.get("text", "") for o in out)
    check(
        "S8 bot never claims image upload unsupported",
        "不支持直接作为附件" not in reply_txt and "不支持上传图片" not in reply_txt,
        reply_txt[:240],
    )
    cards = sim.cards(out)
    if not cards:
        out2 = sim.say(msg("信息已齐，请直接生成工单草稿卡片", U1, DM))
        cards = sim.cards(out2)
    check("S8 create draft card generated with image attachment", len(cards) >= 1)
    if cards:
        draft_id = card_draft_id(cards[-1])
        draft = sim.support.get_draft(draft_id)
        pending_atts = draft["payload"].get("pending_attachments") or []
        check("S8 draft payload has pending_attachments", len(pending_atts) == 1, str(pending_atts))
        patches_before = len(sim.api.patched)
        sim.say(msg("还没点确认，帮我把这个待确认草稿的严重级别改成 S2，抄送加上 sre-lead@example.com", U1, DM))
        updated_draft = sim.support.get_draft(draft_id)
        check(
            "S8 revise_case_draft updated severity to S2 & patched card",
            updated_draft["payload"]["severity"] == "S2" and len(sim.api.patched) > patches_before,
            str(updated_draft["payload"]),
        )
        sim.bot.handle_card_action(click("confirm", draft_id, U1, cards[-1]["id"], DM))
        confirmed = sim.support.get_draft(draft_id)
        created_case = str(confirmed.get("result_case_name") or "")
        check(
            "S8 confirmed case uploaded staged screenshot",
            bool(created_case) and len(sim.support.get_case(created_case).get("attachments", [])) >= 1,
            created_case,
        )

    # Simulate TSE notification card message binding & user quote-reply without case ID or keyword
    action_case = created_case or f"projects/{PROJECT}/cases/84920102"
    with sim.support._get_conn() as conn:
        conn.execute("UPDATE cases SET state = 'ACTION_REQUIRED' WHERE case_name = ?", (action_case,))
        conn.commit()
    sim.bot.register_notification_message(
        chat_id=DM,
        message_id="om_tse_notify_action",
        case_name=action_case,
        summary="状态变更: 待客户补充信息；工程师请求补充复现环境配置与时间戳",
    )
    comments_before = len(sim.support.get_case(action_case)["comments"])
    sim.say(msg("我们复现用的是 Cloud Run Gen2，VPC Direct Egress 已开启，复现时间是 14:20 UTC", U1, DM, parent_id="om_tse_notify_action"))
    after_case = sim.support.get_case(action_case)
    check(
        "S8 quote-reply added comment to quoted case & transitioned state",
        len(after_case["comments"]) > comments_before and after_case["state"] == "IN_PROGRESS_GOOGLE_SUPPORT",
        f"comments={len(after_case['comments'])}, state={after_case['state']}",
    )

    # Reply to CLOSED case -> creates follow-up case draft card
    closed_case = "projects/gcp-analytics-data-02/cases/84920103"
    out_closed, closed_cards = converse_until_card(
        sim,
        msg(f"已关闭的工单 {closed_case} 今天凌晨又复发了，BigQuery Slot 再次打满报错，帮我跟进重开", U1, DM),
        [msg("确认基于原工单创建跟进新工单草稿，严重级别 S3", U1, DM)],
    )
    check("S8 closed case follow-up draft card created", len(closed_cards) >= 1, str([o.get("text", "")[:120] for o in out_closed]))


def s_sre_ops_war_room(sim: Sim) -> None:
    print("\n=== S9 Cloud SRE Ops: secret redaction + TSE yaml reader + cross-project search + shift handover")
    from app.security import validate_and_store_raw_bytes

    # 1. SRE pastes command containing Bearer token & DB password -> auto-redacted in draft
    fake_tok = "ya29." + "sim_dummy_token_fixture_999888777"
    out_sec, sec_cards = converse_until_card(
        sim,
        msg(
            f"在 {PROJECT} 紧急提个 S1 工单：Cloud SQL prod-pg-01 主库无法连接，"
            "应用连接串 postgresql://admin:SuperSecretDb99@10.10.0.5:5432/core 报错，"
            f"复现请求带了 Authorization: Bearer {fake_tok}，请直接生成工单草稿",
            U1,
            DM,
        ),
        [msg("信息齐了，请立即生成草稿", U1, DM)],
    )
    check("S9 secret-containing case generated draft card", len(sec_cards) >= 1)
    if sec_cards:
        did = card_draft_id(sec_cards[-1])
        d_obj = sim.support.get_draft(did)
        desc_saved = d_obj["payload"]["description"]
        check(
            "S9 raw DB password & Bearer token scrubbed from draft payload",
            "SuperSecretDb99" not in desc_saved and fake_tok not in desc_saved,
            desc_saved[:240],
        )
        check(
            "S9 S1 readiness tips attached to draft",
            bool(d_obj["payload"].get("diagnostic_tips")),
            str(d_obj["payload"].get("diagnostic_tips")),
        )

    # 2. Seed a TSE diagnostic YAML attachment on 84920101 and ask bot to read it
    tse_yaml = b"# TSE Fix\ntcp_keepalives_idle: 30\ntcp_keepalives_interval: 10\n"
    meta = validate_and_store_raw_bytes(tse_yaml, "tse_pg_tuning.yaml")
    sim.support.add_attachment_metadata(
        case_name=f"projects/{PROJECT}/cases/84920101",
        original_filename=meta["original_filename"],
        stored_filename=meta["stored_filename"],
        mime_type=meta["mime_type"],
        size_bytes=meta["size_bytes"],
    )
    out_att = sim.say(msg(f"帮我读取工单 projects/{PROJECT}/cases/84920101 里的附件 tse_pg_tuning.yaml 内容", U1, DM))
    att_reply = " ".join(o.get("text", "") for o in out_att)
    check(
        "S9 bot read TSE YAML attachment content",
        "tcp_keepalives_interval" in att_reply and "10" in att_reply,
        att_reply[:240],
    )

    # 3. Cross-project search + Shift handover summary
    out_handover = sim.say(msg("我们要交接班了，帮我生成一份所有项目的运维值班巡检与超时工单简报", U1, DM))
    ho_reply = " ".join(o.get("text", "") for o in out_handover)
    check(
        "S9 shift handover report includes multi-project cases & SLA status",
        "84920101" in ho_reply and ("84920104" in ho_reply or "gcp-ai-sandbox-03" in ho_reply),
        ho_reply[:300],
    )


SCENARIOS = {
    "s1": s_dm_create_confirm, "s2": s_group_followups, "s3": s_group_other_member_supplement,
    "s4": s_escalate_close, "s5": s_query, "s6": s_misc, "s7": s_error_surface,
    "s8": s_multimodal_revise_and_reply, "s9": s_sre_ops_war_room,
}


def main() -> int:
    names = sys.argv[1:] or list(SCENARIOS)
    sim = Sim()
    for n in names:
        try:
            SCENARIOS[n](sim)
        except Exception as exc:
            traceback.print_exc()
            check(f"{n} crashed", False, f"{type(exc).__name__}: {exc}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== RESULT: {passed}/{len(RESULTS)} checks passed =====")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {name}: {detail[:300]}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
