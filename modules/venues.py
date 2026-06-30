import os
import json
import time
import logging
import requests
from typing import Tuple, Optional

logger = logging.getLogger(__name__)
BASE_URL = "https://api.collegefootballdata.com"
CACHE_FILE = os.path.join("data", "venues_cache.json")
os.makedirs("data", exist_ok=True)

# ----------------------------
# Auth helpers
# ----------------------------
def _get_cfbd_key() -> Optional[str]:
    key = os.getenv("CFBD_API_KEY") or os.getenv("CFB_API_KEY")
    if key:
        key = key.strip()
        if len(key) >= 10:
            return key
    return None

def _auth_headers() -> dict:
    key = _get_cfbd_key()
    return {"Authorization": f"Bearer {key}"} if key else {}

# ----------------------------
# Cache helpers
# ----------------------------
def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def _to_float(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None

# ----------------------------
# Public API
# ----------------------------
def get_venue_coords(venue_id: str | None) -> Tuple[Optional[float], Optional[float]]:
    """Retorna (lat, lon) para um VenueID usando cache + /venues."""
    if not venue_id:
        return (None, None)

    vid = str(venue_id)
    cache = _load_cache()
    if vid in cache:
        v = cache[vid] or {}
        return (_to_float(v.get("lat")), _to_float(v.get("lon")))

    # Retry a couple times for transient 5xx/network issues
    url = f"{BASE_URL}/venues"
    params = {"id": vid}
    headers = _auth_headers()

    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            arr = r.json() or []
            if not arr:
                logger.warning(f"[Venues] Venue not found for id={vid}")
                cache[vid] = {"lat": None, "lon": None}
                _save_cache(cache)
                return (None, None)

            v = arr[0] if isinstance(arr, list) else arr
            lat = _to_float(v.get("latitude"))
            lon = _to_float(v.get("longitude"))

            cache[vid] = {"lat": lat, "lon": lon}
            _save_cache(cache)
            return (lat, lon)
        except requests.HTTPError as e:
            # 401 is almost always missing/invalid key
            if r is not None and r.status_code == 401:
                logger.warning(
                    f"[Venues] 401 Unauthorized for id={vid}. "
                    "Verifique CFBD_API_KEY no .env"
                )
                break  # no point retrying without valid auth
            last_err = e
        except Exception as e:
            last_err = e

        # backoff before retry (only for non-401 cases)
        time.sleep(0.5 * (2 ** attempt))

    logger.warning(f"[Venues] Failed to fetch venue id={vid}: {last_err}")
    return (None, None)
