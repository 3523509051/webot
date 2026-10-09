# 微信 AGI 群聊助手（webot）

让**你自己的微信号**在群里当一个有记性、有分寸的 AI 群友：
默认闭嘴，**@ 它才出现**；被 @ 后打开一段滑动空闲窗口，在窗口内自己判断该不该接话。

> ⚠️ **先读这句**：本项目通过**读取微信本地数据库 + 驱动微信界面**来收发消息，属于**灰色用法**。
> 请用**小号**、别刷屏营销，并对账号风险有心理准备（详见文末《红线与安全》）。
> 仅供个人学习与研究使用。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **@ 才出现** | 不 @ 不插嘴。被 @ 后开 30 秒滑动窗口（默认），窗口内由 LLM 自己决定接不接话（`[PASS]` = 保持沉默） |
| **群聊记忆 L1** | 常驻滚动记录群聊背景并注入请求 —— 「群里聊了 20 条，最后 @ 它 你看看」时它接得上话 |
| **长期记忆 L3** | 窗口休眠后**异步**提炼群员画像（谁说过的偏好/事实），下次进群可用；按群隔离，可查看/清空 |
| **群知识库 L4** | 用本地嵌入（Ollama `bge-m3`）做检索，`/garemember` 手动写知识；按群隔离 |
| **碎片合并** | 「这是你」+「吗」这种分开打的短句会被合成一句话，只出一条回复 |
| **无内容消息不瞎接** | 图片/表情包/单个标点这类没有语义的消息，**从"名字"入手**判断（谁刚点过它、它刚在跟谁说话），对不上就不回 |
| **图片能看** | 图片解密成本地文件发给多模态模型；**表情包也当图片处理**（动图只取第一帧） |
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
这样不额外增加 LLM 往返（延迟只多约 1.2 秒的模型生成时间）。

---

## 前置条件（先把这些准备好）

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | **Windows 10/11 64 位** | 桥依赖微信 Windows 客户端与 UIA，Linux/macOS 跑不了 |
| 微信 | **桌面版 4.1.12+**（本项目在 **4.1.15.13** 上实测） | ⚠️ 密钥提取依赖该版本的 DLL 特征码，**换版本可能要等上游适配** |
| Python | **64 位 3.9+**（实测 3.11） | 只给桥用；AstrBot 在容器里，不需要本机 Python |
| 容器 | **Docker Desktop + WSL2** | 见下方 §0；也可用 Docker Engine（WSL2 里） |
| 内存 | 建议 **8 GB 以上** | AstrBot + 微信 + 桥约 3–5 GB；用本地嵌入（L4）再加约 2 GB |
| 模型 | 任一 **OpenAI 兼容 API**（示例 DeepSeek） | 需支持**多模态**才能看图/表情包；Key 填在 WebUI |
| 账号 | **建议专用小号** | 见《红线与安全》 |

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

### 1. 取代码

```powershell
git clone https://github.com/3523509051/webot.git c:\webot
cd c:\webot
copy .env.example .env        # 可选：改端口/时区用；LLM Key 不在这里
```

> 下面所有命令都假设仓库在 `c:\webot`。放别处也能跑，但脚本里有 `c:\webot` 的默认路径，
> 需要顺手改掉（`scripts\*.ps1`、`*.cmd`）。

### 2. 起 AstrBot

```powershell
cd c:\webot
docker compose up -d astrbot
docker compose ps
docker logs --tail 50 webot-astrbot     # 会打印面板初始账号与密码
```

- 面板：<http://localhost:6185>
- OneBot 反向 WS 端口：**6199**（桥会连这里）

### 3. 面板里配三件事

| 项 | 值 |
|---|---|
| **消息平台** | 新增 **aiocqhttp**（OneBot v11）：反向 WS 端口 `6199`，host `0.0.0.0`，token 可留空 |
| **服务提供商** | 新增你的模型（示例 DeepSeek）：`api_base = https://api.deepseek.com/v1`，模型 `deepseek-flash`，思考模式**关闭** |
| **人格 / 回复风格** | 按你的口味写；本仓库不固化人格 |

