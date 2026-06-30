"""
main.py
=================================
College WinCast - College Football Win Probability & Value Betting App
Streamlit Interactive Dashboard
"""

from __future__ import annotations
import os
import json
import time
import requests
import streamlit as st
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv, find_dotenv
import logging
from typing import Dict, Optional
import os
import pathlib
import logging
from typing import Iterable, Optional, Tuple
import pandas as pd
import requests
from modules.fetch_massey import fetch_massey

# ---------------------------------------------------
# Internal module imports (rollback-safe)
# ---------------------------------------------------
from modules.fetch_cfb import fetch_games_cfbd
from modules.fetch_weather import enrich_with_weather
from modules.fetch_advanced_stats import fetch_advanced_team_metrics
from modules.compute_winprob import add_winprob_column
from modules.compute_spread import compute_spread
from modules.odds_value_analysis import fetch_game_odds, evaluate_value_bets
from modules.export_data import export_to_excel
# Dual-key CFBD client (optional)
try:
    from modules.cfbd_dual import cfbd_get_json
except Exception:
    cfbd_get_json = None  # fall back to single-key requests

# Massey helper module (safe to import; degrades gracefully)
try:
    from modules.massey_builder import (
        ensure_massey_for_seasons,
        enable_massey_if_available,
        enrich_with_massey_diffs,
    )
    _MASSEY_OK = True
except Exception:
    _MASSEY_OK = False

# ======================================
# Logging configuration
# ======================================
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("winCast_debug.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------
# Load environment variables (.env from project root)
# ---------------------------------------------------
ENV_PATH = find_dotenv(usecwd=True)  # finds .env even when launched via `streamlit run`
load_dotenv(dotenv_path=ENV_PATH, override=False)

def _sanitize_cfbd_key(raw: Optional[str]) -> str:
    """
    Normalize CFBD key from environment:
    - strip whitespace and quotes
    - remove leading 'Bearer ' if user pasted it
    """
    if not raw:
        return ""
    key = raw.strip().strip('"').strip("'")
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1]
    return key

# Accept common aliases, sanitize, and re-export so all modules see the clean value.
_raw_key = os.getenv("CFBD_API_KEY") or os.getenv("CFB_API_KEY") or os.getenv("CFBD_TOKEN")
CFBD_API_KEY = _sanitize_cfbd_key(_raw_key or "")
if CFBD_API_KEY:
    os.environ["CFBD_API_KEY"] = CFBD_API_KEY  # make available to imported modules

