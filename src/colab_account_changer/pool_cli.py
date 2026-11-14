"""``colab-account pool`` / ``colab-pool``: name-addressed Colab sessions."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from colab_account_changer.pool import (
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_LOCK_WARN_AFTER,
    SESSION_COMMANDS,
    ColabPool,
    snapshot_json,
    snapshot_usages,
)
from colab_account_changer.usage import format_eta, total_usage

console = Console()
error_console = Console(stderr=True)
pool_app = typer.Typer(
    name="pool",
    help=(
        "Use Colab sessions by name across a pool of saved accounts. Every "
        "command switches to the session's owning account under a global lock."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

PASSTHROUGH_SETTINGS = {
    "ignore_unknown_options": True,
    "allow_interspersed_args": False,
}
SessionArgument = Annotated[str, typer.Argument(help="Pooled session name.")]
ExtraArguments = Annotated[
    list[str] | None,
    typer.Argument(help="Arguments passed through to Colab CLI unchanged."),
]
CachedOption = Annotated[
    bool,
    typer.Option("--cached", help="Use cached usage instead of refreshing it."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON.")]


def _notify(message: str) -> None:
    error_console.print(Text(f"[pool] {message}", style="dim"))


@pool_app.callback()
def pool_root(
    ctx: typer.Context,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            envvar="COLAB_POOL_OFFLINE",
            help="Switch accounts without online token identity checks.",
        ),
    ] = False,
    colab_command: Annotated[
        str,
        typer.Option(
            "--colab-command",
            envvar="COLAB_POOL_COLAB_COMMAND",
            help="Colab CLI executable to run.",
        ),
    ] = "colab",
    lock_timeout: Annotated[
        float,
        typer.Option(
            "--lock-timeout",
            envvar="COLAB_POOL_LOCK_TIMEOUT",
            min=0,
            help="Seconds to wait for the account lock (exit code 75 on timeout).",
        ),
    ] = DEFAULT_LOCK_TIMEOUT,
    lock_warn_after: Annotated[
        float,
        typer.Option(
            "--lock-warn-after",
            envvar="COLAB_POOL_LOCK_WARN_AFTER",
            min=0,
            help="Warn once on stderr after holding the lock this long (0: never).",
        ),
    ] = DEFAULT_LOCK_WARN_AFTER,
) -> None:
    """Use Colab sessions by name; accounts are chosen and switched for you."""
    ctx.obj = ColabPool(
        colab_command=colab_command,
        verify=not offline,
        lock_timeout=lock_timeout,
        lock_warn_after=lock_warn_after,
        notify=_notify,
    )


def _pool(ctx: typer.Context) -> ColabPool:
    return ctx.obj


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


@pool_app.command(context_settings={"ignore_unknown_options": True})
def new(
    ctx: typer.Context,
    name: SessionArgument,
    args: ExtraArguments = None,
    account: Annotated[
        str | None,
        typer.Option("--account", help="Create on this account instead of choosing."),
    ] = None,
    cached: CachedOption = False,
    if_missing: Annotated[
        bool,
        typer.Option(
            "--if-missing",
            help=(
                "If NAME is registered and still on the server, do nothing "
                "(exit 0); if it is confirmed gone, clean it up and recreate it."
            ),
        ),
    ] = False,
) -> None:
    """Create a session on the pool's account and register it.

    Unknown options are passed to [cyan]colab new[/cyan], e.g.
    [cyan]pool new node-x --gpu g4[/cyan]; put them after [cyan]--[/cyan] to
    be explicit. Creation is refused at the limits shown by
    [cyan]pool limits[/cyan].
    """
    session, selection = _pool(ctx).create(
        name, args or [], account=account, refresh=not cached, if_missing=if_missing
    )
    if selection.existing:
        console.print(
            f"{escape(session.name)} already exists on account "
            f"{escape(session.account)} ({escape(session.hardware)}, endpoint "
            f"{escape(session.endpoint or 'unknown')}); not creating another."
        )
        return
    _notify(
        f"{session.name} created on account {selection.account} "
        f"({session.hardware}, endpoint {session.endpoint})"
    )


@pool_app.command()
def stop(
    ctx: typer.Context,
    name: SessionArgument,
    forget: Annotated[
        bool,
        typer.Option("--forget", help="Only deregister; do not contact Colab."),
    ] = False,
) -> None:
    """Stop a session and remove it from the pool."""
    _pool(ctx).stop(name, forget=forget)
    _notify(f"{name} {'forgotten' if forget else 'stopped and deregistered'}")


@pool_app.command("stop-all")
def stop_all_instances(
    ctx: typer.Context,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Stop every pooled and orphan VM on every saved account, then verify."""
    if not yes and not Confirm.ask(
        "Stop [bold]every Colab VM on every saved account[/bold]? "
        "Unsaved work on all of them is lost.",
        default=False,
    ):
        raise typer.Abort
    result = _pool(ctx).stop_all()
    for session in result.stopped_sessions:
        console.print(
            f"[green]Stopped/deregistered[/green] {escape(session.name)} "
            f"on {escape(session.account)}"
        )
    for orphan in result.reaped_orphans:
        console.print(
            f"[green]Stopped orphan[/green] {escape(orphan.endpoint)} "
            f"on {escape(orphan.account)}"
        )
    for target, message in result.errors.items():
        error_console.print(
            Panel(
                Text(message),
                title=f"Stop operation failed: {target}",
                border_style="yellow" if result.success else "red",
            )
        )
    if result.success:
        console.print(
            "[green]Verified: no server instances, inspection errors, or pool "
            "registrations remain.[/green]"
        )
        return
    for entry in result.final.server:
        error_console.print(
            f"[red]Remaining instance:[/red] {escape(entry.endpoint)} on "
            f"{escape(entry.account)}"
            + (f" ({escape(entry.session)})" if entry.session else " (orphan)")
        )
    for session in result.final.vanished:
        error_console.print(
            f"[yellow]Remaining stale registration:[/yellow] "
            f"{escape(session.name)} on {escape(session.account)}"
        )
    for account, message in result.final.errors.items():
        error_console.print(
            Panel(
                Text(message), title=f"Could not inspect {account}", border_style="red"
            )
        )
    error_console.print(
        f"[red]Stop-all incomplete:[/red] {len(result.final.server)} server "
        f"instance(s), {len(result.remaining_sessions)} pool registration(s), "
        f"and {len(result.final.errors)} inspection error(s) remain."
    )
    raise typer.Exit(1)


