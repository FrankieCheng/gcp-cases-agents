# Cloud Case Management Agent（Google Cloud Support 智能工单与飞书协同平台）

基于 **Google ADK (`gemini-2.5-flash`) + FastAPI + 飞书长连接机器人 + Google Cloud Support API v2** 构建的企业级云运维工单全生命周期管理智能体。支持在飞书单聊、SRE War Room 群聊及 IAP 保护的 Web 工作台中完成**多模态识图提单、草稿在线修订、敏感密钥防泄漏脱敏、工程师回复实时卡片通知、引用卡片无单号直接回复、远端附件在线读取、跨项目全局搜索与值班交接 SLA 超时巡检**。

---

## 📚 核心中文文档导航

- 🚀 **[部署与运维配置指南 (`docs/DEPLOYMENT_GUIDE_ZH.md`)](./docs/DEPLOYMENT_GUIDE_ZH.md)**  
  涵盖架构拓扑、GCP IAM 与 Secret Manager 初始化、飞书开放平台应用权限与长连接配置、安全演练模式（`FORCE_TEST_CASE=true`）与正式接单模式（`FORCE_TEST_CASE=false`）一键部署切换、环境变量字典与故障排查速查。
- 📖 **[用户与云运维操作手册 (`docs/USER_MANUAL_ZH.md`)](./docs/USER_MANUAL_ZH.md)**  
  涵盖 HITL 人工卡片确认安全护栏、飞书直接传图/传诊断文件（`.yaml`/`.har`/`.csv`/`.log`/`.pdf`/`.zip`）、6 大回复工单场景、War Room 换班转交确认权、TSE 附件在线解析、早晚班交接巡检雷达及常用指令速查表。

---

## ✨ 核心特性一览

1. **严格 HITL（Human-in-the-Loop）安全护栏**：创建（`create`）、升级（`escalate`）、关闭（`close`）三大不可逆操作均由 AI 生成结构化飞书交互卡片，仅在人工点击卡片确认后执行；支持 War Room `@同事` 转交确认权与值班主管代批。
2. **飞书多模态识图与全格式运维附件直传**：原生解析飞书单图（`image`）、文件（`file`）与富文本粘贴截图（`post`），`gemini-2.5-flash` 自动提取报错详情并随工单上传至 Cloud Support API v2。
3. **双层防泄密防线**：硬性阻断误传 `.pem`/`.key`/`.tfstate`/`.env`/GCP Service Account 私钥 JSON，并自动对描述、评论与日志摘录中的 `Bearer Token`、API Key、AKSK 与数据库连接密码进行脱敏。
4. **双向实时同步与飞书推送闭环**：后台守护进程（默认 60s）自动检测 Google 工程师新回复、状态变更（`ACTION_REQUIRED` / `SOLUTION_PROVIDED` / `CLOSED`）、升级动态及新增附件，推送飞书彩色卡片并支持直接「引用回复」。
5. **云运维（Cloud SRE）值班利器**：内置产品级黄金排障要素体检、S1 全球接力（Follow-the-Sun）提醒、飞书内直接读取 TSE 附件、跨项目全局工单检索及早晚班交接 SLA 静默超时预警。

---

## 📂 项目目录结构

```text
cloud-case-manage/
├── app/
│   ├── agent.py                  # Google ADK 主智能体 (gemini-2.5-flash)、安全拦截器与离线兜底编排
│   ├── tools.py                  # ADK FunctionTools 工具集（严禁直接暴露建单/升级/关单执行函数）
│   ├── support_client.py         # MOCK / LIVE 双模 Cloud Support API v2 客户端与运维能力引擎
│   ├── feishu_bot.py             # 飞书 WebSocket 长连接机器人、群聊上下文路由、传图与交互卡片处理
│   ├── case_notifier.py          # 工单增量变更监控器与飞书通知卡片构建器
│   ├── security.py               # Session/CSRF 防护、文件魔数校验、高危密钥拦截与敏感凭据脱敏
│   ├── config.py                 # 多级配置、项目白名单校验与运行模式管理
│   ├── fast_api_app.py           # FastAPI BFF 网关与多模态对话入口
│   └── frontend/                 # Web 可视化工单工作台前端静态资源
├── deployment/
│   ├── deploy_prod.sh            # Cloud Run 生产/演练一键部署脚本
│   └── terraform/                # 基础设施即代码模板
├── docs/
│   ├── DEPLOYMENT_GUIDE_ZH.md    # 中文部署与运维配置指南
│   └── USER_MANUAL_ZH.md         # 中文用户与云运维操作手册
├── tests/
│   ├── test_customer_case_scenarios.py  # 客户提单、改草稿与 6 种回复场景端到端测试
│   ├── test_sre_ops_scenarios.py        # 云运维实战防泄密、TSE 附件解析、交接班巡检与换班协同测试
│   ├── test_live_sync_and_notifier.py   # LIVE API 同步、飞书通知与安全边界测试
│   ├── test_feishu_bot.py               # 飞书长连接消息路由与卡片权限单元测试
│   └── sim/feishu_simulation.py         # 真实 gemini-2.5-flash 飞书全链路仿真套件 (S1–S9)
├── Dockerfile                    # Cloud Run 生产容器构建定义
└── pyproject.toml                # Python 依赖声明
```

---

## ⚡ 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 运行全量自动化测试（80 项用例）
.venv/bin/pytest -v

# 3. 运行真实大模型飞书全场景仿真
GOOGLE_CLOUD_PROJECT=demo-core-prod GOOGLE_CLOUD_LOCATION=global GOOGLE_GENAI_USE_VERTEXAI=true \
  .venv/bin/python -u tests/sim/feishu_simulation.py

# 4. 按安全演练模式部署至 Cloud Run（不触发真人工程师接单）
FORCE_TEST_CASE=true CASE_AGENT_MODEL=gemini-2.5-flash bash deployment/deploy_prod.sh
```

详细配置步骤与使用教程请参阅 [部署指南](./docs/DEPLOYMENT_GUIDE_ZH.md) 与 [操作手册](./docs/USER_MANUAL_ZH.md)。
