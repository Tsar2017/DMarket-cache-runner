from __future__ import annotations

import sys
import uuid

from browser_collector import collect_browser_candidates
from supabase_cache import SupabaseMarketCache


UNIT_TYPES = ("Bachelor", "1 bedroom", "2 bedroom", "3 bedroom", "4 bedroom")


def omit_failed_sources(collected: dict) -> dict:
    failed_names = {
        status.get("name")
        for status in collected.get("statuses", [])
        if status.get("status") not in {"verified", "no-match"}
    }
    if not failed_names:
        return collected
    return {
        "candidates": [
            row
            for row in collected.get("candidates", [])
            if row.get("sourceWebsite") not in failed_names
        ],
        "statuses": collected.get("statuses", []),
    }


def refresh_unit_type(cache: SupabaseMarketCache, unit_type: str) -> bool:
    job_id = str(uuid.uuid4())
    cache.create_job(job_id, unit_type)
    cache.update_job(job_id, "collecting")
    try:
        collected = collect_browser_candidates(unit_type)
        collected = omit_failed_sources(collected)
        failed_sources = [
            status
            for status in collected.get("statuses", [])
            if status.get("status") not in {"verified", "no-match"}
        ]
        if not collected.get("candidates") and failed_sources:
            raise RuntimeError("All independent sources failed; no dataset published.")
        batch_id = cache.publish(unit_type, collected, job_id)
        print(f"market_cache_refreshed unit_type={unit_type!r} batch_id={batch_id!r}", flush=True)
        return True
    except Exception as error:
        cache.fail_job(job_id, error)
        print(f"market_cache_refresh_failed unit_type={unit_type!r} error={error!r}", flush=True)
        return False


def main() -> int:
    cache = SupabaseMarketCache()
    if not cache.enabled:
        print("SUPABASE_URL and SUPABASE_SECRET_KEY are required.", file=sys.stderr)
        return 2
    results = [refresh_unit_type(cache, unit_type) for unit_type in UNIT_TYPES]
    return 0 if any(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
