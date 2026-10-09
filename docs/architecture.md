# 架构设计

> 以**当前代码实际行为**为准。最后同步：2026-10-07

---

## 1. 部署拓扑

```
┌──────────────────────── Windows（本机，非容器）────────────────────────┐
│                                                                        │
│   ┌──────────────────┐        ┌──────────────────────────────────┐     │
│   │ 微信客户端 4.1.12+│        │ bridge/（Python 3.11 venv）      │     │
│   │ （已登录、不锁屏） │◀──读────│  WeChatDB：本地数据库增量         │     │
│   │                  │───写───▶│  WeChatGUI：UIA 优先 + OCR 兜底   │     │
│   └──────────────────┘        └────────────┬─────────────────────┘     │
│                                             │ OneBot v11 反向 WS        │
└─────────────────────────────────────────────┼──────────────────────────┘
                                              │ ws://127.0.0.1:6199/ws
┌─────────────────────────────────────────────┼──────────────────────────┐
│ Docker                                      ▼                          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ AstrBot v4.28.2                                                  │  │
│  │  · aiocqhttp 适配器（0.0.0.0:6199）                               │  │
│  │  · 管线：WakingCheck → … → ProcessStage → ResultDecorate → Respond │  │
│  │  · 插件 astrbot_plugin_group_activation v0.3.0                    │  │
│  │  · 默认 LLM：DeepSeek deepseek-flash                              │  │
│  │  · WebUI http://localhost:6185                                    │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 组件清单

### 2.1 桥（`bridge/`，Windows 本机进程）

| 文件 | 职责 |
|---|---|
| `main.py` | 主循环：轮询微信数据库 → 组 OneBot 消息段 → 上报；响应 AstrBot 下发的 action |
| `wechat_client.py` | 封装 `wechatauto-replica`：读群消息、发消息、昵称/群名解析、**跳过自己发的消息** |
| `onebot11.py` | 极简 OneBot v11 反向 WS 客户端（握手头、心跳、上报、action 往返） |
| `paths.py` | 定位微信数据目录（兼容「文档」被 OneDrive 重定向） |
| `probe_db.py` / `introspect.py` / `probe_api.py` / `probe_msgs.py` | 自检与探测脚本 |
| `config.json` | 实际配置（`config.example.json` 为模板） |

**桥实现的 action**：`get_login_info`、`get_version_info`、`get_status`、`get_group_list`、
`can_send_image`、`can_send_record`、`send_group_msg`、`send_msg`。其余一律返回 `None`。

> 注意：AstrBot 的 aiocqhttp 适配器在解析 `at` 消息段时会调用 `get_group_member_info`。
> 本桥未实现该 action，因此 At 组件拿不到昵称（`name=""`）。**这不影响功能** —— 适配器会走
> `else` 分支补一个空名 At，`is_at_or_wake_command` 判定仍然成立。

### 2.2 插件（`plugins/astrbot_plugin_group_activation/` v0.8.1）

| 文件 | 职责 |
|---|---|
| `main.py` | 事件 handler（@ 开关 + L1 记录 + 放行默认 LLM）、`on_llm_request` 记忆注入、L3 异步提炼、调试命令 |
| `window.py` | 每群一份的滑动空闲窗口状态机 + L1 滚动缓冲 + 渲染 |
| `memory.py` | **L3 长期记忆**：按群隔离的结构化「群员画像」，JSON 持久化（含时间戳）、去重、渲染 |
| `config.py` | 配置默认值与 `config.json` 覆盖 |
| `metadata.yaml` | 插件元信息 |

**三大铁律**（违反会抑制或重复默认 LLM）：
> 不要 `event.send()`（会置 `_has_send_oper` 从而抑制默认 LLM）、
> 不要 `event.call_llm = True`、不要 `event.stop_event()`。

---

## 3. 收消息链路（微信 → LLM）

```
① 微信收到群消息，落库
② 桥轮询：get_new_messages(group_id, since_seq)   （poll_interval=0.8s）
     文本：过滤空文本 / XML 内容（以 < 开头）/ 自己发的消息
     非文本：图片 → 解密 .dat → base64；表情包 / 语音 / 链接卡片 → 占位文本
③ 桥把纯文本里的 @昵称 转成 OneBot at 段（build_segments，全局正则；
     别名 = 数据库实时读到的自己昵称 + config 里的历史别名，长别名优先）
④ 上报 message 事件 → WS → AstrBot aiocqhttp 适配器
⑤ AstrBot 管线 WakingCheck：
     · message_str 以 wake_prefix「/」开头 或 消息链里有 At(self) → is_at_or_wake_command=True
     · 遍历插件 handler 的 filter：任一通过 → is_wake=True（事件不被 stop）
⑥ ProcessStage → 执行本插件 on_group_message（见 §5）
⑦ 若 event.is_at_or_wake_command 且未发过消息且未置 call_llm → 调默认 LLM
     调 LLM 前触发本插件 on_llm_request → 注入 L1 背景
