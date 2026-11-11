from datetime import date

import frontmatter
import pytest

from finance import cli
from finance.planner import review
from tests.conftest import add_snapshot, add_tx, seed_month

TODAY = date(2026, 11, 5)


def _data(conn, ef=10400.0):
    for m in ("2026-08", "2026-09", "2026-10"):
        seed_month(conn, m)
    add_snapshot(conn, "checking", "2026-10-31", 3000)
    add_snapshot(conn, "savings", "2026-10-31", ef)


def test_review_writes_note_and_log(conn, schema, goals, cfg):
    _data(conn)
    path = review.write(conn, schema, goals, "2026-10", cfg.reviews_dir, today=TODAY)
    assert path == cfg.vault_finance / "Reviews" / "2026-10.md"
    post = frontmatter.load(path)
    assert post["month"] == "2026-10" and post["generated_at"]
    assert post["totals"]["observed_income_usd"] == 5900
    assert post["totals"]["actual_allocations_usd"] == 1583
    body = post.content
    for h in ("## Plan vs actual", "## Income", "## Waterfall check", "## Forecast", "## Goals"):
        assert h in body
    assert body.rstrip().endswith("## Commentary")
    assert "| `housing_rent` | fixed | $1,650.00 (1,500 EUR) | $1,650.00 |" in body
    assert "ended at $10,400.00 (snapshot 2026-10-31), target $10,000.00, so $400.00 should have moved to brokerage" in body
    assert "$1,400.00 expected; observed: $1,000.00" in body
    assert "not yet trackable" in body  # Roth IRA goal, null progress_source
    row = conn.execute("SELECT * FROM review_log WHERE month='2026-10'").fetchone()
    assert row["path"] == str(path) and '"observed_income_usd": 5900' in row["totals_json"]


def test_review_refuses_overwrite(conn, schema, goals, cfg):
    _data(conn)
    review.write(conn, schema, goals, "2026-10", cfg.reviews_dir, today=TODAY)
    with pytest.raises(FileExistsError, match="--force"):
        review.write(conn, schema, goals, "2026-10", cfg.reviews_dir, today=TODAY)
    review.write(conn, schema, goals, "2026-10", cfg.reviews_dir, today=TODAY, force=True)


def test_review_below_target_and_requires_snapshot(conn, schema, goals, cfg):
    seed_month(conn, "2026-10")
    with pytest.raises(Exception, match="snapshot"):
        review.write(conn, schema, goals, "2026-10", cfg.reviews_dir, today=TODAY)
    assert not (cfg.reviews_dir / "2026-10.md").exists()
    add_snapshot(conn, "checking", "2026-10-31", 3000)
    add_snapshot(conn, "savings", "2026-10-31", 9500)
    text, _ = review.render(conn, schema, goals, "2026-10", today=TODAY)
    assert "$500.00 short of the $10,000.00 target" in text


def test_cli_review(conn, schema, goals, cfg, monkeypatch, capsys):
    _data(conn)
    conn.close()
    monkeypatch.setattr(cli, "_cfg", lambda require_env=True: cfg)
    cli.main(["review", "2026-10"])
    assert (cfg.reviews_dir / "2026-10.md").exists()
    with pytest.raises(SystemExit):
        cli.main(["review", "2026-10"])
    assert "--force" in capsys.readouterr().err
    cli.main(["review", "2026-10", "--force"])
    cli.main(["plan", "--month", "2026-10"])
    out = capsys.readouterr().out
    assert "housing_rent" in out and "observed 5900.00" in out


def test_cli_forecast(conn, schema, goals, cfg, monkeypatch, capsys):
    _data(conn)
    conn.close()
    monkeypatch.setattr(cli, "_cfg", lambda require_env=True: cfg)
    cli.main(["forecast", "--start", "2026-12", "--months", "3"])
    out = capsys.readouterr().out
    assert "2027-02" in out and "goal Emergency Fund" in out and "not yet trackable" in out
