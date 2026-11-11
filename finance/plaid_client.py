"""Thin wrapper over plaid-python. Read-only products only; nothing here can move money."""

from __future__ import annotations

from datetime import date

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

from .config import Config

ENVS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}


class PlaidError(RuntimeError):
    def __init__(self, code: str, message: str, item_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.item_id = item_id


def _wrap(e: plaid.ApiException) -> PlaidError:
    try:
        import json

        body = json.loads(e.body)
        return PlaidError(body.get("error_code", "UNKNOWN"), body.get("error_message", str(e)))
    except Exception:
        return PlaidError("UNKNOWN", str(e))


class PlaidClient:
    def __init__(self, cfg: Config):
        env = cfg.secret("PLAID_ENV").lower()
        if env not in ENVS:
            raise ValueError(f"PLAID_ENV must be sandbox or production, got {env}")
        self.env = env
        configuration = plaid.Configuration(
            host=ENVS[env],
            api_key={"clientId": cfg.secret("PLAID_CLIENT_ID"), "secret": cfg.secret("PLAID_SECRET")},
        )
        self.api = plaid_api.PlaidApi(plaid.ApiClient(configuration))

    # --- Link -------------------------------------------------------------
    def create_link_token(self, user_id: str, access_token: str | None = None, days_requested: int = 730) -> str:
        """New Item: products=transactions with investments/liabilities optional. Update mode: pass access_token."""
        kwargs: dict = dict(
            client_name="Finance (local)",
            language="en",
            country_codes=[CountryCode("US")],
            user=LinkTokenCreateRequestUser(client_user_id=user_id),
        )
        if access_token:
            kwargs["access_token"] = access_token
        else:
            kwargs["products"] = [Products("transactions")]
            kwargs["optional_products"] = [Products("investments"), Products("liabilities")]
            from plaid.model.link_token_transactions import LinkTokenTransactions

            kwargs["transactions"] = LinkTokenTransactions(days_requested=days_requested)
        try:
            resp = self.api.link_token_create(LinkTokenCreateRequest(**kwargs))
        except plaid.ApiException as e:
            raise _wrap(e)
        return resp.link_token

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        try:
            resp = self.api.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public_token))
        except plaid.ApiException as e:
            raise _wrap(e)
        return resp.access_token, resp.item_id

    def sandbox_public_token(self, institution_id: str = "ins_109508") -> str:
        """Sandbox only: a public token for a test Item without opening Link (default: First Platypus Bank)."""
        if self.env != "sandbox":
            raise ValueError("sandbox_public_token needs PLAID_ENV=sandbox")
        try:
            return self.api.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
                institution_id=institution_id, initial_products=[Products("transactions")])).public_token
        except plaid.ApiException as e:
            raise _wrap(e)

    # --- Item -------------------------------------------------------------
    def item(self, access_token: str) -> dict:
        try:
            return self.api.item_get(ItemGetRequest(access_token=access_token)).to_dict()
        except plaid.ApiException as e:
            raise _wrap(e)

    # --- Data -------------------------------------------------------------
    def transactions_sync(self, access_token: str, cursor: str | None) -> dict:
        """One page. Caller loops on has_more. Returns dict with added/modified/removed/next_cursor/has_more/accounts."""
        kwargs = dict(access_token=access_token, count=500, options=TransactionsSyncRequestOptions(include_original_description=True))
        if cursor:
            kwargs["cursor"] = cursor
        try:
            return self.api.transactions_sync(TransactionsSyncRequest(**kwargs)).to_dict()
        except plaid.ApiException as e:
            raise _wrap(e)

    def balances(self, access_token: str) -> list[dict]:
        try:
            return self.api.accounts_balance_get(AccountsBalanceGetRequest(access_token=access_token)).to_dict()["accounts"]
        except plaid.ApiException as e:
            raise _wrap(e)

    def liabilities(self, access_token: str) -> dict:
        try:
            return self.api.liabilities_get(LiabilitiesGetRequest(access_token=access_token)).to_dict()
        except plaid.ApiException as e:
            raise _wrap(e)

    def holdings(self, access_token: str) -> dict:
        try:
            return self.api.investments_holdings_get(InvestmentsHoldingsGetRequest(access_token=access_token)).to_dict()
        except plaid.ApiException as e:
            raise _wrap(e)

    def investment_transactions(self, access_token: str, start: date, end: date) -> dict:
        """All pages merged: investment_transactions, securities, accounts."""
        out: dict = {"investment_transactions": [], "securities": [], "accounts": []}
        offset = 0
        while True:
            req = InvestmentsTransactionsGetRequest(
                access_token=access_token,
                start_date=start,
                end_date=end,
                options=InvestmentsTransactionsGetRequestOptions(count=500, offset=offset),
            )
            try:
                page = self.api.investments_transactions_get(req).to_dict()
            except plaid.ApiException as e:
                raise _wrap(e)
            out["investment_transactions"] += page["investment_transactions"]
            out["securities"] = page["securities"]
            out["accounts"] = page["accounts"]
            offset += len(page["investment_transactions"])
            if offset >= page["total_investment_transactions"] or not page["investment_transactions"]:
                break
        return out
