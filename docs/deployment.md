# 部署与运维

> 以**当前实际可用的部署方式**为准。最后同步：2026-10-07

---

## 1. 前置条件

| 项 | Windows（完整可用） | Ubuntu（部分） |
|---|---|---|
| 系统 | Windows 10/11（本机 22H2 专业版） | 22.04+ |
| 虚拟化 | BIOS 已开启（本机已开） | — |
| 容器 | Docker Desktop（需 WSL2） | Docker Engine + Compose |
| 微信 | **Windows 桌面版 4.1.12+，已登录并保持运行** | ❌ 无可用桥 |
| Python（桥） | **64 位** 3.9+（本机 3.11.4） | ❌ 不适用 |
| AstrBot | 容器内，无需本机 Python | 同左 |

---

## 2. Windows 完整部署

### 2.1 装 Docker Desktop（含 WSL2）

管理员 PowerShell：

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
```

两条都应返回 `RestartNeeded : True` → **重启整台电脑**。
（**只启用 WSL 主体是不够的**，`VirtualMachinePlatform` 是独立功能，漏掉会让 Docker 报
`Virtual Machine Platform not enabled`。）

然后：

```powershell
winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
```

重启 → 启动 Docker Desktop → 等到 `docker info` 能输出。

### 2.2 起 AstrBot

```powershell
cd c:\webot
docker compose up -d astrbot
docker compose ps
docker logs --tail 50 webot-astrbot     # 里面会打印面板初始账号与密码
```

- 面板：<http://localhost:6185>
- OneBot 反向 WS 端口：**6199**

> `docker-compose.yml` 现在**只有 `astrbot` 一个服务**（早期 Pad 路线那三个容器已删除，
> 见 §5）。`.env` 里只需时区与端口，**LLM 的 Key 在面板里配**。

### 2.3 面板配置（三件事）

| 项 | 值 |
|---|---|
| 消息平台 | 添加 **aiocqhttp**（OneBot v11），反向 WS 端口 `6199`，host `0.0.0.0`，token 可留空 |
| LLM Provider | **DeepSeek**：`api_base = https://api.deepseek.com/v1`，模型 **`deepseek-flash`** |
| 人格 / 回复风格 | 在面板配置（本仓库未固化） |

**务必保持关闭**（面板 → 群聊相关设置）：
`群聊上下文（group_icl_enable）`、`群聊消息历史`、`主动回复（active_reply）`
—— 与插件功能重叠，开了会重复且显著增加延迟。

### 2.4 插件挂载（关键，别踩）

`docker-compose.yml` 的 astrbot 服务必须包含：

```yaml
    volumes:
      - ./data/astrbot:/AstrBot/data
      - ./plugins:/AstrBot/data/plugins     # ← 缺了这行，改 ./plugins 下的代码不会生效
```

验证容器内读到的是新版本：

```powershell
docker exec webot-astrbot cat /AstrBot/data/plugins/astrbot_plugin_group_activation/metadata.yaml
```

### 2.5 跑桥

```powershell
cd c:\webot\bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 自检（确认能找到微信数据目录、能读到账号/群/消息）
.\.venv\Scripts\python.exe probe_db.py
.\.venv\Scripts\python.exe introspect.py

# 运行
.\.venv\Scripts\python.exe main.py
```

`config.json` 必须改对这两项：

| 字段 | 说明 |
|---|---|
| `wechat.bot_nickname` | 机器人在群里的昵称，**必须与微信群内显示一致**（用于把 `@昵称` 转成 at 段） |
| `wechat.groups` | `{"群wxid@chatroom": "群名称"}`，**群名称必须与微信界面完全一致**（发送靠它定位会话） |

期望日志：

```
[bridge] 监听 1 个群: ['测试群']
[bridge] 水位基线已对齐
[onebot] 已连接 AstrBot -> ws://127.0.0.1:6199/ws
```

