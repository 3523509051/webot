# 备用路线笔记：Pad 协议桥（WeChatPadPro）

> ⚠️ **本路线当前未采用。** 现役链路是「本机 Windows 桥」（见 `architecture.md`）。
> 本文件保留调研与排错记录，供 **Ubuntu 跨平台**（见 `roadmap.md` P0）时复用。
> 最后同步：2026-10-07

---

## 1. 为什么当初考虑它

Pad / iPad 协议桥**不需要在本机安装微信客户端**，靠 HTTP 服务 + 扫码登录，
因此**可以跑在 Docker 里 → Ubuntu 和 Windows 都能用**，正对原始的「双平台」要求。

> ⚠️ **现状：这三个服务已从 `docker-compose.yml` 删除**（当前链路不用它们，见 `deployment.md` §5）。
> 要用这条路线就把下面这段贴回 compose，或单独存成 `docker-compose.pad.yml` 起：

```yaml
services:
  wechatpadpro:
    image: wechatpadpro/wechatpadpro:${WECHAT_TAG:-v18.6}
    container_name: webot-wechatpadpro
    restart: always
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    ports:
      - "${WPP_WEB_PORT:-1238}:1238"     # 实测 v18.6 仅 1238 提供服务（8080 无监听）
    env_file: [.env]
    environment:
      - TZ=Asia/Shanghai
      - DB_HOST=mysql
      - REDIS_HOST=redis
      # 桥只认完整 DSN（单独设 DB_HOST 不生效）
      - "MYSQL_CONNECT_STR=${MYSQL_USER}:${MYSQL_PASSWORD}@tcp(mysql:3306)/${MYSQL_DATABASE}?charset=utf8mb4&parseTime=True&loc=Local"
    volumes:
      - ./.env:/app/.env

  mysql:
    image: mysql:8.0
    container_name: webot-mysql
    restart: always
    environment:
      - MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
      - MYSQL_DATABASE=${MYSQL_DATABASE}
      - MYSQL_USER=${MYSQL_USER}
      - MYSQL_PASSWORD=${MYSQL_PASSWORD}
      - TZ=Asia/Shanghai
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-u", "root", "-p${MYSQL_ROOT_PASSWORD}"]
      interval: 5s
      timeout: 5s
      retries: 20

  redis:
    image: redis:6
    container_name: webot-redis
    restart: always
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 20

volumes:
  mysql_data:
  redis_data:
```

（配套的 `.env` 变量：`WECHAT_TAG` / `WPP_WEB_PORT` / `ADMIN_KEY` / `GH_WXID` /
`MYSQL_ROOT_PASSWORD` / `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` / `REDIS_PASSWORD`。）

---

## 2. 为什么最终没用

| 问题 | 说明 |
|---|---|
| 免费版停更 | Docker Hub 上公开的 `v18.6` 是 **2025-08-25** 的构建（约 13 个月未更新），`latest` 与它同 digest；新版（付费版已到 v875）**需赞助获取** |
| 授权接口拒绝免费版 | 见 §4 —— 授权服务在线，但对免费版返回 **401**，**免费公开版无法完成激活** |
| 第三方依赖风险 | 协议服务可能跑路（同类项目 Gewechat 已于 2025 年停止维护） |
| 封号风险 | 非官方协议，风险客观存在 |

于是改为**自研本机桥**（读本地数据库 + UI 自动化）：完全自主、零费用、无第三方依赖，
代价是**牺牲了跨平台**。

---

## 3. 部署与接口速查（v18.6 / 内部版本 861）

### 3.1 镜像

| 项 | 值 |
|---|---|
| 镜像 | `wechatpadpro/wechatpadpro:v18.6` |
| 架构 | 仅 `linux/amd64` |
| 端口 | **实测只有 1238 提供服务**（README 里写的 8080 无监听） |
| 数据库连接 | 桥**只认完整 DSN**（`MYSQL_CONNECT_STR`），单独设 `DB_HOST` 不生效 |

### 3.2 实际 API 路径

README 里写的 `GenAuthKey2` 在免费版中**不存在**，实际接口为：

| 用途 | 方法 | 路径 |
|---|---|---|
| 生成授权码 | POST | `/admin/GenAuthKey1?key=<ADMIN_KEY>`，body `{"Count":1,"Days":365,"Remark":"..."}` |
| 获取登录二维码 | POST | `/login/GetLoginQrCodePadX?key=<AuthKey>`，body `{"Proxy":"","Check":false}` |
| 查询登录状态 | GET | `/login/CheckLoginStatus` |
| API 文档 | GET | `/docs/`（Swagger UI）、`/docs/swagger.json` |

---

## 4. 排错记录

### 4.1 生成授权码失败：`授权服务暂时不可用`

```
{"Code":300,"Data":null,"Text":"生成授权码失败: 授权服务暂时不可用，请稍后再试"}
```

**① VPN 拦截（fake-IP 模式）**

桥需访问授权服务器 `https://adminkeyservice.knowhub.cloud`。
若系统开了 Clash / Surge 类代理，该域名会被解析到保留网段：

```powershell
Resolve-DnsName adminkeyservice.knowhub.cloud   # 若为 198.18.x.x 即被代理拦截
```

解决：关闭 VPN（或给 `*.knowhub.cloud` 加「直连」规则），然后

```powershell
docker compose restart wechatpadpro
```

**② 厂商授权门槛（HTTP 401）**

排除 VPN 后，从容器内直接请求授权接口：

```powershell
docker exec webot-wechatpadpro sh -c "wget -T 12 -S -O - --post-data='{}' --header='Content-Type: application/json' 'https://adminkeyservice.knowhub.cloud/api/v1/public/auth/generate'"
```

若返回 `HTTP/1.1 401 Unauthorized` → 授权服务**正常在线但拒绝免费版的激活请求**，
即**免费公开版无法激活**，需向厂商获取有效授权。

### 4.2 授权服务与接口速查

| 项 | 值 |
|---|---|
| 授权服务器 | `https://adminkeyservice.knowhub.cloud` |
| 授权接口 | `POST /api/v1/public/auth/generate` |
| 本地生成入口 | `POST /admin/GenAuthKey1?key=<ADMIN_KEY>` |

---

## 5. 若要在 Ubuntu 重启这条路线

需要确认/解决的前置问题：

1. 是否愿意为最新版付费（否则只能用 2025-08 的旧构建，且免费版**激活被拒**）
2. 是否有替代的 Pad 协议实现（同类项目多有跑路史，需自行评估）
3. 若走通，**AstrBot 与插件侧零改动** —— 只要它对外提供 OneBot v11 接口
   （或把它的私有 API 用一层薄适配转成 `send_group_msg` + 消息上报）

> 接口契约已经固定：**OneBot v11 反向 WS → `ws://<host>:6199/ws`**。
> 这是当初选 OneBot 做解耦的目的 —— 换桥不返工插件。
