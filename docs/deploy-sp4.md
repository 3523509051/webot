# Surface Pro 4 部署 Runbook（写给执行部署的 AI agent）

> **读者**：负责在这台 Surface Pro 4 上从零部署本项目的 **AI agent**。
> **用法**：按阶段顺序执行。每个阶段都有【目标 / 前置 / 步骤 / 验收】；**验收不过就停在原地诊断，
> 不要跳步、不要"先继续后面再说"**。所有命令在 **PowerShell** 里执行（标注"管理员"的需提权）。
>
> 相关文档：[`porting.md`](porting.md)（为什么有些东西必须重做）、[`deployment.md`](deployment.md)（通用部署与排障）、
> [`architecture.md`](architecture.md)（链路细节）。本文件是 SP4 专属的**执行清单**。

---

## 0. 执行纪律（先读，贯穿全程）

### 0.1 逐条验收
每条命令执行后，核对**退出码**与本文给出的**关键输出行**。输出对不上 → 进入该阶段的"诊断"小节，
拿到结论后再继续。宁可停下来向用户报告，也不要带着异常往前走。

### 0.2 必须由"人"完成的动作（agent 无法代办，要提前告知用户）

| 动作 | 为什么 |
|---|---|
| **微信扫码登录** | 账号凭据只能人来输入；且建议用**小号**（见红线） |
| **UAC 弹窗点"是"** | 装 Docker、装驱动、建计划任务都会弹 |
| **重启电脑** | 启用 WSL2 功能后必须整机重启 |
| **把微信窗口停在目标群** | 桥按"群名称"定位会话 |
| **面板里填 LLM API Key** | 密钥不进仓库、不进配置文件 |

### 0.3 禁止事项（做任何一步之前都记住）

1. **不要改电源设置**（`powercfg`、注册表 `PlatformAoAcOverride`、屏保、按钮动作……一律不碰）。
   本项目的熄屏走**虚拟显示器**路线，与电源状态无关；历史上三条电源路线全部弃用（见 `deployment.md` §4.3）。
2. **不要 `git push --force`**；提交用命令级身份（见 §3.3）。
3. **不要从旧机器整目录拷贝 `c:\webot`** —— 那会带来 `data/`（会话、记忆、知识库、API Key）、
   日志、`bridge/config.json` 等敏感/机器相关文件。**只在 SP4 上 `git clone`**，配置从 `*.example.json` 现配。
4. 不要把桥的拟人节奏 `rhythm` 改成 `off`（风控风险，见 README《红线与安全》）。
5. 改过任何 `.ps1` 之后必须跑：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-bom.ps1`
   （Windows PowerShell 5.1 对无 BOM 的文件按 GBK 解码，中文会乱）。
6. 面板（6185）与 OneBot（6199）**不要暴露公网**。

### 0.4 环境差异注意

- 中文 Windows 的日志文件要用 `-Encoding Default` 读（按 UTF-8 读会乱码）。
- `docker compose` 的进度输出走 **stderr** —— 在脚本里执行时套一层 `cmd /c "... 2>&1"`，
  直接执行会被 PowerShell 当成 `NativeCommandError` 刷红字（不影响结果）。
- SP4 若是 4GB 内存版：**装完基础链路后直接关掉 L4 知识库**（§7.1），不要尝试跑本地嵌入。

### 0.5 机器档案（部署前先确认）

| 项 | 期望 | 怎么查 |
|---|---|---|
| 型号/内存 | Surface Pro 4（i5-6300U 一类），**≥8GB** 才建议跑 L4 | `systeminfo`、任务管理器 |
| 系统 | Windows 10/11 64 位 | `winver` |
| 屏幕 | 12.3" **2736×1824（3:2）**，默认缩放 200% | 设置 → 屏幕 |
| 网络 | 能访问 GitHub / PyPI / Docker Hub / 模型 API | `curl.exe -I https://github.com` |
| 微信账号 | **专用小号**，已进目标群 | — |

**显示缩放定下来就别再改**（150%/200% 都行）：桥的窗口校准与缩放相关，中途改缩放 = 校准作废、`@` 点错位置。

