# Integration and acceptance map

| Required behavior | Implementation | Producer verification | Additional independent verification |
|---|---|---|---|
| Exact 24/48h destinations; all five escalation payload fields | `rules.py`, `directory.py`, `service.py` | unit boundaries and PG `test_late_scan_catches_all_tiers_and_persists_once` | `tests/independent/` frozen-time checks |
| Shanghai third calendar day, overdue dates, urgent flag | `clock.py`, `rules.due_levels`, `enqueue_due_notifications` | parameterized calendar tests; persisted PG urgent | independent calendar boundary cases |
| Once per level/epoch across repeated or concurrent scans | outbox unique constraints, stable key, PG row locks | PG repeat/concurrent scanner tests | independent concurrency and recovery |
| Returned for info ≥48h to consultant supervisor | recipient directory + `returned_48` | PG status/supervisor test | independent returned-time boundary cases |
| State changes reset clocks/markers and cancel old queued sends | Alembic status trigger + state hooks | PG reset/reentry/direct-SQL tests | independent stale-outbox cases |
| Duration from original generation; strict >24h | claim hook + status trigger | PG exact24/24+1µs/original-generation/history tests | independent claim reset/baseline checks |
| Default/configurable thresholds; site lead only within own org/site | `Thresholds`, `api.py`, scoped atomic upsert | unit validation/auth and PG settings/API tests | independent cross-scope/role/no-change tests |
| Fail before send or after acceptance/before local marker | persistent outbox + independent durable receipt | PG two failure-window cases | independent crash/retry tests |
| Real PG16, migrations and reproducible execution | `migrations/`, manual workflow, requirements | upgrade/downgrade/upgrade + drift check + required-PG suite | same CI run includes separate test author |

The directory, authentication principal and notification gateway are explicit adapters. The reference handover table is provided because the upstream order model is a separate integration dependency. Replace/merge adapters during platform integration; retain org/site scope, original generation timestamp, state epoch and downstream idempotency semantics. No external customer account or production environment is required to reproduce this package's acceptance tests.

Evidence claims must refer to an actual run for the delivered commit. This map lists test coverage, not a declaration that a yet-unrun PG test passed. Actual logs and JUnit from the manual workflow are the verification record.
