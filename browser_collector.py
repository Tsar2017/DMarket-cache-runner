from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
VENDOR_DIR = APP_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


CHROME_PATH = Path(os.environ.get("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
CHROMEDRIVER_PATH = Path(os.environ.get("CHROMEDRIVER_PATH", ""))
DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9224"))
PROFILE_DIR = Path(os.environ.get("DMARKET_PROFILE_DIR", str(APP_DIR / ".dmarket-browser-profile")))
HEADLESS_BROWSER = os.environ.get("HEADLESS_BROWSER", "false").lower() in {"1", "true", "yes"}
RENTALS_MAX_PAGES = max(1, int(os.environ.get("RENTALS_MAX_PAGES", "3")))
BROWSER_LOCK = threading.Lock()
_driver = None
_chrome_process = None
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
APARTMENTS_BROWSER_USER_AGENT = BROWSER_USER_AGENT
LINK_CHECK_SECONDS = max(4, int(os.environ.get("LINK_CHECK_SECONDS", "12")))
MAX_LINK_CHECKS_PER_SOURCE = max(1, int(os.environ.get("MAX_LINK_CHECKS_PER_SOURCE", "40")))
MARKET_CACHE_PAYLOAD_VERSION = 2


def _log_source(name: str, query: str, status: dict, started_at: float, error: Exception | None = None) -> None:
    duration = time.monotonic() - started_at
    error_text = str(error) if error else ""
    print(
        "source_collection "
        f"source={name!r} query={query!r} status={status.get('status', 'unknown')!r} "
        f"results={status.get('collected', 0)} duration_seconds={duration:.2f} "
        f"error={error_text!r}",
        flush=True,
    )


def _browser_ready() -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _start_browser() -> None:
    global _chrome_process
    if _browser_ready():
        return
    if not CHROME_PATH.exists():
        raise RuntimeError("Google Chrome is required for automatic rental-source collection.")
    PROFILE_DIR.mkdir(exist_ok=True)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    browser_command = [
            str(CHROME_PATH),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--window-position=-32000,-32000",
            "--window-size=1024,768",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-notifications",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-sync",
            "--disk-cache-size=1",
            "--media-cache-size=1",
            "--renderer-process-limit=1",
            "about:blank",
    ]
    if os.name != "nt":
        browser_command[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
    if HEADLESS_BROWSER:
        browser_command[1:1] = ["--headless=new"]
    if os.name != "nt":
        browser_command[0:0] = ["nice", "-n", "10"]
    _chrome_process = subprocess.Popen(
        browser_command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        start_new_session=os.name != "nt",
    )
    for _ in range(120):
        if _browser_ready():
            return
        time.sleep(0.25)
    return_code = _chrome_process.poll() if _chrome_process else None
    if return_code is not None:
        raise RuntimeError(f"The rental-source browser exited during startup with status {return_code}.")
    raise RuntimeError("The rental-source browser could not start within the startup timeout.")


def _get_driver():
    global _driver
    if _driver:
        try:
            _driver.title
            return _driver
        except WebDriverException:
            _driver = None
    _start_browser()
    options = Options()
    options.debugger_address = f"127.0.0.1:{DEBUG_PORT}"
    options.page_load_strategy = "none"
    service = Service(str(CHROMEDRIVER_PATH)) if CHROMEDRIVER_PATH.is_file() else Service()
    _driver = webdriver.Chrome(service=service, options=options)
    _driver.set_page_load_timeout(45)
    _driver.set_script_timeout(45)
    try:
        # A "HeadlessChrome" user agent triggers Cloudflare interactive
        # challenges on RentFaster and Rentals.ca; always present a real
        # Chrome user agent for every source.
        _set_user_agent(_driver, BROWSER_USER_AGENT)
    except WebDriverException:
        pass
    return _driver


def _reset_driver() -> None:
    global _driver
    try:
        if _driver:
            _driver.quit()
    except Exception:
        pass
    _driver = None


def _shutdown_browser() -> None:
    global _chrome_process
    _reset_driver()
    if _chrome_process:
        try:
            if os.name == "nt":
                _chrome_process.terminate()
            else:
                os.killpg(_chrome_process.pid, signal.SIGTERM)
            _chrome_process.wait(timeout=5)
        except Exception:
            try:
                if os.name == "nt":
                    _chrome_process.kill()
                else:
                    os.killpg(_chrome_process.pid, signal.SIGKILL)
            except Exception:
                pass
    _chrome_process = None


def _get_fresh_driver():
    _shutdown_browser()
    if PROFILE_DIR.exists():
        try:
            shutil.rmtree(PROFILE_DIR)
        except OSError:
            time.sleep(0.5)
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    return _get_driver()


def _set_user_agent(driver, user_agent: str) -> None:
    driver.execute_cdp_cmd(
        "Network.setUserAgentOverride",
        {
            "userAgent": user_agent,
            "platform": "Windows",
            "acceptLanguage": "en-CA,en;q=0.9",
        },
    )


def _unit_details(unit_type: str) -> tuple[int, str, str, str]:
    lowered = unit_type.lower()
    if "bachelor" in lowered or "studio" in lowered:
        return 0, "Bachelor", "bachelor", "studios"
    match = re.search(r"\b([1-4])\b", lowered)
    number = int(match.group(1)) if match else 1
    return number, f"{number} bedroom", f"{number}-bedrooms", f"{number}-bedrooms"


def _first_number(value) -> int | None:
    match = re.search(r"[\d,]+", str(value or ""))
    return int(match.group(0).replace(",", "")) if match else None


def _string_list(value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, dict):
                values.extend(str(v) for v in item.values() if isinstance(v, (str, int, float)))
            else:
                values.append(str(item))
        return ", ".join(values)
    return str(value)


def _rentfaster_exact_bedrooms(value: str, target_beds: int) -> bool:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if target_beds == 0:
        return cleaned in {"bachelor", "studio", "0"}
    return bool(re.fullmatch(rf"{target_beds}(?:\s*\+\s*den)?", cleaned))


def _rentfaster_street_address(value) -> str:
    cleaned = html.unescape(str(value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"(?:,\s*)?calgary(?:,\s*(?:alberta|ab))?\s*$", "", cleaned, flags=re.I)
    cleaned = cleaned.strip(" ,")
    if not re.search(r"\d", cleaned) or not re.search(r"[a-z]", cleaned, flags=re.I):
        return ""
    return cleaned


def _rentfaster_address_from_link(link) -> str:
    path = unquote(urlparse(str(link or "")).path).rstrip("/")
    match = re.search(r"/properties/([^/?#]+)$", path, flags=re.I)
    if not match:
        return ""
    slug = re.sub(r"-\d+$", "", match.group(1))
    slug = re.sub(r"-calgary$", "", slug, flags=re.I)
    if not re.match(r"^\d+(?:-|$)", slug) or not re.search(r"[a-z]", slug, flags=re.I):
        return ""

    replacements = {
        "ave": "Avenue",
        "avenue": "Avenue",
        "blvd": "Boulevard",
        "boulevard": "Boulevard",
        "cir": "Circle",
        "circle": "Circle",
        "cres": "Crescent",
        "crescent": "Crescent",
        "ct": "Court",
        "court": "Court",
        "dr": "Drive",
        "drive": "Drive",
        "pl": "Place",
        "place": "Place",
        "rd": "Road",
        "road": "Road",
        "st": "Street",
        "street": "Street",
        "trl": "Trail",
        "trail": "Trail",
    }
    directions = {"n", "ne", "e", "se", "s", "sw", "w", "nw"}
    words = []
    for token in slug.split("-"):
        lower = token.lower()
        if lower in directions:
            words.append(lower.upper())
        elif lower in replacements:
            words.append(replacements[lower])
        elif re.fullmatch(r"\d+(?:st|nd|rd|th)", lower):
            words.append(lower)
        else:
            words.append(token.title())
    return _rentfaster_street_address(" ".join(words))


def _rentfaster_listing_address(listing: dict) -> str:
    for field in (
        "address",
        "street_address",
        "streetAddress",
        "full_address",
        "fullAddress",
    ):
        street = _rentfaster_street_address(listing.get(field))
        if street:
            return f"{street}, Calgary, AB"
    street = _rentfaster_address_from_link(listing.get("link"))
    return f"{street}, Calgary, AB" if street else ""


def _rentfaster_listing_url(link) -> str:
    return urljoin("https://www.rentfaster.ca", str(link or "").strip())


def _rent_value(value) -> int | None:
    match = re.search(r"[\d,]+", str(value or ""))
    return int(match.group(0).replace(",", "")) if match else None


def _advertised_bedroom_counts(value: str) -> set[int]:
    text = re.sub(r"\s+", " ", str(value or "").lower())
    words = {"one": 1, "two": 2, "three": 3, "four": 4}
    counts = {
        int(match.group(1))
        for match in re.finditer(r"\b([1-4])[\s-]*(?:bed(?:room)?s?|bdrms?)\b", text)
    }
    counts.update(
        words[match.group(1)]
        for match in re.finditer(
            r"\b(one|two|three|four)[\s-]*(?:bed(?:room)?s?|bdrms?)\b",
            text,
        )
    )
    for match in re.finditer(
        r"\b([1-4])\s*(?:&|and)\s*([1-4])[\s-]*(?:bed(?:room)?s?|bdrms?)\b",
        text,
    ):
        counts.update((int(match.group(1)), int(match.group(2))))
    return counts


def _unit_description_match(
    title: str,
    target_beds: int,
    advertisement_text: str = "",
) -> tuple[str, str]:
    if target_beds == 0:
        return ("match", "") if re.search(r"\b(?:studio|bachelor)\b", title, re.I) else ("unknown", "")
    counts = _advertised_bedroom_counts(title)
    evidence_label = "title"
    if not counts:
        description_counts = _advertised_bedroom_counts(advertisement_text)
        # A building advertisement may enumerate many legitimate floor plans.
        # Treat descriptive text as contradictory only when it makes one
        # unambiguous bedroom claim.
        if len(description_counts) == 1:
            counts = description_counts
            evidence_label = "description"
    if counts:
        if target_beds in counts:
            return "match", ""
        return (
            "mismatch",
            f"Advertisement {evidence_label} describes {sorted(counts)} bedroom(s), not {target_beds}.",
        )
    return "unknown", ""


def _shared_room_evidence(
    text: str,
    target_beds: int,
    rent: int | None,
    structured_type: str = "",
) -> tuple[str, str]:
    normalized_type = re.sub(r"\s+", " ", str(structured_type or "").strip().lower())
    if normalized_type in {"room", "room for rent", "shared", "bed space", "bedspace"}:
        return "structured-room-offering", f"Structured source type: {structured_type}."

    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    room_in_shared = re.compile(
        r"\b(?:1|one)\s+(?:private\s+)?room\s+in\s+(?:a\s+)?"
        r"([1-4])\s*(?:bed(?:room)?s?|bdrms?)\b.{0,60}\bshared\b",
        re.I,
    )
    for index, line in enumerate(lines):
        match = room_in_shared.search(line)
        if not match:
            continue
        context = " ".join(lines[index : index + 3])
        prices = {
            int(value.replace(",", ""))
            for value in re.findall(r"\$\s*([\d,]+)", context)
        }
        offered_beds = int(match.group(1))
        if offered_beds == target_beds or (rent is not None and rent in prices):
            return "shared-room-offering", line[:500]

    compact = " ".join(lines)
    decisive_patterns = (
        r"\bprivate\s+bedroom\b",
        r"\b(?:private\s+)?(?:bedroom|room)\s+for\s+rent\b",
        r"\brent(?:ed|ing)?\s+by\s+the\s+(?:bedroom|room)\b",
        r"\bindividual(?:ly)?\s+(?:leased|lease|leasing)\b",
        r"\bper\s+(?:bedroom|room)\b",
        r"\bshared\s+accommodations?\b",
        r"\bbed\s*space\b",
    )
    for pattern in decisive_patterns:
        match = re.search(pattern, compact, re.I)
        if not match:
            continue
        context = compact[max(0, match.start() - 160) : match.end() + 160]
        prices = {
            int(value.replace(",", ""))
            for value in re.findall(r"\$\s*([\d,]+)", context)
        }
        if not prices or rent is None or rent in prices:
            return "explicit-room-language", context[:500]
    return "", ""


def _full_unit_evidence(text: str) -> tuple[str, str]:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    match = re.search(
        r"\b(?:entire|complete)\s+(?:apartment|unit|suite|home)\b|"
        r"\bfull[- ](?:apartment|unit|suite)\s+rental\b",
        compact,
        re.I,
    )
    if not match:
        return "", ""
    context = compact[max(0, match.start() - 160) : match.end() + 160]
    return "explicit-full-unit-language", context[:500]


def _annotate_occupancy(
    candidate: dict,
    target_beds: int,
    advertisement_text: str = "",
    structured_type: str = "",
) -> dict:
    candidate["marketCachePayloadVersion"] = MARKET_CACHE_PAYLOAD_VERSION
    title = str(candidate.get("advertisementTitle") or candidate.get("buildingName") or "")
    candidate["advertisementTitle"] = title
    candidate["occupancyClassification"] = "unknown"
    candidate["occupancyReason"] = ""
    candidate["occupancyEvidence"] = ""
    candidate["advertisementText"] = re.sub(
        r"\s+\n",
        "\n",
        str(advertisement_text or "").strip(),
    )[:8000]
    candidate["structuredOccupancyEvidence"] = str(structured_type or "").strip()
    unit_match, unit_reason = _unit_description_match(
        title,
        target_beds,
        advertisement_text,
    )
    candidate["advertisedUnitMatch"] = unit_match
    candidate["advertisedUnitReason"] = unit_reason
    reason, evidence = _shared_room_evidence(
        advertisement_text,
        target_beds,
        _rent_value(candidate.get("rentPrice")),
        structured_type,
    )
    if reason:
        candidate["occupancyClassification"] = "shared-room"
        candidate["occupancyReason"] = reason
        candidate["occupancyEvidence"] = evidence
    else:
        reason, evidence = _full_unit_evidence(advertisement_text)
        if reason:
            candidate["occupancyClassification"] = "full-unit"
            candidate["occupancyReason"] = reason
            candidate["occupancyEvidence"] = evidence
    return candidate


def _rentfaster_occupancy_inspection_ids() -> set[str]:
    """Return a small high-signal set of IDs whose pages warrant inspection.

    Ordinary apartment buildings reuse IDs across bedroom feeds extensively,
    so reuse alone is deliberately not enough to inspect hundreds of pages.
    A higher-bedroom feed carrying a lower price prioritizes the page for
    evidence inspection, but does not classify or exclude the listing.
    """
    observations: dict[str, list[tuple[int, int]]] = {}
    for beds in (1, 2, 3, 4):
        for listing in _rentfaster_api_listings(beds):
            listing_id = str(listing.get("id") or listing.get("ref_id") or "").strip()
            price = _rent_value(listing.get("price"))
            if listing_id and price:
                observations.setdefault(listing_id, []).append((beds, price))
    inspection_ids = set()
    for listing_id, values in observations.items():
        ordered = sorted(set(values))
        if any(
            higher_beds > lower_beds and higher_price < lower_price
            for lower_beds, lower_price in ordered
            for higher_beds, higher_price in ordered
        ):
            inspection_ids.add(listing_id)
    return inspection_ids


def _collect_rentfaster(driver, unit_type: str) -> tuple[list[dict], dict]:
    beds, normalized_unit, rentfaster_slug, _ = _unit_details(unit_type)
    params = [
        ("type[]", "Apartment"),
        ("beds[]", "Bachelor" if beds == 0 else str(beds)),
        ("sortby", "price"),
    ]
    if beds:
        params.append(("beds[]", f"{beds} + Den"))
    url = (
        f"https://www.rentfaster.ca/ab/calgary/rentals/apartment/{rentfaster_slug}/?"
        + urlencode(params)
    )
    driver.get(url)
    for _ in range(30):
        listings = driver.execute_script(
            """
            const scoped = Array.from(document.querySelectorAll(".listing-item"))
              .map(element => {
                try { return angular.element(element).scope().listing; }
                catch (_error) { return null; }
              })
              .filter(Boolean);
            return scoped.length ? scoped : (window.preloadedListings || []);
            """
        )
        if len(listings) >= 20:
            break
        time.sleep(0.25)
    else:
        listings = []
    collection_note = "Collected {count} active exact-bedroom listing(s) automatically."
    if not listings:
        listings = _rentfaster_api_listings(beds)
        collection_note = (
            "The search page was unavailable to the browser; collected {count} active "
            "exact-bedroom listing(s) from RentFaster's public listing API instead."
        )
    try:
        occupancy_inspection_ids = _rentfaster_occupancy_inspection_ids() if beds else set()
    except Exception:
        occupancy_inspection_ids = set()
    candidates = []
    for listing in listings:
        if str(listing.get("availability", "")).lower() == "no vacancy":
            continue
        if not _rentfaster_exact_bedrooms(listing.get("beds", listing.get("bedrooms", "")), beds):
            continue
        rent = _first_number(listing.get("price"))
        latitude = listing.get("latitude")
        longitude = listing.get("longitude")
        if not rent or latitude is None or longitude is None:
            continue
        promotions = _string_list(listing.get("promotions")).replace("_", " ").title()
        listing_address = _rentfaster_listing_address(listing)
        listing_url = _rentfaster_listing_url(listing.get("link"))
        source_listing_id = str(listing.get("id") or listing.get("ref_id") or "").strip()
        candidate = {
                "companyName": "Independent",
                "buildingName": listing.get("title") or listing_address or "RentFaster listing",
                "address": listing_address,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "unitType": normalized_unit,
                "rentPrice": rent,
                "promoAdjustedPrice": promotions,
                "squareFootage": _string_list(listing.get("sq_feet")),
                "securityDeposit": "",
                "featuresAmenities": ", ".join(
                    label
                    for field, label in (
                        ("dishwasher", "Dishwasher"),
                        ("laundry_in_suite", "In-suite laundry"),
                        ("air_conditioning", "Air conditioning"),
                    )
                    if listing.get(field)
                ),
                "parking": "Available" if listing.get("parking_available") else "",
                "utilitiesNotes": _string_list(listing.get("utilities_included")),
                "comments": "Active same-bedroom listing collected automatically from RentFaster.",
                "sourceWebsite": "RentFaster",
                "listingUrl": listing_url,
                "sourceListingId": source_listing_id,
                "isVerified": True,
                "verifiedAt": date.today().isoformat(),
                "promotionText": promotions,
                "advertisementTitle": listing.get("title") or "",
                "_occupancyInspectionRequested": source_listing_id in occupancy_inspection_ids,
            }
        advertisement_text = "\n".join(
            str(value)
            for value in (
                listing.get("title"),
                listing.get("intro"),
                listing.get("type"),
                _string_list(listing.get("promotions")),
            )
            if value
        )
        candidates.append(
            _annotate_occupancy(
                candidate,
                beds,
                advertisement_text=advertisement_text,
                structured_type=str(listing.get("type") or ""),
            )
        )
    return candidates, {
        "name": "RentFaster",
        "url": url,
        "status": "verified" if candidates else "no-match",
        "detail": collection_note.format(count=len(candidates)),
        "collected": len(candidates),
    }


def _rentfaster_api_listings(beds: int) -> list[dict]:
    """Fetch active RentFaster listings from the public map.json API.

    The HTML search page sits behind a Cloudflare challenge for many
    automated environments, but the JSON API answers plain requests, so it
    is used whenever the in-browser collection returns nothing.
    """
    params = [("cur_page", "0"), ("city_id", "1"), ("type", "Apartment")]
    params.append(("beds", "bachelor" if beds == 0 else str(beds)))
    url = "https://www.rentfaster.ca/api/map.json?" + urlencode(params)
    request = Request(url, headers={"User-Agent": BROWSER_USER_AGENT})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return payload.get("listings") or []


def _matching_floorplans(node: dict, target_beds: int) -> list[dict]:
    matches = []
    for plan in node.get("floorPlans") or []:
        plan_beds = plan.get("beds")
        if plan_beds is None:
            continue
        if target_beds == 0 and float(plan_beds) == 0:
            matches.append(plan)
        elif target_beds and float(plan_beds) == target_beds:
            matches.append(plan)
    return matches


def _rentals_search_response(driver, page_url: str) -> dict | None:
    """Wait for Rentals.ca's search store, retrying transient empty loads."""
    for _navigation_attempt in range(5):
        driver.get(page_url)
        for _poll_attempt in range(30):
            candidate_response = driver.execute_script(
                "return window.App && App.store && App.store.search ? App.store.search.response : null;"
            )
            edges = (
                candidate_response.get("data", {}).get("edges", [])
                if candidate_response
                else []
            )
            if edges:
                return candidate_response
            time.sleep(0.25)
    return None


def _collect_rentals(
    driver,
    unit_type: str,
    origin: tuple[float, float] | None = None,
    our_rent: float | None = None,
) -> tuple[list[dict], dict]:
    beds, normalized_unit, _, rentals_slug = _unit_details(unit_type)
    url = f"https://rentals.ca/calgary/{rentals_slug}"
    candidates = []
    seen_paths = set()
    for page_number in range(1, RENTALS_MAX_PAGES + 1):
        page_url = url if page_number == 1 else f"{url}?p={page_number}"
        response = _rentals_search_response(driver, page_url)
        edges = response.get("data", {}).get("edges", []) if response else []
        new_rows = 0
        for edge in edges:
            node = edge.get("node") or {}
            path = str(node.get("path", "")).lstrip("/")
            if not path or path in seen_paths:
                continue
            plans = _matching_floorplans(node, beds)
            rents = [int(plan["rent"]) for plan in plans if plan.get("rent")]
            location = node.get("rentalListingLocation") or []
            address = node.get("address") or {}
            if not rents or len(location) < 2:
                continue
            seen_paths.add(path)
            matching_plan = min((plan for plan in plans if plan.get("rent")), key=lambda plan: plan["rent"])
            parking = node.get("parking") or {}
            promotions = _string_list(node.get("promotions"))
            candidate = {
                    "companyName": "Independent",
                    "buildingName": node.get("rentalListingName") or address.get("street") or "Rentals.ca listing",
                    "address": f"{address.get('street', '')}, Calgary, AB {address.get('postalCode', '')}".strip(),
                    "latitude": float(location[1]),
                    "longitude": float(location[0]),
                    "unitType": normalized_unit,
                    "rentPrice": min(rents),
                    "promoAdjustedPrice": promotions,
                    "squareFootage": matching_plan.get("sqft") or "",
                    "securityDeposit": "",
                    "featuresAmenities": _string_list(node.get("petOptions")),
                    "parking": _string_list(parking.get("parkingTypes")),
                    "utilitiesNotes": "",
                    "comments": "Active same-bedroom floor plan collected automatically from Rentals.ca.",
                    "sourceWebsite": "Rentals.ca",
                    "listingUrl": "https://rentals.ca/" + path,
                    "isVerified": True,
                    "verifiedAt": date.today().isoformat(),
                    "promotionText": promotions,
                    "sourceListingId": path,
                    "advertisementTitle": node.get("rentalListingName") or "",
                }
            advertisement_text = "\n".join(
                _string_list(value)
                for value in (
                    node.get("rentalListingName"),
                    node.get("description"),
                    node.get("summary"),
                    node.get("rentalListingType"),
                    matching_plan,
                )
                if value
            )
            candidates.append(
                _annotate_occupancy(
                    candidate,
                    beds,
                    advertisement_text=advertisement_text,
                    structured_type=str(node.get("rentalListingType") or ""),
                )
            )
            new_rows += 1
        if not new_rows:
            break
        if origin and our_rent:
            nearby_affordable = sum(
                1
                for candidate in candidates
                if candidate["rentPrice"] <= our_rent
                and _haversine_km(
                    origin,
                    (float(candidate["latitude"]), float(candidate["longitude"])),
                )
                <= 7
            )
            if nearby_affordable >= 3:
                break
    return candidates, {
        "name": "Rentals.ca",
        "url": url,
        "status": "verified" if candidates else "no-match",
        "detail": f"Collected {len(candidates)} active exact-bedroom listing(s) automatically.",
        "collected": len(candidates),
    }


def _haversine_km(origin: tuple[float, float], target: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, target)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(value))


def _strip_markup(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _address_tokens(value: str) -> list[str]:
    value = value.lower()
    value = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", value)
    replacements = {
        "northwest": "nw",
        "northeast": "ne",
        "southwest": "sw",
        "southeast": "se",
        "street": "st",
        "avenue": "ave",
        "av": "ave",
        "road": "rd",
        "drive": "dr",
        "boulevard": "blvd",
        "place": "pl",
        "court": "ct",
        "crescent": "cres",
        "trail": "trl",
    }
    for source, target in replacements.items():
        value = re.sub(rf"\b{source}\b", target, value)
    return re.findall(r"[a-z]+|\d+", value)


_STREET_TYPE_TOKENS = {"st", "ave", "rd", "dr", "blvd", "pl", "ct", "cres", "trl"}


def _street_types_conflict(candidate_tokens: list[str], result_text: str) -> bool:
    """True when the result names the candidate's street with a different type.

    Catches "12 Ave SW" being matched against "12th St SW": the token before
    the candidate's street type ("12") appears in the result immediately
    followed by a different street type and never by the right one. Building
    names that merely contain "Place"/"Court" do not trigger a conflict.
    """
    result_sequence = _address_tokens(result_text)
    for index in range(1, len(candidate_tokens)):
        type_token = candidate_tokens[index]
        if type_token not in _STREET_TYPE_TOKENS:
            continue
        street_token = candidate_tokens[index - 1]
        followers = {
            result_sequence[position + 1]
            for position, token in enumerate(result_sequence[:-1])
            if token == street_token and result_sequence[position + 1] in _STREET_TYPE_TOKENS
        }
        if followers and type_token not in followers:
            return True
    return False


def _address_matches(candidate_address: str, result_text: str) -> bool:
    street_address = candidate_address.split(", Calgary", 1)[0].split(", AB", 1)[0]
    candidate_tokens = _address_tokens(street_address)
    result_tokens = set(_address_tokens(result_text))
    significant = [token for token in candidate_tokens if token not in _STREET_TYPE_TOKENS]
    if not any(token.isdigit() for token in significant):
        # Seeds like "Centre Street N" or "Northwest Calgary" carry no house
        # number, so any match against them is guesswork.
        return False
    if _street_types_conflict(candidate_tokens, result_text):
        return False
    return len(significant) >= 2 and all(token in result_tokens for token in significant)


def _address_matches_loose(candidate_address: str, result_text: str) -> bool:
    """Looser but still-safe address match.

    Requires every numeric token (house number, numbered street) plus the
    quadrant token (nw/ne/sw/se) when the candidate has one. Alphabetic
    street-name tokens may be absent because indexed snippets and
    Apartments.com URL slugs often truncate or abbreviate them.
    """
    street_address = candidate_address.split(", Calgary", 1)[0].split(", AB", 1)[0]
    candidate_tokens = _address_tokens(street_address)
    result_tokens = set(_address_tokens(result_text))
    quadrants = {"nw", "ne", "sw", "se"}
    numbers = [token for token in candidate_tokens if token.isdigit()]
    required = list(numbers) + [token for token in candidate_tokens if token in quadrants]
    if len(required) < 2 or not all(token in result_tokens for token in required):
        return False
    if _street_types_conflict(candidate_tokens, result_text):
        # "12 Ave SW" must never match "12th St SW".
        return False
    name_tokens = [
        token
        for token in candidate_tokens
        if not token.isdigit() and token not in quadrants and token not in _STREET_TYPE_TOKENS
    ]
    if name_tokens and not any(token in result_tokens for token in name_tokens):
        # A named street ("Falconridge Gardens NE") must share at least one
        # name word with the result, or the numeric match is coincidence.
        return False
    return True


def _is_direct_apartments_url(url: str) -> bool:
    path_parts = [part for part in urlparse(url).path.lower().split("/") if part]
    if not path_parts:
        return False
    category_roots = {
        "calgary-ab",
        "houses",
        "condos",
        "townhomes",
        "transit",
        "local-guide",
        "rent-market-trends",
    }
    return path_parts[0] not in category_roots


def _yahoo_index_results(query: str) -> list[tuple[str, str, str]]:
    search_url = "https://search.yahoo.com/search?p=" + quote(query)
    request = Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
    page = urlopen(request, timeout=20).read().decode("utf-8", "replace")
    results = []
    for match in re.finditer(
        r"RU=(https?%3a%2f%2f(?:www\.)?apartments\.com%2f.*?)/RK=",
        page,
        re.I,
    ):
        listing_url = unquote(match.group(1))
        result_start = page.rfind("<li", 0, match.start())
        result_end = page.find("</li>", match.end())
        result_markup = page[max(0, result_start) : result_end + 5 if result_end >= 0 else match.end() + 5000]
        result_text = _strip_markup(result_markup)
        title_match = re.search(r"<h3[^>]*>(.*?)</h3>", result_markup, re.I | re.S)
        title = _strip_markup(title_match.group(1)) if title_match else ""
        results.append((listing_url, result_text, title))
    return results


def _bing_index_results(query: str) -> list[tuple[str, str, str]]:
    search_url = "https://www.bing.com/search?q=" + quote(query)
    request = Request(search_url, headers={"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"})
    page = urlopen(request, timeout=20).read().decode("utf-8", "replace")
    results = []
    for block in re.finditer(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', page, re.I | re.S):
        markup = block.group(0)
        href_match = re.search(r'href="(https?://(?:www\.)?apartments\.com/[^"]+)"', markup, re.I)
        if not href_match:
            continue
        result_text = _strip_markup(markup)
        title_match = re.search(r"<h2[^>]*>(.*?)</h2>", markup, re.I | re.S)
        title = _strip_markup(title_match.group(1)) if title_match else ""
        results.append((html.unescape(href_match.group(1)), result_text, title))
    return results


def _duckduckgo_index_results(query: str) -> list[tuple[str, str, str]]:
    search_url = "https://html.duckduckgo.com/html/?q=" + quote(query)
    request = Request(search_url, headers={"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"})
    page = urlopen(request, timeout=20).read().decode("utf-8", "replace")
    results = []
    for block in re.finditer(r'<div class="result[^"]*".*?(?=<div class="result[^"]*"|</div>\s*</div>\s*</body>)', page, re.I | re.S):
        markup = block.group(0)
        href_match = re.search(r"uddg=(https?%3A%2F%2F(?:www\.)?apartments\.com%2F[^&\"]+)", markup, re.I)
        if not href_match:
            continue
        result_text = _strip_markup(markup)
        title_match = re.search(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', markup, re.I | re.S)
        title = _strip_markup(title_match.group(1)) if title_match else ""
        results.append((unquote(href_match.group(1)), result_text, title))
    return results


_SEARCH_INDEXES = (
    ("yahoo", _yahoo_index_results),
    ("bing", _bing_index_results),
    ("duckduckgo", _duckduckgo_index_results),
)


def _seed_queries(seed: dict) -> list[str]:
    queries = []
    street_address = seed.get("address", "").split(", Calgary", 1)[0].strip()
    if street_address:
        queries.append(f'site:apartments.com "{street_address}" Calgary')
        queries.append(f"site:apartments.com {street_address} Calgary")
    building_name = str(seed.get("buildingName", "")).strip()
    if building_name and building_name.lower() != street_address.lower() and len(building_name.split()) >= 2:
        queries.append(f'site:apartments.com "{building_name}" Calgary apartment')
    return queries


def _find_indexed_apartments_page(seed: dict) -> tuple[str, str] | None:
    """Locate the seed's direct Apartments.com property page via public search indexes.

    Tries several query variants across several search indexes; every
    candidate result must still pass address matching against the seed
    (strict first, then a looser numeric+quadrant match) so a wrong or
    stale property page is never returned.
    """
    loose_fallback: tuple[str, str] | None = None
    for query in _seed_queries(seed):
        for _engine, fetch_results in _SEARCH_INDEXES:
            try:
                results = fetch_results(query)
            except Exception:
                continue
            for listing_url, result_text, title in results:
                if not _is_direct_apartments_url(listing_url):
                    continue
                if "calgary" not in urlparse(listing_url).path.lower():
                    # Calgary property slugs always carry "-calgary-ab"; this
                    # rejects same-numbered listings from other cities.
                    continue
                if "0 units available" in result_text.lower():
                    continue
                clean_url = listing_url.split("#", 1)[0]
                match_text = f"{result_text} {clean_url.replace('-', ' ')}"
                clean_title = title or seed.get("buildingName", "")
                if _address_matches(seed.get("address", ""), match_text):
                    return clean_url, clean_title
                if loose_fallback is None and _address_matches_loose(seed.get("address", ""), match_text):
                    loose_fallback = (clean_url, clean_title)
            if results:
                # This index answered; trying further indexes for the same
                # query only re-ranks the same web, so move to the next query.
                break
    return loose_fallback


def _collect_indexed_apartments(
    unit_type: str,
    seed_candidates: list[dict],
    origin: tuple[float, float] | None,
    our_rent: float | None,
) -> list[dict]:
    eligible = []
    for seed in seed_candidates:
        rent = float(seed.get("rentPrice") or 0)
        if our_rent and rent > our_rent:
            continue
        if seed.get("latitude") is None or seed.get("longitude") is None:
            continue
        distance = (
            _haversine_km(origin, (float(seed["latitude"]), float(seed["longitude"])))
            if origin
            else 0
        )
        if origin and distance > 7:
            continue
        eligible.append((distance, rent, seed))
    eligible.sort(key=lambda item: (item[0], item[1]))

    candidates = []
    seen_urls = set()
    for _distance, _rent, seed in eligible[:36]:
        try:
            match = _find_indexed_apartments_page(seed)
        except Exception:
            continue
        if not match:
            continue
        listing_url, indexed_title = match
        if "mainstreet" in " ".join(
            (
                listing_url,
                indexed_title,
                str(seed.get("buildingName", "")),
            )
        ).lower():
            seed["isMainstreet"] = True
            continue
        if listing_url in seen_urls:
            continue
        seen_urls.add(listing_url)
        candidates.append(
            {
                **seed,
                "buildingName": seed.get("buildingName") or indexed_title,
                "unitType": _unit_details(unit_type)[1],
                "comments": (
                    f"Active same-bedroom rent cross-checked through {seed.get('sourceWebsite')}; "
                    "the matching property page was found on Apartments.com."
                ),
                "sourceWebsite": "Apartments.com",
                "listingUrl": listing_url,
                "isVerified": True,
                "verifiedAt": date.today().isoformat(),
            }
        )
        if len(candidates) >= 10:
            break
        time.sleep(0.15)
    return candidates


def _collect_apartments(
    driver,
    unit_type: str,
    seed_candidates: list[dict] | None = None,
    origin: tuple[float, float] | None = None,
    our_rent: float | None = None,
) -> tuple[list[dict], dict]:
    beds, normalized_unit, _, apartments_slug = _unit_details(unit_type)
    url = f"https://www.apartments.com/calgary-ab/{apartments_slug}/"
    _set_user_agent(driver, APARTMENTS_BROWSER_USER_AGENT)
    driver.delete_all_cookies()
    driver.get(url)
    time.sleep(4)
    if "access denied" in driver.title.lower():
        indexed_candidates = _collect_indexed_apartments(
            unit_type,
            seed_candidates or [],
            origin,
            our_rent,
        )
        return indexed_candidates, {
            "name": "Apartments.com",
            "url": url,
            "status": "verified" if indexed_candidates else "blocked",
            "detail": (
                f"Direct search was blocked; {len(indexed_candidates)} matching direct property page(s) "
                "were cross-checked through the public search index."
                if indexed_candidates
                else "Apartments.com refused this browser session and no matching indexed property page was found."
            ),
            "collected": len(indexed_candidates),
        }
    cards = driver.find_elements(By.CSS_SELECTOR, "article")
    candidates = []
    unit_pattern = re.compile(r"\bstudio\b", re.I) if beds == 0 else re.compile(rf"\b{beds}\s+beds?\b", re.I)
    for card in cards:
        lines = [line.strip() for line in card.text.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        unit_index = next((index for index, line in enumerate(lines) if unit_pattern.search(line)), None)
        if unit_index is None:
            continue
        rent = next((_first_number(line) for line in lines[unit_index + 1 :] if "$" in line), None)
        anchors = card.find_elements(By.CSS_SELECTOR, "a[href*='apartments.com']")
        raw_href = next((anchor.get_attribute("href") for anchor in anchors if anchor.get_attribute("href")), "")
        try:
            _ph = urlparse(raw_href).hostname or ""
            href = raw_href if (_ph == "apartments.com" or _ph.endswith(".apartments.com")) else ""
        except Exception:
            href = ""
        if not rent or not href:
            continue
        if "mainstreet" in f"{lines[0]} {href}".lower():
            continue
        candidate = {
                "companyName": "Independent",
                "buildingName": lines[0],
                "address": lines[1],
                "latitude": None,
                "longitude": None,
                "unitType": normalized_unit,
                "rentPrice": rent,
                "promoAdjustedPrice": next(
                    (line for line in lines if "free" in line.lower() or "special" in line.lower()),
                    "",
                ),
                "squareFootage": "",
                "securityDeposit": "",
                "featuresAmenities": "",
                "parking": "",
                "utilitiesNotes": "",
                "comments": "Active same-bedroom listing collected automatically from Apartments.com.",
                "sourceWebsite": "Apartments.com",
                "listingUrl": href.split("#", 1)[0],
                "isVerified": True,
                "verifiedAt": date.today().isoformat(),
                "promotionText": next((line for line in lines if "free" in line.lower() or "special" in line.lower()), ""),
                "sourceListingId": href.split("#", 1)[0],
                "advertisementTitle": lines[0],
            }
        candidates.append(
            _annotate_occupancy(
                candidate,
                beds,
                advertisement_text="\n".join(lines),
            )
        )
    return candidates, {
        "name": "Apartments.com",
        "url": url,
        "status": "verified" if candidates else "no-match",
        "detail": f"Collected {len(candidates)} active exact-bedroom listing(s) automatically.",
        "collected": len(candidates),
    }


def _link_check_verdict(driver, url: str) -> str:
    """Open a detail link in the JavaScript-enabled browser and classify it.

    Returns "dead" only for definitive not-found signals (RentFaster's
    client-side #listingNotFound redirect, explicit 404/not-found pages).
    Challenges, access blocks, and errors are "inconclusive" and the row is
    kept, matching the listing-link liveness policy.
    """
    try:
        driver.get(url)
    except WebDriverException:
        return "inconclusive"
    deadline = time.monotonic() + LINK_CHECK_SECONDS
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            state = driver.execute_script(
                """
                return {
                  href: String(location.href || ""),
                  title: String(document.title || ""),
                  ready: document.readyState,
                  notFoundEl: Boolean(document.querySelector("#listingNotFound")),
                  heading: (document.querySelector("h1") || {}).innerText || "",
                };
                """
            )
        except WebDriverException:
            return "inconclusive"
        href = state.get("href", "").lower()
        title = state.get("title", "").lower()
        heading = str(state.get("heading", "")).lower()
        if state.get("notFoundEl") or "listingnotfound" in href:
            return "dead"
        if "just a moment" in title or "access denied" in title:
            return "inconclusive"
        if re.search(r"\b404\b|page not found|listing not found", f"{title} {heading}"):
            return "dead"
        if state.get("ready") == "complete" and title:
            return "alive"
    return "inconclusive"


def _advertisement_page_text(driver, url: str) -> str:
    try:
        driver.get(url)
    except WebDriverException:
        return ""
    deadline = time.monotonic() + LINK_CHECK_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            state = driver.execute_script(
                """
                return {
                  title: String(document.title || ""),
                  ready: document.readyState,
                  bodyText: String((document.body || {}).innerText || "").slice(0, 120000),
                };
                """
            )
        except WebDriverException:
            return ""
        title = str(state.get("title", "")).lower()
        if "just a moment" in title or "access denied" in title:
            return ""
        body_text = str(state.get("bodyText", ""))
        if state.get("ready") == "complete" and body_text:
            return body_text
    return ""


def _classify_occupancy_pages(candidates: list[dict], target_beds: int) -> None:
    to_inspect = [
        row
        for row in candidates
        if row.get("sourceWebsite") == "RentFaster"
        and row.get("_occupancyInspectionRequested")
        and row.get("occupancyClassification") != "shared-room"
        and row.get("listingUrl")
    ]
    if to_inspect:
        try:
            driver = _get_fresh_driver()
        except Exception as error:
            print(f"occupancy_check_unavailable error={error!r}", flush=True)
            driver = None
        if driver is not None:
            try:
                for row in to_inspect:
                    page_text = _advertisement_page_text(driver, row["listingUrl"])
                    if not page_text:
                        continue
                    unit_match, unit_reason = _unit_description_match(
                        str(row.get("advertisementTitle") or row.get("buildingName") or ""),
                        target_beds,
                        page_text,
                    )
                    row["advertisedUnitMatch"] = unit_match
                    row["advertisedUnitReason"] = unit_reason
                    reason, evidence = _shared_room_evidence(
                        page_text,
                        target_beds,
                        _rent_value(row.get("rentPrice")),
                    )
                    if reason:
                        row["occupancyClassification"] = "shared-room"
                        row["occupancyReason"] = reason
                        row["occupancyEvidence"] = evidence
                        existing_text = str(row.get("advertisementText") or "").strip()
                        row["advertisementText"] = "\n".join(
                            part for part in (existing_text, evidence) if part
                        )[:8000]
                    elif row.get("occupancyClassification") == "unknown":
                        reason, evidence = _full_unit_evidence(page_text)
                        if reason:
                            row["occupancyClassification"] = "full-unit"
                            row["occupancyReason"] = reason
                            row["occupancyEvidence"] = evidence
            finally:
                _shutdown_browser()
    for row in candidates:
        row.pop("_occupancyInspectionRequested", None)


def _remove_dead_links(candidates: list[dict]) -> list[dict]:
    """Drop only confirmed-dead detail links, checked in a real browser."""
    checkable = {"RentFaster", "Rentals.ca", "Apartments.com"}
    to_check = [row for row in candidates if row.get("sourceWebsite") in checkable]
    if not to_check:
        return candidates
    per_source_counts: dict[str, int] = {}
    dead_urls = set()
    try:
        driver = _get_fresh_driver()
    except Exception as error:
        print(f"link_check_unavailable error={error!r}", flush=True)
        return candidates
    try:
        for row in to_check:
            source = row.get("sourceWebsite", "")
            url = row.get("listingUrl", "")
            if not url:
                continue
            per_source_counts[source] = per_source_counts.get(source, 0) + 1
            if per_source_counts[source] > MAX_LINK_CHECKS_PER_SOURCE:
                continue
            verdict = _link_check_verdict(driver, url)
            print(f"link_check source={source!r} url={url!r} verdict={verdict!r}", flush=True)
            if verdict == "dead":
                dead_urls.add(url)
    finally:
        _shutdown_browser()
    if not dead_urls:
        return candidates
    return [row for row in candidates if row.get("listingUrl") not in dead_urls]


def collect_browser_candidates(
    unit_type: str,
    origin: tuple[float, float] | None = None,
    our_rent: float | None = None,
) -> dict:
    with BROWSER_LOCK:
        candidates = []
        statuses = []

        def run_source(source_name: str, collector) -> list[dict]:
            started_at = time.monotonic()
            collector_error = None
            try:
                driver = _get_fresh_driver()
                rows, status = collector(driver)
            except Exception as error:
                collector_error = error
                rows = []
                status = {
                    "name": source_name,
                    "url": "",
                    "status": "unavailable",
                    "detail": f"Automatic collection failed: {error}",
                    "collected": 0,
                }
            finally:
                _shutdown_browser()
            _log_source(source_name, unit_type, status, started_at, collector_error)
            statuses.append(status)
            return rows

        candidates.extend(run_source("RentFaster", lambda driver: _collect_rentfaster(driver, unit_type)))
        candidates.extend(
            run_source("Rentals.ca", lambda driver: _collect_rentals(driver, unit_type, origin, our_rent))
        )
        apartment_seeds = list(candidates)
        candidates.extend(
            run_source(
                "Apartments.com",
                lambda driver: _collect_apartments(driver, unit_type, apartment_seeds, origin, our_rent),
            )
        )
        target_beds = _unit_details(unit_type)[0]
        _classify_occupancy_pages(candidates, target_beds)
        candidates = _remove_dead_links(candidates)
        for status in statuses:
            survivors = sum(1 for row in candidates if row.get("sourceWebsite") == status.get("name"))
            if status.get("status") == "verified" and survivors < status.get("collected", 0):
                status["collected"] = survivors
                status["detail"] = (
                    f"{status.get('detail', '')} "
                    f"{survivors} listing(s) remained after dead-link checks."
                ).strip()
                if not survivors:
                    status["status"] = "no-match"
        return {"candidates": candidates, "statuses": statuses}
