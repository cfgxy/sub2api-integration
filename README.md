# Sub2API Integration Platform

一键部署 **Sub2API** + **NextChat** 集成平台，两个系统同域运行，用户无缝切换。

## 架构

```
:8000 (Caddy 反向代理)
├── /chat, /chat/*          → NextChat
├── /_next/*                → NextChat 静态资源
├── /api/config             → NextChat API
├── /api/openai/*           → NextChat LLM 代理
├── /chat-bootstrap         → API Key 自动绑定引导页
├── /api/*                  → Sub2API
├── /v1/*                   → Sub2API (OpenAI 兼容接口)
└── 其余路由                 → Sub2API
```

### 组件

| 组件 | 镜像 | 说明 |
|------|------|------|
| **Sub2API** | `weishaw/sub2api:latest` | API 网关、用户管理、通道管理、支付 |
| **NextChat** | `yidadaa/chatgpt-next-web` | 网页聊天对话 |
| **PostgreSQL** | `postgres:15-alpine` | Sub2API 数据库 |
| **Redis** | `redis:8-alpine` | 缓存 |
| **Caddy** | `caddy:2-alpine` | 反向代理，统一域名 |

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 修改 .env 中的 EXTERNAL_URL 为你的实际域名/IP
```

### 2. 启动

```bash
docker compose up -d
# 等待所有容器 healthy（约 60 秒）
```

### 3. 初始化菜单

```bash
bash setup.sh
```

### 4. 使用

浏览器访问 `http://<你的IP>:8000`

1. 用管理员账号登录（默认 `admin@example.com` / `admin123456`）
2. 首次登录需接受合规声明
3. 左侧导航栏出现 **Chat** 按钮
4. Admin → Channels → 添加 AI 供应商（否则聊天会返回余额不足）

## 用户流程

1. **注册/登录 Sub2API** → 自动获得 API Key
2. 点击侧栏 **Chat** → 引导页自动创建 API Key 并写入 NextChat
3. 在 NextChat 中直接对话，无需手动配置

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EXTERNAL_URL` | `http://localhost` | 外部访问地址 |
| `HTTP_PORT` | `8000` | 对外端口 |
| `POSTGRES_USER` | `sub2api` | 数据库用户 |
| `POSTGRES_PASSWORD` | `sub2api_pg_dev` | 数据库密码 |
| `SUB2API_ADMIN_EMAIL` | `admin@example.com` | 管理员邮箱 |
| `SUB2API_ADMIN_PASSWORD` | `admin123456` | 管理员密码 |
| `NEXTCHAT_CODE` | (空) | NextChat 访问密码 |

## 设计要点

### 同域集成

通过 Caddy 将 NextChat 和 Sub2API 置于同一域名下，消除跨域问题：

- NextChat 部署在 `/chat` 子路径
- Sub2API 作为 `/api/*` 和 `/v1/*`
- Sub2API 的 OpenAI 兼容接口供 NextChat 后台调用

### 自动 API Key 绑定

`/chat-bootstrap` 引导页面：

1. 从 URL 读取用户 JWT Token
2. 调用 Sub2API 创建/获取 Chat 专用 API Key
3. 写入浏览器的 localStorage（NextChat 的配置存储）
4. 跳转到 NextChat 首页，即可直接对话

### 最小改动原则

所有组件均使用官方 Docker 镜像，**零修改**。集成逻辑通过 Caddy 路由和纯前端引导页实现，无自定义镜像、无 Sidecar、无额外的后台任务。
