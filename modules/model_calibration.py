# modules/model_calibration.py
"""
Calibrate model coefficients from CFBD history, with heavy diagnostics.

Fix: handle camelCase 'homePoints'/'awayPoints' (and lineScores) returned by /games.
Also: make RMSE computation backward-compatible with older scikit-learn that
doesn't support mean_squared_error(..., squared=False).

Optional: merge per-season Massey ratings from CSV using fetch_massey.py.
Enable using these features during calibration by setting CALIB_USE_MASSEY=1.
"""

from __future__ import annotations
import os, json, time, re, requests, pandas as pd, numpy as np
from typing import Tuple, Dict, Optional, List
from sklearn.linear_model import LogisticRegression, LinearRegression
from sklearn.metrics import mean_squared_error, r2_score, accuracy_score, log_loss

# Optional import (graceful fallback if helper/module not present)
try:
    from modules.fetch_massey import fetch_massey  # type: ignore
except Exception:
    fetch_massey = None  # type: ignore

BASE_URL = "https://api.collegefootballdata.com"
os.makedirs("data", exist_ok=True)
PARAM_PATH = os.path.join("data", "model_params.json")
CALIBRATION_IMPL = "v2.5-games+ratings+massey-opt+camelcase-fix"

# ---------- env flags ----------
DUMP = os.getenv("CALIB_PROBE_DUMP", "0") == "1"
SAMPLES = max(1, int(os.getenv("CALIB_PROBE_SAMPLES", "3")))
USE_MASSEY = os.getenv("CALIB_USE_MASSEY", "0") == "1"  # off by default for full backward-compat

# ---------- auth / http ----------
def _sanitize_cfbd_key(raw: Optional[str]) -> str:
    if not raw: return ""
    key = raw.strip().strip('"').strip("'")
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1]
    return key

def _auth_headers() -> Dict[str, str]:
    raw = os.getenv("CFBD_API_KEY") or os.getenv("CFB_API_KEY") or os.getenv("CFBD_TOKEN") or ""
    key = _sanitize_cfbd_key(raw)
    return {"Authorization": f"Bearer {key}"} if key else {}

def _get(url: str, params: dict, timeout: int = 30, retries: int = 3, backoff: float = 1.6):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_auth_headers(), params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 401:
                raw = os.getenv("CFBD_API_KEY") or ""
                mask = raw if not raw else (raw[:4] + "…" + raw[-4:])
                print(f"⚠️ 401 Unauthorized. env key='{mask}'. Check CFBD_API_KEY.")
                last_err = e; break
            if status in (429,500,502,503,504) and attempt < retries:
                wait = backoff ** (attempt-1)
                print(f"🌧️ HTTP {status} {url} try {attempt}/{retries}; retry in {wait:.1f}s")
                time.sleep(wait); continue
            text = (e.response.text[:500] if e.response and e.response.text else "")
            print(f"⚠️ CFBD {status} on {url}: {text}")
            last_err = e; break
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff ** (attempt-1)
                print(f"🌧️ Request error {url} try {attempt}/{retries}: {e}; retry in {wait:.1f}s")
                time.sleep(wait); continue
            break
    raise last_err

