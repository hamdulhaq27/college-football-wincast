# modules/cfbd_dual.py
from __future__ import annotations
import os, time, logging, requests
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

logger = logging.getLogger("cfbd_dual")
BASE = "https://api.collegefootballdata.com"

def _mask(key: str) -> str:
    if not key: return "<empty>"
    if len(key) <= 8: return "*" * len(key)
    return key[:4] + "…" + key[-4:]

def _load_keys() -> List[str]:
    """
    Supported env styles:
      - CFBD_API_KEY (primary)
      - CFBD_API_KEY_2 (fallback)
      - CFBD_KEYS="key1,key2"  (or semicolon-separated)
    """
    keys: List[str] = []
    k1 = (os.getenv("CFBD_API_KEY") or "").strip().strip('"').strip("'")
    k2 = (os.getenv("CFBD_API_KEY_2") or os.getenv("CFBD_API_KEY_SECONDARY") or "").strip().strip('"').strip("'")
    if k1: keys.append(k1)
    if k2 and k2 != k1: keys.append(k2)

    klist = (os.getenv("CFBD_KEYS") or "").strip()
    if klist:
        for k in klist.replace(";", ",").split(","):
            k = k.strip().strip('"').strip("'")
            if k and k not in keys:
                keys.append(k)

    # Strip accidental "Bearer " prefixes
    norm = []
    for k in keys:
        if k.lower().startswith("bearer "):
            k = k.split(None, 1)[1]
        norm.append(k)
    return [k for k in norm if k]

def _headers_for(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}

def cfbd_request(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
    retries: int = 2,
    backoff: float = 0.8,
    rotate_status: Iterable[int] = (401, 403, 408, 429, 500, 502, 503, 504),
) -> requests.Response:
    """
    Robust GET with key rotation. Tries all keys for each attempt before backing off.
    Raises requests.HTTPError on failure (like requests.get(...).raise_for_status()).
    """
    keys = _load_keys()
    if not keys:
        raise RuntimeError("No CFBD API keys found (set CFBD_API_KEY and/or CFBD_API_KEY_2).")

    url = f"{BASE}{path}" if path.startswith("/") else f"{BASE}/{path}"
    params = params or {}

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        # Try each key this attempt
        for idx, key in enumerate(keys):
            try:
                r = requests.get(url, headers=_headers_for(key), params=params, timeout=timeout)
                # Fast-path: success
                if r.status_code < 400:
                    return r
                # If status suggests rotating keys, try next key (or next attempt)
                if r.status_code in rotate_status:
                    logger.debug(f"[cfbd_dual] {url} -> {r.status_code}; key {_mask(key)}; will rotate/backoff")
                    # Try next key in loop or backoff after loop
                    last_err = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
                    continue
                # Other errors: raise immediately
                r.raise_for_status()
                return r
            except requests.HTTPError as e:
                last_err = e
                # Try next key this attempt
                continue
            except requests.RequestException as e:
                last_err = e
                # Network issue: try next key
                continue

        # No key succeeded this attempt; back off before next attempt
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))

    # Out of attempts
    if last_err:
        raise last_err
    raise RuntimeError("Unknown error in cfbd_request")

def cfbd_get_json(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
    retries: int = 2,
    backoff: float = 0.8,
) -> Union[List[Any], Dict[str, Any]]:
    """Convenience wrapper that returns parsed JSON (list/dict)."""
    r = cfbd_request(path, params=params, timeout=timeout, retries=retries, backoff=backoff)
    try:
        return r.json() or []
    except Exception:
        return []
