# 本机 Windows：webot＋Pero 从零部署

组合启动由 webot 管理，Pero 保持独立服务。此路线只使用本机回环地址，不需要 Tailscale，也不需要服务器已有配置或数据。

## 1. 克隆到同一父目录

准备 Windows 10/11、Git、64 位 Python 3.10+、Node.js/npm，以及兼容驱动的微信客户端。

```powershell
git clone https://github.com/3523509051/Pero.git
git clone https://github.com/3523509051/webot.git
```

两个目录应并列：`部署目录/Pero` 和 `部署目录/webot`。

## 2. 安装 Pero

```powershell
Set-Location .\Pero
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Set-Location .\frontend
npm ci
npm run build
Set-Location ..\..
```

此时不要启动 Pero 的 start.cmd；组合由 webot 统一启动。Pero 独立入口仍可用于不接微信的场景。

## 3. 安装 webot 并配置数据根目录

```powershell
Set-Location .\webot\bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Set-Location ..
```

已有配置时不要覆盖 config.json。在微信“账号与存储”查看数据根目录，把它填写到 bridge/config.json 的 wechat.db_dir，例如 `D:/微信/xwechat_files`。填写账号子目录的上一级，而不是 db_storage 本身。根目录自定义位置必须显式配置，不沿用其他机器的路径。

## 4. 单号登录与账号绑定

手动登录机器人微信，暂时退出其他账号。在 webot 根目录双击 `绑定微信账号.cmd`。工具会核对唯一主窗口与其进程打开的数据库，再要求你在这个账号自己的“文件传输助手”手动发送一行随机绑定码，验证后输入 YES。

生成的 bridge/account-binding.json 保存账号目录和内部 wxid，不含聊天正文或密钥，不进入 Git。失败时不猜账号；核对桌面用户、目录、权限和驱动。不要从 SSH 或无交互桌面启动此步骤。

## 5. 首次选择接入群：在命令行操作

双击 webot 的 `setup.cmd`。程序读取已绑定账号的群列表，显示类似：

```text
1. 测试群
2. 朋友群
3. 工作群
Group numbers separated by commas; ALL explicitly enables all; blank cancels:
```

只接测试群就输入 `1`；接第 1 和第 3 个群输入 `1,3`。只有明确输入大写 `ALL` 才接全部群，空白或非法编号会取消。**这是命令行选择，不是前端新增的选择页面。** 建议先只选一个测试群。

setup 会在 webot/data/combo/config.json 生成组合运行配置，并在本机 data/launcher 保存群配对记录和备份。它不调用模型、不发送微信消息。后续启动仅沿用已选择且仍可见的群，不自动接入其他新群。

## 6. 启动并配置模型

双击 **webot/start.cmd**。准备成功后，监督进程在后台运行并启动 Pero Core、控制室和桥，浏览器打开 `http://127.0.0.1:8766/`。成功后可关闭启动命令窗口，不要手动结束监督进程。

双击 webot/login-code.cmd 查看本机管理访问码并登录控制室，在“模型与 API Key”配置提供方与凭据，在 Agent 配置所需人格和唤醒规则。管理码、OneBot 令牌和模型 Key 用途不同，前两者由启动器在本机用户作用域加密保存并通过环境注入，不能提交凭据。

控制室的“Agent → 群聊与私聊”用于调整已接入群的开关与规则；首次群选择由上一步命令行完成。不要为了找该入口而反复扫描全部群。

## 7. 验证收发后再双开

由你在所选测试群发一条唤醒消息，确认回复来自机器人账号。再登录第二个微信，复测文本及实际使用的图片、@和文件。进程或账号身份不一致时桥应停止操作，不能自动换到另一账号。无法确认的发送不自动重发。

## 8. 日常入口都在 webot

| 入口 | 用途 |
|---|---|
| start.cmd | 准备并后台启动组合；已有实例不会重复接管 |
| stop.cmd | 请求对话收尾并停止本次组合，不退出微信 |
| status.cmd | 查看本组合的状态和日志路径 |
| setup.cmd | 首次扫描与选择群，不启动服务 |
| login-code.cmd | 明确显示当前用户的管理访问码 |
| 打开控制室.cmd | 只打开本机网页，不重启业务 |
| 绑定微信账号.cmd | 桥停止时核对并绑定机器人账号 |

非并列目录可给 webot 入口传 `--pero-root "D:\Apps\Pero"`。停止、状态和访问码入口需使用相同目录参数。高级 --config/--state-dir 需在各入口保持一致。

服务器/电脑重启后，恢复登录桌面和机器人微信，再从 webot 启动组合。锁屏或远程桌面断开后的 GUI 可操作性需实测。绑定或版本不兼容时按真实错误处理，不删除微信目录、记忆或凭据。

webot 旧 scripts/webot-service.ps1 和 Docker/AstrBot 资料保留作历史兼容，不属于这条 Pero 部署路线。GitHub 克隆不包含虚拟环境、前端构建、本机配置、绑定和凭据，需要按以上步骤首次准备。
