from __future__ import annotations
import base64,json,os,shutil,subprocess,sys,time,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
payload=json.loads(base64.urlsafe_b64decode(os.environ["GITHUB_TOKEN"]).decode())
os.environ.update(payload)
os.environ["HEADLESS_BROWSER"]="true"
os.environ["MARKET_CACHE_TTL_SECONDS"]="21600"
os.environ["MAX_LINK_CHECKS_PER_SOURCE"]="1"
os.environ["LINK_CHECK_SECONDS"]="4"
os.environ["MARKET_CACHE_REQUIRED_SOURCES"]="RentFaster,Rentals.ca"
os.environ["CHROME_PATH"]=next((p for p in ("/usr/bin/google-chrome","/usr/bin/google-chrome-stable","/usr/bin/chromium") if Path(p).exists()),"google-chrome")
os.environ["CHROMEDRIVER_PATH"]="/usr/bin/chromedriver" if Path("/usr/bin/chromedriver").exists() else ""
from supabase_cache import SupabaseMarketCache
from refresh_market_cache import record_skipped_job
cache=SupabaseMarketCache()
required={"RentFaster","Rentals.ca"}
unit_types=("Bachelor","1 bedroom","2 bedroom","3 bedroom","4 bedroom")
failed=[]
for unit_type in unit_types:
    existing=cache.load_latest(unit_type)
    collected_at=datetime.fromisoformat(existing["collectedAt"].replace("Z","+00:00")) if existing and existing.get("collectedAt") else None
    sources={row.get("sourceWebsite") for row in existing.get("candidates",[])} if existing else set()
    if collected_at and datetime.now(timezone.utc)-collected_at < timedelta(hours=4) and required <= sources:
        job_id=str(uuid.uuid4())
        cache.create_job(job_id,unit_type)
        record_skipped_job(cache,job_id)
        print(f"isolated_refresh_skip_fresh unit_type={unit_type!r} job_id={job_id!r}",flush=True)
        continue
    succeeded=False
    for attempt in range(1,4):
        profile=Path("/tmp") / ("dmarket-" + unit_type.replace(" ","-") + f"-{attempt}")
        shutil.rmtree(profile,ignore_errors=True)
        env=os.environ.copy();env["DMARKET_PROFILE_DIR"]=str(profile)
        print(f"isolated_refresh unit_type={unit_type!r} attempt={attempt}",flush=True)
        if subprocess.call([sys.executable,"refresh_market_cache.py",unit_type],env=env)==0:
            succeeded=True;break
        time.sleep(5)
    if not succeeded:failed.append(unit_type)
if failed:
    print("isolated_refresh_failed unit_types="+", ".join(failed),file=sys.stderr,flush=True)
    raise SystemExit(1)
