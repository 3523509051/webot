# 微信 AGI 群聊助手（webot）

让**你自己的微信号**在群里当一个有记性、有分寸的 AI 群友：
默认闭嘴，**@ 它才出现**；被 @ 后打开一段滑动空闲窗口，在窗口内自己判断该不该接话。

> ⚠️ **先读这句**：本项目通过**读取微信本地数据库 + 驱动微信界面**来收发消息，属于**灰色用法**。
> 请用**小号**、别刷屏营销，并对账号风险有心理准备（详见文末《红线与安全》）。
> 仅供个人学习与研究使用。

> 🚀 **第一次安装**：从「前置条件」开始，按部署 §0 → §8 顺序做。
> 只想查 **API Key 怎么填** → §3.2；**桥怎么用/怎么排错** → §5.4、§5.5。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **@ 才出现** | 不 @ 不插嘴。被 @ 后开 30 秒窗口；先在本地筛选同一人的明显追问，放行后模型仍能用 `[PASS]` 保持沉默 |
| **群聊记忆 L1** | 常驻滚动记录群聊背景并注入请求 —— 「群里聊了 20 条，最后 @ 它 你看看」时它接得上话 |
| **长期记忆 L3** | 窗口休眠后**异步**提炼群员画像（谁说过的偏好/事实），下次进群可用；按群隔离，可查看/清空 |
| **群知识库 L4** | 用本地嵌入（Ollama `bge-m3`）做检索，`/garemember` 手动写知识；按群隔离 |
| **多人提问合并** | 首次 @ 后收集相关消息，静默 5 秒统一回复、最多等 30 秒；其他人 @ 和提问者补充可加入同一批次 |
| **无内容消息不瞎接** | 图片/表情包/单个标点这类没有语义的消息，**从"名字"入手**判断（谁刚点过它、它刚在跟谁说话），对不上就不回 |
| **图片能看** | @ 时直接看图；仅发图片先记住，随后明确问图时再交给多模态模型；表情包只取第一帧 |
| **图片跟随追问** | 先发一张图、再问「这是什么」时，图会跟着后一条请求一起给模型，且只出一条回复 |
| **引用回复带原文** | 「（引用 X 的消息：「原话」）你这句话」—— 被引用的原话会一起带上，不再只会答「我没捞到原话」 |
| **空 @ 接上下文** | 单发一个 @（没正文）时，自动接上他刚说的那句来回答 |
| **熄屏挂机** | 屏幕真黑（切到虚拟显示器），机器人照跑；**只有真人碰鼠标/键盘才唤醒**，它自己的点击不会把屏幕弄亮 |

---

## 架构

```
┌── Windows 本机（非容器）────────────────────────────────┐
│  微信桌面版 4.1.15.x（已登录、保持运行、不锁屏）           │
│     ↑ 读：本地数据库增量                                  │
│     ↓ 写：UIA 自动化优先 + 坐标/OCR 兜底（拟人节奏层）      │
│  bridge/（Python venv）                                   │
│     ↕ OneBot v11 反向 WebSocket                           │
└────────────────────────┬──────────────────────────────────┘
                         │ ws://127.0.0.1:6199/ws
┌────────────────────────┴─ Docker ─────────────────────────┐
│  AstrBot（aiocqhttp 适配）                                 │
│    · 默认 LLM 管线 → 任意 OpenAI 兼容模型（示例 DeepSeek）  │
│    · 插件 astrbot_plugin_group_activation                  │
│        ├─ @ 开关 → 滑动空闲窗口                            │
│        ├─ L1/L3/L4 记忆 → 注入请求                         │
│        └─ 碎片合并 / 名字判定 / 引用解析 / 图片跟随          │
│    · WebUI  http://localhost:6185                          │
└────────────────────────────────────────────────────────────┘
```

**插件边界**：插件**不生成任何回复**，只做「开关 + 记忆 + 判定」；所有回复交给 AstrBot 默认 LLM 管线。
本地略过的消息不调用 LLM；放行的消息仍需支付模型调用费用。

### 新功能与升级

当前版本 **v0.19.0**：新增本地表情库，AI 可检索、发送、入库表情图片（图片气泡）；文字+表情用 `[[表情:...]]` 标记一次 API 完成，发送顺序固定文字→图片；窗口 preflight、@+引用图策略、图片 L1 编号、L2 压缩等沿用 v0.18.0。

已有服务器请按 [新功能升级指南](docs/upgrade-v0.19.0.md) 更新；功能配置详见 [地图与网页工具说明](docs/maps-web.md)。

### 消耗控制

- v0.19.0 本地表情库：`D:\webot\stickers` 保存图片与 `index.json`；`list_stickers` 按情绪/场景/关键词检索，`send_sticker` 用 `gui.send_image` 发图片气泡，`save_sticker` 把本群缓存图片正式入库并让 AI 命名、打标签。每条回复最多发 1 张、入库 1 张；同图 sha256 去重，不覆盖不删除。
- v0.18.0 接话 preflight：@ 后的 30 秒窗口内，非 @、非明确请求的**纯文字**批次先用 L1 + 本批 + 机器人上一句判定；PASS 直接停止，不调用完整模型；JOIN 才带 L2/L3/L4 回答。@ 和明确请求直接完整回答，不加分类延迟。
- v0.18.0 窗口图片：只有 **@ + 引用图片** 自动附图；@ 配新图、窗口期非 @ 图都只保留 `[图片 #ID]`，模型需要时调用 `look_image`。
- v0.18.0 图片请求压 L2：只有 **@ + 引用图片** 的请求只带最近 `image_history_max_turns=4` 轮 / `image_history_max_chars=6000` 字；纯文字、@ 新图仍 8 轮 / 12000 字。
- v0.17.0 图片编号与按需查看：桥按**每群独立编号**缓存图片，默认**保留所有**（不自动删除）；L1 记 `[图片 #12]`。模型需要旧图时调用 `list_images` 找编号、`look_image` 取图。
- v0.17.0 防循环：同一编号重复取会返回“已提供”；过期/不存在直接终止；`list_images` 每轮最多 2 次、最多 50 条；`look_image` 每轮最多 10 张（可配置）。工具返回的图只用于本轮回答，提示词禁止转发。
- v0.17.0 分辨率：桥 `image_max_edge=0`、`image_max_bytes=10000000`；AstrBot `provider_settings.image_compress_options.max_size=16384`。`Dockerfile.astrbot` 在**镜像构建时**直接把 `MODEL_IMAGE_MAX_BYTES` 从 512KB 改成 8MB；用 `docker compose up -d --build astrbot` 重建镜像后生效。
- v0.16.1 多图与记忆：L1 只记录编号/占位，不存图片本体；L2 历史图默认剥离（`strip_history_images=true`），旧图不会每轮重发；只有 @ / 明确请求的当前图会送给模型，窗口图只给编号、按需 `look_image`；提示词要求“没被问到的图不主动提、不逐张介绍”。
- v0.16.0 多 @：模型在正文里写 `@昵称`，桥按顺序逐个用 UIA 点选插入真实 @（默认最多 3 个）。插件每次请求向桥拉取**完整群成员名单**，没发过言的成员也能被 @。相关配置：`mention_only_sender`、`mention_max_count`、`force_at_sender`，以及桥的 `wechat.max_mentions`。
- v0.16.0 先图后问兜底：AstrBot 对纯图片事件不一定触发插件缓存时，桥按「群 + 发送者」缓存 30 秒；同一人随后问图时，把图并入同一条事件再上报。
- v0.16.0 路径修正：脚本按仓库自身位置解析，不再硬编码 `C:\webot`；`webot-service.ps1` 从卸载注册表自动识别微信安装目录。
- v0.15.1 网页读取每日上限从 20 次提高到 300 次，可通过 `website_daily_limit` 调整。
- v0.15.0 后台 Playwright 浏览器搜索替代百度 API：默认必应网页版，真实打开页面并填写、提交搜索表单，提取结果链接和摘要；不需搜索 API Key，结果和最终回复均审核，保留 ping <300ms、额度和取消规则。
- v0.14.4 移除搜索和网站读取的批次授权名单校验及对应提示；正常放行对话可自行查询并读取官网或搜索结果，无需逐批提供链接或放权。保留取消、额度、延迟和内容检查，天气仍走高德。
- v0.14.3 网站读取支持 HTTP、裸域名及中文域名正规化；每次跳转仍检查公网地址、ping 延迟及内容；修复直接搜索指令漏判。
- v0.14.2 网站读取默认关闭域名白名单，真实 @ 中带链接即可授权；支持链接紧接中文问题，要求 ping 严格小于 300ms 并通过内容审核；修复 https 链接误判为发送者导致机器人自回消息被再次上报的问题。
- v0.14.1 天气问题直接使用高德实况/短期预报，无需额外要求联网；缺城市先问、重名地区需确认；注明数据更新时间，不再走百度搜索。
- v0.14.0 新增高德地点/多段路线工具及受限网站读取：当前批次真实 @ 授权、域名白名单、公网地址与 ping ≤300ms 检查、重定向复核、独立浏览器只读文字、网页与发送前内容审核。高德需单独配置 Web 服务 Key，详见 [配置与测试说明](docs/maps-web.md)。
- v0.13.3 修复搜索和知识库检索：搜索执行前检查当前批次中对应发送者的明确授权，支持撤回；多人授权需指定发送者。跨群每日请求额度先预留并原子保存，失败、取消和无结果也计入额度，计数异常时停止新增搜索。知识库预取随批次文本更新，最终使用同一查询，默认最多 1000 字、整次检索最多 5 秒，超时跳过注入且不重复检索。
- v0.13.2 修复活动清理与发送状态：成功回复后重新安排长期记忆提炼；`/gasleep`、`/gaclear`、`/gaforget`、`/gakbforget`、`/gareset` 会取消本群旧批次、数字/图片临时任务和后台提炼，并阻止旧请求继续发送。各命令仍只清除其对应的持久化记忆。微信明确发送失败返回非零错误；已操作发送但无法确认则标记 `delivery_state=unknown`，不自动重发。`verify_send=true` 时回读确认使用桥配置的同一份微信数据库。

