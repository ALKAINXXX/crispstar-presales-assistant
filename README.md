# 澄星售前助手 · CrispStar Presales Assistant

[简体中文](#简体中文) · [English](#english)

## 简体中文

澄星售前助手是一个面向 B2B SaaS 售前咨询的个人 AI 项目。它围绕一轮客户沟通，把需求澄清、套餐选型、订阅计价和草案保存串联起来，也通过本地 RAG 为产品问答提供原文依据。

项目使用合成的团队协作产品“澄星协作”作为业务样例，界面和知识资料目前为中文。API、Webhook、SSO 等是样例产品的能力。

### 从咨询到草案

一轮咨询可以从这样的需求开始：

> 我们需要 API 和 Webhook，公司有 120 人，这次先购买 18 个付费席位，订阅 9 个月。

在样例目录中，同时满足这两项功能的最低档是 Team，订阅基价为 **80 × 18 × 9 = 12,960 元**。公司人数与付费席位分别处理；购买信息还不完整时，助手会继续询问。方案确认后，用户可以在页面保存草案，随后按编号或保存记录查回。

同一段沟通里也可以继续问产品细节、修改需求或调整订阅周期。资料问答会展示采用的原文、小节和行号；已保存的草案保留当时的需求、价格和依据，调整后可另存新草案。

### 实现思路

- **把模型理解接入业务流程。** 模型从客户表达中提出结构化候选，由同一模型配置的另一次调用独立核对，程序校验后更新任务。套餐匹配读取产品目录，订阅金额由 Python 服务计算。
- **让产品问答能够回查原文。** 6 篇 Markdown 资料按小节组成 24 个检索单元，使用本地 Embedding 和向量索引召回。引用与本轮检索、问题及原文位置关联，页面展示实际采用的依据。
- **把用户确认落到持久化结果。** 草案绑定当前任务和交互身份，经用户点击确认后保存为固定快照。聊天和已保存草案使用 SQLite 存储；未提交任务保留在进程内，重启后需要重新准备。

### 环境与依赖

已验证环境为 **Windows x64、CPython 3.11.9**。SQLite 使用 Python 标准库；聊天需要 DeepSeek API Key，本地检索使用 `bge-small-zh-v1.5`。

Python 直接依赖如下；[requirements.txt](requirements.txt) 同时固定直接依赖与间接依赖的版本：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| `fastapi` | `0.141.1` | Web 服务 |
| `uvicorn[standard]` | `0.54.0` | ASGI 服务器 |
| `pydantic` | `2.13.5` | 数据模型与校验 |
| `pydantic-settings` | `2.15.0` | 配置加载 |
| `python-dotenv` | `1.2.3` | 本地环境配置 |
| `tiktoken` | `0.14.0` | Token 计数 |
| `httpx` | `0.28.1` | HTTP 客户端 |
| `pytest` | `9.1.1` | 测试 |
| `openai` | `3.19.2` | DeepSeek 的 OpenAI 兼容 SDK |
| `fastembed` | `0.8.1` | 本地 Embedding |
| `onnxruntime` | `1.30.0` | CPU 模型推理 |
| `numpy` | `2.4.6` | 向量计算 |
| `huggingface-hub` | `1.33.0` | 模型缓存与下载 |
| `tokenizers` | `0.23.2` | 文本分词 |

前端使用 React / TypeScript 与 assistant-ui 组件，展示业务结果、资料引用和草案确认交互。

仓库已附前端构建，运行应用不需要 Node.js；修改前端时使用 Node.js 24.21.0、npm 11.19.0，完整依赖见 [frontend/package.json](frontend/package.json) 和 [frontend/package-lock.json](frontend/package-lock.json)。

### 快速开始

以下命令均在仓库根目录的 PowerShell 中执行。

#### 1. 安装依赖

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

已有可用的 `.venv` 时可直接使用。

#### 2. 配置模型

在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys)准备自己的 API Key。下列命令复制配置示例，并保留已有 `.env`：

