# T3 集成栈手工冒烟清单（SHAN-342 R1-R4/CR2 交付后执行）

适用：`docker compose up -d` 拉起集成栈后的运行时验收。自动化守卫（tests/env-guard.sh、
tests/caddy-routes-test.sh）覆盖不了的浏览器链路与人机交互项在此清单人工核对。
每项通过打勾并记录执行日期与栈基线 SHA；任何一项不过不得宣称集成栈可用。

## 1. 容器与健康

- [ ] `docker compose ps` 全部服务 Up 且 healthy（sub2api/postgres/redis/caddy/nextchat）
- [ ] `docker compose logs --since 5m` 无 crashloop、无迁移报错

## 2. 网关路由（对应 R1/R2）

- [ ] `curl -s http://<EXTERNAL_URL>/health` 返回 200（sub2api /health 经 handle /api/* 之外的健康探测路径可用）
- [ ] 浏览器打开 `http://<EXTERNAL_URL>/` 能加载 Sub2API 前端
- [ ] 浏览器打开 `http://<EXTERNAL_URL>/api/config` 返回 NextChat 配置 JSON（R2 合并路由）
- [ ] NextChat 页面静态资源 `/_next/*` 正常加载、无 404（R2 合并路由）
- [ ] `http://<EXTERNAL_URL>/v1/models` 带 API Key 返回模型列表（裸代理路径）

## 3. Chat 引导链路（对应 R3）

- [ ] 以普通员工登录 Sub2API，取会话 token
- [ ] 打开 `<EXTERNAL_URL>/chat-bootstrap?token=<token>`：页面自动完成查 key/建 key，跳转 `/chat` 后 NextChat 可对话
- [ ] 已有 `chat-key` 的账号重复进入引导页：不重复建 key，直接可用（findChatKey 复用路径）
- [ ] 左侧菜单出现 setup.sh 注入的 Chat 入口（对应 R4 后重跑 setup.sh 的场景）

## 4. 数据库与初始化（对应 R4）

- [ ] `setup.sh` 在 `.env` 自定义 `POSTGRES_USER`/`POSTGRES_DB` 的栈上可跑通（不再硬编码 sub2api）
- [ ] `settings.custom_menu_items` 注入成功且 `docker restart sub2api` 后菜单仍生效

## 5. 环境键（对应 T1）

- [ ] 复制 `.env.example` 为 `.env` 后 `docker compose config` 通过；`POSTGRES_PASSWORD`/`JWT_SECRET`/`TOTP_ENCRYPTION_KEY`/`REDIS_PASSWORD` 按注释指引手填后栈可启动
