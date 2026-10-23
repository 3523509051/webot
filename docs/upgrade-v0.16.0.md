# webot v0.16.0 新功能升级指南

适用于已经部署微信、Docker、AstrBot 和 webot 桥接的 Windows 服务器。本次更新聚焦**指名回复与多 @**，不需要重新配置虚拟屏，也不需要新增 Python 依赖。

## 1. 本次更新

- 一条回复可以 @ 多个群成员（默认最多 3 个）：模型在正文写 `@昵称`，桥按顺序逐个用 UIA 点选插入真实 @。
- 插件每次请求向桥拉取**完整群成员名单**，没发过言的成员也能被 @；不再只依赖 L1/L3 里出现过的人。
- 新增插件配置 `mention_only_sender`、`mention_max_count`、`force_at_sender`；桥新增 `wechat.max_mentions`。
- 修复多段粘贴时第二个 @ 后面的正文丢失：第一段后不再重复点击输入框，改为 `SetFocus` + `Ctrl+V` 追加。
- 修复「先发图、再问图」在 AstrBot 纯图片事件不触发插件缓存时的丢图：桥按「群 + 发送者」缓存 30 秒，追问时把图并入同一条事件。
- 脚本不再硬编码 `C:\webot`：`scripts\webot-service.ps1` 按自身位置找仓库根，微信安装路径从卸载注册表读取。
- 插件版本号更新为 **v0.16.0**。

## 2. 停止服务并备份

进入实际仓库目录（下例为 `C:\webot`，放在别处就换成实际路径）：

```powershell
Set-Location C:\webot
git status --short
git rev-parse HEAD
```

记下提交号以便回退。停止桥接（前台运行按 Ctrl+C；计划任务运行先停任务），再停止 AstrBot：

```powershell
docker compose stop astrbot
```

备份 `.env`、`bridge/config.json`、`data/` 和修改过的插件配置到仓库外的私有目录。**不要提交密钥、会话和记忆。**

## 3. 拉取代码

```powershell
git pull --ff-only origin main
```

本次没有新增桥依赖；如果 `requirements.txt` 有变化，再执行：

```powershell
.\bridge\.venv\Scripts\python.exe -m pip install -r bridge\requirements.txt
```

## 4. 合并配置（不要留重复键）

### 4.1 插件配置

`plugins\astrbot_plugin_group_activation\config.json` 中确认或加入：

```json
"mention_only_sender": false,
"mention_max_count": 3,
"force_at_sender": false
```

- `mention_only_sender=false` 是启用多 @ 的前提；`true` 会退回「只 @ 发送者、删除多余 @」。
- `mention_max_count` 要和桥的 `wechat.max_mentions` 一致。
- `force_at_sender=false` 表示不强制回复开头 @ 发送者，由模型决定。

如果原来的配置里已经有这些键，只改值，不要新增第二个同名键。

### 4.2 桥配置

`bridge\config.json` 的 `wechat` 中确认或加入：

```json
"max_mentions": 3,
"image_carry_seconds": 30
```

`max_mentions` 要和插件 `mention_max_count` 一致；`image_carry_seconds` 设 0 可关闭桥侧的图片跟随兜底。

### 4.3 提示词

`plugins\astrbot_plugin_group_activation\config.py` 的 `mention_prompt` 已更新为：

- 可以 @ 本群任何成员，不要求对方先发言；
- 发送者说「告诉 X / 提醒 X / 转告 X」时，必须在给 X 的那段开头写 `@X`；
- 使用 `send_message_to_user` 时，`plain` 文本里也要写 `@昵称`（桥只认正文 @ 名字）；
- 不要只写标题、不要把正文拆成另一条消息。

如果服务器上有自定义的 `mention_prompt` 覆盖（写在 `config.json` 里），需要手动同步这些规则，否则会覆盖代码默认值。

### 4.4 AstrBot 侧

- **不要移除 `send_message_to_user` 工具**：长正文依赖它发送；移除后模型可能只输出标题。
- 但桥不解析工具里的结构化 `mention_user`，所以提示词要求模型在 plain 文本里写 `@NULL`。
- Provider 的 `modalities` 需包含 `tool_use`（使用 `web_search` 等工具时）；当前 DeepSeek 配置已包含。
- 如果人格/系统提示词里写了「不要 @ 人」「只 @ 发送者」之类，删掉或改为与插件一致。

## 5. 启动与验证

```powershell
docker compose start astrbot
```

确认日志出现：

```
[group_activation] 已加载 v0.16.0 …
```

然后启动桥接：

```powershell
.\restart-bridge.cmd
```

在目标群测试：

1. `@机器人 你查一下高斯生平，然后告诉NULL，同时也提醒我待会查山姆生平`
   → 回复里 `@NULL` 和 `@NONE` 两段都有正文；
   → 桥日志出现 `成员名单 N 人：…` 和 `[发送@] …（2 个）`。
2. 只 @ 一个人 → 仍能正常回复，不再强制开头 @。

## 6. 常见问题

- **只 @ 了第一个人**：确认 `mention_only_sender=false`、`mention_max_count` 与桥 `max_mentions` 都 ≥2；确认已 `restart-astr.cmd`。
- **模型不知道某个成员**：看日志「成员名单」是否包含目标；群成员超过 `mention_max_names`（默认 12）时调大；新成员最多等 10 分钟缓存过期或重启 AstrBot。
- **@ 了但没有正文**：确认没有移除 `send_message_to_user`；提示词要求「一次写完整正文、不要只写标题」；桥需为最新版本（多段粘贴修复）。
- **`@NONE` 后面没内容**：确认桥已重启到包含「SetFocus + Ctrl+V 追加」的版本；日志里应看到 `共N个` 且正文完整。
- **模型继续模仿旧回复**：在群里发 `/gareset` 重置 L2；必要时 `/gaclear` 清 L1；旧历史里的「只 @NONE」会被模仿。
- **想退回单 @**：插件设 `mention_only_sender=true`、`mention_max_count=1`；桥 `max_mentions=1`；重启 AstrBot 和桥。

## 7. 回退

停止桥接与 AstrBot，保存本次本地修改，切换到第 2 步记录的旧提交，恢复 `.env`、`bridge/config.json`、`data/` 和插件配置备份，再按原方式启动。不要删除 `data/`，不要执行带 `-v` 的 Docker 清理命令。