### 2.6 验证

1. 微信**停在目标群**、**不锁屏**
2. 群里发 `@机器人 你好`
3. 预期：几秒内回复；桥日志出现 `[收到]`/`[上报]`/`[发送]`；AstrBot 日志出现插件的
   「窗口打开」与「已注入 L1 背景」

---

## 3. Ubuntu 部署现状

| 组件 | 能否部署 |
|---|---|
| Docker + AstrBot + 插件 | ✅（`docker compose up -d astrbot`） |
| 本项目的桥 | ❌ **不能** —— 依赖 Windows 微信客户端 + UIA/OCR |

要在 Ubuntu 跑通，必须**另做一条桥**，只需对齐同一个接口：
`OneBot v11 反向 WS → ws://<host>:6199/ws`，实现 `send_group_msg` 与消息上报即可。
**AstrBot 侧与插件无需任何改动**（这是当初选 OneBot 协议做解耦的目的）。

候选路线见 `tech-stack.md` §3。

---

## 4. 日常运维

### 4.1 一键启动 / 停止（推荐，重启电脑后就用它）

| 双击这个文件 | 作用 |
|---|---|
| **`start.cmd`** | 启动（或重启）全部服务：拉起 Docker Desktop → 起 AstrBot → 起微信桥，最后打印状态 |
| `stop.cmd` | 停掉微信桥与全部容器（Docker Desktop 保持运行） |
| `status.cmd` | 只看状态，不改动任何东西 |
| **`restart-bridge.cmd`** | **只重启微信桥**（不碰 AstrBot/Docker）。用途：改了 `bridge/config.json` 或桥代码、**新进了群**（群列表只在桥启动时扫描） |
| **`restart-astr.cmd`** | **只重启 AstrBot**（桥自动重连，不用管）。用途：人格在 WebUI 之外被改、主配置 `cmd_config.json` 变更、插件代码更新。⚠️ 直接改数据库里的人格需要它（内存缓存）；WebUI 里改会自动刷新，不需要 |
| **`screen-off.cmd`** | **熄屏（真黑）但继续跑**（夜里挂机用）。切到虚拟显示器 → 物理屏断电真黑，程序与 AI 操控照跑；真人碰一下鼠标/键盘即恢复。详见 §4.3 |
| `scripts\vdd.ps1` | 虚拟显示器驱动管理：`-Action status / setup / enable / disable / teardown`（装/卸驱动、开关虚拟屏）。详见 §4.3 |


