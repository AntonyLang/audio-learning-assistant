# ADR 0001：PostgreSQL 是队列的唯一真相，Celery 只搬运 id

- 状态：已采纳
- 日期：2026-10-25

## 背景

音频生成包含多次外部调用（LLM、搜索、TTS），一次任务可能跑几分钟。用消息队列直接承载
"任务是否完成"会带来两个问题：

1. 消息被重复投递时，worker 无法从 broker 判断该任务是否已经在执行；
2. broker 丢失数据或可见性超时配置不一致时，"已提交但没执行"无法被发现。

Celery 的 `task_acks_late` 降低而不消除这些风险：普通失败仍会被确认，`visibility_timeout`
到期会重投，worker 子进程被杀由 `reject_on_worker_lost` 决定是否重投。

## 决策

- `generation_runs` 是唯一的任务记录。API 在同一事务里写业务行与 run 行，`202` 返回时任务已经持久化。
- dispatcher 每 5 秒扫描到期的 `QUEUED/RETRY_WAIT`，短事务写入投递租约，提交后再向 Celery 发送 `run_id`。
- worker 收到消息后先原子领取 attempt（行锁 + 租约 + attempt 上限），领取成功才执行；
  重复消息在领取阶段被拒绝，因此不需要 Celery 级别的全局去重。
- broker 丢消息时，`dispatched_at` 过期的行会被重新投递；worker 全部离线时，行仍然在那里。
- 业务状态、阶段、预算、事件全部读 PostgreSQL，不使用 `result_backend`。

## 后果

- 好处：重复、丢失、崩溃都能收敛到"某个 attempt 最终完成或被明确标记失败"。
- 代价：多一次数据库往返，dispatcher 需要常驻进程；`with_for_update(skip_locked=True)`
  必须在短事务里使用，否则会放大锁竞争。
- 不承诺"恰好一次"：外部调用可能被重复执行，由产物指纹与发布事务把重复副作用收敛掉。
