"""
choice_api.py — Choice FinX OpenAPI client.

Docs: https://finx.choiceindia.com/api/OpenAPI/Info

── Auth model ─────────────────────────────────────────────────────────────────
Every request needs three headers:
    VendorId : fixed per-vendor id issued by Choice.
    VendorKey: fixed per-vendor secret issued by Choice.
    Bearer   : the long-lived API Key generated from
               Finx website > Profile > Generate API Key.
               (Choice's doc literally names this header "Bearer", it is
               NOT "Authorization: Bearer <token>".)

On top of that, every call EXCEPT the login endpoints also needs:
    Authorization: <SessionId>
SessionId is obtained via the 3-step login flow below and is cached to
.env (mirrors the old pyintegrate session-key pattern) so we don't need to
log in again until it expires.

── Login flow ─────────────────────────────────────────────────────────────────
  1. LoginTOTP          POST /api/OpenAPIV1/LoginTOTP
                        body: {"MobileNo": <AES-encrypted>}
                        → triggers an OTP via SMS/email to the client.
  2. GetClientLoginTOTP POST /api/OpenAPIV1/GetClientLoginTOTP
                        body: {"MobileNo": <AES-encrypted>}
                        → vendor-only: fetches that OTP programmatically so
                          an unattended bot doesn't need a human to read SMS.
  3. ValidateTOTP       POST /api/OpenAPIV1/ValidateTOTP
                        body: {"MobileNo": <AES-encrypted>, "OTP": <otp>}
                        → returns SessionId (exact response field name is
                          unconfirmed from the doc; ChoiceClient.login()
                          probes a few likely keys — see _extract_session_id).

MobileNo encryption: AES-256-CBC, PKCS7 padding, base64-encoded ciphertext.
Key/IV are issued by Choice in a separate document and must be supplied via
the CHOICE_AES_KEY / CHOICE_AES_IV env vars (base64 or hex — see
_load_aes_material). NOTE: this module cannot be exercised end-to-end
without those two secrets; until they're set, login() will raise.

── Chart data (replaces 1-min fetch + 5-min resample) ─────────────────────────
POST /api/OpenGraph/ChartData
    body: {SegmentId, Token, FromDate, ToDate, Interval}
    FromDate/ToDate: epoch seconds, but counted from 1980-01-01 (NOT the
    usual 1970-01-01 Unix epoch). See _to_choice_epoch / _from_choice_epoch.
    Interval: "5" → 5-minute intraday bars (matches config.BAR_MINUTES).
    Response: {"PriceDivisor": ..., "Volume": ..., "lstChartHistory": [...]}
    Each row: PriceDate, OpenPrice, HighPrice, LowPrice, ClosePrice, Volume
    (prices in paise — divide by PriceDivisor to get rupees).

NOTE ON TIMEZONE: the doc doesn't state which timezone PriceDate is
expressed in. We assume IST (matching NSE trading hours convention used
throughout the rest of this codebase) — if bars come back misaligned with
09:15–15:30 IST, this is the first thing to check.
"""

import base64
import logging
import os
from datetime import datetime, timedelta

import requests

from config_daily import IST

logger = logging.getLogger(__name__)

CHOICE_BASE_URL = os.environ.get("CHOICE_BASE_URL", "https://finx.choiceindia.com")

# Choice's custom epoch origin (NOT Unix epoch).
_CHOICE_EPOCH_ORIGIN = datetime(1980, 1, 1)


