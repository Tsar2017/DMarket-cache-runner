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
SOURCE_STATUS_REQUIRED_FOR_FRESH_SKIP = {"Apartments.com"}
SUCCESSFUL_SOURCE_STATUSES = {"verified", "no-match"}
UNIT_TYPES = ("Bachelor", "1 bedroom", "2 bedroom", "3 bedroom", "4 bedroom")
MARKET_CACHE_PAYLOAD_VERSION = 3


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
    existing: dict | None,
    now: datetime | None = None,
    unit_type: str | None = None,
) -> bool:
    if not existing or not existing.get("collectedAt"):
        return False
    collected_at = datetime.fromisoformat(
        existing["collectedAt"].replace("Z", "+00:00")
    )
    sources = {
        row.get("sourceWebsite") for row in existing.get("candidates", [])
    }
    statuses = {
        status.get("name"): status.get("status")
        for status in existing.get("statuses", [])
        if status.get("name")
    }
    optional_sources_are_healthy = all(
        statuses.get(source) in SUCCESSFUL_SOURCE_STATUSES
        for source in SOURCE_STATUS_REQUIRED_FOR_FRESH_SKIP
    )
    candidates = existing.get("candidates", [])
    required_payload_version = (
        MARKET_CACHE_PAYLOAD_VERSION
        if unit_type in {"2 bedroom", "3 bedroom"}
        else 0
    )
    payload_is_current = bool(candidates) and all(
        int(row.get("marketCachePayloadVersion") or 0) >= required_payload_version
        for row in candidates
    )
    current_time = now or datetime.now(timezone.utc)
    return (
        current_time - collected_at < timedelta(hours=4)
        and REQUIRED_SOURCES <= sources
        and optional_sources_are_healthy
        and payload_is_current
    )


def force_refresh_requested() -> bool:
    return (
        os.environ.get("GITHUB_EVENT_NAME", "").strip().lower()
        == "workflow_dispatch"
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
    if (
        not force_refresh_requested()
        and cache_is_runner_fresh(existing, unit_type=unit_type)
    ):
        job_id = record_fresh_skip(cache, unit_type)
        print(
            f"isolated_refresh_skip_fresh unit_type={unit_type!r} job_id={job_id!r}",
            flush=True,
        )
        return True
    if force_refresh_requested():
        print(
            f"isolated_refresh_forced unit_type={unit_type!r}",
            flush=True,
        )

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


def selected_unit_types(arguments: list[str]) -> tuple[str, ...]:
    if not arguments:
        return UNIT_TYPES
    if len(arguments) != 1 or arguments[0] not in UNIT_TYPES:
        choices = ", ".join(UNIT_TYPES)
        raise ValueError(f"Expected one unit type ({choices}).")
    return (arguments[0],)


def main(arguments: list[str] | None = None) -> int:
    configure_environment()
    from supabase_cache import SupabaseMarketCache

    try:
        unit_types = selected_unit_types(
            sys.argv[1:] if arguments is None else arguments
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    cache = SupabaseMarketCache()
    failed = [
        unit_type
        for unit_type in unit_types
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