# Celery 的 visibility timeout 与任务上限

## 它解决什么问题

Redis 作为 broker 时，消息被 worker 取走之后并没有真正的"处理中"状态。visibility timeout 决定了一条被取走的消息在多久之后会重新可见。如果任务执行时间超过这个值，消息会被重新投递，另一个 worker 会再执行一次同样的任务。

所以 visibility timeout 必须大于任务的执行上限，并且与 task_time_limit 以及 broker 的传输选项保持一致。只把其中一处调大，重复执行仍然会发生。

## 与 ACK 策略的关系

acks_late 表示任务执行完成之后才确认消息。它降低"消息被确认但任务没跑"的概率，但不会让执行变成恰好一次。worker 子进程被杀时，reject_on_worker_lost 决定消息是否重新入队。

late ACK 不等于任何异常都会重投：普通失败默认仍然会被确认。业务重试需要由自己的状态机和调度器安排，而不是叠加无限 autoretry。

## 数据库作为队列的记录

把任务状态写在自己的数据库里，broker 只负责传递 id，是更稳的组合。消息丢失时可以从数据库补投，消息重复时可以用租约和 attempt 编号拦截。这样 Celery 的语义就不再决定业务正确性。
