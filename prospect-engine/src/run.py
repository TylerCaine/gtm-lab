"""Command-line entry point.

    python -m src.run icp "description..." --out config/icp_new.json
    python -m src.run estimate --icp config/icp_default.json --accounts 25
    python -m src.run pipeline --icp config/icp_default.json --accounts 25 --budget 5 [--dry-run] [--mock] [--sheet ID]
    python -m src.run report --run RUN_ID [--mock]
    python -m src.run suppress add someone@example.com --reason "opted out"
"""

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src import db
from src.config import CONFIG_DIR, load_settings, resolve

log = logging.getLogger("prospect_engine")


# ---------- logging ----------

def setup_logging(settings: dict[str, Any], run_id: str | None = None) -> Path | None:
    """INFO to the console; DEBUG to data/logs/<run_id>.log when a run_id is given.

    Returns the log file path (or None). Safe to call more than once.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_prospect_engine", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.DEBUG)

    console = RichHandler(level=logging.INFO, show_path=False, markup=False)
    console._prospect_engine = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if run_id is None:
        return None
    log_dir = resolve(settings["paths"]["logs_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{run_id}.log"
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler._prospect_engine = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    return path


def db_path(settings: dict[str, Any], mock: bool) -> Path:
    """The real database, or the separate mock database (D5)."""
    return resolve(settings["paths"]["mock_db" if mock else "db"])


# ---------- commands ----------

def cmd_suppress(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Add an email or domain to the private suppression list (D9)."""
    conn = db.connect(db_path(settings, mock=False))
    try:
        value = db.add_suppression(conn, args.value, args.reason,
                                   csv_path=resolve(settings["paths"]["suppression_file"]))
    except ValueError as exc:
        Console().print(f"[red]Not added:[/red] {exc}")
        return 1
    finally:
        conn.close()
    Console().print(f"Suppressed [bold]{value}[/bold]. It will never appear in output.")
    return 0