def _auth_headers() -> Dict[str, str]:
    key = os.environ.get("CFBD_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}

@st.cache_data(show_spinner=False, ttl=900)
def _ping_cfbd(_: str) -> dict:
    """
    Connectivity probe. Uses dual-key client if available; otherwise plain requests.
    """
    # Dual-key path
    if cfbd_get_json is not None:
        try:
            _ = cfbd_get_json("/conferences", timeout=8, retries=1)
            return {"ok": True, "msg": "CFBD connectivity OK (key rotation enabled)."}
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 401:
                return {"ok": False, "msg": "401 Unauthorized for all configured keys. Check .env values."}
            if status == 429:
                return {"ok": False, "msg": "Rate limited by CFBD (429). Ease up request bursts or wait briefly."}
            return {"ok": False, "msg": f"CFBD HTTP error: {status} {e}"}
        except Exception as e:
            return {"ok": False, "msg": f"CFBD connectivity error: {e}"}

    # Single-key fallback
    key = os.environ.get("CFBD_API_KEY", "").strip()
    try:
        r = requests.get("https://api.collegefootballdata.com/conferences",
                         headers={"Authorization": f"Bearer {key}"} if key else {},
                         timeout=12)
        if r.status_code == 401:
            mask = key[:4] + "…" + key[-4:] if len(key) >= 8 else "****"
            return {"ok": False,
                    "msg": f"401 Unauthorized. Using key {mask}. Fix .env: CFBD_API_KEY=<key> (no quotes, no 'Bearer ')."}
        r.raise_for_status()
        return {"ok": True, "msg": "CFBD connectivity OK."}
    except Exception as e:
        return {"ok": False, "msg": f"CFBD connectivity error: {e}"}
       
        
probe = _ping_cfbd(CFBD_API_KEY)
if not probe["ok"]:
    st.error("⚠️ CFBD API key problem.\n\n" + probe["msg"])
    st.stop()

# --- Google OAuth paths (you can change via .env) ---
GOOGLE_OAUTH_CLIENT_SECRETS = os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS", "config/client_secret.json")
GOOGLE_OAUTH_TOKEN = os.getenv("GOOGLE_OAUTH_TOKEN", "config/token.json")

# ---------------------------------------------------
# Optional: Google Sheets Service Account (for export)
# ---------------------------------------------------
GOOGLE_SA_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # path to JSON file
GOOGLE_SA_INFO = os.getenv("GOOGLE_SERVICE_ACCOUNT_INFO")  # raw JSON string
GOOGLE_SA_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")    # base64 JSON (optional)

def _load_gs_service_account():
    """
    Try to build a gspread client using a Service Account (3 ways):
    - GOOGLE_SERVICE_ACCOUNT_JSON (path)
    - GOOGLE_SERVICE_ACCOUNT_INFO (raw JSON)
    - GOOGLE_SERVICE_ACCOUNT_B64 (base64 JSON)
    Returns (client or None, error or None)
    """
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
    except Exception as e:
        return None, f"Missing packages for Google Sheets export: {e}. Run `pip install gspread google-auth google-auth-oauthlib`."

    info_obj: Optional[dict] = None

    # b64 -> dict
    if GOOGLE_SA_B64:
        try:
            import base64, json as _json
            info_obj = _json.loads(base64.b64decode(GOOGLE_SA_B64).decode("utf-8"))
        except Exception as e:
            return None, f"Invalid GOOGLE_SERVICE_ACCOUNT_B64: {e}"

    # raw JSON -> dict
    if (not info_obj) and GOOGLE_SA_INFO:
        try:
            info_obj = json.loads(GOOGLE_SA_INFO)
        except Exception as e:
            return None, f"Invalid GOOGLE_SERVICE_ACCOUNT_INFO JSON: {e}"

    # build creds
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        if info_obj:
            creds = Credentials.from_service_account_info(info_obj, scopes=scopes)
        elif GOOGLE_SA_PATH and os.path.exists(GOOGLE_SA_PATH):
            creds = Credentials.from_service_account_file(GOOGLE_SA_PATH, scopes=scopes)
        else:
            return None, "No Service Account found in env. Use the 🔐 Connect to Google button (OAuth) or set SA env vars."
        gc = gspread.authorize(creds)
        return gc, None
    except Exception as e:
        return None, f"Failed to load Service Account: {e}"

def _load_gs_user_oauth():
    """
    User OAuth: opens browser, asks consent, stores token in GOOGLE_OAUTH_TOKEN.
    Returns (gspread client or None, error or None).
    """
    try:
        import gspread  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
    except Exception as e:
        return None, f"Missing Google OAuth packages: {e}. Run `pip install gspread google-auth google-auth-oauthlib`."

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = None

    # load cached token
    try:
        if os.path.exists(GOOGLE_OAUTH_TOKEN):
            creds = Credentials.from_authorized_user_file(GOOGLE_OAUTH_TOKEN, SCOPES)
    except Exception:
        creds = None

    try:
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(GOOGLE_OAUTH_CLIENT_SECRETS):
                    return None, (
                        f"OAuth client secrets not found at {GOOGLE_OAUTH_CLIENT_SECRETS}. "
                        f"Download your OAuth Desktop credentials JSON and place it there, "
                        f"or set GOOGLE_OAUTH_CLIENT_SECRETS in .env."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_OAUTH_CLIENT_SECRETS, SCOPES)
                # choose any free port; opens browser to consent
                creds = flow.run_local_server(port=0)
            # cache token
            os.makedirs(os.path.dirname(GOOGLE_OAUTH_TOKEN), exist_ok=True)
            with open(GOOGLE_OAUTH_TOKEN, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        gc = gspread.authorize(creds)
        return gc, None
    except Exception as e:
        return None, f"OAuth flow failed: {e}"

def _get_gspread_client():
    """
    Unified getter:
    1) If a Service Account works, use it.
    2) Else, if we've already connected via OAuth this session, reuse it.
    3) Else, if token.json exists, try OAuth client silently.
    """
    # 1) SA
    gc, err = _load_gs_service_account()
    if gc:
        return gc, None

    # 2) session OAuth client
    gc = st.session_state.get("gs_oauth_client")
    if gc:
        return gc, None

    # 3) try token.json silently (no popup)
    try:
        import gspread
        from google.oauth2.credentials import Credentials
        SCOPES = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        if os.path.exists(GOOGLE_OAUTH_TOKEN):
            creds = Credentials.from_authorized_user_file(GOOGLE_OAUTH_TOKEN, SCOPES)
            if creds and creds.valid:
                return gspread.authorize(creds), None
    except Exception:
        pass

    return None, err or "Not connected to Google yet."

def export_to_google_sheets(
    df_tabs: Dict[str, pd.DataFrame],
    spreadsheet_name: str = "College WinCast Snapshot"
) -> str:
    """
    Creates/opens a Google Sheets spreadsheet and writes each DataFrame as a tab.

    Hardens against "empty sheet" by:
      - Converting datetimes to strings
      - Always writing starting at A1
      - Resizing the grid to the exact DataFrame shape (incl. header row)
      - Writing a header row even when the DataFrame has 0 rows
      - Deleting the default "Sheet1" so the file doesn't open blank
    """
    gc, err = _get_gspread_client()
    if err and not gc:
        raise RuntimeError(err)

    # 1) Open or create the spreadsheet
    try:
        sh = gc.open(spreadsheet_name)  # type: ignore
    except Exception:
        sh = gc.create(spreadsheet_name)  # type: ignore

    # 2) Optional sharing (if created by a Service Account and you want it in your Drive)
    share_with = os.getenv("GOOGLE_SHARE_WITH")
    if share_with:
        try:
            sh.share(share_with, perm_type="user", role="writer")  # type: ignore
        except Exception:
            pass

    # 3) Remove the default blank sheet if it's the only one
    try:
        worksheets = sh.worksheets()  # type: ignore
        if len(worksheets) == 1 and worksheets[0].title.lower() in {"sheet1", "sheet 1"}:
            sh.del_worksheet(worksheets[0])  # type: ignore
    except Exception:
        pass

    # 4) Write each tab explicitly at A1
    for tab, raw_df in df_tabs.items():
        df = to_display(raw_df)

        # Build values (always include a header)
        header = df.columns.tolist()
        data_rows = df.fillna("").astype(str).values.tolist()

        if not header:
            # Extremely rare: truly column-less DataFrame
            header = ["Info"]
            data_rows = [["No columns in DataFrame"]]
        elif len(data_rows) == 0:
            # Keep sheet visually non-empty while still reflecting structure
            data_rows = [["—" for _ in header]]

        values = [header] + data_rows
        nrows = max(1, len(values))
        ncols = max(1, len(header))

        title = str(tab)[:100]

        # Recreate the worksheet fresh to avoid stale data
        try:
            ws = sh.worksheet(title)  # type: ignore
            sh.del_worksheet(ws)      # type: ignore
        except Exception:
            pass

        ws = sh.add_worksheet(title=title, rows=nrows, cols=ncols)  # type: ignore

        # Clear (defensive), resize, then write starting at A1
        try:
            ws.clear()  # type: ignore
        except Exception:
            pass

        try:
            ws.resize(rows=nrows, cols=ncols)  # type: ignore
        except Exception:
            # Some backends restrict resize; update will still expand
            pass

        ws.update("A1", values, value_input_option="RAW")  # type: ignore

        # Log what we wrote
        logger.info(f"[Sheets] Wrote tab '{title}' with df shape {df.shape} "
                    f"(rows incl. header: {nrows}, cols: {ncols})")

    return sh.url  # type: ignore

# ---------------------------------------------------
# Internal module imports
# ---------------------------------------------------
from modules.fetch_cfb import fetch_games_cfbd
from modules.fetch_weather import enrich_with_weather
from modules.fetch_advanced_stats import fetch_advanced_team_metrics
from modules.compute_winprob import add_winprob_column
from modules.compute_spread import compute_spread
from modules.odds_value_analysis import fetch_game_odds, evaluate_value_bets
from modules.export_data import export_to_excel
# keep import for type checking; runtime will hot-reload module explicitly
import importlib
import modules.model_calibration as calib_mod

# ---------------------------------------------------
# Helpers: safe date formatting for tables
# ---------------------------------------------------
def safe_strftime(value):
    if value is None or pd.isna(value):
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)
    except Exception:
        return ""

def to_display(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].apply(safe_strftime)
    return out

def to_excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64tz_dtype(out[col].dtype):
            out[col] = out[col].dt.tz_convert("UTC").dt.tz_localize(None)
        elif pd.api.types.is_object_dtype(out[col]):
            def _strip_tz(v):
                if isinstance(v, pd.Timestamp) and getattr(v, "tzinfo", None) is not None:
                    try:
                        return v.tz_convert("UTC").tz_localize(None)
                    except Exception:
                        return v.tz_localize(None)
                return v
            if out[col].apply(lambda v: isinstance(v, pd.Timestamp) and getattr(v, "tzinfo", None) is not None).any():
                out[col] = out[col].apply(_strip_tz)
    return out

# ---------------------------------------------------
# Weather override (heuristic adjustments)
# ---------------------------------------------------
def apply_weather_override_to_totals(df: pd.DataFrame, condition: str, temp_f: float, wind_mph: float) -> pd.DataFrame:
    df = df.copy()
    if "Total_Pred" not in df.columns:
        return df
    delta = 0.0
    cond = (condition or "").lower()
    if "rain" in cond or "snow" in cond or "storm" in cond:
        delta -= 3.0
    elif "cloud" in cond or "overcast" in cond:
        delta -= 0.5
    if wind_mph >= 20:
        delta -= 3.0
    elif wind_mph >= 15:
        delta -= 1.5
    if temp_f < 40:
        delta -= 1.0
    df["Adj_Total_Pred"] = (pd.to_numeric(df["Total_Pred"], errors="coerce") + delta).clip(lower=20, upper=90).round(1)
    return df

# ---------------------------------------------------
# 🔧 Calibration helpers (hot-reload + inline fallback)
# ---------------------------------------------------
import importlib, inspect

def _reload_model_calibration_module():
    """
    Try to import and hot-reload modules.model_calibration.
    Returns (mc_module_or_None, msg_string, mode_string).
    """
    try:
        import modules.model_calibration as mc  # type: ignore
        importlib.reload(mc)
        p = inspect.getfile(mc)
        mtime = time.ctime(os.path.getmtime(p))
        has_teamstats = hasattr(mc, "_finished_fbs_games_via_teamstats")
        mode = "teamstats ✓" if has_teamstats else "legacy (/games) ⚠"
        return mc, f"Using {p} (mtime: {mtime}) — mode: {mode}", mode
    except Exception as e:
        return None, f"Could not import modules.model_calibration: {e}", "unavailable"

def _inline_calibrate_all(season_start: int = 2014, season_end: int = 2024) -> dict:
    """
    Self-contained calibration using /games/teams + ratings, with SP/FPI diffs.
    Writes data/model_params.json on success.
    """
    import numpy as np
    import pandas as pd
    import requests
    from sklearn.linear_model import LogisticRegression, LinearRegression
    from sklearn.metrics import accuracy_score, log_loss, mean_squared_error, r2_score

    BASE = "https://api.collegefootballdata.com"

    def _GET(path: str, params: dict, timeout=60):
        url = f"{BASE}{path}"
        r = requests.get(url, headers=_auth_headers(), params=params, timeout=timeout)
        try:
            r.raise_for_status()
            return r.json() or []
        except requests.HTTPError as e:
            if r.status_code == 401:
                raise RuntimeError("CFBD 401 Unauthorized — check CFBD_API_KEY in your .env") from e
            raise

    def _fbs(year: int) -> set[str]:
        data = _GET("/teams/fbs", {"year": year})
        return {str(x.get("school") or x.get("team")) for x in data if (x.get("school") or x.get("team"))}

    def _finished_fbs_games_via_teamstats(year: int) -> pd.DataFrame:
        teams_rows = _GET("/games/teams", {"year": year, "seasonType": "regular"}, timeout=75)
        if not teams_rows:
            return pd.DataFrame(columns=["Season","GameID","HomeTeam","AwayTeam","HomePts","AwayPts","NeutralSite"])
        df = pd.DataFrame(teams_rows)

        gid = "game_id" if "game_id" in df.columns else ("id" if "id" in df.columns else None)
        team_col = "school" if "school" in df.columns else ("team" if "team" in df.columns else None)
        ha_col = "home_away" if "home_away" in df.columns else ("homeAway" if "homeAway" in df.columns else None)
        pts_col = "points" if "points" in df.columns else None
        if not all([gid, team_col, ha_col, pts_col]):
            return pd.DataFrame(columns=["Season","GameID","HomeTeam","AwayTeam","HomePts","AwayPts","NeutralSite"])

        home = df[df[ha_col].str.lower() == "home"][[gid, team_col, pts_col]].rename(columns={gid:"GameID", team_col:"HomeTeam", pts_col:"HomePts"})
        away = df[df[ha_col].str.lower() == "away"][[gid, team_col, pts_col]].rename(columns={gid:"GameID", team_col:"AwayTeam", pts_col:"AwayPts"})
        games = home.merge(away, on="GameID", how="inner")

        # neutral-site flag from /games
        gmeta = _GET("/games", {"year": year, "seasonType": "regular"})
        neutral = {}
        for x in gmeta:
            gid_x = x.get("id") or x.get("game_id")
            if gid_x is not None:
                neutral[int(gid_x)] = bool(x.get("neutral_site") or x.get("neutralSite") or False)
        games["NeutralSite"] = games["GameID"].astype(int).map(neutral).fillna(False).astype(bool)

        # FBS filter
        fbs_set = _fbs(year)
        games = games[(games["HomeTeam"].isin(fbs_set)) & (games["AwayTeam"].isin(fbs_set))]
        games = games.dropna(subset=["HomePts", "AwayPts"])
        games["Season"] = year
        return games[["Season","GameID","HomeTeam","AwayTeam","HomePts","AwayPts","NeutralSite"]]

    def _ratings(year: int) -> pd.DataFrame:
        sp = pd.DataFrame(_GET("/ratings/sp", {"year": year}))
        fpi = pd.DataFrame(_GET("/ratings/fpi", {"year": year}))
        def _pick(df, cands):
            for c in cands:
                if c in df.columns: return c
            return None
        team_sp = _pick(sp, ["team","school"]); sp_col = _pick(sp, ["rating","sp","sp_total"])
        team_fp = _pick(fpi,["team","school"]); fpi_col= _pick(fpi,["fpi","rating","fpi_total","overall"])
        out_sp = pd.DataFrame(columns=["Team","SP_Total"])
        out_fp = pd.DataFrame(columns=["Team","FPI_Total"])
        if team_sp and sp_col: out_sp = sp[[team_sp,sp_col]].rename(columns={team_sp:"Team", sp_col:"SP_Total"})
        if team_fp and fpi_col: out_fp = fpi[[team_fp,fpi_col]].rename(columns={team_fp:"Team", fpi_col:"FPI_Total"})
        out = out_sp.merge(out_fp, on="Team", how="outer")
        if "Team" in out.columns: out["Team"] = out["Team"].astype("string")
        return out

    frames = []
    for yr in range(season_start, season_end + 1):
        g = _finished_fbs_games_via_teamstats(yr)
        if g.empty:
            continue
        r = _ratings(yr)
        if r.empty:
            continue
        df = g.merge(r.add_suffix("_Home"), left_on="HomeTeam", right_on="Team_Home", how="left")
        df = df.merge(r.add_suffix("_Away"), left_on="AwayTeam", right_on="Team_Away", how="left")
        df["SP_Diff"]   = pd.to_numeric(df["SP_Total_Home"], errors="coerce") - pd.to_numeric(df["SP_Total_Away"], errors="coerce")
        df["FPI_Diff"]  = pd.to_numeric(df["FPI_Total_Home"], errors="coerce") - pd.to_numeric(df["FPI_Total_Away"], errors="coerce")
        df["Score_Diff"] = pd.to_numeric(df["HomePts"], errors="coerce") - pd.to_numeric(df["AwayPts"], errors="coerce")
        df["Win"] = (df["Score_Diff"] > 0).astype(int)
        df["HomeAdv"] = (~df["NeutralSite"]).astype(int)
        frames.append(df)
        time.sleep(0.12)  # be nice to the API

    if not frames:
        raise RuntimeError("Inline fallback: no rows assembled. Check API key/quotas.")

    data = pd.concat(frames, ignore_index=True).dropna(subset=["SP_Diff","FPI_Diff","Score_Diff","Win"])
    if data.empty:
        raise RuntimeError("Inline fallback: dataset empty after NA drop.")

    # Logistic
    X = data[["SP_Diff","FPI_Diff","HomeAdv"]].values
    y = data["Win"].values
    from sklearn.linear_model import LogisticRegression
    logi = LogisticRegression(max_iter=400)
    logi.fit(X, y)
    p = logi.predict_proba(X)[:, 1]
    from sklearn.metrics import accuracy_score, log_loss
    acc = accuracy_score(y, (p >= 0.5).astype(int))
    ll  = log_loss(y, p)

    log_params = {
        "intercept": float(logi.intercept_[0]),
        "coef_SP": float(logi.coef_[0][0]),
        "coef_FPI": float(logi.coef_[0][1]),
        "home_adv": float(logi.coef_[0][2]),
    }

    # Spread (linear)
    from sklearn.linear_model import LinearRegression
    from sklearn.metrics import mean_squared_error, r2_score
    Xs = data[["SP_Diff","FPI_Diff"]].values
    ys = data["Score_Diff"].values
    lin = LinearRegression()
    lin.fit(Xs, ys)
    pred = lin.predict(Xs)
    rmse = mean_squared_error(ys, pred, squared=False)
    r2   = r2_score(ys, pred)
    spr_params = {"intercept": float(lin.intercept_), "coef_SP": float(lin.coef_[0]), "coef_FPI": float(lin.coef_[1])}

    results = {
        "logistic": log_params,
        "spread": spr_params,
        "metrics": {"accuracy": float(acc), "log_loss": float(ll), "rmse": float(rmse), "r2": float(r2)},
        "updated_at": pd.Timestamp.utcnow().isoformat() + "Z",
        "season_window": {"start": int(season_start), "end": int(season_end)},
        "rows_used": int(len(data)),
    }
    os.makedirs("data", exist_ok=True)
    with open(os.path.join("data","model_params.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return results

# ---------------------------------------------------
# Locks and cached wrappers (debounce + speed)
# ---------------------------------------------------
if "run_lock" not in st.session_state:
    st.session_state.run_lock = False
if "calib_lock" not in st.session_state:
    st.session_state.calib_lock = False

# ---------------------------------------------------
# Cached wrappers (rate-limit friendly + fallbacks)
# ---------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

GAMES_TTL   = _env_int("CACHE_TTL_GAMES",   21600)  # 6h
ADV_TTL     = _env_int("CACHE_TTL_ADV",     21600)  # 6h
ODDS_TTL    = _env_int("CACHE_TTL_ODDS",     1800)  # 30m
CALIB_TTL   = _env_int("CACHE_TTL_CALIB",   21600)  # 6h

@st.cache_data(ttl=900)
def fetch_games_cached(season: int, week: int) -> pd.DataFrame:
    # direct call into modules.fetch_cfb (no cfbd_http)
    return fetch_games_cfbd(season=season, week=week, team=None)

@st.cache_data(ttl=900)
def fetch_adv_cached(season: int) -> pd.DataFrame:
    # direct call into modules.fetch_advanced_stats
    return fetch_advanced_team_metrics(season=season)

@st.cache_data(ttl=300)
def fetch_odds_cached(season: int, week: int) -> pd.DataFrame:
    # direct call into modules.odds_value_analysis
    return fetch_game_odds(season=season, week=week)

@st.cache_data(show_spinner=True, ttl=21600)
def run_calibration_cached(season_start: int, season_end: int) -> dict:
    importlib.reload(calib_mod)  # force fresh code
    print(f"[calib] impl -> {getattr(calib_mod, 'CALIBRATION_IMPL', '<unknown>')}")
    return calib_mod.calibrate_all(season_start=season_start, season_end=season_end)

# ---------------------------------------------------
# Sidebar parameters
# ---------------------------------------------------
st.sidebar.header("⚙️ Parameters")

season = st.sidebar.number_input("Season (year)", min_value=2014, max_value=2025, value=2024)
week_start = st.sidebar.number_input("Start week", min_value=1, max_value=20, value=1)
week_end = st.sidebar.number_input("End week", min_value=1, max_value=20, value=3)

# Manual Weather Override
st.sidebar.subheader("🌦️ Manual Weather Override (optional)")
use_override = st.sidebar.checkbox("Enable manual override", value=False)
override_condition = st.sidebar.selectbox("Condition", ["Sunny", "Cloudy", "Rain/Snow"], index=0, disabled=not use_override)
override_temp = st.sidebar.slider("Temperature (°F)", min_value=10, max_value=100, value=65, disabled=not use_override)
override_wind = st.sidebar.slider("Wind (mph)", min_value=0, max_value=40, value=10, disabled=not use_override)

# Google Sheets export controls
st.sidebar.subheader("📤 Google Sheets Export")
enable_gsheets = st.sidebar.checkbox("Enable Google Sheets export", value=False, key="gsheets_enable")
gs_title = st.sidebar.text_input("Spreadsheet name", value="College WinCast Snapshot", disabled=not enable_gsheets, key="gsheets_title")

# 🔐 Connect button (OAuth)
    # Disconnect (revoke + delete token + clear session)
# 🔐 Connect button (OAuth)
if enable_gsheets:
    connected = False
    # show connection status
    _client_check, _err_check = _get_gspread_client()
    if _client_check:
        connected = True
        st.sidebar.success("Connected to Google Sheets ✔")
    else:
        st.sidebar.info("Not connected yet.")

    if st.sidebar.button("🔐 Connect to Google (OAuth)", disabled=connected):
        gc, err = _load_gs_user_oauth()
        if err:
            st.sidebar.error(err)
        else:
            st.session_state["gs_oauth_client"] = gc
            st.sidebar.success("Connected to Google Sheets ✔")
            st.rerun()

    # Disconnect (revoke + delete token + clear session)
    if st.sidebar.button("🔓 Disconnect Google", disabled=not connected and not os.path.exists(GOOGLE_OAUTH_TOKEN)):
        # --- Phase 1: cleanup in a protected block
        try:
            # Try to revoke the current token if present (best-effort)
            try:
                from google.oauth2.credentials import Credentials  # type: ignore
                SCOPES = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                ]
                if os.path.exists(GOOGLE_OAUTH_TOKEN):
                    creds = Credentials.from_authorized_user_file(GOOGLE_OAUTH_TOKEN, SCOPES)
                    if getattr(creds, "token", None):
                        requests.post(
                            "https://oauth2.googleapis.com/revoke",
                            params={"token": creds.token},
                            headers={"content-type": "application/x-www-form-urlencoded"},
                            timeout=10,
                        )
            except Exception as e:
                st.sidebar.info(f"Token revoke attempt skipped: {e}")

            # Delete local cached token
            try:
                if os.path.exists(GOOGLE_OAUTH_TOKEN):
                    os.remove(GOOGLE_OAUTH_TOKEN)
            except Exception as e:
                st.sidebar.error(f"Couldn't delete token file ({GOOGLE_OAUTH_TOKEN}): {e}")

            # Clear in-memory client
            st.session_state.pop("gs_oauth_client", None)

            # Messaging (note: Service Account may still auto-connect)
            is_sa_env = bool(GOOGLE_SA_PATH or GOOGLE_SA_INFO or GOOGLE_SA_B64)
            if is_sa_env:
                st.sidebar.warning(
                    "Disconnected OAuth, but a Service Account is configured via environment. "
                    "Unset GOOGLE_SERVICE_ACCOUNT_* or remove that file to fully disconnect."
                )
            else:
                st.sidebar.success("Disconnected. You'll be asked to sign in again next time you export.")
        except Exception as e:
            st.sidebar.error(f"Disconnect cleanup ran with warnings: {e}")

        # --- Phase 2: force a clean rerun (DO NOT wrap this in try/except)
        st.rerun()

# ---------------------------------------------------
# Conferences / Teams (rate-limit friendly + fallbacks)
# ---------------------------------------------------
try:
    _ = _env_int  # defined earlier in the file
except NameError:
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except Exception:
            return default

CFBD_BASE = "https://api.collegefootballdata.com"
CONFS_TTL = _env_int("CACHE_TTL_CONFS", 21600)  # 6h
TEAMS_TTL = _env_int("CACHE_TTL_TEAMS", 21600)  # 6h

def _rl_get(url: str, params: dict | None = None, timeout: int = 20, retries: int = 3, backoff: float = 0.8):
    """Tiny retry helper for CFBD GETs with exponential backoff."""
    headers = _auth_headers()
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            logger.warning(f"[meta] GET {url} failed after retries: {e}")
            raise

@st.cache_data(ttl=CONFS_TTL, show_spinner=False)
def get_all_conferences() -> list[str]:
    try:
        data = _rl_get(f"{CFBD_BASE}/conferences")
        names = sorted([c["name"] for c in data if isinstance(c, dict) and c.get("name")])
        if names:
            st.session_state["_fallback_confs"] = names
            return names
        raise RuntimeError("empty list")
    except Exception as e:
        fb = st.session_state.get("_fallback_confs")
        if fb:
            logger.warning(f"[meta] Using fallback conferences: {e}")
            return fb
        st.warning(f"⚠️ Unable to load conferences: {e}")
        return []

@st.cache_data(ttl=TEAMS_TTL, show_spinner=False)
def get_all_teams(conference: str | None = None) -> list[str]:
    key = conference or "__ALL__"
    try:
        params = {"conference": conference} if conference else {}
        data = _rl_get(f"{CFBD_BASE}/teams/fbs", params=params)
        teams = sorted([t["school"] for t in data if isinstance(t, dict) and t.get("school")])
        if teams:
            st.session_state.setdefault("_fallback_teams", {})[key] = teams
            return teams
        raise RuntimeError("empty list")
    except Exception as e:
        fb = st.session_state.get("_fallback_teams", {}).get(key)
        if fb:
            logger.warning(f"[meta] Using fallback teams for {conference or 'All'}: {e}")
            return fb
        st.warning(f"⚠️ Unable to load teams: {e}")
        return ["Alabama", "Georgia", "Texas", "Florida", "Ohio State"]

# Initialize session state for conference/team selection
if "selected_conf" not in st.session_state:
    st.session_state.selected_conf = "All Conferences"
if "all_teams" not in st.session_state:
    st.session_state.all_teams = get_all_teams()

st.sidebar.subheader("🏟️ Team Selection")

conferences = get_all_conferences()
new_conf = st.sidebar.selectbox(
    "Filter by Conference (optional)",
    ["All Conferences"] + conferences,
    index=(["All Conferences"] + conferences).index(st.session_state.selected_conf)
)

if new_conf != st.session_state.selected_conf:
    st.session_state.selected_conf = new_conf
    if new_conf == "All Conferences":
        st.session_state.all_teams = get_all_teams()
    else:
        st.session_state.all_teams = get_all_teams(conference=new_conf)

selected_teams = st.sidebar.multiselect(
    "Select Teams",
    options=st.session_state.all_teams,
    default=["Alabama", "Georgia", "Texas"]
)

# ---------------------------------------------------
# Main pipeline (Generate)
# ---------------------------------------------------
if st.sidebar.button("🚀 Generate Predictions"):
    if st.session_state.run_lock:
        st.warning("A run is already in progress. Please wait for it to finish.")
        st.stop()

    st.session_state.run_lock = True
    try:
        st.info("Fetching game data from CollegeFootballData API (cached)…")
        df_all = pd.concat(
            [fetch_games_cached(season=season, week=w) for w in range(week_start, week_end + 1)],
            ignore_index=True,
        )

        if selected_teams:
            df_all = df_all[df_all["Team"].isin(selected_teams)]

        if df_all.empty:
            st.warning("No games found for the given parameters.")
            st.stop()

        st.success(f"{len(df_all)} games retrieved successfully.")

        # Weather
        df_all["Date"] = pd.to_datetime(df_all["Date"], errors="coerce", utc=True)
        skipped_na_dates = int(df_all["Date"].isna().sum())
        if skipped_na_dates > 0:
            st.info(f"Skipping weather for {skipped_na_dates} games without a valid date.")
        df_for_weather = df_all.dropna(subset=["Date"]).copy()

        location_map = {
            "Alabama": "Tuscaloosa, AL",
            "Texas": "Austin, TX",
            "Georgia": "Athens, GA",
            "Florida": "Gainesville, FL",
            "Ohio State": "Columbus, OH",
        }

        for k in ["GameID", "Team", "Is_Home"]:
            if k in df_all.columns:
                df_all[k] = df_all[k].astype("string")
            if k in df_for_weather.columns:
                df_for_weather[k] = df_for_weather[k].astype("string")

        providers_order = ["weatherapi", "open-meteo"] if os.getenv("WEATHERAPI_KEY") else ["open-meteo"]
        st.info(f"Fetching weather data via {', '.join(providers_order)}...")

        if df_for_weather.empty:
            st.warning("No dated games available for weather enrichment; continuing without weather.")
            df_weather = df_all.copy()
        else:
            df_weather_valid = enrich_with_weather(df_for_weather, location_map, providers=providers_order)
            merge_keys = [k for k in ["GameID", "Team", "Is_Home"] if k in df_all.columns and k in df_weather_valid.columns]
            if not merge_keys:
                merge_keys = ["GameID"]
            for k in merge_keys:
                df_weather_valid[k] = df_weather_valid[k].astype("string")

            df_weather = df_all.merge(
                df_weather_valid[merge_keys + ["Condition", "Temp", "Wind", "PrecipProb", "Data_Source", "Last_Updated"]],
                on=merge_keys,
                how="left"
            )

        # === MASSEY: ensure CSVs exist + add per-game diffs (only if module import succeeded) ===
        if _MASSEY_OK:
            _ = enable_massey_if_available(current_year=season, years_back=max(0, season - 2014))
            df_weather = enrich_with_massey_diffs(df_weather, season=season)
            try:
                nz = (df_weather[["Massey_Total_Diff", "Massey_Off_Diff", "Massey_Def_Diff"]].abs().sum(axis=1) > 0).sum()
                logger.info(f"[Massey] nonzero diff rows: {nz}/{len(df_weather)}")
            except Exception:
                pass
        else:
            st.info("Massey module not loaded; proceeding without Massey diffs.")

        # Advanced metrics
        st.info("Fetching advanced team metrics (SP+, FPI, EPA, etc.)...")
        df_adv = fetch_adv_cached(season=season)

        # Merge datasets
        df_merged = df_weather.merge(df_adv, on="Team", how="left")

        # Win prob + spread
        st.info("Computing calibrated Win Probabilities...")
        df_merged = add_winprob_column(df_merged)

        st.info("Computing calibrated Spread & Total predictions...")
        df_merged = compute_spread(df_merged)

        if use_override:
            st.info("Applying manual weather override to totals (heuristic).")
            df_merged = apply_weather_override_to_totals(
                df_merged,
                condition=override_condition,
                temp_f=float(override_temp),
                wind_mph=float(override_wind)
            )

        df_final = df_merged.copy()

        # Value Bets
        st.header("💰 Value Bets (Expected Value Analysis)")
        enable_value_bets = st.sidebar.checkbox("Enable Value Bets Analysis", value=True, key="enable_ev_checkbox")

        df_value = pd.DataFrame()
        if enable_value_bets:
            try:
                st.info("Fetching betting odds from CFBD API (cached)…")
                df_odds = fetch_odds_cached(season=season, week=week_end)

                if df_odds.empty:
                    st.warning("No betting odds available for this week.")
                else:
                    keys = ["GameID", "Team", "Opponent"]
                    for k in [c for c in keys if c in df_final.columns]:
                        df_final[k] = df_final[k].astype("string")
                    for k in [c for c in keys if c in df_odds.columns]:
                        df_odds[k] = df_odds[k].astype("string")

                    df_value = evaluate_value_bets(df_final, df_odds)

                    if "Spread_Value" in df_value.columns:
                        df_value["Spread_Confidence"] = (df_value["Spread_Value"].abs() / 3.0).clip(0, 1).round(2)
                    if "Total_Value" in df_value.columns:
                        df_value["Total_Confidence"] = (df_value["Total_Value"].abs() / 5.0).clip(0, 1).round(2)

                    sort_candidates = [c for c in ["EV_Home_ML", "EV_Team"] if c in df_value.columns]
                    if "Is_Value_Bet" in df_value.columns:
                        df_valuebets = df_value[df_value["Is_Value_Bet"] == True].copy()
                    else:
                        df_valuebets = df_value.copy()
                    if not df_valuebets.empty and sort_candidates:
                        df_valuebets = df_valuebets.sort_values(by=sort_candidates[0], ascending=False)

                    if df_valuebets.empty:
                        st.info("No positive EV bets detected this week.")
                    else:
                        providers = ["All"] + sorted(df_valuebets["Provider"].dropna().unique().tolist())
                        selected_provider = st.selectbox("Select odds provider", providers)
                        if selected_provider != "All":
                            df_valuebets = df_valuebets[df_valuebets["Provider"] == selected_provider]

                        df_valuebets_disp = to_display(df_valuebets)

                        def _color_pos_neg(v):
                            if isinstance(v, (int, float)):
                                if v > 0:
                                    return "color: green; font-weight: bold;"
                                if v < 0:
                                    return "color: red;"
                            return ""

                        display_cols = [
                            "Team", "Opponent", "Win_%", "Spread_Pred", "Total_Pred",
                            "Home_ML", "Away_ML", "EV_Home_ML", "EV_Away_ML",
                            "Spread_Value", "Total_Value", "Spread_Confidence", "Total_Confidence", "Provider"
                        ]
                        display_cols = [c for c in display_cols if c in df_valuebets_disp.columns]
                        subset_cols = [c for c in ["EV_Home_ML", "EV_Away_ML", "Spread_Value", "Total_Value"] if c in df_valuebets_disp.columns]

                        styled = df_valuebets_disp[display_cols].style
                        if subset_cols:
                            styled = styled.map(_color_pos_neg, subset=pd.IndexSlice[:, subset_cols])

                        st.dataframe(styled, use_container_width=True)
            except Exception as e:
                st.error(f"Error fetching or processing odds: {e}")

        # Persist outputs for export on future reruns
        st.session_state["predictions_df"] = df_final.copy()
        st.session_state["value_bets_df"] = df_value.copy() if isinstance(df_value, pd.DataFrame) else pd.DataFrame()
        st.session_state["run_meta"] = {"season": season, "week_start": week_start, "week_end": week_end}

        st.success("✅ Data processed successfully!")

        # Show sample
        st.subheader("📋 Predictions (sample)")
        st.dataframe(to_display(df_final).head(10), use_container_width=True)

        # Chart
        st.subheader("📈 Win Probability vs Temperature")
        if "Temp" in df_final.columns and "Win_%" in df_final.columns:
            df_plot = df_final.dropna(subset=["Temp", "Win_%"])
            if df_plot.empty:
                st.warning("Not enough weather data for visualization.")
            else:
                fig = px.scatter(
                    df_plot, x="Temp", y="Win_%", color="Team", size="Wind",
                    hover_data=["Opponent", "Condition"],
                    title="Win Probability vs Temperature (Marker size = Wind speed)",
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("Not enough weather data for visualization.")

    except Exception as e:
        st.error(f"❌ Error during execution: {e}")
    finally:
        st.session_state.run_lock = False

# =========================================================
# 📤 Export / Snapshot
# =========================================================
st.subheader("📤 Export / Snapshot")

pred_df = st.session_state.get("predictions_df")
value_df = st.session_state.get("value_bets_df", pd.DataFrame())
meta = st.session_state.get("run_meta", {})

if pred_df is None or (isinstance(pred_df, pd.DataFrame) and pred_df.empty):
    st.info("Run predictions first to enable exports.")
else:
    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("💾 Export Predictions to Excel"):
            try:
                path = export_to_excel(to_excel_safe(pred_df))
                st.success(f"Saved: `{path}`")
            except Exception as e:
                st.error(f"Export failed: {e}")

    with c2:
        if st.button("📸 Create Viewer Snapshot (Excel both tabs)"):
            try:
                from modules.export_data import export_to_excel as _export  # ensure latest
                p1 = _export(to_excel_safe(pred_df), filename_prefix="predictions")
                if value_df is not None and not value_df.empty:
                    p2 = _export(to_excel_safe(value_df), filename_prefix="value_bets")
                    st.success(f"Predictions: `{p1}`\nValue bets: `{p2}`")
                else:
                    st.info("Value bets not computed or empty; exported predictions only.")
                    st.success(f"Predictions: `{p1}`")
            except Exception as e:
                st.error(f"Snapshot failed: {e}")

    with c3:
        _client_check, _err_check = _get_gspread_client()
        disabled_export = not st.session_state.get("gsheets_enable", False) or not _client_check
        if st.button("⬆️ Export Snapshot to Google Sheets", disabled=disabled_export):
            try:
                title = st.session_state.get("gsheets_title") or "College WinCast Snapshot"
                tabs = {
                    f"Predictions W{meta.get('week_start','?')}-{meta.get('week_end','?')}": pred_df
                }
                if isinstance(value_df, pd.DataFrame) and not value_df.empty:
                    tabs[f"ValueBets W{meta.get('week_end','?')}"] = value_df
                url = export_to_google_sheets(tabs, spreadsheet_name=title)
                st.success(f"Exported to Google Sheets: {url}")
            except Exception as e:
                st.error(f"Google Sheets export failed: {e}")


# ==== MASSEY BLOCK (paste into mainn.py) ======================================
# Ensures Massey CSVs exist (auto-download if MASSEY_URL_TEMPLATE is set),
# and provides enrich_with_massey_diffs(...) to add Massey_*_Diff columns.



# --- simple canonicalizer (match model_calibration.py behavior) ---
try:
    # If you have the same function in your calibration module, prefer that
    from modules.model_calibration import _canon_team  # type: ignore
except Exception:
    import re
    _PUNCT = re.compile(r"[^\w\s]")
    def _canon_team(name: Optional[str]) -> str:
        if not name: return ""
        n = str(name).strip().lower()
        n = n.replace("&", "and")
        n = _PUNCT.sub("", n)
        n = re.sub(r"\bst\b\.?", "state", n)
        n = re.sub(r"\buniv\b\.?", "university", n)
        n = re.sub(r"\buni\b\.?", "university", n)
        n = re.sub(r"\s+", " ", n).strip()
        return n

# --- paths & logger ---
DATA_DIR = pathlib.Path("data")
MASSEY_DIR = DATA_DIR / "massey"
PARAM_PATH = DATA_DIR / "model_params.json"

log = logging.getLogger("massey")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------- download / presence checks ----------
def _download_massey_csv(season: int, template: str) -> Tuple[bool, pathlib.Path]:
    """
    Download data/massey/massey_<season>.csv using a URL template like:
      MASSEY_URL_TEMPLATE="https://host/path/massey_{season}.csv"
    """
    MASSEY_DIR.mkdir(parents=True, exist_ok=True)
    dst = MASSEY_DIR / f"massey_{season}.csv"
    url = template.format(season=season)
    try:
        log.info(f"[Massey] downloading {season} -> {url}")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        text = r.text
        if "," not in text and "\t" not in text:
            log.warning(f"[Massey] {season} content doesn't look like CSV; skipping save.")
            return False, dst
        dst.write_text(text, encoding="utf-8")
        log.info(f"[Massey] saved {dst.as_posix()}")
        return True, dst
    except Exception as e:
        log.warning(f"[Massey] download failed for {season}: {e}")
        return False, dst

def _ensure_massey_file_for_season(season: int) -> bool:
    """
    Ensure a Massey CSV exists and loads non-empty via fetch_massey(season).
    Will attempt a download if MASSEY_URL_TEMPLATE is set.
    """
    # Already present & loadable?
    df = fetch_massey(season)
    if not df.empty:
        log.info(f"[Massey] {season} OK (rows={len(df)})")
        return True

    template = os.getenv("MASSEY_URL_TEMPLATE", "").strip()
    if template:
        ok, _ = _download_massey_csv(season, template)
        if ok:
            df2 = fetch_massey(season)
            if not df2.empty:
                log.info(f"[Massey] {season} OK after download (rows={len(df2)})")
                return True
            log.warning(f"[Massey] {season} file saved but load returned empty.")
    else:
        log.info("[Massey] MASSEY_URL_TEMPLATE not set; skipping download.")

    log.warning(f"[Massey] {season} NOT available.")
    return False

def ensure_massey_for_seasons(seasons: Iterable[int]) -> bool:
    """
    Ensure CSVs for a list of seasons. Returns True if any season succeeded.
    """
    ok_any = False
    for y in seasons:
        ok_any = _ensure_massey_file_for_season(y) or ok_any
    return ok_any

def enable_massey_if_available(current_year: int, years_back: int = 10) -> bool:
    """
    Convenience: ensure [current_year-years_back .. current_year],
    and set CALIB_USE_MASSEY=1 if at least one season is available.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MASSEY_DIR.mkdir(parents=True, exist_ok=True)

    start = int(os.getenv("CALIB_SEASON_START", current_year - years_back))
    end   = int(os.getenv("CALIB_SEASON_END", current_year))
    seasons = list(range(start, end + 1))
    log.info(f"[Massey] ensuring seasons: {seasons}")

    have_any = ensure_massey_for_seasons(seasons)
    if have_any:
        os.environ["CALIB_USE_MASSEY"] = "1"
        log.info("[Massey] enabled (CALIB_USE_MASSEY=1)")
    else:
        os.environ.pop("CALIB_USE_MASSEY", None)
        log.info("[Massey] disabled (no CSVs found)")
    return have_any

# ---------- attach diffs to a weekly dataframe ----------
def enrich_with_massey_diffs(df: pd.DataFrame, season: int) -> pd.DataFrame:
    """
    Add Massey_Total_Diff, Massey_Off_Diff, Massey_Def_Diff to a DataFrame
    that has columns: Team, Opponent.

    If no CSV for the season (or no matches), fills 0.0 so downstream code
    won't break.
    """
    out = df.copy()

    m = fetch_massey(season)
    if m.empty:
        for c in ("Massey_Total_Diff", "Massey_Off_Diff", "Massey_Def_Diff"):
            out[c] = 0.0
        return out

    m = m.copy()
    m["TeamCanon"] = m["Team"].astype(str).map(_canon_team)

    out["TeamCanon"] = out["Team"].astype(str).map(_canon_team)
    out["OpponentCanon"] = out["Opponent"].astype(str).map(_canon_team)

    # Merge team-side
    mt = m.rename(columns={
        "Massey_Total": "Massey_Total_Team",
        "Massey_Off":   "Massey_Off_Team",
        "Massey_Def":   "Massey_Def_Team",
    })
    out = out.merge(
        mt[["TeamCanon","Massey_Total_Team","Massey_Off_Team","Massey_Def_Team"]],
        on="TeamCanon", how="left"
    )

    # Merge opponent-side
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

    to_num = lambda s: pd.to_numeric(s, errors="coerce")

    out["Massey_Total_Diff"] = (to_num(out.get("Massey_Total_Team")) - to_num(out.get("Massey_Total_Opp"))).fillna(0.0)
    out["Massey_Off_Diff"]   = (to_num(out.get("Massey_Off_Team"))   - to_num(out.get("Massey_Off_Opp"))).fillna(0.0)
    out["Massey_Def_Diff"]   = (to_num(out.get("Massey_Def_Team"))   - to_num(out.get("Massey_Def_Opp"))).fillna(0.0)

    return out
# ==== END MASSEY BLOCK =========================================================

# =========================================================
# 🔧 Model Calibration (Backtesting) — always visible
# =========================================================
st.header("🔧 Model Calibration (Backtesting)")
st.markdown("""
Recalibrate model coefficients using historical data (2014–present).
This adjusts logistic (win probability) and linear (spread) parameters.
""")

colA, colB = st.columns(2)
calib_start = colA.number_input("Calibration start season", min_value=2014, max_value=2025, value=2018, step=1, key="calib_start")
calib_end   = colB.number_input("Calibration end season",   min_value=2014, max_value=2025, value=2024, step=1, key="calib_end")

disabled_calib = st.session_state.calib_lock
if st.button("🧮 Recalibrate Models", disabled=disabled_calib, key="recalibrate_btn"):
    if st.session_state.calib_lock:
        st.warning("Calibration already running.")
    else:
        st.session_state.calib_lock = True
        try:
            # Optionally ensure Massey CSVs exist for selected seasons
            if _MASSEY_OK:
                seasons = list(range(int(calib_start), int(calib_end) + 1))
                st.info(f"Checking Massey ratings for seasons {seasons[0]}–{seasons[-1]}…")
                have_massey = ensure_massey_for_seasons(seasons)
                if have_massey:
                    os.environ["CALIB_USE_MASSEY"] = "1"
                    st.success("Massey ratings found — calibration will include Massey diffs.")
                else:
                    os.environ.pop("CALIB_USE_MASSEY", None)
                    st.warning("No Massey CSVs found for selected seasons; calibration will exclude Massey.")
            else:
                os.environ.pop("CALIB_USE_MASSEY", None)

            # Hot-reload calibration module (will print impl)
            mc, msg, mode = _reload_model_calibration_module()
            st.info(msg)

            # Clear cached calibration so env/feature toggles take effect
            try:
                run_calibration_cached.clear()
            except Exception:
                pass

            with st.spinner("Running calibration…"):
                results = run_calibration_cached(calib_start, calib_end)

            feat = "SP, FPI, HomeAdv" + (", Massey" if os.environ.get("CALIB_USE_MASSEY") == "1" else "")
            st.caption(f"Feature set: {feat}")

            st.success("✅ Calibration completed.")
            st.json(results)
        except Exception as e:
            st.error(f"Calibration failed: {e}")
        finally:
            st.session_state.calib_lock = False
