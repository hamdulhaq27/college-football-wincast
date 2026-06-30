# modules/fetch_cfb.py
from __future__ import annotations

import os
import json
import logging
import requests
import pandas as pd

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("winCast_debug.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

BASE_URL = "https://api.collegefootballdata.com"

def _get_headers():
    key = os.getenv("CFBD_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CFBD_API_KEY not found")
    return {"Authorization": f"Bearer {key}"}

def _infer_week_anchor_date(season: int, week: int) -> pd.Timestamp | None:
    """Look up /calendar and infer an anchor date for a regular-season week."""
    try:
        url = f"{BASE_URL}/calendar"
        params = {"year": season}
        r = requests.get(url, headers=_get_headers(), params=params, timeout=20)
        r.raise_for_status()
        cal = r.json() or []

        block = next(
            (x for x in cal if x.get("seasonType") == "regular" and x.get("week") == week),
            None
        )
        if not block:
            logger.warning(f"[CFB] Calendar: no block for season={season}, week={week}")
            return None

        for key in ("firstGameStart", "startDate", "lastGameStart", "endDate"):
            raw = block.get(key)
            if raw:
                anchor = pd.to_datetime(raw, errors="coerce", utc=True)
                if pd.notna(anchor):
                    return anchor

        logger.warning(f"[CFB] Calendar: no valid date fields for season={season}, week={week}")
        return None

    except Exception as e:
        logger.warning(f"[CFB] Calendar fallback failed for {season} w{week}: {e}")
        return None

def fetch_games_cfbd(season: int, week: int | None = None, team: str | None = None) -> pd.DataFrame:
    """
    Pull games from CFBD, normalize fields, add venue metadata, and ensure Date.
    """
    try:
        headers = _get_headers()
    except RuntimeError as e:
        logger.error(f"❌ {e}")
        return pd.DataFrame()

    url = f"{BASE_URL}/games"
    params: dict = {"year": season, "seasonType": "regular"}
    if week:
        params["week"] = week
    if team:
        params["team"] = team

    try:
        logger.info(f"🌐 Requesting CFBD data | Params: {params}")
        r = requests.get(url, headers=headers, params=params, timeout=25)
        logger.debug(f"HTTP Status: {r.status_code} | URL: {r.url}")
        r.raise_for_status()
        data = r.json()

        debug_path = f"games_raw_{season}_week{week or 'ALL'}.json"
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"🧾 Saved raw JSON for inspection: {debug_path} ({len(data)} records)")
        except Exception as dump_err:
            logger.warning(f"Could not save debug JSON: {dump_err}")

        if not data:
            logger.warning(f"No data returned for {season} week {week or 'ALL'}")
            return pd.DataFrame()

        records: list[dict] = []
        for g in data:
            home = g.get("home_team") or g.get("homeTeam")
            away = g.get("away_team") or g.get("awayTeam")
            if not home or not away:
                continue

            # primary date from /games
            date_str = g.get("start_date") or g.get("startTime")
            date = pd.to_datetime(date_str, errors="coerce", utc=True) if date_str else pd.NaT

            # scores (skip games without any score)
            home_pts = g.get("home_points") or g.get("homePoints") or 0
            away_pts = g.get("away_points") or g.get("awayPoints") or 0
            if not (home_pts or away_pts):
                continue

            game_id_raw = g.get("id") or g.get("game_id")
            game_id = str(game_id_raw) if game_id_raw is not None else None

            venue_id = g.get("venue_id") or g.get("venueId")
            venue_name = g.get("venue") or g.get("venueName")
            neutral_site = bool(g.get("neutral_site") or g.get("neutralSite") or False)
            venue_city = g.get("venue_city") or g.get("venueCity")
            venue_state = g.get("venue_state") or g.get("venueState")

            # home row
            records.append({
                "GameID": game_id,
                "Date": date,
                "Team": home,
                "Opponent": away,
                "Is_Home": True,
                "TDs_For": float(home_pts) / 7.0,
                "TDs_Against": float(away_pts) / 7.0,
                "VenueID": str(venue_id) if venue_id is not None else None,
                "Venue": venue_name,
                "VenueCity": venue_city,
                "VenueState": venue_state,
                "NeutralSite": neutral_site,
            })
            # away row
            records.append({
                "GameID": game_id,
                "Date": date,
                "Team": away,
                "Opponent": home,
                "Is_Home": False,
                "TDs_For": float(away_pts) / 7.0,
                "TDs_Against": float(home_pts) / 7.0,
                "VenueID": str(venue_id) if venue_id is not None else None,
                "Venue": venue_name,
                "VenueCity": venue_city,
                "VenueState": venue_state,
                "NeutralSite": neutral_site,
            })

        df = pd.DataFrame(records)
        if df.empty:
            logger.warning(f"All records for week {week} were invalid or unplayed.")
            return df

        # normalize Date
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce", utc=True)
        nulls_before = int(df["Date"].isna().sum())
        if nulls_before > 0 and week is not None:
            anchor = _infer_week_anchor_date(season, week)
            if pd.notna(anchor):
                df.loc[df["Date"].isna(), "Date"] = anchor
                nulls_after = int(df["Date"].isna().sum())
                filled = nulls_before - nulls_after
                logger.info(
                    f"[CFB] Filled {filled} missing dates with calendar anchor {anchor.date()} for week {week} "
                    f"(remaining NaT: {nulls_after})."
                )
            else:
                logger.warning(f"[CFB] Could not infer calendar date for season={season}, week={week}.")
        logger.debug(f"Converted Date to datetime64[ns, UTC] | Nulls: {df['Date'].isna().sum()}")

        # friendly dtypes for merges
        for col in ["GameID", "Team", "Opponent", "VenueID"]:
            df[col] = df[col].astype("string")
        df["Is_Home"] = df["Is_Home"].astype("boolean")

        logger.info(f"✅ {len(df)} valid team records parsed for week {week}.")
        logger.debug(f"Sample valid data:\n{df.head().to_string()}")
        return df

    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ HTTP error while fetching CFBD games: {e}")
    except requests.exceptions.RequestException as e:
        logger.error(f"🌐 Network error: {e}")
    except Exception as e:
        logger.exception(f"⚠️ Unexpected error while fetching CFBD data: {e}")

    return pd.DataFrame(columns=[
        "GameID","Date","Team","Opponent","TDs_For","TDs_Against","Is_Home",
        "VenueID","Venue","VenueCity","VenueState","NeutralSite"
    ])
