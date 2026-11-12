"""`finance` CLI. Every command fails loudly; nothing here writes to any institution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as cfgmod
from .db import open_db
from .vault import load_budget_schema, load_goals


def _cfg(require_env: bool = True):
    return cfgmod.load(require_env=require_env)


def cmd_init(args):
    import shutil

    cfg = cfgmod.load(require_env=False, require_vault=False)
    for d in (cfg.vault_finance, cfg.goals_dir, cfg.reviews_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not cfg.budget_schema_path.exists():
        shutil.copy(cfg.root / "examples" / "Budget Schema.md", cfg.budget_schema_path)
        print(f"wrote a starter {cfg.budget_schema_path}: edit it, or run the finance-budget-schema-ingestion skill")
    conn = open_db(cfg.db_path)
    v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    print(f"db {cfg.db_path} at schema version {v}")
    try:
        s = load_budget_schema(cfg.budget_schema_path)
        print(f"budget schema ok: {len(s.categories)} categories, {len(s.accounts)} accounts, {len(s.waterfall)} waterfall steps")
        goals = load_goals(cfg.goals_dir)
        print(f"goals: {[g.title for g in goals]}")
    except Exception as e:
        print(f"budget schema: {e}")
    if not (cfg.root / ".env").exists():
        print("no .env yet: copy .env.example and fill in the Plaid credentials (plus any provider tokens)")


def cmd_status(args):
    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path, ro=True)
    print("Plaid items:")
    for r in conn.execute("SELECT institution, status, last_synced, last_error FROM plaid_items ORDER BY institution"):
        print(f"  {r['institution']:<12} {r['status']:<15} synced {r['last_synced'] or 'never'}  {r['last_error'] or ''}")
    print("Accounts:")
    for r in conn.execute("SELECT id, institution, type, currency, name FROM accounts ORDER BY institution, id"):
        flag = "  (UNMAPPED)" if ":" in r["id"] else ""
        print(f"  {r['id']:<40} {r['institution']:<10} {r['type']:<10} {r['currency']}  {r['name'] or ''}{flag}")
    n = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM transactions").fetchone()
    print(f"Transactions: {n[0]} ({n[1]} .. {n[2]})")
    print("Last runs:")
    for r in conn.execute("SELECT source, started, added, modified, removed, error FROM sync_runs ORDER BY id DESC LIMIT 8"):
        print(f"  {r['started']} {r['source']:<24} +{r['added']} ~{r['modified']} -{r['removed']}  {r['error'] or ''}")


def cmd_sync(args):
    from . import providers
    from .providers import sync as provider_sync

    cfg = _cfg()
    conn = open_db(cfg.db_path)
    names = list(providers.configured(cfg))
    if args.source not in ("plaid", "all", *names):
        raise SystemExit(f"unknown source {args.source!r}: plaid, all, or a [providers.<name>] from config.toml ({names or 'none configured'})")
    if args.source in ("plaid", "all"):
        from .ingest import plaid_sync

        if args.source == "all" and not conn.execute("SELECT COUNT(*) FROM plaid_items").fetchone()[0]:
            print("plaid: skipped (no Items linked yet; run `finance app`)")
        else:
            res = plaid_sync.sync_all(conn, cfg, only=args.item)
            print("plaid:", json.dumps(res, indent=2))
    schema_ids = None
    try:
        schema_ids = set(load_budget_schema(cfg.budget_schema_path).account_ids)
    except Exception:
        pass  # no schema yet: provider accounts get provisional ids
    failed = []
    for name in names if args.source == "all" else [n for n in names if n == args.source]:
        try:
            p = providers.get(cfg, name)
            days = args.days or int(p.settings.get("days", 90))
            print(f"{name}:", json.dumps(provider_sync.sync(conn, p, days=days, schema_ids=schema_ids), indent=2))
        except Exception as e:
            if args.source != "all":
                raise
            failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        raise RuntimeError("; ".join(failed))


def cmd_snapshot(args):
    from . import snapshot

    cfg = _cfg()
    conn = open_db(cfg.db_path)
    print(snapshot.run(conn, cfg, notify=not args.no_notify))


def cmd_categorize(args):
    from .pipeline import categorize

    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path)
    schema = load_budget_schema(cfg.budget_schema_path)
    if args.action == "run":
        stats = categorize.run(conn, schema, cfg, rematch=args.rematch)
        print(json.dumps(stats, indent=2))
        n = stats["handoff"]["unknown_merchants"]
        if n:
            print(f"\n{n} unknown merchants written to {stats['handoff']['path']}. Run /finance-categorize in Claude Code, then `finance categorize apply`.")
    elif args.action == "apply":
        print(json.dumps(categorize.apply(conn, schema, cfg, Path(args.file) if args.file else None), indent=2))
    elif args.action == "correct":
        if args.txn is not None:
            if args.category is not None:
                raise SystemExit("with --txn, pass only the category: finance categorize correct --txn <id> <category>")
            categorize.correct_txn(conn, schema, args.txn, args.merchant, args.subcategory)
            print(f"transaction {args.txn} -> {args.merchant} (this transaction only)")
        else:
            if args.category is None:
                raise SystemExit("usage: finance categorize correct <merchant> <category> | --txn <id> <category>")
            n = categorize.correct(conn, schema, args.merchant, args.category, args.subcategory)
            print(f"{args.merchant!r} -> {args.category}: {n} transactions updated (user entry, permanent)")
    elif args.action == "transfer":
        categorize.mark_transfer(conn, args.txn)
        print(f"transaction {args.txn} -> internal transfer (hidden from spending and income)")
    elif args.action == "unclear":
        rows = categorize.unclear_queue(conn, cfg, args.month)
        print(f"{len(rows)} merchants in the unclear queue" + (f" for {args.month}" if args.month else ""))
        for r in rows:
            print(f"  {r['merchant_norm']:<40} n={r['n']:<3} ${r['total_usd']:<9} {r['category'] or '-':<22} {r['category_source'] or ''} {r['category_confidence'] or ''} {r['reasons'] or ''}")
        txns = categorize.unclear_transactions(conn, cfg, args.month)
        if txns:
            print(f"\n{len(txns)} transactions need a per-transaction category (finance categorize correct --txn <id> <category>):")
            for r in txns:
                tag = "payback" if r["is_reimbursement"] else ""
                print(f"  id={r['id']:<6} {r['date']} ${r['amount_usd']:<9} {r['merchant_raw'] or '':<40} {tag}")


def cmd_accounts(args):
    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path)
    if args.action == "list":
        for r in conn.execute("SELECT id, institution, type, currency, name FROM accounts ORDER BY institution, id"):
            print(f"  {r['id']:<40} {r['institution']:<10} {r['type']:<10} {r['currency']}  {r['name'] or ''}")
    elif args.action == "map":
        from .accounts import map_account

        schema = load_budget_schema(cfg.budget_schema_path)
        map_account(conn, schema, args.old_id, args.new_id)
        print(f"{args.old_id} -> {args.new_id}")


def cmd_link_sandbox(args):
    from .linking import register_item
    from .plaid_client import PlaidClient

    cfg = _cfg()
    client = PlaidClient(cfg)
    res = register_item(cfg, client, client.sandbox_public_token(args.institution_id), args.label)
    print(f"linked sandbox Item {res['item_id']} as {args.label!r}; accounts: {res['accounts']}")
    print("next: map each plaid:<id> to a schema id (`finance accounts map` or the Accounts page), then `finance sync plaid`")


def cmd_backfill(args):
    from .ingest import csv_backfill

    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path)
    schema = load_budget_schema(cfg.budget_schema_path)
    res = csv_backfill.run(conn, schema, args.institution, Path(args.file), args.account, dry_run=args.dry_run)
    print(json.dumps(res, indent=2))


def cmd_app(args):
    import uvicorn

    uvicorn.run("finance.app.main:app", host="127.0.0.1", port=args.port, reload=args.reload)


def cmd_schema(args):
    cfg = _cfg(require_env=False)
    s = load_budget_schema(cfg.budget_schema_path)
    print(f"base {s.base_currency}, pay {s.pay_schedule}, planned gross {s.planned_gross:.0f}, net {s.planned_net:.0f}")
    for c in s.categories:
        print(f"  {c.id:<28} {c.group:<10} {c.type:<11} {c.planned:>8.0f} {c.currency}" + (f"  -> {c.account}" if c.account else ""))
    print("waterfall:")
    for w in s.waterfall:
        print(f"  {w.step}. {w.rule}" + (f" (target {w.target:.0f})" if w.target else "") + (f" [{w.goal}]" if w.goal else ""))


def cmd_plan(args):
    from datetime import date

    from .planner import month as month_plan

    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path, ro=True)
    s = load_budget_schema(cfg.budget_schema_path)
    p = month_plan.build(conn, s, args.month or date.today().strftime("%Y-%m"), income_category=cfg.income_category)
    print(f"{p.month}  day {p.days_elapsed}/{p.days_in_month}  fx {p.fx_rates}")
    print(f"  {'category':<28} {'planned':>10} {'actual':>10} {'var':>10} {'used':>6}  notes")
    for l in p.lines:
        notes = []
        if l.paid is not None:
            notes.append("paid" if l.paid else "not yet paid")
        if l.projected_usd is not None:
            notes.append(f"pace {l.projected_usd:.2f}")
        if l.reimbursed_usd:
            notes.append(f"net of {l.reimbursed_usd:.2f} reimbursed")
        used = f"{l.pct_used:.0f}%" if l.pct_used is not None else "-"
        print(f"  {l.id:<28} {l.planned_usd:>10.2f} {l.actual_usd:>10.2f} {l.variance_usd:>10.2f} {used:>6}  {'; '.join(notes)}")
    print(f"income: planned net {p.planned_net_income:.2f}, observed {p.observed_income:.2f}, variance {p.income_variance:.2f}")
    print(json.dumps(p.totals, indent=2))


def cmd_forecast(args):
    from .planner import forecast, goals as goals_mod

    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path, ro=True)
    s = load_budget_schema(cfg.budget_schema_path)
    fc = forecast.build(conn, s, months=args.months, start=args.start)
    print(f"{'month':<8} {'income':>9} {'fixed':>9} {'variable':>9} {'alloc':>9} {'surplus':>9} {'hub':>10}")
    for m in fc.months:
        print(f"{m.month:<8} {m.income_usd:>9.2f} {m.fixed_usd:>9.2f} {m.variable_usd:>9.2f} {m.allocations_usd:>9.2f} {m.surplus_usd:>9.2f} {m.hub_balance_usd:>10.2f}")
    for b in fc.basis:
        if b.type == "variable":
            avg = f"{b.trailing_avg_usd:.2f}" if b.trailing_avg_usd is not None else "-"
            print(f"  {b.id:<24} planned {b.planned_usd:>8.2f}  trailing {avg:>8}  used {b.used}")
    wf = goals_mod.simulate(conn, s, fc)
    for w in wf:
        print(f"  waterfall {w.month}: " + ", ".join(f"{a} {v:+.2f}" for a, v in w.moves.items()) + f"; left in hub {w.unallocated_usd:.2f}")
    for g in goals_mod.project_goals(conn, s, load_goals(cfg.goals_dir), fc, wf=wf):
        if g.status == "not_trackable":
            print(f"  goal {g.title}: {g.reason}")
        else:
            print(f"  goal {g.title}: {g.current_usd:.2f}/{g.target_amount:.2f} -> {g.status}, projected {g.projected_month}"
                  f"{' (extrapolated)' if g.beyond_horizon else ''}, on_track={g.on_track}")


def cmd_review(args):
    from .planner import review

    cfg = _cfg(require_env=False)
    conn = open_db(cfg.db_path)
    s = load_budget_schema(cfg.budget_schema_path)
    path = review.write(conn, s, load_goals(cfg.goals_dir), args.month, cfg.reviews_dir, force=args.force,
                        months_ahead=args.months, income_category=cfg.income_category, unclear_threshold=cfg.unclear_threshold)
    print(f"wrote {path}. Run /finance-review-commentary {args.month} in Claude Code to fill ## Commentary.")


def cmd_install_schedule(args):
    import platform

    from . import schedule

    cfg = _cfg(require_env=False)
    if args.cron:
        print("\n".join(schedule.cron_lines(cfg)))
        return
    if platform.system() != "Darwin":
        raise SystemExit("launchd is macOS only; use `finance install-schedule --cron` and add the lines with `crontab -e`")
    if args.uninstall:
        for p in schedule.uninstall(cfg):
            print(f"removed {p}")
        return
    if args.dry_run:
        for lbl, data in schedule.render(cfg).items():
            print(f"# {lbl}.plist\n{data.decode()}")
        return
    for p in schedule.install(cfg):
        print(f"loaded {p}")
    print("logs: ~/Library/Logs/finance-sync.log, ~/Library/Logs/finance-snapshot.log")


def main(argv=None):
    p = argparse.ArgumentParser(prog="finance")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/migrate the db, validate the vault notes").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="items, accounts, counts, last runs").set_defaults(fn=cmd_status)
    sub.add_parser("schema", help="print the parsed Budget Schema").set_defaults(fn=cmd_schema)

    s = sub.add_parser("sync", help="pull transactions")
    s.add_argument("source", help="plaid, all, or the name of a [providers.<name>] table in config.toml")
    s.add_argument("--item", help="plaid: only this institution")
    s.add_argument("--days", type=int, help="providers: how many days back (default: the provider's `days`, else 90)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("snapshot", help="balances + holdings -> snapshots (the scheduled job)")
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("categorize")
    ss = s.add_subparsers(dest="action", required=True)
    r = ss.add_parser("run", help="matcher, normalize, rules, map; write handoff for unknowns")
    r.add_argument("--rematch", action="store_true", help="reset and redo transfer matching (after a backfill)")
    a = ss.add_parser("apply", help="apply work/categorized.json")
    a.add_argument("--file")
    c = ss.add_parser("correct", help="user correction: per merchant (permanent), or one transaction with --txn")
    c.add_argument("merchant", help="merchant_norm, or the category when --txn is given")
    c.add_argument("category", nargs="?")
    c.add_argument("--txn", type=int, help="correct only this transaction id (split paybacks, Zelle)")
    c.add_argument("--subcategory")
    t = ss.add_parser("transfer", help="mark one transaction as a move between own accounts")
    t.add_argument("--txn", type=int, required=True)
    u = ss.add_parser("unclear", help="list the unclear queue")
    u.add_argument("--month", help="YYYY-MM")
    s.set_defaults(fn=cmd_categorize)

    s = sub.add_parser("accounts")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    m = ss.add_parser("map", help="rename a provisional plaid:<id> or <provider>:<id> account to its schema id")
    m.add_argument("old_id")
    m.add_argument("new_id")
    s.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("link-sandbox", help="PLAID_ENV=sandbox only: link a test Item without opening Link")
    s.add_argument("--institution-id", default="ins_109508", help="sandbox institution (default: First Platypus Bank)")
    s.add_argument("--label", default="Sandbox Bank")
    s.set_defaults(fn=cmd_link_sandbox)

    from .ingest.csv_backfill import PARSERS

    s = sub.add_parser("backfill", help="one-time CSV import (optional adapters for history older than Plaid's window)")
    s.add_argument("institution", choices=sorted(PARSERS), help="CSV layout; 'generic' = Date, Description, signed Amount")
    s.add_argument("file")
    s.add_argument("--account", help="schema account id the file belongs to (required for all but wise, which uses wise_<currency>)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_backfill)

    s = sub.add_parser("app", help="run the Link + dashboard app on localhost")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("plan", help="month plan vs actual per schema category")
    s.add_argument("--month", help="YYYY-MM (default: this month)")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("forecast", help="cash-flow forecast, waterfall and goal projections")
    s.add_argument("--months", type=int, default=6)
    s.add_argument("--start", help="first forecast month YYYY-MM (default: next month)")
    s.set_defaults(fn=cmd_forecast)

    s = sub.add_parser("review", help="write Finance/Reviews/YYYY-MM.md (tables; Commentary left for Claude)")
    s.add_argument("month", help="YYYY-MM")
    s.add_argument("--force", action="store_true", help="overwrite an existing review")
    s.add_argument("--months", type=int, default=6, help="forecast horizon")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("install-schedule", help="install the daily sync + weekly snapshot jobs (launchd, or --cron)")
    s.add_argument("--dry-run", action="store_true", help="print the plists, change nothing")
    s.add_argument("--uninstall", action="store_true", help="unload and remove the jobs")
    s.add_argument("--cron", action="store_true", help="print crontab lines instead (Linux)")
    s.set_defaults(fn=cmd_install_schedule)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
