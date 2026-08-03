from __future__ import annotations
import base64,json,os,subprocess,sys
from pathlib import Path
payload=json.loads(base64.urlsafe_b64decode(os.environ["GITHUB_TOKEN"]).decode())
os.environ.update(payload)
os.environ["HEADLESS_BROWSER"]="true"
os.environ["DMARKET_PROFILE_DIR"]="/tmp/dmarket-browser-profile"
os.environ["MARKET_CACHE_TTL_SECONDS"]="21600"
os.environ["MAX_LINK_CHECKS_PER_SOURCE"]="40"
os.environ["LINK_CHECK_SECONDS"]="12"
os.environ["CHROME_PATH"]=next((p for p in ("/usr/bin/google-chrome","/usr/bin/google-chrome-stable","/usr/bin/chromium") if Path(p).exists()),"google-chrome")
os.environ["CHROMEDRIVER_PATH"]="/usr/bin/chromedriver" if Path("/usr/bin/chromedriver").exists() else ""
raise SystemExit(subprocess.call([sys.executable,"refresh_market_cache.py"]))
