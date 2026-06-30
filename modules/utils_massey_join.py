# utils_massey_join.py (or inline in your pipeline)
from fetch_massey import fetch_massey
from modules.model_calibration import _canon_team  # same canonicalizer used elsewhere
import pandas as pd

def attach_massey_diffs(df: pd.DataFrame, season: int) -> pd.DataFrame:
    """
    Expects df with columns: Team, Opponent (strings). Creates:
    Massey_Total_Team, Massey_Total_Opp, Massey_Total_Diff
    and, if available, *_Off_* and *_Def_* analogs.
    """
    m = fetch_massey(season)
    if m.empty:
        # ensure columns exist (zeros) so downstream code doesn't break
        out = df.copy()
        out["Massey_Total_Diff"] = 0.0
        out["Massey_Off_Diff"]   = 0.0
        out["Massey_Def_Diff"]   = 0.0
        return out

    m = m.copy()
    m["TeamCanon"] = m["Team"].astype(str).map(_canon_team)

    out = df.copy()
    out["TeamCanon"]     = out["Team"].astype(str).map(_canon_team)
    out["OpponentCanon"] = out["Opponent"].astype(str).map(_canon_team)

    # merge for the "team" side
    m_team = m.rename(columns={
        "TeamCanon": "TeamCanon",
        "Massey_Total": "Massey_Total_Team",
        "Massey_Off":   "Massey_Off_Team",
        "Massey_Def":   "Massey_Def_Team",
    })
    out = out.merge(
        m_team[["TeamCanon","Massey_Total_Team","Massey_Off_Team","Massey_Def_Team"]],
        on="TeamCanon", how="left"
    )

    # merge for the "opponent" side
    m_opp = m.rename(columns={
        "TeamCanon": "OpponentCanon",
        "Massey_Total": "Massey_Total_Opp",
        "Massey_Off":   "Massey_Off_Opp",
        "Massey_Def":   "Massey_Def_Opp",
    })
    out = out.merge(
        m_opp[["OpponentCanon","Massey_Total_Opp","Massey_Off_Opp","Massey_Def_Opp"]],
        on="OpponentCanon", how="left"
    )

    # compute diffs (team - opponent)
    for base in ["Total", "Off", "Def"]:
        team_col = f"Massey_{base}_Team"
        opp_col  = f"Massey_{base}_Opp"
        diff_col = f"Massey_{base}_Diff"
        if team_col in out.columns and opp_col in out.columns:
            out[diff_col] = pd.to_numeric(out[team_col], errors="coerce") - pd.to_numeric(out[opp_col], errors="coerce")

    # fill NaNs to 0 to keep downstream math simple
    for c in ["Massey_Total_Diff", "Massey_Off_Diff", "Massey_Def_Diff"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    return out
