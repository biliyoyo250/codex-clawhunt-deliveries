import argparse
import json
import logging
import os
import time
from uuid import UUID

from .clock import SystemClock, next_scan_after
from .db import make_session_factory
from .directory import StaticRecipientDirectory
from .notifications import DatabaseNotificationStub
from .service import scan_handover_timeout


def directory_from_env():
    """Local integration fixture, not customer directory discovery or outbound SDK."""
    raw = json.loads(os.environ.get("HANDOVER_DIRECTORY_JSON", "{}"))
    directory = StaticRecipientDirectory()
    for item in raw.get("sites", []):
        key = UUID(item["org_id"]), UUID(item["site_id"])
        directory.groups[key] = item["manager_group_id"]
        directory.leads[key] = UUID(item["site_lead_id"])
    for item in raw.get("supervisors", []):
        key = UUID(item["org_id"]), UUID(item["site_id"]), UUID(item["consultant_id"])
        directory.supervisors[key] = UUID(item["supervisor_id"])
    return directory


def run_worker(session_factory, gateway, recipients, clock=None, *, stop=None, sleep=time.sleep):
    clock = clock or SystemClock()
    logger = logging.getLogger(__name__)
    # One startup catch-up covers downtime, then run on local :00 / :30 boundaries.
    while stop is None or not stop():
        try:
            result = scan_handover_timeout(session_factory, gateway, recipients, clock)
            logger.info("handover_scan %s", json.dumps(result, sort_keys=True))
        except Exception:
            # Remain alive after a transient DB/adapter failure. Avoid logging secrets.
            logger.error("handover_scan_failed; retry at next half-hour boundary")
        now = clock.now()
        delay = max(0.01, (next_scan_after(now) - now).total_seconds())
        sleep(delay)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["once", "worker"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sessions = make_session_factory()
    clock = SystemClock()
    gateway = DatabaseNotificationStub(sessions, clock)
    directory = directory_from_env()
    if args.command == "once":
        print(json.dumps(scan_handover_timeout(sessions, gateway, directory, clock), sort_keys=True))
    else:
        run_worker(sessions, gateway, directory, clock)


if __name__ == "__main__":
    main()
