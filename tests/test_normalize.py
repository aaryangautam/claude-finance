import pytest

from finance.pipeline.normalize import normalize


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SQ *BLUE BOTTLE COFFEE #1234 SAN FRANCISCO CA", "Blue Bottle Coffee"),
        ("TST* THE SMITH - MIDTOWN NEW YORK NY", "The Smith - Midtown"),
        ("AMAZON.COM*2K4LM9X03 AMZN.COM/BILL WA", "Amazon"),
        ("REWE SAGT DANKE. 10115 Berlin DEU", "Rewe Sagt Danke"),
        ("EDEKA HAUSMANN BERLIN DE", "Edeka Hausmann"),
        ("WHOLE FOODS MARKET AUSTIN TX", "Whole Foods Market"),
        ("7-ELEVEN 34211 LOS ANGELES CA", "7-Eleven"),
        ("Netflix.com", "Netflix"),
        ("SHELL OIL 57444126100 FRANKFURT DEU", "Shell Oil"),
        ("ACME CORP PAYROLL 240815", "Acme Corp Payroll"),
        ("AMEX EPAYMENT ACH PMT 240901 W1234", "Amex Epayment Ach Pmt"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_stable():
    a = "SQ *BLUE BOTTLE COFFEE #1234 SAN FRANCISCO CA"
    b = "SQ *BLUE BOTTLE COFFEE #9876 OAKLAND CA"
    assert normalize(a) == normalize(b)
