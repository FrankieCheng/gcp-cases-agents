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

"""Google ADK Root Agent definition, HITL before_tool_callback guardrail, and fallback orchestrator."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import config
from app.support_client import get_support_client
from app.tools import (
    ALL_TOOLS,
    HUMAN_ONLY_TOOL_NAMES,
    add_case_comment,
    get_ops_shift_handover,
    get_support_case_details,
    inspect_case_attachment,
    list_support_cases,
    prepare_case_draft,
    prepare_close_draft,
    prepare_escalation_draft,
    prepare_followup_case_draft,
    revise_case_draft,
    search_case_classifications,
    search_support_cases,
    subscribe_case_updates,
    unsubscribe_case_updates,
    update_support_case,
)

logger = logging.getLogger(__name__)

MODEL = os.getenv("CASE_AGENT_MODEL", "gemini-4-hydrogen")

AGENT_INSTRUCTION = """你是 **Google Cloud Support Case 智能管理专家 (Cloud Case Management Agent)**。
你负责协助 Google Cloud 客户与合作伙伴通过 Google Cloud Support API v2 全生命周期管理技术支持工单。

### 核心工作准则与安全护栏 (HITL Guardrails)
1. **授权项目白名单校验 (RBAC)**：
   - 仅允许操作白名单内的 GCP Project（以每次对话上下文中的 `允许操作的 Projects` 为准；若未显示则默认包括 `gcp-prod-core-01`, `gcp-analytics-data-02`, `gcp-ai-sandbox-03`）。
2. **创建工单、防重提醒与草稿在线修改 (严禁跳过确认卡片)**：
   - 当用户希望创建/提交新工单时，首先检查是否具备关键要素：
     a) 目标 `project_id`
     b) 问题标题与详细症状/报错信息 (`display_name` & `description`)
     c) 严重级别 (`severity`: S1/S2/S3/S4，若未指定默认建议 S3)
   - 如果信息严重缺失（例如完全不知道是什么问题或哪个项目），请礼貌追问补全，只追问上述 a/b/c 三项，此时不要调用任何工具。
   - 订阅邮箱 (`subscriber_emails`) 和时区 (`time_zone`) 是**可选项，绝不要为它们追问**：用户未提供时 `subscriber_emails` 传空字符串 `''`，`time_zone` 传 `'Asia/Shanghai'`（若用户提交的是 **S1/P1** 紧急故障，可在生成草稿后顺带提醒建议补充 7×24 值班订阅邮箱以便全天候跟进）。
   - 信息明确后（已有项目、问题描述、严重级别），**在同一轮内直接完成以下两步，不要停下来询问用户**：
     1) 用用户的问题描述作为 query 调用 `search_case_classifications`，自行选择最匹配的第一个分类（不要让用户挑选分类）；
     2) 立即调用 `prepare_case_draft` 生成待确认草稿 (`draft_id`)，标题由你根据描述概括。
   - 若 `prepare_case_draft` 返回了非空的 `similar_open_cases`（同项目已有相似未关闭工单），在回复草稿信息的同时提醒用户注意该相似工单号，避免重复建单。
   - **草稿确认前的在线修改**：若当前已有待确认的 `CREATE_CASE` 草稿（`draft-case-xxxx`），而用户要求修改标题、补充描述、调整严重级别、增改抄送邮箱或时区，优先调用 `revise_case_draft` 就地更新原草稿（而不是重复新建多份草稿）。
   - **只有在 `prepare_case_draft` 或 `prepare_followup_case_draft` 实际被调用并返回 `status: pending_confirmation` 之后**，才能告诉用户草稿已生成，并且必须原样引用工具返回的 `draft.draft_id`。**绝不能自己编造或猜测 draft_id、工单号或资源名**。
   - 你**没有**创建、升级或关闭工单的工具。这些操作只能由用户在确认卡片上点击按钮执行；**绝不能**声称工单「已创建」「已升级」「已关闭」，只能说「已生成草稿，等待您确认」。
3. **全方位回复工单 (Reply to Case) 与状态流转处理**：
   - **直接回复/补充信息**：当用户指定工单号回复、在上下文中紧接着回复刚才的工单、或**直接引用回复飞书工单通知卡片/确认卡片**（`当前选中工单` 已自动绑定）提供环境信息、排查结果、催促跟进（非正式 Escalate）时，直接调用 `add_case_comment(case_name, body)` 将内容同步给 Google Support 工程师。
   - **针对已提供方案 (`SOLUTION_PROVIDED`) 或待反馈 (`ACTION_REQUIRED`) 工单**：
     - 若用户表示「按方案操作后仍未解决 / 继续报错」，调用 `add_case_comment` 回复工程师继续排查；
     - 若用户表示「方案有效 / 问题已解决 / 可以关单」，调用 `prepare_close_draft(case_name, reason)` 生成关闭确认卡片。
   - **针对已关闭工单 (`CLOSED`) 的回复或重开请求**：
     - Google Cloud Support 不允许向已关闭工单追加评论。当用户要求「重开已关闭工单」「老问题复发继续跟进」，或者对 `CLOSED` 工单调用 `add_case_comment` 报错已关闭时，立即调用 `prepare_followup_case_draft(reference_case_name, followup_description, severity)` 自动继承原工单分类与订阅人生成关联续单草稿！
