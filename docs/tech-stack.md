# 技术选型

> 全部条目以**当前实际运行状态**为准。最后同步：2026-10-07

---

## 1. 选型总表

| 层 | 选型 | 理由 | 备选 | 备注 |
|---|---|---|---|---|
| 运行平台 | Windows 10/11（桥）+ 任意（框架） | 见 §5 差距 | Ubuntu | 桥不支持 Ubuntu |
| 容器 | Docker Desktop + Compose | 与 Ubuntu 部署零差异 | 裸装 | Windows 需 WSL2 |
| 编排框架 | **AstrBot**（`soulter/astrbot`） | 插件生态、LLM Provider 层、面板 | NoneBot / 自研 | 用户指定 |
| 微信接入 | **本机桥**（读本地 DB + UIA/OCR 写） | 自主可控、零费用、不依赖第三方协议服务 | Pad 协议 / PC Hook | Windows 专用 |
| 桥 ↔ 框架 | **OneBot v11 反向 WebSocket** | 标准协议，桥可整体替换 | HTTP / 直连 API | 端口 6199 |
| 对话 LLM | **DeepSeek `deepseek-flash`** | 中文强、便宜、JSON 稳、**原生多模态** | Qwen / GLM / GPT | 文本与图片同一个模型 |
| 图片理解 | **同一模型原生完成** | 无需独立视觉模型 | GLM-4V-Flash（免费） | — |
| 思考模式 | **显式关闭**（`thinking.type=disabled`） | 上下文变大后思考每轮 15–20s，是回复慢的主因（2026-10-08 复测，推翻旧结论） | 关闭（已实施） |
| 记忆 | **L1 自研 + L2 用 AstrBot 原生会话** | 原生不把未 @ 的群消息送进 LLM | RAG 插件（二期） | L3 未做 |
| 语音 | **不处理**（记 `[语音]`） | 用户决定砍掉 ASR | 云 ASR / Whisper | 语音内容无法理解 |
| 拟人节奏 | **`rhythm=fast`** | 安全档中最快 | `natural`/`calm`（更慢）/ **`off`（禁用）** | ⚠️ 见 §6 风险 |
| 插件语言 | Python 3.10+（容器内 3.12） | AstrBot 生态 | — | — |
| 桥语言 | Python 3.11（64 位，venv） | UIA/OCR 库要求 | — | 必须 64 位 |

---

## 2. 版本基线（本机实测）

| 组件 | 版本 |
|---|---|
| 微信客户端 | 4.1.12.26 |
| AstrBot | v4.28.2（容器） |
| 插件 | `astrbot_plugin_group_activation` v0.3.0 |
| 桥依赖 | `wechatauto-replica` 1.2.4.4 |
| Docker | 29.8.2 / Compose v5.5.1 |
| DeepSeek 模型 | `deepseek-flash`（V4.1 Flash，1M 上下文，原生多模态）|

---

## 3. 微信接入的三条路线（关键选型）

| 路线 | 做法 | 跨平台 | 成本 | 主要风险 |
|---|---|---|---|---|
| **A. 本机桥（当前）** | 读微信本地数据库拿消息；用 UIA/OCR 驱动界面发送 | ❌ 仅 Windows | 0 | 界面自动化被风控；微信升级可能失效 |
| B. Pad / iPad 协议 | 第三方 HTTP 服务（如 WeChatPadPro），扫码登录，无需装微信 | ✅ Docker 可跑 | 免费版停更（2025-08），新版需赞助 | 第三方协议服务可能跑路；封号；付费 |
| C. PC Hook | `wxauto` / `WeChatFerry` 类注入 | ❌ 仅 Windows | 0 | 注入易被检测 |

**当前采用 A**：它完全自主、零费用、不依赖任何第三方服务，代价是**牺牲了跨平台**。

### 关于 docker-compose 里的三个「闲置服务」（已清理）

`docker-compose.yml` 里曾保留 `wechatpadpro` / `mysql` / `redis` 三个服务（为路线 B 预留），
**当前链路完全没有用到它们**，只会让新部署多装三个容器、每次开机被自动拉起。

**现已删除**，compose 只剩 `astrbot`。原始定义留档在 `wechatpadpro-notes.md`。
（背景与清理过程见 `deployment.md` §5。）

---

## 4. 被否决 / 已放弃的方案

| 方案 | 否决原因 |
|---|---|
| 企业微信 | 用户明确排除 |
| Claw / ClawBot | 用户明确排除 |
| Gewechat | 2025 年已停止维护，官方仓库标注不可用 |
| 语音转文字（微信原生） | 走微信服务端（智聆），**只对公众号/小程序开放**，个人号协议无此能力 |
| 独立视觉模型 | DeepSeek V4.1 Flash 已原生多模态，无需第二个模型 |
| `deepseek-v4-flash-vision-exp` | 旧代号，已被 `deepseek-flash` 取代 |
| 插件自研决策（`decide.py` + `{reply, content, extend_seconds}`） | 与 AstrBot 默认 LLM 管线打架；改为「插件只管记忆与开关」（v0.3.0） |

