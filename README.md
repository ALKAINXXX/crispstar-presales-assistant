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

### Docker 本机运行

这是独立于上文本地 Python 环境的运行方式：使用 Docker Desktop 的 **Linux 容器模式**和 Docker Compose，不需要在电脑上安装项目的 Python 依赖或 Node.js。以下命令均在**仓库根目录（包含 `Dockerfile` 和 `compose.yaml` 的目录）的 PowerShell** 中执行；无需先创建 `.venv` 或 `.env`。

配置见 [Dockerfile](Dockerfile)、[.dockerignore](.dockerignore)、[compose.yaml](compose.yaml) 和 [requirements.docker.txt](requirements.docker.txt)。后者复用原依赖清单并固定 Linux 补充依赖 `uvloop==0.23.0`。应用保持单进程，使用仓库已有的前端构建和正式索引。

#### 1. 构建镜像，准备缓存并检查索引

正式版默认项目名为 `crispstar-formal`，镜像为 `crispstar-presales-assistant:docker-formal`，与旧练习资源分开。这一步不需要 DeepSeek API Key。

若连接 `auth.docker.io` 超时，先检查 Docker Desktop 的代理配置；需要代理的网络还可为当前终端设置 `HTTP_PROXY` 和 `HTTPS_PROXY`，地址使用本机代理软件的实际入口。代理地址不写入公开 Docker 配置，变量说明见 [Docker CLI 文档](https://docs.docker.com/reference/cli/docker/#environment-variables)。

```powershell
docker compose config --quiet
docker compose build app
docker compose run --rm --no-deps app python -X utf8 -m scripts.check_embedding --timeout-seconds 600 --output /tmp/embedding-sample.json
docker compose run --rm --no-deps app python -c "import tiktoken; encoding = tiktoken.get_encoding('cl100k_base'); assert encoding.encode('cache check'); print('cl100k_base ready')"
docker compose run --rm --no-deps app python -X utf8 -m scripts.knowledge_index check --offline
```

首次模型准备从网络下载前文所述的固定 `bge-small-zh-v1.5` 快照；分词准备会下载 `cl100k_base` 数据。两类缓存均写入 Docker 数据卷。Embedding 小样写入容器临时目录；索引命令只核验现有正式索引，不重建或覆盖。任一步失败，先处理该步，再继续启动。

随后使用同一个分词卷，在禁止联网的新容器中确认能够加载缓存；这条命令使用上述默认项目名对应的卷：

```powershell
docker run --rm --network none `
  --mount type=volume,source=crispstar-formal_tiktoken-cache,target=/app/data/tiktoken `
  crispstar-presales-assistant:docker-formal python -c "import tiktoken; encoding = tiktoken.get_encoding('cl100k_base'); assert encoding.encode('cache check'); print('cl100k_base offline ready')"
```

#### 2. 私下提供密钥并启动

在同一个仓库根目录的 PowerShell 窗口中，隐藏输入自己的 DeepSeek API Key。以下方式只设置当前终端会话的环境变量，不把真实值写入项目文件或命令文本。新开终端后，启动或重建容器前需要重新提供密钥。

```powershell
$deepSeekSecret = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $deepSeekSecret).Password
Remove-Variable deepSeekSecret
docker compose up -d --no-build
docker compose ps
docker compose logs --tail 50 app
```

默认模型为 `deepseek-flash`，工具输出模式为 `deepseek_strict_beta`；可在启动前用同名环境变量覆盖。仅有缓存不代表聊天可离线运行，真实聊天仍需有效密钥和 DeepSeek 网络连接。检查 Compose 配置时使用 `config --quiet`，避免把展开后的密钥输出到终端或聊天。

打开 [http://127.0.0.1:8799/](http://127.0.0.1:8799/)，健康接口为 [http://127.0.0.1:8799/health](http://127.0.0.1:8799/health)。端口仅绑定本机；若有冲突，可在启动前设置 `$env:CRISPSTAR_PORT = '另一个空闲端口号'`，并相应调整访问地址。健康检查只确认 HTTP 服务可响应；还需实际检查页面、少量真实问答、引用及一份已保存草案。

#### 3. 数据位置与保留数据重建

| Docker 数据卷（默认项目名） | 容器内目录 | 内容 |
| --- | --- | --- |
| `crispstar-formal_database` | `/app/data/database` | SQLite 聊天及已保存草案 |
| `crispstar-formal_embedding-models` | `/app/data/embedding_models` | 固定 Embedding 模型缓存 |
| `crispstar-formal_tiktoken-cache` | `/app/data/tiktoken` | `cl100k_base` 分词缓存 |

这些卷由 Docker 管理，不写回仓库源码目录；镜像已为运行用户准备可写目录。正式索引位于镜像内 `/app/data/vector_store/chengxing_index.json`，不挂载整个 `data` 目录。源码通过构建时复制进入镜像，修改后需要重新执行 `docker compose build app`，再执行 `docker compose up -d --no-build`。

在页面保存聊天和测试草案后，可在同一个已配置密钥的仓库根目录终端中移除并重建容器，再回查原聊天和草案：

```powershell
docker compose down
docker compose up -d --no-build
```

以上命令保留数据卷；保持相同项目名才能继续使用原卷。不要给 `down` 添加 `-v`，也不执行全局清理。当前任务和未提交草案保存在进程内，不属于重建后的持久化保证。

**本轮 Docker 验证（2026-10-21）：** 在 Windows 上通过 Docker Desktop 的 Linux/amd64 环境完成以下检查，容器内为 CPython 3.11.17：

- 镜像构建及 `pip check` 通过；应用以普通用户运行，三个数据卷均可写，正式索引与仓库文件的 SHA-256 一致。
- 固定模型下载与真实 Embedding 小样通过；断网加载模型并核验 6 篇资料、24 个检索小节的正式索引通过；`cl100k_base` 在独立断网容器中加载通过。
- 页面、静态资源和健康接口可访问。两条真实 DeepSeek 问答通过：SSO 前提回答的引用对应原文小节和行号；API＋Webhook、18 席、9 个月选出 Team，订阅基价为 12,960 元，并在页面保存一份测试草案。
- 保留数据卷并强制重建容器后，1 个聊天、4 条消息、1 份草案仍可查回，消息内容和草案快照摘要一致；页面能够重新读取草案。内存中的当前业务需求未恢复，符合持久化边界。

以上仅覆盖本机小规模部署与上述样例，不代表生产部署、跨机器验证、性能提升或任意表达的模型完成率；原有本地验证记录见下节。

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

### Run locally with Docker

This is an alternative to the local Python setup above. Use Docker Desktop in **Linux container mode** with Docker Compose; no project Python dependencies or Node.js installation is required on the host. Run every command below in **PowerShell at the repository root, containing `Dockerfile` and `compose.yaml`**. Creating a host `.venv` or `.env` is not required.

See [Dockerfile](Dockerfile), [.dockerignore](.dockerignore), [compose.yaml](compose.yaml), and [requirements.docker.txt](requirements.docker.txt). The last file reuses the original dependency list and pins the additional Linux dependency `uvloop==0.23.0`. The application uses one server process, the included frontend build, and the formal index.

#### 1. Build, prepare caches, and check the index

The defaults are project `crispstar-formal` and image `crispstar-presales-assistant:docker-formal`, separate from earlier practice resources. No DeepSeek API key is needed for this step.

If connecting to `auth.docker.io` times out, check Docker Desktop proxy settings. Networks requiring a proxy can also set `HTTP_PROXY` and `HTTPS_PROXY` for the current terminal, using the actual local proxy endpoint. No machine-specific proxy address is included in the public Docker configuration; see the [Docker CLI documentation](https://docs.docker.com/reference/cli/docker/#environment-variables).

```powershell
docker compose config --quiet
docker compose build app
docker compose run --rm --no-deps app python -X utf8 -m scripts.check_embedding --timeout-seconds 600 --output /tmp/embedding-sample.json
docker compose run --rm --no-deps app python -c "import tiktoken; encoding = tiktoken.get_encoding('cl100k_base'); assert encoding.encode('cache check'); print('cl100k_base ready')"
docker compose run --rm --no-deps app python -X utf8 -m scripts.knowledge_index check --offline
```

First-time preparation downloads the pinned `bge-small-zh-v1.5` snapshot described above and the `cl100k_base` tokenizer data into Docker volumes. The embedding sample goes to a temporary container directory. The index command checks the existing formal index without rebuilding or replacing it. Resolve any failed step before starting the application.

Then confirm tokenizer loading in a new container with networking disabled. This command uses the volume belonging to the default project name above:

```powershell
docker run --rm --network none `
  --mount type=volume,source=crispstar-formal_tiktoken-cache,target=/app/data/tiktoken `
  crispstar-presales-assistant:docker-formal python -c "import tiktoken; encoding = tiktoken.get_encoding('cl100k_base'); assert encoding.encode('cache check'); print('cl100k_base offline ready')"
```

#### 2. Supply your key privately and start

In the same repository-root PowerShell window, enter your own DeepSeek API key using hidden input. This sets an environment variable for the current terminal session without writing the real value into project files or command text. Supply the key again before starting or recreating containers from a new terminal.

```powershell
$deepSeekSecret = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $deepSeekSecret).Password
Remove-Variable deepSeekSecret
docker compose up -d --no-build
docker compose ps
docker compose logs --tail 50 app
```

The defaults are `deepseek-flash` and `deepseek_strict_beta`; override the corresponding environment variables before startup if needed. Cached embeddings do not make chat offline: real chat still needs a valid key and network access to DeepSeek. Use `config --quiet` for Compose validation to avoid printing the resolved key into terminal or chat output.

Open [http://127.0.0.1:8799/](http://127.0.0.1:8799/); the health endpoint is [http://127.0.0.1:8799/health](http://127.0.0.1:8799/health). The port binds only to localhost. For a conflict, set `$env:CRISPSTAR_PORT` to another available port before startup and adjust the URL. A passing health check establishes HTTP responsiveness only; also check the page, a few real questions, citations, and a saved test draft.

#### 3. Data locations and container recreation

| Docker volume with the default project name | Container directory | Contents |
| --- | --- | --- |
| `crispstar-formal_database` | `/app/data/database` | SQLite conversations and saved drafts |
| `crispstar-formal_embedding-models` | `/app/data/embedding_models` | Pinned embedding model cache |
| `crispstar-formal_tiktoken-cache` | `/app/data/tiktoken` | `cl100k_base` tokenizer cache |

Docker manages these volumes outside the source checkout. The image prepares writable directories for the application user. The formal index remains at `/app/data/vector_store/chengxing_index.json` inside the image; the whole `data` directory is not mounted. Source code is copied at build time. After source changes, run `docker compose build app` followed by `docker compose up -d --no-build`.

After saving a conversation and test draft in the UI, remove and recreate the container from the same repository-root terminal with the key configured, then retrieve the original records:

```powershell
docker compose down
docker compose up -d --no-build
```

These commands preserve volumes. Keep the same project name to reuse them. Do not add `-v` to `down` or run global cleanup. Current tasks and unfinished drafts live in memory and are outside this persistence guarantee.

**Docker verification on 2026-10-21:** The following checks passed in Docker Desktop's Linux/amd64 environment on Windows, using CPython 3.11.17 inside the container:

- Image building and `pip check` passed. The application runs as a non-root user; all three volumes are writable, and the formal index SHA-256 matches the repository file.
- The pinned model download and real embedding sample passed. With networking disabled, the model loaded and the formal index covering 6 documents and 24 retrieval sections passed validation. `cl100k_base` also loaded in a separate container with networking disabled.
- The page, static assets, and health endpoint were accessible. Two real DeepSeek questions passed: SSO prerequisites cited the original sections and line numbers; API plus Webhook requirements for 18 seats over 9 months selected Team at CNY 12,960, and one test draft was saved through the UI.
- After forcing container recreation while retaining volumes, 1 conversation, 4 messages, and 1 draft remained retrievable, with unchanged message-content and draft-snapshot digests. The UI retrieved the saved draft again. In-memory business requirements were not restored, matching the persistence boundary.

This covers a small local deployment and the stated examples only. It does not establish production readiness, cross-machine compatibility, performance improvements, or a completion rate for arbitrary model inputs. Earlier local verification is recorded below.

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
