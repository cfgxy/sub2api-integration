# Sub2API 本地多槽位测试基础设施 — 交接文档

本文档与 `local/worktree.sh` / `local/slotctl.py` 的实际行为冲突时，**以脚本行为为准**。

## 1. 唯一入口

所有槽位生命周期操作（申领/绑定镜像与基线/启动/健康检查/查看状态/停止/释放）
一律通过：

```
./local/worktree.sh <slot> <action> [issue] [--image <ref>] [--expect-sha <sha>]
```

`action` 取值：`claim` `release` `status` `list` `up` `down` `health`。

**禁止**直接对 `local/docker-compose.slot.yml` 执行 `docker compose` 手工操作槽位——
该文件仅是被 `worktree.sh` 用生成好的 `.env` 参数化调用的模板，绕过入口直接操作
会跳过 ownership 复验与 SHA 校验，判定为违规路径。

**禁止**手工删除 `local/<slot>/.slot-lock/` 下的文件来"绕过占用"——占用与释放
只能通过 `worktree.sh <slot> release <issue>` 或竞争接管（见第 3 节）完成。

## 2. 槽位清单与端口/资源映射

槽位定义见 `local/slots.json`；6 个槽位相互隔离（各自独立的 app/postgres/redis/minio
容器、数据卷与监听端口），无共享数据库或 Redis 实例。

| 槽位 | 类型 | app 端口 | postgres 端口 | redis 端口 | minio API | minio 控制台 |
|------|------|----------|----------------|------------|-----------|---------------|
| dev1 | dev  | 18101    | 15401          | 16401      | 19101     | 19201         |
| dev2 | dev  | 18102    | 15402          | 16402      | 19102     | 19202         |
| dev3 | dev  | 18103    | 15403          | 16403      | 19103     | 19203         |
| qa1  | qa   | 18104    | 15404          | 16404      | 19104     | 19204         |
| qa2  | qa   | 18105    | 15405          | 16405      | 19105     | 19205         |
| qa3  | qa   | 18106    | 15406          | 16406      | 19106     | 19206         |

所有端口均绑定 `127.0.0.1`，不对外暴露。每个槽位的数据卷命名为
`sub2api_slot_<slot>_{app,postgres,redis,minio}_data`，容器命名为
`sub2api-slot-<slot>-{app,postgres,redis,minio}`。

## 3. 租约规则（无状态、与 Multica Issue 绑定）

Owner 记录固定为两个字段，落盘于 `local/<slot>/.slot-lock/owner.json`：

```json
{"environment": "<slot>", "issue": "<issue-key>"}
```

不含分支、worktree、仓库、commit、manifest、候选/生效/待定、回执、代数、TTL、心跳
或恢复相关字段——按 ADR-014 的无状态结论实现，禁止再引入这些字段。

**接管规则**：`claim` 遇到已有 owner 时，查询 `multica issue get <owner-issue>
--output json`：
- `status=in_progress` → 拒绝接管；
- `todo`/`blocked`/`in_review`/`done`/`cancelled` → 允许新 issue 接管；
- 查询失败或返回不可解析 → 判定为接管失败，**零写入**，返回可重试错误；
- 写入前发现 owner 记录已被并发修改 → 同样零写入并重试。

原子竞争通过 `fcntl.flock` 独占锁保证同一槽位在并发申领下只有一个赢家
（见 `local/tests/test_slotctl.py::test_concurrent_claim_only_one_winner_on_empty_slot`，
真实多线程验证）。

## 4. 加载与验证流程（推荐操作序列）

```bash
export SUB2API_CALLER_OWNER=SHAN-XXX
./local/worktree.sh dev1 claim SHAN-XXX
./local/worktree.sh dev1 up SHAN-XXX --image <镜像引用> --expect-sha <完整40位SHA>
./local/worktree.sh dev1 health
# ... 开发/测试 ...
./local/worktree.sh dev1 down SHAN-XXX
./local/worktree.sh dev1 release SHAN-XXX
```

`up` 的执行顺序：先复验 ownership → 用 `docker inspect` 探测目标镜像的
`org.opencontainers.image.revision` label 并与 `--expect-sha` 比对（不一致直接拒绝，
不产生任何容器）→ 渲染槽位 `.env`（0600 权限，密钥经 SHA256 派生，仅限本地测试用途，
**非生产安全强度**）→ 再次复验 ownership → `docker compose up -d` → 落盘
`.expected-sha` 供后续 `health` 复核。

**关于基线 SHA 的关键说明**：`--expect-sha` 指定的期望 tip **不要求是 `main` 的祖先**——
开发者本地已提交但尚未 push 的交付 SHA 同样是合法基线，脚本不做"是否可达 main"的校验。
这是刻意的设计（避免商会此前踩过的坑：要求基线必须是远端可达提交，导致本地未推送的
真实交付无法验证）。