- 近期交谈者的明确指令（如「来吧，数数」「请翻译」）和问题可开启新批次，即使上一批已经结束。明确请求不再附加允许 `[PASS]` 的参与提示；模型误选沉默时会给出回答或澄清，不额外重试。数数任务按群和发送者独立保存数字消息，跨批次保留，默认最后一次数字后120秒过期、最多200条/12000字符；超限会明确标记记录不完整。单独数字只保存，不独立调用模型。

- 提问批次默认静默 `merge_seconds=5` 秒后派发，从首条起最多 `merge_max_seconds=30` 秒。等待期间每秒检查本地缓冲，不调用模型。其他人明确 @ 可加入同批次，已参与提问的人可以补充；旁观者闲聊、单发图片不续等待。批次结束后新问题进入下一批。每批只放行一个默认模型请求（模型工具调用、失败重试及 L3 提炼仍可能产生额外请求）。图片按发送者绑定；统一回复用昵称分段，需要提醒多人时模型可写多个 `@昵称`（默认最多 3 个）。

- 平时所有群消息进入 L1（短文字，或 `[图片 #12]` / `[图片]` 这种编号/占位），不向模型逐条上报。被 @ 必回；窗口内只有近期和它交谈的人明显追问，才进一步交给模型判断。无关闲聊不会把窗口续活。
- 单发图片或表情包会先缓存并分配本群编号；同一人在 30 秒内发「这张图写了什么」等文字，或单独 @ 它时才补挂。直接 `@机器人` 发图依旧会看图。
- **旧图按需查看**：L1 只保留最近 10 条；更早的图不在 L1 里，模型需要时调用 `list_images` 按发送者/时间找编号，再调用 `look_image` 取图。工具返回的图只用于本轮回答，不转发、不写进 L2；每轮有取图/列目录上限，防止模型反复查。
- 桥默认**不缩分辨率**：`wechat.image_max_edge=0`，单张字节上限 `image_max_bytes=10000000`；超字节上限才退化成 `[图片]`。想省视觉 token 可把 `image_max_edge` 设回 1280。
- AstrBot 默认还有 **512KB** 模型输入上限；本仓库的 `Dockerfile.astrbot` 在构建镜像时直接改成 8MB。更新基础镜像后执行 `docker compose build --pull astrbot`，再 `docker compose up -d --force-recreate astrbot`。
- 图片缓存默认**保留所有**（`image_cache_keep=-1`，不自动删除）。磁盘会持续增长，请自己定期查看 `bridge\cache\image` 大小；需要清理时手动处理，桥不会自动删。
- L1 不存图片本体；L2 历史图默认剥离（`strip_history_images=true`），旧图不会每轮重发。只有**当前批次**的图片会随请求送给模型；提示词要求只描述被问到的图，未被问到的图不主动提、不逐张介绍。
- 每次请求的原生历史：文字请求限最近 8 轮 / 约 12000 个字符；带当前图的请求压到 4 轮 / 6000 个字符。旧图片换成占位；本轮的 L1/L3/L4 随当前请求提供。`plugins/astrbot_plugin_group_activation/config.json` 可调 `local_routing`、`preflight_enable`、`history_max_turns`、`history_max_chars`、`image_history_max_turns`、`image_history_max_chars`、`image_carry_seconds`。字符数只用于粗略限额，实际 token 由模型计费端确定。
- 当前版本的路由使用本地关键词和近期对话关系，因此一些含蓄的插话可能被漏掉；可以直接 @ 它，或把问题问完整。用 `/gareset` 可以新开原生会话，清除既有的长历史。

改插件设置后运行 `restart-astr.cmd`；改桥的 `image_max_edge` 后运行 `restart-bridge.cmd`。观察 AstrBot 日志中的「本地略过非续话」和「已把上一条的图补挂」，再用服务商用量记录核对节省效果。

---

## 前置条件（先把这些准备好）

### 软硬件清单

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | **Windows 10/11 64 位** | 桥依赖微信 Windows 客户端与 UIA，Linux/macOS 跑不了 |
| 微信 | **桌面版 4.1.12+**（本项目在 **4.1.15.13** 上实测） | ⚠️ 密钥提取依赖该版本的 DLL 特征码，**换版本可能要等上游适配** |
| Python | **64 位 3.9+**（实测 3.11） | 只给桥用；AstrBot 在容器里，不需要本机 Python |
| Git | 任意近期版本 | 拉代码、后续更新用 |
| 容器 | **Docker Desktop + WSL2** | 见下方 §0；也可用 Docker Engine（WSL2 里） |
| 浏览器 | **Microsoft Edge** | 后台搜索与网页读取走本机 Edge（Playwright `msedge` 通道） |
| 内存 | 建议 **8 GB 以上** | AstrBot + 微信 + 桥约 3–5 GB；用本地嵌入（L4）再加约 2 GB |
| 模型 | 任一 **OpenAI 兼容 API**（示例 DeepSeek） | 需支持**多模态**才能看图/表情包；Key 填在 WebUI |
| 账号 | **建议专用小号** | 见《红线与安全》 |