---

## 5. 关键机制事实（供选型判断）

1. **AstrBot 原生只把「被 @ 的那条消息」送进 LLM**，未 @ 的群消息不进上下文
   → 这就是必须自研 L1 记忆的根本原因。
2. **AstrBot 内置的「群聊上下文 / 主动回复」功能默认关闭**（`group_icl_enable=false`、
   `group_message_history_enable=false`、`active_reply.enable=false`）—— 与我们的插件功能重叠，保持关闭。
3. **只要插件 handler 的 filter 通过，`waking_check` 就会置 `is_wake=True`**，
   因此插件能收到**所有**群消息（含未被 @ 的）—— 这是 L1 能工作的前提。
4. **插件侧置 `event.is_at_or_wake_command = True` 即可让默认 LLM 处理该消息**（等效于「被 @"）。

---

## 6. 风险登记

| 风险 | 等级 | 说明与缓解 |
|---|---|---|
| **账号风控 / 封号** | **高** | 读微信进程内存提取数据库密钥 + 界面自动化均属灰色行为。`rhythm.py` 作者记载**本机账号已于 2026-09-21 触发过一次风控**。缓解：保持 `rhythm=fast`，**禁止改 `off`**；控制发送频率；不要营销刷屏 |
| 微信升级导致失效 | 中 | 界面结构变化需重新校准（`WeChatGUI.calibrate_layout()`）；数据库格式变化可能使读取失效 |
| 锁屏无法发送 | 中 | UIA/OCR 需要可见窗口。缓解：关闭自动锁屏 |
| 群名必须稳定且唯一 | 中 | 发送依赖群名匹配（存量群改名会反复触发会话切换）。缓解：用唯一的群名 |
| 自己发的消息靠前缀判断 | 中 | 依赖微信库中「自己发的消息不带 `wxid:` 前缀」这一格式特征，微信改格式即失效 |
| L1 与窗口为内存态 | 低 | 桥或 AstrBot 重启即丢失，不影响正确性 |
| 群聊上下文功能被误开 | 低 | 若在面板里开启 AstrBot 内置的群聊上下文，会与插件重复且显著增加延迟 |

---

## 7. 成本

| 项 | 估算 |
|---|---|
| DeepSeek | 一次短回复约 100–300 token（含 L1 背景约 1–3k token 前缀）；开启思考模式时输出 token 约翻数倍 |
| 缓存 | system prompt 稳定前缀可命中缓存（价格约为未命中的 1/50） |
| 基础设施 | 0（全部本地） |
| 微信桥 | 0（路线 A 无授权费） |
| 搜索服务（若开启联网） | 百度 AI 搜索 / Tavily 有免费额度；博查按量付费 |

---

## 8. 联网搜索（未启用，属可选能力）

**结论先说**：联网搜索和「用哪家的模型 API」无关 —— DeepSeek 的 API **没有**服务端联网，
模型自己不会上网；它也和桥无关（桥只负责收发消息）。这是 **AstrBot 层**的内置能力，当前未启用。

当前配置（`cmd_config.json`）：`provider_settings.web_search = false`，各搜索服务的 key 全为空。

开启后模型会拿到一组 `web_search_*` 工具（agentic 方式，模型自己决定何时搜），
与知识库检索工具**并存**：知识库查内部资料，联网查外部信息。

**代价**：会多一轮工具调用（约 1–2 秒起），回复整体更慢 —— 在已有风控顾虑的前提下需权衡。

支持的搜索服务（`provider_settings.websearch_provider`）：

| 服务 | 配置键（key） | 备注 |
|---|---|---|
| Tavily | `websearch_tavily_key`（列表） | 为 LLM 设计；免费 1000 次/月（≈33 次/天，比百度少） |
| **百度 AI 搜索** | `websearch_baidu_app_builder_key`（字符串） | 国内直连、中文友好。⚠️ 百度把它拆成两个 API：**AstrBot 用的是「百度搜索」（裸搜索，`/v2/ai_search/web_search`，赠送 50 次/天）**；另一个「智能搜索生成」（100 次/天，搜索+AI 总结）AstrBot 不用 |
| 博查 Bocha | `websearch_bocha_key` | 国内、按量付费 |
| Brave / Exa / Firecrawl / AnySearch | 对应 `websearch_*_key` | 国外为主 |

开启方式（`cmd_config.json` → `provider_settings`，三行）：

```json
"web_search": true,
"websearch_provider": "tavily",
"websearch_tavily_key": ["tvly-xxxxxxxx"]
```

改完必须重启 AstrBot 才生效。