def _to_choice_epoch(dt: datetime) -> int:
    """Convert a naive/aware datetime to seconds-since-1980-01-01."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return int((dt - _CHOICE_EPOCH_ORIGIN).total_seconds())


def _from_choice_epoch(seconds: int) -> "object":
    """Convert seconds-since-1980-01-01 to an IST-aware pd.Timestamp."""
    import pandas as pd
    naive = _CHOICE_EPOCH_ORIGIN + timedelta(seconds=int(seconds))
    return pd.Timestamp(naive).tz_localize(IST, nonexistent="shift_forward", ambiguous="NaT")


# ── AES-256-CBC/PKCS7 mobile-number encryption ────────────────────────────────

def _load_aes_material() -> tuple[bytes, bytes]:
    """
    Load AES key + IV from env. Accepts either base64 or hex encoding,
    auto-detected by length/charset. Raises if not configured.
    """
    key_raw = os.environ.get("CHOICE_AES_KEY", "")
    iv_raw  = os.environ.get("CHOICE_AES_IV", "")
    if not key_raw or not iv_raw:
        raise RuntimeError(
            "CHOICE_AES_KEY / CHOICE_AES_IV are not set in .env. These are "
            "issued by Choice in a separate document alongside the API key "
            "and are required to encrypt MobileNo for login."
        )

    def _decode(raw: str, expected_len: int) -> bytes:
        # Try base64 first, then hex, then raw UTF-8.
        try:
            b = base64.b64decode(raw)
            if len(b) == expected_len:
                return b
        except Exception:
            pass
        try:
            b = bytes.fromhex(raw)
            if len(b) == expected_len:
                return b
        except Exception:
            pass
        # Last resort: treat the value as a raw UTF-8 string key/iv.
        b = raw.encode("utf-8")
        if len(b) == expected_len:
            return b
        raise RuntimeError(
            f"Could not decode AES material to {expected_len} bytes "
            f"(tried base64, hex, and raw UTF-8). "
            f"Got {len(b)} bytes. Check CHOICE_AES_KEY/CHOICE_AES_IV."
        )

    return _decode(key_raw, 32), _decode(iv_raw, 16)


def encrypt_mobile_no(mobile_no: str) -> str:
    """
    AES-256-CBC encrypt + PKCS7-pad + base64-encode a mobile number,
    per Choice's "Encrypted MobileNo" parameter spec.
    """
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    key, iv = _load_aes_material()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(mobile_no.encode("utf-8"), AES.block_size, style="pkcs7")
    ct = cipher.encrypt(padded)
    return base64.b64encode(ct).decode("ascii")


# ── Client ─────────────────────────────────────────────────────────────────────

class ChoiceClient:
    """
    Thin REST client for Choice FinX OpenAPI.

    Usage:
        client = ChoiceClient(vendor_id, vendor_key, api_key)
        client.login(mobile_no)                 # full 3-step OTP flow
        # ... or, to reuse a cached session:
        client.set_session_id(cached_session_id)

        df = client.get_chart_data(segment_id=1, token=2885,
                                    from_dt=..., to_dt=..., interval="5")
    """

    def __init__(self, vendor_id: str, vendor_key: str, api_key: str,
                 base_url: str = CHOICE_BASE_URL, timeout: float = 15.0):
        self.vendor_id  = vendor_id
        self.vendor_key = vendor_key
        self.api_key    = api_key
        self.base_url   = base_url.rstrip("/")
        self.timeout    = timeout
        self.session_id: str | None = None
        self._http = requests.Session()

    # ── Header helpers ────────────────────────────────────────────────────────

    def _login_headers(self) -> dict:
        return {
            "VendorId":  self.vendor_id,
            "VendorKey": self.vendor_key,
            "Bearer":    self.api_key,
            "Content-Type": "application/json",
        }

    def _auth_headers(self) -> dict:
        if not self.session_id:
            raise RuntimeError("Not logged in: call login() or set_session_id() first.")
        h = self._login_headers()
        h["Authorization"] = f"Bearer {self.session_id}"
        return h

    # ── Low-level request helper ──────────────────────────────────────────────

    def _request(self, method: str, path: str, headers: dict,
                 json_body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._http.request(method, url, headers=headers,
                                  json=json_body, timeout=self.timeout)
        if resp.status_code == 401:
            raise PermissionError(f"401 from {path}: session expired or invalid")
        resp.raise_for_status()
        data = resp.json()

        # Some Choice endpoints return a bare string instead of a JSON object
        if isinstance(data, str):
            return data

        status = str(data.get("Status", "")).lower()
        if status not in ("success", "ok", "1", "true"):
            reason = data.get("Reason", "")
            logger.warning(f"{path} returned Status={data.get('Status')!r} Reason={reason!r}")
        return data

    # ── Login flow ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_session_id(resp: dict) -> str | None:
        """
        Pull the session token out of a ValidateTOTP response.

        Known response shapes observed:
          {'Status':'Success', 'Response': {'LogonStatus':10000, 'LogonMessage':'<token>'}}
          {'Status':'Success', 'Response': '<token-string>'}
          {'Status':'Success', 'Response': {'SessionId':'<token>'}}
        """
        payload = resp.get("Response") or {}
        # If Response is a plain non-empty string, treat it as the token.
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            for key in (
                "SessionId", "sessionId", "session_id",
                "AccessToken", "access_token", "Authorization", "Token",
                "LogonMessage",  # Used by older APIs
            ):
                val = payload.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    def login(self, mobile_no: str, otp: str | None = None) -> str:
        """
        Full login flow:
          1. LoginTOTP           — triggers OTP (skipped if `otp` already provided)
          2. GetClientLoginTOTP  — fetches the OTP programmatically (skipped if
                                    `otp` already provided, e.g. via CLI prompt
                                    or --otp flag)
          3. ValidateTOTP        — exchanges OTP for SessionId

        Returns and caches the SessionId.

        When called from run_paper._fetch_or_prompt_otp(), steps 1+2 have
        already been handled there, so `otp` will be non-None on entry.
        """
        enc_mobile = encrypt_mobile_no(mobile_no)
        headers = self._login_headers()

        if otp is None:
            # Steps 1+2: trigger and fetch OTP automatically (vendor-only path).
            logger.info("Choice login: requesting OTP (LoginTOTP)…")
            self._request("POST", "/api/OpenAPIV1/LoginTOTP", headers,
                          {"MobileNo": enc_mobile})

            logger.info("Choice login: fetching OTP (GetClientLoginTOTP)…")
            otp_resp = self._request("POST", "/api/OpenAPIV1/GetClientLoginTOTP",
                                     headers, {"MobileNo": enc_mobile})
            payload = otp_resp.get("Response") or {}
            if isinstance(payload, str) and payload.strip():
                otp = payload.strip()
            elif isinstance(payload, dict):
                otp = payload.get("OTP") or payload.get("Otp") or payload.get("otp")
            else:
                otp = None
            if not otp:
                raise RuntimeError(
                    "GetClientLoginTOTP did not return an OTP in the expected "
                    "shape; inspect otp_resp['Response'] and adjust this parser."
                )

        logger.info("Choice login: validating OTP (ValidateTOTP)…")
        val_resp = self._request("POST", "/api/OpenAPIV1/ValidateTOTP", headers,
                                 {"MobileNo": enc_mobile, "OTP": str(otp)})

        # Log the full response so we can diagnose unexpected session formats.
        logger.debug(f"ValidateTOTP raw response: {val_resp!r}")
        logger.info(f"ValidateTOTP response keys: Status={val_resp.get('Status')!r} "
                    f"Response={val_resp.get('Response')!r}")

        session_id = self._extract_session_id(val_resp)
        if not session_id:
            raise RuntimeError(
                f"Could not find SessionId in ValidateTOTP response: {val_resp!r}.\n"
                "Common causes:\n"
                "  1. OTP was wrong or expired — try re-running.\n"
                "  2. The session field has an unexpected key — check the log above\n"
                "     and add it to _extract_session_id().\n"
                "  3. The API returned Status!=success — check Reason in the log."
            )
        self.session_id = session_id

        # Update to the dynamic gateway URL returned by ValidateTOTP.
        base_url = val_resp.get("BaseURL")
        if base_url and isinstance(base_url, str):
            self.base_url = base_url.rstrip("/")
            logger.info(f"Updated BaseURL from login response: {self.base_url}")

        logger.info(f"Choice login successful. Session ID starts with: {session_id[:12]}…")
        return session_id

    def set_session_id(self, session_id: str):
        """Restore a previously cached session (skip the OTP round-trip)."""
        self.session_id = session_id

    def is_session_valid(self) -> bool:
        """
        Quick sanity probe to verify the current session_id is set and non-empty.
        A True result here does NOT guarantee the server accepts it—the real
        proof comes from the first authenticated API call (ChartData/warmup).
        Use this only to catch obviously-bad states (empty/None session).
        """
        return bool(self.session_id and self.session_id.strip())

    # ── Chart data (historical + "live" 5-min bars) ──────────────────────────

    def get_chart_data(self, segment_id: int, token: int,
                       from_dt: datetime, to_dt: datetime,
                       interval: str = "5") -> "object":
        """
        Fetch OHLCV bars for one instrument.

        Returns a pandas DataFrame indexed by IST-aware Timestamp with
        columns Open/High/Low/Close/Volume (already converted from paise
        to rupees). Empty DataFrame on no data.
        """
        import pandas as pd

        body = {
            "SegmentId": segment_id,
            "Token":     token,
            "FromDate":  _to_choice_epoch(from_dt),
            "ToDate":    _to_choice_epoch(to_dt),
            "Interval":  interval,
        }
        resp = self._request("POST", "/api/OpenGraph/ChartData",
                             self._auth_headers(), body)
        if isinstance(resp, str):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        payload = resp.get("Response") or resp
        if isinstance(payload, str):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        rows = payload.get("lstChartHistory") or []
        divisor = float(payload.get("PriceDivisor") or 100)  # paise default

        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        records = []
        for r in rows:
            if isinstance(r, str):
                r = r.split(",")
            # Field names per doc: PriceDate, OpenPrice, HighPrice, LowPrice,
            # ClosePrice, Volume — order given as "consolidated order wise".
            
            if isinstance(r, dict):
                ts = _from_choice_epoch(r.get("PriceDate") or r.get("Date")).floor('5min')
                o = float(r.get("OpenPrice", 0)) / divisor
                h = float(r.get("HighPrice", 0)) / divisor
                l = float(r.get("LowPrice", 0)) / divisor
                c = float(r.get("ClosePrice", 0)) / divisor
                v = float(r.get("Volume", 0))
            else:
                ts = _from_choice_epoch(r[0]).floor('5min')
                o = float(r[1]) / divisor
                h = float(r[2]) / divisor
                l = float(r[3]) / divisor
                c = float(r[4]) / divisor
                v = float(r[5])
            records.append({"ts": ts, "Open": o, "High": h, "Low": l,
                           "Close": c, "Volume": v})

        df = pd.DataFrame(records).set_index("ts").sort_index()
        df.index.name = None
        # In choice_api.py → ChoiceClient.get_chart_data(), add right before the final return:
        if df.index.has_duplicates:
            df = df[~df.index.duplicated(keep='last')]

        return df[["Open", "High", "Low", "Close", "Volume"]]

    # ── Scrip details (optional cross-check for symbol_mapper) ───────────────

    def get_scrip_details(self, segment_id: int, token: int) -> dict:
        resp = self._request("POST", "/api/OpenAPI/ScripDetails",
                             self._auth_headers(),
                             {"SegmentId": segment_id, "Token": token})
        return resp.get("Response") or {}

    def get_market_status(self) -> dict:
        resp = self._request("GET", "/api/OpenAPI/MarketStatus",
                             self._auth_headers())
        return resp.get("Response") or {}