⑧ 生成结果 → ResultDecorate → Respond → 下发 send_group_msg action
```

**关键机制事实**：

1. **插件能收到所有群消息**（含未 @ 的）。原因是 `WakingCheckStage` 里只要某个 handler 的 filter
   通过就会置 `is_wake=True`；我们的 filter 是「群消息」，对所有群消息都通过。
2. **只有 `is_at_or_wake_command=True` 的消息才会调 LLM** —— 所以「窗口内也回话」的实现方式
   就是在插件里把它置为 `True`。
3. **AstrBot 内置的群聊上下文 / 主动回复功能全部关闭**（`provider_ltm_settings`），
   否则会与插件重复、并额外产生 LLM 往返。

### 3.1 「这条是谁发的」怎么判（有个必须知道的坑）

| 消息类型 | 库里 `content` 的形态 | 判定依据 |
|---|---|---|
| 文本（别人发的） | `wxid_xxx:\n正文` | 有 `wxid:` 前缀 → 别人 |
| 文本（自己发的） | `正文`（无前缀） | 无前缀 → 自己 |
| **图片** | **加密二进制，完全没前缀** | 用**从文本消息现学的** `sender_id → wxid` 映射 |

图片若沿用「无前缀 = 自己发的」这条规则，会被当成本机回声丢掉 ——
所以接入图片通道时必须同时改 `_normalize`。映射还没学到时保守地当作群友：
把群友的图丢掉是用户看得见的故障，而「把桥自己发的图当群友的」不会发生（桥从不发图）。

### 3.2 图片 / 表情包通道

- **图片**：桥用 `wechatauto.media.MediaDownloader.download_image()` 把 `FileStorage` 里的
  加密 `.dat` 解密成本地 jpg/png/gif（密钥由 `detect_image_key()` 本地派生，不扫进程内存）。
- **重试**：刚收到的图片微信可能还没写好本地副本 → 重试 3 次、间隔 1.5s。
- **为什么用 base64 而不是 `file://`**：AstrBot 跑在容器里，宿主机路径它读不到。
  实测 27KB 照片 → 36756 字符 base64 → 容器内解回 27567 字节，逐字节一致。
- **表情包（动画表情）已经能看了（2026-10-09 解锁）**：以前记 `[表情包]` 占位，现在**当图片处理**。
  链路（`WeChatClient.sticker_frame`）：
  1. 表情消息的 `message_content` 是 **zstd 帧**（头 `28 b5 2f fd`），解压后是
     `<msg><emoji … md5="…" cdnurl="http://…/stodownload?m=<md5>&filekey=…" …>`；
  2. 本机 `cache/<月>/Emoticon/<md5前2位>/<md5>` 那份是**加密**的，而且不是图片那套
     V1/V2 容器（密钥/ECB/CBC/CFB/OFB/各种偏移都试过，解不开）→ **不走它**；
  3. 直接用 XML 里的 `cdnurl`（带 filekey 的签名地址）下载 —— **下到的就是明文**
     `GIF/PNG`（实测 2.4MB 动图、100KB 静图各拿到）；
  4. **动图只取第一帧**（PIL `seek(0)`）→ 长边压到 1024 → 存 JPEG
     （`bridge/cache/emoji/<md5>.jpg`，同一个表情只下一次、只转一次，缓存留最近 300 个）。
  5. 取不到（无 cdnurl / 下载失败 / 无 PIL）才退化为 `[表情包]` 占位 —— 与以前兼容。
  开关：`wechat.fetch_stickers`（默认 true）。
  ⚠️ 开关**不能**在 `build_media_segments` 里直接读 `cfg` —— 它是 `main()` 的**局部**变量，
  模块级函数拿不到（2026-10-09 实测：每收一个表情包就抛
  `name 'cfg' is not defined`，外层 `except` 把**整条消息丢掉**，群里表现就是
  「它不认识表情包」）。现在走模块级 `MEDIA_OPTS` + `init_media_opts(cfg)`（启动时打印
  「媒体开关：发送图片=… 表情包当图=… 单图上限=… 字节」），并且取图整条包了 try/except ——
  **失败只退化为 `[表情包]` 占位，绝不丢消息**。
  测试教训：自测必须**不注入任何配置**地调用真实入口（上次手工塞了 `cfg`，恰好把
  NameError 遮住，白跑一轮）。
  ⚠️ 副作用：第一次遇到某个表情会多花约 1–3 秒（下载 + 转帧），之后走缓存。
- 图片 > `image_max_bytes`（默认 3MB）时退化为 `[图片]` 占位，避免 WS 大帧失败。
- 配置开关：`wechat.send_images`（默认 true）。

### 3.3 卡片 / 引用回复（同为 `type 49`，必须解析）

**引用回复也是 `type 49`**，而它的正文放在 appmsg 的 `<title>` 里。
整条当卡片丢掉的话，「@机器人 + 引用一张图问这是什么」会变成
**@ 丢失 → 窗口不开 → 机器人装死**（实测踩到过）。

