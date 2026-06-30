# modules/massey_builder.py
# -*- coding: utf-8 -*-
"""
Build and consume simple Massey-like ratings per season.

Exports:
- build_season(season) -> Path                      # creates data/massey/massey_<season>.csv
- build_if_missing(seasons) -> dict                 # bulk build if files are missing/empty
- ensure_massey_for_seasons(seasons) -> bool        # ensure files exist & loadable
- enable_massey_if_available(current_year, years_back=10) -> bool  # sets CALIB_USE_MASSEY
- enrich_with_massey_diffs(df, season) -> DataFrame # adds Massey_*_Diff columns (Team/Opponent)
"""

from __future__ import annotations

import os
import pathlib
import logging
from typing import Dict, Iterable, Tuple, Optional

import pandas as pd
import requests

# ---------- paths / logging ----------
DATA_DIR = pathlib.Path("data")
MASSEY_DIR = DATA_DIR / "massey"
MASSEY_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------- CFBD auth helpers ----------

def _sanitize_key(raw: str | None) -> str:
    if not raw:
        return ""
    k = raw.strip().strip('"').strip("'")
    if k.lower().startswith("bearer "):
        k = k.split(None, 1)[1]
    return k

CFBD_API_KEY = _sanitize_key(
    os.getenv("CFBD_API_KEY") or os.getenv("CFB_API_KEY") or os.getenv("CFBD_TOKEN")
)
if CFBD_API_KEY:
    os.environ["CFBD_API_KEY"] = CFBD_API_KEY  # expose to other modules