---

## 阶段一：系统与微信

**目标**：微信桌面版登录可用、数据目录可定位、目录结构就绪。

1. **暂停 Windows 更新的自动重启**（设置 → Windows 更新 → 使用时间/暂停更新）。
   重启 = 机器人离线（§7.2 有自启方案）。
2. **装微信桌面版**。版本要求 **≥ 4.1.12**；旧机器实测版本为 **4.1.15.13** ——
   **优先装完全相同的版本**（密钥提取依赖 `weixin.dll` 特征码与偏移，版本变了可能要等上游适配）。
   装完由用户扫码登录，**打开目标群的聊天窗口**。
3. （推荐）把微信的**文件管理目录移出 OneDrive**：微信 → 设置 → 文件管理 → 改到
   `C:\WeChatFiles` 之类非同步目录。留在 OneDrive 里会被同步成千上万个 `.dat`，拖慢且可能锁文件。
4. 建目录并克隆仓库（**强调：clone，不要拷贝旧机器目录**）：

   ```powershell
   git clone https://github.com/3523509051/webot.git C:\webot
   cd C:\webot
   copy .env.example .env      # 可选：只含时区与端口；LLM Key 不在这里
   ```

   > 仓库默认路径是 `C:\webot`（部分脚本按它写死）。放在别处也能跑，但要顺手改
   > `scripts\*.ps1` 与根目录 `*.cmd` 里的路径。

**验收**

```powershell
Test-Path C:\webot\docker-compose.yml                 # True
Test-Path C:\webot\tools\vdd                          # False（驱动还没放，正常）
Get-Process Weixin -ErrorAction SilentlyContinue      # 能看到微信进程
```

---

## 阶段二：WSL2 + Docker Desktop

**目标**：`docker info` 正常、`docker compose version` 有输出。

1. **管理员** PowerShell：

   ```powershell
   Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart
   Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
   ```

   两条都应返回 `RestartNeeded : True`。**只启用 WSL 主体不够**，`VirtualMachinePlatform` 是独立功能。
2. **重启整机**（告知用户）。
3. 装 Docker Desktop：

   ```powershell
   winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
   ```

   （没有 winget → 到 docker.com 下安装包手动装。）装完启动 Docker Desktop，等它就绪。
4. 验证：`docker info`（应输出 Server Version 等一大段）、`docker compose version`。

**诊断**：`docker info` 报 500/空 → 回到第 1 步（多半缺 `VirtualMachinePlatform`）；
仍不行 → `wsl --update` 后再试。SP4 是 2015 年的机器，若 WSL2 起不来，进 BIOS 确认 VT-x（要人操作）。

---

## 阶段三：AstrBot（容器）与面板

**目标**：`webot-astrbot` 容器在跑，面板能登录，插件已被容器读到。

1. 起容器：

   ```powershell
   cd C:\webot
   docker compose up -d astrbot
   docker compose ps                       # 应看到 webot-astrbot 为 running
   docker logs --tail 50 webot-astrbot     # 记下面板的初始账号与密码（告诉用户）
   ```

   > compose 里**只有 astrbot 一个服务**（早期的 wechatpadpro/mysql/redis 已删除，
   > 其定义留档在 `docs/wechatpadpro-notes.md`，本阶段用不到）。
2. 面板 <http://localhost:6185> 配三件事（人来操作，agent 给出指引）：
   - **消息平台** → 新增 **aiocqhttp**（OneBot v11）：反向 WS 端口 `6199`，host `0.0.0.0`，token 留空
   - **服务提供商** → DeepSeek（示例）：`api_base = https://api.deepseek.com/v1`，模型 `deepseek-flash`，思考模式关闭
     （换其它 OpenAI 兼容模型也行；**要看图/表情包必须支持多模态**）
   - **人格** → 按用户口味写
3. **务必关闭**面板里的：`群聊上下文`、`群聊消息历史`、`主动回复`（与插件重复且显著变慢）。

**验收**（插件挂载 —— 最容易漏的一步）：

