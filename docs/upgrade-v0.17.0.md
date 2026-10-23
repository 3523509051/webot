# webot v0.17.0 图片编号与按需查看升级指南

本次更新让图片**按群独立编号、全量缓存**，L1 只记 `[图片 #ID]`；模型需要旧图时调用 `list_images` / `look_image`，不再强制把历史图塞进上下文。人格、L1/L3/L4 和会话历史不变。

## 1. 本次更新

- 桥新增图片索引：每群独立编号、单文件存储、默认**保留所有**，不自动裁剪。
- 图片事件带 `[图片 #ID]` 文字标记；L1 记录编号，不存图片本体。
- 插件新增两个 LLM 工具：
  - `list_images(sender?, since?, limit?)`：按发送者/时间找旧图编号；
  - `look_image(image_id)`：取图，AstrBot 把图追加到同一轮上下文，模型继续用原人格/记忆回答。
- L1 只保留最近 10 条；更早的图通过 `list_images` 检索。
- 防循环：重复编号返回“已提供”；过期/不存在终止；`list_images` 每轮最多 2 次、最多 50 条；`look_image` 每轮最多 10 张。
- 图片默认不降分辨率；AstrBot 的 512KB 上限由 `Dockerfile.astrbot` 在镜像构建时直接改成 8MB。
- 插件版本 **v0.17.0**。

## 2. 停止服务并备份

```powershell
Set-Location C:\webot
git status --short
git rev-parse HEAD
```

停止桥接和 AstrBot，备份 `.env`、`bridge/config.json`、`data/` 和插件配置。**不要备份/提交 `bridge/cache/` 里的图片和索引**（体积大，且是机器本地数据）。

## 3. 拉取代码

```powershell
git pull --ff-only origin main
```

本次没有新增 Python 依赖。

## 4. 桥配置

`bridge\config.json` 的 `wechat` 中确认：

```json
"image_max_edge": 0,
"image_max_bytes": 10000000,
"image_cache_keep": -1,
"image_carry_seconds": 30
```

- `image_max_edge=0`：不缩放；
- `image_cache_keep=-1`：保留所有（0 = 关闭图片索引；不自动删除）；
- `image_carry_seconds`：先图后问的补挂窗口。

桥启动后会创建：

```
bridge\cache\image\g<group_hash>_<seq>.jpg
bridge\cache\image_index.sqlite3
```

旧版 `cache/image/<md5>.jpg`、`t<time>.jpg` 不会被删除，引用旧图时仍可 fallback。

## 5. AstrBot 配置

先停容器再改：

```powershell
docker compose stop astrbot
```

`data\astrbot\cmd_config.json` 的 `provider_settings.image_compress_options`：

```json
"image_compress_options": {
  "max_size": 16384
}
```

不要留重复键。首次升级或基础镜像更新后，重新构建带补丁的本地镜像：

```powershell
docker compose build --pull astrbot
docker compose up -d --force-recreate astrbot
```

> AstrBot 默认 `MODEL_IMAGE_MAX_BYTES = 512KB`。本仓库的 `Dockerfile.astrbot`
> 在**构建镜像时**直接把 `media_utils.py` 改成 8MB，因此 `--force-recreate`
> 不会丢；以后更新基础镜像时重新执行 `docker compose build --pull astrbot` 即可。
>
> 如果上游改了这行代码，构建会因 `assert`/`grep` 失败而停下，不会静默恢复 512KB。

## 6. 插件配置

`plugins\astrbot_plugin_group_activation\config.json` 可调：

```json
"image_look_max_per_turn": 10,
"image_list_max_per_turn": 2,
"image_list_limit": 20
```

`image_look_max_per_turn` 是安全上限，不是功能限制；模型仍按提问决定查几张，但不会在同一轮无限取图。

## 7. 启动与验证

启动桥接，日志应出现：

```
[图片] 已缓存 #1（local_id=...）
[group_activation] 已加载 v0.17.0 …
```

测试：

1. 群里发一张图（不 @）→ 桥日志出现 `已缓存 #N`；`/gactx` 能看到 `[图片 #N]`。
2. 直接 `@机器人 这张图是什么` → 当前批次图片照常送给模型，不需要工具。
3. 等 L1 滚出后，`@机器人 帮我看看之前那张图` → AstrBot 日志出现
   `Agent 使用工具: ['list_images', 'look_image']`；模型回答基于取回的图。
4. 同一轮多次取同一编号 → 工具返回“已提供”，不会重复追加图片。
5. 取一个不存在/已过期的编号 → 返回“不存在/已过期”，不重试。
6. 看 L2：下一轮请求的 `GA-DIAG` 里 `ctx` 不应再带图片；`历史图片已剥离` 日志会出现。

## 8. 常见问题

- **图片没有编号**：确认桥是新版本、`image_cache_keep` 不是 0；看桥日志是否有 `已缓存 #N`。
- **模型不调用工具**：确认 AstrBot 日志已注册 `list_images` / `look_image`；确认 provider 支持 `tool_use`；提示词里 L1 应有 `[图片 #ID]`。
- **工具取图后模型想发图**：工具结果已明确要求不要转发；如果仍发生，可在人格里补一句“缓存图只用于理解，不要转发”。
- **磁盘涨得快**：默认保留所有，桥不会自动删。需要清理时停桥，删除 `bridge\cache\image` 与 `image_index.sqlite3`（两者要一起清），再启动。不要在运行中只删文件。
- **旧图找不回**：先看 `list_images` 是否返回编号；索引启动前发送的图/表情包没有编号，只能靠引用或微信本地库找回。引用时如果本机确实没有原图，桥会明确标“本机没有找到原图”，不会再拿最近的其他图顶替。
- **引用表情包识别错**：确认桥是新版本（表情包也会记录 `create_time` 和 md5）；旧表情包如果在索引启动前就发过，仍可能找不回，重新发一次即可获得编号。
- **合并转发聊天记录里的图片**：桥只能读标题、文字摘要和图片数量；记录内图片是 CDN 加密引用，当前不下载，会明确标“无法直接读取”，不会拿别的图猜。单张转发图片不受影响。
- **AstrBot 升级后取图不返回图片**：确认新版本仍支持工具返回 `ImageContent`；必要时回退到 v0.17.0 对应提交。

## 9. 回退

停止桥接和 AstrBot，切换到第 2 步记录的旧提交，恢复配置备份，再启动。不要删除 `data/`；不要执行带 `-v` 的 Docker 清理。