```powershell
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

编辑 `.env`，将占位密钥替换为自己的值：

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TOOL_OUTPUT_MODE=deepseek_strict_beta
```

聊天调用远程 DeepSeek 接口，Embedding 在本地执行。真实密钥仅保存在本机 `.env`，修改配置后重新启动服务。

#### 3. 准备本地模型与索引

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_embedding
.\.venv\Scripts\python.exe -X utf8 -m scripts.knowledge_index check --offline
```

首次准备会下载 [Qdrant/bge-small-zh-v1.5 的固定模型快照](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59)。模型缓存不随仓库提交；正常检索使用本地缓存。仓库包含产品资料、清单和正式索引。

#### 4. 启动

```powershell
.\.venv\Scripts\python.exe -X utf8 run.py
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。入口显示实际数据库位置，首次启动自动初始化所需表；按 `Ctrl+C` 停止。

### 配置与数据

| 配置 | 默认值或用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 自己的 DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-flash`；主候选与独立核对共用 |
| `DEEPSEEK_TOOL_OUTPUT_MODE` | 示例配置使用 `deepseek_strict_beta`；另支持 `standard` |
| `DEEPSEEK_MAX_CONTEXT_TOKENS` | `90000` |
| `DEEPSEEK_DEFAULT_MAX_COMPLETION_TOKENS` | `2048` |
| `SAAS_DATABASE_URL` | `sqlite:///data/saas_assistant.db` |
| `EMBEDDING_CACHE_DIR` | `data/embedding_models` |
| `EMBEDDING_THREADS` | `2`，需与正式索引的表示配置一致 |
| `APP_ENV` | `development`，用于健康检查中的环境标识 |

客户端固定接入 DeepSeek：`deepseek_strict_beta` 使用 `https://api.deepseek.com/beta`，`standard` 使用 `https://api.deepseek.com`。更换模型或工具输出模式后，应重新验证工具调用与业务结果。

进程环境变量优先于 `.env`。旧 `GROQ_*` 模型配置名称仍可作为 DeepSeek 字段的兼容别名，同一来源内 `DEEPSEEK_*` 优先；终端里的旧变量也会优先于 `.env` 中的新名称。

可覆盖端口和数据库：

```powershell
.\.venv\Scripts\python.exe -X utf8 run.py --port 8797 --database data/local/saas.sqlite3
```

数据库选择优先级为命令行参数、环境变量、`.env`、应用默认值；相对路径以项目根目录为准。切换数据库不会迁移或合并旧记录。聊天和已保存草案保存在 SQLite 中，当前任务与未提交草案保存在进程内，重启后需要重新准备。

已有完整缓存时，可检查离线加载；主动更新产品资料后，可重新构建索引：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_embedding --offline
.\.venv\Scripts\python.exe -X utf8 -m scripts.knowledge_index build --offline
```

索引核对涵盖资料、模型文件、依赖版本与表示配置；构建命令通过独立检查后才会替换正式索引。

### 开发与验证

2026-10-18 的验证记录：**784 项 Python 定向检查通过，前端 141 项通过、2 项因外部响应夹具缺失跳过**；正常启动、页面及静态资源访问、本地检索和索引检查通过。

Python 回归使用临时数据库和受控模型，部分检查使用真实本地 Embedding。以下命令运行基础业务、检索及启动的快速检查子集：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_catalog.py tests/test_matching.py tests/test_pricing.py tests/test_runtime_isolation.py tests/test_conversation_store.py tests/test_knowledge_tool_availability.py tests/test_business_tools.py tests/test_run.py tests/test_ui_delivery.py
```

前端安装、构建和测试：

```powershell
cd frontend
npm ci
npm run build
npm test
cd ..
```

构建会更新后端使用的 `app/ui` 页面。开发时先启动后端，再在 `frontend` 目录执行 `npm run dev`；Vite 将接口请求代理到 `127.0.0.1:8000`。

