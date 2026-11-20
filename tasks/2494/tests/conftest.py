"""Real-PG-only integration fixture. Destructive cleanup requires explicit test opt-in."""
import os

import pytest
from sqlalchemy import text

from handover_reminders.db import make_session_factory


@pytest.fixture
def pg_sessions():
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    required = os.getenv("REQUIRE_POSTGRES") == "1"
    if not url:
        if required:
            pytest.fail("REQUIRE_POSTGRES=1 requires a real PostgreSQL 16 test database")
        pytest.skip("PostgreSQL integration requires dedicated DATABASE_URL")
    if os.getenv("HANDOVER_TEST_ALLOW_DESTRUCTIVE") != "1":
        pytest.fail("Use a dedicated test database and set HANDOVER_TEST_ALLOW_DESTRUCTIVE=1")
    sessions = make_session_factory(url)
    with sessions.begin() as session:
        version = int(session.scalar(text("SHOW server_version_num")))
        assert 160000 <= version < 170000, f"PG16 required; got {version}"
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "2494_0001"
        session.execute(text("TRUNCATE hr_notification_receipt_stub, hr_notification_outbox, hr_reminder_config, hr_handover CASCADE"))
    yield sessions
    sessions.kw["bind"].dispose()
