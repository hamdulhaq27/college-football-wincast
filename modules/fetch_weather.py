"""
fetch_weather.py
=================================
Weather data enrichment using WeatherAPI.com + Open-Meteo.
- WeatherAPI.com (history/forecast) when key available.
- Open-Meteo (archive/forecast) as fallback or primary.
- Venue-first geolocation (CFBD venues), then venue city, then home/away city, then location_map fallback.
- Results normalized to: Condition, Temp(°F), Wind(mph), PrecipProb(%).
- Caches responses in 'data/weather_cache.json'.
"""

import os
import json
import time
import logging
import requests
import pandas as pd
from datetime import datetime
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# -------- optional venue resolver (safe import) --------
try:
    from .venues import get_venue_coords  # returns (lat, lon) or (None, None)
except Exception:
    def get_venue_coords(_venue_id: Optional[str]):
        return (None, None)

CACHE_PATH = os.path.join("data", "weather_cache.json")
os.makedirs("data", exist_ok=True)

WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "").strip()
PROVIDER_PREFERENCE = ["weatherapi", "open-meteo"] if WEATHERAPI_KEY else ["open-meteo"]

# =============================
# CFBD helpers (auth + venues)
# =============================
def _cfbd_headers() -> dict:
    key = os.getenv("CFBD_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}

def _get_venue_coords_cfbd(venue_id: str) -> Tuple[Optional[float], Optional[float], dict]:
    """
    Direct CFBD /venues query with Authorization header (fixes 401).
    Returns (lat, lon, raw_json) — lat/lon can be None if not found.
    """
    if not venue_id:
        return (None, None, {})
    try:
        url = "https://api.collegefootballdata.com/venues"
        r = requests.get(url, headers=_cfbd_headers(), params={"id": venue_id}, timeout=15)
        r.raise_for_status()
        data = r.json() or []
        if not data:
            return (None, None, {})
        v = data[0]
        # CFBD venues usually have 'latitude' and 'longitude'
        lat = v.get("latitude")
        lon = v.get("longitude")
        if lat is None or lon is None:
            # alguns dumps antigos têm 'location' como string "lat,lon"
            loc = v.get("location")
            if isinstance(loc, str) and "," in loc:
                try:
                    lat_s, lon_s = [s.strip() for s in loc.split(",", 1)]
                    lat, lon = float(lat_s), float(lon_s)
                except Exception:
                    pass
        return (float(lat) if lat is not None else None,
                float(lon) if lon is not None else None,
                v)
    except Exception as e:
        logger.warning(f"[Venues] Failed to fetch venue id={venue_id}: {e}")
        return (None, None, {})

# -----------------------------
# Cache helpers
# -----------------------------
def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[Weather] Failed to save cache: {e}")

# -----------------------------
# Providers
# -----------------------------
def om_fetch(lat: float, lon: float, date_utc: pd.Timestamp) -> dict | None:
    """Open-Meteo (archive for past, forecast for today/future)."""
    date_utc = pd.to_datetime(date_utc, errors="coerce", utc=True)
    if pd.isna(date_utc):
        return None
    date_str = date_utc.strftime("%Y-%m-%d")
    today_utc = pd.Timestamp.utcnow().normalize()

    is_archive = (date_utc.normalize() < today_utc)
    if is_archive:
        base_url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_str,
            "end_date": date_str,
            "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_sum",
            "timezone": "UTC",
        }
    else:
        base_url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_str,
            "end_date": date_str,
            "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_probability_max",
            "timezone": "UTC",
        }

    try:
        r = requests.get(base_url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        times = daily.get("time", [])
        if not daily or not times:
            logger.debug(f"[OpenMeteo] Empty daily for {lat},{lon} on {date_str} | archive={is_archive}")
            return None
        try:
            idx = times.index(date_str)
        except ValueError:
            idx = 0

        tmax = _safe_idx(daily.get("temperature_2m_max", []), idx)
        tmin = _safe_idx(daily.get("temperature_2m_min", []), idx)
        wind = _safe_idx(daily.get("windspeed_10m_max", []), idx)

        temp_c = None
        if tmax is not None and tmin is not None:
            temp_c = (float(tmax) + float(tmin)) / 2.0

        if is_archive:
            precip_sum = _safe_idx(daily.get("precipitation_sum", []), idx)
            precip_prob = 100 if (precip_sum is not None and float(precip_sum) > 0.0) else 0
        else:
            precip_prob = _safe_idx(daily.get("precipitation_probability_max", []), idx)
            precip_prob = int(precip_prob) if precip_prob is not None else 0

        temp_f = _c_to_f(temp_c) if temp_c is not None else None
        condition = _derive_condition(temp_f=temp_f, precip_prob=precip_prob)

        return {
            "Condition": condition,
            "Temp": round(temp_f, 1) if temp_f is not None else None,
            "Wind": round(_kmh_to_mph(wind), 1) if wind is not None else None,
            "PrecipProb": int(precip_prob) if precip_prob is not None else 0,
            "Data_Source": "Open-Meteo-Archive" if is_archive else "Open-Meteo-Forecast",
            "Last_Updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
    except Exception as e:
        logger.warning(f"⚠️ Open-Meteo failed ({lat},{lon},{date_str}): {e}")
        return None

def wapi_fetch(lat: float, lon: float, date_utc: pd.Timestamp) -> dict | None:
    """WeatherAPI.com (history for past, forecast for today/future)."""
    if not WEATHERAPI_KEY:
        return None

    date_utc = pd.to_datetime(date_utc, errors="coerce", utc=True)
    if pd.isna(date_utc):
        return None
    date_str = date_utc.strftime("%Y-%m-%d")
    today_utc = pd.Timestamp.utcnow().normalize()

    is_history = (date_utc.normalize() < today_utc)
    base = "http://api.weatherapi.com/v1/history.json" if is_history else "http://api.weatherapi.com/v1/forecast.json"

    params = {"key": WEATHERAPI_KEY, "q": f"{lat},{lon}", "dt": date_str}
    if not is_history:
        params["days"] = 1

    try:
        r = requests.get(base, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        days = data.get("forecast", {}).get("forecastday", [])
        if not days:
            logger.debug(f"[WeatherAPI] No forecastday for {lat},{lon} on {date_str} | history={is_history}")
            return None
        day = days[0].get("day", {})
        if not day:
            return None

        temp_f = _to_float(day.get("avgtemp_f"))
        wind_mph = _to_float(day.get("maxwind_mph"))
        precip_prob = day.get("daily_chance_of_rain")
        if precip_prob is None:
            precip_in = _to_float(day.get("totalprecip_in"))
            precip_prob = 100 if (precip_in is not None and precip_in > 0.0) else 0
        else:
            precip_prob = int(precip_prob)

        cond_text = day.get("condition", {}).get("text", "") or "Unknown"
        condition = _derive_condition(temp_f=temp_f, precip_prob=precip_prob, fallback_text=cond_text)

        return {
            "Condition": condition,
            "Temp": round(temp_f, 1) if temp_f is not None else None,
            "Wind": round(wind_mph, 1) if wind_mph is not None else None,
            "PrecipProb": precip_prob if precip_prob is not None else 0,
            "Data_Source": "WeatherAPI-History" if is_history else "WeatherAPI-Forecast",
            "Last_Updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
    except Exception as e:
        logger.warning(f"⚠️ WeatherAPI failed ({lat},{lon},{date_str}): {e}")
        return None

# -----------------------------
# Unit helpers & condition
# -----------------------------
def _c_to_f(c):
    return (c * 9/5) + 32 if c is not None else None

def _kmh_to_mph(kmh):
    return kmh * 0.621371 if kmh is not None else None

def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None

def _safe_idx(seq, i):
    try:
        return seq[i]
    except Exception:
        return None

def _derive_condition(temp_f: float | None, precip_prob: int | None, fallback_text: str = "") -> str:
    """Coarse condition bucketing to keep the model simple."""
    try:
        if temp_f is not None and temp_f < 32:
            return "Snow"
        if precip_prob is not None and precip_prob > 60:
            return "Rain"
        if precip_prob is not None and precip_prob > 20:
            return "Cloudy"
        low = fallback_text.lower()
        if any(k in low for k in ["snow", "flurr", "blizzard"]):
            return "Snow"
        if any(k in low for k in ["rain", "shower", "drizzle", "thunder"]):
            return "Rain"
        if any(k in low for k in ["cloud", "overcast"]):
            return "Cloudy"
        return "Sunny"
    except Exception:
        return "Sunny"

# -----------------------------
# City → coordinates (fallback DB)
# -----------------------------
CITY_COORDS = {
    "Tuscaloosa, AL": (33.21, -87.57),
    "Austin, TX": (30.27, -97.74),
    "Athens, GA": (33.96, -83.38),
    "Gainesville, FL": (29.65, -82.32),
    "Columbus, OH": (39.96, -83.00),
}

# -----------------------------
# Coord resolver (priority logic)
# -----------------------------
def _resolve_coords(row: pd.Series, location_map: dict) -> tuple[Optional[float], Optional[float], str]:
    """
    Priority:
      1) VenueID -> venues.get_venue_coords() -> CFBD /venues (auth) fallback
      2) VenueCity/VenueState -> CITY_COORDS
      3) If Is_Home: city of Team; else: city of Opponent (location_map)
      4) Fallback: location_map[Team]
    Returns (lat, lon, origin_tag).
    """
    # 1) VenueID
    venue_id = row.get("VenueID")
    if pd.notna(venue_id) and str(venue_id).strip():
        lat, lon = get_venue_coords(str(venue_id))
        if lat is None or lon is None:
            lat, lon, _ = _get_venue_coords_cfbd(str(venue_id))  # <-- usa Authorization header
        if lat is not None and lon is not None:
            return (float(lat), float(lon), "venue")

    # 2) Venue city/state
    venue_city = (row.get("VenueCity") or "").strip()
    venue_state = (row.get("VenueState") or "").strip()
    if venue_city and venue_state:
        key = f"{venue_city}, {venue_state}"
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return (float(lat), float(lon), "venue_city")

    # 3) Home vs Away city
    is_home = bool(row.get("Is_Home"))
    team = row.get("Team")
    opp = row.get("Opponent")
    city = None
    if is_home and team in location_map:
        city = location_map[team]
    elif (not is_home) and opp in location_map:
        city = location_map[opp]
    if city and city in CITY_COORDS:
        lat, lon = CITY_COORDS[city]
        return (float(lat), float(lon), "homeaway_city")

    # 4) Fallback: team city only
    if team in location_map and location_map[team] in CITY_COORDS:
        lat, lon = CITY_COORDS[location_map[team]]
        return (float(lat), float(lon), "team_city_fallback")

    return (None, None, "unknown")

# -----------------------------
# Public: enrich_with_weather
# -----------------------------
def enrich_with_weather(df: pd.DataFrame, location_map: dict, providers: list[str] = None) -> pd.DataFrame:
    """
    Add weather columns (Condition, Temp, Wind, PrecipProb) to df.
    - Accepts tz-aware/naive Date; NaT rows are skipped safely.
    - Leverages stadium coords when available for precise weather.
    - providers: override preference (e.g., ["open-meteo","weatherapi"])
    """
    providers = providers or PROVIDER_PREFERENCE
    cache = load_cache()

    rows = []
    miss_date = miss_loc = 0
    used_venue = used_city = 0
    no_weather_count = 0

    for _, row in df.iterrows():
        date = pd.to_datetime(row.get("Date"), errors="coerce", utc=True)
        if pd.isna(date):
            miss_date += 1
            rows.append(_empty_weather())
            continue

        lat, lon, origin = _resolve_coords(row, location_map)
        if lat is None or lon is None:
            miss_loc += 1
            rows.append(_empty_weather())
            continue

        if origin in ("venue", "venue_city"):
            used_venue += 1
        else:
            used_city += 1

        cache_key = f"{round(lat,4)},{round(lon,4)}|{date.strftime('%Y-%m-%d')}"
        if cache_key in cache:
            rows.append(cache[cache_key])
            continue

        weather_obj = None
        for prov in providers:
            if prov == "weatherapi":
                weather_obj = wapi_fetch(lat, lon, date)
            elif prov == "open-meteo":
                weather_obj = om_fetch(lat, lon, date)
            if weather_obj:
                break

        if not weather_obj:
            no_weather_count += 1
            logger.debug(f"[Weather] No provider data for {cache_key} (origin={origin})")
            weather_obj = _empty_weather()

        cache[cache_key] = weather_obj
        save_cache(cache)
        time.sleep(0.3)  # be nice to APIs

        rows.append(weather_obj)

    if miss_date or miss_loc:
        logger.info(f"[Weather] Skipped rows -> missing_date={miss_date}, missing_loc={miss_loc}")
    logger.info(f"[Weather] Loc origin usage -> venue={used_venue}, city_based={used_city}")
    if no_weather_count:
        logger.info(f"[Weather] Provider misses -> {no_weather_count} dates/locations returned no data")

    weather_df = pd.DataFrame(rows)
    return pd.concat([df.reset_index(drop=True), weather_df.reset_index(drop=True)], axis=1)

def _empty_weather():
    return {
        "Condition": None,
        "Temp": None,
        "Wind": None,
        "PrecipProb": None,
        "Data_Source": None,
        "Last_Updated": None
    }