底层是 `scripts\webot-service.ps1`，也可以带参数直接跑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 stop
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 restart
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 bridge   # 只重启桥
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 astr     # 只重启 AstrBot
```

脚本做的事：确保 Docker 引擎在跑（没跑就拉起 Desktop 并等就绪）→ `docker compose up -d --no-deps astrbot`
→ 等 OneBot 适配器就绪 → 停掉残留桥进程并重新拉起 → 等桥连上 → 打印状态汇总。

> ⚠️ 改 `webot-service.ps1` 后**必须保证它是 UTF-8 with BOM**。
> Windows PowerShell 5.1 对**无 BOM** 的 `.ps1` 会按系统 ANSI(GBK) 解码，中文会全部乱码。
> 用编辑器另存为「UTF-8 带 BOM」；或者改完跑一句自动补上：
> `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-bom.ps1`

### 4.2 手工命令

| 操作 | 命令 |
|---|---|
| 起 AstrBot | `docker compose up -d --no-deps astrbot` |
| 停 AstrBot | `docker compose stop astrbot` |
| 看 AstrBot 日志 | `docker logs -f webot-astrbot` |
| 重启 AstrBot（改插件后） | `docker compose restart astrbot` |
| 看桥日志 | `Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 50` |
| 跑桥 | `c:\webot\bridge\.venv\Scripts\python.exe c:\webot\bridge\main.py` |
| 停桥 | 结束对应 `python.exe`（命令行含 `main.py`）进程 |
| 查窗口状态（群内） | 发 `/gaw`；看注入内容发 `/gactx`；强制休眠发 `/gasleep` |

> 桥在 Windows 上会呈现**两个进程**（`.venv\Scripts\python.exe` 与其子进程
> `C:\Python311\python.exe`），这是 venv 启动器的正常行为，**不是重复运行**。

### 4.3 夜里熄屏继续跑（不锁屏、不睡眠、不触发连接待机）

双击 **`screen-off.cmd`** → 体检（桥 / AstrBot 在跑吗）→ **虚拟屏熄灭（物理屏真黑：面板断电、背光灭）**
→ 碰一下鼠标或键盘即恢复。日志：`c:\webot\logs\screen-off.log`。

**原理一句话**：把显示拓扑切到一块虚拟显示器（`usbmmidd_v2`），物理屏失去信号而真黑；
系统层面「显示器一直开着」→ **不触发连接待机**，进程不被 DAM 挂起、网卡不断开。

**2026-10-09 端到端实测（全绿）**：

| 检查项 | 结果 |
|---|---|
| 物理屏黑着期间 Kernel-Power 506/507/172 | **0 条**（没进连接待机、没断网） |
| UIA 定位微信窗口与输入框（`ChatInputField`） | ✅ 找到，`IsOffscreen=False` |
| 输入框点击 | ✅ 0.76s |
| **桥发带 @ 的消息** | ✅ `[发送@] 测试群 -> 小泽布尔（UIA 4.78s, 路径=direct, @=成）` |
| `/internal` 恢复 | ✅ 内屏恢复为主显示器 |

**踩过的坑与根因（2026-10-09 逐条实测得出，都已写进代码）**：

| 现象 | 根因 | 处理 |
|---|---|---|
| 熄屏后 **15~40 秒内屏自己亮回来** | **`DisplaySwitch.exe` 切换完不退出**，留着全屏 `Shell_LightDismissOverlay`（窗口名「关闭」）+「投影」面板；面板约 15 秒后自动消失时**把显示配置回退**了（10:01 / 10:03 / 10:05 三次复现，`_vdd_test5` 对照实验证实） | 切换后 **`Stop-Process -Name DisplaySwitch -Force`** → 实测黑屏连续 103 秒零回退 |
| 虚拟屏「未挂载」导致脚本降级成遮罩 | 挂载是 **`/external` 这个动作本身**触发的（`_vdd_step.ps1` 实测：单独 `enableidd` 循环不挂载，一次 `/external` 就把 DISPLAY14 挂成 3840×2160 主屏） | 改成**先切、后检查**；`enableidd` 循环只作兜底 |
| 切过去后 @ 失败、降级纯文本 | 虚拟屏用驱动默认 **1024×768**，微信窗口（1776×1380）被挤出屏幕 → 输入框不可达 | 切换后**必须把分辨率顶到 3840×2160** |
| 换分辨率到内屏原生值 | 驱动模式表**编译在驱动里**（固定 9 档，最高 3840×2160，**不含 2736×1824**）；往注册表 `Parameters\Monitors` 加，`reg add` 成功但驱动不读 | 放弃这条路，用 3840×2160（窗口放得下，几何等价） |
| 怀疑网易 GameViewer 服务改回显示配置 | **排除**：停掉 `GameViewerService` 照样回退（`_vdd_test2`） | 仍留了 `webot-gv-stop/start` 两个任务备用 |
| 每次熄屏都弹 UAC，没人点就静默降级 | `enableidd` 需管理员 | 建了**最高权限计划任务**（`vdd.ps1 -Action install-task`）：`webot-vdd-enable/disable`，脚本用 `Start-ScheduledTask` 触发，**免 UAC** |

**兜底看门狗**：等待真人输入期间每 2 秒检查一次「内屏是否又被接上」，是就立刻重新切黑屏，
并把每次发生写进日志（`screen-off.log` 的「⚠️ 内屏被拉回」）——既是保险，也是证据。

**稳定性实测（脚本本体）**：`screen-off.ps1 -Seconds 90` → 10:12:33 熄屏 → 10:14:16 恢复，
**全程 103 秒零回退**；期间桥 / AstrBot / 微信全部在线。

#### ⚠️ 铁律：屏幕动作必须**排在消息发送之后**

踩到的坑（2026-10-09 10:36）：`/gaoff` 触发熄屏的同时，机器人那条"已熄屏"的回复正在发送 ——
显示拓扑切换让微信窗口的分辨率/DPI 变化、布局缓存失效，输入框探测**连续失败 6 次**并触发一次
完整 OCR 重校准，**那一条发送被拖了 145.46 秒**（正常是 1.5~2.7s）。

所以：**绝不能让「切显示拓扑」与「发送消息」重叠**。实现上做了三件事：

1. 命令（`/gaon` `/gaoff`）**先回消息、再切屏幕**；
2. 被 @ 自动开屏：`wake_screen_on_at` —— **已按用户要求默认关闭**（每次 @ 都切屏本身
   就是慢发送的隐患，且群里别人 @ 也会亮屏）。代码保留，放到
   `@filter.after_message_sent()` 钩子里（回复先出去）；要重新打开就把
   `config.json` 的 `wake_screen_on_at` 改成 `true`。启动日志会打印当前值：`｜ 被@自动开屏=False`；
3. 桥在切换后**后台预热布局**（`_refresh_layout_after_switch`，等 14 秒再探一次输入框），
   把重校准开销从"下一条要发的消息"挪走。

**升级插件/桥后请确认**：插件日志 `已加载 v0.7.0`、桥日志 `已连接 AstrBot`。

**驱动可信度（已查验）**：`usbmmidd.cat` 签名 Valid（`Microsoft Windows Hardware Compatibility
Publisher` = WHQL）→ **不需要测试签名模式**；与 Amyuni 官方包 SHA-256 **逐字节一致**；
UMDF 用户态间接显示驱动（非内核模式驱动）。
管理：`scripts\vdd.ps1 -Action status|setup|enable|disable|teardown`（需管理员的动作会自动弹 UAC）。

**只有真人输入才恢复**：低级钩子过滤带 `INJECTED` 的合成输入 —— 机器人自己的点击 / 回车不会误唤醒
（实测：一次 @ 发送期间过滤掉 2 个注入事件，屏幕没亮）。

❌ **别按物理电源键熄屏**：电源键 / 睡眠键已从「关闭显示器」改回 **`000 = 不采取任何操作`** ——
那条是 DPMS 路线，会让整机进连接待机（下面有证据），机器人静默。

> 已弃用两条路：① 调背光（哪怕调到 0，面板仍发光，不算黑屏）；
> ② 直接面板断电（DPMS）—— 本机只支持 S0 待机，而 S0 由「显示屏关闭」触发，一关就进连接待机。

#### ⚠️ 为什么不能真·关显示器（2026-10-08 踩到）

本机 `powercfg /a` 显示**只支持 S0 低电量待机（Modern Standby / 连接待机）**，固件不支持 S3。
而 S0 的触发条件就是「显示屏关闭」—— 广播一次 `SC_MONITORPOWER` 等于让整机进连接待机：
桌面进程被 **DAM 挂起**、网卡断开。症状是**熄屏期间机器人不回消息，亮屏登录后才把积压的消息处理掉**。

实测证据（事件日志 + 桥日志）：

```
23:56:02  Kernel-Power 506  系统正在进入连接待机状态
23:56:13  Kernel-Power 172  备用连接状态: Disconnected，原因: 7
23:57:10  Kernel-Power 507  系统正在退出连接待机状态
桥日志：23:47:19 → 23:57:17  断层 598 秒
```

#### 弃用过的三条路（留档，别再走）

| 路线 | 为什么弃用 |
|---|---|
| 调背光 | 背光调到 0 面板仍在发光，**不算黑屏** |
| 真·面板断电（DPMS / 把电源键设成"关闭显示器"） | 本机只支持 **S0 低电量待机**，而 S0 的触发条件就是「显示屏关闭」→ 一关就进连接待机：进程被 DAM 挂起、网卡断开、机器人静默 |
| 禁用 Modern Standby（`PlatformAoAcOverride=0`） | 试过：写了注册表但**一直没生效**（快速启动把内核会话从 hiberfil 恢复，开机才读的项不会重读；必须关快速启动 + 用「开始菜单 → 重启」）；而且**虚拟屏方案根本不需要它** → 相关脚本与文档已全部删除 |

**现在只有一条路：虚拟显示器（本节开头那条）。它不依赖上面任何电源改动。**

**顺手纠正一个误判**：那次"重启"把微信和桥一起带走了（它们不随系统自启），
所以"加了虚拟屏之后微信找不到了"其实是**它们根本没在跑** —— 双击 `start.cmd` 拉起即可。

**为什么"AI 操控"在虚拟屏下仍然可用**（逐条对应本项目的链路）：

| 环节 | 依据 |
|---|---|
| UIA 元素定位 | 不依赖画面亮度/可见性，只看窗口树 → 照常 |
| 坐标点击 | `uiautomation` 用 `GetSystemMetrics(SM_CXSCREEN/SM_CYSCREEN)`（**主显示器**）+ `MOUSEEVENTF_ABSOLUTE`（不带 `VIRTUALDESK`）→ 只要虚拟屏成为**唯一/主**显示器，坐标基准就是一致的（**必须用 `/external` 全切**；只"扩展"会错位） |
| OCR 路径（`wechatauto.guia` 是「坐标+OCR」模块） | 截图抓的是**桌面渲染结果**，虚拟屏是真实渲染目标 → 截图与 OCR 照常（这与"面板断电"不同，后者也照常；两者都不影响渲染） |
| 粘贴 / 发送 / @ | 剪贴板 + Ctrl+V / SendKeys / 点击，与显示无关 |
| 窗口几何 | 会变（内屏 2736×1824 → 虚拟屏如 1920×1080）。桥的布局是**相对比例**（`SIDEBAR_RATIO`/`SEND_BUTTON_RATIO`…）且输入框探测失败会**自动重校准** → 理论可自适应，**但必须实测，尤其是"切群发送"（校准飘了可能发错群）** |

**驱动可信度（2026-10-09 实测查验）**：

| 检查 | 结果 |
|---|---|
| `usbmmidd.cat` 签名 | **Valid**，签名者 `Microsoft Windows Hardware Compatibility Publisher` → **WHQL 签名，不需要测试签名模式** |
| NullSignal 自带的驱动 vs Amyuni 官方包 | `usbmmidd.cat` / `usbmmIdd.inf` / `deviceinstaller64.exe` / `x64\usbmmIdd.dll` **SHA-256 逐一相同** → 无夹带 |
| 遗留风险 | ① 装驱动需要**管理员**（`deviceinstaller*.exe` 本身无签名，只是安装小工具）；② **强杀残留**：黑屏期间若进程被杀，虚拟屏可能一直启用、物理屏持续无输出（恢复：`deviceinstaller64.exe enableidd 0`，或设备管理器卸载/重启）；③ NullSignal 自己的唤醒是"光标位移 >5px"轮询 —— 本项目**不采用**它，改用已验证的低级钩子（过滤注入事件，机器人自己点击不会误唤醒） |

包已下载到 `c:\webot\tools\vdd\`（`amyuni\` 与 `nullsignal\` 两份，已解包）。
安装/启用/恢复与我们的唤醒逻辑集成见 `scripts\screen-off.ps1` 的第三种模式（★ 待实测后启用）。

**只有真人输入才恢复**：低级钩子（`WH_MOUSE_LL` / `WH_KEYBOARD_LL`）过滤带 `INJECTED` 的合成输入
（`_probe_hook.ps1` 实测：`SetCursorPos` 不产生事件、`mouse_event` 带 `injected=True` 被过滤）——
所以机器人自己的点击/回车**不会**把屏幕弄亮。

参数：

```powershell
screen-off.cmd                     # 双击：体检 → 切虚拟屏（物理屏真黑）
scripts\screen-off.ps1             # 同上
scripts\screen-off.ps1 -Restore    # 立刻恢复（切回内屏）
scripts\screen-off.ps1 -NoOff      # 只体检，不熄屏
scripts\screen-off.ps1 -Seconds 20 # 测试：20 秒后自动恢复
```

保险机制：① 熄屏期间每 2 秒检查「内屏是否又被接上」，是就立刻重新切黑屏（并记日志
`⚠️ 内屏被拉回`）；② 最多黑 10 小时自动恢复；③ 再次双击 `screen-off.cmd` = 请求恢复（即"开关"）；
④ 驱动包缺失或切换失败时**明确报错并退出**（不会静默变成别的东西）。

复现/诊断命令：`_diag_screen.ps1`（是否进连接待机）、`_vdd_diag.ps1`、`_probe_hook.ps1`（钩子与注入标志）。

> ℹ️ **电源设置不属于本方案**，也不必为熄屏改动任何电源选项：虚拟屏路线只切**显示拓扑**，
> 不碰电源状态。（"别让系统自己睡眠/休眠"是另一件事、与熄屏脚本无关。）

❌ **三件绝对不要做的事**（都会让机器人静默下线，且外表看不出异常）：

1. **锁屏**（Win+L）或让屏保恢复时需要登录 —— 锁屏切到安全桌面，SendKeys / 点击全部失效
2. **注销 / 切换用户 / 断开远程桌面**（RDP 断开 = 会话锁定，同样失效）
3. **让笔记本跑电池** —— 电量耗尽就关机。要么插着电，要么接受它随时掉线

⚠️ **重启电脑 = 机器人离线**：目前**没有配开机自启**，重启后要手动双击 `start.cmd`。
另外 Windows 更新装完可能自动重启（设置 → Windows 更新 → 使用时间 / 暂停更新），
夜里挂机前值得留意。

---

## 5. 关于「闲置服务」：已清理

早期调研「Pad 协议桥」路线时，`docker-compose.yml` 里曾同时放着 `wechatpadpro` / `mysql` / `redis`
三个容器（还带 `restart: always`、`astrbot` 上的 `depends_on`、`.env` 里的数据库密码）。
**当前链路完全不用它们**，而且会带来三个副作用：每次开机被自动拉起、`up` 需要 `--no-deps` 才不连带、
新人照着 compose 会以为要装 MySQL。

现已**从 `docker-compose.yml` 中删除**：compose 只剩 `astrbot` 一个服务，`.env` 也只留时区与端口。
（那三个服务的原始定义保留在 `docs/wechatpadpro-notes.md`，将来真要试 Pad 路线时照那份起即可。）

---

## 6. 排障

| 现象 | 原因与处理 |
|---|---|
| `docker info` 报 500 / GUI 卡在 Starting | 缺 `VirtualMachinePlatform` 或 WSL 未装；按 §2.1 启用后**重启整机** |
| 桥连不上 AstrBot（`did not receive a valid HTTP response`） | AstrBot 容器未就绪或正在重启；等它起来后桥会自动重连（每 5s 一次） |
| 改了插件代码但行为不变 | 没挂载 `./plugins`（见 §2.4），或没 `restart astrbot` |
| 桥报「未找到微信数据库目录」 | 「文档」被 OneDrive 重定向；`paths.py` 已兜底，仍失败则设 `WECHAT_DB_DIR` 或在 `config.json` 填 `wechat.db_dir` |
| 机器人不回复 | 群名与 `config.json` 不一致；或微信被锁屏；或消息类型不在已打通的通道内（图片/表情包已支持，其它类型跳过） |
| 回复很慢 | 见 `latency-diagnosis.md`：主要是 UI 键入与发送闸门，不是 LLM |
| 每条消息回两次 | 检查是否同时有多个桥实例在跑（正常只有 venv 启动器 + 其子进程这一对） |
| AstrBot 日志刷 `get_group_member_info` 相关告警 | 本桥未实现该 action，属预期，不影响功能 |

---

## 7. 安全提醒

- `.env` 中的 `ADMIN_KEY`、数据库密码**必须改成强随机值**，不要照抄示例。
- AstrBot 面板与桥的接口不要暴露到公网。
- 本方案读取微信本地数据库密钥并驱动界面，**属灰色行为，存在账号风控风险**；
  请使用小号，不要用于营销刷屏，并保持 `rhythm=fast`（**不得改 `off`**）。

---

## 8. 本地知识库（Ollama 嵌入）

### 为什么必须单独配嵌入模型

AstrBot 自带知识库功能（`kb.db` 启动时自动初始化），但**必须有一个嵌入（embedding）Provider** 才能用 ——
**DeepSeek 只有 chat 接口，不能做嵌入**。本项目用**本机 Ollama + bge-m3**：零成本、数据不出本机。

### 已完成配置

| 项 | 值 |
|---|---|
| Ollama | `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`，监听 `127.0.0.1:11434` |
| 嵌入模型 | `bge-m3`（多语言，**1024 维**） |
| AstrBot Provider | `provider_sources` 中的 `ollama_embedding` 条目 |
| ⚠️ 地址 | **`http://host.docker.internal:11434`** —— 不能用 `localhost`，那指向容器自己 |

