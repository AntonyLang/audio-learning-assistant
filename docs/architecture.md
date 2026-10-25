# 架构说明

本文记录实现层面的结构与取舍。产品层面的范围与验收标准来自 `01_个人音频学习助手_详细架构方案.docx`，
本仓库是它的一次可运行复现；两者不一致的地方见文末「与原始方案的差异」。

## 1. 三条独立的状态线

系统里没有单一的 `status` 字段。同一个任务可能"执行成功但还没发布"，也可能"音频就绪但预算不足"，
把它们合并成一个状态，就无法在恢复时判断该重做什么。

| 对象 | 状态 | 恢复依据 |
| --- | --- | --- |
| `source_versions` | `RECEIVED / EXTRACTING / READY / PARTIAL / FAILED` | 源快照、提取器版本、完整性 `quality` |
| `generation_runs` | `DEFERRED / QUEUED / RUNNING / RETRY_WAIT / WAITING_REVIEW / SUCCEEDED / FAILED / CANCELLED` | attempt、租约、`next_attempt_at`、已提交产物 |
| `episode_revisions` | `BUILDING / SCRIPT_READY / AUDIO_READY / READY / BLOCKED` | evidence / script / audio 产物 ID |
| 发布 | 由 `episodes.published_revision_id + published_at + withdrawn_at` 推导 | 发布事务 |

`generation_runs.phase` 单独记录 `INGEST / RESEARCH / SCRIPT / TTS / AUDIO_CHECK / PUBLISH`，
它只用于展示和定位失败，不参与调度判断。

## 2. 请求到音频的完整路径

```
POST /v1/sources ──▶ sources + INGEST run（同一事务，202 立即返回）
      │
      ├─ dispatcher（每 5s）取 QUEUED ──▶ Celery(run_id) ──▶ worker
      │
      └─ ingest：抓取/解析 → 快照入对象存储 → source_version（不可变）
                 → 段落切块（字符区间）→ jieba 词元 → tsvector + pgvector
                 → index_state READY

POST /v1/episodes ──▶ episodes + episode_revisions + GENERATE run + plan_item 预留
      │                  预算不足：run=DEFERRED，不入队
      └─ 内容图（LangGraph）
            focus      主资料提纲 + 候选要点 → core_question / critical_points
            gap_plan   逐点检查覆盖；未覆盖的概念 → 至多 2 个 gap
            search_fetch 每次查询先占预算；外部页面也存成独立 source_version
            evidence   混合检索 + 邻段补齐 → 引文 span 校验 → EvidencePack 产物
            draft      LLM 结构化输出 → ScriptDoc（每条关键陈述带 claim_id）
            verify     结构检查 + 逐条核源 → 全部关键陈述 SUPPORTED 才通过
            revise     仅一次事实修订，之后 WAITING_REVIEW
      │
      └─ TTS：按 400 字分段 → 片段指纹缓存 → 统一重采样 → concat → MP3
              解码检查 → ffprobe 实测毫秒（向上取整）
              超 300000 ms：一次缩稿重跑；仍超 → WAITING_REVIEW
      │
      └─ 自动创建 PUBLISH run
            verify_audio（锁外）→ 发布事务（锁内）→ plan_item AVAILABLE
            预算不足 → DEFERRED，不重做研究/写稿/合成
```

## 3. 证据合同

```json
{
  "claim_id": "c03",
  "text": "调用超时并不能证明远端操作没有完成",
  "kind": "external_fact",
  "critical": true,
  "evidence": [{
    "source_version_id": "…",
    "chunk_id": "…",
    "span_start": 18,
    "span_end": 72,
    "quote": "调用超时并不能证明远端操作没有完成",
    "relation": "supports"
  }],
  "review": "SUPPORTED"
}
```

三层校验：

1. **结构**（Pydantic + 程序）：`body[span_start:span_end] == quote`，chunk 属于该版本，
   版本属于该 owner，span 落在 chunk 内。
2. **语义**（LLM 核源）：逐条返回 `SUPPORTED / PARTIAL / CONTRADICTED / NOT_FOUND`；
   任何关键陈述不是 `SUPPORTED` 都会阻断自动发布。
3. **人工抽样**：仓库不假装这一步已经完成——评测报告只声称约束生效，不声称事实错误率为零。

字符偏移统一以标准化文本的 Unicode code point 为单位，前端高亮使用同一单位。

## 4. 检索

两条通道，各自限 12 条，按 RRF 融合后取前 8，再补邻段、裁剪到证据预算：

```sql
-- 关键词通道（simple 配置，jieba 词元预写入）
SELECT id, ts_rank_cd(lexemes_tsv, to_tsquery('simple', :q)) AS rank
FROM chunks
WHERE owner_id = :owner AND source_version_id = ANY(:versions) AND lexemes_tsv @@ to_tsquery('simple', :q)
ORDER BY rank DESC LIMIT 12;

-- 语义通道（pgvector 精确扫描）
SELECT id FROM chunks
WHERE owner_id = :owner AND source_version_id = ANY(:versions) AND embedding IS NOT NULL
ORDER BY embedding <=> :vec LIMIT 12;
```

`rrf(chunk) = Σ 1 / (60 + rank)`。缺席某一通道的项对那一路贡献为 0，
这样融合的是排名而不是不同尺度的原始分数。

