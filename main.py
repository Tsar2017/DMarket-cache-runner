from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


REQUIRED_SOURCES = {"RentFaster", "Rentals.ca"}
UNIT_TYPES = ("Bachelor", "1 bedroom", "2 bedroom", "3 bedroom", "4 bedroom")
MARKET_CACHE_PAYLOAD_VERSION = 2


def configure_environment() -> None:
    payload = json.loads(
        base64.urlsafe_b64decode(os.environ["GITHUB_TOKEN"]).decode()
    )
    os.environ.update(payload)
    os.environ["HEADLESS_BROWSER"] = "true"
    os.environ["MARKET_CACHE_TTL_SECONDS"] = "21600"
    os.environ["MAX_LINK_CHECKS_PER_SOURCE"] = "1"
    os.environ["LINK_CHECK_SECONDS"] = "4"
    os.environ["MARKET_CACHE_REQUIRED_SOURCES"] = ",".join(sorted(REQUIRED_SOURCES))
    os.environ["CHROME_PATH"] = next(
        (
            path
            for path in (
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
            )
            if Path(path).exists()
        ),
        "google-chrome",
    )
    os.environ["CHROMEDRIVER_PATH"] = (
        "/usr/bin/chromedriver" if Path("/usr/bin/chromedriver").exists() else ""
    )


def cache_is_runner_fresh(
    existing: dict | None, now: datetime | None = None
) -> bool:
    if not existing or not existing.get("collectedAt"):
        return False
    collected_at = datetime.fromisoformat(
        existing["collectedAt"].replace("Z", "+00:00")
    )
    sources = {
        row.get("sourceWebsite") for row in existing.get("candidates", [])
    }
    candidates = existing.get("candidates", [])
    payload_is_current = bool(candidates) and all(
        row.get("marketCachePayloadVersion") == MARKET_CACHE_PAYLOAD_VERSION
        for row in candidates
    )
    current_time = now or datetime.now(timezone.utc)
    return (
        current_time - collected_at < timedelta(hours=4)
        and REQUIRED_SOURCES <= sources
        and payload_is_current
    )


def record_fresh_skip(
    cache,
    unit_type: str,
    record_skip: Callable | None = None,
) -> str:
    if record_skip is None:
        from refresh_market_cache import record_skipped_job

        record_skip = record_skipped_job
    job_id = str(uuid.uuid4())
    cache.create_job(job_id, unit_type)
    record_skip(cache, job_id)
    return job_id


def refresh_one_unit_type(cache, unit_type: str) -> bool:
    existing = cache.load_latest(unit_type)
    if cache_is_runner_fresh(existing):
        job_id = record_fresh_skip(cache, unit_type)
        print(
            f"isolated_refresh_skip_fresh unit_type={unit_type!r} job_id={job_id!r}",
            flush=True,
        )
        return True

    for attempt in range(1, 4):
        profile = Path("/tmp") / (
            "dmarket-" + unit_type.replace(" ", "-") + f"-{attempt}"
        )
        shutil.rmtree(profile, ignore_errors=True)
        environment = os.environ.copy()
        environment["DMARKET_PROFILE_DIR"] = str(profile)
        print(
            f"isolated_refresh unit_type={unit_type!r} attempt={attempt}",
            flush=True,
        )
        if (
            subprocess.call(
                [sys.executable, "refresh_market_cache.py", unit_type],
                env=environment,
            )
            == 0
        ):
            return True
        time.sleep(5)
    return False


def main() -> int:
    configure_environment()
    from supabase_cache import SupabaseMarketCache

    cache = SupabaseMarketCache()
    failed = [
        unit_type
        for unit_type in UNIT_TYPES
        if not refresh_one_unit_type(cache, unit_type)
    ]
    if failed:
        print(
            "isolated_refresh_failed unit_types=" + ", ".join(failed),
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())