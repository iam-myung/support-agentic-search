# S17 试点回退 Runbook（REQ-010 / AC-013；PRD §4.3）

本文件描述单企业试点环境中切回上一兼容应用镜像与配置的步骤。
不宣称多区域 HA；计时门槛以 PRD §4.3 为准，不得在本文件放宽。

## 前置

- 已批准目标主机、镜像标签、备份目录（默认 `.backup/s17/`）。
- 部署前已完成数据库/知识卷备份（`python -m support_platform.deploy.backup` 或运维等价物）。
- 记录当前运行镜像标签与上一稳定镜像标签（previous image）。

## 回退到上一镜像（previous image rollback）

1. 停止当前 `web` / `worker`（保留 `db` / `redis` 除非数据恢复需要一并处理）：
   `docker compose stop web worker`
2. 将 compose/环境中的应用 `image`（或 build 产物标签）切回上一兼容镜像，例如：
   `support-platform:pilot-previous`（以部署记录中的 previous image 为准）。
3. 若本次迁移不可逆且已确认需数据回退：先从备份恢复数据库/知识卷到备份时点，再启动应用。
4. 启动上一镜像的 worker 与 web：
   `docker compose up -d worker web`
5. 验证就绪：
   - `GET /health/live` → 200 `{status: alive}`
   - `GET /health/ready` → 200（配置 + PostgreSQL + Redis）
6. 在 PRD §4.3 时限内完成就绪检查与真实入口 smoke；失败则保持回退态并上报。

## 说明

- Redis 不可用时任务真相仍在 PostgreSQL；ready 必须报 `not_ready`，不得用 live 冒充。
- 备份时点之后的新任务不承诺由旧备份找回。
