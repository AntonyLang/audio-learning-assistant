# 个人音频学习助手 · Audio Learning Assistant

## 当前主入口：Windows 本地版

**双击 `start-local.cmd`**：启动本地服务、加载已准备的 Qwen3-TTS C 引擎，并打开页面。
本机项目上一级也提供了 `启动音频学习助手.cmd`。CPA 由使用者手动启动，应用连接
`http://127.0.0.1:8317/v1`，默认使用 `deepseek-v4.1-flash-1017`。

| 本地工作台 | 行为 |
| --- | --- |
| 左侧资料投递 Outbox | 一个条目可同时附带正文、多个 URL 和多个文件 |
| 解析节点 | PDF 文本 / 扫描 PDF、图片 OCR、TXT / Markdown / CSV / JSON / HTML、DOCX / PPTX |
| 研究 Agent | CPA 工具调用；多轮网页搜索、网页与 PDF 正文读取、Crossref 论文检索、长资料段落检索；保留来源与处理过程 |
| 右侧音频 | 生成进度、播放、讲稿与来源，以及在文件资源管理器定位成品 |
| 模型控制 | 页面显式启动 / 停止 TTS；停止时暂停语音生成，保留已完成片段，启动后继续 |
| 轻量运行 | 一个 Python 进程 + SQLite + 一个按需 TTS 容器；本地版不启动 PostgreSQL、Redis、Celery 或 Node 服务 |

页面：`http://127.0.0.1:18743/desktop/`。TTS 仅监听本机回环地址，由 Docker 限制为
1.5 核 / 3GiB，使用已通过听感确认的 Qwen3-TTS 0.6B CustomVoice、Serena、INT8。
图片与扫描 PDF 使用 Windows 已安装的 OCR 语言包；图示请附文字说明，OCR 不等于图表语义理解。
联网搜索无需额外密钥，目标网站需要登录或拒绝访问时会记录失败，Agent 可改查其他来源。

数据在 `_data/desktop/`：SQLite、原资料、解析内容、语音片段缓存，以及 `audio/` 下的 MP3、讲稿和来源。
文本和联网查询会发给所配置的 CPA / 搜索服务；勾选“联网补充与核实”可控制 Agent 的补充检索。
主动投递的 URL 仍会读取。`stop-local.cmd` 关闭本地服务及其 TTS 容器；关闭网页不会结束后台生成。

CPA 密钥读取本机 `CPA_API_KEY`，或现有的 `%USERPROFILE%/.pi/agent/models.json` 中的 CPA 配置；
不会复制到代码仓库。模型、密钥、数据库和生成音频均不提交 Git。

### 其他电脑首次准备

本地版运行需要 Python 3.12、Docker Desktop、FFmpeg、Windows 中英文 OCR，以及已构建的前端。
已有项目环境的当前笔记本直接双击启动即可。新电脑需先在 `backend` 建立 `.venv` 并安装项目依赖，
在 `apps/web` 安装依赖后运行 `npm run build`。随后准备已固定版本的 TTS：

```powershell
python scripts/download_tts_model.py --weights-source modelscope
python scripts/download_qwen_native_source.py
docker build -f apps/tts/Dockerfile.native -t audio-learning-qwen-native:ef339be5-cpu1 deploy/.private/verification/native-build
```

模型约 2.50GB。首次启动会加载并量化权重；页面可以先使用，模型状态会更新。
以下为原有服务端拓扑及其验证说明，本地版通过 `python -m app.desktop` 独立启动。

把个人阅读清单变成**有来源、时长受控**的中文短讲：投递文章 / 文本 / PDF，系统围绕一个核心问题组织讲解，仅在必要处做有预算的补充检索，生成一条实测不超过 300 秒的音频，并通过网页与私有 RSS 交付。

>A personal reading list → sourced, duration-bounded Chinese micro-lessons. Every claim in
>the audio points back to an immutable snapshot of the source, the final duration is
>measured with `ffprobe` instead of estimated from a character count, and the daily
>listening budget is enforced by a database transaction rather than by convention.
>
>Everything runs offline with deterministic local adapters: no API key is required to
>reproduce the tests, the demo, or the evaluation report.

```
投递 ──▶ 摄取/快照 ──▶ 切块 + 混合检索 ──▶ LangGraph 内容图 ──▶ 分段 TTS ──▶ ffprobe 验收 ──▶ 发布事务 ──▶ 网页 / 私有 RSS
                              │                    │
                              │                    ├─ 缺口补证（有预算）
                              └─ 不可变 source_version + 字符区间   └─ 证据包 + 核源（可定位、可回查）
```

## 这个项目解决什么

普通的"AI 生成播客"很容易在三个地方失真：

