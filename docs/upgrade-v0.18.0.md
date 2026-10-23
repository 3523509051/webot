# webot v0.18.0 接话 preflight 与图片请求压缩升级指南

本版把「窗口内非 @ 消息」从“每条都发完整上下文让模型决定”改成：

- 纯文字：先做 L1-only 的 `JOIN` / `PASS` 判定；PASS 不调用完整模型；
- 带图：只有 **@ + 引用图片** 自动附图；@ 新图、窗口图都只给 L1 编号，按需 `look_image`；
- 图片请求：只有 @ + 引用图片的请求把 L2 压到 4 轮 / 6000 字。

## 1. 行为矩阵

| 场景 | 分类器 | 图 | L1 | L2 | L3/L4 |
|---|---|---|---|---|---|
| @ 纯文字 | 跳过 | 不带 | 有 | 8 轮 / 12000 字 | 有 |
| @ + 引用图片 | 跳过 | **直接带引用图** | 有 | 4 轮 / 6000 字 | 有 |
| @ + 新图（不是引用） | 跳过 | 不带，只给 `[图片 #ID]` | 有 | 8 轮 / 12000 字 | 有 |
| @ 后窗口，非 @ 纯文字 | L1-only JOIN/PASS | 不带 | 判定时有 | PASS 0；JOIN 8 / 12000 | PASS 0；JOIN 有 |
| @ 后窗口，非 @ 图/表情 | L1-only JOIN/PASS | 不带，只给 `[图片 #ID]` | 判定时有 | PASS 0；JOIN 8 / 12000 | PASS 0；JOIN 有 |
| 历史图/旧图 | 由完整模型决定 | 调用 `look_image` | 有 | 8 / 12000 | 有 |

## 2. 升级步骤

```powershell
Set-Location D:\webot
git pull --ff-only origin main
```

无新增依赖。确认 `plugins\astrbot_plugin_group_activation\config.json`：

```json
"preflight_enable": true,
"preflight_timeout": 8.0,
"image_history_max_turns": 4,
"image_history_max_chars": 6000
```

然后：

```powershell
docker compose restart astrbot
```

不需要重建镜像；本版没有改 Dockerfile 的 8MB 上限逻辑。

## 3. 验证

1. **@ 纯文字**：`@机器人 你好` → 直接回复，日志不应出现 preflight。
2. **窗口纯文字**：先 @ 一次，30 秒内再发一句普通陈述：
   - 应看到 `preflight PASS` 或 `preflight JOIN`；
   - PASS 时没有 `Prepare to send`；JOIN 时正常回答。
3. **窗口带图**：先 @ 一次，再发一张表情包/图（不 @）：
   - 请求的 `image_urls` 应为 0，L1 里有 `[图片 #ID]`；
   - 如果需要看图，模型应调用 `look_image`；
   - 不应把无关表情包自动塞进请求。
4. **@ + 引用图片**：引用一张图并 @ 机器人 → 正常看图；日志出现 `图片请求：L2 压缩为 4 轮 / 6000 字`。
5. **@ + 新图（非引用）**：@ 同时发一张新图 → 请求 `image_urls=0`，L1 有 `[图片 #ID]`，模型需要时才 `look_image`。
6. **历史图**：引用/旧图 → `look_image` 正常。

## 4. 指标与回退

观察：

- `preflight PASS/JOIN` 比例；
- JOIN 后是否又出现 `LLM 选择不接话`（应被禁用；若出现说明 prompt/流程有问题）；
- `图片请求：L2 压缩` 是否只在 @ + 引用图片出现；
- 总延迟和 token 用量。

回退：把 `preflight_enable` 设为 `false`，`restart-astr.cmd`；窗口图片仍会按 v0.18.0 的“仅 L1”策略，若要连图片策略一起回退，切回 v0.17.0 提交并恢复配置。