def _auth_headers() -> Dict[str, str]:
    key = os.environ.get("CFBD_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}

# ---------- HTTP / utils ----------

BASE = "https://api.collegefootballdata.com"

def _GET(path: str, params: dict, timeout: int = 45):
    url = f"{BASE}{path}"
    r = requests.get(url, headers=_auth_headers(), params=params, timeout=timeout)
    r.raise_for_status()
    return r.json() or []

def _pick(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None

def _to_num(s):
    return pd.to_numeric(s, errors="coerce")

def _z(series: pd.Series) -> pd.Series:
    s = _to_num(series)
    mu = s.mean()
    sd = s.std(ddof=0)
    if sd == 0 or pd.isna(sd):
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd

# ---------- team canonicalizer (align names across sources) ----------

import re as _re
_PUNCT = _re.compile(r"[^\w\s]")

def _canon_team(name: Optional[str]) -> str:
    if not name:
        return ""
    n = str(name).strip().lower()
    n = n.replace("&", "and")
    n = _PUNCT.sub("", n)
    n = _re.sub(r"\bst\b\.?", "state", n)
    n = _re.sub(r"\buniv\b\.?", "university", n)
    n = _re.sub(r"\buni\b\.?", "university", n)
    n = _re.sub(r"\s+", " ", n).strip()
    return n

# ---------- build CSV from CFBD SP & FPI ----------

def build_season(season: int) -> pathlib.Path:
    """
    Build data/massey/massey_<season>.csv using CFBD SP and FPI totals.
    Columns: Team, Massey_Total, Massey_Off, Massey_Def
    """
    dst = MASSEY_DIR / f"massey_{int(season)}.csv"

    sp = pd.DataFrame(_GET("/ratings/sp", {"year": int(season)}))
    fpi = pd.DataFrame(_GET("/ratings/fpi", {"year": int(season)}))

    sp_team = _pick(sp, ["team", "school"])
    sp_total = _pick(sp, ["rating", "sp", "sp_total"])
    fpi_team = _pick(fpi, ["team", "school"])
    fpi_total = _pick(fpi, ["fpi", "rating", "overall", "fpi_total"])

    if not sp_team or not sp_total:
        log.warning(f"[MasseyBuilder] SP mapping failed for {season}.")
    if not fpi_team or not fpi_total:
        log.warning(f"[MasseyBuilder] FPI mapping failed for {season}.")

    out_sp = pd.DataFrame(columns=["Team", "SP_Total"])
    out_fp = pd.DataFrame(columns=["Team", "FPI_Total"])

    if sp_team and sp_total:
        out_sp = sp[[sp_team, sp_total]].rename(columns={sp_team: "Team", sp_total: "SP_Total"})

        # try to pull offense/defense from SP (flat or nested)
        sp_off = _pick(sp, ["offense", "off", "off_rating"])
        sp_def = _pick(sp, ["defense", "def", "def_rating"])

        def _extract_nested(df, col):
            try:
                if col and col in df.columns and df[col].apply(lambda v: isinstance(v, dict)).any():
                    return df[col].apply(lambda d: d.get("rating") if isinstance(d, dict) else None)
            except Exception:
                pass
            return None

        off_nested = _extract_nested(sp, sp_off)
        def_nested = _extract_nested(sp, sp_def)

        out_sp["SP_Off"] = _to_num(off_nested if off_nested is not None else (sp[sp_off] if sp_off in sp.columns else pd.NA))
        out_sp["SP_Def"] = _to_num(def_nested if def_nested is not None else (sp[sp_def] if sp_def in sp.columns else pd.NA))

    if fpi_team and fpi_total:
        out_fp = fpi[[fpi_team, fpi_total]].rename(columns={fpi_team: "Team", fpi_total: "FPI_Total"})

    merged = out_sp.merge(out_fp, on="Team", how="outer")
    if merged.empty:
        pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"]).to_csv(dst, index=False, encoding="utf-8")
        log.info(f"[MasseyBuilder] Wrote empty file: {dst.as_posix()}")
        return dst

    merged["Massey_Total"] = (_z(merged.get("SP_Total")) + _z(merged.get("FPI_Total"))) / 2.0
    merged["Massey_Off"] = _to_num(merged.get("SP_Off"))
    merged["Massey_Def"] = _to_num(merged.get("SP_Def"))

    out = merged[["Team", "Massey_Total", "Massey_Off", "Massey_Def"]].dropna(subset=["Team"]).drop_duplicates("Team")
    out.to_csv(dst, index=False, encoding="utf-8")
    log.info(f"[MasseyBuilder] Wrote {dst.as_posix()} with {len(out)} rows.")
    return dst

def build_if_missing(seasons: Iterable[int]) -> dict:
    """Build seasons if CSV missing/empty. Returns {'built': [...], 'skipped': [...], 'errors': {...}}."""
    built, skipped, errors = [], [], {}
    for y in seasons:
        try:
            dst = MASSEY_DIR / f"massey_{int(y)}.csv"
            need = True
            if dst.exists():
                try:
                    probe = pd.read_csv(dst, nrows=3, encoding="utf-8")
                    need = probe.empty
                except Exception:
                    need = True
            if need:
                build_season(int(y))
                built.append(int(y))
            else:
                skipped.append(int(y))
        except requests.HTTPError as http_e:
            errors[int(y)] = f"HTTP {getattr(http_e.response, 'status_code', '?')}"
        except Exception as e:
            errors[int(y)] = str(e)
    return {"built": built, "skipped": skipped, "errors": errors}

# ---------- read/merge helpers ----------

def _fetch_massey_csv(season: int) -> pd.DataFrame:
    """
    Load data/massey/massey_<season>.csv and normalize columns:
    returns Team, Massey_Total, Massey_Off, Massey_Def
    """
    path = MASSEY_DIR / f"massey_{int(season)}.csv"
    if not path.exists():
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])
    try:
        raw = pd.read_csv(path, encoding="utf-8")
    except Exception:
        # last-resort attempt without encoding (Windows oddities)
        try:
            raw = pd.read_csv(path)
        except Exception:
            return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    if raw.empty:
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    cols = {c.lower(): c for c in raw.columns}
    team_col = cols.get("team") or cols.get("school") or cols.get("name")
    tot_col  = cols.get("massey_total") or cols.get("massey") or cols.get("rating") or cols.get("overall")
    off_col  = cols.get("massey_off") or cols.get("offense") or cols.get("off")
    def_col  = cols.get("massey_def") or cols.get("defense") or cols.get("def")

    if not team_col or not tot_col:
        return pd.DataFrame(columns=["Team", "Massey_Total", "Massey_Off", "Massey_Def"])

    out = pd.DataFrame()
    out["Team"] = raw[team_col].astype(str)
    out["Massey_Total"] = _to_num(raw[tot_col])
    out["Massey_Off"] = _to_num(raw[off_col]) if off_col in raw.columns else pd.NA
    out["Massey_Def"] = _to_num(raw[def_col]) if def_col in raw.columns else pd.NA

    return out.dropna(subset=["Team"]).drop_duplicates("Team").reset_index(drop=True)