def _first(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns: return c
    return None

# ---------- canonical names ----------
_PUNCT = re.compile(r"[^\w\s]")
def _canon_team(name: Optional[str]) -> str:
    if not name: return ""
    n = name.strip().lower()
    n = n.replace("&", "and")
    n = _PUNCT.sub("", n)
    n = re.sub(r"\bst\b\.?", "state", n)
    n = re.sub(r"\buniv\b\.?", "university", n)
    n = re.sub(r"\buni\b\.?", "university", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n

# ---------- score helpers ----------
def _sum_lines(x) -> Optional[int]:
    try:
        if isinstance(x, list) and x:
            s = sum(int(pd.to_numeric(v, errors="coerce")) for v in x if v is not None)
            # If all were NaN, s will be 0; detect that:
            if any(v is not None for v in x):
                return int(s)
    except Exception:
        pass
    return None

def _extract_score(g: dict, side: str) -> Optional[int]:
    """
    Extracts points for 'home' or 'away' from many possible shapes:
      snake:  home_points / away_points
      camel:  homePoints  / awayPoints
      score:  homeScore   / awayScore
      nested: home.points|score / away.points|score
      lines:  sum(homeLineScores) / sum(awayLineScores)
    """
    if side not in ("home", "away"):
        return None
    # 1) flat snake/camel/score
    for k in (f"{side}_points", f"{side}Points", f"{side}Score", f"{side}_score"):
        if k in g and g[k] is not None:
            try:
                return int(pd.to_numeric(g[k], errors="coerce"))
            except Exception:
                pass
    # 2) nested dict
    node = g.get(side)
    if isinstance(node, dict):
        for k in ("points", "score"):
            v = node.get(k)
            if v is not None:
                try:
                    return int(pd.to_numeric(v, errors="coerce"))
                except Exception:
                    pass
        # nested line scores fallback
        ls = node.get("lineScores")
        s = _sum_lines(ls)
        if s is not None:
            return s
    # 3) top-level line scores
    for k in (f"{side}LineScores", f"{side}_line_scores"):
        if k in g:
            s = _sum_lines(g.get(k))
            if s is not None:
                return s
    return None

# ---------- probes ----------
def _probe_games_payload(year: int, data: List[dict]) -> None:
    total = len(data)
    def nz(x): return x is not None
    c = {
        "total": total,
        # snake keys
        "has_home_points_key": sum("home_points" in g for g in data),
        "has_away_points_key": sum("away_points" in g for g in data),
        "nonnull_home_points": sum(nz(g.get("home_points")) for g in data),
        "nonnull_away_points": sum(nz(g.get("away_points")) for g in data),
        # camel keys
        "has_homePoints_key": sum("homePoints" in g for g in data),
        "has_awayPoints_key": sum("awayPoints" in g for g in data),
        "nonnull_homePoints": sum(nz(g.get("homePoints")) for g in data),
        "nonnull_awayPoints": sum(nz(g.get("awayPoints")) for g in data),
        # alt names
        "has_homeScore_key": sum("homeScore" in g for g in data),
        "has_awayScore_key": sum("awayScore" in g for g in data),
        "nonnull_homeScore": sum(nz(g.get("homeScore")) for g in data),
        "nonnull_awayScore": sum(nz(g.get("awayScore")) for g in data),
        # nested containers
        "has_nested_home": sum(isinstance(g.get("home"), dict) for g in data),
        "has_nested_away": sum(isinstance(g.get("away"), dict) for g in data),
        "nested_home_points": sum(isinstance(g.get("home"), dict) and nz(g["home"].get("points")) for g in data),
        "nested_away_points": sum(isinstance(g.get("away"), dict) and nz(g["away"].get("points")) for g in data),
        "nested_home_score": sum(isinstance(g.get("home"), dict) and nz(g["home"].get("score")) for g in data),
        "nested_away_score": sum(isinstance(g.get("away"), dict) and nz(g["away"].get("score")) for g in data),
        # line scores
        "has_homeLineScores": sum("homeLineScores" in g for g in data),
        "has_awayLineScores": sum("awayLineScores" in g for g in data),
        # completion status
        "completed_true": sum(bool(g.get("completed")) for g in data),
        "status_final": sum(str(g.get("status","")).lower() == "final" for g in data),
    }
    print(f"[PROBE {year}] counters: {json.dumps(c, ensure_ascii=False)}")

    try:
        keys_union = sorted(set().union(*[set(g.keys()) for g in data[:min(5, total)]]))
    except Exception:
        keys_union = []
    print(f"[PROBE {year}] sample keys (<=5 rows): {keys_union}")

    # examples: completed==True but we STILL can't find any score anywhere
    examples = []
    for g in data:
        completed = bool(g.get("completed"))
        status = str(g.get("status","")).lower()
        hp = _extract_score(g, "home")
        ap = _extract_score(g, "away")
        if (completed or status == "final") and (hp is None or ap is None):
            examples.append({
                "id": g.get("id") or g.get("game_id"),
                "season": g.get("season"), "week": g.get("week"),
                "home_team": g.get("home_team") or g.get("homeTeam") or (g.get("home",{}) or {}).get("team") or (g.get("home",{}) or {}).get("school"),
                "away_team": g.get("away_team") or g.get("awayTeam") or (g.get("away",{}) or {}).get("team") or (g.get("away",{}) or {}).get("school"),
                "completed": completed, "status": g.get("status"),
                "homePoints": g.get("homePoints"), "awayPoints": g.get("awayPoints"),
                "home_points": g.get("home_points"), "away_points": g.get("away_points"),
                "homeScore": g.get("homeScore"), "awayScore": g.get("awayScore"),
                "homeLineScores": g.get("homeLineScores"), "awayLineScores": g.get("awayLineScores"),
                "parsed_home": hp, "parsed_away": ap
            })
            if len(examples) >= SAMPLES:
                break
    if examples:
        print(f"[PROBE {year}] examples (completed but parser missing scores):\n{json.dumps(examples, indent=2)[:1500]}")
    else:
        print(f"[PROBE {year}] parser found scores for completed games or no completed games in sample.")

    if DUMP:
        path = os.path.join("data", f"probe_games_{year}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data[: 5000], f)
            print(f"[PROBE {year}] raw dump -> {path}")
        except Exception as e:
            print(f"[PROBE {year}] dump failed: {e}")

# ---------- parse /games rows ----------
def _parse_game_row(g: dict, season: int) -> Optional[dict]:
    # team names
    h = g.get("home_team") or g.get("homeTeam")
    a = g.get("away_team") or g.get("awayTeam")
    if not h or not a:
        if isinstance(g.get("home"), dict):
            h = h or g["home"].get("team") or g["home"].get("school") or g["home"].get("name")
        if isinstance(g.get("away"), dict):
            a = a or g["away"].get("team") or g["away"].get("school") or g["away"].get("name")

    # points from many shapes (now includes camelCase + lineScores)
    hp = _extract_score(g, "home")
    ap = _extract_score(g, "away")

    if hp is None or ap is None or h is None or a is None:
        return None

    neutral = bool(g.get("neutral_site") or g.get("neutralSite") or g.get("neutral") or False)
    return {
        "Season": season,
        "HomeTeam": str(h), "AwayTeam": str(a),
        "HomePts": int(hp), "AwayPts": int(ap),
        "NeutralSite": neutral,
        "HomeTeamCanon": _canon_team(h),
        "AwayTeamCanon": _canon_team(a),
    }

# ---------- fetch history ----------
def fetch_historical_games(season_start: int = 2014, season_end: int = 2024) -> pd.DataFrame:
    rows: List[dict] = []
    print("🔎 Using endpoints: /games, /ratings/sp, /ratings/fpi")
    for year in range(season_start, season_end + 1):
        print(f"📅 Fetching season {year} games…")
        r = _get(f"{BASE_URL}/games", params={"year": year, "seasonType": "regular"}, timeout=45)
        data = r.json() or []
        _probe_games_payload(year, data)  # diagnostics

        kept = 0
        for g in data:
            row = _parse_game_row(g, season=year)
            if row and row["HomeTeamCanon"] and row["AwayTeamCanon"]:
                rows.append(row); kept += 1
        print(f"   ➜ total={len(data)}, finished kept={kept}")
    df = pd.DataFrame(rows)
    if not df.empty:
        for c in ["HomeTeam","AwayTeam","HomeTeamCanon","AwayTeamCanon"]:
            df[c] = df[c].astype("string")
    return df

# ---------- ratings ----------
def fetch_team_ratings(season: int) -> pd.DataFrame:
    try:
        df_sp  = pd.DataFrame(_get(f"{BASE_URL}/ratings/sp",  params={"year": season}, timeout=25).json() or [])
        df_fpi = pd.DataFrame(_get(f"{BASE_URL}/ratings/fpi", params={"year": season}, timeout=25).json() or [])
    except Exception as e:
        print(f"⚠️ Ratings fetch failed for {season}: {e}")
        return pd.DataFrame(columns=["Team","TeamCanon","SP_Total","FPI_Total"])

    team_sp = _first(df_sp,  ["team","school","Team"]); sp_col  = _first(df_sp,  ["rating","sp","sp_total"])
    team_fp = _first(df_fpi, ["team","school","Team"]); fpi_col = _first(df_fpi, ["fpi","rating","fpi_total","overall"])

    out_sp  = df_sp[[team_sp, sp_col]].rename(columns={team_sp:"Team", sp_col:"SP_Total"}) if (team_sp and sp_col) else pd.DataFrame(columns=["Team","SP_Total"])
    out_fpi = df_fpi[[team_fp, fpi_col]].rename(columns={team_fp:"Team", fpi_col:"FPI_Total"}) if (team_fp and fpi_col) else pd.DataFrame(columns=["Team","FPI_Total"])

    ratings = out_sp.merge(out_fpi, on="Team", how="outer")
    if not ratings.empty:
        ratings["Team"] = ratings["Team"].astype("string")
        ratings["TeamCanon"] = ratings["Team"].apply(_canon_team).astype("string")
        ratings = ratings.drop_duplicates(subset=["TeamCanon"], keep="first")
    print(f"   [ratings] {season}: rows={len(ratings)} (SP={len(out_sp)}, FPI={len(out_fpi)})")
    return ratings

# ---------- MASSEY merge (optional) ----------
def _merge_massey_for_year(df_y: pd.DataFrame, year: int) -> pd.DataFrame:
    """
    Merge Massey ratings for the given season if available. Produces:
      Massey_Total_Diff, Massey_Off_Diff, Massey_Def_Diff
    Missing data defaults to 0.0 so features are neutral when not present.
    """
    # If no helper or file, create neutral columns and return.
    if fetch_massey is None:
        for c in ("Massey_Total_Diff","Massey_Off_Diff","Massey_Def_Diff"):
            if c not in df_y.columns:
                df_y[c] = 0.0
        return df_y

    m = fetch_massey(year)  # may warn and return empty if CSV missing
    if m is None or m.empty:
        for c in ("Massey_Total_Diff","Massey_Off_Diff","Massey_Def_Diff"):
            if c not in df_y.columns:
                df_y[c] = 0.0
        return df_y

    m = m.copy()
    m["TeamCanon"] = m["Team"].astype(str).map(_canon_team)

    mh = m.rename(columns={
        "TeamCanon":"HomeTeamCanon",
        "Massey_Total":"Massey_Total_Home",
        "Massey_Off":"Massey_Off_Home",
        "Massey_Def":"Massey_Def_Home",
    })[["HomeTeamCanon","Massey_Total_Home","Massey_Off_Home","Massey_Def_Home"]]

    ma = m.rename(columns={
        "TeamCanon":"AwayTeamCanon",
        "Massey_Total":"Massey_Total_Away",
        "Massey_Off":"Massey_Off_Away",
        "Massey_Def":"Massey_Def_Away",
    })[["AwayTeamCanon","Massey_Total_Away","Massey_Off_Away","Massey_Def_Away"]]

    df_y = df_y.merge(mh, on="HomeTeamCanon", how="left").merge(ma, on="AwayTeamCanon", how="left")

    # Build diffs (fillna 0 so features are neutral if missing)
    def _diff_pair(h, a):
        return pd.to_numeric(h, errors="coerce").fillna(0.0) - pd.to_numeric(a, errors="coerce").fillna(0.0)

    df_y["Massey_Total_Diff"] = _diff_pair(df_y.get("Massey_Total_Home"), df_y.get("Massey_Total_Away"))
    df_y["Massey_Off_Diff"]   = _diff_pair(df_y.get("Massey_Off_Home"),   df_y.get("Massey_Off_Away"))
    df_y["Massey_Def_Diff"]   = _diff_pair(df_y.get("Massey_Def_Home"),   df_y.get("Massey_Def_Away"))

    return df_y

# ---------- build dataset ----------
def prepare_calibration_dataset(season_start: int = 2018, season_end: int = 2024) -> pd.DataFrame:
    games = fetch_historical_games(season_start, season_end)
    if games.empty:
        print("⚠️ Games dataset empty after fetch."); return games

    frames = []
    for year in range(season_start, season_end + 1):
        ratings = fetch_team_ratings(year)
        if ratings.empty:
            print(f"⚠️ Ratings empty for {year}, skipping."); continue

        df_y = games[games["Season"] == year].copy()
        if df_y.empty:
            print(f"   [{year}] no parsed games, skipping merges.")
            continue

        r_home = ratings.rename(columns={
            "Team":"Team_Home","TeamCanon":"Canon_Home",
            "SP_Total":"SP_Total_Home","FPI_Total":"FPI_Total_Home"
        })[["Canon_Home","Team_Home","SP_Total_Home","FPI_Total_Home"]]

        r_away = ratings.rename(columns={
            "Team":"Team_Away","TeamCanon":"Canon_Away",
            "SP_Total":"SP_Total_Away","FPI_Total":"FPI_Total_Away"
        })[["Canon_Away","Team_Away","SP_Total_Away","FPI_Total_Away"]]

        before = len(df_y)
        df_y = df_y.merge(r_home, left_on="HomeTeamCanon", right_on="Canon_Home", how="inner")
        df_y = df_y.merge(r_away, left_on="AwayTeamCanon", right_on="Canon_Away", how="inner")

        # Core diffs & labels
        df_y["SP_Diff"]    = pd.to_numeric(df_y["SP_Total_Home"], errors="coerce") - pd.to_numeric(df_y["SP_Total_Away"], errors="coerce")
        df_y["FPI_Diff"]   = pd.to_numeric(df_y["FPI_Total_Home"], errors="coerce") - pd.to_numeric(df_y["FPI_Total_Away"], errors="coerce")
        df_y["Score_Diff"] = pd.to_numeric(df_y["HomePts"], errors="coerce") - pd.to_numeric(df_y["AwayPts"], errors="coerce")
        df_y["Win"]        = (df_y["Score_Diff"] > 0).astype(int)
        df_y["HomeAdv"]    = (~df_y["NeutralSite"]).astype(int)

        # OPTIONAL: Massey merge (adds *_Diff columns; neutral 0.0 if missing)
        df_y = _merge_massey_for_year(df_y, year)

        df_y = df_y.dropna(subset=["SP_Diff","FPI_Diff","Score_Diff","Win"])
        after = len(df_y)
        print(f"   [{year}] rows after merges: {before} ➜ kept {after}")

        if not df_y.empty:
            frames.append(df_y)

    if not frames:
        print("⚠️ No season produced valid merged rows."); return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    print(f"📦 Calibration rows assembled: {len(df_all)}")
    return df_all

# ---------- fit models ----------
def _select_features_logistic(df: pd.DataFrame) -> List[str]:
    feats = ["SP_Diff","FPI_Diff","HomeAdv"]
    if USE_MASSEY:
        for c in ["Massey_Total_Diff","Massey_Off_Diff","Massey_Def_Diff"]:
            if c in df.columns:
                feats.append(c)
    return feats

def _select_features_spread(df: pd.DataFrame) -> List[str]:
    feats = ["SP_Diff","FPI_Diff"]
    if USE_MASSEY:
        for c in ["Massey_Total_Diff","Massey_Off_Diff","Massey_Def_Diff"]:
            if c in df.columns:
                feats.append(c)
    return feats

def calibrate_logistic(df: pd.DataFrame) -> Tuple[Dict[str,float], Dict[str,float]]:
    feats = _select_features_logistic(df)
    X = df[feats].values
    y = df["Win"].values
    model = LogisticRegression(max_iter=500).fit(X,y)
    proba = model.predict_proba(X)[:,1]
    acc = accuracy_score(y,(proba>=0.5).astype(int))
    ll  = log_loss(y, proba)
    print(f"🎯 Logistic — Acc {acc:.3f}, LogLoss {ll:.3f}")

    # Map coefs back to stable names (legacy keys preserved; extras are additive)
    coef_map = {"intercept": float(model.intercept_[0])}
    for i, f in enumerate(feats):
        if f == "SP_Diff":        coef_map["coef_SP"] = float(model.coef_[0][i])
        elif f == "FPI_Diff":     coef_map["coef_FPI"] = float(model.coef_[0][i])
        elif f == "HomeAdv":      coef_map["home_adv"] = float(model.coef_[0][i])
        elif f == "Massey_Total_Diff": coef_map["coef_Massey_Total"] = float(model.coef_[0][i])
        elif f == "Massey_Off_Diff":   coef_map["coef_Massey_Off"] = float(model.coef_[0][i])
        elif f == "Massey_Def_Diff":   coef_map["coef_Massey_Def"] = float(model.coef_[0][i])

    return coef_map, {"accuracy":float(acc), "log_loss":float(ll)}

def calibrate_spread(df: pd.DataFrame) -> Tuple[Dict[str,float], Dict[str,float]]:
    feats = _select_features_spread(df)
    X = df[feats].values
    y = df["Score_Diff"].values
    lin = LinearRegression().fit(X,y)
    pred = lin.predict(X)
    # Backward-compatible RMSE (older sklearn lacks 'squared' kwarg)
    try:
        rmse = mean_squared_error(y, pred, squared=False)
    except TypeError:
        rmse = float(np.sqrt(mean_squared_error(y, pred)))
    r2   = r2_score(y, pred)
    print(f"📈 Spread — RMSE {rmse:.2f}, R² {r2:.3f}")

    coef_map = {"intercept": float(lin.intercept_)}
    for i, f in enumerate(feats):
        if f == "SP_Diff":        coef_map["coef_SP"] = float(lin.coef_[i])
        elif f == "FPI_Diff":     coef_map["coef_FPI"] = float(lin.coef_[i])
        elif f == "Massey_Total_Diff": coef_map["coef_Massey_Total"] = float(lin.coef_[i])
        elif f == "Massey_Off_Diff":   coef_map["coef_Massey_Off"] = float(lin.coef_[i])
        elif f == "Massey_Def_Diff":   coef_map["coef_Massey_Def"] = float(lin.coef_[i])

    return coef_map, {"rmse":float(rmse), "r2":float(r2)}

# ---------- orchestrator ----------
def calibrate_all(season_start: int = 2018, season_end: int = 2024) -> dict:
    print(f"🔧 Starting model calibration… impl={CALIBRATION_IMPL}")
    if USE_MASSEY:
        print("➕ Feature set: SP, FPI, HomeAdv, +Massey diffs (Total/Off/Def where available)")
    else:
        print("➕ Feature set: SP, FPI, HomeAdv (Massey disabled; set CALIB_USE_MASSEY=1 to enable)")

    df = prepare_calibration_dataset(season_start, season_end)
    if df.empty:
        raise RuntimeError("No calibration data assembled. After merging SP/FPI, dataset is empty.")

    log_params, log_metrics = calibrate_logistic(df)
    spr_params, spr_metrics = calibrate_spread(df)

    results = {
        "logistic": log_params,
        "spread": spr_params,
        "metrics": {**log_metrics, **spr_metrics},
        "updated_at": pd.Timestamp.utcnow().isoformat()+"Z",
        "season_window": {"start": int(season_start), "end": int(season_end)},
        "rows_used": int(len(df)),
    }
    with open(PARAM_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"✅ Calibration complete. Saved to {PARAM_PATH}")
    return results

if __name__ == "__main__":
    print(json.dumps(calibrate_all(2018, 2024), indent=2)[:900], "...\n")