def _print_holders(
    holders: list[dict], *, title: str, out: Console = error_console
) -> None:
    if not holders:
        return
    table = Table(title=title, box=box.ROUNDED, header_style="bold cyan")
    table.add_column("PID", justify="right")
    table.add_column("Command", overflow="fold")
    table.add_column("Mode")
    table.add_column("Held", justify="right")
    for holder in holders:
        seconds = holder["held_seconds"]
        table.add_row(
            Text(str(holder["pid"])),
            Text(holder["label"]),
            Text("waiting (exclusive)" if holder["waiting"] else holder["mode"]),
            Text("—" if seconds is None else f"{seconds:.0f}s"),
        )
    out.print(table)


def _usage_cell(value: float | None) -> Text:
    return Text("—" if value is None else f"{value:.2f}")


@pool_app.command("list")
def list_pool(
    ctx: typer.Context, cached: CachedOption = False, as_json: JsonOption = False
):
    """Show account balances and live server instances across all accounts."""
    pool = _pool(ctx)
    # Seen before waiting: once this command holds the lock, others cannot.
    holders = pool.lock_holders()
    if as_json:
        with pool.hold("list"):
            server_report = pool.reconcile(all_accounts=True, refresh=False)
            snapshot = pool.snapshot(refresh=not cached)
        _print_json(dict(snapshot_json(snapshot, server_report), lock_holders=holders))
        if server_report.errors:
            raise typer.Exit(1)
        return
    _print_holders(holders, title="Lock holders when this command started")
    with console.status("Loading pool overview…", spinner="dots"), pool.hold("list"):
        server_report = pool.reconcile(all_accounts=True, refresh=False)
        snapshot = pool.snapshot(refresh=not cached)
    accounts = Table(title="Pool accounts", box=box.ROUNDED, header_style="bold cyan")
    accounts.add_column("", width=2)
    accounts.add_column("Alias", style="bold", overflow="fold")
    accounts.add_column("Email", overflow="fold", no_wrap=True)
    accounts.add_column("CU", justify="right")
    accounts.add_column("CU/hr", justify="right")
    accounts.add_column("ETA h", justify="right")
    accounts.add_column("VMs", justify="right")
    accounts.add_column("Pooled", justify="right")
    accounts.add_column("Usage", style="dim")
    for account in snapshot.accounts:
        usage = account.usage
        if not snapshot.usage_refreshed:
            usage_state = "cached" if usage else "—"
        elif not account.stored:
            usage_state = "no credential"
        elif account.name in snapshot.errors:
            usage_state = "stale" if usage else "error"
        else:
            usage_state = "live" if usage else "—"
        marker = Text("★", style="yellow") if account.is_primary else Text("")
        if account.is_active:
            marker.append("●", style="green")
        accounts.add_row(
            marker,
            Text(account.name),
            Text(account.email or "Not verified"),
            Text("error")
            if account.name in snapshot.errors and not usage
            else _usage_cell(usage.balance if usage else None),
            _usage_cell(usage.hourly_rate if usage else None),
            Text(format_eta(usage)),
            Text(str(usage.assignments)) if usage else Text("—"),
            Text(str(account.sessions)),
            Text(usage_state),
        )
    console.print(accounts)
    usages = snapshot_usages(snapshot)
    total = total_usage(usages)
    if total is None:
        console.print("[dim]Total CU: — (no usage data)[/dim]")
    else:
        known = sum(usage is not None for usage in usages)
        label = (
            "Total"
            if known == len(usages)
            else f"Known total ({known}/{len(usages)} accounts)"
        )
        console.print(
            f"[bold]{label}:[/bold] {total.balance:.2f} CU  |  "
            f"{total.hourly_rate:.2f} CU/hr  |  ETA {format_eta(total)} hr"
        )
    console.print(
        f"[dim]★ Primary ({escape(snapshot.primary or 'unset')}; switch below "
        f"{snapshot.threshold:g} CU)  ● Active Colab account[/dim]"
    )
    inspected = len(server_report.accounts) - len(server_report.errors)
    sessions = Table(
        title=(
            f"Colab server instances ({inspected}/{len(server_report.accounts)} "
            "accounts inspected)"
        ),
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    sessions.add_column("Local name", style="bold", overflow="fold")
    sessions.add_column("Account", overflow="fold")
    sessions.add_column("Hardware")
    sessions.add_column("Endpoint", overflow="fold")
    sessions.add_column("State")
    sessions.add_column("Registered (local)", style="dim")
    pooled = {session.name: session for session in snapshot.sessions}
    for entry in server_report.server:
        session = pooled.get(entry.session) if entry.session else None
        sessions.add_row(
            Text(entry.session or "—"),
            Text(entry.account),
            Text(entry.hardware),
            Text(entry.endpoint),
            Text("pooled", style="green")
            if entry.session
            else Text("orphan", style="red"),
            Text(session.created_at if session else "—"),
        )
    for session in server_report.vanished:
        sessions.add_row(
            Text(session.name),
            Text(session.account),
            Text(session.hardware),
            Text(session.endpoint or "missing record", style="yellow"),
            Text("vanished", style="yellow"),
            Text(session.created_at),
        )
    if server_report.server or server_report.vanished:
        console.print(sessions)
    else:
        console.print(
            Panel(
                "No server instances in successfully inspected accounts.",
                title="Colab server instances",
                border_style="cyan",
            )
        )
    for name, message in snapshot.errors.items():
        error_console.print(
            Panel(
                Text(message), title=f"Usage unavailable: {name}", border_style="yellow"
            )
        )
    for name, message in server_report.errors.items():
        error_console.print(
            Panel(Text(message), title=f"Could not inspect {name}", border_style="red")
        )
    if server_report.errors:
        raise typer.Exit(1)


@pool_app.command()
def reconcile(
    ctx: typer.Context,
    all_accounts: Annotated[
        bool, typer.Option("--all", help="Probe every saved account.")
    ] = False,
    cached: CachedOption = False,
    as_json: JsonOption = False,
) -> None:
    """Compare the pool with Colab servers; exit 1 on any mismatch."""
    report = _pool(ctx).reconcile(all_accounts=all_accounts, refresh=not cached)
    if as_json:
        _print_json(report.to_json())
        raise typer.Exit(0 if report.consistent else 1)
    table = Table(
        title="Pool vs. Colab servers", box=box.ROUNDED, header_style="bold cyan"
    )
    table.add_column("Account", style="bold")
    table.add_column("Endpoint", overflow="fold")
    table.add_column("Hardware")
    table.add_column("State")
    for entry in report.server:
        table.add_row(
            Text(entry.account),
            Text(entry.endpoint),
            Text(entry.hardware),
            Text(f"= {entry.session}", style="green")
            if entry.session
            else Text("orphan (not pooled)", style="red"),
        )
    for session in report.vanished:
        table.add_row(
            Text(session.account),
            Text(session.endpoint or "—"),
            Text(session.hardware),
            Text(f"{session.name}: gone from server", style="yellow"),
        )
    console.print(table)
    for account, message in report.errors.items():
        error_console.print(
            Panel(
                Text(message), title=f"Could not inspect {account}", border_style="red"
            )
        )
    if report.consistent:
        console.print("[green]Pool and servers agree.[/green]")
        return
    if report.orphans:
        console.print(
            "[dim]Stop orphans with [cyan]pool reap ACCOUNT ENDPOINT[/cyan], or "
            "register them with [cyan]pool adopt[/cyan].[/dim]"
        )
    if report.vanished:
        console.print(
            "[dim]Deregister vanished sessions with [cyan]pool stop NAME[/cyan]; it "
            "also removes their local Colab records.[/dim]"
        )
    raise typer.Exit(1)


@pool_app.command("prune-state")
def prune_state(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Delete the stale records (default: report)."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Find Colab state records that are neither pooled nor on the server.

    Such leftovers block [cyan]pool new[/cyan] from reusing a name. Records
    whose endpoint is still assigned are never touched; use
    [cyan]pool adopt[/cyan] or [cyan]pool reap[/cyan] for those.
    """
    report = _pool(ctx).prune_state(apply=yes)
    if as_json:
        _print_json(report.to_json())
        raise typer.Exit(1 if report.errors else 0)
    removed = {(record.account, record.name) for record in report.removed}
    table = Table(
        title="Unpooled Colab state records", box=box.ROUNDED, header_style="bold cyan"
    )
    table.add_column("Account", style="bold")
    table.add_column("Name", overflow="fold")
    table.add_column("Endpoint", overflow="fold")
    table.add_column("State")
    for record in report.stale:
        table.add_row(
            Text(record.account),
            Text(record.name),
            Text(record.endpoint),
            Text("removed", style="green")
            if (record.account, record.name) in removed
            else Text("stale (gone from server)", style="yellow"),
        )
    for record in report.live:
        table.add_row(
            Text(record.account),
            Text(record.name),
            Text(record.endpoint),
            Text("live on server (kept)", style="red"),
        )
    if report.stale or report.live:
        console.print(table)
    else:
        console.print("[green]No unpooled Colab state records found.[/green]")
    for target, message in report.errors.items():
        error_console.print(
            Panel(Text(message), title=f"Could not check {target}", border_style="red")
        )
    if report.stale and not yes:
        console.print(
            "[dim]Remove the stale records with [cyan]pool prune-state --yes[/cyan]."
            "[/dim]"
        )
    if report.live:
        console.print(
            "[dim]Register live records with [cyan]pool adopt[/cyan], or stop them "
            "with [cyan]pool reap ACCOUNT ENDPOINT[/cyan].[/dim]"
        )
    if report.errors:
        raise typer.Exit(1)


@pool_app.command()
def adopt(
    ctx: typer.Context,
    name: SessionArgument,
    account: Annotated[str, typer.Argument(help="Owning colab-account alias.")],
    config: Annotated[
        str, typer.Argument(help="Colab --config session-state file holding NAME.")
    ],
    force: Annotated[
        bool, typer.Option("--force", help="Register even without a matching record.")
    ] = False,
) -> None:
    """Register a session created outside the pool."""
    session = _pool(ctx).adopt(name, account, config, force=force)
    console.print(
        f"[green]Adopted[/green] [bold]{escape(session.name)}[/bold] on "
        f"{escape(session.account)} ({escape(session.endpoint or 'no record')})"
    )


@pool_app.command("import")
def import_prototype(
    ctx: typer.Context,
    source: Annotated[
        str, typer.Argument(help="Prototype pool.json, or the directory holding it.")
    ],
    primary_file: Annotated[
        str | None,
        typer.Option("--primary-file", help="Defaults to .colab-primary beside it."),
    ] = None,
) -> None:
    """Adopt every session from a prototype pool registry (read-only)."""
    result = _pool(ctx).import_legacy(source, primary_file)
    console.print(
        f"[green]Imported {len(result.imported)} session(s)[/green]"
        + (
            f"; primary set to [bold]{escape(result.primary)}[/bold]"
            if result.primary
            else ""
        )
    )
    for name, reason in result.skipped.items():
        error_console.print(
            Panel(Text(reason), title=f"Skipped {name}", border_style="yellow")
        )
    if result.skipped:
        raise typer.Exit(1)


@pool_app.command()
def reap(
    ctx: typer.Context,
    account: Annotated[str, typer.Argument(help="Account that owns the VM.")],
    endpoint: Annotated[str, typer.Argument(help="Endpoint shown by reconcile.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Stop an orphan (unpooled) VM by endpoint."""
    if not yes and not Confirm.ask(
        f"Stop VM [bold]{escape(endpoint)}[/bold] on account "
        f"[bold]{escape(account)}[/bold]? Unsaved work on it is lost.",
        default=False,
    ):
        raise typer.Abort
    _pool(ctx).reap(account, endpoint)
    console.print(f"[green]Stopped[/green] {escape(endpoint)} on {escape(account)}")


@pool_app.command()
def primary(
    ctx: typer.Context,
    account: Annotated[
        str | None, typer.Argument(help="Account to pin (omit to show).")
    ] = None,
    threshold: Annotated[
        float | None,
        typer.Option("--threshold", min=0, help="Switch away below this many CU."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show or set the primary account and its switch threshold."""
    pool = _pool(ctx)
    if account is not None:
        pool.set_primary(account)
    if threshold is not None:
        pool.set_threshold(threshold)
    registry = pool.store.load()
    if as_json:
        _print_json(
            {"primary": registry["primary"], "threshold": registry["threshold"]}
        )
        return
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Primary", Text(registry["primary"] or "unset"))
    details.add_row("Threshold", f"{registry['threshold']:g} CU")
    console.print(Panel(details, title="Pool primary", border_style="cyan"))


@pool_app.command("limits")
def limits_command(
    ctx: typer.Context,
    max_sessions: Annotated[
        int | None,
        typer.Option(
            "--max-sessions", min=1, help="Most sessions across the whole pool."
        ),
    ] = None,
    max_per_account: Annotated[
        int | None,
        typer.Option("--max-per-account", min=1, help="Most sessions per account."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show or set the caps that [cyan]pool new[/cyan] enforces.

    Registered sessions count, plus the target account's unregistered VMs when
    its server listing can be trusted.
    """
    current = _pool(ctx).set_limits(
        max_sessions=max_sessions, max_per_account=max_per_account
    )
    if as_json:
        _print_json(current)
        return
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Max sessions", str(current["max_sessions"]))
    details.add_row("Max per account", str(current["max_per_account"]))
    console.print(Panel(details, title="Pool limits", border_style="cyan"))


def _passthrough(subcommand: str):
    def command(ctx: typer.Context, name: SessionArgument, args: ExtraArguments = None):
        raise typer.Exit(_pool(ctx).run(subcommand, name, args or []))

    command.__doc__ = (
        f"Run [cyan]colab {subcommand} -s NAME ...[/cyan] under the owning account."
    )
    return command


for _subcommand in SESSION_COMMANDS:
    if _subcommand == "status":
        continue
    pool_app.command(_subcommand, context_settings=PASSTHROUGH_SETTINGS)(
        _passthrough(_subcommand)
    )


@pool_app.command("status", context_settings=PASSTHROUGH_SETTINGS)
def status(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(help="Pooled session name (omit for the lock).")
    ] = None,
    args: ExtraArguments = None,
) -> None:
    """Run [cyan]colab status -s NAME[/cyan]; without NAME, show lock holders.

    Showing the lock holders never waits for the lock.
    """
    pool = _pool(ctx)
    if name is not None:
        raise typer.Exit(pool.run("status", name, args or []))
    holders = pool.lock_holders()
    if not holders:
        console.print("[green]The pool lock is free.[/green]")
        return
    _print_holders(holders, title="Pool lock holders", out=console)
    console.print(
        "[dim]Commands waiting behind these holders are queued, not lost.[/dim]"
    )


@pool_app.command("cmd", context_settings=PASSTHROUGH_SETTINGS)
def generic(
    ctx: typer.Context,
    subcommand: Annotated[str, typer.Argument(help="Colab CLI subcommand.")],
    name: SessionArgument,
    args: ExtraArguments = None,
) -> None:
    """Run any per-session Colab subcommand: [cyan]pool cmd SUB NAME ...[/cyan]."""
    raise typer.Exit(_pool(ctx).run(subcommand, name, args or []))
