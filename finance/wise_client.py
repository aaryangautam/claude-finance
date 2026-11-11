"""Wise personal API, read-only. Handles the SCA (x-2fa-approval) challenge on statement endpoints if a key is registered."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config

BASE = "https://api.wise.com"


class WiseError(RuntimeError):
    pass


class WiseClient:
    def __init__(self, cfg: Config):
        self.token = cfg.secret("WISE_TOKEN")
        key_path = cfg.optional_secret("WISE_PRIVATE_KEY_PATH")
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
