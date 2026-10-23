# webot v0.19.0 本地表情库升级指南

本版新增本地表情库：

- `D:\webot\stickers` 保存图片和 `index.json`；
- 插件新增 `list_stickers` / `send_sticker` / `save_sticker`；
- 发送的是**微信图片气泡**，不是微信收藏的 sticker 消息；
- AI 可正式入库，用中文名、情绪、场景、标签命名。

## 1. 目录

```text
D:\webot\stickers\
  index.json
  files\
    st_0001_无语_无语扶额.gif
  README.md
```

`files/`、`index.json` 已在 `.gitignore` 中；表情图片不进 Git，换机需单独拷贝。

## 2. 升级

```powershell
Set-Location D:\webot
git pull --ff-only origin main
```

本次无新增 Python 依赖，不需要重建镜像。

确认插件配置：

```json
"sticker_send_max_per_turn": 1,
"sticker_save_max_per_turn": 1,
"sticker_send_timeout": 90.0
```

重启：

```powershell
restart-bridge.cmd
restart-astr.cmd
```

（桥需要加载 `sticker_library.py` 和三个新动作；插件需要注册三个新工具。）

## 3. 使用

- 用户说“发个无语的表情”：AI 调 `list_stickers(emotion="无语")`，再 `send_sticker("st_0001")`；
- **文字 + 表情（推荐）**：在最终回复末尾写 `[[表情:无语]]` 或 `[[表情:st_0001]]`；插件解析后调用桥 `text_sticker`，一个桥动作先发文字再发图，只有一次模型 API；
- 标记只能放在完整正文末尾；只写 @/标记时插件和桥会补兜底正文；含 @ 时走真实 @ 再发图；
- 文字发送后会缓存输入框，图片优先 fast-paste；仍受微信 UIA 粘贴/草稿检测时间限制，不是零延迟；
- 机器人自己发出的图片回声会被桥丢弃，不再进入 L1/视觉；
- 默认 `send_sticker(..., standalone=false)`：文字先发，图片由 `after_message_sent` 钩子补发；
- 只想用表情表达、不需要文字：`send_sticker(sticker_id="st_0001", standalone=true)`，插件后台发图并丢弃最终文字；
- AI 看到合适的图：`look_image(id)` 后调
  `save_sticker(image_id=id, name="无语扶额", emotion="无语", scenes="被问住,不想回答", tags="扶额,叹气")`；
- 同一张图按 sha256 去重；重名/同名文件自动编号；
- 每条回复最多发 1 张、入库 1 张。

## 4. 验证

1. 让 AI 把一张已缓存图片入库：日志应出现 `[表情] 已入库 st_0001（...）`；
2. 查看 `D:\webot\stickers\index.json` 和 `files\`；
3. 让 AI `list_stickers` 检索并 `send_sticker`，群里应收到一张图片气泡；
4. `send_sticker` 不经过 `send_message_to_user`，不应触发文字转发。

## 5. 限制

- 这不是微信真 sticker：`wechatauto` 没有 send_sticker；要真 sticker 需表情面板 UIA/协议，另做；
- 图片发送需要切会话/UIA 校准，实测可能 25 秒以上；`sticker_send_timeout` 默认 90 秒，过短会出现“图片已发出但工具报失败”；
- 动图目前按缓存静态帧处理；要保留动画需另存原始 GIF；
- AI 只新增，不覆盖/删除；需要删除时手动处理 `index.json` 和 `files/`；
- 不要只删 `files/` 而保留 `index.json`，否则 `find` 会返回不存在。
