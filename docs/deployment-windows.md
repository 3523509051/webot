# Windows 本机部署：Pero 与 webot 独立后台运行

**Pero/start.cmd 只启动 Pero 核心和控制室；webot/start.cmd 只启动微信桥。** 两者后台运行，各自 stop.cmd 只停止本项目。只打开网页用 Pero 控制室地址 http://127.0.0.1:8766/，不会启动服务。无需 Tailscale。

## 首次部署顺序

1. 克隆两个仓库到同一父目录：Pero 与 webot。
2. 安装 64 位 Python 3.10+、Node.js/npm。
3. 双击 webot/install.cmd 安装双方依赖；它只安装，不启动。也可分别运行 Pero/install-pero.cmd、webot/install-bridge.cmd。
4. 手动登录机器人微信，先只保留这一个账号。
5. 双击 webot/配置微信目录.cmd，填写 xwechat_files 根目录（不是账号子目录或 db_storage）。已有配置先备份。
6. 双击 webot/绑定微信账号.cmd，按提示在自己的文件传输助手手动发送绑定码并确认。
7. **双方服务停止时**，双击 webot/连接Pero.cmd，一次性同步该账号全部群路由、连接地址和本机加密凭据。没有选群步骤，此脚本不启动服务。非并列目录可传 `--pero-root "D:\Apps\Pero"`。
8. 双击 **Pero/start.cmd**，等待核心与控制室后台启动，浏览器自动打开本机控制室。无需先启动微信桥。
9. 双击 **Pero/login-code.cmd** 查看本机管理访问码并登录。访问码由 Pero 负责，模型 API Key 另在控制室配置。
10. 双击 **webot/start.cmd**，它只启动桥，并按已保存的配置连接 Pero。若 Pero 尚未运行，桥不会代为启动它；连接状态看 webot/status.cmd、桥日志和控制室。
11. 由你在测试群发唤醒消息确认收发，再登录第二个微信复测。

## 独立使用

| 入口 | 行为 |
|---|---|
| Pero/start.cmd | 后台启动 Core、Manager；打开控制室，不启动 webot |
| Pero/stop.cmd | 收尾并停止 Core、Manager，不停止 webot |
| Pero/status.cmd | 查看 Pero 自己的状态 |
| Pero/login-code.cmd | 查看由 Pero 管理的访问码；先启动 Pero 或完成连接配置 |
| webot/start.cmd | 后台启动微信桥，不启动 Pero 或微信 |
| webot/stop.cmd | 停止本次桥进程，不停止 Pero 或微信 |
| webot/status.cmd | 查看桥进程和日志位置；进程运行不等于已连接后端 |
| webot/连接Pero.cmd | 停止双方后同步连接和全部群配置，不启动服务 |

管理访问码保存在 Pero/data/manager，OneBot 连接令牌也由 Pero 提供给接入桥。连接配置脚本只把桥需要的值按当前 Windows 用户加密保存在 webot/data/connection.dpapi；明文不写配置或日志。凭据改变后，停止双方并重新运行连接配置，再分别启动。

## 群聊范围

数据库扫描与读取固定使用同一份绑定中的完整账号目录和内部 wxid，不依赖发送窗口的持续有效性。发送端另外核对微信进程、窗口及前台；窗口校验失败只拒绝发送，不应因此停止固定数据库的读取。底层数据库若自动回退其他账号，仍立即拒绝读取，不会按更新时间重新选择。

连接配置自动接入数据库扫描到的全部群，不要求前端或命令行选群。通过机器人入群、退群控制范围；当前不热更新后端路由，变更后分别停止双方、运行连接Pero.cmd 同步，再分别启动。每条消息是否回复仍由唤醒词及群规则决定。

## 旧组合入口升级

若原组合实例还在运行，先在 webot 运行 stop-old-combination.cmd 正常收尾。这是一次性迁移用的停止入口，不能启动服务。之后按上面的连接配置与独立启动顺序使用。

旧组合配置、数据和加密凭据不删除。首次连接时若 Pero/config/local.json 不存在，可采用旧 webot/data/combo/config.json 作为配置起点，保留其原有数据路径；否则沿用已有 Pero 配置。不要删除旧数据目录来处理启动错误。新系统各自维护进程状态；旧管理界面保存的模型配置如需迁移，应单独核对，不以重新安装覆盖数据。

## 故障定位

- 缺少 .venv：运行对应仓库安装脚本。
- 缺少前端资源：运行 Pero/install-pero.cmd，或在 Pero/frontend 执行 npm ci、npm run build。
- 尚未生成访问码：先运行 Pero/start.cmd。Pero 启动无需微信登录或 webot。
- 桥连接不上：确认 Pero 已启动、双方地址/端口/路由一致；必要时双方停止后重新运行连接Pero.cmd。
- 桥日志位于 webot/logs/bridge，Pero 日志位于 Pero/logs/launcher。只结束各自拥有的进程，不按 Python 名称批量终止。