以上检查不代表真实模型对任意表达的完成率。完整 Python 测试集中的部分超时与子进程退出用例曾在 Windows 下挂起，尚未完成全量无人值守验证。

### 目录与业务资料

```text
app/                       后端入口、业务编排、状态、模型接线与业务服务
  data/                    产品目录、知识原文和清单
  ui/                      正式前端构建
data/vector_store/         正式知识索引
docs/                      业务知识与设计依据
frontend/                  前端源码、依赖与构建配置
scripts/                   模型准备、索引与功能检查
tests/                     业务规则、状态、存储和接口契约检查
run.py                     正常启动入口
requirements.txt           Python 依赖与固定版本
```

[业务知识与设计依据](docs/业务知识与设计依据.md)保留完整产品目录、知识库全文及业务规则与代码的对照，供了解业务和设计选择。

`.env`、业务数据库、模型缓存、`.venv`、`node_modules` 和运行日志保留本地，不提交。

### 适用范围

当前版本使用合成资料，面向本地单用户、单进程场景，保存的是内部报价草案。金额仅包含订阅基价，不含完整税费、折扣、实施费或客户专属报价规则。SSO 前提依据用户明确自述，没有连接客户身份系统验证。

模型理解及工具参数输出可能失败，复杂咨询组合、否定、撤回和条件表达仍有限制。

### 来源与许可

