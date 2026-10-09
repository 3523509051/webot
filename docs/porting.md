# 迁移指南：把这套东西搬到另一台 Windows（示例：Surface Pro 4）

> 结论先行：**软件层几乎全部可直接复用**（代码 + 容器 + 插件 + 表情包/碎片/引用那套逻辑都与机器无关），
> **需要重做的是三类"环境绑定量"**：① 微信版本锚点与登录态 ② 屏幕几何（UIA 校准、DPI/分辨率）
> ③ 电源与熄屏策略（S0 连接待机）。另外 4GB 内存的机器要砍掉本地嵌入（Ollama bge-m3）。

---

## 1. 可直接复用（照搬即可）

| 组件 | 说明 |
|---|---|
| `plugins/astrbot_plugin_group_activation/` | 纯 Python + 配置驱动，**零机器相关信息**。碎片合并、名字判定、图片携带、空 @ 接上下文、引用原文解析全在这一层 |
| `bridge/`（代码） | 逻辑全部可复用；依赖公开包 `wechatauto-replica`（Apache-2.0，`github.com/fanyuantaier/wechatauto-replica`，pip 可装） |
| `docker-compose.yml` + `.env.example` | AstrBot 容器化部署，跨机可用 |
| `docs/` | 设计说明与踩坑记录。里面的**实测数据**是这台机器的（群名、时间戳、分辨率），但**结论通用** |
| `scripts/fix-bom.ps1`、`webot-service.ps1` | 逻辑可复用，但**硬编码了 `c:\webot`**（全仓 23 个文件）→ 换目录要改一遍 |
| 表情包链路（zstd → cdnurl → 取第一帧） | 与机器无关，换机即用（详见 §3.2） |
| `scripts\vdd.ps1` + `screen-off.cmd` / `screen-off.ps1` | 逻辑可复用（虚拟显示器熄屏 = 本方案唯一的灭屏方式）；**驱动本体不随仓库分发**，且两个计划任务要在新机重建（见 §2.4） |
| `bridge/requirements.txt` | 已含 `wechatauto-replica>=1.2.4`、`winsdk`、`pypinyin`、`websockets` ✓ |

---

## 2. 必须重做 / 重采（环境绑定量）

### 2.1 微信版本锚点（**最大风险**）

`wechatauto-replica` 靠**扫 `weixin.dll` 特征码 + 指针链偏移**从内存里取数据库主密钥与图片密钥
（`db.py` 的 `MASTER_DLL_PATTERN`、`CFG_PTR_BACK=0x138`、`CFG_OFFSET`、`CFG_DWORD_OFF` 等，代码注释写明
"每版本需重采"）。当前这台机器的微信版本是 **4.1.15.13**。

- 新机**装同一版本微信**风险最低；
- 版本不同 → 需要重采锚点（等库更新，或自定义偏移）；
- 验证方法：跑 `python probe_db.py`，看到 `[i] 账号 = …` 说明 DB 密钥没问题；
  跑任意一张图的解密（日志 `图片解密密钥: ('…', xxx)`）说明图片密钥没问题。

### 2.2 登录态与风控

- 新机要重新扫码登录微信；
- **同一账号换设备登录可能触发风控**（要求手机确认、甚至限制登录）→ 建议**先在新机单独登录验证**，
  再决定是否把主力号切过去。别在挂机跑着的机器上直接掉登录。

### 2.3 屏幕几何：UIA 校准（发送路径的地基）

发送链路依赖「微信窗口在前台 + 输入框坐标 / 侧栏比例」，桥有自动校准（`_align_window`，含 OCR 兜底）。
Surface Pro 4 是 **12.3" 2736×1824，默认 200% 缩放**，与台式机完全不同：

- 首次发送会自动校准（日志会出现窗口校准/侧栏比例）；
- **建议固定显示缩放**（150% 或 200%）后就别再改 —— 一改，校准全部作废，`@` 会开始点错位置；
- 别把微信窗口最大化后再手动拖到非最大化状态（几何变化会触发重校准，期间发送会慢）。

### 2.4 熄屏（**只走虚拟显示器**）

**背景**：这类机器（Surface 是典型）几乎只支持 **S0 低电量待机（Modern Standby）**，而 **S0 的触发条件
就是"显示屏关闭"** → 真·关屏会让整机打盹、网卡断开、机器人静默（已实测踩到，见 `deployment.md` §4.3）。
所以**不碰电源、不关面板**，而是**把显示拓扑切到一块虚拟显示器**。

**方案**：`screen-off.cmd` → `scripts\vdd.ps1` → 切到 `usbmmidd_v2` 虚拟屏。物理屏失去信号而真黑，
而**系统层面"显示器一直开着"** → 不触发连接待机、进程不被挂起、网卡不断开。
已端到端实测（黑屏期间 Kernel-Power 506/507 零条、UIA 定位微信输入框正常、桥带 @ 发送成功）。
**这条路线与电源设置、S0 是否禁用都无关** ✓

新机上必须重做的部分（细节见 `deployment.md` §4.3）：