def cmd_report(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Print a run's status and its cost breakdown per provider from api_calls."""
    console = Console()
    conn = db.connect(db_path(settings, mock=args.mock))
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            console.print(f"[red]No run with id {args.run}[/red]"
                          f"{' in the mock database' if args.mock else ''}.")
            return 1
        rows = conn.execute(
            """
            SELECT provider,
                   SUM(CASE WHEN cached = 0 AND error IS NULL THEN 1 ELSE 0 END) AS paid_calls,
                   SUM(cached) AS cache_hits,
                   SUM(CASE WHEN error = 'mock' THEN 1 ELSE 0 END) AS mock_calls,
                   SUM(CASE WHEN error LIKE 'refused:%' THEN 1 ELSE 0 END) AS refused,
                   SUM(CASE WHEN error IS NOT NULL AND error != 'mock'
                            AND error NOT LIKE 'refused:%' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN cached = 0 THEN credits ELSE 0 END) AS credits,
                   SUM(CASE WHEN cached = 0 THEN est_cost_usd ELSE 0 END) AS usd
            FROM api_calls WHERE run_id = ? GROUP BY provider ORDER BY provider
            """,
            (args.run,),
        ).fetchall()
    finally:
        conn.close()

    console.print(f"Run [bold]{run['run_id']}[/bold]  status={run['status']}  "
                  f"started={run['started_at']}  finished={run['finished_at'] or '-'}  "
                  f"budget=${run['budget_usd'] or 0:.2f}{'  (mock)' if run['mock'] else ''}")
    table = Table(title="API calls by provider")
    for col in ("provider", "paid calls", "cache hits", "mock", "refused", "failed",
                "credits", "cost $"):
        table.add_column(col, justify="left" if col == "provider" else "right")
    total = 0.0
    for r in rows:
        total += r["usd"] or 0
        table.add_row(r["provider"], str(r["paid_calls"]), str(r["cache_hits"]),
                      str(r["mock_calls"]), str(r["refused"]), str(r["failed"]),
                      f"{r['credits'] or 0:g}", f"{r['usd'] or 0:.4f}")
    console.print(table)
    console.print(f"Total spend: [bold]${total:.4f}[/bold]")
    return 0


def print_estimate(est, console: Console) -> None:
    """Show the estimate table, totals and warnings."""
    table = Table(title=f"Estimate for {est.target_accounts} accounts "
                        f"({est.candidates} candidates qualified)")
    numeric = ("max calls", "max credits", "typical $", "worst $")
    for col in ("provider", "endpoint", *numeric, "note"):
        table.add_column(col, justify="right" if col in numeric else "left")
    for line in est.lines:
        table.add_row(line.provider, line.endpoint, str(line.calls), f"{line.credits:g}",
                      f"{line.usd_typical:.2f}", f"{line.usd_worst:.2f}", line.note)
    console.print(table)
    console.print(f"Total: typical [bold]${est.usd_typical:.2f}[/bold], worst case "
                  f"[bold]${est.usd_worst:.2f}[/bold]. Run budget ${est.budget_usd:.2f}; "
                  f"spent this month ${est.month_spent_usd:.2f} of ${est.month_cap_usd:.2f}.")
    console.print("Free-plan providers cost $0; their limit is credits (see max credits).")
    for w in est.warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")


def print_keys(console: Console) -> list[str]:
    """Show which keys are set (never their values). Returns missing required keys."""
    from src.estimate import key_status
    table = Table(title="API keys in .env (values never shown)")
    for col in ("variable", "status", "used for"):
        table.add_column(col)
    missing = []
    for name, required, is_set, purpose in key_status():
        status = "set" if is_set else ("MISSING" if required else "not set (optional)")
        if required and not is_set:
            missing.append(name)
        table.add_row(name, status, purpose)
    console.print(table)
    return missing


def print_balances(settings: dict[str, Any], console: Console) -> None:
    """Free live balance checks: Prospeo /account-information and Hunter /account."""
    from src.providers import hunter, prospeo
    from src.providers.base import CallContext, MissingApiKey, ProviderError
    conn = db.connect(db_path(settings, mock=False))
    db.init_schema(conn)
    ctx = CallContext(conn=conn, settings=settings)
    try:
        try:
            d = prospeo.account_information(ctx)
            console.print(f"Prospeo: {d.get('remaining_credits')} credits left "
                          f"(plan {d.get('current_plan')}), renews "
                          f"{str(d.get('next_quota_renewal_date') or '')[:10]}")
        except (MissingApiKey, ProviderError) as exc:
            console.print(f"Prospeo: [yellow]could not check[/yellow] ({exc})")
        try:
            reqs = hunter.account(ctx).get("requests") or {}
            left = {k: v.get("available") for k, v in reqs.items() if isinstance(v, dict)}
            console.print(f"Hunter: available this month {left}")
        except (MissingApiKey, ProviderError) as exc:
            console.print(f"Hunter: [yellow]could not check[/yellow] ({exc})")
    finally:
        conn.close()


def cmd_estimate(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Print calls, credits and cost for a run. Spends nothing."""
    from src.config import load_icp
    from src.estimate import estimate_run
    console = Console()
    icp = load_icp(args.icp)
    conn = db.connect(db_path(settings, mock=False))
    db.init_schema(conn)
    try:
        est = estimate_run(conn, icp, settings, args.accounts or icp.target_accounts, args.budget)
    finally:
        conn.close()
    print_estimate(est, console)
    print_keys(console)
    if args.balances:
        print_balances(settings, console)
    return 0


def confirm(prompt: str) -> bool:
    """Ask the user; only an exact 'yes' counts."""
    try:
        return input(prompt).strip().lower() == "yes"
    except EOFError:
        return False


def cmd_pipeline(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Run the pipeline: --mock (fixtures, $0), --dry-run (discovery only) or live."""
    from src.config import load_icp, load_scoring
    from src.estimate import estimate_run
    from src.pipeline import load_suppression, run_pipeline
    from src.providers.base import CallContext

    console = Console()
    icp = load_icp(args.icp)
    scoring = load_scoring()
    target = args.accounts or icp.target_accounts
    budget = args.budget if args.budget is not None else \
        float(settings["budget"]["run_default_usd"])

    path = db_path(settings, mock=args.mock)
    if args.mock and path.exists():
        path.unlink()   # mock runs start from a clean mock database (D5); real DB untouched
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        if not args.mock:
            # Spec rule 11: show the estimate, then spend only after an explicit "yes".
            print_estimate(estimate_run(conn, icp, settings, target, budget), console)
            missing = print_keys(console)
            if missing:
                console.print(f"[red]Stopped:[/red] add {', '.join(missing)} to .env first. "
                              "Nothing was called or spent.")
                return 1
            what = ("discovery only (TheirStack + Prospeo company search)" if args.dry_run
                    else "the full pipeline")
            if not confirm(f"Run {what} with a ${budget:.2f} budget? Type yes to continue: "):
                console.print("Cancelled. Nothing was called or spent.")
                return 1

        load_suppression(conn, settings)
        run_id = db.new_run_id()
        log_file = setup_logging(settings, run_id)
        db.create_run(conn, run_id, icp.name, budget, mock=args.mock,
                      notes="dry-run" if args.dry_run else None)
        ctx = CallContext(conn=conn, settings=settings, run_id=run_id, budget_usd=budget,
                          mock=args.mock)
        try:
            if args.dry_run:
                return _dry_run(ctx, icp, settings, scoring, target, budget, console)
            res = run_pipeline(ctx, icp, settings, scoring, target)
        except BaseException:
            db.finish_run(conn, run_id, "failed")
            raise
        db.finish_run(conn, run_id, "done")
    finally:
        conn.close()

    kind = "Mock run" if args.mock else "Run"
    console.print(f"\n[bold green]{kind} {run_id} finished[/bold green]"
                  + (" (no network, $0)" if args.mock else f" (spent ${res.spent_usd:.4f})"))
    if not args.mock:
        console.print(f"Cost breakdown: python -m src.run report --run {run_id}")
    console.print(f"Candidates: {res.candidates}  Tiers: {res.tiers}  Contacts: {res.contacts}  "
                  f"Emails: {res.emails}")
    console.print(f"Prospect rows: {res.rows}  Needs review: {res.needs_review}")
    for name, p in res.paths.items():
        console.print(f"  {name:13} {p}")
    if log_file:
        console.print(f"  {'log':13} {log_file}")
    return 0


def _dry_run(ctx, icp, settings, scoring, target, budget, console: Console) -> int:
    """Discovery only, then what the later stages would cost for the companies found."""
    from src.discover import discover
    from src.estimate import estimate_run
    disc = discover(ctx, icp, settings, scoring, target)
    db.finish_run(ctx.conn, ctx.run_id, "done")
    console.print(f"\n[bold]Dry run {ctx.run_id}:[/bold] {disc.hiring_companies} hiring "
                  f"companies, {len(disc.candidates)} passed hard filters, "
                  f"{len(disc.excluded)} excluded. "
                  f"Spent ${db.run_spend(ctx.conn, ctx.run_id):.2f}.")
    for domain, reason in sorted(disc.excluded.items()):
        console.print(f"  excluded {domain}: {reason}")
    multiplier = int((settings.get("discover") or {}).get("candidate_multiplier", 2))
    later = estimate_run(ctx.conn, icp, settings,
                         max(1, math.ceil(len(disc.candidates) / multiplier)), budget)
    later.lines = [line for line in later.lines
                   if (line.provider, line.endpoint) not in
                   {("theirstack", "jobs/search"), ("prospeo", "search-company")}]
    console.print("Later stages for these candidates would use at most:")
    print_estimate(later, console)
    return 0


def cmd_not_built(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Placeholder for commands whose stages are not written yet."""
    Console().print(f"[yellow]'{args.command}' is not built yet[/yellow] "
                    "(arrives in build step 3/4). Nothing was called or spent.")
    return 2


# ---------- argument parsing ----------

def build_parser() -> argparse.ArgumentParser:
    """Define all commands and options."""
    parser = argparse.ArgumentParser(prog="python -m src.run",
                                     description="Prospect Engine: ICP -> scored prospect list.")
    parser.add_argument("--settings", default=str(CONFIG_DIR / "settings.yaml"),
                        help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("icp", help="turn a plain-English description into an ICP JSON file")
    p.add_argument("description")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_not_built)

    default_icp = str(CONFIG_DIR / "icp_default.json")
    p = sub.add_parser("estimate", help="estimate calls and cost for a run, without spending")
    p.add_argument("--icp", default=default_icp)
    p.add_argument("--accounts", type=int, help="target accounts (default: from the ICP)")
    p.add_argument("--budget", type=float, help="run budget to compare against")
    p.add_argument("--balances", action="store_true",
                   help="also check live Prospeo/Hunter credit balances (free; needs keys)")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("pipeline", help="run the full pipeline")
    p.add_argument("--icp", default=default_icp)
    p.add_argument("--accounts", type=int, help="target accounts (default: from the ICP)")
    p.add_argument("--budget", type=float, help="max dollars for this run (default from settings)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="fixtures only: no network, $0")
    mode.add_argument("--dry-run", action="store_true",
                      help="real discovery only; print what later stages would cost")
    p.add_argument("--sheet", help="also write to this Google Sheet ID")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("report", help="summary and cost breakdown of a run")
    p.add_argument("--run", required=True)
    p.add_argument("--mock", action="store_true", help="read the mock database")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("suppress", help="manage the suppression list")
    ssub = p.add_subparsers(dest="action", required=True)
    pa = ssub.add_parser("add", help="never output this email or domain")
    pa.add_argument("value")
    pa.add_argument("--reason")
    pa.set_defaults(func=cmd_suppress)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load .env and settings, and run the chosen command."""
    load_dotenv()  # makes keys in .env available via os.environ; values are never printed
    args = build_parser().parse_args(argv)
    settings = load_settings(args.settings)
    setup_logging(settings)
    return int(args.func(args, settings))


if __name__ == "__main__":
    sys.exit(main())
