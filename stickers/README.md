# webot 本地表情库

这个目录由桥和插件使用；**运行时表情**放 `files/` 和 `index.json`，不进 Git；仓库自带一份**默认表情快照** `default/`。

```text
stickers/
  index.json          # 运行时生成：名称/情绪/场景/标签/来源/哈希
  files/              # 运行时实际图片
  default/            # 随仓库发布的默认表情（24 张）
    index.json
    files/
  README.md
```

- 新设备/空运行时库启动时，`StickerLibrary` 会自动把 `default/` 播种到 `files/` 和 `index.json`；
- 已有运行时库不会被默认库覆盖；
- 新表情由 AI 调 `save_sticker` 自动入库；
- `list_stickers` 按情绪/场景/关键词检索；
- `send_sticker` 通过微信发送为**图片气泡**，不是微信收藏的 sticker 消息；
- 同一张图按 sha256 去重，不覆盖、不自动删除；
- 重名自动加序号；文件名会去掉 Windows 非法字符；
- 想手动整理可以直接改 `index.json`，但不要让 `file` 指向 `files/` 之外。

`.gitignore` 只忽略运行时的 `files/` 和 `index.json`；`default/` 会随 Git 发布。群聊里新收的表情仍不会自动上传。
