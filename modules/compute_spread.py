"""
compute_spread.py
=================================
Predicts spread (point difference) using calibrated linear regression
parameters from data/model_params.json.

Robust features:
- schema-tolerant diffs (SP/FPI) with fallbacks
- optional Massey diffs (Total/Off/Def) if present (coefs default to 0)
- optional weather & site adjustments (if columns exist)
- safer numeric coercion and outlier clipping
"""

from __future__ import annotations

import os
import json
import numpy as np
import pandas as pd
from typing import Any, Dict

PARAM_PATH = os.path.join("data", "model_params.json")


# ------------------------------------------------------------
# 0. Helpers
# ------------------------------------------------------------
def _to_num_series(s: Any, index: pd.Index, default=0.0) -> pd.Series:
    """
    Coerce arbitrary input to a numeric Series aligned to `index`.
    Avoids calling .fillna on scalars by always returning a Series.
    """
    if isinstance(s, pd.Series):
        v = s.reindex(index)
    else:
        # broadcast scalars/arrays to index length
        try:
            arr = np.asarray(s)
        except Exception:
            arr = np.array([s])
        if arr.ndim == 0:
            arr = np.repeat(arr.item(), len(index))
        v = pd.Series(arr, index=index)
    return pd.to_numeric(v, errors="coerce").fillna(default)


def _first_col(df: pd.DataFrame, names: list[str]) -> pd.Series | None:
    """Return first matching column as Series (None if none exist)."""
    for n in names:
        if n in df.columns:
            return df[n]
    return None


