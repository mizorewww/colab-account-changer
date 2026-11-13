"""Rich/Typer command-line interface."""

from __future__ import annotations

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
from colab_account_changer.manager import AccountManager, Profile

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


@app.command("list")
def list_accounts() -> None:
    """List saved profiles and their keyring status."""
    profiles = AccountManager().profiles()
    if not profiles:
        console.print(
            Panel(
                "No saved accounts yet.\n\n"
                "Save the current login: [cyan]colab-account save NAME[/cyan]\n"
                "Authenticate another: [cyan]colab-account add NAME[/cyan]",
                title="Colab accounts",
                border_style="cyan",
            )
        )
        return
    table = Table(title="Colab accounts", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("Alias", style="bold")
    table.add_column("Email")
    table.add_column("Credential")
    table.add_column("Updated", style="dim")
    for profile in profiles:
        table.add_row(
            Text("●", style="green") if profile.is_active else Text(""),
            Text(profile.name),
            Text(profile.email or "Not verified"),
            Text("saved", style="green")
            if profile.stored
            else Text("missing", style="red"),
            profile.updated_at.replace("T", " ")[:19],
        )
    console.print(table)
    console.print(
        "[dim]● Active profile for the default Colab CLI OAuth provider.[/dim]"
    )


@app.command()
def current() -> None:
    """Show the current local profile without contacting Google."""
    status = AccountManager().status()
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
    with console.status(f"Switching to {name}…", spinner="dots"):
        profile = AccountManager().use(
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


def main() -> None:
    try:
        app()
    except AccountChangerError as exc:
        error_console.print(
            Panel(
                Text(str(exc)),
                title="[red]Account operation failed[/red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc
    except KeyboardInterrupt as exc:
        error_console.print("\n[yellow]Cancelled.[/yellow]")
        raise SystemExit(130) from exc


if __name__ == "__main__":
    main()