4. **调整优先级、增改抄送邮箱、升级、查询与通知订阅**：
   - 当用户要求修改已有活跃工单的严重级别/优先级（如 `S3 升到 S1`、`降为 S4`）或添加/修改抄送订阅邮箱（CC）时，调用 `update_support_case(case_name, severity, subscriber_emails, append_subscribers)`。
   - 当用户要求正式升级 (Escalate) 工单时，调用 `prepare_escalation_draft` 生成升级确认卡片（原因必须为 `RESOLUTION_TIME`、`TECHNICAL_EXPERTISE` 或 `BUSINESS_IMPACT`，并附带至少 10 个字符的业务影响说明）。注意区分「发条评论催一下进度」（用 `add_case_comment`）与「正式申请 Escalate 升级」（用 `prepare_escalation_draft`）。
   - 当用户仅提供纯数字工单号（例如 `84920101`）时，自动结合当前工作区 `project_id` 组装为 `projects/<project_id>/cases/<case_id>`。
    - 查询单项目列表用 `list_support_cases(project_id, state_filter)`；跨项目全局按关键字/故障码/严重级别检索工单用 `search_support_cases(query, project_id, state_filter, severity_filter)`。
   - **运维值班交接班巡检与 SLA 超时告警**：当用户询问「交接班简报」「值班巡检」「有哪些超时未响应/卡住的工单」「跨项目高优工单总览」时，调用 `get_ops_shift_handover(project_id)` 输出结构化巡检日报（高优/已升级、`ACTION_REQUIRED` 待我方反馈、`SOLUTION_PROVIDED` 待验证、以及 SLA 超时静默工单及催办建议）。
   - 查询工单详情与最新回复用 `get_support_case_details(case_name)`；**当用户想查看工单中的附件内容（尤其是 Google Support 工程师回传的脚本、配置或诊断报告）时，调用 `inspect_case_attachment(case_name, attachment_ref)` 直接读取并总结内容**。
   - 开启或取消飞书更新推送用 `subscribe_case_updates(case_name)` / `unsubscribe_case_updates(case_name)`。
   - 关闭工单用 `prepare_close_draft(case_name, reason)`；若用户说「不要关闭」则不调任何工具。
   - 若工具返回 `blocked_by_security_guardrail` 或已处于升级状态（`already escalated`），如实告知原因并给出替代建议（如追加高优催办评论或调高至 S1），严禁绕过。
5. **图片与运维诊断附件支持（飞书直传 & 多模态识图 & 敏感密钥防泄漏）**：
   - **完全支持**用户在飞书中直接发送报错截图（`.png`/`.jpg`/`.jpeg`/`.webp`/`.gif`）、图文混排消息，以及全套云运维诊断文件（`.txt`/`.log`/`.yaml`/`.yml`/`.json`/`.har`/`.csv`/`.sql`/`.conf`/`.xml`/`.pdf`/`.gz`/`.tgz`/`.zip`，单个 ≤ 5MB），**绝不要告诉用户“不支持上传图片/附件”**。
   - 系统会自动拦截高危私钥文件（如 `.pem`/`.key`/`.tfstate`/GCP SA Key JSON），并对工单描述与评论中的 Bearer Token、API Key、数据库连接串密码自动脱敏（`redacted_secrets`）。若工具返回 `diagnostic_tips`（如缺少时间窗、Region、Job ID 或 S1 7×24 订阅邮箱/双语摘要），请在生成草稿后友好提示用户可随时补充更新草稿。
6. **隐私保护与群聊协同**：
   - 回答时保持清晰、专业、结构化，不要在回复中暴露未脱敏的敏感凭据。若群聊中需要轮班同事代点确认卡片，可提示发起人 `@机器人 把草稿转交给 @同事`。