### 需要准备哪些 Key（只有模型 Key 必填）

| 用途 | 是否必需 | 填写位置 |
|---|---|---|
| 对话模型 / 看图模型 | **必需** | AstrBot WebUI →「服务提供商」 |
| 高德 Web 服务 Key | 可选（要用天气、地图、路线才填） | 仓库根目录 `.env` 的 `AMAP_WEB_KEY`，然后重建容器 |
| 搜索 API Key | **不需要** | 搜索走本机 Edge 打开必应网页版，不调用付费搜索 API |
| 嵌入模型（L4 知识库） | 可选 | 本机 Ollama + `bge-m3`，在面板加一个嵌入 Provider，详见 §8 |

### 装软件并验证

**Git 与 Python**（普通 PowerShell，不需要管理员）：

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
```

装 Python 时务必勾选 **Add python.exe to PATH**。装完**新开一个** PowerShell 验证：

```powershell
git --version
python --version
python -c "import struct;print('64 位，可用' if struct.calcsize('P')==8 else '32 位，必须换成 64 位')"
```

> 32 位 Python 装不上 `wechatauto-replica` 的依赖，必须是 64 位。
> 如果 `python` 指向 Microsoft Store 的占位程序，用 `py -3.11 --version` 确认，
> 并在后续命令里把 `python` 换成 `py -3.11`。

**Windows 上的两个必要设置**（否则机器人会静默掉线）：

1. **不要锁屏**：设置 → 个性化 → 锁屏界面 → 屏幕保护程序 → 取消「在恢复时显示登录屏幕」，并且**不要按 Win+L**。
2. **不要睡眠**：设置 → 系统 → 电源 → 屏幕和睡眠 → 「使设备进入睡眠状态」设为**从不**（接电源时）。
   屏幕可以关（用 §7 的虚拟显示器方案），但**不能锁屏、不能睡眠、不能注销**。

---

## 部署（从零开始，按顺序做）

### 0. 装 WSL2 与 Docker Desktop

<details open>
<summary><b>展开：详细步骤（含没装 winget 的情况）</b></summary>

**0.1 启用 WSL2 与虚拟机平台**（**管理员** PowerShell）：

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
```

两条都应返回 `RestartNeeded : True`。**只启用 WSL 主体是不够的** ——
`VirtualMachinePlatform` 是独立功能，漏掉会让 Docker 报
`Virtual Machine Platform not enabled`。

**0.2 重启整台电脑**（必须；只重启 WSL 服务不生效）。

**0.3 装 Docker Desktop**：

```powershell
winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
```

没有 `winget`（Win10 较早版本）就到 <https://www.docker.com/products/docker-desktop/> 下安装包手动装。

**0.4 启动 Docker Desktop 并确认可用**：

```powershell
docker info          # 能输出一大段信息（含 Server Version）就 OK
docker compose version
```

若 Docker 报 `500` 或 `docker info` 一直空：多半是缺 WSL/虚拟机平台（回到 0.1），
或 WSL 内核太旧 —— `wsl --update` 后重试。
</details>

### 1. 取代码与建 `.env`

```powershell
git clone https://github.com/3523509051/webot.git c:\webot
cd c:\webot
copy .env.example .env
```

`.env` 只有三件事，**LLM 的 API Key 不在这里**：

```ini
TZ=Asia/Shanghai
ASTRBOT_WEB_PORT=6185
ONEBOT_WS_PORT=6199
AMAP_WEB_KEY=
```

- `ASTRBOT_WEB_PORT`：面板端口；`ONEBOT_WS_PORT`：桥连的端口。改了端口要同步改
  `.env`、面板里的 aiocqhttp 端口、`bridge\config.json` 的 `astrbot.ws_url` 三处。
- `AMAP_WEB_KEY`：可选，高德**「Web服务」**类型的 Key（不是 JS API / 地图 Key），
  要用天气、地点、路线才填。直接贴在 `=` 后面，不要加引号、不要留空格。
- 保存后执行 `docker compose up -d --force-recreate astrbot` 让环境变量生效。

> 下面命令以仓库在 `c:\webot` 为例。仓库可以放在别处（例如 `D:\webot`）：
> `scripts\webot-service.ps1` 会按自身位置推算仓库根，微信安装路径从卸载注册表自动识别；
> 不再需要在脚本里手改 `c:\webot`。

### 2. 起 AstrBot（第一次要拉基础镜像并构建本地镜像）

```powershell
cd c:\webot
docker compose up -d --build astrbot
docker compose ps
docker logs --tail 80 webot-astrbot
```

首次会下载 `soulter/astrbot:latest`，再按 `Dockerfile.astrbot` 构建本地镜像
`webot-astrbot:local`（构建时把图片上限改成 8MB），视网络几分钟到十几分钟。
`docker compose ps` 里 `STATUS` 出现 `Up` 才继续。

**拿面板账号密码**（首次启动才有）：

```powershell
docker logs webot-astrbot 2>&1 | Select-String -Pattern "账号|密码|username|password"
```

- 面板：<http://localhost:6185>
- 用户名通常是 `astrbot`；密码默认 `astrbot`，**新版镜像会生成随机初始密码并打印在首次启动日志里**，
  以日志为准。登录后到「设置 / 账户」里改成自己的密码。
- OneBot 反向 WS 端口：**6199**（桥会连这里）
- 如果忘了密码：先试面板登录页的「忘记密码 / 重置」流程；不行就
  `docker compose stop astrbot`，删除 `data\astrbot\cmd_config.json` 里的 `dashboard` 段再启动，
  让面板重新初始化 —— **会丢面板配置，删前先备份**。



### 3. 面板配置（消息平台 / API Key / 人格）

浏览器打开 <http://localhost:6185>，用 §2 拿到的账号密码登录。下面三块按顺序配。

#### 3.1 消息平台：加一个 aiocqhttp

面板 → **消息平台** → 新增 → 选 **aiocqhttp（OneBot v11）**：

| 配置项 | 填什么 | 说明 |
|---|---|---|
| 平台名称 / ID | `webot` | 任意唯一名字 |
| 启用 | 开 | |
| 反向 WebSocket 主机 | `0.0.0.0` | **必须 0.0.0.0**，写 `127.0.0.1` 容器外连不上 |
| 反向 WebSocket 端口 | `6199` | 与 `.env` 的 `ONEBOT_WS_PORT` 一致 |
| Token | 留空 | 若填了，桥的 `config.json` 里 `astrbot.token` 必须**一模一样** |
| 心跳间隔 | `15` | 与桥默认值一致即可 |

**只建一个实例**：重复的 aiocqhttp 平台会让同一条消息被处理两次（群里会看到双回复）。

保存后确认适配器起来了：

```powershell
docker logs --tail 30 webot-astrbot
```

看到 aiocqhttp 启动、监听 `6199` 即可。桥还没启动时这里没有客户端连接，属正常。

#### 3.2 服务提供商：API Key 怎么填（重点）

需要准备一个 **OpenAI 兼容**的接口。以 DeepSeek 为例：

1. 打开 <https://platform.deepseek.com> → 登录 → **API keys** → 创建 → 复制 `sk-...`
   （**只显示一次**，先存到密码管理器）。
2. 面板 → **服务提供商** → 新增 → 类型选 **OpenAI / OpenAI 兼容**。
3. 按下表填（不同面板版本字段名略有差异，认准「API Base / API Key / 模型」三项）：

