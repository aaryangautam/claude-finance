"""`finance install-schedule`: the two scheduled jobs, rendered from config.toml [schedule] for this checkout.

  sync      daily: `finance sync all && finance categorize run` (no LLM; unknown merchants wait for /finance-categorize)
  snapshot  weekly: balances + holdings -> snapshots

macOS: writes LaunchAgents and loads them with launchctl. Elsewhere: `--cron` prints equivalent crontab lines.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import Config, ConfigError

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
LOG_DIR = Path("~/Library/Logs").expanduser()


def _uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise ConfigError("uv not found on PATH; install it (https://docs.astral.sh/uv/) before scheduling")
    return uv


def _jobs(cfg: Config) -> dict[str, dict]:
    s = cfg.schedule
    uv, root = _uv(), str(cfg.root)
    run = f"{shlex.quote(uv)} run --directory {shlex.quote(root)} finance"
    return {
        "sync": dict(cmd=f"{run} sync all && {run} categorize run",
                     when={"Hour": int(s.get("sync_hour", 6)), "Minute": int(s.get("sync_minute", 30))}),
        "snapshot": dict(cmd=f"{run} snapshot",
                         when={"Weekday": int(s.get("snapshot_weekday", 0)), "Hour": int(s.get("snapshot_hour", 9)),
                               "Minute": int(s.get("snapshot_minute", 0))}),
    }


def label(cfg: Config, job: str) -> str:
    return f"{cfg.schedule.get('label_prefix', 'local.finance')}.{job}"


def render(cfg: Config) -> dict[str, bytes]:
    """{label: plist bytes}. launchd runs a missed StartCalendarInterval on the next wake."""
    path_env = os.pathsep.join(dict.fromkeys([str(Path(_uv()).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    out = {}
    for job, j in _jobs(cfg).items():
        lbl = label(cfg, job)
        log = str(LOG_DIR / f"finance-{job}.log")
        out[lbl] = plistlib.dumps({
            "Label": lbl,
            "ProgramArguments": ["/bin/sh", "-c", j["cmd"]],
            "WorkingDirectory": str(cfg.root),
            "StartCalendarInterval": j["when"],
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "EnvironmentVariables": {"PATH": path_env},
        })
    return out


def cron_lines(cfg: Config) -> list[str]:
    lines = []
    for job, j in _jobs(cfg).items():
        w = j["when"]
        dow = str(w["Weekday"]) if "Weekday" in w else "*"
        lines.append(f"{w['Minute']} {w['Hour']} * * {dow} {j['cmd']} >> {shlex.quote(str(Path('~/finance-' + job + '.log').expanduser()))} 2>&1")
    return lines


def install(cfg: Config, dry_run: bool = False) -> list[Path]:
    uid = os.getuid()
    written = []
    for lbl, data in render(cfg).items():
        path = LAUNCH_AGENTS / f"{lbl}.plist"
        written.append(path)
        if dry_run:
            continue
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)  # not loaded yet is fine
        path.write_bytes(data)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    return written


def uninstall(cfg: Config) -> list[Path]:
    uid = os.getuid()
    removed = []
    for lbl in render(cfg):
        path = LAUNCH_AGENTS / f"{lbl}.plist"
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
            path.unlink()
            removed.append(path)
    return removed