| 问题 | 本项目的处理 |
| --- | --- |
| 说了原文没有的话 | 每条关键陈述绑定 `source_version + chunk + 字符区间 + 原文引文`，写入前逐个重定位校验；校验失败直接拒收 |
| 说好 5 分钟，成品 12 分钟 | 以 `ffprobe` 实测毫秒数为准，向上取整；超过 300000 ms 不发布，只允许一次缩稿 |
| 重复生成、重复扣费、重复发布 | PG 任务行 + attempt 租约 + 阶段产物指纹；Celery 只传 id，业务状态以数据库为准 |

## 快速开始

### 方式一：Docker Compose（完整拓扑）

```bash
cp .env.example .env
docker compose up --build
# Web:  http://127.0.0.1:8080     API: http://127.0.0.1:8000/docs
# 演示账号：demo@local / demo12345
```

Compose 会启动 `db(pgvector)`、`redis`、`api`、`worker`、`dispatcher`、`scheduler`、`web`。
默认 `PROVIDER_MODE=fake`，不需要任何模型密钥即可跑通整条链路。

### 方式二：本地开发

```bash
# 1) 数据库（需要 pgvector 扩展）
docker run -d --name ala-pg -p 55432:5432 \
  -e POSTGRES_USER=ala -e POSTGRES_PASSWORD=ala -e POSTGRES_DB=ala \
  pgvector/pgvector:pg17

# 2) 后端
cd backend
python -m venv .venv && .\.venv\Scripts\activate      # Windows
# source .venv/bin/activate                            # macOS / Linux
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# 3) 前端
cd ../apps/web
npm install && npm run dev      # http://127.0.0.1:5173，代理到 8000
```

本地没有 Redis / worker 时，把 `.env` 里的 `INLINE_WORKER=true`，API 会在进程内执行已提交的任务。

### 一条命令看完整效果（离线）

```bash
python scripts/demo_offline.py
# [1/4] ingest → [2/4] generate → [3/4] publish
# 输出实测时长、预算用量、音频对象键与 SHA-256、RSS 入口
```

对**正在运行的部署**做一次完整冒烟（登录 → 投递 → 生成 → 发布 → 详情 → RSS → Range → 网页）：

```bash
python scripts/smoke_http.py                       # 默认 http://127.0.0.1:8000 与 :8080
python scripts/smoke_http.py http://127.0.0.1:8000 http://127.0.0.1:8080
```

### 测试与评测

```bash
cd backend && pytest -q                 # 51 个测试：单元 + 真实 PostgreSQL + 真实 ffmpeg
python evals/run_eval.py --ablations    # 生成 evals/report.md（8 个用例 + A/B/C 消融）
```

`pytest` 需要可用的 `pgvector` 数据库（默认 `postgresql+asyncpg://ala:ala@localhost:55432/ala_test`，
可用 `TEST_DATABASE_URL` 覆盖）以及 `ffmpeg`/`ffprobe` 在 PATH 中。

CI 定义在 [`deploy/github-actions-ci.yml`](deploy/github-actions-ci.yml)（同样的 ruff + pytest + 前端构建）。
它没有放在 `.github/workflows/` 下，是因为推送环境的 OAuth token 缺少 `workflow` 权限；
把它复制到 `.github/workflows/ci.yml` 并给 token 加上 `workflow` scope 即可启用。

## 实测结果（确定性离线适配器）

来自 `evals/report.md`，可用同一条命令复现：

| 指标 | 结果 | 说明 |
| --- | --- | --- |
| 时长合规 | 8/8 | 每条音频实测 ≤ 300000 ms |
| 引用可定位 | 8/8 | 所有 span 在保存版本中逐字重定位 |
| 证据召回 | 100% | 标注引用片段出现在证据包中 |
| 必讲点覆盖 | 81% | 标注术语出现在口播文本中 |
| 无依据陈述率 | 0% | 未通过核源的关键陈述为 0（门禁会阻断发布） |
| 补证用例 | 6/8 | 证据包中出现外部来源的用例 |

消融（开发分组 5 例）显示：A 基线 0 次外部补证，B/C 为 3 例、平均 0.6 次检索 / 1.4 个新增页面。
**这些数字只说明约束生效，不构成模型效果结论**；要比较"更好听/更有用"，需要人工评分与更大样本。

## 架构

| 层 | 选择 | 职责 |
| --- | --- | --- |
| API | Python 3.12 · FastAPI · Pydantic 2 | 会话、幂等键、SSE、RSS、媒体 Range |
| 内容编排 | LangGraph | `focus → gap_plan → search_fetch → evidence → draft → verify → revise` |
| 数据 | PostgreSQL 17 · pgvector · SQLAlchemy 2 · Alembic | 业务状态、全文 + 向量检索、行锁事务 |
| 后台任务 | Celery 5 · Redis · prefork | 只负责传递 `run_id`；数据库是队列的唯一真相 |
| 摄取 | HTTPX · Trafilatura · pypdf · feedparser | SSRF 防护、快照、页面/页码定位 |
| 音频 | 云 TTS 适配器 · FFmpeg · ffprobe | 分段合成、统一重采样、实测时长 |
| 前端 | React · TypeScript · Vite | 收件箱、今日收听、单集详情、播放器 |

