# Windows 微信桥

把**个人微信**接入 AstrBot 的桥接程序（仅 Windows）。

```
微信客户端(4.x)
  ↑ 读: WeChatDB(本地数据库增量)   ↓ 写: WeChatGUI(UIA/OCR)
本程序 (bridge/)
  ↕ OneBot v11 反向 WebSocket
AstrBot (aiocqhttp, 默认 127.0.0.1:6199)
```

---

## 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | **64 位** 3.9+（本机 3.11.4 ✅） |
| 微信 | **4.1.12+** 桌面版（本机 4.1.12.26 ✅），**必须已登录并保持运行** |
| AstrBot | 已添加 `OneBot v11`（aiocqhttp）消息平台，反向端口 `6199` |

> ⚠️ 发送依赖界面自动化，**锁屏时无法发送**；建议关闭自动锁屏。

---

## 安装

```powershell
cd c:\webot\bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 自检

```powershell
.\.venv\Scripts\python.exe probe_db.py     # 定位数据目录
.\.venv\Scripts\python.exe introspect.py   # 读取账号/群/消息
.\.venv\Scripts\python.exe probe_api.py    # 打印 WeChatDB 全部 API 签名
```

### ⚠️ 常见坑：找不到数据目录

若「文档」被 **OneDrive 重定向**（`C:\Users\<你>\OneDrive\文档`），
`wechatauto-replica` 的自动探测会失败并报：

```
RuntimeError: 未找到微信数据库目录，请通过 db_dir 参数手动指定
```

本桥已内置兼容（`paths.py` 会依次尝试标准路径与 OneDrive 路径）。
也可显式指定：

```powershell
$env:WECHAT_DB_DIR = "C:\Users\<你>\OneDrive\文档\xwechat_files"
```

或在 `config.json` 的 `wechat.db_dir` 中填写。

## 配置

首次运行会自动生成 `config.json`（模板见 `config.example.json`）：

| 字段 | 说明 |
|---|---|
| `astrbot.ws_url` | AstrBot 的 OneBot 反向 WS 地址，默认 `ws://127.0.0.1:6199/ws` |
| `astrbot.self_id` | 机器人 QQ 号（OneBot 协议需要，任意数字即可，当前 `10001`） |
| `wechat.bot_nickname` | 机器人在群里的昵称。**必须与微信界面完全一致**，否则 `@昵称` 转不成 at 段、收不到唤醒 |
| `wechat.groups` | `{"群wxid@chatroom": "群名称"}`。**群名称必须与微信界面完全一致**，发送靠它定位会话；留空则走 `group_whitelist` |
| `wechat.group_whitelist` | 按群名/群 ID 过滤要监听的群（`groups` 为空时才生效） |
| `wechat.poll_interval` | 轮询间隔（秒），当前 `0.8` |
| `wechat.verify_send` | 发送后回读数据库确认；`true` 最多多等 8 秒，当前 `false` |
| `wechat.rhythm` | 拟人节奏档位：`natural`/`calm`/`fast`/`off`，当前 **`fast`** |
| `logging.wechatauto_level` | 设为 `DEBUG` 会打出会话查找/切换细节（零运行时开销），当前 `DEBUG` |

## 运行

```powershell
.\.venv\Scripts\python.exe main.py
```

期望日志：
```
[bridge] 拟人节奏层档位: fast
[wechat] 群数量: N，昵称缓存: M
[wechat] 微信账号: xxx (wxid_xxx)
[bridge] 监听 1 个群: ['测试群']
[bridge] 水位基线已对齐
[onebot] 已连接 AstrBot -> ws://127.0.0.1:6199/ws
```

---

## 工作原理

1. 启动时用 `get_messages(limit=1)` 对齐每个群的水位基线，**避免重放历史消息**
2. 循环用 `get_new_messages(group, since_seq)` 增量拉取新消息
3. 把新消息按 OneBot v11 `message` 事件格式上报给 AstrBot
4. AstrBot 触发回复时下发 `send_group_msg`，桥调用 `WeChatGUI.send_msg()` 发送

---

## 已知限制

- **仅 Windows**：依赖 Windows 微信客户端 + UIA/OCR，**无法在 Ubuntu 运行**（跨平台见 `../docs/roadmap.md` P0）
- 发送走 UIA/OCR，窗口布局变化时可能需重新校准（`WeChatGUI.calibrate_layout()`）
- 微信版本更新可能导致库失效
- **锁屏时无法发送**（界面必须可见）
- **群名与昵称必须与微信界面完全一致**；群改名会导致每次发送都触发会话切换
- 「自己发的消息」靠「库中不带 `wxid:` 前缀」判断，微信改格式即失效
- 读数据库需从微信进程内存提取密钥，属第三方逆向行为，**存在账号风控风险**

## ⚠️ 红线

1. `rhythm` **不得改为 `off`** —— 该账号已于 **2026-09-21 触发过一次风控**（见 `wechatauto/rhythm.py` 注释）。
2. 日志中出现**两个 python 进程**（`.venv\Scripts\python.exe` 与其子进程 `C:\Python311\python.exe`）
   是 venv 启动器的正常结构，**不是重复运行**，不要误杀。
3. 图片依赖 AstrBot 侧 DeepSeek 原生视觉；**语音不做 ASR**，统一记 `[语音]` 占位。
