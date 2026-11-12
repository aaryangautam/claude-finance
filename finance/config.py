"""Configuration: .env (secrets) + config.toml (everything else). Fails loudly on anything missing."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    root: Path
    db_path: Path
    work_dir: Path
    exports_dir: Path
    vault_finance: Path
    env: dict[str, str]
    raw: dict = field(repr=False)

    # --- secrets ---------------------------------------------------------
    def secret(self, key: str) -> str:
        val = self.env.get(key) or os.environ.get(key)
        if not val:
            raise ConfigError(f"{key} is not set in {self.root / '.env'}")
        return val

    def optional_secret(self, key: str) -> str | None:
        return self.env.get(key) or os.environ.get(key) or None

    def plaid_tokens(self) -> dict[str, str]:
        """{'PLAID_TOKEN_MYBANK': 'access-...', ...}"""
        return {k: v for k, v in self.env.items() if k.startswith("PLAID_TOKEN_") and v}

    # --- typed accessors -------------------------------------------------
    @property
    def unclear_threshold(self) -> float:
        return float(self.raw["categorizer"]["unclear_threshold"])

    @property
    def reimbursement_category(self) -> str:
        return self.raw["categorizer"]["reimbursement_category"]

    @property
    def matcher_window_days(self) -> int:
        return int(self.raw["matcher"]["window_days"])

    @property
    def matcher_tolerance_usd(self) -> float:
        return float(self.raw["matcher"]["amount_tolerance_usd"])

    @property
    def transfer_patterns(self) -> dict[str, list[str]]:
        return dict(self.raw["matcher"].get("transfer_patterns", {}))

    @property
    def income_category(self) -> str:
        return self.raw["categorizer"]["rules"]["income_category"]

    @property
    def plaid_seed(self) -> tuple[dict[str, str], dict[str, str]]:
        c = self.raw["categorizer"]
        return dict(c.get("plaid_seed", {})), dict(c.get("plaid_seed_detailed", {}))

    @property
    def reimbursement_senders(self) -> list[str]:
        return [s.lower() for s in self.raw.get("reimbursements", {}).get("senders", [])]

    @property
    def sender_reimbursements(self) -> dict[str, str]:
        """lowercased sender substring -> category the payback nets against, for inflows on any account."""
        r = self.raw.get("reimbursements", {})
        out = {s: self.reimbursement_category for s in self.reimbursement_senders}
        out.update({k.lower(): v for k, v in r.get("categories", {}).items()})
        return out

    @property
    def other_income_patterns(self) -> list[str]:
        return [s.upper() for s in self.raw["income"].get("other_patterns", [])]

    @property
    def other_income_category(self) -> str | None:
        return self.raw["income"].get("other_income_category")

    @property
    def zelle_reimbursements(self) -> dict[str, str | None]:
        """lowercased sender substring -> category id, or None when the category varies per transaction."""
        z = self.raw.get("reimbursements", {}).get("zelle", {})
        return {k.lower(): (v or None) for k, v in z.items()}

    @property
    def link_institutions(self) -> list[str]:
        """Labels offered in the Plaid Link dropdown; each becomes an Item name and its PLAID_TOKEN_<LABEL> key."""
        return list(self.raw.get("link", {}).get("institutions", [])) or ["Bank"]

    @property
    def schedule(self) -> dict:
        return dict(self.raw.get("schedule", {}))

    @property
    def per_transaction_patterns(self) -> list[str]:
        return [s.upper() for s in self.raw["categorizer"].get("per_transaction_patterns", [])]

    @property
    def payroll_patterns(self) -> list[str]:
        return [s.upper() for s in self.raw["income"]["payroll_patterns"]]

    @property
    def budget_schema_path(self) -> Path:
        return self.vault_finance / "Budget Schema.md"

    @property
    def goals_dir(self) -> Path:
        return self.vault_finance / "Goals"

    @property
    def reviews_dir(self) -> Path:
        return self.vault_finance / "Reviews"


def load(root: Path | None = None, require_env: bool = True, require_vault: bool = True) -> Config:
    root = root or ROOT
    toml_path = root / "config.toml"
    if not toml_path.exists():
        raise ConfigError(f"missing {toml_path} (copy config.example.toml and edit it)")
    with toml_path.open("rb") as f:
        raw = tomllib.load(f)

    env_path = root / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        env = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
    elif require_env:
        raise ConfigError(f"missing {env_path} (copy .env.example)")

    paths = raw["paths"]

    def rel(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else root / q

    vault = rel(paths["vault_finance"])
    if require_vault and not vault.exists():
        raise ConfigError(f"vault Finance folder not found: {vault} (set [paths] vault_finance in config.toml, or run `finance init`)")

    return Config(
        root=root,
        db_path=rel(paths["db"]),
        work_dir=rel(paths["work"]),
        exports_dir=rel(paths["exports"]),
        vault_finance=vault,
        env=env,
        raw=raw,
    )
