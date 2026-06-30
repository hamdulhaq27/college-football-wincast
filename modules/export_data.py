# export_data.py
# =================================
# Módulo para exportar resultados para Excel (.xlsx)
# ou atualizar uma planilha Google Sheets via API.
#
# - Corrige erro de timezone no Excel (converte tz-aware -> naive/UTC)
# - Suporta filename_prefix para nomes de arquivos
# - Suporta exportação multi-aba (um workbook com várias planilhas)
# - Exportação Google Sheets robusta (credenciais por path ou JSON em env)
# =================================

from __future__ import annotations

import os
import time
from typing import Dict, Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _to_excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Deixa o DataFrame seguro para Excel:
    - Converte datetimes com timezone (tz-aware) para naive em UTC.
    - Normaliza colunas datetime/object que possam conter Timestamps com tz.
    - Converte 'category' em string.
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    for col in out.columns:
        s = out[col]

        # datetime64 tz-aware -> UTC -> naive
        if str(s.dtype).startswith("datetime64[ns,"):
            out[col] = pd.to_datetime(s, errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)

        # datetime64 (naive) -> apenas garante dtype coerente
        elif pd.api.types.is_datetime64_any_dtype(s):
            out[col] = pd.to_datetime(s, errors="coerce")

        # object que pode conter Timestamp com tz -> trata item a item
        elif s.dtype == "object":
            # faz uma checagem leve para evitar apply desnecessário
            needs_apply = False
            for v in s.values:
                t = type(v).__name__
                if ("Timestamp" in t or "datetime" in t) and hasattr(v, "tz"):
                    needs_apply = True
                    break
            if needs_apply:
                def _coerce_obj(v):
                    try:
                        if hasattr(v, "tz") and v.tz is not None:
                            # para UTC e remove tz
                            return v.tz_convert("UTC").tz_localize(None)
                        return v
                    except Exception:
                        return v
                out[col] = out[col].apply(_coerce_obj)

    # categorias -> string
    for c in out.select_dtypes(include=["category"]).columns:
        out[c] = out[c].astype(str)

    return out


def _to_sheets_safe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converte datetimes para strings amigáveis ao Google Sheets.
    Mantém resto como string quando necessário.
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    def _fmt_dt_series(s: pd.Series) -> pd.Series:
        # Se tz-aware
        if str(s.dtype).startswith("datetime64[ns,"):
            s2 = pd.to_datetime(s, errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)
            return s2.dt.strftime("%Y-%m-%d %H:%M:%S")
        # Se datetime naive
        if pd.api.types.is_datetime64_any_dtype(s):
            s2 = pd.to_datetime(s, errors="coerce")
            return s2.dt.strftime("%Y-%m-%d %H:%M:%S")
        # Se object possivelmente com timestamps
        if s.dtype == "object":
            def _coerce(v):
                try:
                    if hasattr(v, "tz") and v.tz is not None:
                        v = v.tz_convert("UTC").tz_localize(None)
                    # tenta formatar como datetime
                    return pd.to_datetime(v).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return v
            return s.apply(_coerce)
        return s

    for col in out.columns:
        out[col] = _fmt_dt_series(out[col])

    # categorias -> string
    for c in out.select_dtypes(include=["category"]).columns:
        out[c] = out[c].astype(str)

    return out


