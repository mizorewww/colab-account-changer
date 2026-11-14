"""Rich/Typer command-line interface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from colab_account_changer import __version__
from colab_account_changer.errors import AccountChangerError
from colab_account_changer.manager import AccountManager, DiscoveryResult, Profile
from colab_account_changer.pool_cli import pool_app
from colab_account_changer.usage import Usage, format_eta, total_usage

console = Console()
error_console = Console(stderr=True)
app = typer.Typer(
    name="colab-account",
    help="Securely switch Google Colab CLI OAuth accounts.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(pool_app, name="pool")

NameArgument = Annotated[str, typer.Argument(help="Local profile alias, not an email.")]
OfflineOption = Annotated[
    bool,
    typer.Option("--offline", help="Skip online token refresh and identity checks."),
]
ForceOption = Annotated[
    bool,
    typer.Option("--force", help="Replace a profile with a different Google identity."),
]


def _show_profile(action: str, profile: Profile) -> None:
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Alias", Text(profile.name))
    details.add_row("Email", Text(profile.email or "Not verified (offline)"))
    console.print(
        Panel(
            details,
            title=f"[green]{action}[/green]",
            border_style="green",
            box=box.ROUNDED,
        )
    )


def _version(value: bool) -> None:
    if value:
        console.print(f"colab-account-changer {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show version."
        ),
    ] = None,
) -> None:
    """Manage local Colab CLI accounts without writing tokens to disk."""


def _discover(
    manager: AccountManager, *, offline: bool, include_gcloud: bool
) -> DiscoveryResult:
    result = manager.discover(verify=not offline, include_gcloud=include_gcloud)
    for message in result.errors:
        error_console.print(
            Panel(Text(message), title="Discovery warning", border_style="yellow")
        )
    return result


def _cache_age(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    try:
        fetched = datetime.fromisoformat(timestamp)
        seconds = max(0, int((datetime.now(UTC) - fetched).total_seconds()))
    except (TypeError, ValueError):
        return "cached"
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _print_usage_total(usages: list[Usage | None]) -> None:
    total = total_usage(usages)
    if total is None:
        console.print("[dim]Total CU: — (no usage data)[/dim]")
        return
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


def _render_accounts(
    manager: AccountManager,
    *,
    gcloud_accounts: list[str] | None = None,
    with_usage: bool = False,
) -> None:
    profiles = manager.profiles()
    usage_values = {}
    usage_errors = {}
    if with_usage:
        with console.status("Fetching account usage…", spinner="dots"):
            for profile in profiles:
                if not profile.stored:
                    continue
                try:
                    usage_values[profile.name] = manager.usage(profile.name)
                except AccountChangerError as exc:
                    usage_errors[profile.name] = str(exc)

    displayed_usage = {
        profile.name: usage_values.get(profile.name) if with_usage else profile.usage
        for profile in profiles
    }

    if not profiles:
        console.print(
            Panel(
                "No saved accounts or discoverable OAuth credentials.\n\n"
                "Authenticate an account: [cyan]colab-account add NAME[/cyan]",
                title="Colab accounts",
                border_style="cyan",
            )
        )
    else:
        table = Table(title="Colab accounts", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("", width=2)
        table.add_column("Alias", style="bold", overflow="fold")
        table.add_column("Email", overflow="fold", no_wrap=True)
        table.add_column("CU", justify="right")
        table.add_column("CU/hr", justify="right")
        table.add_column("ETA h", justify="right")
        table.add_column("VMs", justify="right")
        table.add_column("Age", style="dim")
        for profile in profiles:
            usage = displayed_usage[profile.name]
            cached_at = (
                "just now"
                if profile.name in usage_values
                else _cache_age(profile.usage_fetched_at)
            )
            row = [
                Text("●", style="green") if profile.is_active else Text(""),
                Text(profile.name),
                Text(profile.email or "Not verified"),
                Text(f"{usage.balance:.2f}")
                if usage
                else Text("error" if profile.name in usage_errors else "—"),
                Text(f"{usage.hourly_rate:.2f}") if usage else Text("—"),
                Text(format_eta(usage)),
                Text(str(usage.assignments)) if usage else Text("—"),
                Text(cached_at),
            ]
            table.add_row(*row)
        console.print(table)
        _print_usage_total(list(displayed_usage.values()))
        console.print(
            "[dim]● Active profile. Usage is cached; use --usage to refresh. "
            "ETA = balance ÷ current rate.[/dim]"
        )
    for name, message in usage_errors.items():
        error_console.print(
            Panel(
                Text(message), title=f"Usage unavailable: {name}", border_style="yellow"
            )
        )

    linked_emails = {profile.email.casefold() for profile in profiles if profile.email}
    gcloud_only = [
        account
        for account in gcloud_accounts or []
        if account.casefold() not in linked_emails
    ]
    if gcloud_only:
        console.print(
            Panel(
                Text("\n".join(gcloud_only)),
                title="gcloud accounts without a Colab OAuth credential",
                subtitle="Discovered, but not switchable or eligible for usage query",
                border_style="yellow",
            )
        )


@app.command("list")
def list_accounts(
    with_usage: Annotated[
        bool,
        typer.Option("--usage", help="Refresh live usage and update the local cache."),
    ] = False,
    offline: OfflineOption = False,
    no_discover: Annotated[
        bool,
        typer.Option("--no-discover", help="Only read existing changer profiles."),
    ] = False,
) -> None:
    """List accounts with cached usage; optionally refresh it live."""
    manager = AccountManager()
    discovery = (
        None
        if no_discover
        else _discover(manager, offline=offline, include_gcloud=True)
    )
    _render_accounts(
        manager,
        gcloud_accounts=discovery.gcloud_accounts if discovery else [],
        with_usage=with_usage,
    )


@app.command()
def sync(offline: OfflineOption = False) -> None:
    """Import existing keyring and legacy Colab OAuth credentials."""
    manager = AccountManager()
    result = _discover(manager, offline=offline, include_gcloud=True)
    console.print(
        f"[green]Discovered {len(result.profiles)} OAuth account(s); "
        f"imported {len(result.imported)} new profile(s).[/green]"
    )
    _render_accounts(manager, gcloud_accounts=result.gcloud_accounts)


@app.command()
def usage(
    name: Annotated[
        str | None,
        typer.Argument(help="Profile alias (defaults to the active account)."),
    ] = None,
) -> None:
    """Show live compute-unit balance and rate without switching accounts."""
    manager = AccountManager()
    _discover(manager, offline=False, include_gcloud=False)
    value = manager.usage(name)
    config = manager.config.load()
    profile_name = name or config["active_profile"]
    email = (
        config["profiles"].get(profile_name, {}).get("email") if profile_name else None
    )
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Account", Text(profile_name or "current"))
    if email:
        details.add_row("Email", Text(email))
    details.add_row("Current balance", f"{value.balance:.2f} compute units")
    details.add_row("Usage rate", f"{value.hourly_rate:.2f} CU/hr")
    details.add_row("Estimated time remaining", f"{format_eta(value)} hr")
    details.add_row("Active assignments", str(value.assignments))
    console.print(Panel(details, title="Colab usage", border_style="cyan"))


@app.command()
def current() -> None:
    """Show the current local profile without contacting Google."""
    manager = AccountManager()
    _discover(manager, offline=True, include_gcloud=False)
    status = manager.status()
    if not status.has_credential:
        console.print(
            Panel(
                "No active Colab OAuth credential.\n"
                "Use [cyan]colab-account add NAME[/cyan] to sign in.",
                title="Not signed in",
                border_style="yellow",
            )
        )
        return
    if not status.active_profile:
        console.print(
            Panel(
                "An OAuth credential exists, but it is not tracked by this tool.\n"
                "Run [cyan]colab-account save NAME[/cyan] before switching.",
                title="Unsaved account",
                border_style="yellow",
            )
        )
        return
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Alias", Text(status.active_profile))
    details.add_row("Email", Text(status.email or "Not verified"))
    details.add_row(
        "Saved copy",
        "current" if status.matches_saved else "changed; run save to update",
    )
    console.print(Panel(details, title="Current Colab account", border_style="green"))


@app.command()
def save(
    name: Annotated[
        str | None, typer.Argument(help="Alias (defaults to active profile).")
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Email label for --offline saves.")
    ] = None,
    offline: OfflineOption = False,
    force: ForceOption = False,
) -> None:
    """Save the current Colab login under a local profile alias."""
    with console.status("Validating the current OAuth credential…", spinner="dots"):
        profile = AccountManager().save(
            name, email=email, verify=not offline, force=force
        )
    _show_profile("Saved and activated", profile)


@app.command()
def add(
    name: NameArgument,
    offline: OfflineOption = False,
    colab_command: Annotated[
        str,
        typer.Option("--colab-command", help="Colab CLI executable to run."),
    ] = "colab",
) -> None:
    """Sign in a new Google account and save it as a profile."""
    console.print(
        Panel(
            "The current profile is saved first. Colab CLI will then ask you to\n"
            "authorize the new account. Failure or Ctrl-C restores the old account.",
            title=f"Add {escape(name)}",
            border_style="cyan",
        )
    )
    manager = AccountManager(colab_command=colab_command)
    _discover(manager, offline=offline, include_gcloud=False)
    profile = manager.add(name, verify=not offline)
    _show_profile("Added and activated", profile)


@app.command()
def use(
    name: NameArgument,
    no_save: Annotated[
        bool,
        typer.Option(
            "--no-save", help="Do not update the current profile before switching."
        ),
    ] = False,
    discard_unsaved: Annotated[
        bool,
        typer.Option(
            "--discard-unsaved",
            help="Allow an untracked current credential to be replaced (unsafe).",
        ),
    ] = False,
    offline: OfflineOption = False,
) -> None:
    """Switch to a saved account, preserving the current one by default."""
    if discard_unsaved:
        console.print(
            "[yellow]Warning:[/yellow] the current credential may be replaced "
            "without being saved."
        )
    manager = AccountManager()
    with console.status(f"Switching to {name}…", spinner="dots"):
        if not discard_unsaved:
            _discover(manager, offline=offline, include_gcloud=False)
        profile = manager.use(
            name,
            save_current=not no_save,
            discard_unsaved=discard_unsaved,
            verify=not offline,
        )
    _show_profile("Active account", profile)


@app.command()
def remove(
    name: NameArgument,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Delete an inactive profile's credential and metadata."""
    if not yes and not Confirm.ask(
        f"Delete profile [bold]{escape(name)}[/bold] from the OS keyring?",
        default=False,
    ):
        raise typer.Abort
    profile = AccountManager().remove(name)
    console.print(f"[green]Removed profile[/green] [bold]{escape(profile.name)}[/bold]")


def _run(application: typer.Typer) -> None:
    try:
        application()
    except AccountChangerError as exc:
        error_console.print(
            Panel(
                Text(str(exc)),
                title="[red]Account operation failed[/red]",
                border_style="red",
            )
        )
        raise SystemExit(getattr(exc, "exit_code", 1)) from exc
    except KeyboardInterrupt as exc:
        error_console.print("\n[yellow]Cancelled.[/yellow]")
        raise SystemExit(130) from exc


def main() -> None:
    _run(app)


def pool_main() -> None:
    """Entry point for ``colab-pool``, equivalent to ``colab-account pool``."""
    _run(pool_app)


if __name__ == "__main__":
    main()
