"""The provider contract: how any data source that Plaid doesn't cover plugs in.

A provider only translates its API into these dataclasses. It never touches the database, never matches transfers and
never categorizes; `finance.providers.sync` does the storage, and the pipeline treats provider rows like Plaid rows.

Sign convention (same as everywhere): amount > 0 = money INTO the account. Liability balances are negative.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from ..config import Config


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderAccount:
    external_id: str          # stable id within this provider
    name: str
    type: str                 # checking|savings|credit|loan|brokerage|ira|multicurrency|other (display only)
    currency: str
    suggested_id: str | None = None  # used as the account id when the Budget Schema has it; else provisional <provider>:<id>


@dataclass
class ProviderTransaction:
    external_id: str          # unique within this provider; re-syncing the same id updates the row
    account_external_id: str
    date: date | str          # posting date
    amount: float             # native currency, >0 into the account
    currency: str
    description: str | None   # what the statement says; becomes merchant_raw
    counterparty: str | None = None   # sender/recipient name if the API gives one (payback matching)
    amount_usd: float | None = None   # if the provider knows the USD value exactly (e.g. its own FX leg)
    fx_rate_to_usd: float | None = None
    pending: bool = False
    raw: dict = field(default_factory=dict)  # stored as raw_json for debugging; never read by the pipeline


@dataclass
class ProviderBalance:
    account_external_id: str
    balance: float            # native currency; negative for liabilities
    currency: str


@dataclass
class ProviderHolding:
    account_external_id: str
    ticker: str
    quantity: float | None
    price: float | None
    value_usd: float | None
    name: str | None = None


class Provider:
    """Subclass and implement accounts/transactions/balances. `settings` is the provider's [providers.<name>] table."""

    institution: str = ""     # shown in `finance status`; defaults to the config name

    def __init__(self, cfg: Config, name: str, settings: dict):
        self.cfg = cfg
        self.name = name
        self.settings = settings

    def secret(self, key: str) -> str:
        """A value from .env, named by settings[key] (e.g. token_env = "WISE_TOKEN") or the key itself."""
        return self.cfg.secret(self.settings.get(key, key))

    def accounts(self) -> list[ProviderAccount]:
        raise NotImplementedError

    def transactions(self, start: date, end: date) -> Iterable[ProviderTransaction]:
        raise NotImplementedError

    def balances(self) -> list[ProviderBalance]:
        raise NotImplementedError

    def holdings(self) -> list[ProviderHolding]:
        return []