长问题先用 `query_terms()` 抽 6 个受控词再构造 `tsquery`：`plainto_tsquery` 会把所有词
用 AND 连接，直接喂整句会静默掉召回。

## 5. 后台执行与失败恢复

- **数据库是队列的真相**：dispatcher 只扫描到期的 `QUEUED/RETRY_WAIT` 行，用短事务打投递租约，
  提交后再发消息；发送期间不持有数据库锁。
- **attempt 领取**：`SELECT … FOR UPDATE` 检查状态、租约与 attempt 上限，写入 `active_attempt_id`
  并把 `attempt_no` 加一。数据库能容忍重复消息，所以不依赖 Celery task_id 去重。
- **租约**：每 15 秒续约 60 秒；每次阶段提交在同一个事务里校验 `active_attempt_id` 与租约未过期，
  条件不满足即 `LostAuthority`，旧 worker 停止后续写入。
- **重试边界**：只有读取、限流、临时存储错误可重试；证据不足、核源失败属于业务结果，
  重试整条任务只会重复花预算。退避 15/60/180 秒，最多 4 个 attempt。
- **预算跨 attempt 累计**：`generation_runs.budget_usage` 是 JSONB 计数（检索、页面、事实修订、
  音频缩稿、token、TTS 字符），只在行锁下自增，checkpoint 换新也不清零。

## 6. 发布事务

```
begin
  lock daily_plan → lock episode → lock generation_run（需要时）
  校验 owner 与 plan_version
  已撤回 → 拒绝；已发布 → 固定使用 published_revision_id
  校验 revision.audio_id 与锁外得到的 AudioVerification 的 sha256 一致
  要求 duration_ms ≤ 300000
  用实测时长替换预留，重新汇总 AVAILABLE + RESERVED ≤ budget_ms
  仅在为 NULL 时设置 published_revision_id
  plan_item 置 AVAILABLE，追加 durable event
commit
```

hash 与 ffprobe 都在锁外完成，事务内只读取已校验的元数据。预算跨行，因此需要计划锁 +
事务内汇总，单行 CHECK 表达不了。发布指针第一次设置后不再被重试替换；纠正事实要新建节目
并写 `supersedes_episode_id`。

## 7. SSE 与媒体

- `run_events(run_id, seq)` 的主键即游标；`event_seq` 在持有 run 行锁的事务里递增，
  与业务更新同事务提交。用全局自增会出现"序号大的先提交、客户端跳过前一条"。
- `GET /v1/runs/{id}/events` 先回放 `Last-Event-ID` 之后的事件，再每秒轮询同一日志，
  空闲时发心跳；终态且无新事件后发送 `stream.end`。
- 媒体路由只支持单段 Range：返回 206 / `Content-Range` / `Content-Length`，
  不可满足时 416 + `bytes */size`，多段 Range 忽略并返回完整 200。
  对象存储的签名 URL 不写进 RSS，媒体入口稳定，内部位置可以更换。

## 8. 测试策略

| 层 | 覆盖 |
| --- | --- |
| 单元（34） | 切块区间、RRF 数学、引文定位、时长取整、分段上限、RSS 转义、Range 解析 |
| 集成（8） | 完整链路产出真实 MP3、失败释放预留、attempt 围栏、预算拒绝、证据篡改拒收、发布延后、固定版本、缺口补证 |
| API（9） | 登录/CSRF、幂等键冲突、SSE 重放、今日预算、单集详情、RSS + Range(206/416/HEAD)、owner 隔离、反馈建续篇、PARTIAL 需确认 |

集成测试使用真实 PostgreSQL（pgvector）与真实 `ffmpeg/ffprobe`：音频不是 mock，
时长也不是伪造的。

## 9. 评测设计

- 8 个可执行用例（开发 5 / 冻结验证 3），按**原文**分组切分，避免同一篇文章的不同问题跨集合泄漏。
- 每个用例标注：核心问题、必讲术语、必须被引用的片段。
- 指标：时长合规、引用可定位、证据召回、必讲点覆盖、无依据陈述率、补证用例数、平均时长。
- 消融 A/B/C 只改变一个机制（是否补证、是否语义核源），避免一次改多个变量。

## 10. 与原始方案的差异

| 方案 | 本仓库 | 原因 |
| --- | --- | --- |
| `CREATE INDEX ix_chunk_fts ON chunks USING GIN(lexemes)` | GIN 建在生成列 `lexemes_tsv` 上 | 对 text 列建 GIN 需要额外操作符类；生成列 + GIN 是等价且更标准的做法 |
| LangGraph `PostgresSaver` + `thread_id` | 每次 attempt 重建图，从已提交产物续跑 | 业务围栏不会自动保护 stock saver；不让两个 attempt 竞争写同一 thread |
| 40 个评测用例 | 8 个可执行用例 + 标注格式 | 交付可运行子集，扩充无需改代码 |
| 云端 TTS 单次 600 字符、URL 24h 有效 | 适配器记录 URL 后立刻下载，片段上限 400 字 | 保留"已合成未下载可续传"的语义，同时不把临时 URL 写进产物 |
| Playback 事件（方案未列接口） | 增加 `/v1/playback-events` | 网页播放器需要可观察区间，RSS 下载不应被当成听完 |
