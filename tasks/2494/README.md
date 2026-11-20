# Handover timeout reminders — task 2494

Python 3.11+ / FastAPI / SQLAlchemy 2 / Alembic / PostgreSQL **16** reference implementation. It contains the half-hour worker, scoped settings API, state and claim integration hooks, a transactional outbox and an explicitly local notification stub. All examples and test records are fictional.

## Run

Use a dedicated PostgreSQL 16 database. The reference migration creates four `hr_` tables. The server may have pgvector installed; this task does not require vector operations or Redis. No outbound data SDK is used. Database, directory and authentication configuration comes from the environment or explicitly injected trusted adapters.

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
export PYTHONPATH=src
export DATABASE_URL='postgresql+psycopg://localhost/handover_reference'
python -m alembic upgrade head
```

PowerShell equivalents are `$env:PYTHONPATH='src'` and `$env:DATABASE_URL='postgresql+psycopg://localhost/handover_reference'`. Authentication/passwords belong in your deployment environment, never source files, logs or delivery evidence. Do not point test cleanup at an integration or production database.

Provide the reference directory as a JSON environment value, or inject your platform's directory adapter:

```sh
export HANDOVER_DIRECTORY_JSON='{"sites":[{"org_id":"00000000-0000-0000-0000-000000000001","site_id":"00000000-0000-0000-0000-000000000002","manager_group_id":"fictional-group","site_lead_id":"00000000-0000-0000-0000-000000000004"}],"supervisors":[{"org_id":"00000000-0000-0000-0000-000000000001","site_id":"00000000-0000-0000-0000-000000000002","consultant_id":"00000000-0000-0000-0000-000000000005","supervisor_id":"00000000-0000-0000-0000-000000000006"}]}'
python -m handover_reminders once
python -m handover_reminders worker
```

The worker catches up once at startup, then runs on the next **Asia/Shanghai :00 and :30** boundaries. Restart after downtime is safe. More than one worker is supported. A system process manager should restart a terminated worker. An existing task scheduler may instead invoke `once` every 30 minutes with the same timezone. `clock.next_scan_after` and the injectable `FrozenClock` make alignment and business time testable.

## Business rules

| Condition in current state epoch | Notification destination | Dedup level |
|---|---|---|
| `pending_claim`, elapsed ≥ configured first threshold (default 24h) | Specified target manager **only**, otherwise this site's manager group | `pending_24` |
| `pending_claim`, elapsed ≥ escalation threshold (default 48h) | This site's lead | `pending_48` |
| Active unclaimed state, planned move-in Shanghai date minus Shanghai today ≤ configured days (default 3) | This site's lead, `urgent=true` | `urgent` |
| `returned_for_info`, elapsed ≥ configured threshold (default 48h) | The consultant's supervisor in the same org/site | `returned_48` |

Both `pending_claim` and `returned_for_info` are active unclaimed states. Overdue move-in dates remain urgent. Calendar dates, including the exact third day, determine urgency; elapsed 72 hours is not a substitute. Missing move-in date produces no urgency reminder. A late first scan catches up all applicable tiers: at 48 hours, both first and escalation reminders are queued, plus urgency if applicable. Each tier is delivered at most once per handover **per state epoch**.

Every notification snapshot includes company name, consultant ID/name, signing timestamp, planned move-in date, current-state wait duration (decimal minutes and hours), recipient, org/site IDs, handover ID, epoch and original generation time. Directory lookup failure is visible in scan output and retried on later scans; it does not record a successful notification. Soft-deleted, claimed or cancelled handovers are excluded.

True status changes reset `status_changed_at`, `notified_levels`, and `urgent`, increment `status_epoch`, and cancel undelivered messages from the previous epoch. Same-status retries and ordinary metadata edits do not restart the clock. A database trigger provides the same protection for direct SQL status updates. Service hooks inject the frozen UTC instant through a transaction-local setting; direct SQL without that setting uses the database clock. Original `generated_at` and handover org/site are immutable.

On claim, `handover_duration_minutes = (claimed_at - generated_at) / 60 seconds`, stored to nine decimal places. **The baseline is original generation, even after a state reset.** `exceeded_24h` means strictly greater than 24h; exactly 24h is false. This dashboard flag remains a fixed 24h measurement even if reminder settings change. Repeating a claim preserves the first claim timestamp. Reopening an unclaimed state clears the current claim fields and retains prior entries in `claim_history`.

## Integration boundaries

The `Handover` ORM model is a reference adapter for the upstream handover order. Map its `generated_at` to the upstream **original generation timestamp**, and preserve `status_epoch` / `status_changed_at` when integrating. Populate required company/consultant/signing/plan/target fields from the actual order service. There is no customer account, real directory, customer dataset, or notification credential in this package.

- `service.enqueue_due_notifications(session, now, recipients)` queues reminders within the caller's transaction.
- `service.dispatch_outbox(session_factory, gateway, now)` retries due persisted messages.
- `service.scan_handover_timeout(session_factory, gateway, recipients, clock)` performs both steps.
- `service.transition_handover(session, id, new_status, now, actor_id=None)` handles non-claim status transitions.
- `service.claim_handover(session, id, claimed_at, actor_id=None)` handles claim measurements.
- `directory.RecipientDirectory` resolves site groups, site leads, and consultant supervisors. The supplied `StaticRecipientDirectory` is a local fixture adapter. Missing mapping is an error, not a fallback to another site's user.
- `notifications.NotificationGateway.send(message, idempotency_key)` is the notification API contract. The supplied `DatabaseNotificationStub` records local accepted messages in `hr_notification_receipt_stub` using an independent commit. It sends no actual email, chat or webhook.

Lower-level service hooks are for trusted application code: authorize the caller before invoking them. The supplied API enforces org/site scope and a `handover_operator` or `site_lead` role for state hooks. The database trigger protects scope and generation-time immutability.

### Delivery consistency and recovery

The persistent outbox has unique `(handover_id, status_epoch, level)` and a stable key containing org/site/handover/epoch/level. Concurrent scanners take PostgreSQL `FOR UPDATE SKIP LOCKED` handover locks. Dispatch and transitions use the same **handover → outbox** lock order. Dispatch verifies the current epoch, state and scope and holds the handover lock through acceptance and local acknowledgement, so a transition cannot overtake a send.

`notified_levels` records accepted sends, not queued attempts. A failed send increments attempts, leaves the marker unset and retries with bounded backoff (1, 2, 4, 8, 16, then 30 minutes; later attempts remain at 30). No attempt limit silently drops a reminder. Gateway exception text is not persisted because it could contain secrets. Monitor failed counts and directory errors. Repeated scans safely recover pending work. Configuration changes apply to new queue decisions; an already queued message retains its original payload and destination until delivered or its epoch is cancelled.

**The real notification API must durably deduplicate the supplied idempotency key.** If acceptance succeeds but the worker loses the acknowledgement or crashes before its local commit, the exact same key is retried. The durable stub proves this recovery without accepting a second message. At-most-once acceptance is conditional on that downstream idempotency contract; a non-idempotent remote API cannot offer exactly-once delivery through an arbitrary process/network failure. The stub receipt intentionally has no handover FK, so its independent commit does not wait on the scanner's locked handover row. The adapter must have a bounded I/O timeout: dispatch holds a row lock during that call.

### Settings and authenticated API

`api.create_app(session_factory, clock=None, principal_provider=None)` builds the API. It fails closed with HTTP 401 until the platform injects a **verified** `service.Actor(org_id, site_id, user_id, roles)` principal. It does not trust client-supplied role/tenant headers. Adapt the existing authenticated platform identity through `principal_provider` (or FastAPI's dependency override for `trusted_principal`); do not turn unverified headers into a production authentication mechanism.

- GET/PUT `/orgs/{org_id}/sites/{site_id}/handover-reminder-config`
- POST `/orgs/{org_id}/sites/{site_id}/handovers/{handover_id}/transition`, JSON `{"status":"returned_for_info"}`
- POST `/orgs/{org_id}/sites/{site_id}/handovers/{handover_id}/claim`

Only a `site_lead` in **the same org and site** may change settings. GET also requires matching scope. Request fields are strict integers: first >0, escalation >first, returned >0, urgent ≥0, all within PostgreSQL's signed integer range. Invalid requests return 422; wrong scope/role returns 403. Defaults are 24/48/48 hours and 3 days if no active config exists. The unique org/site row is atomically upserted; an authorized write may reactivate a soft-deleted config. State and claim HTTP hooks use the injected server clock rather than a user-supplied timestamp.

An ASGI server may host the app as part of your existing FastAPI application. No standalone open-auth server is launched by this package.

## Tests and acceptance evidence

Pure frozen-clock and HTTP-auth tests:

```sh
python -m pytest tests/test_unit.py -q
```

Full suite, including **real PostgreSQL 16** persistence/concurrency/migration tests:

```sh
export DATABASE_URL='postgresql+psycopg://localhost/handover_2494_test'
export REQUIRE_POSTGRES=1
export HANDOVER_TEST_ALLOW_DESTRUCTIVE=1
python -m alembic upgrade head
python -m alembic downgrade base
python -m alembic upgrade head
python -m alembic check
python -m pytest tests -vv --junitxml=evidence/junit.xml
```

`HANDOVER_TEST_ALLOW_DESTRUCTIVE=1` permits test fixture `TRUNCATE` in the **dedicated test database**. Do not set it for production. Without a database, producer PG tests are explicitly skipped locally; CI sets `REQUIRE_POSTGRES=1` and fails if PG16 is unavailable. SQLite is never used as an acceptance substitute. Separate independent QA tests are placed in `tests/independent/` before the acceptance run.

`.github/workflows/task-2494.yml` is a manual-only GitHub Actions workflow. When this package resides at `tasks/2494`, place that workflow at repository `.github/workflows/task-2494.yml`. It runs Ubuntu 24.04, Python 3.12, a PG16 pgvector service, migration upgrade/downgrade/upgrade, drift check, all tests, and stores JUnit plus raw dependency/version/migration/test logs. Actions are pinned to official release commit SHAs. It has `contents: read` only and does not deploy, use customer accounts or send real notifications. A green generic environment probe is not task acceptance: use the run for this exact source commit and its actual test evidence.

`evidence/` is populated by actual runs. A run link/commit and test results should accompany delivery. Do not label a skipped PG suite as passed or an estimated work duration as measured labor.
