"""Wise as a provider: one account per balance currency, balance statements as transactions. Read-only.

Settings ([providers.wise] in config.toml):
    token_env    = "WISE_TOKEN"              # .env key holding a read-only personal API token
    key_path_env = "WISE_PRIVATE_KEY_PATH"   # optional .env key: RSA private key for SCA-protected statements
    days         = 90                        # default statement window for `finance sync wise`

Handles the SCA (x-2fa-approval) challenge on statement endpoints if a key is registered. Conversions and USD-funded
transfers carry both legs, so the line's USD value and Wise's own rate come from the transfer, and the hub-account leg
matches exactly. Account ids default to wise_<currency> when the Budget Schema has them.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from datetime import date, datetime, time, timezone
from pathlib import Path

import requests

from .base import Provider, ProviderAccount, ProviderBalance, ProviderError, ProviderTransaction

BASE = "https://api.wise.com"


class WiseError(ProviderError):
    pass


class WiseClient:
    def __init__(self, token: str, key_path: str | None = None):
        self.token = token
        self.key_path = Path(key_path).expanduser() if key_path else None
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {self.token}"
        self._profile_id: int | None = None

    # --- SCA -------------------------------------------------------------
    def _sign(self, one_time_token: str) -> str:
        if not self.key_path or not self.key_path.exists():
            raise WiseError(
                "endpoint requires Strong Customer Authentication but WISE_PRIVATE_KEY_PATH is not set. "
                "Generate an RSA key pair, register the public key in Wise settings > API tokens, set the path in .env."
            )
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            raise WiseError("SCA needs the 'cryptography' package: uv add cryptography")
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        sig = key.sign(one_time_token.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _get(self, path: str, **params) -> dict | list:
        r = self.s.get(BASE + path, params=params, timeout=30)
        if r.status_code == 403 and r.headers.get("x-2fa-approval-result") == "REJECTED":
            ott = r.headers["x-2fa-approval"]
            r = self.s.get(
                BASE + path,
                params=params,
                headers={"x-2fa-approval": ott, "X-Signature": self._sign(ott)},
                timeout=30,
            )
        if r.status_code >= 400:
            raise WiseError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- Endpoints -------------------------------------------------------
    def profile_id(self) -> int:
        if self._profile_id is None:
            profiles = self._get("/v2/profiles")
            personal = [p for p in profiles if p.get("type", "").lower() == "personal"]
            if not personal:
                raise WiseError(f"no personal profile in {profiles}")
            self._profile_id = int(personal[0]["id"])
        return self._profile_id

    def balances(self) -> list[dict]:
        """[{id, currency, amount:{value,currency}, type}] for STANDARD balances."""
        return self._get(f"/v4/profiles/{self.profile_id()}/balances", types="STANDARD")

    def statement(self, balance_id: int, currency: str, start: datetime, end: datetime) -> dict:
        """Balance statement (JSON, COMPACT). SCA-protected."""
        fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return self._get(
            f"/v1/profiles/{self.profile_id()}/balance-statements/{balance_id}/statement.json",
            currency=currency,
            intervalStart=fmt(start),
            intervalEnd=fmt(end),
            type="COMPACT",
        )

    def transfers(self, created_since: datetime | None = None, limit: int = 100) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            params = dict(profile=self.profile_id(), offset=offset, limit=limit)
            if created_since:
                params["createdDateStart"] = created_since.strftime("%Y-%m-%d %H:%M:%S")
            page = self._get("/v1/transfers", **params)
            out += page
            if len(page) < limit:
                return out
            offset += limit


def usd_value(line: dict, currency: str) -> tuple[float | None, float | None]:
    """(amount_usd, rate_to_usd) from a statement line's transfer details when a USD leg is present."""
    d = line.get("details") or {}
    src = d.get("sourceAmount") or {}
    tgt = d.get("targetAmount") or {}
    amt = float(line["amount"]["value"])
    if currency == "USD":
        return amt, 1.0
    if src.get("currency") == "USD" and tgt.get("currency") == currency and float(tgt.get("value") or 0):
        rate = float(src["value"]) / float(tgt["value"])
        return round(amt * rate, 2), rate
    if tgt.get("currency") == "USD" and src.get("currency") == currency and float(src.get("value") or 0):
        rate = float(tgt["value"]) / float(src["value"])
        return round(amt * rate, 2), rate
    return None, None


def statement_line(balance_id: str, currency: str, line: dict) -> ProviderTransaction:
    amt = float(line["amount"]["value"])  # Wise statement amounts are signed: DEBIT negative
    if line.get("type") == "DEBIT" and amt > 0:
        amt = -amt
    det = line.get("details") or {}
    usd, rate = usd_value(line, currency)
    if usd is not None and (usd > 0) != (amt > 0):
        usd = -usd
    return ProviderTransaction(
        external_id=f"{balance_id}:{line.get('referenceNumber')}",
        account_external_id=balance_id,
        date=line["date"][:10],
        amount=amt,
        currency=currency,
        description=det.get("description") or (det.get("merchant") or {}).get("name") or line.get("referenceNumber"),
        counterparty=det.get("senderName") or det.get("recipientName") or None,
        amount_usd=usd,
        fx_rate_to_usd=rate if currency != "USD" else None,
        raw=line,
    )


class WiseProvider(Provider):
    institution = "Wise"

    def __init__(self, cfg, name, settings):
        super().__init__(cfg, name, settings)
        key_env = settings.get("key_path_env", "WISE_PRIVATE_KEY_PATH")
        self.client = WiseClient(cfg.secret(settings.get("token_env", "WISE_TOKEN")), cfg.optional_secret(key_env))
        self._balances: list[dict] | None = None

    def _raw_balances(self) -> list[dict]:
        if self._balances is None:
            self._balances = self.client.balances()
        return self._balances

    def accounts(self) -> list[ProviderAccount]:
        return [ProviderAccount(external_id=str(b["id"]), name=f"Wise {b['currency']}", type="multicurrency",
                                currency=b["currency"], suggested_id=f"wise_{b['currency'].lower()}")
                for b in self._raw_balances()]

    def transactions(self, start: date, end: date) -> Iterable[ProviderTransaction]:
        s = datetime.combine(start, time.min, tzinfo=timezone.utc)
        e = datetime.combine(end, time.max, tzinfo=timezone.utc)
        for b in self._raw_balances():
            stmt = self.client.statement(int(b["id"]), b["currency"], s, e)
            for line in stmt.get("transactions", []):
                yield statement_line(str(b["id"]), b["currency"], line)

    def balances(self) -> list[ProviderBalance]:
        return [ProviderBalance(str(b["id"]), float(b["amount"]["value"]), b["currency"]) for b in self._raw_balances()]