更细的说明见 [docs/architecture.md](docs/architecture.md)，关键取舍见 [docs/adr](docs/adr)。

### 数据库约束（节选）

```sql
CREATE UNIQUE INDEX uq_run_request      ON generation_runs(owner_id, kind, request_key);
CREATE UNIQUE INDEX uq_generate_revision ON generation_runs(revision_id) WHERE kind = 'GENERATE';
CREATE UNIQUE INDEX uq_artifact_input   ON stage_artifacts(owner_id, kind, input_fingerprint);
CREATE UNIQUE INDEX uq_daily_plan       ON daily_plans(owner_id, local_date);
CREATE INDEX        ix_chunk_fts        ON chunks USING GIN(lexemes_tsv);
```

发布事务按 `plan → episode → run` 的顺序加锁，在事务内重新汇总当日预算；
预算超限时把该条标记为 `DEFERRED` 而不是让任务失败。

## API 速览

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| POST | `/v1/session` | 登录，返回 CSRF token 并设置 HttpOnly Cookie |
| POST | `/v1/sources` | 投递 URL / 文本 / RSS；支持 `Idempotency-Key` |
| POST | `/v1/sources/upload` | 上传 PDF（体积与页数受限） |
| POST | `/v1/episodes` | 从已就绪版本创建节目；先预留当日预算 |
| GET | `/v1/runs/{id}` | 运行快照：状态、阶段、预算、`last_seq` |
| GET | `/v1/runs/{id}/events` | SSE，支持 `Last-Event-ID` 断线重放 |
| GET | `/v1/today` | 当日清单与预算占用 |
| GET | `/v1/episodes/{id}` | 当前/已发布版本、claim、来源卡片、音频入口 |
| POST | `/v1/episodes/{id}/feedback` | 四种反馈 → 偏好更新或续篇候选 |
| POST | `/v1/feed-tokens` | 创建可撤销的私有订阅凭据 |
| GET | `/v1/feeds/{token}.xml` | 私有 RSS（稳定 GUID + 真实字节数） |
| GET/HEAD | `/v1/feeds/{token}/episodes/{id}/audio/{rev}.mp3` | 单段 Range、206/416 |
| GET/HEAD | `/v1/episodes/{id}/audio` | 网页播放器入口（会话鉴权，同样支持 Range） |

## 仓库结构

```
backend/app/api/         身份、投递、节目、运行、SSE、RSS、媒体、播放埋点
backend/app/graphs/      内容图（LangGraph）
backend/app/services/    摄取、检索、证据校验、音频、计划、发布、预算、事件
backend/app/adapters/    LLM / Embedding / Search / TTS / Storage（fake 与云端两套）
backend/app/jobs/        Celery、dispatcher、scheduler
backend/migrations/      Alembic 迁移
backend/tests/           34 单元 + 17 集成（真实 PG + ffmpeg）
apps/web/                React + TS + Vite（三页面 + 播放器）
evals/                   固定用例、离线 web 夹具、评测脚本与报告
scripts/demo_offline.py  一条命令跑通 摄取 → 生成 → 发布
docs/                    架构说明与 ADR
```

## VPS 部署准备

生产配置位于 `deploy/compose.yaml`。语音模型需通过同稿试听后再配置。
代码支持 HTTPS 安全 Cookie、正式账号初始化、
新标签页会话恢复、改密后撤销其他设备会话、跨设备续播和登录限速。

| 服务 | 公网端口 | 资源约束 |
| --- | --- | --- |
| Caddy HTTPS | TCP 80 / 443 | 128 MiB；自动续签证书 |
| 网页 / API | 无；网页仅绑定服务器回环 8091 | 非 root、只读根文件系统 |
| PostgreSQL / Redis | 无 | 独立内部网络 |
| 后台生成任务 | 无 | 单并发，语音服务按选型配置 |

```bash
# 1. 在本地构建 Linux 镜像，使用明确版本发布到有权限的镜像仓库。
docker build -t YOUR_REGISTRY/audio-api:VERSION -f backend/Dockerfile .
docker build -t YOUR_REGISTRY/audio-web:VERSION apps/web

# 2. 将 deploy 中的生产配置放入 /opt/apps/audio-learning-assistant/。
# 真实配置由 deploy/.env.example 填写。
# .env 必须包含实际镜像版本、强数据库密码、初始账号、模型密钥和 PUBLIC_HOST。
chmod 600 .env
docker compose -f compose.yaml config -q
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d --wait
```

