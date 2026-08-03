from __future__ import annotations
import base64,json,os,subprocess,sys
from pathlib import Path
payload=json.loads(base64.urlsafe_b64decode(os.environ["GITHUB_TOKEN"]).decode())
os.environ.update(payload)
os.environ["HEADLESS_BROWSER"]="true"
os.environ["DMARKET_PROFILE_DIR"]="/tmp/dmarket-browser-profile"
os.environ["MARKET_CACHE_TTL_SECONDS"]="21600"
os.environ["MAX_LINK_CHECKS_PER_SOURCE"]="1"
os.environ["LINK_CHECK_SECONDS"]="4"
os.environ["CHROME_PATH"]=next((p for p in ("/usr/bin/google-chrome","/usr/bin/google-chrome-stable","/usr/bin/chromium") if Path(p).exists()),"google-chrome")
os.environ["CHROMEDRIVER_PATH"]="/usr/bin/chromedriver" if Path("/usr/bin/chromedriver").exists() else ""
result=subprocess.call([sys.executable,"refresh_market_cache.py"])
if result == 0:
    workflow = """name: Refresh DMarkeT market cache

on:
  workflow_dispatch:
  schedule:
    - cron: "17 */6 * * *"

concurrency:
  group: dmarket-cache-refresh
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  refresh:
    runs-on: ubuntu-latest
    timeout-minutes: 75
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt
      - name: Install Python dependencies
        run: pip install -r requirements.txt
      - name: Refresh all bedroom caches
        env:
          GITHUB_TOKEN: ${{ secrets.GIT_TOKEN }}
        run: python main.py
"""
    Path(".github/workflows/task.yml").write_text(workflow, encoding="utf-8")
raise SystemExit(result)