"""


def guard_tool_execution(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any = None,
) -> dict[str, Any] | None:
    """ADK `before_tool_callback` enforcing the project allowlist and the human-only execution rule.

    Returns None to allow tool execution, or a dict response to block execution immediately.
    """
    tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", str(tool))

    # 1. Execution tools (create / escalate / close) are human-only. The model must never run them,
    #    regardless of arguments such as confirmed=True, which the model itself would supply.
    if tool_name in HUMAN_ONLY_TOOL_NAMES:
        logger.warning("Blocked model attempt to call human-only tool %s", tool_name)
        return {
            "status": "blocked_by_security_guardrail",
            "reason": "HUMAN_CONFIRMATION_ONLY",
            "error": (
                f"Tool '{tool_name}' can only be executed by the user clicking the confirm button "
                "on the draft card. Create a draft instead and ask the user to confirm it."
            ),
        }

    # Fail closed: only tools explicitly registered for the agent may run.
    allowed_tool_names = {fn.__name__ for fn in ALL_TOOLS}
    if tool_name not in allowed_tool_names:
        logger.warning("Blocked unregistered tool %s", tool_name)
        return {
            "status": "blocked_by_security_guardrail",
            "reason": "TOOL_NOT_ALLOWED",
            "error": f"Tool '{tool_name}' is not permitted for this agent.",
        }

    # 2. Validate project_id / case_name / reference_case_name against ALLOWED_PROJECTS
    project_arg = args.get("project_id")
    if project_arg:
        try:
            config.validate_project_access(str(project_arg))
        except (PermissionError, ValueError) as exc:
            logger.warning("Blocked unauthorized project_id in tool %s: %s", tool_name, exc)
            return {
                "status": "blocked_by_security_guardrail",
                "reason": "PROJECT_NOT_ALLOWED",
                "error": str(exc),
            }

    for case_key in ("case_name", "reference_case_name"):
        case_name_arg = args.get(case_key)
        if case_name_arg:
            try:
                config.validate_project_access(str(case_name_arg))
            except (PermissionError, ValueError) as exc:
                logger.warning("Blocked unauthorized %s in tool %s: %s", case_key, tool_name, exc)
                return {
                    "status": "blocked_by_security_guardrail",
                    "reason": "PROJECT_NOT_ALLOWED",
                    "error": str(exc),
                }

    return None


root_agent = Agent(
    name="cloud_case_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=2),
    ),
    instruction=AGENT_INSTRUCTION,
    tools=ALL_TOOLS,
    before_tool_callback=guard_tool_execution,
)

app = App(
    root_agent=root_agent,
    name="app",
)


def _invoke_guarded_tool(
    tool_fn: Any,
    args: dict[str, Any],
    tool_calls_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute a tool function through guard_tool_execution and record its call trace."""
    guard_result = guard_tool_execution(tool_fn, args, tool_context=None)
    if guard_result is not None:
        tool_calls_log.append(
            {
                "tool": tool_fn.__name__,
                "args": args,
                "result": guard_result,
            }
        )
        return guard_result

    result = tool_fn(**args)
    tool_calls_log.append(
        {
            "tool": tool_fn.__name__,
            "args": args,
            "result": result,
        }
    )
    return result