配置由 `scripts/add_embedding_provider.py` 幂等写入（自动备份 `cmd_config.json`）。
**必须先停容器再写**，否则 AstrBot 会回写覆盖：

```powershell
docker compose stop astrbot
c:\webot\bridge\.venv\Scripts\python.exe c:\webot\scripts\add_embedding_provider.py "c:\webot\data\astrbot\cmd_config.json"
docker compose start astrbot
```

### 坑：代理的 fake-IP 会让 `ollama pull` 失败

```
Error: redirect target not allowed: <...>.r2.cloudflarestorage.com resolves to non-public 198.18.0.21
```

代理软件（Clash/Mihomo 类）的 **fake-IP** 模式把 `registry.ollama.ai` 与
`*.cloudflarestorage.com` 解析成 `198.18.x.x` 保留地址，而 **Ollama 有 SSRF 防护、拒绝跟随**。

两种解法（任选其一）：

1. **给这两个域名加直连规则**（推荐）：
   ```yaml
   rules:
     - DOMAIN-SUFFIX,ollama.ai,DIRECT
     - DOMAIN-SUFFIX,cloudflarestorage.com,DIRECT
   ```
2. **用脚本手动下载**（不改代理）：`scripts/pull_ollama_model.ps1`
   用 curl 取 manifest 与 blob（curl 无 SSRF 检查），直接落盘到 `%USERPROFILE%\.ollama\models\`，
   支持断点续传，可重复执行。

> 同一类问题也曾拦过 WeChatPadPro 的授权服务，见 `wechatpadpro-notes.md`。

### 验证嵌入链路

用 AstrBot 自己的 Provider 类实调一次（比只看配置可靠）：

```powershell
docker cp <脚本> webot-astrbot:/tmp/ ; docker exec webot-astrbot python3 /tmp/<脚本>
```

实测 `bge-m3` 返回 **1024 维**向量、批量调用正常。

### 还差最后一步：建集合 + 传文档

知识库集合要在 AstrBot 面板创建（或调 `POST /api/knowledge-bases`，需面板登录态）。
**创建时会用 `embedding_provider_id` 做一次真实嵌入校验**，所以顺序不能反：
**先有嵌入 Provider，才能建集合。**

> 面板账号是 `astrbot`，密码仅在**首次启动**日志中打印过，且以哈希存储、无法还原。
> 若已遗忘，可在面板登录页走重置流程，或删除 `cmd_config.json` 中 `dashboard` 段重新初始化。

### 两种工作模式（`kb_agentic_mode`）

知识库接入 LLM 有两种模式，由 `astr_main_agent.py::_apply_kb` 决定：

| 值 | 行为 | 延迟影响 |
|---|---|---|
| **`false`**（当前默认） | **每次 LLM 请求前自动检索**：拿当前消息当 query，结果以 `[Related Knowledge Base Results]` 塞进 `extra_user_content_parts`，并 `mark_as_temp()`（临时，不进历史） | ⚠️ **每条消息都多一次嵌入 + 检索** |
| `true` | 不自动检索，把 `KnowledgeBaseQueryTool` 加进 `req.func_tool`，**由模型自己决定要不要查** | 只在模型认为需要时检索 |

> **本项目建议用 `true`**：群聊对延迟敏感（见 `latency-diagnosis.md`），
> 自动检索会给每条被放行的消息都加一次嵌入调用。等知识库有内容后再切。

### 检索的生效条件

`retrieve_knowledge_base()` 的判定顺序：

1. **会话级配置优先**：`kb_config.kb_ids`（为空列表＝该会话显式不用知识库），`top_k` 也来自这里
2. 否则用**全局** `kb_names` + `kb_final_top_k`（默认 5）
3. `kb_names` 为空 → 直接返回，不检索
4. 配置的库**全为空**（`doc_count == 0 且 chunk_count == 0`）→ 跳过检索

所以「建了集合但没传文档」等于没建。

### 按群聊隔离知识库（原生支持）

AstrBot 的会话级可配置项里就包含知识库：

```python
AVAILABLE_SESSION_RULE_KEYS = [
    "session_service_config",
    "session_plugin_config",
    "kb_config",          # ← 会话级知识库绑定
    f"provider_perf_{ProviderType.CHAT_COMPLETION.value}",
    ...
]
```

- 每个会话（umo，即每个群）可存 `kb_config = {"kb_ids": [...], "top_k": N}`
- 优先级：**会话级 `kb_ids` > 全局 `kb_names`**
- `kb_ids: []`（空列表）＝ **该群显式禁用知识库**
- 设置入口：面板「会话管理」页（`/api/sessions`）

→ 所以「A 群用 KB1、B 群用 KB2、C 群不用」是**开箱支持**的。

### 注意：AstrBot 原生知识库**不能**从对话自动学习（但插件已经补上了）

AstrBot 的知识库是**文档驱动**的：上传文档 → 分块 → 嵌入 → 存入 FAISS。
**原生没有任何「从聊天自动积累」的机制**；更新只能重新导入文档。

本项目的做法（v0.6.0，`kb.py`）：插件在窗口休眠后的提炼里**用同一次 LLM
调用**顺带产出「群公共知识」，经语义查重（`kb_dedup_score=0.90`）与每日额度
（20 条/天）后调 `upload_document(pre_chunked_text=...)` 写入**每群一个**的知识库；
回复前由插件直查直注（不用 `kb_agentic_mode`，避免工具往返延迟）。
详见 `context-design.md` §6。

而「记住某群员的性格爱好」这类**会变化的结构化事实**，不走向量库
（向量检索适合语义相似，不适合按人精确查属性，且更新/遗忘困难、难以天然按群隔离）——
那是 **L3 结构化记忆**（`{群 → 人 → 属性}`，JSON 存储，按群注入）的职责。
