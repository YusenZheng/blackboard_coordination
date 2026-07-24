"""blackboard —— 【L1 地基层】共享黑板:事件流 + 派生视图 + 四账本 + 租约。

依赖:只 import contracts。不 import 任何上层。

可参考:
    - Mobius 投递状态机(谁该收/收没收/重试)—— subscribe 的可靠投递语义。
    - Oxygent Event Log(长期) + State(工作记忆)双层思想 —— 事件流 vs 派生视图两面。
    - DeerFlow 实测复现过 lost update("读→生成→写"长事务并发丢更新):
      append 做幂等键去重 + 版本校验,是地基钢筋。
    - MVP 用内存 dict;真上可换成熟消息中间件(Redis Stream/Kafka/NATS),
      创新留给上层协作协议,不自研存储引擎。
"""
