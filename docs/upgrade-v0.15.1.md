# webot v0.15.1 新功能升级指南

适用于已经部署微信、Docker、AstrBot 和 webot 桥接的 Windows 服务器。以下操作在服务器 PowerShell 中执行，不需要重新配置虚拟屏。

## 1. 本次更新

- 搜索改为 Playwright 在后台打开 Edge、填写并提交必应网页版搜索表单，不需要百度搜索 API Key。
- 新增高德地点查询、同城路线规划和天气查询；天气直接调用高德。
- 网页读取不再要求域名白名单、固定访问词或逐批次授权；仍需 ping 成功且平均延迟严格小于 300ms，并通过内容审核。
- 网页读取提高为所有群共享 300 次/天。浏览器搜索 50 次/天、高德请求 100 次/天，三个额度独立，失败也计数。

## 2. 停止服务并备份

进入服务器实际仓库目录；下面以默认部署目录为例：

```powershell
Set-Location C:\webot
git status --short
git rev-parse HEAD
```

记下当前提交号以便回退。使用原有停止方式停止桥接（前台运行时按 Ctrl+C；计划任务运行时先停止对应任务，避免自动重启）。随后停止 AstrBot：

```powershell
docker compose stop astrbot
```

将 `.env`、`bridge/config.json`、`data/` 及自己修改过的插件配置备份到仓库外的私有目录。备份包含密钥、会话和记忆，不要上传 GitHub。不要用其他机器的配置覆盖服务器的微信路径、群列表和登录数据。

## 3. 拉取代码并安装桥接依赖

```powershell
git pull --ff-only origin main
.\bridge\.venv\Scripts\python.exe -m pip install -r bridge\requirements.txt
```

每条命令成功后再执行下一条。如果 Git 提示本地修改冲突，先保留修改并逐项合并，不要使用强制重置或清理命令。尤其需要保留服务器特有的配置。

服务器需要安装 Microsoft Edge；默认使用 `msedge` 通道，无需另外安装 Playwright Chromium。搜索使用独立后台浏览器，不读取个人浏览器登录态。

## 4. 合并新配置

在服务器 `.env` 中增加高德 **Web 服务 API** Key：

```dotenv
AMAP_WEB_KEY=填入你自己的Key
```

不要把示例文本当成真实 Key。已设置就保留原值。模型 API Key 仍在 AstrBot 面板中配置。

在 `bridge/config.json` 的顶层增加或合并 `website`（不要替换整个文件，注意 JSON 逗号）：

```json
"website": {
  "enabled": true,
  "restrict_domains": false,
  "max_ping_ms": 300,
  "browser_channel": "msedge",
  "search_engine": "bing"
}
```

如果原来已经有 `website`，直接修改对应字段；不要留下重复键。原有 `allowed_domains` 在 `restrict_domains=false` 时不生效。

确认 `plugins/astrbot_plugin_group_activation/config.json` 中：

```json
"website_enable": true,
"website_daily_limit": 300,
"amap_enable": true,
"amap_daily_limit": 100,
"search_enable": true,
"search_daily_limit": 50
```

以上为需要核对的字段片段，不是完整配置。若保留了旧配置，必须将 `website_daily_limit` 的旧值 20 改为 300；配置文件的值优先于代码默认值。以后可改为更大的正整数，**不要设置成 0，0 不代表无限制**。当天已有计数会保留。

## 5. 启动并验证

因为增加了高德环境变量，应重新创建 AstrBot 容器：

```powershell
docker compose up -d --force-recreate astrbot
docker compose logs --tail 100 astrbot
```

确认插件加载版本为 `v0.15.1`。确认微信已登录、目标群已加入桥接配置，然后用原有方式启动桥接；也可以前台运行以便观察错误：

```powershell
Set-Location .\bridge
.\.venv\Scripts\python.exe -u main.py
```

不要同时启动两份桥接。看到连接 AstrBot 成功及目标群监听成功后，再用另一个账号在目标群真正 @ 机器人测试：

1. 「搜索深圳技术大学官网」：应使用必应网页版，返回来源链接。
2. 「读取 https://www.sztu.edu.cn/ ，概括首页内容」：应读取正文并通过审核后回答。
3. 「深圳现在天气怎么样？」：应返回高德天气及数据时间。
4. 「查询杭州东站到西湖的公交路线」：应查询地点和路线；地点不明确时先澄清。

网页读取是文字工具，不支持视频、登录后内容或必须运行网站 JavaScript 的页面。搜索遇到验证码会停止；网站禁 ping 或延迟达到 300ms 也会拒绝。网页内容和最终回复各有审核，通常会增加两次模型调用。

## 6. 常见问题与回退

- **仍提示每日 20 次**：核对插件配置值，重启 AstrBot，确认已加载 v0.15.1，且 Compose 使用仓库 `plugins` 目录挂载。
- **提示找不到浏览器或 Playwright**：确认 Edge 已安装，依赖装在运行桥接的同一虚拟环境，安装后重启桥接。
- **缺少高德 Key**：确认修改的是服务器仓库 `.env`，然后执行上述重新创建容器的命令；仅 restart 不会更新容器环境变量。
- **无法监听群**：核对服务器 `bridge/config.json` 的群名、群 ID 和微信数据库路径，保留服务器自身配置。
- **需要回退**：停止桥接和 AstrBot，保存本次本地修改，再切换至第 2 步记录的旧提交，恢复对应私有配置备份并重新创建容器、启动桥接。不要删除 `data/`，不要执行带 `-v` 的清理操作。

更多细节见 [地图与网页工具说明](maps-web.md)。
