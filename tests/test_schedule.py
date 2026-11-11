import plistlib

from finance import schedule


def test_plists_point_at_this_checkout(cfg):
    out = schedule.render(cfg)
    assert set(out) == {"local.finance.sync", "local.finance.snapshot"}
    sync = plistlib.loads(out["local.finance.sync"])
    assert sync["WorkingDirectory"] == str(cfg.root)
    assert "sync all &&" in sync["ProgramArguments"][2] and "categorize run" in sync["ProgramArguments"][2]
    assert sync["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    snap = plistlib.loads(out["local.finance.snapshot"])
    assert snap["StartCalendarInterval"] == {"Weekday": 0, "Hour": 9, "Minute": 0}


def test_cron_lines(cfg):
    lines = schedule.cron_lines(cfg)
    assert lines[0].startswith("30 6 * * * ") and lines[1].startswith("0 9 * * 0 ")
