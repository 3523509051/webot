# webot 本地表情库

这个目录由桥和插件使用，图片本身**不进 Git**。

```text
stickers/
  index.json          # 自动生成：名称/情绪/场景/标签/来源/哈希
  files/              # 实际图片，文件名形如 st_0001_无语_无语扶额.gif
  README.md
```

- 新表情由 AI 调 `save_sticker` 自动入库；
- `list_stickers` 按情绪/场景/关键词检索；
- `send_sticker` 通过微信发送为**图片气泡**，不是微信收藏的 sticker 消息；
- 同一张图按 sha256 去重，不覆盖、不自动删除；
- 重名自动加序号；文件名会去掉 Windows 非法字符；
- 想手动整理可以直接改 `index.json`，但不要让 `file` 指向 `files/` 之外。

`.gitignore` 已忽略 `files/` 和 `index.json`，避免把群聊图片和本地表情提交到 GitHub。
