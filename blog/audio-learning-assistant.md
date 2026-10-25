# 让 AI 播客可以被核对：一个「有来源、有时长门禁」的个人音频学习助手

> 仓库：https://github.com/AntonyLang/audio-learning-assistant · 51 个测试 · 8 个评测用例 · 全链路离线可复现
> 关键词：FastAPI、LangGraph、PostgreSQL + pgvector、Celery、ffprobe、证据溯源、SSE、私有 RSS

## 一、问题不是"生成音频"，而是"生成一段可以被核对的讲解"

我最初的想法很朴素：把收藏的文章丢给大模型，让它写成一段口语稿，再调 TTS 合成音频，
通勤路上听。做完原型后，真正难的部分才露出来，而且都不是模型能力问题：

1. **它说了原文没有的话。** 模型会把"某个版本之后不再支持"讲成"这个功能被移除了"，
   把"在这份配置下有效"讲成"普遍有效"。听起来很顺，但你没法核对。
2. **它从来不是五分钟。** 脚本字数按 1200 字算，实测音频 4 分 12 秒；换一篇术语多的文章，
   同样字数变成 6 分 40 秒。字数只是估计，不是承诺。
3. **它会重复做同一件事。** 网络超时后重试，TTS 被调用两次；worker 被重启，
   同一个任务被两个进程推进；某天预算已经用完，任务还在继续烧调用额度。

所以我给自己换了一个目标：**不是"生成一段讲解"，而是"生成一段每条关键陈述都能回到原文、
且实测时长不超过 5 分钟的讲解"**。这三个约束一旦写进数据模型和事务里，整个系统的形状就变了。

## 二、三个决定性的设计

### 1. 证据不是"引用了一段话"，而是"可被机器重定位的区间"

很多 RAG 系统把 `chunk_id` 当作引用。这不够：chunk 是检索单位，不是引用单位；
原文更新后 chunk 会变；而且没有任何东西能证明模型引用的就是它说的那句。

我把证据定义成四元组，并强制校验：

```python
class EvidenceRef(BaseModel):
    source_version_id: uuid.UUID
    chunk_id: uuid.UUID
    span_start: int
    span_end: int
    quote: str          # 必须与 body[span_start:span_end] 逐字相等
```

校验是确定性的，不依赖模型：

```python
actual = body[item.span_start : item.span_end]
if actual != item.quote:
    problems.append(Problem("quote_mismatch", ...))
if not (chunk_start <= item.span_start and item.span_end <= chunk_end):
    problems.append(Problem("span_outside_chunk", ...))
```

配套的还有两个约束：`source_versions` 不可变（唯一键是 `source_id + content_hash + extractor_version`，
原网页改版也会生成新版本），以及 `chunks.text` 永远是 `body_text[start:end]` 的切片——
切块器如果破坏了这个不变量，测试会立刻失败。

结果是：引用可以被程序验证，历史节目在原网页改版后仍能回查当时依据。代价是存储放大、
以及"模型改写后引用"必须显式带上原文片段。我认为这个代价是值得的。

### 2. 时长用 `ffprobe` 量，超了只允许缩稿一次

```python
async def probe_duration_ms(path: Path) -> int:
    out = await run(["ffprobe", "-v", "error", "-show_entries", "format=duration", ...])
    return duration_to_ms(out)      # Decimal 解析 + ROUND_CEILING

def duration_to_ms(value: str) -> int:
    return int((Decimal(value) * 1000).to_integral_value(rounding=ROUND_CEILING))
```

超过 300000 ms 就不进 `READY`。缩稿也只有一次机会：用"当前字数 × 270000 / 实测毫秒"
估一个新目标，重新组织完整论证、重新核源、重新合成、重新检测。第二次仍然超长，
保留待处理稿并进入 `WAITING_REVIEW`——不截断尾部，也不用倍速凑数。

这条规则听起来很硬，但它把"五分钟"从提示词里的愿望变成了产品可验证的事实。
270 秒只是留裕量的起始参数，必须用真实语音校准。

### 3. 数据库是队列，Celery 只负责搬运 `run_id`

`task_acks_late` 能降低"消息确认了但任务没跑"的概率，但不能让执行变成恰好一次：
普通失败仍会被确认，`visibility_timeout` 到期会重投，worker 子进程被杀由
`reject_on_worker_lost` 决定。

所以我让 broker 只做传输，业务状态全部在 PG：

```python
async def claim_attempt(session, *, run_id, worker_id, settings):
    run = await session.get_for_update(GenerationRun, run_id)
    if run.state == RUNNING and lease_still_valid(run):
        return None                      # 另一个 executor 还活着
    if run.attempt_no >= settings.max_attempts:
        run.state = FAILED; return None
    attempt_no = run.attempt_no + 1
    run.active_attempt_id = insert_attempt(attempt_no)
    run.lease_expires_at = now() + 60s
    return ClaimedAttempt(...)
```

阶段提交时在同一个事务里校验 `active_attempt_id` 与租约未过期，失败即 `LostAuthority`，
旧 worker 停止后续写入。预算放在 `generation_runs.budget_usage`（JSONB），
只在行锁下自增，跨 attempt 累计——checkpoint 换新也不清零。