```powershell
docker exec webot-astrbot cat /AstrBot/data/plugins/astrbot_plugin_group_activation/metadata.yaml
```

能看到内容 = `./plugins` 挂载生效 ✓。看不到 → 检查 `docker-compose.yml` 的 volumes 是否包含
`./plugins:/AstrBot/data/plugins`，改后 `docker compose up -d --force-recreate astrbot`。

---

## 阶段四：微信桥（本机 Python）

**目标**：桥进程在跑，日志出现四条关键行，群消息能进能出。

1. 装依赖：

   ```powershell
   cd C:\webot\bridge
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   （requirements 已包含表情包功能需要的 `zstandard` / `Pillow` / `imageio-ffmpeg`，别删。）
2. 自检（两条都要过）：

   ```powershell
   .\.venv\Scripts\python.exe probe_db.py       # 应打印 [i] db_dir = ... 与 [i] 账号 = ...
   .\.venv\Scripts\python.exe introspect.py     # 应能列出会话/群
   ```

   `probe_db.py` 找不到目录 → 「文档」被 OneDrive 重定向；设环境变量 `WECHAT_DB_DIR`
   或在 `config.json` 的 `wechat.db_dir` 直接指定（阶段一第 3 步做过的话此处通常直接过）。
3. 配置（**从 example 现配，不要拷旧机器的**）：

   ```powershell
   copy config.example.json config.json
   notepad config.json
   ```

   必改字段：

   | 字段 | 要求 |
   |---|---|
   | `wechat.bot_nickname` | 机器人在群里的昵称，与微信界面**完全一致** |
   | `wechat.groups` | `{"群wxid@chatroom": "群名称"}`，群名称与微信界面**完全一致** |

   群 wxid 不知道？在微信里把目标群**置顶**，跑一次 `introspect.py` 看会话列表即可拿到。
4. 启动（第一次前台跑，便于看日志；之后交给 `start.cmd`）：

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

**验收** —— 日志必须按顺序出现：

```
微信账号: <昵称> (wxid_xxx)
图片解密密钥: ('xxxxxxxxxxxxxxxx', nnn)
监听 N 个群: ['群名', ...]
已连接 AstrBot -> ws://127.0.0.1:6199/ws
```

- `图片解密密钥` 一行拿不到 = **微信版本与库的锚点不匹配** → 这是最重的故障，见 §10 对照表。
- 桥呈现两个 python 进程是 venv 启动器的正常结构，不是重复运行。

---

## 阶段五：端到端验证

**目标**：群里 @ 它，它能回。

1. 微信停在目标群、不锁屏。
2. 群里发 `@<机器人昵称> 你好`。
3. 验收：
   - 微信侧：数秒内收到回复（首次会慢，桥在做窗口校准）；
   - 桥日志（`bridge\bridge.err.log`）：`[收到]` → `[上报]` → `[发送@]` 三段齐全；
   - 容器日志：`[group_activation] 放行默认 LLM（被@=True …）`。

> 提醒用户：机器人发送时会把微信窗口**切到前台**（界面自动化的需要），正用电脑时它会抢焦点，属正常。

**诊断链**（哪段断了解释哪段）：
`[收到]` 缺 → 桥没读到消息（微信/群名配置/DB）→ `[上报]` 缺 → 该条被判定无需处理
→ `放行默认 LLM` 缺 → 窗口没开或 @ 未被识别 → 有 `参与判定已生效` 但没回复 → 模型自己选了 `[PASS]`。

---

## 阶段六：熄屏挂机（虚拟显示器，唯一方案）

**目标**：`screen-off.cmd` 后物理屏真黑，机器人照常收发；**不碰任何电源设置**。

### 6.1 放置驱动（仓库不含驱动，需自行获取）

1. 获取 `usbmmidd_v2`：Amyuni 官方样例包，或社区项目
   <https://github.com/IYATT-yx/NullSignal> 仓库内附的同一份（旧机已比对过 SHA-256，无夹带；
   `usbmmidd.cat` 为 WHQL 签名，**不需要测试签名模式**）。
2. 放到**固定路径**（脚本按它找驱动）：

   ```
   C:\webot\tools\vdd\amyuni\usbmmidd_v2\deviceinstaller64.exe   ← 必须存在
   ```

### 6.2 安装与免 UAC

**管理员**执行（会弹 UAC，让人点"是"）：

```powershell
cd C:\webot
scripts\vdd.ps1 -Action status          # 先看当前状态
scripts\vdd.ps1 -Action setup           # 装驱动
scripts\vdd.ps1 -Action install-task    # 建 webot-vdd-enable / webot-vdd-disable 两个最高权限计划任务
```

> 为什么必须建任务：驱动开关（`enableidd`）要管理员权限；做成最高权限任务后，
> 熄屏脚本用 `Start-ScheduledTask` 触发，**免 UAC** —— 否则每次熄屏弹窗，没人点就熄不了屏。

### 6.3 熄屏测试（先短测再长测）

```powershell
scripts\screen-off.ps1 -NoOff           # 体检：应显示「熄屏方式 : 虚拟屏（面板真黑，程序照跑）」
scripts\screen-off.ps1 -Seconds 90      # 90 秒真黑测试（期间别碰鼠标键盘）
```

**验收**（90 秒测试期间执行）：

```powershell
# 1) 没有进连接待机（期望 0 条）
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power';
  StartTime=(Get-Date).AddMinutes(-5)} -ErrorAction SilentlyContinue |
  Where-Object { $_.Id -in 506,507,172 } | Measure-Object