- 解析后统一转成可读文本：`[文件] xxx.md`、`[聊天记录] …`、`[链接] …`、`[卡片]`。
- **引用回复的 `<title>` 要按文本再走一遍 `build_segments`**，`@` 才能变成 at 段。
- **被引用的原文要一起带出来（2026-10-08 修）**：从 `<refermsg><content>` 取原文，
  拼成 `（引用 小泽布尔 的消息：「太难了，一点一点拆开来」）这句话`。
  之前只带「谁被引用」，模型看不到被引的就是**只有半句话**（对方答「这句话」时，
  机器人只能回「我只看到『这句话』三个字，原话没捞上来」—— 如实描述，但很难看）。
  引用图片时该字段是 XML，跳过（图片另有 md5 通道）。
- **被引用的图片会一并解出来带上**（否则模型回答不了「这是什么图」）。
  定位方式：先按 md5 在 `packed_info` 里找；**不行再用 `<createtime>` 配 `create_time`（容差 3 秒）**。
  实测引用 XML 里的 md5 是**原始大图在 CDN 上的 md5**，与本地存储那份并不相同 —— 只靠 md5 会查不到；
  两者时间戳偶有 1 秒漂移，所以要容差。
  ⚠️ 查图片**只在 `refer_md5` 存在时才做** —— 引用文本也带 `createtime`，
  之前拿它去查图必然落空，白付一次查库 + 一行误导性日志。

---

## 4. 发消息链路（LLM → 微信）与耗时

```
① LLM 生成文本 → AstrBot 下发 send_group_msg（含 group_id 与文本）
② 桥 handle_action：clean_markdown() 去掉 Markdown 标记 → wx.send_text(群名, 文本)
③ WeChatGUI.send_msg(text, 群名, verify=False) 的三段选择：
     a) 快路径：who == gui._current_chat 且已有输入框缓存
        （UIA 路径不写 _last_input_box，故实测此路径不触发）
     b) UIA 路径（正常走这条）：
          uia.current_chat() == who ?  → 直接用
          : uia.open_chat(who)          → 需要切换
          然后 uia.send_text(text)：ensure_window → 定位输入框 → 剪贴板 Ctrl+V
                                   → rhythm.gate('send') → SendKeys Enter
     c) OCR 兜底（UIA 不可用时）：ensure_visible → 侧栏查找点击最多 3 轮
                                  → 最后才用搜索框
④ 桥回 ack，AstrBot 完成本轮
```

**实测耗时拆解**（详见 `latency-diagnosis.md`）：

| 环节 | 耗时 |
|---|---|
| DeepSeek 调用 | **1.1–1.4s** |
| AstrBot 管线 + L1 注入 | <0.3s |
| UIA 探测（`current_chat()` 等） | 0.3–0.8s |
| **逐字键入** | **0.04–0.08s/字**（回复越长越慢） |
| **`rhythm.gate('send')` 发送闸门** | **0.6–1.4s**（`fast` 档） |
| 会话切换（仅当不在目标群） | 1–3s |

> **「已在目标群时不做切换」由 `guia.send_msg` 的短路逻辑保证**：
> `if not who or uia.current_chat() == who or uia.open_chat(who)`。
> 实测 `current_chat()` 返回 `'测试群'`，与配置一致，故正常不切换。

### 4.1 出站 @（指名回复）

模型**自己决定**要不要在某条回复里 @ 某人 —— 用来表达「这条是回他的」。

- **为什么不让 AstrBot 直接发 at 段**：`At.toDict()` **只序列化 `qq`、不带 `name`**；
  而 OneBot 的 qq 到我们这儿是个 crc32 假 ID，换不出微信 @ 面板要的「界面显示名」。
  所以改成「模型在正文里写 `@昵称` → 桥识别并换成微信**真实 @**」——
  微信特有的知识全部留在桥里。
- 插件注入「本群出现过的人」名单（L3 群员 ∪ L1 里发过言的人，L3 优先）。
- **桥做二次校验**：名字必须命中本群真实成员（数据来自微信数据库）；
  模型瞎编的名字一律按普通文本发出去 —— 不冒险驱动界面去选一个不存在的人
  （那会白付一次 OCR + 点击）。
- 一次只 @ 一个人（`gui.at_member` 就是单成员）；**只有 @ 没有正文时退回纯文本**，
  不发空 @。
- **⚠️ 长回复曾被「合并转发」吞掉（真实事故，2026-10-08）**：主配置
  `platform_settings.forward_threshold`（默认 1500 字）以上的回复，AstrBot 的
  result_decorate 阶段（**只对 aiocqhttp 平台**）会把它包成 `Node`，发送时改调
  OneBot 的 **`send_group_forward_msg`** 动作；而桥当时既没实现这个动作、发送解析
  也只认 `text` 段 → 消息**静默消失**（模型生成完、AstrBot 也「Prepare to send」，
  群里什么都没有。KMP 长代码答案就是这么没的）。
  **现在的处理（两层）**：① `forward_threshold` 调到 999999 —— 实质关闭，
  长回复和短回复走**同一条普通消息**路径；② 桥实现 `send_group_forward_msg`
  （把 nodes 里的文本抽出来按普通长消息发）+ `_extract_text` **递归穿透
  `node`/`nodes`** + **永不静默丢弃**（转不出文字时 WARNING 并列出段类型，
  未实现的 `send_*` 动作也会告警）。
  另确认：`t2i=false`（文本转图片关）、`segmented_reply.enable=false`（分段关），
  所以长文本不会被转成图片或拆成多条。