| 字段 | 值 |
|---|---|
| 名称 | `deepseek`（随意，自己认得出即可） |
| API Base URL | `https://api.deepseek.com/v1` |
| API Key | `sk-...`（整段粘贴，**前后不要有空格或换行**） |
| 模型 / 模型 ID | `deepseek-flash`（以你控制台实际开通的为准） |
| 思考 / 推理模式 | **关闭**（群聊要的是低延迟） |
| 多模态 / 图片输入 | 纯文本模型**不要勾**；要它看图就换多模态模型 |

4. 保存后把它设为**默认对话模型**（面板里通常叫「使用」「设为默认」）。
5. 面板自带「对话测试 / 聊天」发一句 `你好`，能出字说明 Key、Base、模型名都对。

**换成别家**：任何 OpenAI 兼容端点都行 —— 把 Base 换成服务商给的地址（一般以 `/v1` 结尾），
模型换成对应 ID；OpenAI 官方是 `https://api.openai.com/v1`，国内厂商按各自文档填兼容端点。
不要填成 `.../chat/completions`，AstrBot 自己会拼路径。

**关于看图**：桥会把图片以 base64 上报（不走文件路径，因为 AstrBot 在容器里）。
纯文本模型收到图片会报错或被忽略；要稳定看图/表情包，默认 provider 必须是**多模态模型**
（如 Gemini、Qwen-VL、GPT-4o 一类），且面板里勾选图片输入能力。

**Key 报错对照**：

| 现象 | 原因 |
|---|---|
| `401` / `invalid api key` | Key 复制不全、过期，或前后带了空格 |
| `403` | Key 没有该模型权限，或账号欠费 |
| `404` | Base URL 少了/多了 `/v1`，或写成了完整 `/chat/completions` |
| `model not found` | 模型 ID 与账号可用模型不一致 |
| 一直超时 | 本机需要代理；AstrBot 在容器里，容器默认不走宿主机代理，先试直连 |

**安全**：Key 只保存在 `data\astrbot\cmd_config.json`（已 gitignore），
**不要**写进 `.env`、README、群聊或截图；怀疑泄露立刻到服务商后台吊销重发。
面板 `6185` 与 OneBot `6199` 不要映射到公网。

#### 3.3 关掉与插件重复的功能（必做）

面板 → 配置 / 通用设置 → 群聊相关，把这三项**保持关闭**：

- `群聊上下文`（`group_icl_enable`）
- `群聊消息历史`
- `主动回复`（`active_reply`）

原因：本项目的插件自己维护 L1/L3/L4 记忆与接话判定；框架这些功能再开一遍会
**重复回复、上下文翻倍、延迟明显上升**。不用的 TTS/语音、自带群聊类插件也建议关掉。

#### 3.4 人格（回复风格）

面板 → **人格 / Persona** → 新建或修改系统提示词，然后在平台/会话里选中它。
建议：短句、群友口吻、不用客服腔、不复述提问、不主动说「我是 AI」。
本仓库不固化人格。改完在群里发 `/gareload`（或重启 AstrBot）生效。


### 4. 确认插件挂载（**这一步最容易踩**）

```powershell
docker exec webot-astrbot cat /AstrBot/data/plugins/astrbot_plugin_group_activation/metadata.yaml
```

看得到内容就对了。看不到说明 `docker-compose.yml` 少了这一行（本仓库已带）：

```yaml
    volumes:
      - ./data/astrbot:/AstrBot/data
      - ./plugins:/AstrBot/data/plugins     # ← 缺这行，改 ./plugins 下的代码不生效
```

改完插件代码后要 `docker compose restart astrbot`，并看日志确认版本：
`[group_activation] 已加载 v0.19.0 …`

#### 4.1 插件参数（可选，按需改）

文件：`plugins\astrbot_plugin_group_activation\config.json`（仓库内，挂载进容器）。
改完执行 `restart-astr.cmd` 或 `docker compose restart astrbot`，日志会打印新版本与关键参数。

| 字段 | 默认 | 作用 |
|---|---|---|
| `w0_seconds` | 30 | 被 @ 后的初始窗口长度（秒） |
| `idle_seconds` | 30 | 最后一次活动后多久休眠（滑动窗口） |
| `window_max_seconds` | 120 | 窗口最长存活时间 |
| `active_groups_max` | 3 | 同时活跃的群上限 |
| `l1_size` | 10 | 每次注入多少条 L1 群聊背景 |
| `local_routing` | true | 本地先筛掉无关消息，省 token |
| `llm_decides_participation` | true | 放行后模型仍可输出 `[PASS]` 保持沉默 |
| `merge_seconds` / `merge_max_seconds` | 5 / 30 | 碎片合并的静默等待 / 最长等待 |
| `history_max_turns` / `history_max_chars` | 8 / 12000 | 原生多轮历史的上限 |
| `image_history_max_turns` / `image_history_max_chars` | 4 / 6000 | 带当前图的请求使用的 L2 上限 |
| `preflight_enable` | true | 纯文字窗口消息先做 L1-only 接话判定 |
| `preflight_timeout` | 8 | 接话判定超时（秒），失败时 fail-open |
| `image_carry_seconds` | 30 | 「先发图、再问图」的图片跟随窗口 |
| `image_look_max_per_turn` | 10 | 每轮最多调用几次 `look_image`（防工具循环） |
| `image_list_max_per_turn` | 2 | 每轮最多调用几次 `list_images` |
| `image_list_limit` | 20 | `list_images` 默认返回条数（硬上限 50） |
| `sticker_send_max_per_turn` | 1 | 每条回复最多发几张本地表情 |
| `sticker_save_max_per_turn` | 1 | 每条回复最多入库几张表情 |
| `sticker_send_timeout` | 90 | 图片发送工具等待桥完成的秒数；不要设太短 |
| `strip_history_images` | true | 把 L2 历史消息里的图换成占位，避免每轮重发 |
| `stripped_image_placeholder` | `[图片]` | 剥离历史图后的占位文字 |
| `mention_only_sender` | false | true = 只允许 @ 发送者、删除多余 @；false 才支持多 @ 和 @ 其他成员 |
| `mention_max_count` | 3 | 一条回复最多保留几个 @（`mention_only_sender=false` 时生效），需与桥的 `wechat.max_mentions` 一致 |
| `force_at_sender` | false | 被 @ 时是否强制回复开头 @ 发送者；false 由模型决定 |
| `mention_max_names` | 12 | 注入给模型的可 @ 名单上限；群成员多于 12、目标不在名单时调大 |
| `bot_names` | `[]` | 机器人昵称补充别名；通常留空，插件会从桥的 `get_login_info` 自动取 |
| `kb_enable` | false | L4 知识库；本仓库默认关闭，装 Ollama 嵌入后改 `true`（不影响聊天） |
| `search_enable` / `search_daily_limit` | true / 50 | 后台浏览器搜索与每日上限 |
| `amap_enable` / `amap_daily_limit` | false / 100 | 高德工具与每日上限；填 `AMAP_WEB_KEY` 后改 `true` |
| `website_enable` / `website_daily_limit` | true / 300 | 网页正文读取与每日上限 |
| `ignore_prefixes` | `["/"]` | 以这些前缀开头的消息不参与 |

### 5. 装并启动微信桥（本机，不在容器里）

先确认：**微信已登录、保持运行、停在目标群、没有锁屏**。

#### 5.1 建虚拟环境、装依赖

