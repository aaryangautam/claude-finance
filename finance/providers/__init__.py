"""Pluggable data providers for anything Plaid doesn't cover (Wise, other banks' APIs, exchanges, pension portals).

Enable one with a table in config.toml:

    [providers.wise]                                   # the name tags its rows (source) and provisional account ids
    module = "finance.providers.wise:WiseProvider"     # optional for bundled providers; any importable module:Class
    token_env = "WISE_TOKEN"                           # every other key is passed to the provider as settings

The repo root is on the import path, so a provider can live in your own folder (e.g. my_providers/revolut.py ->
module = "my_providers.revolut:RevolutProvider"). See README "Adding a data source".
"""

from __future__ import annotations

import importlib
import sys

from ..config import Config, ConfigError
from .base import Provider, ProviderAccount, ProviderBalance, ProviderError, ProviderHolding, ProviderTransaction

BUILTIN = {"wise": "finance.providers.wise:WiseProvider"}
RESERVED = {"plaid", "all", "csv_backfill", "test", "ecb", "manual"}

__all__ = ["Provider", "ProviderAccount", "ProviderBalance", "ProviderError", "ProviderHolding", "ProviderTransaction",
           "configured", "load", "get"]


def configured(cfg: Config) -> dict[str, dict]:
    """{name: settings} for every enabled [providers.<name>] table."""
    out = {}
    for name, settings in (cfg.raw.get("providers") or {}).items():
        if not isinstance(settings, dict):
            raise ConfigError(f"[providers.{name}] must be a table")
        if name in RESERVED:
            raise ConfigError(f"provider name {name!r} is reserved")
        if settings.get("enabled", True):
            out[name] = settings
    return out


def _resolve(cfg: Config, name: str, settings: dict) -> type[Provider]:
    spec = settings.get("module") or BUILTIN.get(name)
    if not spec or ":" not in spec:
        raise ConfigError(f"[providers.{name}]: set module = \"package.module:ClassName\"")
    mod_name, cls_name = spec.split(":", 1)
    root = str(cfg.root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        cls = getattr(importlib.import_module(mod_name), cls_name)
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"[providers.{name}]: can't load {spec}: {e}")
    if not (isinstance(cls, type) and issubclass(cls, Provider)):
        raise ConfigError(f"[providers.{name}]: {spec} is not a finance.providers.Provider subclass")
    return cls


def get(cfg: Config, name: str) -> Provider:
    conf = configured(cfg)
    if name not in conf:
        raise ConfigError(f"no enabled provider {name!r} in config.toml (configured: {sorted(conf) or 'none'})")
    settings = conf[name]
    return _resolve(cfg, name, settings)(cfg, name, settings)


def load(cfg: Config) -> list[Provider]:
    return [get(cfg, name) for name in configured(cfg)]