# 2) 熄屏日志正常
Get-Content C:\webot\logs\screen-off.log -Encoding UTF8 -Tail 10
```

- 日志应出现切换与恢复记录；若有 `⚠️ 内屏被拉回`，看 §10 对照表。
- **期间机器人收发不受影响** —— 可以让用户在群里 @ 一次做最终确认。

### 6.4 日常使用

| 操作 | 效果 |
|---|---|
| 双击 `screen-off.cmd` | 体检 → 熄屏 |
| 碰一下鼠标/键盘 | 恢复（**只有真人输入会唤醒**；机器人自己点击被低级钩子过滤，不会误唤醒） |
| 再双击一次 `screen-off.cmd` | 恢复 |
| 群里发 `/gaon` / `/gaoff` | 远程点亮 / 熄屏 |

---

## 阶段七（可选）：L4 知识库 与 开机自启

### 7.1 L4 知识库（本地嵌入）

前提：装 Ollama 并保持后台运行：

```powershell
winget install -e --id Ollama.Ollama
scripts\pull_ollama_model.ps1           # 拉取 bge-m3（脚本绕过代理 fake-IP 导致的 pull 失败）
```

> **SP4 内存提醒**：bge-m3 约占 1–2GB。**4GB 机型直接跳过本节**，把插件 `config.json` 的
> `kb_enable` 设为 `false`（只影响 `/garemember` 知识，不影响日常聊天）；8GB 机型也建议实测内存余量后再开。

### 7.2 开机自启（仓库默认没有，建议配）

管理员 PowerShell：

```powershell
schtasks /Create /TN "webot-autostart" /SC ONLOGON /RL HIGHEST /TR "C:\webot\start.cmd"
```

> `start.cmd` 会拉起 Docker Desktop → 起容器 → 起桥，全程免人工。
> ⚠️ 仓库**不**含该任务，换机/重装系统后要重建。

---

## 阶段八：验收总清单（全部 ✅ 才算部署完成）

| # | 检查 | 通过标准 |
|---|---|---|
| 1 | `docker compose ps` | `webot-astrbot` running |
| 2 | `docker exec webot-astrbot cat /AstrBot/data/plugins/astrbot_plugin_group_activation/metadata.yaml` | 有输出 |
| 3 | 桥日志四行 | 微信账号 / 图片解密密钥 / 监听 N 个群 / 已连接 AstrBot |
| 4 | 群里 `@它 你好` | 有回复，桥日志 `[发送@]` 成功 |
| 5 | 发一张图片 + `@它 这是什么` | **一条**回复且描述的是那张图 |
| 6 | 发一个表情包 | 桥日志出现 `表情包 → 静态图: …jpg（原 … 字节/… 帧/…x… → … 字节）` |
| 7 | `screen-off.ps1 -Seconds 90` + 事件日志检查 | Kernel-Power 506/507/172 = **0** 条；期间机器人能回消息 |
| 8 | 群内 `/gaw`、`/gactx` | 有输出 |

---

## 10. 故障对照表（现象 → 诊断 → 处置）

| 现象 | 诊断 | 处置 |
|---|---|---|
| `probe_db.py` 找不到数据目录 | 「文档」被 OneDrive 重定向 | 设 `WECHAT_DB_DIR` 或 `config.json` 的 `wechat.db_dir`；最好把微信存储目录移出 OneDrive |
| 桥日志没有 `图片解密密钥` 或解密全失败 | 微信版本与库锚点不匹配 | 确认微信版本（旧机 4.1.15.13）；换成该版本，或等 `wechatauto-replica` 适配新版 |
| `@ 它`没反应 | 按 §5 诊断链逐段看日志 | 常见：`config.json` 群名/昵称与界面不一致；窗口没开（群内 `/gaw`） |
| 表情包只回占位符 | 桥日志搜 `表情包` | 无 `cdnurl`（部分表情确实没有）→ 正常退化；有报错 → 按报错处理 |
| 熄屏报 `✗ 找不到虚拟显示器驱动包` | 驱动没放到位 | 放到 `tools\vdd\amyuni\usbmmidd_v2\`（§6.1 固定路径） |
| 熄屏时弹 UAC | 计划任务没建 | `vdd.ps1 -Action install-task`（§6.2） |
| 熄屏后 15~40 秒内屏自己亮回来 | `DisplaySwitch.exe` 未退出，把显示配置回退 | 脚本已自动杀它；仍复现 → `logs\screen-off.log` + `scripts\_vdd_diag.ps1` |
| 熄屏后 `@` 失败/发错位置 | 虚拟屏分辨率不对（微信窗口被挤出屏幕） | 确认脚本把虚拟屏顶到 3840×2160（默认 `-VddW 3840`）；必要时删 `logs` 下校准缓存让它重校准 |
| 一条消息发送拖到 100+ 秒 | 切显示拓扑与发送**重叠**了 | 铁律：屏幕动作必须排在消息发送之后；用 `/gaoff`（先回消息再切屏），不要在桥发送期间手动切 |
| Docker 报 500 / `docker info` 空 | 缺 WSL2/虚拟机平台 | §阶段二 第 1 步 + 重启；`wsl --update` |
| 改了插件没效果 | 插件挂载缺失 | §阶段三验收；`docker compose up -d --force-recreate astrbot` |
| 重启后机器人没起来 | 未配自启（默认没有） | 双击 `start.cmd`；或按 §7.2 建计划任务 |

---

## 11. 与旧机器的差异备忘（agent 容易想当然的地方）

| 项 | 旧机（Windows 台式/笔记本） | SP4 |
|---|---|---|
| 屏幕 | 1366×768 级别 | **2736×1824 @200%** → 校准数据不通用，首次发送会重校准 |
| 触摸 | 无 | **有**：不要考虑"全黑遮罩"路线（触摸事件可能点在遮罩上），只走虚拟屏 |
| 合盖 | 已设"不操作" | **本方案不碰电源设置** —— 但要提醒用户：合盖/系统自动睡眠会让整条链路停摆，属部署者自己的系统管理决策（可用 `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE` 查看现状） |
| 内存 | 充足 | 4GB 版必须关 L4 |
| 熄屏 | 虚拟屏 | **同款**（虚拟屏与机器无关）；但驱动与两个计划任务要重装 |
| 数据 | 有历史记忆/知识库 | **全新开始**（不要拷 `data/`）；需要历史知识就手动 `/garemember` 重建 |

---

*最后核对本文件的机器：旧部署机（Windows，微信 4.1.15.13）。SP4 部署时若发现与本文不符的实测行为，
把现象与结论补回对应章节 —— 这份文档就是给下一个执行部署的 agent 看的。*