```powershell
cd c:\webot\bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

- 依赖里含 `wechatauto-replica`（读微信库 + UIA 发送）、`websockets`、`Pillow`、
  `playwright` 等，第一次装需要几分钟。
- 网页搜索/读取用**系统已装的 Microsoft Edge**（`browser_channel=msedge`），
  一般**不需要**再跑 `playwright install`。如果日志明确报缺浏览器，再执行
  `.\.venv\Scripts\python.exe -m playwright install chromium`。
- 装依赖失败先确认 Python 是 **64 位**（见上方「前置条件」）。

#### 5.2 自检：先确认能读到微信

```powershell
.\.venv\Scripts\python.exe probe_db.py       # 找微信数据目录
.\.venv\Scripts\python.exe introspect.py     # 读账号 / 群列表 / 消息字段
```

`probe_db.py` 成功时会打印一行 `>>> 请在 config.json 中使用 db_dir = ...`，
把那个路径填进 `wechat.db_dir` 即可（找不到目录时必填）。

`introspect.py` 会打印 `self_info`（你的微信昵称、wxid）和 `groups` 列表。
在输出里找形如 `"username": "xxxxxxxx@chatroom"` 和对应的群名称，
这两个值就是 `wechat.groups` 的键和值。

> 这两个脚本失败，说明微信没登录、版本不兼容，或「文档」目录被 OneDrive 重定向。
> 先解决自检，不要急着启动 `main.py`。

#### 5.3 写 `bridge\config.json`

```powershell
copy config.example.json config.json
notepad config.json
```

第一次只需要改这两项（其余保持默认就能跑）：

```json
{
  "astrbot": {
    "ws_url": "ws://127.0.0.1:6199/ws"
  },
  "wechat": {
    "bot_nickname": "你的机器人群昵称",
    "groups": {
      "12345678901@chatroom": "群名称（与微信界面完全一致）"
    }
  }
}
```

完整字段说明：

| 字段 | 默认 | 说明 |
|---|---|---|
| `astrbot.ws_url` | `ws://127.0.0.1:6199/ws` | 与面板 aiocqhttp 的端口一致；**末尾的 `/ws` 不能少** |
| `astrbot.token` | 空 | 面板里填了 Token 就必须一致 |
| `astrbot.self_id` | `10001` | 桥对 AstrBot 自称的机器人 ID，仅内部用 |
| `astrbot.heartbeat_seconds` | 15 | 心跳间隔 |
| `wechat.bot_nickname` | `群助手` | 机器人群昵称，作 @ 识别的补充别名（主用数据库里的本人昵称），建议与微信界面一致 |
| `wechat.bot_nicknames` | `[]` | 额外别名（改过昵称、有特殊符号时用） |
| `wechat.groups` | `{}` | `{"群wxid@chatroom": "群显示名"}`，**群名必须完全一致**（发送靠它定位会话） |
| `wechat.group_whitelist` | `[]` | 仅当 `groups` 为空时生效，按群名/ID 过滤自动扫到的群 |
| `wechat.db_dir` | 空 | 微信数据目录；留空自动探测，OneDrive 重定向时必填 |
| `wechat.poll_interval` | 1.5 | 轮询间隔（秒），越小越及时、越费 CPU |
| `wechat.rhythm` | `fast` | 拟人节奏层：`natural`/`calm`/`fast`/`off`；**不要设 `off`**（风控） |
| `wechat.verify_send` | false | 发送后回读数据库确认，更可靠但最多多等 8 秒 |
| `wechat.send_images` | true | 把图片以 base64 上报给 AstrBot |
| `wechat.image_max_bytes` | 10000000 | 单张图片字节上限，超了退化成 `[图片]` |
| `wechat.image_max_edge` | 0 | 长边像素上限；`0` = 不缩分辨率，`1280` = 省视觉 token |
| `wechat.fetch_stickers` | true | 表情包当图片处理（只取第一帧） |
| `wechat.image_cache_keep` | -1 | 图片缓存保留策略；-1 = 保留所有（默认），0 = 关闭索引；不自动删除 |
| `wechat.image_carry_seconds` | 30 | 桥侧「先图后问」兜底窗口；0 = 关闭 |
| `wechat.max_mentions` | 3 | 一条回复最多转换几个真实 @；需与插件 `mention_max_count` 一致 |
| `website.enabled` | true | 网页读取 / 后台搜索总开关 |
| `website.search_engine` | `bing` | `bing` 或 `baidu`（网页版，不需要 API Key） |
| `website.restrict_domains` | false | 是否启用域名白名单 |
| `website.max_ping_ms` | 300（示例配置） | 访问前 ping 必须严格小于该值；不写该字段时内置兜底是 500 |
| `logging.level` | INFO | 排障时可改 `DEBUG` |

#### 5.4 启动

两种方式任选，**不要同时开两份**：

```powershell
# 方式 A：前台运行，方便看日志（调试用）
.\.venv\Scripts\python.exe main.py
```

**方式 B（推荐日常）**：双击仓库根目录的 `start.cmd`。它会拉起 Docker Desktop →
起 AstrBot → 等 OneBot 就绪 → 后台起桥，桥的输出写到 `bridge\bridge.log` 与
`bridge\bridge.err.log`，关掉窗口不会杀掉机器人。

启动成功的日志（`bridge.err.log` 或前台窗口）应包含：

```
微信账号: <昵称> (wxid_xxx)
图片解密密钥: ('xxxxxxxxxxxxxxxx', nnn)
监听 N 个群: ['群名', ...]
已连接 AstrBot -> ws://127.0.0.1:6199/ws
```

缺哪行就查哪项：

| 缺的行 | 含义 |
|---|---|
| `微信账号` | 微信没登录 / 版本不匹配 / 数据目录找不到（回 §5.2） |
| `图片解密密钥` | 密钥提取失败；图片会退化成 `[图片]`，文字仍可用 |
| `监听 N 个群` | `groups` 配错或为空；群名/群 ID 与微信不一致 |
| `已连接 AstrBot` | AstrBot 没起、端口不对、Token 不匹配（桥每 5 秒自动重连） |

#### 5.5 桥的日常使用规则

- **@ 才出现**：不 @ 不回复。被 @ 后开 30 秒窗口，窗口内只接「和它刚说的话有关」的追问。
- **首次 @ 会等 5 秒**：合并同一批提问，最多等 30 秒，避免一句话回三遍。
- **图片**：`@机器人 + 图片` 直接看图；单发图片先缓存并分配本群编号，30 秒内补一句「这是什么」会带上图。
  引用旧图或让模型按 `[图片 #ID]` / `list_images` 查找时，也能找回（默认保留所有）。
- **引用消息**：引用别人的原话会一起带上，不需要它自己猜。
- **只监听 `groups` 里的群**：新进群必须把群加进 `config.json` 再 `restart-bridge.cmd`
  （群列表只在桥启动时读一次）。
- **改配置后重启对应服务**：改 `bridge\config.json` / 桥代码 → `restart-bridge.cmd`；
  改插件配置 / 插件代码 → `restart-astr.cmd`。
- **微信窗口**：桥发送时会自己切会话、把微信窗口切到前台，属正常现象；
  期间**不要锁屏**，否则 UIA 点击和键入会失败。

#### 5.6 指名回复与多 @

模型不直接操作微信 @ 面板，它只在正文里写 `@昵称`；桥负责把文字变成真实 @：

1. 桥从正文里按群成员显示名找出 `@昵称`（最多 `wechat.max_mentions` 个）；
2. 按正文顺序：粘前段 → 键入 `@` → 等成员弹层 → 鼠标点选该成员 → 粘下一段；
3. 一条回复可以 @ 多个成员，默认最多 3 个；没发过言的成员也能被 @。

相关开关：

- 插件 `mention_only_sender=false`：允许多 @ / @ 其他成员；设为 `true` 会退回「只 @ 本条发送者并删除多余 @」。
- 插件 `mention_max_count` 与桥 `wechat.max_mentions`：两边要一致，默认 3。
- 插件 `force_at_sender=false`：不强制回复开头 @ 发送者，由模型决定。
- 插件 `mention_max_names`：注入给模型的名单上限，默认 12；群成员多时把目标挤掉就调大。

