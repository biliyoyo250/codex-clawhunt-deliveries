# 工单 #2494 验收记录

实际测试源码 commit：`b566240f65fa98acbd80373c001e87a6ab199aba`。

CI：[Task 2494 acceptance](https://github.com/biliyoyo250/codex-clawhunt-deliveries/actions/runs/44778431678)，运行编号 `44778431678`，结论 success。源码、迁移、依赖和工作流均来自该 commit。随后新增的交付证据文件不改变这份已测试的代码。

## 实际环境与结果

- Ubuntu 24.04，Python 3.12.16，PostgreSQL 16.16，pgvector 0.8.7。
- FastAPI 0.165.0、SQLAlchemy 2.1.9、Alembic 1.20.1、psycopg 3.3.9。
- `alembic upgrade head` → `downgrade base` → `upgrade head` 全部成功。
- `alembic check`：`No new upgrade operations detected.`
- `python -m pytest tests --junitxml=evidence/junit.xml -vv`：**134 passed，0 failed，0 errors，0 skipped**。
- 其中独立 QA 编写的测试 98 项；真实 PG16 持久化、并发、权限及恢复测试共 55 项（独立 QA 42，制作方 13）。其余为纯规则、HTTP 验证和真实 worker 循环编排检查。
- 一条 Starlette/httpx 弃用提示；没有测试失败。完整依赖版本见原始日志。

## 验收项与复核入口

| 验收点 | 实际测试入口 |
|---|---|
| 满 24/48 小时的对象、指定经理独占、负责人消息五类字段 | `test_pg_independent_thresholds_and_persisted_repeat`、`test_pg_independent_target_vs_group_and_48_payload` |
| 上海日期第三天、已过期日期、急件标记与负责人提醒 | `test_pg_independent_shanghai_urgent_persists_both_states`、独立纯规则跨午夜测试 |
| 每档持久幂等、重复扫描、并发扫描和并发派送 | `test_pg_independent_concurrent_scanners_single_durable_event`、`test_pg_independent_concurrent_dispatchers_do_not_duplicate` |
| 原始生成时间领取时长、严格超过 24h、预读旧状态回归 | `test_pg_independent_claim_uses_original_generation_not_state_clock`、`test_pg_independent_claim_exceeded24_is_strict`、三个 stale_preload 回归 |
| 补料 48h 主管、状态重置、旧队列取消、同状态不重计时 | returned_supervisor_boundary、transition_cancels_old_epoch_pending_delivery、new_epoch_rearms_but_same_state_does_not |
| 配置默认、修改生效、组织/会所/角色权限、整数上界 | config_defaults_scoped_update_and_effect、config_denied_requests_never_mutate、authorized_config_http_and_overflow_rejected |
| 发送失败、接受后确认失败、确认事务回滚后重启 | delivery_failure_restart_stable_key、committed_outbox_survives_service_restart、hard_crash_after_accept_rolls_back_ack_transaction |
| 每半小时调度、启动补扫、扫描异常后继续 | `test_independent_actual_worker_loop_catches_failure_and_keeps_shanghai_schedule` |

所有完整用例名与逐例结果见 `actual-ci/pytest.log` 和 `actual-ci/junit.xml`。测试只使用虚构数据、独立测试数据库和本地通知桩；未调用真实业务通知服务。

## 原始证据

`actual-ci/postgres.log`、`environment.log`、`install.log`、`migrations.log`、`pytest.log`、`junit.xml` 均为上述实际 CI artifact 原始字节。各文件 SHA-256 与 artifact 来源记录见 `acceptance-summary.json`。本地跳过 PG 的运行没有计为 PG 通过。

## 对接说明

详见根目录 `README.md` 和 `INTEGRATION.md`。上游交接模型、可信认证主体、收件目录和通知接口为显式适配边界；本单已按需求使用桩，不需要客户生产账号。生产通知接口必须持久去重同一幂等键，并设置有界网络超时；否则不能把任意外部故障下的效果称为 exactly-once。

源码由制作子代理编写，独立测试由另一真实子代理编写，另经静态审计和主代理终审；作者及测试哈希见 `authorship.json`。最终交付状态以平台回读为准，测试通过不代表买家已验收或报酬已结算。
