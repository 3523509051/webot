# webot 桥接契约

本文描述当前源码提供的连接、事件与动作边界。标准消息使用 OneBot v11 的相应结构，`pero_*` 动作与 `pero_ext` 是额外协议；不代表完整实现 OneBot v11。

## 1. 连接

webot 是反向 WebSocket 客户端，由 `bridge/onebot11.py` 主动连接 `config.json` 中 `astrbot.ws_url`。`astrbot` 是保留的配置名，目标可以是 Pero。

| 项目 | 当前实现 |
| --- | --- |
| 账号 | `X-Self-ID` 来自 `astrbot.self_id`，事件的 self_id 使用同一配置值 |
| 角色 | `X-Client-Role: Universal` |
| 认证 | 配置 token 时发送 `Authorization: Bearer <token>` |
| 事件 | lifecycle、heartbeat、群聊及已授权私聊 message |
| 重连 | 断线后重新连接；重连不等同于重放微信历史 |
| 动作响应 | 保留收到的 echo，返回 status、retcode、data；发送失败可含 delivery_state |

认证在连接目标实现，部署者需核对令牌与网络入口。桥没有另开一个通用 HTTP 管理面板。

## 2. 入站事件

基础群聊事件示例，数字仅作结构说明：

```json
{
  "time": 1795305600,
  "self_id": 10001,
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "message_id": 12345,
  "group_id": 20001,
  "user_id": 30001,
  "message": [{"type": "text", "data": {"text": "你好"}}],
  "raw_message": "你好",
  "sender": {"user_id": 30001, "nickname": "示例成员"}
}
```

私聊事件改为 private，并使用对应 user_id。上层应该消费结构化 message 及实际来源字段，不仅靠 raw_message 解析身份和提及。

当前群／用户的数字标识以微信来源进行稳定映射，不能把它当作真实 QQ 号。部分来源取不到时仍有未知或回退情况，必须结合可信扩展与身份目录判断；映射空间也不是全平台无碰撞主键。message_id 是桥运行中的生成标识，不是微信平台永久消息 ID。

## 3. 可信来源扩展

`pero_ext` v1 在现有事件之外提供当前可得的来源信息，例如原始会话 wxid、发送者 wxid／名称、桥映射 ID、桥 session、本地消息号、引用及文件事实。

字段的存在与含义以 `bridge/main.py` 的扩展构造函数及 `onebot11.py` 的事件组装为准。不能把空字段补成猜测值，也不能因为拿到了发送者名称就认定取得了 wxid。群成员目录、消息映射与引用中的别名来源应分别保留。

Pero 使用这些事实处理私聊兑换、身份关联、资源获取及引用。其它后端可以忽略不使用的扩展；需要相应能力时必须明确适配。

## 4. 动作目录

| 类别 | 动作 | 说明 |
| --- | --- | --- |
| 状态 | get_login_info、get_version_info、get_status | 登录、版本及连接侧状态；不证明 GUI 当前能成功发消息 |
| 群目录 | get_group_list、get_group_member_list | 返回配置群与可获得的成员信息 |
| 身份 | get_identity_directory、get_message_identity | 微信来源、桥映射、别名与消息来源查询 |
| 文字 | send_group_msg、send_private_msg、send_msg | 文本抽取、目标解析与 GUI 发送；不是所有消息段都可直接执行 |
| 兼容转发 | send_group_forward_msg | 展开 nodes 文本为普通长消息，不创建原生合并转发卡片 |
| 单图媒体 | pero_media_send_v1 | 文字和单图按实际结果分别回执 |
| 批量媒体 | pero_media_send_v2 | 当前最多六图，文字与图片按序执行并逐项回执 |
| 文件原件 | pero_file_bytes_v1、pero_file_chunk_v1 | 按会话及文件编号查询，校验大小、偏移、长度等事实 |
| 引用原图 | pero_quote_image_v1 | 根据当前会话引用线索找回原图，查不到不替代 |
| 兼容资源 | list_files、read_file、list_stickers、save_sticker、rename_sticker、get_cached_image 等 | 既有桥资源／旧组合接口；不替代 Pero 的知识库及资源 API |
| 宿主辅助 | read_website、browser_search、wake_screen、sleep_screen | 非标准可选动作；依赖相应浏览器、脚本或桌面配置，部分旧路径需部署者核对 |

完整参数校验以 `main.py` 动作分支为准。未知发送动作不能按成功处理；未实现的普通查询也不能因为收到响应就认定具有相应能力。当前 `can_send_record` 返回 false。

### 文本动作示例

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": 20001,
    "message": [{"type": "text", "data": {"text": "这是一条测试回复"}}]
  },
  "echo": "request-example-1"
}
```

只有得到当前确认机制的成功结果才返回有效 message_id。该返回 ID 用于桥协议交互，不能当成微信数据库的 local_id。

## 5. 回执

普通成功响应：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {"message_id": 67890},
  "echo": "request-example-1",
  "message": ""
}
```

结果未知示例：

```json
{
  "status": "failed",
  "retcode": 1201,
  "data": {
    "error": "微信未确认发送结果",
    "delivery_state": "unknown",
    "retry_safe": false
  },
  "echo": "request-example-1",
  "message": "微信未确认发送结果"
}
```

unknown 不能简化为“没发出去，可以重发”。GUI 已执行但无法确认时再次发送可能造成重复。普通明确发送失败常使用 retcode=1200；其它错误和媒体分项还需按实际结构解析。

媒体动作在 data 中分别返回文字、图片与整批结果；某张图片失败或未知后，未尝试的项保留 skipped。传输 echo 与媒体业务 action_id 不是同一个标识，不能用旧响应替代新请求。

## 6. 私聊授权联动

当前桥调用 Pero 的内部接口同步有效名单和兑换开通码：

- `GET /internal/v1/private/authorized`：按连接账号消费可信授权结果。
- `POST /internal/v1/private/redeem`：提交账号、桥用户 ID、可信 wxid、开通码及来源事件。

接口需要内部管理访问码。它与 OneBot token 分别配置。同步异常不能当成“全部撤销”，成功空名单也不能当成“没有新变化”。私聊发送目标只从当前有效授权映射解析，不按界面昵称任意发送。

未授权发现流程只尝试符合完整开通码格式的候选，并受周期及限流控制。已有私聊的常规读取不需要每句话重新兑换；主人绑定与跨群权限由 Pero 决定。

## 7. 联调范围

最小联调顺序：连接认证 → 账号一致 → 群／私聊事件 → 普通文字发送 → 失败及未知回执。再分别验证媒体批次、文件补传、引用原图与授权同步。

如果使用其它后端，应记录实际支持的事件、动作与扩展版本。不要把握手成功描述为所有图文、文件、@ 和私聊功能都已兼容。