生产环境设置 `SEED_DEMO_USER=false`，用 `BOOTSTRAP_EMAIL` 和至少 16 位的
`BOOTSTRAP_PASSWORD` 建立首次账号，创建后可清空这两个变量。现有账号不会被启动配置重置。
`DASHSCOPE_API_KEY` 可供百炼接口共用；独立语音服务可使用 `TTS_PROVIDER=openai`
及对应的 `TTS_BASE_URL`、`TTS_MODEL`、`TTS_VOICE`。
未配置 Tavily 时，仅根据投递材料生成，并记录未补齐的证据缺口。

Caddy 配置使用 Let's Encrypt `shortlived` 证书配置，可为公网 IP 签发证书。
IP 必须稳定，且云防火墙与 UFW 都需要允许 TCP 80 / 443。
Caddy 使用主机网络，公网流量由 UFW 处理；数据库、API 和语音服务不发布公网端口。
SSH 的云端来源限制、服务器 UFW 和浏览器备用登录通道需要一起验证，不能仅依赖一个会变化的出口 IP。

`deploy/backup.sh` 保存数据库、音频对象、配置和镜像信息到权限 0700 的备份目录。
升级前执行备份；镜像回退使用备份中的版本，数据库恢复另行处理，不自动回滚或删除数据。

CPU 语音评估代码保存在 `apps/tts/`、`scripts/benchmark_tts.py` 和
`deploy/compose.cpu-tts.yaml`，默认生产配置不启用该实验服务。

CPU 配置使用官方 `Qwen3-TTS-12Hz-0.6B-CustomVoice` 权重、Serena 音色，以及
[社区 C 引擎](https://github.com/gabriele-mastrapasqua/qwen3-tts) 的固定提交
`ef339be58a778b062e1c14382347964552eae007`。源码逐文件校验 Git blob，模型校验 SHA-256；
本地补丁只把 HTTP 日志中的朗读原文替换为字节数。官方 PyTorch 基线仍保留在 `apps/tts/Dockerfile`。

2026-10-25 本地 Docker 实测环境为 1.5 核、3GiB 内存、无额外 Swap、断网：

| 实现 | 文本长度 | 音频长度 | 请求耗时（不含模型加载） | cgroup 峰值内存 |
| --- | ---: | ---: | ---: | ---: |
| 官方 PyTorch BF16 | 12 字 | 2.48 秒 | 125.054 秒 | 2.08GiB |
| C 引擎 INT8 常驻 HTTP | 79 字 | 17.44 秒 | 47.136 秒 | 2.70GiB |

两次文本长度不同，结果用于评估各自的资源需求；本地听感已确认，VPS 性能和整集长时间运行尚未实测。
CPU overlay 使用 80 字分段、单并发、3GiB 内存上限、单段 15 分钟超时及整集 2 小时任务上限。

```bash
python scripts/download_tts_model.py --weights-source modelscope
python scripts/download_qwen_native_source.py
docker build -f apps/tts/Dockerfile.native -t YOUR_REGISTRY/audio-tts:VERSION deploy/.private/verification/native-build
```

构建时可添加 `--build-arg DEBIAN_MIRROR=https://mirrors.ustc.edu.cn` 使用国内 Debian 镜像，签名校验保持启用。
发布时将模型放入部署目录的 `models/qwen3-tts-0.6b-customvoice/`，在部署 `.env` 中设置
`TTS_IMAGE` 为实际镜像版本，并添加 `COMPOSE_FILE=compose.yaml:compose.cpu-tts.yaml`，再执行
`docker compose config -q`、`docker compose pull`、`docker compose up -d --wait`。

## 已知边界（验收状态）

- 离线适配器是**确定性基线**：它按证据逐句组织讲解，不做真正的改写或口语化；接上云端模型才是产品形态。
- 云端适配器（DashScope / Tavily）按公开接口实现，本仓库的测试与评测只覆盖离线模式；没有密钥时不参与验证。
- 评测集是 8 个可执行用例（文档规划为 40 个），扩展只需在 `evals/cases.json` 增加标注。
- RSS 刷新只在调度进程内做一次候选发现，未实现 ETag / Last-Modified 的条件请求。
- 单用户/少量邀请制；没有组织、配额与计费。
- 不做扫描 PDF OCR、实时语音打断、多 Agent 自主规划。

## 致谢与许可

设计参考了 AI Podcast Studio、Podcastfy、GPT Researcher、Open Notebook 与 Agili Hacker Podcast 的公开文档与流程（不复制其代码）。
代码以 [MIT](LICENSE) 发布。

更多背景与实现细节：[作品集博客](blog/audio-learning-assistant.md)。