- **@ 目标硬校正（2026-10-08，插件 `fix_mentions`）**：回复里的 @ 默认只允许指向
  **本条消息的发送者**（模型 @ 了别人 → 改成 @ 发送者；第 2 个及以后的 @ → 去掉）。
  **例外**：本条消息里**点名了**该目标（「atNULL」「艾特小泽布尔」）→ 放行 —— 这是
  对方明确要求 @ 谁，不是乱 @（第一版没留这个例外，把「atNULL」的 `@NULL` 改成了
  `@小泽布尔`，属于矫枉过正；短英文名用词边界匹配，避免「link」里命中「li」）。
  为什么需要硬校正：模型会自我模仿历史里自己的写法（连续多条「@同一个人」后，
  不管谁问都照抄，连「艾特我」都能 @ 错）。配套：`/gareset` 重置本群 L2 打断模仿。
- **@ 的位置 = 模型写的位置（2026-10-08 修复）**：`find_mention()` 返回 @ 的
  前/后两段，UIA 按「粘前段 → 插 @ 令牌 → 粘后段」的顺序输入。之前是「去掉 @
  粘全文、再把 @ 补到结尾」，模型写在句首的 @ 会被搬到消息末尾 —— 群里看到的
  「乱 @」就是这个（@ 糊在结尾、和最后的动作/表情连在一起）。提示词同时要求
  「要 @ 就写在开头，别堆在结尾」，并收紧了使用频率（默认不用，只有对方点名
  要求或短消息点名回复时才用）。
- 实现：`bridge/main.py: find_mention()` + `handle_action` 的发送分支；
  名单由 `wechat_client.member_names()` 提供（TTL 10 分钟，新入群的人过一会儿也能被 @）。
- **发送必须走 UIA 快路径，别用库里的 `gui.at_member`**：后者每次都重做
  「让窗口可见（还会**最小化其它窗口**）→ `open_chat` 重新打开会话 →
  全屏截图 + OCR 找成员」，实测一次 **16–42 秒**（对照：纯文本只要约 1.6 秒）。
  微信的 @ 弹层是**真实 UIA 控件**（`AutomationId=MentionPopover`），按名字精确点击即可。
  实现见 `wechat_client._uia_send_at()`；**失败不回退 OCR 路径**，而是按纯文本发出
  （没有真 @ 总比抢窗口/多花 16–42 秒好，日志会写明 `@=没成`）。
  - 坑：`uia.send_text()` 内部是 `_paste_into(..., clear=True)`，会**把刚插进去的 @ 令牌
    一起清掉**，所以快路径自己拼「粘贴（`clear=False`）+ 回车」，并保留
    `rhythm.gate('send')` 发送闸门。
  - **选人要点（2026-10-08 修复）**：只打 `@` 让弹层列出成员，**精确同名优先**，
    整屏扫完再退回包含匹配（之前「撞上谁点谁」会 @ 错人）；**首屏找不到就用
    `WheelDown` 滚轮往下翻**再扫（8 轮，滚到没有新名字为止）；仍找不到就
    **把名字括进输入框触发微信自带过滤**（`_filter_to_member`），失败时逐字退格复原。
  - **⚠️ 收尾事故（真实踩过）**：@ 选人失败时弹层还开着，直接回车会被弹层消费
    （等于「选中高亮的第一项」）—— 线上表现是「机器人总 @ 到第一个成员」且
    **那条消息根本没发出去**（停在输入框里等下次被 clear 冲掉）。现在失败一定
    先 `Esc` 关弹层，再**用完整正文重新干净粘贴**（`clear=True`；不做逐字退格
    修补 —— 要退几次是猜的，实测残留会把 `@名字` 糊在句尾发出去），最后回车：
    消息照发，只是没有真 @。

### 4.2 引用回复（结论：暂不做）

库里**有** `gui.quote_msg(text, who, target_text=...)`（右键目标气泡 → OCR 找「引用」菜单 → 输入 → 发送），
但有三个硬限制：

1. **不能定位历史消息** —— 库里没有「按 local_id / 时间戳滚动到某条消息」的能力，
   `quote_msg` 只在**当前可视区**做 OCR 子串匹配。所以只能引用屏幕上看得见的那条。
2. **不能和 @ 叠加** —— GUI 层 `quote_msg` 与 `at_member` 是两个独立流程，签名里都没有对方的参数。
   UIA 消息对象层支持叠加（`HumanMessage.quote(text, at=...)`），但那条路对我们的 **DB 消息**
   跑不通（`_DBMessageControl.RightClick` 直接 `raise NotImplementedError`）。
