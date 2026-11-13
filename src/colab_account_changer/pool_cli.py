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
    SESSION_COMMANDS,
    ColabPool,
    snapshot_json,
)

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
) -> None:
    """Use Colab sessions by name; accounts are chosen and switched for you."""
    ctx.obj = ColabPool(
        colab_command=colab_command,
        verify=not offline,
        lock_timeout=lock_timeout,
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
) -> None:
    """Create a session on the pool's account and register it.

    Unknown options are passed to [cyan]colab new[/cyan], e.g.
    [cyan]pool new node-x --gpu g4[/cyan]; put them after [cyan]--[/cyan] to
    be explicit.
    """
    session, selection = _pool(ctx).create(
        name, args or [], account=account, refresh=not cached
    )
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


def _usage_cell(value: float | None) -> Text:
    return Text("—" if value is None else f"{value:.2f}")


@pool_app.command("list")
def list_pool(
    ctx: typer.Context, cached: CachedOption = False, as_json: JsonOption = False
):
    """Show pooled sessions, account balances, and the primary account."""
    pool = _pool(ctx)
    if as_json:
        _print_json(snapshot_json(pool.snapshot(refresh=not cached)))
        return
    with console.status("Fetching account usage…", spinner="dots"):
        snapshot = pool.snapshot(refresh=not cached)
    accounts = Table(title="Pool accounts", box=box.ROUNDED, header_style="bold cyan")
    accounts.add_column("", width=2)
    accounts.add_column("Alias", style="bold", overflow="fold")
    accounts.add_column("Email", overflow="fold")
    accounts.add_column("Balance (CU)", justify="right")
    accounts.add_column("CU/hr", justify="right")
    accounts.add_column("VMs", justify="right")
    accounts.add_column("Pooled", justify="right")
    for account in snapshot.accounts:
        usage = account.usage
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
            Text(str(usage.assignments)) if usage else Text("—"),
            Text(str(account.sessions)),
        )
    console.print(accounts)
    console.print(
        f"[dim]★ Primary ({escape(snapshot.primary or 'unset')}; switch below "
        f"{snapshot.threshold:g} CU)  ● Active Colab account[/dim]"
    )
    sessions = Table(title="Pooled sessions", box=box.ROUNDED, header_style="bold cyan")
    sessions.add_column("Name", style="bold", overflow="fold")
    sessions.add_column("Account", overflow="fold")
    sessions.add_column("Hardware")
    sessions.add_column("Endpoint", overflow="fold")
    sessions.add_column("Created", style="dim")
    for session in snapshot.sessions:
        sessions.add_row(
            Text(session.name),
            Text(session.account),
            Text(session.hardware),
            Text(
                session.endpoint or "missing record",
                style="" if session.endpoint else "yellow",
            ),
            Text(session.created_at),
        )
    if snapshot.sessions:
        console.print(sessions)
    else:
        console.print(
            Panel(
                "No pooled sessions. Create one with [cyan]pool new NAME[/cyan].",
                title="Pooled sessions",
                border_style="cyan",
            )
        )
    for name, message in snapshot.errors.items():
        error_console.print(
            Panel(
                Text(message), title=f"Usage unavailable: {name}", border_style="yellow"
            )
        )


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
            "[dim]Deregister vanished sessions with [cyan]pool stop NAME[/cyan].[/dim]"
        )
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


def _passthrough(subcommand: str):
    def command(ctx: typer.Context, name: SessionArgument, args: ExtraArguments = None):
        raise typer.Exit(_pool(ctx).run(subcommand, name, args or []))

    command.__doc__ = (
        f"Run [cyan]colab {subcommand} -s NAME ...[/cyan] under the owning account."
    )
    return command


for _subcommand in SESSION_COMMANDS:
    pool_app.command(_subcommand, context_settings=PASSTHROUGH_SETTINGS)(
        _passthrough(_subcommand)
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