这三个设计互相咬合：证据合同让"说什么"可核对，时长门禁让"讲多久"可验收，
数据库队列让"做几次"可控。

## 三、内容图：只在需要语义判断的地方用 Agent

LangGraph 在这套系统里只负责内容判断，不负责调度、合成和发布：

```
focus → gap_plan → search_fetch → evidence → draft → verify → revise
                     ↑ 有预算的补证                 ↓ 不通过且还有次数
```

- `focus`：把资料压成有界提纲，产出一句核心问题与 2–3 个关键点；
- `gap_plan`：逐点判断"原文有没有讲"。这里有个真实的坑：如果用通用的词重合度判断覆盖，
  "任务""执行"这种词会让所有问题看起来都被回答了，补证永远不触发。
  我改成"至少两个词重合，且其中一个是低频词"，并在 focus 阶段显式标记
  文档未覆盖的概念；
- `search_fetch`：每次查询前先原子占用预算，失败请求也计数；外部页面同样存成不可变版本，
  搜索摘要永远不能直接进证据包；
- `verify`：结构检查 + 逐条核源，任何关键陈述不是 `SUPPORTED` 都会阻断自动发布。

节点只输出结构化结论、证据 ID 和一句决策理由，不展示模型思维链。

## 四、实测结果

`python evals/run_eval.py --ablations` 生成 `evals/report.md`。数字来自确定性离线适配器
（真实 PostgreSQL、真实 ffmpeg、真实 HTTP 层），因此可以复现：

| 指标 | 结果 |
| --- | --- |
| 用例数 | 8（开发 5 / 冻结验证 3，按原文分组） |
| 运行失败 | 0 |
| 时长合规 | 8/8，平均 91.6 秒 |
| 引用可定位 | 8/8，所有 span 逐字重定位 |
| 证据召回 | 100% |
| 必讲点覆盖 | 81% |
| 无依据陈述率 | 0% |
| 触发外部补证 | 6/8 |

消融 A/B/C 只改一个变量：A 不做补证、不做语义核源；B 允许补证但仍只做结构校验；C 是完整链路。
在 5 个开发用例上，A 没有任何外部补证，B/C 都是 3 例、平均 0.6 次检索 / 1.4 个新增页面。

这些数字**只说明约束生效**，不构成模型效果结论。必讲点覆盖 81% 就是明确的短板：
离线基线是"按证据逐句组织"，不做真正的口语化改写，接入云端模型后需要重新评。

## 五、实现过程中真正卡住我的地方

**1. 并发列表里的相对路径。** `ffmpeg` 的 concat demuxer 会把自己所在目录当作基准，
我写进去的是相对路径，于是路径被拼了两遍，报 `Impossible to open`。改成绝对路径。
这类错误不会被单元测试发现，只有真的跑一遍音频合成才会暴露。

**2. 事件循环与连接池。** pytest 默认每个测试一个事件循环，而 asyncpg 的连接绑定在创建它的
循环上，于是测试结束时出现 `Event loop is closed`。解决方案是让整个会话共用一个循环，
并在会话结束时在同一个循环里 dispose 引擎；Celery 任务里也同理，每个任务在自己的
`asyncio.run` 里创建并关闭资源。

**3. "覆盖"是最容易自欺的判断。** 第一版用词重合度判断关键点是否被资料覆盖，
结果所有问题都"有覆盖"，补证路径从未被触发。直到我把"文档未覆盖的概念"显式建模，
才看到真正的缺口。

**4. 失败的任务会把预算占着。** 一条生成失败后，当天的 300 秒预留一直挂在计划里，
第二天的计划看起来永远是满的。后来在 run 进入终态失败时显式释放预留，
并让"预算不足"走 `DEFERRED` 而不是 `FAILED`——这两种情况对用户的含义完全不同。

## 六、边界与下一步

已实现：URL / 文本 / PDF 摄取与快照、混合检索（jieba + tsvector + pgvector + RRF）、
有预算的内容图、证据核源、分段 TTS 与实测时长、attempt 围栏与故障恢复、每日预算与发布事务、
SSE 断线重放、私有 RSS 与媒体 Range、网页播放器与反馈闭环。

刻意没做：扫描 PDF OCR、实时语音打断、多 Agent 自主规划、Kubernetes、本地模型部署。

接下来优先级最高的三件事：

1. 接上云端模型跑同一套评测，把"离线基线"与"云端质量"分开记录；
2. 把 8 个用例扩到文档规划的 40 个，并按原文分组冻结验证集；
3. 给 `verify` 加人工抽样校准，量化"引用存在但扩大结论"这类错误。

## 七、复现

```bash
git clone https://github.com/AntonyLang/audio-learning-assistant && cd audio-learning-assistant
cp .env.example .env
docker compose up --build          # http://127.0.0.1:8080，demo@local / demo12345

# 或者不启动容器，直接看离线链路：
python scripts/demo_offline.py     # 摄取 → 生成 → 发布，输出实测时长与音频哈希
cd backend && pytest -q            # 51 个测试
python evals/run_eval.py --ablations
```

架构细节见 [`docs/architecture.md`](../docs/architecture.md)，关键取舍见
[`docs/adr`](../docs/adr)，评测口径见 [`evals/report.md`](../evals/report.md)。
