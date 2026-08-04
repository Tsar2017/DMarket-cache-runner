from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class SupabaseCacheError(RuntimeError):
    pass


def normalize_supabase_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    marker = "/rest/v1"
    index = url.lower().find(marker)
    return url[:index].rstrip("/") if index >= 0 else url


class SupabaseMarketCache:
    def __init__(self) -> None:
        self.url = normalize_supabase_url(os.environ.get("SUPABASE_URL", ""))
        self.secret_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        self.ttl = timedelta(seconds=max(60, int(os.environ.get("MARKET_CACHE_TTL_SECONDS", "3600"))))

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret_key)

    def _request(self, method: str, path: str, payload=None, prefer: str = "return=representation"):
        if not self.enabled:
            raise SupabaseCacheError("Supabase market cache is not configured.")
        body = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        request = Request(
            f"{self.url}/rest/v1/{path}",
            data=body,
            method=method,
            headers={
                "apikey": self.secret_key,
                "Authorization": f"Bearer {self.secret_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Prefer": prefer,
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else None
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise SupabaseCacheError(f"Supabase request failed ({error.code}): {detail[:500]}") from error
        except (URLError, TimeoutError) as error:
            raise SupabaseCacheError(f"Supabase is unavailable: {error}") from error

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def latest_completed_batch(self, unit_type: str) -> dict | None:
        query = urlencode(
            {
                "select": "id,unit_type,source_statuses,collected_at,completed_at,listing_count",
                "unit_type": f"eq.{unit_type}",
                "status": "eq.completed",
                "order": "completed_at.desc",
                "limit": "1",
            }
        )
        rows = self._request("GET", f"market_cache_batches?{query}") or []
        return rows[0] if rows else None

    def _load_batch(self, batch: dict) -> dict:
        query = urlencode(
            {"select": "payload", "batch_id": f"eq.{batch['id']}", "order": "id.asc"}
        )
        rows = self._request("GET", f"market_cache_listings?{query}") or []
        return {
            "candidates": [row["payload"] for row in rows],
            "statuses": batch.get("source_statuses") or [],
            "collectedAt": batch.get("completed_at") or batch.get("collected_at"),
            "batchId": batch["id"],
        }

    def load_fresh(self, unit_type: str) -> dict | None:
        batch = self.latest_completed_batch(unit_type)
        if not batch:
            return None
        completed_at = self._timestamp(batch.get("completed_at"))
        if not completed_at or datetime.now(timezone.utc) - completed_at > self.ttl:
            return None
        return self._load_batch(batch)

    def load_latest(self, unit_type: str) -> dict | None:
        batch = self.latest_completed_batch(unit_type)
        return self._load_batch(batch) if batch else None

    def load_stale_after_failed_refresh(self, unit_type: str) -> dict | None:
        batch = self.latest_completed_batch(unit_type)
        if not batch:
            return None
        query = urlencode(
            {
                "select": "status,error,updated_at",
                "unit_type": f"eq.{unit_type}",
                "order": "updated_at.desc",
                "limit": "1",
            }
        )
        jobs = self._request("GET", f"market_collection_jobs?{query}") or []
        if not jobs or jobs[0].get("status") != "failed":
            return None
        failed_at = self._timestamp(jobs[0].get("updated_at"))
        completed_at = self._timestamp(batch.get("completed_at"))
        if (
            not failed_at
            or not completed_at
            or failed_at <= completed_at
            or datetime.now(timezone.utc) - failed_at > self.ttl
        ):
            return None
        result = self._load_batch(batch)
        result["isStale"] = True
        result["cacheWarning"] = (
            "Fresh portal collection failed. Showing the last successful cached listings."
        )
        return result

    def create_job(self, job_id: str, unit_type: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._request(
            "POST",
            "market_collection_jobs",
            {"id": job_id, "unit_type": unit_type, "status": "queued", "created_at": now, "updated_at": now},
        )

    def update_job(self, job_id: str, status: str, **values) -> None:
        payload = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **values}
        self._request("PATCH", f"market_collection_jobs?id=eq.{quote(job_id)}", payload)

    def get_job(self, job_id: str) -> dict | None:
        query = urlencode(
            {
                "select": "id,unit_type,status,error,batch_id,created_at,started_at,completed_at,updated_at",
                "id": f"eq.{job_id}",
                "limit": "1",
            }
        )
        rows = self._request("GET", f"market_collection_jobs?{query}") or []
        return rows[0] if rows else None

    def publish(self, unit_type: str, collected: dict, job_id: str) -> str:
        batch_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._request(
            "POST",
            "market_cache_batches",
            {
                "id": batch_id,
                "unit_type": unit_type,
                "status": "collecting",
                "source_statuses": collected.get("statuses", []),
                "collected_at": now,
                "listing_count": len(collected.get("candidates", [])),
            },
        )
        try:
            candidates = collected.get("candidates", [])
            for start in range(0, len(candidates), 100):
                rows = [
                    {
                        "batch_id": batch_id,
                        "unit_type": unit_type,
                        "source_website": candidate.get("sourceWebsite", ""),
                        "listing_url": candidate.get("listingUrl", ""),
                        "payload": candidate,
                        "collected_at": now,
                    }
                    for candidate in candidates[start : start + 100]
                ]
                if rows:
                    self._request("POST", "market_cache_listings", rows, prefer="return=minimal")
            completed_at = datetime.now(timezone.utc).isoformat()
            self._request(
                "PATCH",
                f"market_cache_batches?id=eq.{quote(batch_id)}",
                {"status": "completed", "completed_at": completed_at},
            )
            self.update_job(job_id, "completed", batch_id=batch_id, completed_at=completed_at, error=None)
            return batch_id
        except Exception as error:
            self._request(
                "PATCH",
                f"market_cache_batches?id=eq.{quote(batch_id)}",
                {"status": "failed", "error": str(error)[:1000]},
            )
            raise

    def fail_job(self, job_id: str, error: Exception) -> None:
        self.update_job(
            job_id,
            "failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(error)[:1000],
        )


def new_job_id() -> str:
    return str(uuid.uuid4())
