"""
fetch_massey.py
=================================
Lightweight loader for Massey ratings (by season).

Expected CSV location (default):
  data/massey/massey_<season>.csv

Flexible columns supported (we’ll try to auto-map):
  - "team" or "Team"
  - Overall:  "massey", "rating", "overall", "Massey_Total"
  - Offense:  "off", "offense", "Massey_Off"
  - Defense:  "def", "defense", "Massey_Def"

Output columns:
  Team, Massey_Total, Massey_Off, Massey_Def

New (optional):
- Set MASSEY_URL_TEMPLATE="https://host/path/massey_{season}.csv"
  to auto-download into data/massey/ when a season file is missing.
"""

from __future__ import annotations
import os
import logging
import pandas as pd

# Optional: only used if MASSEY_URL_TEMPLATE is set
try:
    import requests  # noqa: F401
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False

logger = logging.getLogger(__name__)

DEFAULT_DIR = os.path.join("data", "massey")
MASSEY_URL_TEMPLATE = os.getenv("MASSEY_URL_TEMPLATE", "").strip()

# Simple team-name tweaks if needed (extend as you encounter mismatches)
NAME_FIXES = {
    "Miami FL": "Miami (FL)",
    "Miami (Florida)": "Miami (FL)",
    "Miami OH": "Miami (OH)",
    "UT-Chattanooga": "Chattanooga",
    "Central Florida": "UCF",
    "Texas-San Antonio": "UTSA",
    # add more remaps as needed
}

def _normalize_team(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return name
    name = name.strip()
    return NAME_FIXES.get(name, name)

def _download_massey_csv(season: int, dest_path: str) -> bool:
    """
    If MASSEY_URL_TEMPLATE is set, try to download the season CSV to dest_path.
    Returns True on success, False otherwise. Keeps behavior no-op if template is unset.
    """
    if not MASSEY_URL_TEMPLATE:
        return False
    if not _HAS_REQUESTS:
        logger.warning("[Massey] requests not available; cannot auto-download.")
        return False

    try:
        url = MASSEY_URL_TEMPLATE.format(season=season)
    except Exception:
        logger.warning("[Massey] MASSEY_URL_TEMPLATE missing '{season}' placeholder.")
        return False

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
        logger.info(f"[Massey] Downloaded {url} -> {dest_path}")
        return True
    except Exception as e:
        logger.warning(f"[Massey] Download failed from {url}: {e}")
        return False

def fetch_massey(season: int, path: str | None = None) -> pd.DataFrame:
    """
    Load a season’s Massey ratings from CSV and normalize columns.

    Parameters
    ----------
    season : int
    path   : Optional explicit CSV file path. If None, uses data/massey/massey_<season>.csv

    Returns
    -------
    DataFrame with columns: Team, Massey_Total, Massey_Off, Massey_Def
    """
    if path is None:
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        path = os.path.join(DEFAULT_DIR, f"massey_{season}.csv")

    # If file is missing, optionally try to download it
    if not os.path.exists(path):
        if not _download_massey_csv(season, path):
            logger.warning(f"[Massey] CSV not found at {path}. Skipping.")
            return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    try:
        # tolerate BOM & common encodings
        raw = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        logger.warning(f"[Massey] Could not read {path}: {e}")
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    if raw.empty:
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    # Column auto-map (case-insensitive)
    cols = {c.lower(): c for c in raw.columns}
    team_col = cols.get("team", cols.get("school", cols.get("name")))
    overall_col = cols.get("massey", cols.get("rating", cols.get("overall", cols.get("massey_total"))))
    off_col = cols.get("off", cols.get("offense", cols.get("massey_off")))
    def_col = cols.get("def", cols.get("defense", cols.get("massey_def")))

    if team_col is None or overall_col is None:
        logger.warning(f"[Massey] Missing required columns (team/overall) in {path}.")
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    out = pd.DataFrame()
    out["Team"] = raw[team_col].map(_normalize_team)
    out["Massey_Total"] = pd.to_numeric(raw[overall_col], errors="coerce")

    # Fill optional columns with NaN (keeps dtype numeric where present)
    if off_col in raw.columns:
        out["Massey_Off"] = pd.to_numeric(raw[off_col], errors="coerce")
    else:
        out["Massey_Off"] = pd.Series(pd.NA, index=out.index, dtype="Float64")

    if def_col in raw.columns:
        out["Massey_Def"] = pd.to_numeric(raw[def_col], errors="coerce")
    else:
        out["Massey_Def"] = pd.Series(pd.NA, index=out.index, dtype="Float64")

    out = out.dropna(subset=["Team"]).drop_duplicates("Team").reset_index(drop=True)
    return out