def _first_existing(d: Dict[str, Any], keys: list[str], default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def _truthy_series(s: Any, index: pd.Index) -> pd.Series:
    """
    Convert values to boolean via common truthy strings/numbers.
    No .fillna() needed; NaNs map to False.
    """
    if isinstance(s, pd.Series):
        v = s.reindex(index)
    else:
        v = pd.Series(s, index=index)
    return v.map(lambda x: str(x).strip().lower() in ("true", "1", "yes", "y", "t"))


# ------------------------------------------------------------
# 1. Load calibrated spread/total coefficients
# ------------------------------------------------------------
def load_spread_params() -> Dict[str, float]:
    """
    Reads model_params.json and returns spread/total parameters.
    Supported keys (missing -> sensible defaults):
      Spread:
        intercept, coef_SP, coef_FPI, home_edge,
        coef_Massey_Total, coef_Massey_Off, coef_Massey_Def
      Total:
        base_total, k_spread_abs, k_wind, k_temp_cold, k_temp_hot
    """
    if os.path.exists(PARAM_PATH):
        try:
            with open(PARAM_PATH, "r", encoding="utf-8") as f:
                params = json.load(f) or {}
        except Exception:
            params = {}
    else:
        print("⚠️ model_params.json not found — using default spread coefficients.")
        params = {}

    spread = params.get("spread", {}) or {}
    total = params.get("total", {}) or {}

    return {
        # spread model
        "intercept":          float(_first_existing(spread, ["intercept"], 0.0)),
        "coef_SP":            float(_first_existing(spread, ["coef_SP"], 0.5)),
        "coef_FPI":           float(_first_existing(spread, ["coef_FPI"], 0.4)),
        "home_edge":          float(_first_existing(spread, ["home_edge"], 1.0)),
        # optional Massey coefs (default to 0 for full backward-compat)
        "coef_Massey_Total":  float(_first_existing(spread, ["coef_Massey_Total"], 0.0)),
        "coef_Massey_Off":    float(_first_existing(spread, ["coef_Massey_Off"], 0.0)),
        "coef_Massey_Def":    float(_first_existing(spread, ["coef_Massey_Def"], 0.0)),

        # total model (used additively)
        "base_total":   float(_first_existing(total, ["base_total"], 54.5)),
        "k_spread_abs": float(_first_existing(total, ["k_spread_abs"], 0.25)),

        # weather adjustments (optional, applied only if columns exist)
        "k_wind":       float(_first_existing(total, ["k_wind"], -0.06)),
        "k_temp_cold":  float(_first_existing(total, ["k_temp_cold"], -0.05)),
        "k_temp_hot":   float(_first_existing(total, ["k_temp_hot"], -0.02)),
    }


# ------------------------------------------------------------
# 2. Predict spread and total points using calibration
# ------------------------------------------------------------
def compute_spread(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds Spread_Pred and Total_Pred using calibrated parameters.

    Prefers diffs already present:
      - SP_Diff, FPI_Diff
      - optional Massey_Total_Diff, Massey_Off_Diff, Massey_Def_Diff
    If a diff is missing, attempts to construct from side-by-side columns:
      - SP_Total vs Opp_SP_Total (or *_opp / Opp_* variants)
      - FPI_Total vs Opp_FPI_Total (or variants)
      - Massey_* vs Opp_Massey_*
    Final fallback for SP/FPI diffs: TDs_For - TDs_Against (+ scaled proxy for FPI).

    Optional features (used only if columns exist):
      - Wind (mph), Temp (°F), NeutralSite (bool), Is_Home (bool)
    """
    params = load_spread_params()
    intercept = params["intercept"]
    coef_SP = params["coef_SP"]
    coef_FPI = params["coef_FPI"]
    home_edge = params["home_edge"]

    # optional Massey coefficients (0 if not calibrated)
    coef_mtotal = params["coef_Massey_Total"]
    coef_moff   = params["coef_Massey_Off"]
    coef_mdef   = params["coef_Massey_Def"]

    base_total   = params["base_total"]
    k_spread_abs = params["k_spread_abs"]
    k_wind       = params["k_wind"]
    k_temp_cold  = params["k_temp_cold"]
    k_temp_hot   = params["k_temp_hot"]

    out = df.copy()
    idx = out.index

    # ---------- Build/repair SP_Diff ----------
    if "SP_Diff" not in out.columns:
        sp_team = _first_col(out, ["SP_Total", "SP", "SP_overall"])
        sp_opp  = _first_col(out, ["Opp_SP_Total", "SP_Total_Opp", "SP_opp", "SP_opponent"])
        if sp_team is not None and sp_opp is not None:
            out["SP_Diff"] = _to_num_series(sp_team, idx) - _to_num_series(sp_opp, idx)
        elif "TDs_For" in out.columns and "TDs_Against" in out.columns:
            out["SP_Diff"] = _to_num_series(out["TDs_For"], idx) - _to_num_series(out["TDs_Against"], idx)
        else:
            out["SP_Diff"] = _to_num_series(0.0, idx)

    # ---------- Build/repair FPI_Diff ----------
    if "FPI_Diff" not in out.columns:
        fpi_team = _first_col(out, ["FPI_Total", "FPI", "FPI_overall"])
        fpi_opp  = _first_col(out, ["Opp_FPI_Total", "FPI_Total_Opp", "FPI_opp", "FPI_opponent"])
        if fpi_team is not None and fpi_opp is not None:
            out["FPI_Diff"] = _to_num_series(fpi_team, idx) - _to_num_series(fpi_opp, idx)
        else:
            out["FPI_Diff"] = _to_num_series(out["SP_Diff"], idx) * 0.85

    # ---------- Coerce to numeric ----------
    out["SP_Diff"]  = _to_num_series(out["SP_Diff"], idx)
    out["FPI_Diff"] = _to_num_series(out["FPI_Diff"], idx)

    # ---------- Optional Massey diffs ----------
    if "Massey_Total_Diff" not in out.columns:
        mt_team = _first_col(out, ["Massey_Total"])
        mt_opp  = _first_col(out, ["Opp_Massey_Total", "Massey_Total_Opp", "Massey_Total_opp", "Massey_Total_opponent"])
        if mt_team is not None and mt_opp is not None:
            out["Massey_Total_Diff"] = _to_num_series(mt_team, idx) - _to_num_series(mt_opp, idx)
        else:
            out["Massey_Total_Diff"] = _to_num_series(0.0, idx)

    if "Massey_Off_Diff" not in out.columns:
        mo_team = _first_col(out, ["Massey_Off"])
        mo_opp  = _first_col(out, ["Opp_Massey_Off", "Massey_Off_Opp", "Massey_Off_opp", "Massey_Off_opponent"])
        if mo_team is not None and mo_opp is not None:
            out["Massey_Off_Diff"] = _to_num_series(mo_team, idx) - _to_num_series(mo_opp, idx)
        else:
            out["Massey_Off_Diff"] = _to_num_series(0.0, idx)

    if "Massey_Def_Diff" not in out.columns:
        md_team = _first_col(out, ["Massey_Def"])
        md_opp  = _first_col(out, ["Opp_Massey_Def", "Massey_Def_Opp", "Massey_Def_opp", "Massey_Def_opponent"])
        if md_team is not None and md_opp is not None:
            out["Massey_Def_Diff"] = _to_num_series(md_team, idx) - _to_num_series(md_opp, idx)
        else:
            out["Massey_Def_Diff"] = _to_num_series(0.0, idx)

    # ---------- Home / Neutral booleans (no .fillna) ----------
    is_home = _truthy_series(out["Is_Home"], idx) if "Is_Home" in out.columns else pd.Series(False, index=idx)
    neutral = _truthy_series(out["NeutralSite"], idx) if "NeutralSite" in out.columns else pd.Series(False, index=idx)
    home_bonus = np.where(is_home & (~neutral), home_edge, 0.0)

    # ---------- Spread prediction ----------
    out["Spread_Pred"] = (
        intercept
        + (coef_SP  * out["SP_Diff"])
        + (coef_FPI * out["FPI_Diff"])
        + (coef_mtotal * _to_num_series(out["Massey_Total_Diff"], idx))
        + (coef_moff   * _to_num_series(out["Massey_Off_Diff"], idx))
        + (coef_mdef   * _to_num_series(out["Massey_Def_Diff"], idx))
        + home_bonus
    )
    out["Spread_Pred"] = out["Spread_Pred"].clip(lower=-50, upper=50).round(2)

    # ---------- Total prediction ----------
    spread_magnitude = (_to_num_series(out["SP_Diff"], idx).abs() + _to_num_series(out["FPI_Diff"], idx).abs())
    total_pred = base_total + (k_spread_abs * spread_magnitude)

    if "Wind" in out.columns:
        wind = _to_num_series(out["Wind"], idx, default=0.0)
        total_pred = total_pred + (k_wind * wind)

    if "Temp" in out.columns:
        temp = _to_num_series(out["Temp"], idx, default=np.nan)
        cold_excess = (40.0 - temp).clip(lower=0)  # temp < 40°F
        hot_excess  = (temp - 85.0).clip(lower=0)  # temp > 85°F
        total_pred = total_pred + (k_temp_cold * cold_excess) + (k_temp_hot * hot_excess)

    out["Total_Pred"] = total_pred.clip(lower=20, upper=100).round(1)
    return out