> ⚠️ 桥只解析**正文文本**里的 `@昵称`。如果模型使用 AstrBot 的
> `send_message_to_user` 工具发正文，`plain` 文本里也必须写 `@昵称`；
> 只填工具参数里的 `mention_user_id` 不会生效（桥拿不到显示名，会丢掉结构化 at 段）。

失败时：某个 @ 点选失败，桥会清空输入框、去掉 @ 名字按纯文字发送，日志写
`@=没成→去掉@按纯文字发`；不会出现「@ 到一半」的畸形消息。成功时日志形如：

```
[发送@] 测试群 -> NULL、NONE（UIA 6.55s, 路径=direct, @=成, 共2个）
```

#### 5.7 图片、L1/L2 与多图

- **L1 存编号/占位**：群聊背景里图片/表情包记为 `[图片 #12]`（缓存编号）或 `[图片]`（未入缓存），不含图片数据，不会把历史图重新送进模型。
- **L2 默认剥图**：`strip_history_images=true` 会把会话历史里的 `image_url` 换成 `[图片]`；否则每轮都会重发旧图，token 会随历史线性增长。
- **只有 @ + 引用图片自动附图**：同一批消息里，只有被 @ 且明确引用图片的那条会把引用图放进请求；@ 配新图、窗口期图片都只保留 `[图片 #ID]`，需要时再 `look_image`。
- **窗口外的单图**：单发图先缓存并记编号；窗口期追问只带 L1 编号，模型需要时按 ID 取图；只有引用图 + @ 才自动附图。
- **分辨率**：桥 `wechat.image_max_edge=0` 不缩放，单张上限 `image_max_bytes=10000000`；AstrBot `provider_settings.image_compress_options.max_size=16384`。`Dockerfile.astrbot` 在镜像构建时把 `MODEL_IMAGE_MAX_BYTES` 改为 8MB；首次/更新基础镜像后执行 `docker compose up -d --build astrbot`。
- 想省视觉 token：把桥 `image_max_edge` 设回 `1280`；想看细节：保持 `0`，但注意每张图都会计入模型视觉 token。

#### 5.8 图片编号与按需查看（v0.17.0）

桥会把收到的图片按**每群独立编号**存进 `bridge/cache/image/`，并写 SQLite 索引；默认**保留所有**，不自动裁剪。

- 图片事件会带一段文字标记 `[图片 #12]`，L1 记录这个编号；
- 模型需要看旧图时：
  1. `list_images(sender?, since?, limit?)` → 返回 `#编号 / 时间 / 发送者 / 上下文`；
  2. `look_image(image_id=12)` → 桥取图，AstrBot 把图追加到**同一轮**上下文，模型继续回答；
- 人格、L1/L3/L4、会话历史都不变；工具只是取图；
- 工具返回的图仅用于当轮回答，提示词禁止转发；
- 模型看到 L1 的 `[图片 #ID]`、确实需要看时，应**直接调用 `look_image`**，不要先发“我看不到图 / 我去调出来看看”的预告；工具返回后再回答；
- 引用图片如果本机没有原图（例如那张图在索引启动前就发过），桥会明确标“本机没有找到原图，不要猜测内容”，不会拿最近的其他图冒充；
- **合并转发的聊天记录**：桥能读标题、文字摘要、文字条目、发送者和图片数量；记录**内部的图片**是微信 CDN 加密引用（`cdnthumburl`/`cdnthumbkey`），桥当前不下载，模型会明确说明“记录内图片无法直接读取”，不会猜；单张转发图片仍按普通图片解密。
- L1 只保留最近 10 条，更早的图靠 `list_images` 按发送者/时间检索；
- 防循环：同一编号重复取返回“已提供”；过期/不存在直接终止；`list_images` 每轮最多 2 次、最多 50 条；`look_image` 每轮最多 10 张（`image_look_max_per_turn` 可调）。

> 磁盘：`image_cache_keep=-1` 表示不删除；图片越多占用越大，桥只记录不清理。需要腾空间时手动处理 `bridge\cache\image` 与 `bridge\cache\image_index.sqlite3`（不要只删文件而保留索引）。

#### 5.9 接话 preflight 与图片请求压 L2（v0.18.0）

- **@ / 明确请求**：跳过分类器，直接完整回答；只有 **@ + 引用图片** 才自动附图。
- **@ 后 30 秒窗口内的非 @ 纯文字**：
  1. 先用 L1 + 本批文字 + 机器人上一句做 `JOIN` / `PASS` 判定；
  2. `PASS` → `stop_event`，不调用完整模型；
  3. `JOIN` → 继续完整请求，并禁用完整模型的 `[PASS]`。
- **窗口期非 @ 的图**：只保留 L1 的 `[图片 #ID]`，不自动附图；需要时由模型调用 `look_image`。这样无关表情包不会被塞进每次请求。
- **@ 配新图（不是引用）**：也不自动附图，只给 L1 编号，由模型 `look_image` 决定是否看；只有引用图片才是明确的“看图”请求。
- **图片请求压 L2**：只有 **@ + 引用图片** 的请求用 `image_history_max_turns=4` / `image_history_max_chars=6000`；纯文字、@ 新图、窗口消息仍 8 / 12000。`look_image` 是在同一请求内调用的，无法在工具调用时再压 L2。
- **失败策略**：分类器超时/异常/输出无法解析时 fail-open，继续完整请求，避免装死。

#### 5.10 本地表情库（v0.19.0）

目录：

```text
D:\webot\stickers\
  index.json
  files\st_0001_无语_无语扶额.gif
```

- `list_stickers(query?, emotion?, scene?, limit?)`：按情绪/场景/关键词检索；
- **文字 + 表情（推荐）**：模型在最终回复末尾写 `[[表情:无语]]` 或 `[[表情:st_0006]]`；插件解析标记，调用桥的 `text_sticker` 在一个动作里先发文字再发图，只消耗**一次模型 API**，且顺序固定文字→图片；
- 标记必须放在**完整正文**末尾；不能只写 @ 或只写标记，否则插件/桥会自动补一句兜底正文，避免空 @；
- `text_sticker` 含 @ 时会走真实 @ 再发图；发图复用文字发送后的输入框缓存并优先 fast-paste，但仍受微信 UIA 粘贴/检测时间限制；
- 机器人自己发出的图片会被桥识别为回声并丢弃，不会重新进 L1/视觉；
- `send_sticker(sticker_id, standalone=false)`：默认把表情排队，等文字发送后补图；`standalone=true` 时只发这一张表情、丢弃最终文字，适合“用一个表情表达看法”；桥用 `gui.send_image` 发**图片气泡**，不是微信 sticker 消息；
- `save_sticker(image_id, name, emotion, scenes, tags, description)`：把本群缓存图正式入库，AI 命名/打标签；同图 sha256 去重，不覆盖不删除；
- 每条回复最多发 1 张、入库 1 张（`sticker_send_max_per_turn` / `sticker_save_max_per_turn`）；
- 图片和 `index.json` 不进 Git，详见 [stickers/README.md](stickers/README.md)。

### 6. 验证（按顺序做六条）

微信停在目标群、不锁屏，然后：

1. **纯文本**：群里发 `@你的机器人 你好` → 数秒内回复；
   桥日志出现 `[收到]` → `[上报]` → `[发送@]` 三段。
