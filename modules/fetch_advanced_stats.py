# modules/fetch_advanced_stats.py
from __future__ import annotations

import os
import logging
from functools import lru_cache
from typing import Optional, List, Dict, Any

import requests
import pandas as pd

logger = logging.getLogger(__name__)
BASE_URL = "https://api.collegefootballdata.com"

def _get_cfbd_key() -> str | None:
    key = os.getenv("CFBD_API_KEY") or os.getenv("CFB_API_KEY")
    if key:
        key = key.strip()
        if len(key) >= 10:
            return key
    return None

def _auth_headers() -> dict:
    key = _get_cfbd_key()
    return {"Authorization": f"Bearer {key}"} if key else {}

def _get(url: str, params: dict, timeout: int = 25) -> requests.Response:
    headers = _auth_headers()
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    try:
        resp.raise_for_status()
        return resp
    except requests.HTTPError as e:
        if resp.status_code == 401:
            key = _get_cfbd_key() or ""
            print(
                f"⚠️ CFBD 401 Unauthorized at {url} with params={params}. "
                f"Using key={'*'*len(key) if len(key)<8 else key[:4]+'...'+key[-4:]}. Check .env."
            )
        raise

def _pick_first(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None

def _to_num(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(pd.NA, index=pd.RangeIndex(0), dtype="float")
    return pd.to_numeric(series, errors="coerce")

@lru_cache(maxsize=32)
def fetch_sp_plus(season: int) -> pd.DataFrame:
    """SP+ → Team, SP_Off, SP_Def, SP_Total."""
    try:
        r = _get(f"{BASE_URL}/ratings/sp", params={"year": season}, timeout=20)
        data: List[Dict[str, Any]] = r.json() or []
        if not data:
            return pd.DataFrame(columns=["Team", "SP_Off", "SP_Def", "SP_Total"])

        raw = pd.DataFrame(data)
        team_col = _pick_first(raw, ["team", "school"])
        off_col  = _pick_first(raw, ["offense", "off_rating", "off", "sp_offense"])
        def_col  = _pick_first(raw, ["defense", "def_rating", "def", "sp_defense"])
        tot_col  = _pick_first(raw, ["rating", "sp", "sp_total"])

        if team_col is None:
            logger.warning("[SP+] Missing team field.")
            return pd.DataFrame(columns=["Team", "SP_Off", "SP_Def", "SP_Total"])

        out = pd.DataFrame()
        out["Team"] = raw[team_col].astype("string")
        out["SP_Off"] = _to_num(raw[off_col]) if off_col else pd.NA
        out["SP_Def"] = _to_num(raw[def_col]) if def_col else pd.NA
        out["SP_Total"] = _to_num(raw[tot_col]) if tot_col else pd.NA
        return out.drop_duplicates("Team")
    except Exception as e:
        print(f"⚠️ Failed to fetch SP+ ratings: {e}")
        return pd.DataFrame(columns=["Team", "SP_Off", "SP_Def", "SP_Total"])

@lru_cache(maxsize=32)
def fetch_fpi(season: int) -> pd.DataFrame:
    """FPI → Team, FPI_Total, FPI_Off, FPI_Def."""
    try:
        r = _get(f"{BASE_URL}/ratings/fpi", params={"year": season}, timeout=20)
        data: List[Dict[str, Any]] = r.json() or []
        if not data:
            return pd.DataFrame(columns=["Team", "FPI_Total", "FPI_Off", "FPI_Def"])

        raw = pd.DataFrame(data)
        team_col  = _pick_first(raw, ["team", "school"])
        total_col = _pick_first(raw, ["fpi", "rating", "fpi_total", "overall"])
        off_col   = _pick_first(raw, ["offense", "off_rating", "off", "fpi_offense"])
        def_col   = _pick_first(raw, ["defense", "def_rating", "def", "fpi_defense"])

        if team_col is None:
            logger.warning("[FPI] Missing team field.")
            return pd.DataFrame(columns=["Team", "FPI_Total", "FPI_Off", "FPI_Def"])

        out = pd.DataFrame()
        out["Team"] = raw[team_col].astype("string")
        out["FPI_Total"] = _to_num(raw[total_col]) if total_col else pd.NA
        out["FPI_Off"]   = _to_num(raw[off_col]) if off_col else pd.NA
        out["FPI_Def"]   = _to_num(raw[def_col]) if def_col else pd.NA
        return out.drop_duplicates("Team")
    except Exception as e:
        print(f"⚠️ Failed to fetch FPI ratings: {e}")
        return pd.DataFrame(columns=["Team", "FPI_Total", "FPI_Off", "FPI_Def"])

@lru_cache(maxsize=32)
def fetch_team_advanced_stats(season: int) -> pd.DataFrame:
    """EPA/play, Success Rate, Havoc Rate (team-level)."""
    try:
        r = _get(f"{BASE_URL}/stats/season", params={"year": season}, timeout=25)
        data = r.json() or []
        if not data:
            return pd.DataFrame(columns=["Team", "EPA_per_Play", "Success_Rate", "Havoc_Rate"])

        records: List[Dict[str, Any]] = []
        for item in data:
            team = item.get("team")
            stats_list = item.get("stats") or []
            categories: Dict[str, Any] = {}
            for cat in stats_list:
                if isinstance(cat, dict):
                    c = cat.get("category")
                    v = cat.get("stat")
                    if c is not None:
                        categories[c] = v

            epa = categories.get("EPA/play") or categories.get("EPA per play")
            success = categories.get("Success Rate") or categories.get("Success rate")
            havoc = categories.get("Havoc Rate") or categories.get("Havoc rate")

            if team:
                records.append(
                    {
                        "Team": str(team),
                        "EPA_per_Play": float(epa) if epa not in (None, "") else None,
                        "Success_Rate": float(success) if success not in (None, "") else None,
                        "Havoc_Rate": float(havoc) if havoc not in (None, "") else None,
                    }
                )

        out = pd.DataFrame(records)
        if not out.empty:
            out["Team"] = out["Team"].astype("string")
        return out.drop_duplicates("Team")
    except Exception as e:
        print(f"⚠️ Failed to fetch team advanced stats: {e}")
        return pd.DataFrame(columns=["Team", "EPA_per_Play", "Success_Rate", "Havoc_Rate"])

def fetch_massey(season: int, path: Optional[str] = None) -> pd.DataFrame:
    """
    Optional local CSV loader: expects columns 'team' + 'massey' (and optional 'season').
    """
    path = path or os.getenv("MASSEY_CSV")
    if not path:
        return pd.DataFrame(columns=["Team", "Massey"])
    try:
        df = pd.read_csv(path)
        if df.empty:
            return pd.DataFrame(columns=["Team", "Massey"])
        if "season" in df.columns:
            df = df[df["season"] == season].copy()
        rename = {}
        if "team" in df.columns:
            rename["team"] = "Team"
        if "massey" in df.columns:
            rename["massey"] = "Massey"
        df = df.rename(columns=rename)
        if "Team" not in df.columns:
            logger.info("[Massey] Missing 'team' column in CSV; skipping.")
            return pd.DataFrame(columns=["Team", "Massey"])
        df["Team"] = df["Team"].astype("string")
        if "Massey" in df.columns:
            df["Massey"] = pd.to_numeric(df["Massey"], errors="coerce")
        else:
            df["Massey"] = pd.NA
        return df[["Team", "Massey"]].drop_duplicates("Team")
    except FileNotFoundError:
        logger.info(f"[Massey] File not found at {path}; skipping.")
        return pd.DataFrame(columns=["Team", "Massey"])
    except Exception as e:
        logger.warning(f"[Massey] Failed to load {path}: {e}")
        return pd.DataFrame(columns=["Team", "Massey"])

def fetch_advanced_team_metrics(season: int = 2024) -> pd.DataFrame:
    """Combine SP+, FPI, (optional) Massey, and team advanced stats."""
    print(f"📊 Fetching advanced team metrics for {season}...")

    df_sp  = fetch_sp_plus(season)
    df_fpi = fetch_fpi(season)
    df_adv = fetch_team_advanced_stats(season)
    df_mas = fetch_massey(season)  # optional; empty if not available

    df = df_sp.merge(df_fpi, on="Team", how="outer")
    if not df_mas.empty:
        df = df.merge(df_mas, on="Team", how="outer")
    df = df.merge(df_adv, on="Team", how="outer")

    sort_col = "SP_Total" if "SP_Total" in df.columns else ("FPI_Total" if "FPI_Total" in df.columns else None)
    if sort_col:
        df = df.sort_values(sort_col, ascending=False, na_position="last")

    df = df.drop_duplicates("Team").reset_index(drop=True)
    print(f"✅ Retrieved advanced stats for {len(df)} teams.")
    return df