3. `target_text` 是**子串包含**匹配，正文有重复或截断时可能引用错人。

**当前结论：不做。** @ 已经满足「指明这条是回谁的」，而且会触发对方通知，比引用更强。
将来若要做，只做「引用最近一条」（`quote_msg` 省略 `target_text` 时自动引用最后一条），
这条路径是可靠的。

---

## 5. 插件内部行为

### 5.1 `on_group_message`（`EventMessageType.GROUP_MESSAGE`）

按顺序执行，**不对消息内容做任何特殊分支**：

```
1. 自己发的消息（sender_id == self_id）→ return
2. 以「/」开头（ignore_prefixes）→ return（交给 AstrBot 内置命令）
3. record_l1=True → 写入 L1（user / text / ts / at）
4. mentioned = event.is_at_or_wake_command
      ├─ True  → 若窗口未激活：检查并发上限（active_groups_max=3），然后 activate
      ├─ 窗口内 → touch（滑动续期）
      └─ 否则   → return（只当记忆，不出声）
5. 碎片合并（见 §5.6）：
     上一条还在等续句（now < _merge_until[key]）且本条"像碎片" → 只记 L1、**不放行**
     （append 进 _frag_pending，等那条 LLM 请求来收）
     否则若是「迟到碎片」→ 贴 ga_frag_note 并**跳过噪音过滤**放行
6. 无内容消息（图片 / 表情包 / 单个标点 / 单字，见 §5.7）：
     **从群友名字入手** —— 名字对得上（最近点过它的名 / 它刚在跟这人说话）→ 放行；
     对不上 → 只记 L1、不放行
7. event.is_at_or_wake_command = True   ← 放行默认 LLM
   event.set_extra("ga_mentioned", mentioned)
   未被 @ 时额外置 ga_decidable=True     ← 允许 LLM 用 [PASS] 表示「不参与」
   本条"像碎片" → 置 ga_frag_key 并开 _merge_until 窗口（请求侧先等 merge_seconds）
```

### 5.2 窗口状态机（`window.py`，每群一份）

```
SLEEP ──@机器人──▶ ACTIVE(expire_at)
  ▲                     │
  └──空闲超时 / 存活上限──┘
```

- `activate`：`expire_at = now + w0_seconds(30)`
- `touch`（窗口内每条消息，**含机器人回复完成**）：`expire_at = max(expire_at, now + idle_seconds(30))`
- `is_active` 检查两条终止线：空闲到期、`now - activated_at > window_max_seconds(120)`
- 超限即 `sleep()`：`status=SLEEP, expire_at=0`（**L1 不清空**，见 `context-design.md`）

### 5.3 `inject_memory`（`filter.on_llm_request`）—— 记忆 + 参与规则

```
若群窗口处于激活态：
    ① inject_l1            → req.system_prompt += L1 群聊滚动背景（去掉最后一条）
    ② l3_enable            → req.system_prompt += L3 **本群**群员画像（按群隔离）
    ③ 未被 @ 且开启参与决策 → req.system_prompt += 参与规则（含 [PASS] 标记说明）
```

### 5.3.1 L3 长期记忆：休眠后**异步**提炼

```
每次窗口活动 → 重挂一个延迟任务（并 cancel 掉旧的）
    ├─ 等待 idle_seconds + 2 秒
    ├─ 窗口还被续着？ → 返回，等下一轮
    └─ 窗口已休眠     → _l3_extract()

_l3_extract 的三道闸门：
    ① 窗口内新消息数 ≥ l3_min_new_messages
    ② 距上次提炼   ≥ l3_cooldown_seconds
    ③ L1 非空
→ 取 L1（最近 25 条）喂给 LLM → 解析 JSON → 合并进本群画像 → 原子写盘
```

**为什么必须用延迟任务、而不是监听 sleep 事件**：窗口是**懒判定**的（只在有消息进来时
才检查是否过期），群里没人说话就永远等不到那次检查。

**提炼跑在独立的 asyncio task 里，完全不占用回复延迟。**

存储：`data/plugin_data/astrbot_plugin_group_activation/long_term_memory.json`
（容器挂载点，重启不丢）。详见 `context-design.md` §5。

### 5.4 `decide_participation`（`filter.on_decorating_result`）—— LLM 有权保持沉默

**这是 v0.4.0 的核心机制**：让默认 LLM 自己决定「要不要说话」，且**不增加任何 LLM 往返**。

```
LLM 输出恰为 [PASS] 时：
    event.clear_result()
        → ResultDecorateStage 重读结果发现为空
        → RespondStage 见空 chain 直接 return
        → 消息不发送
```

- 只对 `ga_decidable=True`（**窗口内未被 @** 的消息）生效；**被 @ 的那条永远发送**。
- 判定很保守：只有输出**去掉首尾空白与标点后恰好等于标记**才拦截，避免误杀正常回复。
- 为什么不用「插件先调一次 LLM 判定」：那会给**每次接话**增加约 1.2 秒延迟。
- 标记与规则文案在插件 `config.json` 中可配（`pass_marker` / `participation_prompt`）。