| 要重做 | 说明 |
|---|---|
| 装虚拟显示器驱动 | `tools\vdd\` 的驱动**不随仓库分发**（第三方样例驱动，许可与体积都不合适）→ 自己准备 `usbmmidd_v2`（Amyuni 官方样例包，或社区项目 `github.com/IYATT-yx/NullSignal` 内含的同一份），用 `scripts\vdd.ps1 -Action setup` 安装 |
| 建两个最高权限计划任务 | `vdd.ps1 -Action install-task` → `webot-vdd-enable` / `webot-vdd-disable`。驱动开关（`enableidd`）需管理员，做成任务后脚本用 `Start-ScheduledTask` 触发，**免 UAC**（否则每次熄屏弹 UAC —— 没人点就熄不了屏） |
| 切完立刻杀 `DisplaySwitch` | 切换工具不退出时会留着全屏「投影」浮层，约 15 秒后把显示配置**回退**（实测内屏自己亮回来） |
| 切换后把虚拟屏分辨率顶到 3840×2160 | 驱动默认 1024×768 会把微信窗口（1776×1380）挤出屏幕 → 输入框不可达、`@` 失效 |
| 验证驱动签名要求 | `usbmmidd.cat` 是 WHQL 签名（`Microsoft Windows Hardware Compatibility Publisher`）→ **不需要测试签名模式**。若换用别的驱动包，先查签名 |

> ℹ️ **电源设置（睡眠/合盖/电源键/屏保/Modern Standby）不属于本方案，换机也不必为熄屏改动。**
> 虚拟屏路线只切显示拓扑，不碰电源状态。曾试过的三条替代路线（背光、DPMS 面板断电、
> 禁用 Modern Standby）都已弃用，原因见 `deployment.md` §4.3《弃用过的三条路》。
>
> ⚠️ 另外 **Surface 是触摸屏**：全黑遮罩方案对触摸事件不一定生效（手指可能点在遮罩上）——
> 这也是"只走虚拟显示器"的另一个理由。

### 2.5 性能与内存（Surface Pro 4：i5-6300U / 4–8GB）

当前部署里 L4 知识库的嵌入是 **Ollama `bge-m3`**（本地模型，约 1–2GB 内存 + 明显 CPU）。
AstrBot 容器 + 微信 + 桥 + 浏览器 ≈ 3–5GB。

- **4GB 版基本跑不动**；8GB 版建议：
  - 关掉 L4 知识库（插件 `kb_enable=false`）或把嵌入换成远程 API；
  - `docker compose` 里给容器设内存上限；
  - 关掉 AstrBot 的 t2i（文字转图）等重功能。
- `bge-m3` 只用于「群公共知识库检索」，关掉只影响 `/garemember` 那类长期知识，**不影响日常聊天**。

### 2.6 微信数据目录与 OneDrive

本机的微信数据在 `…\OneDrive\文档\xwechat_files`（文档目录被 OneDrive 重定向）。同步几千个
`.dat` 附件会拖慢并可能锁文件。新机建议：**微信设置里把文件管理目录改到非 OneDrive 路径**；
`bridge/paths.py` 已能自动发现常见位置，也可用 `WECHAT_DB_DIR` 环境变量或 `config.json` 的 `db_dir` 指定。

### 2.7 配置与自启

- `bridge/config.json`：群 ID/群名映射、机器人昵称、`db_dir`；
- AstrBot：LLM provider key（当前用 `deepseek/deepseek-flash`）、嵌入 provider；
- 插件 `config.json`：`bot_names`（昵称别名）等；
- **开机自启**：本机**没配**（重启后要手动双击 `start.cmd`）。新机建议配计划任务（登录后启动 + 失败重启），
  否则一次意外重启就是"静默离线"。

---

## 3. 上传 GitHub 前必须清理

`.gitignore` 已忽略 `data/`（AstrBot 主配置 **含 API key**、会话历史、知识库、插件运行数据 L3 群员画像）
与 `.venv/`。本次又补齐了：

```
bridge/config.json     # 含群名/昵称（用 config.example.json 代替）
bridge/*.log*
bridge/cache/          # 群里发过的表情/图片帧
logs/  wechatauto_logs/
keys.json              # 微信数据库密钥缓存（兜底）
tools/vdd/             # 虚拟显示器工具，Surface 部署用不到
```

需要人工过一眼的：

- `git status` 里不该出现任何 `.env`、`*config.json`（除 `*.example.json`）、`*.log`、`data/`
- 文档里的**群名、群 ID、昵称、路径**（`docs/` 与 `README.md` 中有真实值）→ 上传前决定是否脱敏
- `bridge/config.example.json` 建议补上新增开关（`fetch_stickers` / `send_images` / `image_max_bytes`）

---

## 4. 新机上线顺序（照做即可）

1. 装 **微信 4.1.15.13** → 登录 → 打开一个群聊窗口放着
2. Python 3.11 + `pip install -r bridge\requirements.txt` → `python probe_db.py` 验证 DB/密钥
3. 项目放到 `C:\webot`（或改脚本里的路径）→ 填 `bridge\config.json`
4. 起桥 → 看日志：`微信账号: …`、`图片解密密钥: …`、`监听 N 个群: […]`、`已连接 AstrBot`
5. Docker Desktop（限内存）+ `docker compose up -d` → WebUI 配 provider key / 嵌入 → 放插件
6. 群里 @ 一次做**首次窗口校准**（第一次发送会慢，正常）
7. 电源加固（§2.4）+ 配自启（§2.7）
8. 挂机熄屏：S0 已禁用 → **直接按电源键**（已设为关闭显示器）；未禁用 → `screen-off.cmd` 遮罩

---

## 5. 一句话总结

| | 内容 |
|---|---|
| **直接搬** | 插件全部、桥的代码与依赖、容器编排、文档、表情包/引用/碎片那套逻辑 |
| **重做一遍** | 微信版本锚点与登录、UIA 校准（分辨率/DPI）、电源与熄屏、自启 |
| **按机器裁** | 4GB 就关本地嵌入（Ollama bge-m3）与 t2i；微信数据目录别放 OneDrive |
| **上传前** | 清 `data/`、`bridge/config.json`、日志与缓存（见 §3） |