def run_deterministic_agent_turn(
    message: str,
    active_project: str,
    selected_case_name: str | None = None,
) -> dict[str, Any]:
    """Deterministic ADK tool orchestrator used when live Vertex/Gemini credentials are unavailable
    or when deterministic local execution is requested.

    Executes the exact same ADK tools (`ALL_TOOLS`) and `guard_tool_execution` callback.
    """
    text = (message or "").strip()
    lower = text.lower()
    tool_calls: list[dict[str, Any]] = []
    pending_drafts: list[dict[str, Any]] = []

    # Detect project mention in user text
    target_project = active_project
    for allowed_proj in config.get_allowed_projects():
        if allowed_proj.lower() in lower:
            target_project = allowed_proj
            break
    # Also check if user explicitly mentioned an unauthorized project like gcp-unauthorized-99
    explicit_proj_match = re.search(r"\b(gcp-[a-z0-9\-]{4,28})\b", lower)
    if explicit_proj_match:
        target_project = explicit_proj_match.group(1)

    # Detect case resource name or case ID in user text
    full_case_match = re.search(
        r"(projects/[a-z][a-z0-9\-]{4,29}/cases/[a-zA-Z0-9\-_]+)", text
    )
    case_id_match = re.search(r"\b(8492\d{4}|\d{7,12})\b", text)
    target_case_name = selected_case_name or ""
    if full_case_match:
        target_case_name = full_case_match.group(1)
    elif case_id_match:
        cid = case_id_match.group(1)
        target_case_name = f"projects/{target_project}/cases/{cid}"

    # 1. Draft confirmation is never executed from chat text; only the card button can do it.
    draft_match = re.search(r"\b(draft-(?:case|esc|close)-[a-z0-9]+)\b", lower)
    if draft_match and any(
        k in lower for k in ("修改", "改成", "改为", "调整", "添加邮箱", "抄送", "revise", "update draft")
    ):
        d_id = draft_match.group(1)
        rev_sev = ""
        for sv in ("s1", "s2", "s3", "s4"):
            if sv in lower or sv.replace("s", "p") in lower:
                rev_sev = sv.upper()
                break
        found_emails = re.findall(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+", text)
        res = _invoke_guarded_tool(
            revise_case_draft,
            {
                "draft_id": d_id,
                "display_name": "",
                "description": "",
                "severity": rev_sev,
                "subscriber_emails": ",".join(found_emails),
                "time_zone": "UTC" if "utc" in lower else "",
            },
            tool_calls,
        )
        if res.get("status") == "draft_revised":
            revised = res["draft"]
            return {
                "reply": (
                    f"✏️ 已就地更新待确认草稿 `{d_id}`（当前级别: `{revised['payload'].get('severity')}`，"
                    f"订阅邮箱: {', '.join(revised['payload'].get('masked_subscriber_emails') or []) or '无'}）。"
                    "请在确认卡片核对后点击 **「确认提交」**。"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "revised_draft_ids": [d_id],
            }
        return {
            "reply": f"⚠️ 修改草稿失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    if draft_match and any(k in lower for k in ("确认", "confirm", "同意", "提交", "yes")):
        return {
            "reply": (
                f"🛡️ 草稿 `{draft_match.group(1)}` 需要您在确认卡片上点击确认按钮才会执行，"
                "聊天消息不能直接执行创建、升级或关闭操作。"
            ),
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 1b. Feishu case update notification subscribe / unsubscribe intent
    if any(k in lower for k in ("取消订阅", "退订", "unsubscribe")):
        if not target_case_name:
            return {
                "reply": "请提供需要取消订阅通知的工单号（例如 `projects/gcp-prod-core-01/cases/84920101`）。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        res = _invoke_guarded_tool(
            unsubscribe_case_updates,
            {"case_name": target_case_name},
            tool_calls,
        )
        if res.get("status") == "success":
            return {
                "reply": f"🔕 {res.get('message')}",
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "subscription_actions": [
                    {"action": "unsubscribe", "case_name": str(res.get("case_name") or target_case_name)}
                ],
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 取消订阅失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    if any(k in lower for k in ("订阅工单", "订阅通知", "开启通知", "订阅更新", "subscribe")):
        if not target_case_name:
            return {
                "reply": "请提供需要订阅飞书更新通知的工单号（例如 `projects/gcp-prod-core-01/cases/84920101`）。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        res = _invoke_guarded_tool(
            subscribe_case_updates,
            {"case_name": target_case_name},
            tool_calls,
        )
        if res.get("status") == "success":
            return {
                "reply": f"🔔 {res.get('message')}",
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "subscription_actions": [
                    {"action": "subscribe", "case_name": str(res.get("case_name") or target_case_name)}
                ],
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 订阅工单通知失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 1c. Reopen / Follow-up on a closed case intent -> HITL Follow-up Create Draft
    if target_case_name and any(
        k in lower for k in ("重开", "重新打开", "reopen", "follow-up", "followup", "关联续单", "问题复发")
    ):
        sev_override = ""
        for sv in ("s1", "s2", "s3", "s4"):
            if sv in lower:
                sev_override = sv.upper()
                break
        res = _invoke_guarded_tool(
            prepare_followup_case_draft,
            {
                "reference_case_name": target_case_name,
                "followup_description": text,
                "severity": sev_override,
            },
            tool_calls,
        )
        if res.get("status") == "pending_confirmation":
            draft = res["draft"]
            pending_drafts.append(draft)
            return {
                "reply": (
                    f"🔄 原工单 `{target_case_name}` 已关闭（Google Cloud Support 不支持直接重开已关闭工单），"
                    f"已为您自动继承原分类与订阅信息生成关联续单草稿 `{draft['draft_id']}`：\n\n"
                    f"- **标题**: {draft['payload']['display_name']}\n"
                    f"- **严重级别**: `{draft['payload']['severity']}`\n\n"
                    "请在下方确认卡片点击 **「确认提交」** 创建关联新工单。"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": pending_drafts,
            }
        return {
            "reply": f"⚠️ 无法生成关联续单草稿: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 2. Close case intent -> HITL Close Draft (never closes directly)
    if any(k in lower for k in ("关闭工单", "关闭", "关单", "close case", "close ")):
        if any(neg in lower for neg in ("不要关闭", "别关闭", "不关闭", "don't close", "do not close")):
            return {
                "reply": "好的，不会关闭该工单。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        if not target_case_name:
            return {
                "reply": "请提供或先在左侧选择需要关闭的工单号（例如 `projects/gcp-prod-core-01/cases/84920102`）。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        res = _invoke_guarded_tool(
            prepare_close_draft,
            {"case_name": target_case_name, "reason": text[:500]},
            tool_calls,
        )
        if res.get("status") == "pending_confirmation":
            draft = res["draft"]
            pending_drafts.append(draft)
            return {
                "reply": (
                    f"🛡️ 已生成关闭工单草稿 `{draft['draft_id']}`（目标工单 `{target_case_name}`）。"
                    "请在下方确认卡片点击 **「确认关闭工单」** 后才会正式调用 `cases.close`。"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": pending_drafts,
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 无法生成关闭草稿: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 2b. Update case severity / priority / subscriber CC emails intent
    found_emails = re.findall(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+", text)
    wants_case_update = target_case_name and (
        any(
            k in lower
            for k in (
                "严重级别",
                "优先级",
                "调高",
                "调低",
                "降级",
                "改为s",
                "改成s",
                "改为 s",
                "改成 s",
                "抄送",
                "订阅邮箱",
                "添加邮箱",
                "加到订阅",
            )
        )
        or (bool(found_emails) and any(k in lower for k in ("添加", "加上", "抄送", "cc", "订阅", "邮箱")))
    )
    if wants_case_update and not any(
        k in lower for k in ("新建工单", "创建工单", "提交工单", "提单", "开工单", "开个工单", "提个工单")
    ):
        upd_sev = ""
        for sv, pv in (("s1", "p1"), ("s2", "p2"), ("s3", "p3"), ("s4", "p4")):
            if re.search(rf"\b({sv}|{pv})\b", lower):
                upd_sev = sv.upper()
                break
        replace_mode = any(k in lower for k in ("替换为", "覆盖", "清空邮箱"))
        res = _invoke_guarded_tool(
            update_support_case,
            {
                "case_name": target_case_name,
                "severity": upd_sev,
                "subscriber_emails": ",".join(found_emails),
                "append_subscribers": not replace_mode,
            },
            tool_calls,
        )
        if res.get("status") == "success":
            return {
                "reply": f"✅ {res.get('message')}",
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "updated_case": res.get("case"),
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 更新工单失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 3. Escalate case intent -> HITL Escalation Draft
    if any(k in lower for k in ("升级工单", "升级", "escalate", "加急")):
        if not target_case_name:
            return {
                "reply": "请指定需要升级的工单号（或在左侧列表中点击目标工单），并说明升级原因与业务影响。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        reason = "BUSINESS_IMPACT"
        if "tech" in lower or "专家" in lower or "技术" in lower:
            reason = "TECHNICAL_EXPERTISE"
        elif "time" in lower or "超时" in lower or "太慢" in lower or "时效" in lower:
            reason = "RESOLUTION_TIME"
        justification = (
            text
            if len(text) >= 12
            else f"客户请求紧急升级工单 ({reason})：生产核心业务受影响，需高优技术支持介入。"
        )
        res = _invoke_guarded_tool(
            prepare_escalation_draft,
            {
                "case_name": target_case_name,
                "reason": reason,
                "justification": justification,
            },
            tool_calls,
        )
        if res.get("status") == "pending_confirmation":
            draft = res["draft"]
            pending_drafts.append(draft)
            return {
                "reply": (
                    f"🛡️ **已触发升级安全护栏 (HITL)**：工单升级前需您确认。\n\n"
                    f"已为您生成升级草稿 `{draft['draft_id']}`：\n"
                    f"- **目标工单**: `{target_case_name}`\n"
                    f"- **升级原因**: `{reason}`\n"
                    f"- **业务影响说明**: {justification}\n\n"
                    f"请在下方确认卡片点击 **「确认升级工单」** 以正式执行 `cases.escalate`。"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": pending_drafts,
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 生成升级草稿失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 4. Comment / Reply to Case intent (explicit reply keyword OR contextual reply on selected/quoted case)
    explicit_reply_kw = any(
        k in lower
        for k in (
            "评论",
            "回复",
            "补充",
            "comment",
            "追加",
            "催一下",
            "未解决",
            "没解决",
            "还是报错",
            "继续排查",
        )
    )
    is_contextual_quote_reply = (
        bool(selected_case_name)
        and not full_case_match
        and not case_id_match
        and not any(
            k in lower
            for k in (
                "列表",
                "所有工单",
                "查看工单",
                "详情",
                "detail",
                "新建工单",
                "创建工单",
                "提单",
                "提个",
                "提一个",
                "分类",
                "classification",
            )
        )
    )
    if explicit_reply_kw or is_contextual_quote_reply:
        if not target_case_name:
            return {
                "reply": "请先选择目标工单、直接引用回复工单卡片，或在消息中注明工单号后再发送回复内容。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        comment_body = re.sub(
            r"^(请帮我|帮我)?(向工单|给工单|在工单|对工单)?\s*(projects/[^\s]+|\d{7,12})?\s*(追加评论|添加评论|回复工程师|回复|评论|补充)[:：\s]*",
            "",
            text,
        ).strip()
        if not comment_body:
            comment_body = text
        res = _invoke_guarded_tool(
            add_case_comment,
            {"case_name": target_case_name, "body": comment_body},
            tool_calls,
        )
        if res.get("status") == "success":
            case_res = get_support_client().get_case(target_case_name)
            return {
                "reply": (
                    f"💬 已将您的回复同步至工单 `{target_case_name}`（当前状态: `{case_res['state']}`）：\n"
                    f"> {comment_body}"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "updated_case": case_res,
                "active_case_name": target_case_name,
            }
        err_str = str(res.get("error") or "")
        if "CLOSED" in err_str.upper():
            followup_res = _invoke_guarded_tool(
                prepare_followup_case_draft,
                {
                    "reference_case_name": target_case_name,
                    "followup_description": comment_body,
                    "severity": "",
                },
                tool_calls,
            )
            if followup_res.get("status") == "pending_confirmation":
                f_draft = followup_res["draft"]
                pending_drafts.append(f_draft)
                return {
                    "reply": (
                        f"ℹ️ 工单 `{target_case_name}` 当前已处于 **已关闭 (`CLOSED`)** 状态，无法直接追加评论。\n"
                        f"已自动为您继承原分类生成关联续单草稿 `{f_draft['draft_id']}`，点击下方确认卡片即可提交新跟进工单。"
                    ),
                    "tool_calls": tool_calls,
                    "pending_drafts": pending_drafts,
                }
        return {
            "reply": f"⚠️ 回复工单失败: {err_str}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 5. Create new case intent -> Classification Search + Prepare Case Draft (HITL)
    if any(
        k in lower
        for k in (
            "提单",
            "提交工单",
            "提一个工单",
            "提个工单",
            "开工单",
            "开个工单",
            "新建工单",
            "创建工单",
            "报障",
            "报错",
            "连接超时",
            "连不上",
            "create case",
            "open a case",
            "open case",
            "timeout",
            "502",
            "504",
            "429",
        )
    ):
        # Check if user only said "我要提单" without any symptom description
        if len(text) < 12 and not any(
            kw in lower for kw in ("sql", "gke", "vertex", "vm", "bigquery", "storage", "lb")
        ):
            return {
                "reply": (
                    "好的，我来协助您创建 Google Cloud Support 工单。为了帮您匹配准确的 `CaseClassification` 并生成确认卡片，请补充以下信息：\n"
                    "1. **目标 GCP Project ID**（当前默认: `"
                    + target_project
                    + "`）\n"
                    "2. **受影响的产品组件与具体故障现象/报错信息**（可直接发送报错截图或日志文件）\n"
                    "3. **期望严重级别**（`S1` 生产中断 / `S2` 严重受损 / `S3` 一般问题 / `S4` 咨询建议）"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }

        # Step 5a: Search classifications
        cls_res = _invoke_guarded_tool(
            search_case_classifications,
            {"query": text},
            tool_calls,
        )
        classifications = cls_res.get("classifications") or []
        best_cls = (
            classifications[0]
            if classifications
            else {
                "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62SBN",
                "display_name": "Compute Engine > Virtual Machines > Instance Connectivity",
            }
        )

        # Determine severity
        severity = "S3"
        if any(s in lower for s in ("s1", "p1", "生产中断", "完全不可用", "宕机")):
            severity = "S1"
        elif any(s in lower for s in ("s2", "p2", "严重", "高优", "生产")):
            severity = "S2"
        elif any(s in lower for s in ("s4", "p4", "咨询")):
            severity = "S4"

        sub_emails = ",".join(found_emails) if found_emails else config.get_default_subscriber_emails()
        tz = "UTC" if "utc" in lower else "Asia/Shanghai"
        title = text[:80] if len(text) <= 80 else text[:77] + "..."
        description = (
            text
            if len(text) >= 20
            else f"客户通过 Cloud Case Agent 报告问题：{text}（Project: {target_project}）"
        )

        draft_res = _invoke_guarded_tool(
            prepare_case_draft,
            {
                "project_id": target_project,
                "display_name": title,
                "description": description,
                "severity": severity,
                "classification_id": best_cls["id"],
                "classification_display_name": best_cls["display_name"],
                "subscriber_emails": sub_emails,
                "time_zone": tz,
            },
            tool_calls,
        )

        if draft_res.get("status") == "pending_confirmation":
            draft = draft_res["draft"]
            pending_drafts.append(draft)
            similar = draft.get("payload", {}).get("similar_open_cases") or []
            extra_tips = ""
            if similar:
                sim_str = "、".join(f"`#{s['case_id']}` ({s['display_name'][:30]})" for s in similar[:2])
                extra_tips += f"\n\n💡 **防重提醒**：检测到同项目下已有相似活跃工单 {sim_str}，若属同一故障可直接向原工单追加回复。"
            if severity == "S1" and not sub_emails:
                extra_tips += "\n\n⚡ **S1 紧急响应建议**：P1 工单支持 7×24 全天候跟进，建议补充值班订阅邮箱（如回复「把草稿邮箱加上 oncall@company.com」）。"
            return {
                "reply": (
                    f"🛡️ **已为您匹配最佳产品分类并生成工单预览草稿 (`{draft['draft_id']}`)**：\n\n"
                    f"- **目标项目**: `{draft['project_id']}`\n"
                    f"- **产品分类**: `{best_cls['display_name']}`\n"
                    f"- **严重级别**: `{severity}` (`test_case={draft['payload'].get('test_case')}`)\n"
                    f"- **工单标题**: {title}\n\n"
                    f"请在下方 **结构化确认卡片** 中核对，点击 **「确认提交工单」** 后系统才会正式调用 `cases.create`。{extra_tips}"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": pending_drafts,
            }
        return {
            "reply": f"⚠️ 无法生成工单草稿: {draft_res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 5b. Inspect Case Attachment Content (must run before generic case details)
    if target_case_name and any(
        k in lower for k in ("查看附件", "读取附件", "附件内容", "解析附件", "inspect attachment")
    ):
        file_match = re.search(r"([A-Za-z0-9._\-]+\.[A-Za-z0-9]{2,5})", text)
        res = _invoke_guarded_tool(
            inspect_case_attachment,
            {
                "case_name": target_case_name,
                "attachment_ref": file_match.group(1) if file_match else "",
            },
            tool_calls,
        )
        if res.get("status") == "success":
            att = res["attachment"]
            body_txt = att.get("content_preview") or att.get("note") or str(att.get("archive_entries") or "")
            return {
                "reply": (
                    f"📎 **工单附件 `{att.get('filename')}` 解析结果**（`{att.get('size_bytes')} bytes`）：\n\n"
                    f"```\n{str(body_txt)[:2500]}\n```"
                ),
                "tool_calls": tool_calls,
                "pending_drafts": [],
                "active_case_name": target_case_name,
            }
        return {
            "reply": f"⚠️ 读取附件失败: {res.get('error')}",
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 6. Case details query intent
    if full_case_match or case_id_match or any(k in lower for k in ("详情", "detail", "进展", "查看工单")):
        if target_case_name:
            res = _invoke_guarded_tool(
                get_support_case_details,
                {"case_name": target_case_name},
                tool_calls,
            )
            if res.get("status") == "success":
                c = res["case"]
                comments = c.get("comments") or []
                attachments = c.get("attachments") or []
                latest_cmt_block = ""
                if comments:
                    last_c = comments[-1]
                    role_tag = (
                        "Google Support 工程师"
                        if last_c.get("author_type") == "GOOGLE_SUPPORT"
                        else "客户记录"
                    )
                    latest_cmt_block = (
                        f"\n- **最新回复** ({role_tag} · `{last_c.get('create_time', '')}`):\n"
                        f"  > {str(last_c.get('body') or '')[:300]}"
                    )
                att_block = ""
                if attachments:
                    att_names = ", ".join(str(a.get("filename") or "") for a in attachments[:5])
                    att_block = f"\n- **附件列表**: {att_names}"
                return {
                    "reply": (
                        f"📋 **工单详情 `{c['name']}`**\n\n"
                        f"- **标题**: {c['display_name']}\n"
                        f"- **状态**: `{c['state']}` | **级别**: `{c['severity']}` | **已升级**: `{'是' if c['escalated'] else '否'}`\n"
                        f"- **分类**: {c['classification']['display_name']}\n"
                        f"- **订阅邮箱 (已脱敏)**: {', '.join(c['subscriber_emails']) or '无'}\n"
                        f"- **历史评论数**: {len(comments)} 条 | **诊断附件数**: {len(attachments)} 个"
                        f"{att_block}{latest_cmt_block}\n\n"
                        f"您可以直接回复补充信息、发送报错截图/日志附件、调整优先级或申请升级/关闭工单。"
                    ),
                    "tool_calls": tool_calls,
                    "pending_drafts": [],
                    "updated_case": c,
                    "active_case_name": c["name"],
                }
            return {
                "reply": f"⚠️ 查询工单详情失败: {res.get('error')}",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }

    # 7. Classification search intent
    if any(k in lower for k in ("分类", "classification")):
        res = _invoke_guarded_tool(
            search_case_classifications,
            {"query": text},
            tool_calls,
        )
        items = res.get("classifications", [])
        lines = [f"- **{item['display_name']}** (`{item['id'][:20]}...`)" for item in items[:5]]
        return {
            "reply": "🔍 匹配的 Google Cloud Support 产品分类如下：\n" + "\n".join(lines),
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    # 7b. SRE Ops Shift Handover / SLA Stale Radar
    if any(
        k in lower
        for k in ("交接班", "值班巡检", "巡检报告", "超时工单", "shift handover", "sla", "卡住的工单")
    ):
        scope_proj = target_project if explicit_proj_match else ""
        res = _invoke_guarded_tool(
            get_ops_shift_handover,
            {"project_id": scope_proj},
            tool_calls,
        )
        if res.get("status") == "success":
            h = res["handover"]
            hp = h.get("high_priority_or_escalated") or []
            ar = h.get("action_required") or []
            sp = h.get("solution_provided") or []
            st = h.get("stale_cases") or []
            lines = [
                f"📋 **云运维工单交接班巡检简报**（扫描项目: `{', '.join(h.get('projects_scanned') or [])}` | 活跃工单总数: **{h.get('total_open_cases', 0)}**）",
                f"- 🚨 **高优 (S1/S2) 或已升级**: **{len(hp)}** 单"
                + (" — " + ", ".join(f"`{x['case_id']}` ({x['severity']})" for x in hp[:5]) if hp else ""),
                f"- 🟠 **待我方补充反馈 (`ACTION_REQUIRED`)**: **{len(ar)}** 单"
                + (" — " + ", ".join(f"`{x['case_id']}` (已等待 {x['hours_since_update']}h)" for x in ar[:5]) if ar else ""),
                f"- 🟢 **待验证解决方案 (`SOLUTION_PROVIDED`)**: **{len(sp)}** 单"
                + (" — " + ", ".join(f"`{x['case_id']}`" for x in sp[:5]) if sp else ""),
                f"- ⏳ **SLA 静默超时预警**: **{len(st)}** 单",
            ]
            for item in st[:5]:
                lines.append(
                    f"  - **`{item['case_name']}`** [{item['severity']} | `{item['state']}`]：{item['recommended_action']}"
                )
            return {"reply": "\n".join(lines), "tool_calls": tool_calls, "pending_drafts": []}

    # 7d. Cross-project keyword search
    if any(k in lower for k in ("搜索工单", "查找工单", "全局工单", "所有项目工单")):
        q_clean = re.sub(r"(搜索工单|查找工单|全局工单|所有项目工单|[：:])", " ", text).strip()
        res = _invoke_guarded_tool(
            search_support_cases,
            {
                "query": q_clean,
                "project_id": target_project if explicit_proj_match else "",
                "state_filter": "ALL",
                "severity_filter": "",
            },
            tool_calls,
        )
        if res.get("status") == "success":
            found = res.get("cases") or []
            if not found:
                return {
                    "reply": f"🔍 未检索到匹配 `{q_clean}` 的工单。",
                    "tool_calls": tool_calls,
                    "pending_drafts": [],
                }
            rows_md = [
                f"- **`{c['name']}`** [{c['severity']} | `{c['state']}`] {c['display_name']}"
                for c in found[:8]
            ]
            return {
                "reply": f"🔍 跨项目共检索到 **{len(found)}** 个匹配工单：\n\n" + "\n".join(rows_md),
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }

    # 8. Default: List cases for target_project
    state_filter = "ALL"
    if "closed" in lower or "已关闭" in lower:
        state_filter = "CLOSED"
    elif "escalated" in lower or "已升级" in lower:
        state_filter = "ESCALATED"
    elif "open" in lower or "处理中" in lower or "未关闭" in lower:
        state_filter = "OPEN"

    res = _invoke_guarded_tool(
        list_support_cases,
        {"project_id": target_project, "state_filter": state_filter},
        tool_calls,
    )
    if res.get("status") == "success":
        cases = res.get("cases", [])
        if not cases:
            return {
                "reply": f"项目 `{target_project}` 下当前没有符合筛选条件 (`{state_filter}`) 的工单。",
                "tool_calls": tool_calls,
                "pending_drafts": [],
            }
        summary_lines = [
            f"- **`{c['case_id']}`** [{c['severity']} | `{c['state']}`] {c['display_name']}"
            for c in cases[:8]
        ]
        return {
            "reply": (
                f"📂 项目 **`{target_project}`** 下共有 **{len(cases)}** 个工单（筛选: `{state_filter}`）：\n\n"
                + "\n".join(summary_lines)
                + "\n\n您可以直接点击左侧工单查看详情，或告诉我：**「帮我提一个 Cloud SQL 连接超时的 S2 工单」**、**「查看工单 84920101 详情」**、**「升级工单 84920101」**。"
            ),
            "tool_calls": tool_calls,
            "pending_drafts": [],
        }

    return {
        "reply": f"⚠️ 请求被安全护栏拦截或执行失败: {res.get('error')}",
        "tool_calls": tool_calls,
        "pending_drafts": [],
    }