### 5.5 调试命令

| 命令 | 作用 |
|---|---|
| `/gaw` | 查看当前激活中的窗口（剩余秒数、L1 条数） |
| `/gactx` | 查看本群将要注入给 LLM 的记忆内容 |
| `/gasleep` | 强制当前群休眠 |
| `/gaclear` | 清空本群 L1 记忆并休眠（用于丢掉群内临时人设；AstrBot 原生会话另发 `/reset`） |
| `/gamem` | 查看本群长期记忆（L3），含首见/最近时间与提及次数 |
| `/gaforget` | 清空本群长期记忆（L3） |

> 顶层 `wake_prefix = ["/"]`，所以在群里单发 `/gaw` 即可触发。

### 5.6 碎片合并：把「这是你」+「吗」当成**一句话**（v0.8.0+）

**问题**：人打字是碎片式的（「这是你」→「吗」），而插件对窗口内消息是**逐条放行**的，
于是每条碎片各自触发一次生成 → 用户看到两条互不相关、各自答半句的回复
（2026-10-09 实况：机器人先答「这是你」，再答「吗」）。

**做法（三层，按消息到达的时机分）**：

| 时机 | 机制 | 结果 |
|---|---|---|
| 上一条**还在等续句**（`now < _merge_until[key]`，默认 3 秒） | 碎片只记 L1、不放行，append 进 `_frag_pending`；那条 LLM 请求在 `on_llm_request` 里先 `await sleep(merge_seconds)` 收续句 | 合成一句话 → **一条**回复 |
| 上一条**已经在生成/已发出**（窗口已过） | 不吞，放行 → 命中 **AstrBot 自带的 follow-up 机制**：同群 + 同一个人 + 有活跃 `AgentRunner` 时，新消息被 `runner.follow_up(text)` 捕获，下一步注入 `[SYSTEM NOTICE] User sent follow-up messages…` | 模型**看到新消息并重新评估**（同一条回复里纳入） |
| 上一条**已发出**（就是一条新消息了） | 插件贴 `ga_frag_note`：明确告诉模型「这是在把同一句话分开打，结合 L1 里他上一条理解」 | 第二条也知道自己在接哪句，不再当成孤立新话 |

关键点是**只吞窗口内的**：`_merge_until` 一旦到期（那条请求已经 `await` 完、开始生成），
后续短句一律放行 —— 否则会出现「吞掉了但那一轮已经不在监听，永远没人答」。

**框架侧依据**（AstrBot 源码，容器内 `/AstrBot/astrbot/core/`）：

- `pipeline/process_stage/follow_up.py` → `try_capture_follow_up()`：要求同 `umo`、**同 sender**、
  该会话有活跃 runner；`runner.follow_up()` 产出 `FollowUpTicket`。
- `agent/runners/tool_loop_agent_runner.py` → `FOLLOW_UP_NOTICE_TEMPLATE`
  （写明「User sent follow-up messages while tool execution was in progress」）。
- 因此本机（回复走 `process_stage/method/agent_request.py` → `agent_sub_stages/internal.py`）
  **天然具备**「生成中插话 → 重新评估」的能力，插件只要**别把消息吞掉**即可。

**没做的更激进方案（留档）**：新消息到达时**掐掉未发出的旧回复、用最新上下文重新生成一条**。
框架留了口子 —— `astr_agent_run_util._watch_agent_stop_signal` 每 0.5s 检查
`event.is_stopped()` / `agent_stop_requested` 并 `request_stop()`；`result_decorate/stage.py`
也明确支持插件清空 `result.chain` 让消息不发送；`utils/active_event_registry.stop_all(umo)` 可终止
该会话活跃事件。**未采纳原因**：会丢回复（新消息若被 `[PASS]`，用户就什么都收不到）、
延迟翻倍、且要 import 框架内部模块（升级易碎）。需要时再说。

参数：`merge_enable` / `merge_seconds`(3) / `fragment_max_chars`(10) / `fragment_link_seconds`(20)，
见 §6.3。「像碎片」的判定：短（≤10 字）、**结尾无标点**、单行、非纯占位符（`_looks_fragment`）。

日志：`碎片并入上一条（…共 N 段）`、`合并碎片（群:发送者）：… → 合成文本`。

### 5.7 无内容消息：**从群友名字入手**（v0.9.0）

**问题**：别人发的**图片 / 表情包 / 单个标点**（`？`、`。。。`、`!!!`、纯 emoji）没有文字语义，
判不了它跟话题有没有关系。旧规则只拦「单字」（`_is_noise`，去标点后长度 ≤1），
所以 `。。。`、`？？？` 照样放行给 LLM —— 白花一次调用，换来的还常是一句多余的接话。

**做法**：`_is_contentless(label)` 统一识别无内容消息（去占位符 / 去标点 / 去 emoji 后为空），
然后**只看名字**决定回不回 ——

