# support-agentic-search

Enterprise customer-support agent with **agentic search** — local knowledge base + web evidence, public process, MIXED disclosure, and human handoff.

企业级客服智能体平台：不是聊天玩具，而是**可核对的双源检索**。Agent 在白名单工具内规划调用，把本地政策与公开资料汇成建议，并标注来源；证据不足时可转人工。

## 它解决什么问题

客服答复需要同时满足：

- **有据可依**：本地知识库（FAQ / 政策）优先
- **可对外补证**：必要时开放网搜，并带上可核对链接
- **过程可见**：界面展示公开步骤，而不是原始 CoT / Prompt
- **来源可披露**：`knowledge_basis` 为 `LOCAL` / `WEB` / `MIXED`
- **边界可控**：工具调用硬上限；冲突或不足时 HITL 转人工

## 架构（五层栈）

```text
对话 UI (GET /)
    → 会话 API（鉴权 · 回合写入）
        → Agent 循环（规划 · 调工具 · 到顶即停）
            → 双源工具 local_retrieve / web_search
                → 持久层 Postgres · Redis · 检查点 · 审计
```

综合建议是 **循环收束阶段**，不是第三工具。工具白名单仅：

| 工具 | 作用 |
|------|------|
| `local_retrieve` | 本地库向量 + 关键词召回 |
| `web_search` | 开放互联网补证（带 URL） |

硬上限：工具调用 ≤ 8 · 网搜 ≤ 5。

## 业务主路径（单回合）

1. **提问** — 客服在官方入口提交问题  
2. **规划** — Agent 理解目标，决定调用哪些白名单工具  
3. **调工具** — 本地检索 / 网搜执行，写入公开步骤  
4. **公开过程** — UI 展示可读轨迹（非原始思维链）  
5. **建议** — 综合收束 + `knowledge_basis` 判定  

样本问题（与演示动画一致）：

> 密码重置要不要做 MFA？请对照本地政策与公开最佳实践。

## 数据如何流动

```text
问题
  ├─ local_retrieve → FAQ / 政策片段 → 本回合上下文
  └─ web_search     → 公开摘要 + URL → 本回合上下文
         ↓
   综合收束 → 建议草稿
         ↓
   knowledge_basis = LOCAL | WEB | MIXED
         ↓
   含网络结论时披露提醒；必要时 human_needed
```

## 技术栈

- **API / UI**：FastAPI · Jinja · 静态对话页  
- **编排**：LangGraph Agent 循环 + HITL interrupt  
- **检索**：Postgres + pgvector · 关键词 · RRF 融合  
- **任务**：Redis lease · 检查点 · Worker  
- **模型**：OpenAI-compatible（默认 DashScope：`qwen-plus` / `text-embedding-v3`）  
- **网搜**：可插拔（未配置时走 Fake / 空结果适配器）

Python 包名：`support-platform`（见 `pyproject.toml`）。

## 快速开始（本地）

### 要求

- Python **3.12**
- Docker（Postgres + Redis）
- LLM / Embedding API Key（见 `.env.example`）

### 1. 基础设施

```bash
docker compose up -d db redis
```

### 2. 环境变量

```bash
cp .env.example .env
# 填写 LLM_API_KEY / EMBEDDING_API_KEY
# 需要真实网搜时再配置 WEB_SEARCH_*（见 composition 中的适配器）
```

### 3. 安装与迁移

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head
```

### 4. 启动 API

```bash
python -m uvicorn support_platform.main:app --host 127.0.0.1 --port 8000 --ws none
```

- 官方对话入口：http://127.0.0.1:8000/  
- 工作台：http://127.0.0.1:8000/workbench  
- 探活：`GET /health/live` · 就绪：`GET /health/ready`

### 5. 一键 compose（含 web / worker）

```bash
docker compose up -d --build
```

生产向 compose 默认 `PHASE1_API_MODE=disabled`、`AUTH_BACKEND=postgres`；本地原型可参考 `.env.example` 的 `PHASE1_API_MODE=open`。

## 仓库结构（节选）

```text
src/support_platform/     # 应用代码
  api/                    # 会话 / 调查 API
  investigation/          # Agent 循环 · HITL · 图
  knowledge/ · search/    # 导入与双路检索
  task_runtime/           # Worker · 检查点 · SSE
  templates/ · static/    # 对话 UI
migrations/               # Alembic
fixtures/corpus/          # 示例语料
deploy/                   # 试点回退 Runbook
videos/support-agent-demo/# HyperFrames 项目说明动画（预览源）
tests/                    # pytest
```

## 演示动画

`videos/support-agent-demo/` 是面向 GitHub / 作品集的静音说明片源码（价值 → 架构 → 主路径 → 双源数据流 → 综合判定 → 治理边界）。

本地预览（需已安装 HyperFrames）：

```bash
cd videos/support-agent-demo
npx hyperframes preview
```

## 安全注意

- **不要提交** `.env`、真实 API Key、原始 CoT / Prompt  
- 公开过程仅暴露可读步骤与来源判定  
- 试点回退见 [`deploy/S17_RUNBOOK.md`](deploy/S17_RUNBOOK.md)

## 测试

```bash
pytest
```

## License

未单独声明前，默认保留所有权利；若需开源协议请再补充 `LICENSE`。
