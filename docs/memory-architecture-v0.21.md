# v0.21.0 当前记忆工程图

2026-11-14 实施。运行检查以 [升级进度](memory-upgrade-progress.md) 为准；用法见 [升级说明](upgrade-v0.21.0.md)。

## 1. 从微信到回答

```mermaid
flowchart TD
  W[微信数据库与桌面 GUI] <--> B[Windows 微信桥]
  B -->|OneBot 消息| A[AstrBot 群聊插件]
  A <-->|身份目录与原始消息映射 action| B
  A --> I[wxid / 桥 ID / 群内别名解析]
  I --> RAW[SQLite 持久原文]
  RAW --> HOT[L2 最近500条热视图]
  A --> G[原有 @、窗口、本地筛选与合并批次]
  G -->|放行| SAVE[明确保存：事务写入与真实回执]
  G -->|回忆问题| FIND[回答前检索]
  G --> REQ[当前模型请求]
  HOT -->|通常最近200条 / 12000字| REQ
  A -->|L1 最近10条| REQ
  SAVE --> REQ
  FIND --> REQ
  REQ --> LLM[默认 LLM 与工具]
  LLM --> GUARD[保存回执与无依据否认检查]
  GUARD --> B
  B -->|实际发送状态| POST[记录回复与 L20 关联 / 续窗口]
  POST --> RAW
```

群聊窗口与是否记原文分离。明确保存发生在已经放行给机器人的对话中，不将旁观闲聊擅自当保存命令。

## 2. 可靠记录与派生索引

```mermaid
flowchart LR
  DB[服务端 memory_store.sqlite3]
  DB --> MSG[原始消息与实际回复]
  DB --> NOTE[明确保存记录 / 事件与版本]
  DB --> P[L3 人物事实与人物主体关联]
  DB --> T[L20 工具与资料使用]
  MSG --> J[按量或空闲触发的分块整理]
  J --> SUM[话题总结与来源编号]
  MSG --> DAY[按上海日期的每日索引]
  SUM --> DAY
  NOTE --> DAY
  NOTE --> JOB[持久索引待办 / 幂等重试]
  P --> JOB
  SUM --> JOB
  JOB --> KB[L4 AstrBot 知识库 / bge-m3]
  KB -.->|索引可失效或重建，原文独立保存| DB
```

原文、事项、画像、总结和 L20 均落盘。L2.5 是整理过程；不再靠队列超过 500 条触发。摘要失败不删除来源，索引失败保留待办。物理清理目前关闭。

## 3. 人物身份与查询

```mermaid
flowchart TD
  INPUT[用户问题中的名字 / 我 / 实际@或引用] --> RESOLVE[本群身份与别名解析]
  RESOLVE -->|唯一证据| PID[稳定人物身份]
  RESOLVE -->|多个候选| ASK[明确对象，不合并同名成员]
  PID --> FILTER[交代者或事项主体 + 日期范围]
  FILTER --> NOTES[明确记录与历史版本]
  FILTER --> PERSON[L3 人物事实]
  FILTER --> SOURCE[完整原文 / 日记 / 总结]
  INPUT --> TOPIC[关键词与语义补查]
  TOPIC --> L4[L4 候选]
  L4 --> CHECK[回查本地状态与来源，排除删除和旧版本]
  NOTES --> E[证据与查询状态]
  PERSON --> E
  SOURCE --> E
  CHECK --> E
  E --> ANSWER[根据来源回答；未命中不等于从未发生]
```

已用真实数据验证 Amy 与陈sir可以指向同一 wxid，另一个名字相近的成员保持独立。旧聊天身份仅在原始文本、时间与发送者证据不冲突时回填，无法确认的部分继续保留未解析状态。

## 4. L20 如何接在回复下方

```mermaid
flowchart TD
  CALL[模型工具调用 / 自动附图 / 直接桥动作] --> RESULT[实际结果：成功、失败、未命中或未确认]
  RESULT --> TRACE[L20：请求编号、工具名、对象、时间、简短结果]
  TRACE --> LINK[关联该请求实际发出的回复]
  LINK --> RENDER[L2 中的回复正文下面附简短 L20]
  RENDER --> MODEL[下一次模型读取历史]
  TRACE --> ORPHAN[没有最终回复的工具记录仍独立保留]
  ORPHAN -->|相关时按预算注入| MODEL
  MODEL --> DECIDE[已有结果足够则复用；新细节或实时数据可重查]
```

图片记录包含群内编号、已有文件名或稳定图像标识及真实返回状态。文件记录包含名称、内容版本、范围和截断状态。目录查询不记为已读取。L20 不向微信群额外发送，也不反复注入图片 base64 或完整工具结果。

## 5. 与旧文件及贝蒂的边界

旧 JSONL、L3 JSON 和原有 L4 在迁移期间保留。新系统生成的索引文档不会再次当作旧资料导入；清除和取消通过本地状态与任务版本阻止旧结果重新出现。

贝蒂插件本次未修改。它原有的转达成功写回 L1 行为没有升级为新库写入；这部分仍按用户要求留待后续单独处理。
