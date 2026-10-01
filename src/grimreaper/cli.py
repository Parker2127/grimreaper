"""grimreaper scan | investigate | watch | reap"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from . import __version__, costs, scanners, watch
from .agent import ReaperContext, explain_changes, investigate
from .models import ReapingReport, Resource
from .reaper import reap
from .runtime import AgentRunError
from .safety import protection_reason

console = Console()
DEFAULT_REPORT = Path(".grimreaper/report.json")
ACTION_STYLE = {"delete": "bold red", "review": "yellow", "keep": "green"}


def _session(args) -> boto3.Session:
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def _regions(args, session: boto3.Session) -> list[str]:
    return args.regions.split(",") if args.regions else scanners.enabled_regions(session)


def _whoami(session: boto3.Session) -> None:
    ident = session.client("sts").get_caller_identity()
    console.print(f"[dim]account {ident['Account']} as {ident['Arn']}[/dim]")


# ---------------- scan (no AI) ----------------

def cmd_scan(args) -> int:
    session = _session(args)
    _whoami(session)
    regions = _regions(args, session)
    with console.status(f"Scanning {len(regions)} regions..."):
        errors: list[str] = []
        found = scanners.scan_all(session, regions, on_error=errors.append)
        spend = costs.cost_by_service(session, 30)

    if args.json:
        print(json.dumps([r.to_dict() | {"key": r.key, "protected": protection_reason(r)} for r in found], indent=2, default=str))
        return 0

    table = Table(title="What's billing (last 30 days, credits excluded)")
    for col in ("Service", "Region", "USD"):
        table.add_column(col, justify="right" if col == "USD" else "left")
    for row in spend[:15]:
        table.add_row(row["service"], row["region"], f"{row['usd']:.2f}")
    console.print(table)

    table = Table(title=f"{len(found)} billable resources found")
    for col in ("Kind", "Region", "ID / Name", "Est. $/mo", "Detail", "Protected"):
        table.add_column(col, overflow="fold")
    for r in found:
        label = r.name if r.name and r.name != r.id else r.id
        table.add_row(r.kind, r.region, label, f"{r.monthly_cost:.2f}", r.detail, protection_reason(r) or "")
    console.print(table)
    console.print(f"Estimated total: [bold]${sum(r.monthly_cost for r in found):.2f}/month[/bold] (list prices, rough)")
    for e in errors[:5]:
        console.print(f"[dim]skipped: {e}[/dim]")
    return 0


# ---------------- investigate (AI) ----------------

def _openai_client():
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[red]OPENAI_API_KEY is not set.[/red] `grimreaper scan` works without it.")
        return None
    from openai import OpenAI

    return OpenAI()


def _progress(status, stats: dict):
    def update(p) -> None:
        stats["last"] = p
        status.update(f"The agent is investigating: {p.tool_calls} tool call(s), {p.seconds:.0f}s"
                      + (f", last: {p.last_tool}" if p.last_tool else ""))
    return update


def _print_run_stats(stats: dict) -> None:
    p = stats.get("last")
    if p is None:
        return
    tools = ", ".join(f"{name} x{n}" for name, n in p.tools_used.most_common())
    console.print(f"[dim]agent: {p.tool_calls} local tool call(s) in {p.seconds:.0f}s: {tools}[/dim]")


def _save_report(path: Path, report: ReapingReport, inventory: dict[str, Resource]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "report": report.model_dump(),
        "inventory": {k: r.to_dict() for k, r in inventory.items()},
    }, indent=2, default=str))


def cmd_investigate(args) -> int:
    client = _openai_client()
    if client is None:
        return 2
    session = _session(args)
    _whoami(session)
    ctx = ReaperContext(session=session, regions=_regions(args, session))
    stats: dict = {}
    with console.status("The agent is starting...") as status:
        report, dropped = investigate(client, ctx, args.model, on_progress=_progress(status, stats))

    _print_report(report, ctx.inventory)
    _print_run_stats(stats)
    for key in dropped:
        console.print(f"[dim]ignored verdict for unknown resource {key}[/dim]")
    _save_report(args.out, report, ctx.inventory)
    console.print(f"\nSaved to {args.out}. Next: [bold]grimreaper reap[/bold] (dry run) or [bold]grimreaper reap --execute[/bold]")
    return 0


# ---------------- watch (always-on) ----------------

def cmd_watch(args) -> int:
    session = _session(args)
    _whoami(session)
    ctx = ReaperContext(session=session, regions=_regions(args, session))
    with console.status("Checking for changes..."):
        inventory = ctx.ensure_inventory()
        cost_view = watch.current_costs(session)
    changes = watch.diff(watch.load_state(args.state), inventory, cost_view)

    if watch.is_quiet(changes):
        console.print(f"All quiet: no new resources or charges. Spend ${changes['usd_per_day_now']:.2f}/day.")
        watch.save_state(args.state, inventory, cost_view)
        return 0

    console.print(f"[yellow]{len(changes['new_resources'])} new resource(s), "
                  f"{len(changes['new_or_rising_charges'])} new or rising charge(s).[/yellow] Waking the agent...")
    client = _openai_client()
    if client is None:
        return 2
    stats: dict = {}
    with console.status("The agent is working...") as status:
        report, _ = explain_changes(client, ctx, changes, args.model, on_progress=_progress(status, stats))
    _print_report(report, inventory)
    _print_run_stats(stats)
    _save_report(args.out, report, inventory)

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        watch.post_to_slack(webhook, watch.slack_message(report, changes))
        console.print("Posted to Slack.")
    watch.save_state(args.state, inventory, cost_view)

    flagged = any(v.action != "keep" for v in report.verdicts)
    return 3 if flagged and args.fail_on_findings else 0


def _print_report(report: ReapingReport, inventory: dict[str, Resource]) -> None:
    console.rule("[bold]GrimReaper report")
    console.print(report.summary)
    console.print(f"\nObserved usage spend, last 30 days: [bold]${report.observed_monthly_spend:.2f}[/bold]\n")
    table = Table()
    for col in ("Action", "Resource", "Region", "Save $/mo", "Why"):
        table.add_column(col, overflow="fold")
    for v in report.verdicts:
        r = inventory[v.resource_key]
        table.add_row(
            f"[{ACTION_STYLE[v.action]}]{v.action}[/]", f"{r.kind} {r.name or r.id}", r.region,
            f"{v.est_monthly_savings:.2f}", v.reason,
        )
    console.print(table)
    savings = sum(v.est_monthly_savings for v in report.verdicts if v.action == "delete")
    console.print(f"Reaping everything marked delete saves about [bold]${savings:.2f}/month[/bold].")


# ---------------- reap (deterministic, human-approved) ----------------

def cmd_reap(args) -> int:
    if not args.report.exists():
        console.print(f"[red]No report at {args.report}.[/red] Run `grimreaper investigate` first.")
        return 2
    data = json.loads(args.report.read_text())
    report = ReapingReport.model_validate(data["report"])
    inventory = {k: Resource.from_dict(v) for k, v in data["inventory"].items()}
    targets = [(v, inventory[v.resource_key]) for v in report.verdicts if v.action == "delete" and v.resource_key in inventory]

    if not targets:
        console.print("Nothing marked for deletion.")
        return 0

    session = _session(args)
    _whoami(session)
    mode = "[bold red]EXECUTE[/bold red]" if args.execute else "[bold]DRY RUN[/bold] (add --execute to delete)"
    console.print(f"Mode: {mode}\n")

    failures = 0
    for verdict, resource in targets:
        console.print(f"[bold]{resource.kind}[/bold] {resource.name or resource.id} ({resource.region}), ~${verdict.est_monthly_savings:.2f}/mo")
        console.print(f"  [dim]{verdict.reason}[/dim]")
        if reason := protection_reason(resource):
            console.print(f"  [green]skipped, protected: {reason}[/green]")
            continue
        if not args.execute:
            console.print("  [yellow]would delete[/yellow]")
            continue
        if not Confirm.ask("  Reap it?", default=False):
            console.print("  skipped")
            continue
        try:
            console.print(f"  [red]{reap(session, resource)}[/red]")
        except Exception as e:  # keep going; report every failure
            failures += 1
            console.print(f"  [bold red]failed:[/bold red] {e}")
    return 1 if failures else 0


# ---------------- entrypoint ----------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grimreaper", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="AWS profile to use")
    common.add_argument("--region", default="us-east-1", help="home region for API calls")
    common.add_argument("--regions", help="comma-separated regions to scan (default: all enabled)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", parents=[common], help="inventory billable resources (no AI, read-only)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("investigate", parents=[common], help="let the agent explain your bill (read-only)")
    p.add_argument("--model", help="OpenAI model (default: gpt-6-astra)")
    p.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    p.set_defaults(func=cmd_investigate)

    p = sub.add_parser("watch", parents=[common], help="always-on mode: only wakes the agent when something changed")
    p.add_argument("--model", help="OpenAI model (default: gpt-6-astra)")
    p.add_argument("--state", type=Path, default=Path(".grimreaper/state.json"))
    p.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--fail-on-findings", action="store_true", help="exit 3 when something needs attention (for CI alerts)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("reap", parents=[common], help="delete what the report marked, with per-item approval")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--execute", action="store_true", help="actually delete (default is a dry run)")
    p.set_defaults(func=cmd_reap)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AgentRunError as e:
        console.print(f"[bold red]The agent couldn't finish:[/bold red] {e}")
        return 1
    except KeyboardInterrupt:
        console.print("\naborted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