**务必保持关闭**（面板 → 群聊相关设置）：
`群聊上下文`、`群聊消息历史`、`主动回复` —— 与插件功能重复，开了会重复回复且明显变慢。

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
`[group_activation] 已加载 v0.11.0 …`

### 5. 装并启动微信桥

先确认：**微信已登录、停在目标群、没有锁屏**。

```powershell
cd c:\webot\bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe probe_db.py       # 自检 1：能否找到微信数据目录
.\.venv\Scripts\python.exe introspect.py     # 自检 2：能否读到账号/群/消息

copy config.example.json config.json         # 首次
notepad config.json                          # 必须改下面两项
.\.venv\Scripts\python.exe main.py           # 运行（或双击仓库根的 start.cmd）
```

`config.json` 里**必须改对**这两项，否则收不到 @ 或发不出去：

| 字段 | 要求 |
|---|---|
| `wechat.bot_nickname` | 机器人在群里的昵称，与微信界面**完全一致** |
| `wechat.groups` | `{"群wxid@chatroom": "群名称"}`，群名称与微信界面**完全一致** |

启动日志里应能看到这几行（缺哪行就按那行去查）：

```
微信账号: <昵称> (wxid_xxx)
图片解密密钥: ('xxxxxxxxxxxxxxxx', nnn)
监听 N 个群: ['群名', ...]
已连接 AstrBot -> ws://127.0.0.1:6199/ws
```

### 6. 验证

微信停在目标群且不锁屏，群里发：

```
@你的机器人 你好
```

数秒内应回复。机器人自己的日志（`bridge\bridge.err.log`）里能看到
`[收到]` → `[上报]` → `[发送@]` 三段。

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
> <https://github.com/IYATT-yx/NullSignal> 内附的同一份获取，放到 `tools\vdd\`。

### 8.（可选）本地知识库 L4

L4 用**本地嵌入**（Ollama `bge-m3`）做语义检索，需要额外约 2 GB 内存：

```powershell
scripts\pull_ollama_model.ps1     # 拉取 bge-m3
```

不需要长期知识库的话，把插件 `config.json` 的 `kb_enable` 设为 `false` 即可关掉
（只影响 `/garemember` 那类知识，**不影响日常聊天**）。

---

## 日常使用

```powershell
start.cmd / stop.cmd / status.cmd / restart-astr.cmd / restart-bridge.cmd
screen-off.cmd                     # 熄屏挂机
docker compose logs -f astrbot     # 框架日志
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 50   # 桥日志
```

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
3. **不要暴露到公网**：面板（6185）与 OneBot（6199）只监听本机。若需远程访问，请走内网/隧道并加认证。
4. **密钥与隐私**：`.env`、`data/`（含会话、记忆、知识库）、`bridge/config.json`、`logs/` 都已被
   `.gitignore` 忽略 —— **提交前请再 `git status` 过一眼**。聊天记录与群员画像属于隐私数据，别传上去。
5. **免责**：仅供个人学习研究。使用造成的账号限制、数据丢失等后果由使用者自负。

---

## 常见问题

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
`logs\screen-off.log` 与 `scripts\_vdd_diag.ps1`，详见 [`docs/deployment.md`](docs/deployment.md) §4.3。

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
| [`docs/wechatpadpro-notes.md`](docs/wechatpadpro-notes.md) | 备用路线：Pad 协议桥的调研与排错记录 |
| [`docs/roadmap.md`](docs/roadmap.md) | 里程碑与待办 |

---

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 聊天机器人框架（本项目的容器与 LLM 管线）
- [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) —— 微信 4.x 数据库读取与 UIA/OCR 发送
- [NullSignal](https://github.com/IYATT-yx/NullSignal) / Amyuni `usbmmidd_v2` —— 虚拟显示器（熄屏挂机）

## License

MIT（见 [`LICENSE`](LICENSE)）。
