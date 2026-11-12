"""A provider with canned data: the test double for the provider contract, and a template for writing one."""

from finance.providers import Provider, ProviderAccount, ProviderBalance, ProviderHolding, ProviderTransaction


class FakeProvider(Provider):
    institution = "Fake Neobank"

    def accounts(self):
        return [ProviderAccount("acc-eur", "Fake EUR", "multicurrency", "EUR", suggested_id="wise_eur"),
                ProviderAccount("acc-x", "Fake Broker", "brokerage", "USD")]

    def transactions(self, start, end):
        yield ProviderTransaction("t1", "acc-eur", "2026-10-02", -1500.0, "EUR", "RENT OCTOBER",
                                  counterparty="Landlord GmbH", amount_usd=-1650.0, fx_rate_to_usd=1.10)
        yield ProviderTransaction("t2", "acc-eur", "2026-10-06", 30.0, "eur", "Received money",
                                  counterparty="Sam Lee", fx_rate_to_usd=1.10)
        yield ProviderTransaction("t3", "acc-x", "2026-10-07", 12.5, "USD", "DIVIDEND")

    def balances(self):
        return [ProviderBalance("acc-eur", 2400.0, "EUR"), ProviderBalance("acc-x", 5000.0, "USD")]

    def holdings(self):
        return [ProviderHolding("acc-x", "VTI", 10, 250.0, 2500.0, "Vanguard Total Stock Market")]