2. **引用图**：引用一张图并 `@你的机器人` → 它能描述引用图（只有 @ + 引用图自动附图）。
3. **先图后问**：先单发一张图（不 @），30 秒内再发 `这张图里写了什么`
   → 窗口期只给 L1 编号，模型需要时 `look_image`；不应自动把图塞进请求。
4. **记忆**：先聊两句，再 `@你的机器人 刚才我们说到哪了` → 能接上上下文。
5. **多 @ / 转告**：`@你的机器人 你查一下高斯生平，然后告诉NULL，同时也提醒我待会查山姆生平`
   → 回复里 `@NULL` 和 `@NONE` 两段都有正文；桥日志出现 `共2个`。
6. **旧图按需查**：先发一张图（拿到 `[图片 #ID]`），等 L1 滚出后问
   `@你的机器人 帮我看看之前那张图` → 模型应调用 `list_images` / `look_image`；
   AstrBot 日志出现 `Agent 使用工具: ['list_images', 'look_image']`，且回复不转发图片。

失败时按顺序定位：

```powershell
# 桥有没有收到、有没有上报
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 50
# 框架有没有放行给模型
docker compose logs --tail 80 astrbot
```

桥日志没有 `[收到]` → 桥/微信/群名配置问题；有 `[收到]` 没 `[上报]` → 该条被判为无需处理；
框架日志没有「放行默认 LLM」→ 窗口没开或没识别成 @；
有「参与判定已生效」→ 模型自己选了 `[PASS]`（正常，它在判断不该接话）。

> 第一次发送会慢一些：桥要先定位微信窗口/输入框并做一次窗口校准。

### 7. 熄屏挂机（可选，但这是本项目最实用的部分）

**唯一方案：切到虚拟显示器** —— 物理屏失去信号而真黑，系统仍认为"显示器开着"，
所以**不进连接待机、进程不被挂起、网卡不断开**，AI 照常操控微信。

前提（一次性，需管理员）：

```powershell
scripts\vdd.ps1 -Action setup           # 装虚拟显示器驱动（usbmmidd_v2）
scripts\vdd.ps1 -Action install-task    # 建两个免 UAC 计划任务（否则每次熄屏弹 UAC）
```

之后：

```powershell
screen-off.cmd            # 双击：体检 → 切虚拟屏（物理屏真黑）
# 碰一下鼠标或键盘即恢复；再双击一次 = 恢复
```

详细原理、踩坑记录与排查见 [`docs/deployment.md`](docs/deployment.md) §4.3。

> 驱动包不随本仓库分发（第三方样例驱动）。从 Amyuni 官方样例包或
> <https://github.com/IYATT-yx/NullSignal> 内附的同一份获取。
>
> ⚠️ 脚本按**固定路径**找驱动：`tools\vdd\amyuni\usbmmidd_v2\deviceinstaller64.exe` ——
> 拿到驱动包后请按这个目录结构放置（否则熄屏会报 `✗ 找不到虚拟显示器驱动包`）。

### 8.（可选）本地知识库 L4

前提：先装 **Ollama** 并保持后台运行（`winget install -e --id Ollama.Ollama`）。
L4 用**本地嵌入**（Ollama `bge-m3`）做语义检索，需要额外约 2 GB 内存：

```powershell
scripts\pull_ollama_model.ps1     # 拉取 bge-m3（绕过代理 fake-IP 导致的 pull 失败）
docker compose stop astrbot
c:\webot\bridge\.venv\Scripts\python.exe c:\webot\scripts\add_embedding_provider.py "c:\webot\data\astrbot\cmd_config.json"
docker compose start astrbot
```

要点：

- 嵌入 Provider 的 Base URL 必须是 **`http://host.docker.internal:11434`**，
  写 `localhost` 会指向容器自己。
- Provider ID 要与插件 `config.json` 的 `kb_embedding_provider_id` 一致，默认 `ollama/bge-m3`。
- 写 `cmd_config.json` 前**必须先停容器**，否则 AstrBot 退出时会回写覆盖。

不需要长期知识库的话，把插件 `config.json` 的 `kb_enable` 设为 `false` 即可关掉
（只影响 `/garemember` 那类知识，**不影响日常聊天**）。

---

## 日常使用

### 启动、停止、重启（双击即可）

| 双击这个文件 | 作用 | 什么时候用 |
|---|---|---|
| **`start.cmd`** | 拉起 Docker Desktop → 起 AstrBot → 后台起微信桥，最后打印状态 | 开机后、崩溃后；**不会重启已经在跑的** |
| `stop.cmd` | 停桥 + 停容器（Docker Desktop 保持运行） | 维护、临时下线 |
| `status.cmd` | 只读查看状态 | 怀疑没起来时 |
| **`restart-bridge.cmd`** | 只重启桥（不碰 AstrBot） | 改 `bridge\config.json`、桥代码、**新进群** |
| **`restart-astr.cmd`** | 只重启 AstrBot（桥会自动重连） | 改插件代码、插件 `config.json`、人格 |
| **`screen-off.cmd`** | 切虚拟显示器熄屏，机器人继续跑 | 夜里挂机（见 §7） |

