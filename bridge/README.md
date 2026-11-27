# webot 微信桥

当前项目定位、工程图、安装与配置以 [仓库首页](../README.md) 为入口，协议说明见 [桥接契约](../docs/bridge-integration.md)。

桥运行在已登录微信的 Windows 用户桌面中，使用本地数据库读取与界面自动化发送。当前配套上层服务为 Pero；OneBot 配置中的 `astrbot` 节名是历史保留字段，实际目标由 ws_url 决定。

## 本目录启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

先按首页说明编辑本机 config.json 并启动上层服务，再运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

需要私聊时，启动桥的进程还需获得 `private_chat.token_env` 指定的内部管理访问码。发送依赖可操作的桌面，锁屏状态不是可用的无人值守发送环境。

## 主要源码

| 文件 | 职责 |
| --- | --- |
| main.py | 装配、事件转换、动作分发与资源入口 |
| onebot11.py | 反向 WebSocket、消息事件、心跳、动作 echo |
| wechat_client.py | 微信数据访问、目标缓存、文字／@／图片执行 |
| unified_poll.py | 群聊与私聊统一读取、独立会话水位和队列 |
| private_gate.py | 私聊授权、水位及投递闸门 |
| private_chat_handler.py | Pero 名单同步、开通码与身份校验 |
| identity_directory.py | 身份、别名与消息来源映射 |
| file_library.py／sticker_library.py | 桥文件及兼容表情资源 |
| send_result.py | sent／failed／unknown 发送结果 |
| paths.py | 微信数据目录发现 |

## 本地诊断

`probe_db.py` 定位数据目录，`introspect.py` 检查数据库可读性及消息结构。输出可能包含账号、群名、路径或消息字段，公开分享前请脱敏。

根目录的旧 AstrBot 运维脚本不是 Pero 的启动器。不要据脚本名称直接重启不相关的服务；具体职责和兼容范围见首页。

首次使用请先在仓库根目录运行绑定微信账号.cmd，再运行 start.cmd 自动同步全部群并启动。详见 ../docs/deployment-windows.md。