项目基础参考 [personal-ai-assistant](https://github.com/sairam-s0/personal-ai-assistant)，相关基础代码保留原 [Apache-2.0 许可证](LICENSE)和文件中的来源、修改声明。

前端组件使用 [assistant-ui](https://github.com/assistant-ui/assistant-ui)，保留其 [MIT 许可证](frontend/vendor/assistant-ui-LICENSE)；组件来源说明见 [frontend/NOTICE.md](frontend/NOTICE.md)。正式前端构建保留[依赖许可与版权材料](app/ui/assets/third-party-licenses.txt)。

本地 Embedding 使用 [Qdrant/bge-small-zh-v1.5 固定快照](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59)，该快照声明 MIT 许可。模型文件在本地准备，不随仓库分发；Python 和 npm 依赖保留各自许可证。

---

## English

CrispStar is a personal AI project for B2B SaaS presales consultations. It follows a customer conversation from clarifying requirements to choosing a subscription plan, calculating costs, and saving a quote draft. Local RAG supports product answers with source passages from the knowledge base.

The project uses 澄星协作, a synthetic team collaboration product, as its business example. The application UI and knowledge base are currently in Chinese. API, Webhook, and SSO refer to capabilities of the sample product.

### From conversation to quote draft

The following is an English translation of a Chinese demo request:

> We need API and Webhook support. Our company has 120 people, but we are buying 18 paid seats for 9 months.

In the sample catalog, Team is the lowest plan that supports both features. Its base subscription cost is **CNY 80 × 18 × 9 = CNY 12,960**. Company headcount and paid seats are treated separately; missing purchase details lead to follow-up questions. Once the proposal is ready, the user can confirm and save it, then retrieve the draft by its ID or from the saved records.

The conversation can continue with product questions, requirement changes, or a different subscription period. Answers show the source passages, sections, and line numbers used. Saved drafts retain the requirements, prices, and evidence from that point in time; later revisions can be saved as new drafts.

### Design

- **Connect language understanding to a business workflow.** The model proposes structured updates from the customer's message. A separate call using the same model configuration reviews them, and the application validates and applies accepted changes. Plan matching reads the product catalog; Python services calculate subscription costs.
- **Keep product answers traceable to their sources.** Six Markdown documents form 24 section-based retrieval units. Local embeddings and a vector index retrieve passages; citation checks associate the selected text with the current retrieval, question, and source location.
- **Persist the result the user confirmed.** A draft is tied to the current task and interaction, then saved as a fixed snapshot after an explicit click. SQLite stores conversations and saved drafts. Unfinished tasks live in memory and need to be prepared again after a restart.

### Environment and dependencies

The verified environment is **Windows x64 with CPython 3.11.9**. SQLite comes from Python's standard library. Chat requires a DeepSeek API key; local retrieval uses `bge-small-zh-v1.5`.

Direct Python dependencies are listed below. [requirements.txt](requirements.txt) pins both direct and transitive dependencies:

| Dependency | Version | Purpose |
| --- | --- | --- |
| `fastapi` | `0.141.1` | Web service |
| `uvicorn[standard]` | `0.54.0` | ASGI server |
| `pydantic` | `2.13.5` | Data models and validation |
| `pydantic-settings` | `2.15.0` | Settings loading |
| `python-dotenv` | `1.2.3` | Local environment configuration |
| `tiktoken` | `0.14.0` | Token counting |
| `httpx` | `0.28.1` | HTTP client |
| `pytest` | `9.1.1` | Testing |
| `openai` | `3.19.2` | OpenAI-compatible SDK for DeepSeek |
| `fastembed` | `0.8.1` | Local embeddings |
| `onnxruntime` | `1.30.0` | CPU model inference |
| `numpy` | `2.4.6` | Vector operations |
| `huggingface-hub` | `1.33.0` | Model cache and download |
| `tokenizers` | `0.23.2` | Tokenization |

The frontend uses React / TypeScript and assistant-ui components to present business results, source citations, and draft confirmation.

A frontend build is included, so Node.js is not needed to run the application. For frontend development, use Node.js 24.21.0 and npm 11.19.0. Full dependencies are in [frontend/package.json](frontend/package.json) and [frontend/package-lock.json](frontend/package-lock.json).

### Quick start

Run the following commands in PowerShell from the repository root.

#### 1. Install dependencies

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

Reuse an existing working `.venv` if one is available.

#### 2. Configure the model

Create your own API key on the [DeepSeek platform](https://platform.deepseek.com/api_keys). This command copies the configuration example while preserving an existing `.env`:

```powershell
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

Edit `.env` and replace the placeholder with your own key:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TOOL_OUTPUT_MODE=deepseek_strict_beta
```

Chat calls the remote DeepSeek API; embeddings run locally. Keep real credentials in the local `.env` file and restart the service after configuration changes.

#### 3. Prepare the local model and index

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_embedding
.\.venv\Scripts\python.exe -X utf8 -m scripts.knowledge_index check --offline
```

First-time preparation downloads a [pinned Qdrant/bge-small-zh-v1.5 model snapshot](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59). Model caches are not included in the repository; normal retrieval uses the local cache. The repository contains the product documents, manifest, and formal index.

#### 4. Start the application

```powershell
.\.venv\Scripts\python.exe -X utf8 run.py
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/). The launcher prints the actual database path and initializes required tables on first startup. Press `Ctrl+C` to stop.

### Configuration and data

| Setting | Default or purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Your own DeepSeek API key |
| `DEEPSEEK_MODEL` | `deepseek-flash`; shared by candidate generation and review |
| `DEEPSEEK_TOOL_OUTPUT_MODE` | Example configuration uses `deepseek_strict_beta`; also supports `standard` |
| `DEEPSEEK_MAX_CONTEXT_TOKENS` | `90000` |
| `DEEPSEEK_DEFAULT_MAX_COMPLETION_TOKENS` | `2048` |
| `SAAS_DATABASE_URL` | `sqlite:///data/saas_assistant.db` |
| `EMBEDDING_CACHE_DIR` | `data/embedding_models` |
| `EMBEDDING_THREADS` | `2`; must match the formal index's representation settings |
| `APP_ENV` | `development`; environment label in health checks |

The client connects to fixed DeepSeek endpoints: `deepseek_strict_beta` uses `https://api.deepseek.com/beta`, and `standard` uses `https://api.deepseek.com`. Revalidate tool calls and business results after changing the model or output mode.

Process environment variables take precedence over `.env`. Legacy `GROQ_*` model settings remain accepted as aliases for the DeepSeek fields. Within the same source, `DEEPSEEK_*` names take precedence; an old terminal variable also overrides a new name in `.env`.

Override the port and database when needed:

```powershell
.\.venv\Scripts\python.exe -X utf8 run.py --port 8797 --database data/local/saas.sqlite3
```

Database selection follows this order: command-line argument, process environment, `.env`, then application default. Relative paths resolve from the project root. Switching databases does not migrate or merge existing records. SQLite stores conversations and saved drafts; current tasks and unfinished drafts live in memory and must be prepared again after a restart.

With a complete cache, check offline loading. After intentionally updating product data, rebuild the index:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_embedding --offline
.\.venv\Scripts\python.exe -X utf8 -m scripts.knowledge_index build --offline
```

Index validation checks the corpus, model files, dependency versions, and representation settings. A build replaces the formal index only after an independent check succeeds.

### Development and verification

Verification recorded on 2026-10-18: **784 targeted Python checks passed; 141 frontend tests passed and 2 were skipped because external response fixtures were unavailable**. Normal startup, page and asset delivery, local retrieval, and index checks passed.

Python regression checks use temporary databases and controlled models; some checks use real local embeddings. The following command runs a quick subset covering core business rules, retrieval, and startup:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_catalog.py tests/test_matching.py tests/test_pricing.py tests/test_runtime_isolation.py tests/test_conversation_store.py tests/test_knowledge_tool_availability.py tests/test_business_tools.py tests/test_run.py tests/test_ui_delivery.py
```

Install, build, and test the frontend:

```powershell
cd frontend
npm ci
npm run build
npm test
cd ..
```

Building updates the `app/ui` assets served by the backend. For development, start the backend first, then run `npm run dev` from `frontend`; Vite proxies API requests to `127.0.0.1:8000`.

These checks do not establish a completion rate for arbitrary requests to the live model. Some timeout and subprocess-exit cases in the complete Python suite have hung on Windows; an unattended run of the entire suite has not been fully verified.

### Repository and business documents

```text
app/                       Backend entry, workflow, state, model integration, and business services
  data/                    Product catalog, knowledge documents, and manifest
  ui/                      Frontend production build
data/vector_store/         Formal knowledge index
docs/                      Business knowledge and design rationale
frontend/                  Frontend source, dependencies, and build configuration
scripts/                   Model preparation, indexing, and functional checks
tests/                     Business rules, state, storage, and API contract checks
run.py                     Normal application launcher
requirements.txt           Python dependencies with pinned versions
```

[Business Knowledge and Design Rationale](docs/业务知识与设计依据.md) **(Chinese)** contains the full product catalog, knowledge documents, and mappings from business rules to code.

Keep `.env`, business databases, model caches, `.venv`, `node_modules`, and runtime logs local and out of version control.

### Scope

The current version uses synthetic data, runs locally for one user in one server process, and saves internal quote drafts. Figures cover base subscription costs and exclude complete tax, discount, implementation-fee, or customer-specific pricing rules. SSO prerequisites rely on explicit user statements; the application does not connect to a customer identity system to verify them.

Model interpretation and tool arguments can fail, with remaining limits around complex consultation combinations, negation, withdrawal, and conditional expressions.

### Sources and license

The project foundations reference [personal-ai-assistant](https://github.com/sairam-s0/personal-ai-assistant). The relevant foundation code retains its original [Apache-2.0 license](LICENSE), along with source attribution and modification notices in the files.

Frontend components use [assistant-ui](https://github.com/assistant-ui/assistant-ui), with its [MIT license](frontend/vendor/assistant-ui-LICENSE) retained. Component source details are recorded in [frontend/NOTICE.md](frontend/NOTICE.md). The production frontend build includes [dependency licenses and copyright notices](app/ui/assets/third-party-licenses.txt).

Local embeddings use a [pinned Qdrant/bge-small-zh-v1.5 snapshot](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59), which declares an MIT license. Model files are prepared locally and are not distributed with this repository. Python and npm dependencies retain their respective licenses.