底层脚本也可直接调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 stop
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 bridge   # 只重启桥
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 astr     # 只重启 AstrBot
```

> ⚠️ **开机自启未配置**：重启电脑后机器人不会自己起来，要手动双击 `start.cmd`
>（它会拉起 Docker Desktop → 起容器 → 起桥）。想自动化就建一个"登录时运行 start.cmd"的计划任务。

### 看日志

```powershell
docker compose logs -f astrbot                                          # 框架 + 插件日志
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 50   # 桥日志（推荐）
Get-Content c:\webot\bridge\bridge.log -Encoding Default -Tail 50       # 桥标准输出
```

### 第一次部署完成后，建议按这张清单过一遍

- [ ] `status.cmd` 里 AstrBot 容器是 `Up`，桥显示已连接
- [ ] 群里发 `/gaw` 有回复（说明消息链路通）
- [ ] 发 `/gactx` 能看到 L1 背景（说明记忆注入生效）
- [ ] `@机器人 你好` 能回，且只回一条（回两条说明有重复实例或框架群聊功能没关）
- [ ] 发一张图能描述（纯文本模型会失败，属预期）
- [ ] 打开服务商后台的用量页，确认没有被高频调用

### 改了什么要重启什么

| 改动 | 需要的操作 |
|---|---|
| `bridge\config.json`、桥的 `.py`、新进群 | `restart-bridge.cmd` |
| `plugins\astrbot_plugin_group_activation\config.json`、插件代码 | `restart-astr.cmd` |
| `.env`（端口、高德 Key） | `docker compose up -d --force-recreate astrbot` |
| 面板里的模型 / 人格 / 平台 | 面板内保存即可；人格也可用 `/gareload` |
| 升级代码 | `git pull` → `restart-astr.cmd` → 依赖变了再 `pip install -r bridge\requirements.txt` → `restart-bridge.cmd` |

### 消耗与额度

- 只有**被放行**的消息才会调用模型；L1 背景、本地判定都在本机完成，不花 token。
- 每批提问默认只放行**一个**默认模型请求；网站读取成功通常额外增加**两次内容审核**调用。
- 三个日额度按**本实例所有群合计**计数，改了插件 `config.json` 才生效：
  `website_daily_limit`（默认 300）、`search_daily_limit`（50）、`amap_daily_limit`（100）。
- 失败、取消、无结果也计数 —— 这是本地资源上限，不等于服务商的剩余额度。

**群内命令**（在群里直接发）：

| 命令 | 作用 |
|---|---|
| `/gaw` | 看当前激活的窗口（剩余秒数、L1 条数） |
| `/gactx` | 看这一轮会注入给模型的记忆内容 |
| `/gasleep` | 强制本群休眠 |
| `/gaclear` | 清空本群 L1 记忆并休眠 |
| `/gamem` `/gaforget` | 查看 / 清空本群长期记忆（L3） |
| `/gakb` `/gakbforget` | 查看 / 清空本群知识库（L4） |
| `/garemember <文本>` | 手动往本群知识库写一条 |
| `/gareload` `/gareset` | 重载人格 / 重置本群多轮历史 |
| `/gaon` `/gaoff` | 远程点亮屏幕 / 熄屏 |

---

## 目录结构

```
webot/
├── bridge/                 # Windows 本机微信桥（Python，非容器）
│   ├── main.py             # 主循环：轮询数据库 → 上报；响应发送
│   ├── wechat_client.py    # 封装 wechatauto-replica（媒体解密 / UIA 发送 / 表情包转帧）
│   ├── onebot11.py         # 极简 OneBot v11 反向 WS 客户端
│   ├── config.json         # 桥配置（不入库，用 config.example.json 起步）
│   └── requirements.txt
├── plugins/
│   └── astrbot_plugin_group_activation/   # 插件源码（挂载进容器）
├── scripts/                # 运维与诊断脚本（熄屏、虚拟屏、服务、BOM 修复…）
├── docs/                   # 全部文档（见下）
├── data/astrbot/           # AstrBot 运行时数据（不入库）
├── docker-compose.yml      # 只有 astrbot 一个服务
├── .env.example
└── start.cmd  stop.cmd  status.cmd  screen-off.cmd
```

---

## 红线与安全

1. **账号风险**：本方案读取微信数据库密钥并驱动界面自动化，属**灰色行为**。
   上游库的笔记里记载过**真实账号触发风控**的案例。**请用小号**，不要营销刷屏，
   桥的拟人节奏层（`rhythm`）**不要改成 `off`**。
2. **不能锁屏**：界面自动化需要可见的窗口会话 —— 锁屏（Win+L）、注销、断开远程桌面
   都会让键盘/点击失效。请关闭自动锁屏（熄屏≠锁屏，本项目的熄屏方案不影响发送）。
   另外：机器人**发送时会自动把微信窗口切到前台**（界面自动化的需要）——
   你正用电脑时它回消息，微信会跳到最前面，属正常现象。
3. **不要暴露到公网**：面板（6185）与 OneBot（6199）只监听本机。若需远程访问，请走内网/隧道并加认证。
4. **密钥与隐私**：`.env`、`data/`（含会话、记忆、知识库）、`bridge/config.json`、`logs/` 都已被
   `.gitignore` 忽略 —— **提交前请再 `git status` 过一眼**。聊天记录与群员画像属于隐私数据，别传上去。
5. **免责**：仅供个人学习研究。使用造成的账号限制、数据丢失等后果由使用者自负。

---

## 常见问题

**面板登录密码是多少 / 忘了怎么办**
首次启动日志会打印（§2）：

```powershell
docker logs webot-astrbot 2>&1 | Select-String -Pattern "密码|password|username|账号"
```

忘了就按 §2 的重置办法处理（先备份 `cmd_config.json`）。

**填了 API Key 还是 401 / 404 / model not found**
对照 §3.2 的报错表逐项查：Base 是否以 `/v1` 结尾、Key 前后有没有空格、
模型名是否已开通、有没有设为**默认** provider。面板的「对话测试」会给出具体 HTTP 错误。

**桥一直连不上 AstrBot（`did not receive a valid HTTP response`）**
按顺序查：`docker compose ps` 里容器是否 `Up` → 端口 `6199` 是否映射 →
面板 aiocqhttp 的 host 是否 `0.0.0.0`、端口是否 `6199` → Token 是否与
`bridge\config.json` 的 `astrbot.token` 一致。桥每 5 秒自动重连，改完面板保存后等一会儿即可。

**Docker 引擎报 `500` / `docker info` 为空**
缺 WSL2 或虚拟机平台功能 → 回到 §0.1 两条命令 + **重启整机**；仍不行 `wsl --update`。

**改了插件代码但行为没变**
`docker-compose.yml` 必须挂载 `./plugins:/AstrBot/data/plugins`，改完要 `docker compose restart astrbot`
（§4）。确认日志里的版本号变了。

**桥报「未找到微信数据库目录」**
「文档」目录被 OneDrive 重定向所致；`bridge/paths.py` 已做兜底。仍失败就设环境变量
`WECHAT_DB_DIR`，或在 `config.json` 的 `wechat.db_dir` 里直接指定。

**桥呈现出两个 python 进程**
`.venv\Scripts\python.exe` 与其子进程 `C:\Python311\python.exe` 是 venv 启动器的正常结构，
**不是重复运行**。

**@ 了它却不回**
按顺序查：桥日志有没有 `[收到]`（没收到 → 桥/微信/群名配置问题）→ 有没有 `[上报]`
（没上报 → 该条被判为无需处理）→ 框架日志有没有 `放行默认 LLM`（没有 → 窗口没开或没被识别为 @）
→ `参与判定已生效`（模型自己选择了 `[PASS]`）。

**表情包它说看不到 / 只回一句「发了个表情包」**
表情包现在会当图片处理：先解出消息里的 `md5` 与 `cdnurl`，下载原文件后**只取第一帧**存 JPEG 再上报。
取不到（无 cdnurl / 下载失败）才退化为占位。日志里会有
`表情包 → 静态图: <md5>.jpg（原 … 字节/… 帧/…x… → … 字节）`。

**回复很慢（4–7 秒）**
延迟主要在**桥的界面发送链路**（逐字键入 + 发送闸门），LLM 只占约 1.2 秒。
量化数据见 [`docs/latency-diagnosis.md`](docs/latency-diagnosis.md)。

**熄屏后内屏自己亮回来**
`DisplaySwitch.exe` 不退出会把显示配置回退 → 脚本切换后会杀掉它。仍异常就查
`logs\screen-off.log`，详见 [`docs/deployment.md`](docs/deployment.md) §4.3。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/deployment.md`](docs/deployment.md) | **部署与排障**（含熄屏挂机、电源、常见故障） |
| [`docs/architecture.md`](docs/architecture.md) | 架构、收/发消息链路、插件行为、配置基线 |
| [`docs/context-design.md`](docs/context-design.md) | 记忆设计（L1/L2/L3/L4 各是什么、为什么） |
| [`docs/design.md`](docs/design.md) | 总览、决策总表、现状差距 |
| [`docs/tech-stack.md`](docs/tech-stack.md) | 选型与路线对比、风险登记 |
| [`docs/latency-diagnosis.md`](docs/latency-diagnosis.md) | 延迟量化与优化记录 |
| [`docs/porting.md`](docs/porting.md) | **换机器/迁移指南**（哪些能直接搬、哪些要重做） |
| [`docs/deploy-sp4.md`](docs/deploy-sp4.md) | **Surface Pro 4 部署 Runbook**（写给执行部署的 AI agent：阶段化步骤 + 验收 + 故障对照） |
| [`docs/wechatpadpro-notes.md`](docs/wechatpadpro-notes.md) | 备用路线：Pad 协议桥的调研与排错记录 |
| [`docs/roadmap.md`](docs/roadmap.md) | 里程碑与待办 |

---

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 聊天机器人框架（本项目的容器与 LLM 管线）
- [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) —— 微信 4.x 数据库读取与 UIA/OCR 发送
- [NullSignal](https://github.com/IYATT-yx/NullSignal) / Amyuni `usbmmidd_v2` —— 虚拟显示器（熄屏挂机）

## License

MIT（见 [`LICENSE`](LICENSE)）。