# -------------------------------------------------------------------
# 1) Exportação local (Excel)
# -------------------------------------------------------------------
def export_to_excel(
    df: pd.DataFrame,
    filename_prefix: Optional[str] = None,
    output_dir: str = "data",
    sheet_name: str = "Results",
    index: bool = False,
) -> str:
    """
    Exporta um único DataFrame para arquivo .xlsx (safe para Excel).
    - filename_prefix: prefixo do arquivo, ex: "predictions" -> predictions_YYYYmmdd_HHMMSS.xlsx
    - output_dir: diretório destino
    - sheet_name: nome da aba
    """
    if df is None or df.empty:
        raise ValueError("DataFrame está vazio. Nada a exportar.")

    _ensure_dir(output_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = (filename_prefix or "college_results").strip().replace(" ", "_")
    path = os.path.join(output_dir, f"{base}_{ts}.xlsx")

    safe_df = _to_excel_safe(df)
    with pd.ExcelWriter(path) as writer:
        safe_df.to_excel(writer, index=index, sheet_name=(sheet_name[:31] or "Results"))

    print(f"✅ Dados exportados para Excel: {path}")
    return path


def export_workbook(
    tabs: Dict[str, pd.DataFrame],
    filename_prefix: Optional[str] = None,
    output_dir: str = "data",
    index: bool = False,
) -> str:
    """
    Exporta várias abas (Dict nome_da_aba -> DataFrame) em um único .xlsx.
    """
    if not isinstance(tabs, dict) or not tabs:
        raise ValueError("Parâmetro 'tabs' deve ser um dict não vazio: {'Sheet': df, ...}")

    _ensure_dir(output_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = (filename_prefix or "snapshot").strip().replace(" ", "_")
    path = os.path.join(output_dir, f"{base}_{ts}.xlsx")

    with pd.ExcelWriter(path) as writer:
        for name, df in tabs.items():
            safe_df = _to_excel_safe(df if isinstance(df, pd.DataFrame) else pd.DataFrame())
            sheet = (name or "Sheet").strip()[:31] or "Sheet"
            safe_df.to_excel(writer, index=index, sheet_name=sheet)

    print(f"✅ Snapshot Excel criado: {path}")
    return path


# -------------------------------------------------------------------
# 2) Exportação Google Sheets
# -------------------------------------------------------------------
def _get_gs_client():
    """
    Retorna um cliente gspread autenticado.
    Suporta:
      - GOOGLE_SERVICE_ACCOUNT_INFO (JSON bruto em env)
      - GOOGLE_SERVICE_ACCOUNT_JSON (caminho do arquivo)
      - GSPREAD_CREDENTIALS_PATH (legado)
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        raise RuntimeError(
            f"Dependências do Google Sheets ausentes: {e}. "
            "Instale com: pip install gspread google-auth"
        )

    # 1) JSON bruto em env
    info_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_INFO")
    if info_json:
        import json
        try:
            info = json.loads(info_json)
            creds = Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"]
            )
            return gspread.authorize(creds)
        except Exception as e:
            raise RuntimeError(f"Falha lendo GOOGLE_SERVICE_ACCOUNT_INFO: {e}")

    # 2) Caminho do JSON em env
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GSPREAD_CREDENTIALS_PATH")
    if path and os.path.exists(path):
        creds = Credentials.from_service_account_file(
            path,
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"]
        )
        import gspread
        return gspread.authorize(creds)

    raise RuntimeError(
        "Credenciais Google não configuradas. "
        "Defina GOOGLE_SERVICE_ACCOUNT_INFO (JSON) ou GOOGLE_SERVICE_ACCOUNT_JSON (arquivo)."
    )


def export_to_gsheets(
    df: pd.DataFrame,
    spreadsheet_name: str,
    worksheet_name: str = "Results",
    clear_before: bool = True,
) -> str:
    """
    Atualiza uma planilha Google Sheets com os dados do DataFrame.
    Retorna a URL da planilha.
    """
    if df is None or df.empty:
        raise ValueError("DataFrame está vazio. Nada a exportar.")

    client = _get_gs_client()

    # Abre ou cria a planilha
    try:
        sh = client.open(spreadsheet_name)
    except Exception:
        sh = client.create(spreadsheet_name)

    # Busca ou cria a worksheet
    try:
        ws = sh.worksheet(worksheet_name)
    except Exception:
        ws = sh.add_worksheet(title=worksheet_name[:100], rows=1000, cols=40)

    df_safe = _to_sheets_safe(df)
    values = [df_safe.columns.tolist()] + df_safe.fillna("").astype(str).values.tolist()

    if clear_before:
        try:
            ws.clear()
        except Exception:
            pass

    ws.update(values)
    url = sh.url
    print(f"✅ Dados atualizados no Google Sheets: {url}")
    return url


def export_workbook_to_gsheets(
    tabs: Dict[str, pd.DataFrame],
    spreadsheet_name: str,
    clear_before: bool = True,
) -> str:
    """
    Exporta várias abas para uma única planilha no Google Sheets.
    Retorna a URL da planilha.
    """
    if not isinstance(tabs, dict) or not tabs:
        raise ValueError("Parâmetro 'tabs' deve ser um dict não vazio: {'Aba': df, ...}")

    client = _get_gs_client()

    try:
        sh = client.open(spreadsheet_name)
    except Exception:
        sh = client.create(spreadsheet_name)

    for sheet_name, df in tabs.items():
        title = (sheet_name or "Sheet")[:100]
        try:
            ws = sh.worksheet(title)
        except Exception:
            ws = sh.add_worksheet(title=title, rows=2000, cols=60)

        df_safe = _to_sheets_safe(df if isinstance(df, pd.DataFrame) else pd.DataFrame())
        values = [df_safe.columns.tolist()] + df_safe.fillna("").astype(str).values.tolist()

        if clear_before:
            try:
                ws.clear()
            except Exception:
                pass
        ws.update(values)

    url = sh.url
    print(f"✅ Abas atualizadas no Google Sheets: {url}")
    return url


# -------------------------------------------------------------------
# 3) Teste direto
# -------------------------------------------------------------------
if __name__ == "__main__":
    # Exemplo mínimo de uso
    sample_data = {
        "Team": ["Alabama", "Texas"],
        "Opponent": ["Georgia", "Florida"],
        "TDs_For": [5, 3],
        "TDs_Against": [3, 4],
        "Win_%": [87.5, 42.8],
    }
    df_test = pd.DataFrame(sample_data)

    # Exporta localmente (arquivo com prefixo customizado)
    export_to_excel(df_test, filename_prefix="test_results")

    # Exporta várias abas em um único workbook local
    export_workbook({"Results": df_test, "Summary": df_test.describe()}, filename_prefix="test_snapshot")

    # Exporta para Google Sheets (se credenciais estiverem configuradas)
    # export_to_gsheets(df_test, spreadsheet_name="CFB_Results_2024", worksheet_name="Results")
    # export_workbook_to_gsheets({"Results": df_test}, spreadsheet_name="CFB_Snapshot_2024")