`health` 会同时检查 app/postgres/redis/minio 四项服务健康状态，并将容器内探测到的
镜像 revision label 与落盘的 `.expected-sha` 比对；任一服务不健康或 SHA 不匹配，
`ready` 字段返回 `false`，不得据此宣称槽位就绪。

## 5. 故障恢复路径

- **claim 失败（owner 查询异常）**：不产生任何文件变更，直接重试 `claim`；若持续
  失败，检查 `multica` CLI 凭据与网络，不得手工写 `owner.json` 绕过。
- **up 失败（SHA 不匹配）**：不会启动容器；确认 `--expect-sha` 与实际镜像 label 是否
  一致后重试，禁止改小校验范围或跳过校验重跑。
- **容器异常退出/健康检查持续失败**：先 `./local/worktree.sh <slot> down <issue>`
  停止，检查日志（`docker logs sub2api-slot-<slot>-<service>`，注意脱敏后再外发），
  修复后重新 `up`。
- **ownership 冲突（并发写入被拒绝）**：属于设计内的 fail-closed 行为，重新执行
  `claim`/`up`/`down`/`release`，脚本会基于最新 owner 状态重新判定，不需要人工介入
  锁文件。

## 6. 明确禁止事项

- 禁止直接 `docker compose -f local/docker-compose.slot.yml ...` 手工操作槽位。
- 禁止手工编辑或删除 `local/<slot>/.slot-lock/owner.json` 来抢占或释放槽位。
- 禁止在 `owner.json` 中新增字段（分支、commit、TTL、心跳等）——违反 ADR-014
  无状态结论。
- 禁止把槽位 `.env`、数据库密码、`JWT_SECRET`、`TOTP_ENCRYPTION_KEY`、MinIO 凭据
  写入 Issue 评论、日志、命令行历史或 Git；探测输出必须脱敏后才可外发。
- 禁止跨槽位共享或复用数据卷；`down` 默认不清理卷，如需清理数据卷须先确认
  ownership 且不影响其他槽位/正在进行中的 issue 后手工评估（当前实现不提供
  一键清卷命令，避免误删）。

## 7. 与既有 `shan151-sub2api-enterprise-{dev,qa}` 容器组的关系（迁移说明）

现状核查（2026-09-16 实测）：`shan151-sub2api-enterprise-dev-*` 与
`shan151-sub2api-enterprise-qa-*` 容器组仍在运行且健康；`qa-app.env` 已被
SHAN-267/SHAN-268/SHAN-240/SHAN-239 等在制 issue 的独立 worktree 各自复制一份在用。

**结论：本次交付不迁移、不停止、不复用这组既有容器**（coexist，不 adopt、不
retire）。理由：

1. 这组容器当前由其他在制 issue 占用，停止或改造存在破坏其工作的不可逆风险，
   超出本 issue 授权范围（本角色仅能操作与本 issue 绑定的本地环境）。
2. 新的槽位机制（dev1-3/qa1-3）与旧容器组端口、容器名、卷名均不重叠，可与旧
   容器组并行运行，不产生资源冲突。

**后续迁移建议（留给 Leader/Owner 决策，本次不执行）**：
- 待 SHAN-267/268/240/239 等占用旧容器组的 issue 全部关闭后，可将旧容器组的数据
  按需导入 `dev1`/`qa1`（跑一次性 `pg_dump`/`pg_restore` 及 MinIO 数据同步），随后
  `docker rm`/`docker volume rm` 旧容器组与其卷，退休 `qa-app.env` 散装副本模式；
  或者直接退休旧容器组、要求新 issue 一律走新槽位机制，旧容器数据按需求确定是否
  归档后删除。
- 不建议直接"adopt"旧容器into新模型（如把 `shan151-sub2api-enterprise-dev-app`
  重命名进槽位命名规范）：旧容器不是由 `docker compose` 启动（无 compose 标签），
  与本槽位模板的 compose 生命周期管理方式不兼容，强行接管需要额外的一次性迁移脚本，
  超出本 issue 范围，如需要请由 Leader 另开 issue 明确授权范围。

## 8. 与商会 SaaS `local/` 控制面的差异（刻意不照搬部分）

- 不采用商会共享 DB/Redis + 每槽位 DB-index 偏移的隔离方式，改为每槽位独立全套
  容器（app/postgres/redis/minio），因 Sub2API 技术栈更轻量，issue 明确允许按实际
  技术栈裁剪。
- 不移植商会前端"不可变产物发布"机制（`dist-deploy`/`dist-status`/`dist-rollback`）：
  Sub2API 把前端打进单一 Docker 镜像，没有独立前端产物，该机制不适用。
- Owner 记录字段严格按 ADR-014 的无状态结论实现（两字段），不沿用商会历史脚本中
  candidate/active/pending/receipt/generation/TTL/heartbeat 等字段。
