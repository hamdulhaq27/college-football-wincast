# modules/odds_value_analysis.py
from __future__ import annotations

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
from typing import Iterable, Optional

logger = logging.getLogger(__name__)
BASE_URL = "https://api.collegefootballdata.com"

DEFAULT_PROVIDER_PRIORITY: list[str] = [
    "DraftKings", "FanDuel", "Caesars", "BetMGM", "PointsBet", "Barstool", "Wynn"
]

def _log_warn(msg: str):
    logger.warning(msg)
    try:
        print(msg)
    except Exception:
        pass

def _provider_name(p) -> Optional[str]:
    if isinstance(p, dict):
        return p.get("name")
    if isinstance(p, str):
        return p
    return None

def _auth_headers() -> dict:
    key = os.getenv("CFBD_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}

def fetch_game_odds(
    season: int,
    week: int,
    provider_priority: Optional[Iterable[str]] = None,
    timeout: int = 25,
    retries: int = 2,
    backoff: float = 0.75,
) -> pd.DataFrame:
    """Fetch moneyline/spread/total, normalized to a tidy table."""
    url = f"{BASE_URL}/lines"
    params = {"year": season, "week": week, "seasonType": "regular"}
    headers = _auth_headers()

    data = []
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json() or []
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            _log_warn(f"⚠️ Failed to fetch odds after retries: {e}")
            return pd.DataFrame(columns=[
                "GameID","Team","Opponent","Home_Team","Away_Team",
                "Home_ML","Away_ML","Spread","Total","Provider"
            ])

    if not data:
        _log_warn(f"⚠️ No odds data found for {season} Week {week}.")
        return pd.DataFrame(columns=[
            "GameID","Team","Opponent","Home_Team","Away_Team",
            "Home_ML","Away_ML","Spread","Total","Provider"
        ])

    provider_priority = list(provider_priority) if provider_priority else DEFAULT_PROVIDER_PRIORITY

    def _num(x):
        try: return float(x)
        except Exception: return None

    rows = []
    for g in data:
        gid = g.get("id") or g.get("game_id")
        gid = str(gid) if gid is not None else None

        home = g.get("home_team") or g.get("homeTeam")
        away = g.get("away_team") or g.get("awayTeam")
        if not home or not away:
            continue

        lines = g.get("lines") or []
        if not isinstance(lines, list):
            continue
        lines = [ln for ln in lines if isinstance(ln, dict)]
        if not lines:
            continue

        chosen = None
        for prov in provider_priority:
            chosen = next((ln for ln in lines if _provider_name(ln.get("provider")) == prov), None)
            if chosen:
                break
        if chosen is None:
            chosen = lines[0]

        provider = _provider_name(chosen.get("provider")) or "Unknown"

        home_ml = (
            chosen.get("homeMoneyline") or chosen.get("home_moneyline") or
            chosen.get("homeMl") or chosen.get("moneylineHome")
        )
        away_ml = (
            chosen.get("awayMoneyline") or chosen.get("away_moneyline") or
            chosen.get("awayMl") or chosen.get("moneylineAway")
        )
        spread = chosen.get("spread") or chosen.get("formattedSpread") or chosen.get("spreadOpen")
        total  = chosen.get("overUnder") or chosen.get("total") or chosen.get("overUnderOpen")

        rows.append({
            "GameID": str(gid) if gid is not None else f"{season}_{week}_{home}_{away}",
            "Team": home,
            "Opponent": away,
            "Home_Team": home,
            "Away_Team": away,
            "Home_ML": _num(home_ml),
            "Away_ML": _num(away_ml),
            "Spread": _num(spread),
            "Total": _num(total),
            "Provider": provider,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        _log_warn(f"⚠️ No valid odds entries for {season} Week {week}.")
        return df

    for c in ["GameID", "Team", "Opponent", "Home_Team", "Away_Team", "Provider"]:
        df[c] = df[c].astype("string")

    return df

def american_to_prob(odd):
    if odd is None: return None
    try: odd = float(odd)
    except Exception: return None
    if odd == 0: return None
    if odd > 0: return 100.0 / (odd + 100.0)
    return (-odd) / ((-odd) + 100.0)

def expected_value(win_prob, odds):
    """Return EV% of stake given win_prob (0..100) and American odds."""
    if win_prob is None or odds is None: return None
    try:
        p_win = float(win_prob) / 100.0
        o = float(odds)
    except Exception:
        return None
    if o == 0: return None
    payout = (o / 100.0) if o > 0 else (100.0 / abs(o))
    ev = p_win * payout - (1.0 - p_win)
    return round(ev * 100.0, 2)

def evaluate_value_bets(df_model: pd.DataFrame, df_odds: pd.DataFrame) -> pd.DataFrame:
    """
    Merge model predictions with odds and compute EV metrics.
    """
    if df_model is None or df_model.empty or df_odds is None or df_odds.empty:
        _log_warn("⚠️ Missing data for EV evaluation.")
        return pd.DataFrame()

    df = df_model.copy()
    odds = df_odds.copy()

    for k in ["GameID", "Team", "Opponent"]:
        if k in df.columns:
            df[k] = df[k].astype("string")

    if "Win_%" not in df.columns:
        _log_warn("⚠️ df_model missing Win_% column.")
        return pd.DataFrame()
    df["Win_%"] = pd.to_numeric(df["Win_%"], errors="coerce")
    if df["Win_%"].dropna().between(0, 1).mean() > 0.9:
        df["Win_%"] = df["Win_%"] * 100.0

    rename_map = {
        "id": "GameID", "game_id": "GameID",
        "home_team": "Home_Team", "homeTeam": "Home_Team",
        "away_team": "Away_Team", "awayTeam": "Away_Team",
        "home_moneyline": "Home_ML", "homeMoneyline": "Home_ML", "homeMl": "Home_ML", "moneylineHome": "Home_ML",
        "away_moneyline": "Away_ML", "awayMoneyline": "Away_ML", "awayMl": "Away_ML", "moneylineAway": "Away_ML",
        "spread": "Spread", "formattedSpread": "Spread", "spreadOpen": "Spread",
        "overUnder": "Total", "total": "Total", "overUnderOpen": "Total",
        "provider": "Provider",
    }
    odds = odds.rename(columns={k: v for k, v in rename_map.items() if k in odds.columns})
    if "Team" not in odds.columns and "Home_Team" in odds.columns:
        odds["Team"] = odds["Home_Team"]
    if "Opponent" not in odds.columns and "Away_Team" in odds.columns:
        odds["Opponent"] = odds["Away_Team"]

    for k in ["GameID", "Team", "Opponent", "Home_Team", "Away_Team", "Provider"]:
        if k in odds.columns:
            odds[k] = odds[k].astype("string")
    for mlc in ["Home_ML", "Away_ML", "Spread", "Total"]:
        if mlc in odds.columns:
            odds[mlc] = pd.to_numeric(odds[mlc], errors="coerce")
    if "Provider" not in odds.columns:
        odds["Provider"] = "Unknown"

    merged = pd.DataFrame()
    if "GameID" in df.columns and "GameID" in odds.columns:
        m = df.merge(odds, on="GameID", how="left", suffixes=("", "_odds"))
        if not m[["Home_ML", "Away_ML", "Spread", "Total"]].isna().all(axis=None):
            merged = m

    if merged.empty and {"Team", "Opponent"}.issubset(odds.columns):
        home_rows = odds.assign(
            Team=odds["Team"], Opponent=odds["Opponent"],
            Team_ML=odds.get("Home_ML"), Opp_ML=odds.get("Away_ML")
        )
        away_rows = odds.assign(
            Team=odds["Opponent"], Opponent=odds["Team"],
            Team_ML=odds.get("Away_ML"), Opp_ML=odds.get("Home_ML")
        )
        odds_long = pd.concat([home_rows, away_rows], ignore_index=True)
        keep = [c for c in ["GameID","Team","Opponent","Team_ML","Opp_ML","Spread","Total","Provider"] if c in odds_long.columns]
        merged = df.merge(odds_long[keep], on=["Team","Opponent"], how="left")
        if "Home_ML" not in merged.columns and "Team_ML" in merged.columns:
            merged["Home_ML"] = merged["Team_ML"]
        if "Away_ML" not in merged.columns and "Opp_ML" in merged.columns:
            merged["Away_ML"] = merged["Opp_ML"]

    if merged.empty:
        _log_warn("⚠️ Merge produced no rows.")
        return pd.DataFrame()

    if "Home_ML" in merged.columns:
        merged["Home_Prob_Implied"] = merged["Home_ML"].apply(american_to_prob)
    if "Away_ML" in merged.columns:
        merged["Away_Prob_Implied"] = merged["Away_ML"].apply(american_to_prob)

    merged["EV_Home_ML"] = merged.apply(
        lambda x: expected_value(x["Win_%"], x["Home_ML"]) if pd.notna(x.get("Home_ML")) else None, axis=1
    )
    merged["EV_Away_ML"] = merged.apply(
        lambda x: expected_value(100.0 - x["Win_%"], x["Away_ML"]) if pd.notna(x.get("Away_ML")) else None, axis=1
    )

    if "Spread_Pred" in merged.columns and "Spread" in merged.columns:
        merged["Spread_Value"] = (pd.to_numeric(merged["Spread_Pred"], errors="coerce") -
                                  pd.to_numeric(merged["Spread"], errors="coerce")).round(2)
    else:
        merged["Spread_Value"] = np.nan

    if "Total_Pred" in merged.columns and "Total" in merged.columns:
        merged["Total_Value"] = (pd.to_numeric(merged["Total_Pred"], errors="coerce") -
                                 pd.to_numeric(merged["Total"], errors="coerce")).round(2)
    else:
        merged["Total_Value"] = np.nan

    merged["Is_Value_Bet"] = (
        (pd.to_numeric(merged["EV_Home_ML"], errors="coerce").fillna(-1) > 5)
        | (pd.to_numeric(merged["EV_Away_ML"], errors="coerce").fillna(-1) > 5)
        | (merged["Spread_Value"].abs() > 3)
        | (merged["Total_Value"].abs() > 3)
    )

    cols_out = [
        "GameID", "Team", "Opponent", "Win_%", "Spread_Pred", "Total_Pred",
        "Home_ML", "Away_ML", "EV_Home_ML", "EV_Away_ML",
        "Spread_Value", "Total_Value", "Is_Value_Bet", "Provider"
    ]
    for c in cols_out:
        if c not in merged.columns:
            merged[c] = None

    out = merged[cols_out].copy()
    out = out.sort_values(by=["EV_Home_ML"], ascending=False, na_position="last").reset_index(drop=True)
    return out