| 名字对得上的情况（任一 → 放行） | 依据 |
|---|---|
| 这条本身就点着它的名，或**引用的就是它** | 文本含昵称 / `（引用 <昵称> 的消息…）` |
| 这个人最近（`name_link_seconds`，默认 45s）**@ 过它**，或发言里**叫过它的昵称** | 翻 L1，只看同一发送者的行（`at` 标记 / 昵称出现在 text 里） |
| **机器人刚在跟这个人说话** | `_last_bot_talk[gid] = (昵称, 时间)`，在 `after_message_sent` 记（回复真正发出时才记） |

名字对不上 → 只记 L1、不放行（日志：`无内容消息、名字对不上…→ 只记 L1、不回`）。

为什么用名字而不是"话题相关性"：这类消息**没有语义**，让 LLM 判相关性等于又一次调用；
而"是不是发给它的"有确定性线索 —— 最近是谁在跟它说话。典型正确场景：
`@ 它 看这个` → 紧接着一张图（图本身没文字，但名字对得上 → 放行 → 模型能看到图并回答）。

机器人自己的昵称从桥的 OneBot **`get_login_info`** 取（桥返回 `wx.self_name`，即登录的微信昵称），
懒取 + 缓存 + 失败 60 秒后重试；`bot_names` 可补别名/错字。**取不到也不致命** ——
「这个人最近 @ 过它」靠 L1 的 `at` 标记，与昵称无关。

另外，`inject_memory` 里若 L1 背景包含无内容行，会附一句：
「上面 `[图片]`/`[表情包]` 或纯标点的行没有文字内容，只是背景 —— 没有人点你的名时不要去回应它们」，
防止模型挑着这些行答（群里看到就是"对着表情包自说自话"）。

配置：`skip_contentless_messages`(true) / `name_link_seconds`(45) / `bot_names`([])，见 §6.3。

### 5.8 「先发一张图，再问一句这是什么」= **一轮**（v0.10.0）

`on_llm_request` 的钩子跑在框架 `prepare_request_images` **之前**
（`agent_sub_stages/internal.py`：`call_event_hook(OnLLMRequestEvent)` → `prepare_request_images`），
所以插件可以在这里给请求补挂图片。这条路修掉两个真问题：

1. **图片与随后的问题算一轮**：放行时若本条是**无内容消息**（图片/表情包/标点），
   也开 `merge_seconds`（3s）等待窗口 → 随后那句并进这一轮 →
   `ga_frag_key` / `_frag_pending` → 注入「连着发的几条」说明。**只出一条回复**，
   且模型同时拿到图和问题（图片只有它自己那条事件里才有）。
2. **后一条也要能看到图**：若图片那条**没有被放行**（名字对不上，只记了 L1），
   图仍会缓存 `image_carry_seconds`(60s)；这个人下一条纯文字消息进来时，
   插件把图**补挂到该请求**（`req.image_urls += 缓存`）并附一句
   「他刚发过一张图，已随本条一起给出」→ 模型能答「这是什么」。只补挂一次（补完出队），
   避免这个人的每条消息都重复付费上图。

**等待窗口内 @ 了它**（`@它 这是什么` 紧跟在图后面）：这条也会被并进上一轮
（否则那一轮已经在生成的回复看不到它，而它自己又生成一条 → 两条回复）。
被吞的 @ 记在 `_frag_mentioned[key]`，父请求据此**必回**并在开头 **@ 回去**
（`inject_memory` 里 `set_extra("ga_mentioned", True)` + 注入称呼说明，
后续 `fix_mentions` 的兜底补 @ 因此生效）。

| 场景 | 结果 |
|---|---|
| 图（名字对得上）+ 3 秒内「这是什么」 | **1 条回复**，带图 + 带问题（等 3 秒后生成） |
| 图（名字对不上，不回）+ 60 秒内「这是什么」 | **1 条回复**，图被补挂到这条请求上 |
| 图 + 3 秒内「@它 这是什么」 | **1 条回复**，带图、必回、开头 @ 你 |
| 图之后再无下文 | 名字对得上 → 1 条回复（等 3 秒）；对不上 → 不回（只记 L1） |

配置：`image_carry_seconds`(60)。

---

## 6. 配置基线

### 6.1 AstrBot（`data/astrbot/cmd_config.json`）

| 项 | 值 |
|---|---|
| 平台 | `aiocqhttp`，`ws_reverse_host=0.0.0.0`，`ws_reverse_port=6199`，`token=""` |
| `wake_prefix`（顶层） | `["/"]` |
| Provider | `deepseek/deepseek-flash`，`model=deepseek-flash`，`api_base=https://api.deepseek.com/v1` |
| 模态 | `text, image, tool_use` |
| `streaming_response` | `false` |
| `web_search` | `false` |
| `datetime_system_prompt` | `true` |
| Rate limit | 60s / 30 条，策略 `stall` |
| `provider_ltm_settings` | `group_icl_enable=false`、`group_message_history_enable=false`、`active_reply.enable=false` |
| `agent_runner` | `local`，`max_steps=30`，`tool_schema_mode=full` |
| 人格 / 回复风格 | 在 AstrBot 面板配置（未在本仓库固化） |

