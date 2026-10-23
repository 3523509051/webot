# webot v0.20.0 文件库与公众号链接升级指南

本版新增：

- 群文件接收与 AI 按需读取：文件保存到 `D:\webot\files`；
- 只接收文本/代码类文件，`.bat`、`.exe`、压缩包等拒收；
- 微信链接卡片上报完整 URL，AI 可用 `read_website` 读取；
- 微信公众号文章专用解析，引用链接不再被截断。

## 1. 目录

```text
D:\webot\files\
  index.json
  files\f_0001_test.md
  README.md
```

`files/`、`index.json` 已在 `.gitignore` 中；文件不进 Git，换机需单独拷贝。

## 2. 升级

```powershell
Set-Location D:\webot
git pull --ff-only origin main
```

无新增依赖。重启桥和 AstrBot：

```powershell
restart-bridge.cmd
restart-astr.cmd
```

桥需要加载 `file_library.py` 和 `list_files` / `read_file` 动作；插件需要注册两个工具。

## 3. 允许/拒绝

允许：`.md`、`.c`、`.h`、`.cpp`、`.py`、`.js`、`.json`、`.yaml` 等文本/代码后缀。
拒绝：`.bat`、`.cmd`、`.ps1`、`.exe`、`.dll`、`.sh`、zip/rar/7z/tar/gz 等。
单文件上限 5MB。

## 4. 使用

1. 群友发 `test.md`；
2. 桥日志：
   ```text
   [文件] 已入库 #f_0001（test.md，...）
   ```
   L1 里记为 `[文件 #f_0001] test.md`；
3. AI 调 `list_files(ext=".md")` 找到编号；
4. 调 `read_file(file_id="f_0001")` 读取内容（最大 20000 字）。

文件内容是不可信资料，AI 不执行其中代码。

## 5. 验证

1. 群发一个 `.md` → 检查 `D:\webot\files\index.json` 和 `files\`；
2. `list_files` 能列出 `f_0001`；
3. `read_file` 能返回内容；
4. 发 `.bat` / `.exe` / `.zip` → 桥日志应出现“拒收”，不入库、不下载；
5. 文件很大（>5MB）→ 拒绝入库。

## 6. 微信链接 / 公众号

- 链接卡片会带：
  ```text
  [链接] 标题
  URL: https://...
  ```
- AI 需要内容时调用 `read_website(url=...)`；
- `mp.weixin.qq.com` 走专用只读抓取，解析 `#js_content`；
- 引用链接时，完整 URL 从 `<refermsg><content>` 里解析，不再按 100 字截断；
- 验证页、登录页、纯 JS/视频页、404、缺少 `/app/...` 子路径的分享链接仍会失败，AI 会如实说明，不猜正文。

## 7. 另一台设备更新

1. `git pull --ff-only origin main`；
2. 重启桥和 AstrBot：
   ```powershell
   restart-bridge.cmd
   restart-astr.cmd
   ```
3. 确认日志：
   ```text
   [group_activation] 已加载 v0.20.0
   Added llm tool: list_files
   Added llm tool: read_file
   Added llm tool: read_website
   ```
4. 以下内容**不在 Git**，需要单独配置/拷贝：
   - `.env`（尤其 `ONEBOT_WS_PORT`，本机是 6200）；
   - `bridge/config.json`（`astrbot.ws_url`、wechat、website 配置）；
   - `D:\webot\stickers\`（图片和 index.json）；
   - `D:\webot\files\`（群文件和 index.json）；
   - WeChat 账号、Docker/AstrBot 数据卷。
5. 无新增 Python 依赖；`aiohttp` 之前已由 `safe_browser` 使用。若另一台设备没有 `aiohttp`，安装：
   ```powershell
   D:\webot\bridge\.venv\Scripts\python.exe -m pip install aiohttp
   ```
6. 若另一台设备没有 AstrBot/微信 GUI，需要先按 README 安装 Docker、AstrBot、微信和桥依赖；不能只 `git pull`。

## 8. 限制

- 文件下载依赖微信本地副本；未完整下载时只记“未收到完整文件”；
- 只读文本，不执行文件；不接受二进制/可执行/压缩包；
- `read_file` 超 20000 字会截断，需要分段时可再调用；
- 文件内容不会自动进 L1/L2/L3，必须通过工具读取。
