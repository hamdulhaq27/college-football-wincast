"""
compute_winprob.py
=================================
Computes Win Probability (0–100%) using logistic regression,
calibrated with parameters from data/model_params.json.

Backward-compatible:
- Works with only SP/FPI diffs.
- Optionally uses HomeAdv and Massey diffs (if present in params & data).
"""

from __future__ import annotations
import os
import json
import numpy as np
import pandas as pd
from typing import Any, Dict

# Path to calibrated model parameters
PARAM_PATH = os.path.join("data", "model_params.json")

# ------------------------------------------------------------
# 0) Helpers to avoid 'float has no attribute fillna' issues
# ------------------------------------------------------------
def _as_num_series(val: Any, index: pd.Index, default: float = 0.0) -> pd.Series:
    """
    Always return a numeric Series aligned to `index` (never a scalar).
    """
    if isinstance(val, pd.Series):
        s = val.reindex(index)
    else:
        arr = np.asarray(val)
        if arr.ndim == 0:  # scalar -> broadcast
            arr = np.repeat(arr.item() if arr.size else default, len(index))
        s = pd.Series(arr, index=index)
    return pd.to_numeric(s, errors="coerce").fillna(default)

def _truthy_series(val: Any, index: pd.Index) -> pd.Series:
    """
    Convert values to boolean Series using common truthy strings/numbers.
    """
    if isinstance(val, pd.Series):
        s = val.reindex(index)
    else:
        s = pd.Series(val, index=index)
    def _t(x):
        x = str(x).strip().lower()
        return x in ("true","1","yes","y","t")
    return s.map(_t)

# ------------------------------------------------------------
# 1) Load calibrated parameters or fallback to defaults
# ------------------------------------------------------------
def load_calibrated_params() -> Dict[str, float]:
    """
    Returns a dict with logistic coefficients. Supports optional keys:
      - home_adv, coef_Massey_Total, coef_Massey_Off, coef_Massey_Def
    Missing keys default to 0.0 for full backward-compatibility.
    """
    defaults = {
        "a": -0.25,         # intercept
        "b_sp": 0.08,       # coef_SP
        "b_fpi": 0.07,      # coef_FPI
        "b_home": 0.0,      # home_adv
        "b_mtotal": 0.0,    # coef_Massey_Total
        "b_moff": 0.0,      # coef_Massey_Off
        "b_mdef": 0.0,      # coef_Massey_Def
    }

    if not os.path.exists(PARAM_PATH):
        print("⚠️ model_params.json not found — using default logistic coefficients.")
        return defaults

    try:
        with open(PARAM_PATH, "r", encoding="utf-8") as f:
            params = json.load(f) or {}
        logistic = params.get("logistic", {}) or {}
        return {
            "a":        float(logistic.get("intercept", defaults["a"])),
            "b_sp":     float(logistic.get("coef_SP", defaults["b_sp"])),
            "b_fpi":    float(logistic.get("coef_FPI", defaults["b_fpi"])),
            "b_home":   float(logistic.get("home_adv", defaults["b_home"])),
            "b_mtotal": float(logistic.get("coef_Massey_Total", defaults["b_mtotal"])),
            "b_moff":   float(logistic.get("coef_Massey_Off", defaults["b_moff"])),
            "b_mdef":   float(logistic.get("coef_Massey_Def", defaults["b_mdef"])),
        }
    except Exception as e:
        print(f"⚠️ Could not read model_params.json ({e}) — using defaults.")
        return defaults

# ------------------------------------------------------------
# 2) Logistic Win Probability (vectorized)
# ------------------------------------------------------------
def _logistic_pct(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -50, 50)  # numerical stability
    return 100.0 / (1.0 + np.exp(-z))

# ------------------------------------------------------------
# 3) Apply Win Probability to dataset
# ------------------------------------------------------------
def add_winprob_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds Win_% column to a DataFrame using calibrated logistic parameters.

    Expected columns (any subset is okay; we fall back gracefully):
      - SP_Diff, FPI_Diff
      - optional: HomeAdv (0/1), NeutralSite (bool), Is_Home (bool)
      - optional: Massey_Total_Diff, Massey_Off_Diff, Massey_Def_Diff

    If SP/FPI diffs are missing, we proxy SP_Diff from (TDs_For - TDs_Against)
    and FPI_Diff = SP_Diff * 0.85.
    """
    out = df.copy()
    idx = out.index
    p = load_calibrated_params()

    # --- Build SP/FPI diffs (with legacy fallbacks) ---
    if "SP_Diff" in out.columns:
        sp = _as_num_series(out["SP_Diff"], idx, 0.0)
    else:
        sp = _as_num_series(out.get("TDs_For", 0.0), idx, 0.0) - _as_num_series(out.get("TDs_Against", 0.0), idx, 0.0)

    if "FPI_Diff" in out.columns:
        fp = _as_num_series(out["FPI_Diff"], idx, 0.0)
    else:
        fp = _as_num_series(sp * 0.85, idx, 0.0)

    # --- HomeAdv feature (optional; defaults to 0) ---
    if "HomeAdv" in out.columns:
        home_adv = _as_num_series(out["HomeAdv"], idx, 0.0)
    else:
        if "Is_Home" in out.columns:
            is_home = _truthy_series(out["Is_Home"], idx)
        else:
            is_home = pd.Series(False, index=idx)
        if "NeutralSite" in out.columns:
            neutral = _truthy_series(out["NeutralSite"], idx)
        else:
            neutral = pd.Series(False, index=idx)
        home_adv = (is_home & (~neutral)).astype(float)

    # --- Massey diffs (optional; safe defaults 0) ---
    m_total = _as_num_series(out.get("Massey_Total_Diff", 0.0), idx, 0.0)
    m_off   = _as_num_series(out.get("Massey_Off_Diff",   0.0), idx, 0.0)
    m_def   = _as_num_series(out.get("Massey_Def_Diff",   0.0), idx, 0.0)

    # --- Linear combination -> probability ---
    z = (
        p["a"]
        + p["b_sp"]     * sp.values
        + p["b_fpi"]    * fp.values
        + p["b_home"]   * home_adv.values
        + p["b_mtotal"] * m_total.values
        + p["b_moff"]   * m_off.values
        + p["b_mdef"]   * m_def.values
    )
    out["Win_%"] = _logistic_pct(z).clip(0, 100).round(2)

    return out