### 6.2 桥（`bridge/config.json`）

| 项 | 值 |
|---|---|
| `astrbot.ws_url` | `ws://127.0.0.1:6199/ws` |
| `astrbot.self_id` | `10001` |
| `wechat.bot_nickname` | `hitokami`（用于把 `@昵称` 转成 at 段，**必须与微信群内昵称一致**） |
| `wechat.groups` | `{"49453534744@chatroom": "测试群"}`（**必须与实际群名完全一致**） |
| `wechat.poll_interval` | `0.8` |
| `wechat.verify_send` | `false`（`true` 会回读数据库确认，最多多等 8s） |
| `wechat.rhythm` | `fast` |
| `logging.wechatauto_level` | `DEBUG`（放行 UIA/切换细节，零运行时开销；可改回 `INFO`） |

### 6.3 插件（`plugins/.../config.json`）

| 项 | 默认值 |
|---|---|
| `w0_seconds` | 30 |
| `idle_seconds` | 30 |
| `window_max_seconds` | 120（硬上限） |
| `active_groups_max` | 3 |
| `l1_size` | 25 |
| `record_l1` / `inject_l1` | `true` / `true` |
| `llm_decides_participation` | `true`（LLM 有权用 `[PASS]` 表示不接话） |
| `pass_marker` | `[PASS]` |
| `participation_prompt` | 参与规则文案（含 `{marker}` 占位符） |
| `l3_enable` | `true`（结构化长期记忆） |
| `l3_min_new_messages` / `l3_cooldown_seconds` | 5 / 300（提炼的两道闸门） |
| `l3_max_members` / `l3_max_facts_per_member` | 8 / 4（注入上限） |
| `l3_facts_cap_per_member` | 12（存储上限，超出丢最久未提及的） |
| `merge_enable` | `true`（碎片合并，见 §5.6） |
| `merge_seconds` | 3.0（上一条请求等续句的时长；太长会让正常短句变慢） |
| `fragment_max_chars` | 10（多短 + 无尾标点才算「像碎片」） |
| `fragment_link_seconds` | 20.0（生成已开始后，多久内还算「同一句话的后半截」） |
| `skip_contentless_messages` | `true`（图片/表情包/单个标点：名字对不上就不回，见 §5.7） |
| `name_link_seconds` | 45.0（「名字」往前追溯的时长：他刚 @ 过它 / 他刚被它回过） |
| `bot_names` | `[]`（机器人昵称补充；主名字自动取桥的 `get_login_info`） |
| `image_carry_seconds` | 60.0（先发图、后问一句：图能"随"到后一条请求上的时长，见 §5.8） |
| `ignore_prefixes` | `["/"]` |

> ⚠️ **`config.json` 会覆盖 `config.py` 里的默认值。**
> `PluginConfig.load()` 首次运行时会用默认值生成这个文件，之后只读它 ——
> 所以**只改 `config.py` 的默认值不会生效**，必须同时改 `config.json`
> （或直接删掉 `config.json` 让默认值重新生成）。
> 启动日志会打印生效值：`已加载 v0.4.0：窗口=30s 滑动=30s …`，以此为准。

---

## 7. 脆弱点

| 位置 | 问题 |
|---|---|
| 桥判断「自己发的消息」 | 依赖微信库中「自己发的消息不带 `wxid:` 前缀」的格式特征；**图片没有前缀**，靠 `_sid_map` 现学映射兜底 |
| 桥的 @ 识别 | 微信库里 @ 是**纯文本**、无结构化字段，只能靠昵称匹配。曾因「微信改昵称后配置没跟着改」导致 @ 全部漏识别（表现为机器人装死）；现已改为**优先读数据库里实时的自己昵称** |
| 表情包 | 微信只存加密 content，**无法还原原图**，只能记 `[表情包]` 占位 |
| 引用回复 / 卡片 | 都是 `type 49` 的 appmsg XML，正文在 `<title>`；不解析就等于整条丢失。被引用图片的 md5 与本地 md5 不同，只能靠 `createtime` 匹配（已加 3 秒容差） |
| 图片解密 | 依赖本机存在可解密的 `.dat` 副本；微信只留缩略图时会退化为占位文本 |
| 桥定位会话 | 依赖**群名精确匹配**；群改名会导致每次发送都触发会话切换 |
| 桥群 ID 映射 | `_fake_id` 用 `zlib.crc32`（**不能**用内置 `hash()`，其按进程加盐会使 ID 漂移） |
| 插件挂载 | AstrBot 读的是 `/AstrBot/data/plugins`；**必须靠 compose 里的 `./plugins:/AstrBot/data/plugins` 挂载**，否则改的代码不生效（曾因此长时间误判） |
| 残留旧副本 | `data/astrbot/plugins/astrbot_plugin_group_activation` 里有一份旧代码，已被挂载遮蔽；建议删除以免混淆 |
| 未使用的容器 | `wechatpadpro` / `mysql` / `redis` 三个服务当前链路完全不使用 |
| 窗口与 L1 | 全部为内存态，进程重启即丢失 |