def enrich_with_massey_diffs(df: pd.DataFrame, season: int) -> pd.DataFrame:
    """
    Add Massey_Total_Diff, Massey_Off_Diff, Massey_Def_Diff using Team/Opponent columns.
    Fills zeros if ratings are unavailable.
    """
    out = df.copy()
    m = _fetch_massey_csv(season)
    if m.empty:
        for c in ("Massey_Total_Diff", "Massey_Off_Diff", "Massey_Def_Diff"):
            out[c] = 0.0
        return out

    m = m.copy()
    m["TeamCanon"] = m["Team"].astype(str).map(_canon_team)
    out["TeamCanon"] = out["Team"].astype(str).map(_canon_team)
    out["OpponentCanon"] = out["Opponent"].astype(str).map(_canon_team)

    mt = m.rename(columns={
        "Massey_Total": "Massey_Total_Team",
        "Massey_Off":   "Massey_Off_Team",
        "Massey_Def":   "Massey_Def_Team",
    })
    out = out.merge(
        mt[["TeamCanon","Massey_Total_Team","Massey_Off_Team","Massey_Def_Team"]],
        on="TeamCanon", how="left"
    )

    mo = m.rename(columns={
        "TeamCanon": "OpponentCanon",
        "Massey_Total": "Massey_Total_Opp",
        "Massey_Off":   "Massey_Off_Opp",
        "Massey_Def":   "Massey_Def_Opp",
    })
    out = out.merge(
        mo[["OpponentCanon","Massey_Total_Opp","Massey_Off_Opp","Massey_Def_Opp"]],
        on="OpponentCanon", how="left"
    )

    def _nz(x): return pd.to_numeric(x, errors="coerce")

    out["Massey_Total_Diff"] = (_nz(out.get("Massey_Total_Team")) - _nz(out.get("Massey_Total_Opp"))).fillna(0.0)
    out["Massey_Off_Diff"]   = (_nz(out.get("Massey_Off_Team"))   - _nz(out.get("Massey_Off_Opp"))).fillna(0.0)
    out["Massey_Def_Diff"]   = (_nz(out.get("Massey_Def_Team"))   - _nz(out.get("Massey_Def_Opp"))).fillna(0.0)

    return out

# ---------- ensure/toggle helpers used by main.py ----------

def _csv_exists_and_loads(season: int) -> bool:
    df = _fetch_massey_csv(season)
    return not df.empty

def ensure_massey_for_seasons(seasons: Iterable[int]) -> bool:
    """
    Ensure CSV exists and loads for each season; build when missing.
    Returns True if any season is OK.
    """
    seasons = [int(s) for s in seasons]
    # try building missing
    build_if_missing(seasons)
    # verify
    ok_any = any(_csv_exists_and_loads(s) for s in seasons)
    return ok_any

def enable_massey_if_available(current_year: int, years_back: int = 10) -> bool:
    """
    Ensure [current_year-years_back .. current_year] ratings, then set CALIB_USE_MASSEY accordingly.
    """
    start = int(current_year) - int(years_back)
    end = int(current_year)
    seasons = list(range(start, end + 1))
    log.info(f"[Massey] ensuring seasons: {seasons}")
    ok_any = ensure_massey_for_seasons(seasons)
    if ok_any:
        os.environ["CALIB_USE_MASSEY"] = "1"
        log.info("[Massey] enabled (CALIB_USE_MASSEY=1)")
    else:
        os.environ.pop("CALIB_USE_MASSEY", None)
        log.info("[Massey] disabled (no CSVs found)")
    return ok_any
