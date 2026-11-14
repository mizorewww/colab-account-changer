"""A resource pool of Colab accounts addressed only by session name.

Colab CLI keeps one OAuth credential per machine, and a session is visible only
to the account that created it. When a command runs under another account,
Colab CLI prunes the "invisible" session record. The pool therefore records the
owning account of every session, keeps one Colab session-state file per account,
and runs every Colab command under the account lock after switching to the
owning account.

The pool never reads, prints, or stores OAuth or runtime-proxy tokens. Colab
CLI's own session-state files contain proxy tokens; the pool only extracts the
non-secret ``endpoint`` field from them.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import ReadWriteLock, Timeout

from colab_account_changer.errors import AccountChangerError, ConfigError, PoolError
from colab_account_changer.manager import AccountManager
from colab_account_changer.storage import validate_profile_name, write_private_json
from colab_account_changer.tokens import shares_refresh_token
from colab_account_changer.usage import Usage, total_usage

DEFAULT_THRESHOLD = 5.0
DEFAULT_LOCK_TIMEOUT = 600.0
# Conservative caps against runaway "retry pool new" loops; see 'pool limits'.
DEFAULT_MAX_SESSIONS = 12
DEFAULT_MAX_PER_ACCOUNT = 4
LIMIT_KEYS = ("max_sessions", "max_per_account")
REGISTRY_NAME = "pool.json"
STATE_DIRECTORY = "pool-state"
# Colab subcommands that act on one existing session selected with ``-s``.
SESSION_COMMANDS = (
    "exec",
    "repl",
    "upload",
    "download",
    "ls",
    "rm",
    "edit",
    "install",
    "drivemount",
    "status",
    "log",
    "url",
    "restart-kernel",
    "ssh",
)
SESSION_LINE_RE = re.compile(
    r"^\[(?P<name>[^\]]*)\] (?P<endpoint>\S+) \| Hardware: (?P<hardware>[^|]+?) \|"
)
QUOTA_HINTS = (
    "Allocation refused",
    "Backend rejected accelerator",
    "quota",
    "entitlement",
)
# Printed by ``colab sessions`` when the listing is empty. With an empty local
# state file Colab CLI also prints it after swallowing an authentication
# failure, so the pool never trusts it without an independent cross-check.
NO_SESSIONS_MESSAGE = "No active sessions found on server"
EMPTY_CONFIRM_ATTEMPTS = 2
EMPTY_CONFIRM_RETRY_DELAY = 1.5
STATE_LOCK_TIMEOUT = 30.0


def default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "primary": None,
        "threshold": DEFAULT_THRESHOLD,
        "sessions": {},
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_session_name(name: str) -> str:
    try:
        return validate_profile_name(name)
    except ValueError as exc:
        raise PoolError(
            "Session names must be 1-64 characters: letters, digits, '.', '_', "
            "or '-', starting with a letter or digit."
        ) from exc


def hardware_from_args(args: Sequence[str]) -> str:
    """Best-effort label from ``colab new`` arguments (``--gpu g4`` -> ``g4``)."""
    for index, value in enumerate(args):
        for flag in ("--gpu", "--tpu"):
            if value == flag and index + 1 < len(args):
                return args[index + 1].lower()
            if value.startswith(flag + "="):
                return value.split("=", 1)[1].lower()
    return "cpu"


class PoolStore:
    """The non-secret registry: session name -> owning account and state file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_registry()
        try:
            registry = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid pool registry at {self.path}: {exc}") from exc
        self._validate(registry)
        return registry

    def save(self, registry: dict[str, Any]) -> None:
        self._validate(registry)
        write_private_json(self.path, registry)

    @staticmethod
    def _validate(registry: Any) -> None:
        if not isinstance(registry, dict) or registry.get("version") != 1:
            raise ConfigError("Unsupported pool registry format.")
        primary = registry.get("primary")
        if primary is not None and not isinstance(primary, str):
            raise ConfigError("Pool 'primary' must be a string or null.")
        threshold = registry.get("threshold")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or threshold < 0
        ):
            raise ConfigError("Pool 'threshold' must be a non-negative number.")
        limits = registry.get("limits")
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS):
                raise ConfigError("Pool 'limits' has an unsupported format.")
            for key, value in limits.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(f"Pool limit {key!r} must be a positive integer.")
        sessions = registry.get("sessions")
        if not isinstance(sessions, dict):
            raise ConfigError("Pool 'sessions' must be an object.")
        for name, entry in sessions.items():
            try:
                validate_profile_name(name)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"Invalid pool session name: {name!r}") from exc
            if not isinstance(entry, dict):
                raise ConfigError(f"Pool session {name!r} has invalid metadata.")
            for key in ("account", "config", "hardware", "created_at"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ConfigError(f"Pool session {name!r} is missing {key}.")
            if not Path(entry["config"]).is_absolute():
                raise ConfigError(
                    f"Pool session {name!r} must use an absolute config path."
                )


@dataclass(frozen=True)
class PoolSession:
    name: str
    account: str
    config: str
    hardware: str
    created_at: str
    endpoint: str | None = None


@dataclass(frozen=True)
class AccountState:
    name: str
    email: str | None
    usage: Usage | None
    usage_fetched_at: str | None
    stored: bool
    sessions: int
    is_primary: bool
    is_active: bool


@dataclass(frozen=True)
class Snapshot:
    primary: str | None
    threshold: float
    active: str | None
    sessions: list[PoolSession]
    accounts: list[AccountState]
    errors: dict[str, str] = field(default_factory=dict)
    usage_refreshed: bool = False


@dataclass(frozen=True)
class Selection:
    account: str
    repinned_from: str | None
    balance: float | None
    existing: bool = False  # ``create(if_missing=True)`` reused a live session


@dataclass(frozen=True)
class ServerSession:
    account: str
    endpoint: str
    hardware: str
    session: str | None  # pool session name, or None for an orphan


@dataclass
class ReconcileReport:
    accounts: list[str]
    server: list[ServerSession] = field(default_factory=list)
    vanished: list[PoolSession] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def orphans(self) -> list[ServerSession]:
        return [entry for entry in self.server if entry.session is None]

    @property
    def consistent(self) -> bool:
        return not (self.orphans or self.vanished or self.errors)

    def to_json(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "accounts": self.accounts,
            "server": [asdict(entry) for entry in self.server],
            "orphans": [asdict(entry) for entry in self.orphans],
            "vanished": [asdict(entry) for entry in self.vanished],
            "errors": self.errors,
        }


@dataclass(frozen=True)
class StateRecord:
    account: str
    name: str
    endpoint: str


@dataclass
class PruneStateReport:
    """Unregistered records in the pool's per-account Colab state files."""

    stale: list[StateRecord] = field(default_factory=list)  # gone from server
    live: list[StateRecord] = field(default_factory=list)  # still assigned
    removed: list[StateRecord] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stale": [asdict(record) for record in self.stale],
            "live": [asdict(record) for record in self.live],
            "removed": [asdict(record) for record in self.removed],
            "errors": self.errors,
        }


@dataclass(frozen=True)
class StopAllResult:
    final: ReconcileReport
    remaining_sessions: list[PoolSession]
    stopped_sessions: list[PoolSession] = field(default_factory=list)
    reaped_orphans: list[ServerSession] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.final.consistent and not self.remaining_sessions

    @property
    def stopped_count(self) -> int:
        return len(self.stopped_sessions) + len(self.reaped_orphans)


@dataclass(frozen=True)
class ImportResult:
    imported: list[str]
    skipped: dict[str, str]
    primary: str | None


class ColabPool:
    """Name-addressed Colab sessions spread across saved accounts."""

    def __init__(
        self,
        manager: AccountManager | None = None,
        *,
        colab_command: str = "colab",
        verify: bool = True,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.manager = manager or AccountManager()
        self.colab_command = colab_command
        self.verify = verify
        self.lock_timeout = lock_timeout
        self.runner = runner or subprocess.run
        self.notify = notify or (lambda message: None)
        self.root = self.manager.config.path.parent
        self.store = PoolStore(self.root / REGISTRY_NAME)
        self.state_dir = self.root / STATE_DIRECTORY

    # Locking and account switching -------------------------------------

    def hold(self):
        """The account lock, held across a switch and the Colab subprocess."""
        return self.manager.exclusive(
            timeout=self.lock_timeout,
            message=(
                f"Timed out after {self.lock_timeout:g}s waiting for the account "
                f"lock ({self.manager.lock_path}). Another pool or colab-account "
                "command is still running; retry later or raise --lock-timeout."
            ),
        )

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.state_dir.chmod(0o700)

    def state_file(self, account: str) -> Path:
        return self.state_dir / f"{validate_profile_name(account)}.json"

    def _profiles(self) -> dict[str, Any]:
        return self.manager.config.load()["profiles"]

    def _require_account(self, account: str) -> str:
        try:
            account = validate_profile_name(account)
        except ValueError as exc:
            raise PoolError(str(exc)) from exc
        if account not in self._profiles():
            raise PoolError(
                f"Account {account!r} is not a saved colab-account profile."
            )
        return account

    def _activate(self, account: str) -> None:
        """Switch the upstream credential; the caller must hold the lock."""
        config = self.manager.config.load()
        current = self.manager.credentials.read_colab()
        if config["active_profile"] == account and current is not None:
            saved = self.manager.credentials.read_profile(account)
            if saved is None:
                raise PoolError(
                    f"Active profile {account!r} has no saved credential. Refusing "
                    "to use the current Colab login as a substitute; restore the "
                    "profile credential with 'colab-account save' or switch with "
                    "'colab-account use'."
                )
            if not shares_refresh_token(current, saved):
                raise PoolError(
                    f"The active Colab credential no longer matches profile "
                    f"{account!r} (signed in outside colab-account?). Run "
                    "'colab-account sync' or 'colab-account use' to fix it first."
                )
            return
        previous = config["active_profile"]
        try:
            self.manager.use(account, save_current=True, verify=self.verify)
        except AccountChangerError as exc:
            raise PoolError(f"Could not switch to account {account!r}: {exc}") from exc
        if self.manager.config.load()["active_profile"] != account:
            raise PoolError(f"Switching to account {account!r} did not take effect.")
        self.notify(f"Switched Colab account {previous or '(none)'} -> {account}")

    def _colab(
        self,
        config: Path | str,
        args: Sequence[str],
        *,
        capture: str | None = None,
        interactive: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        options: dict[str, Any] = {"check": False, "text": True}
        if not interactive:
            # Internal probes must fail instead of waiting for an OAuth prompt.
            options["stdin"] = subprocess.DEVNULL
        if capture == "stdout":
            options["stdout"] = subprocess.PIPE
        elif capture == "stderr":
            options["stderr"] = subprocess.PIPE
        elif capture == "both":
            options["capture_output"] = True
        try:
            return self.runner(
                [self.colab_command, "--config", str(config), *args], **options
            )
        except FileNotFoundError as exc:
            raise PoolError(
                f"Colab CLI executable {self.colab_command!r} was not found."
            ) from exc

    # Registry helpers ---------------------------------------------------

    @staticmethod
    def recorded_endpoint(config: str | Path, name: str) -> str | None:
        """Read only the non-secret endpoint of ``name`` from a Colab state file."""
        try:
            records = json.loads(Path(config).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        record = records.get(name) if isinstance(records, dict) else None
        endpoint = record.get("endpoint") if isinstance(record, dict) else None
        return endpoint if isinstance(endpoint, str) and endpoint else None

    @classmethod
    def _session(cls, name: str, entry: dict[str, Any]) -> PoolSession:
        return PoolSession(
            name=name,
            account=entry["account"],
            config=entry["config"],
            hardware=entry["hardware"],
            created_at=entry["created_at"],
            endpoint=cls.recorded_endpoint(entry["config"], name),
        )

    def sessions(self) -> list[PoolSession]:
        registry = self.store.load()
        return [
            self._session(name, entry)
            for name, entry in sorted(registry["sessions"].items())
        ]

    def get(self, name: str) -> PoolSession:
        name = validate_session_name(name)
        entry = self.store.load()["sessions"].get(name)
        if entry is None:
            raise PoolError(
                f"Session {name!r} is not in the pool. Use 'pool list' to see "
                "pooled sessions, or 'pool adopt' to add an existing one."
            )
        return self._session(name, entry)

    def _register(self, name: str, entry: dict[str, Any]) -> None:
        registry = self.store.load()
        updated = copy.deepcopy(registry)
        updated["sessions"][name] = entry
        self.store.save(updated)

    def _deregister(self, name: str) -> None:
        registry = self.store.load()
        if name in registry["sessions"]:
            updated = copy.deepcopy(registry)
            del updated["sessions"][name]
            self.store.save(updated)

    @staticmethod
    def read_records(config: str | Path) -> dict[str, str]:
        """Map record name -> endpoint in a Colab state file (no other fields)."""
        try:
            records = json.loads(Path(config).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(records, dict):
            return {}
        found = {}
        for name, record in records.items():
            endpoint = record.get("endpoint") if isinstance(record, dict) else None
            if isinstance(name, str) and isinstance(endpoint, str) and endpoint:
                found[name] = endpoint
        return found

    def _drop_record(self, config: str | Path, name: str, endpoint: str) -> bool:
        """Delete ``name`` from a Colab state file if it still points at ``endpoint``.

        The write happens under Colab CLI's own state-file lock (``FILE.lock``,
        a filelock ``ReadWriteLock``) and replaces the file atomically. Records
        hold runtime proxy tokens, so their contents are never shown.
        """
        path = Path(config)
        if not path.exists():
            return False
        try:
            lock = ReadWriteLock(
                f"{path}.lock", timeout=STATE_LOCK_TIMEOUT, is_singleton=False
            )
            with lock.write_lock():
                try:
                    records = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise PoolError(f"Could not read Colab state file {path}.") from exc
                if not isinstance(records, dict):
                    raise PoolError(f"Colab state file {path} is not an object.")
                record = records.get(name)
                if not isinstance(record, dict) or record.get("endpoint") != endpoint:
                    return False
                del records[name]
                write_private_json(path, records)
                return True
        except Timeout as exc:
            raise PoolError(
                f"Timed out waiting for the Colab state-file lock of {path}."
            ) from exc
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            raise PoolError(
                f"Could not lock Colab state file {path} ({type(exc).__name__})."
            ) from exc

    def _retire(self, session: PoolSession) -> None:
        """Forget a session whose endpoint is confirmed gone from the server.

        Removes the pool registration and the matching Colab state record, so
        the name can be reused without an "unpooled record" conflict.
        """
        self._deregister(session.name)
        if session.endpoint is None:
            return
        try:
            self._drop_record(session.config, session.name, session.endpoint)
        except AccountChangerError as exc:
            self.notify(
                f"Could not remove the stale Colab record of {session.name!r} "
                f"({exc}); 'pool prune-state --yes' can remove it later."
            )

    def _confirmed_gone(self, account: str, endpoint: str) -> bool:
        """True only if a trustworthy probe shows ``endpoint`` is unassigned."""
        try:
            listed = self._server_sessions(account)
        except AccountChangerError:
            return False
        return all(found != endpoint for found, _ in listed)

    # Primary account and selection -------------------------------------

    def set_primary(self, account: str) -> str:
        with self.hold():
            account = self._require_account(account)
            registry = copy.deepcopy(self.store.load())
            registry["primary"] = account
            self.store.save(registry)
            return account

    def set_threshold(self, threshold: float) -> float:
        if not math.isfinite(threshold) or threshold < 0:
            raise PoolError("The threshold must be a non-negative number.")
        with self.hold():
            registry = copy.deepcopy(self.store.load())
            registry["threshold"] = float(threshold)
            self.store.save(registry)
            return float(threshold)

    @staticmethod
    def limits(registry: dict[str, Any]) -> dict[str, int]:
        """Effective session limits (stored values over the defaults)."""
        stored = registry.get("limits") or {}
        return {
            "max_sessions": stored.get("max_sessions", DEFAULT_MAX_SESSIONS),
            "max_per_account": stored.get("max_per_account", DEFAULT_MAX_PER_ACCOUNT),
        }

    def set_limits(
        self, *, max_sessions: int | None = None, max_per_account: int | None = None
    ) -> dict[str, int]:
        changes = {
            key: value
            for key, value in zip(
                LIMIT_KEYS, (max_sessions, max_per_account), strict=True
            )
            if value is not None
        }
        if any(value < 1 for value in changes.values()):
            raise PoolError("Pool limits must be positive integers.")
        with self.hold():
            registry = copy.deepcopy(self.store.load())
            if changes:
                registry["limits"] = {**(registry.get("limits") or {}), **changes}
                self.store.save(registry)
            return self.limits(registry)

    def refresh_usage(self, accounts: Iterable[str] | None = None) -> dict[str, str]:
        """Refresh cached usage; the caller must hold the lock. Returns errors."""
        profiles = self._profiles()
        errors: dict[str, str] = {}
        for name in sorted(accounts if accounts is not None else profiles):
            if self.manager.credentials.read_profile(name) is None:
                continue
            try:
                self.manager.usage(name)
            except AccountChangerError as exc:
                errors[name] = str(exc)
        return errors

    def _select(self) -> Selection:
        """Stay on the primary until it drops below the threshold."""
        registry = self.store.load()
        profiles = self._profiles()

        def balance(name: str) -> float | None:
            usage = profiles[name].get("usage")
            return usage["balance"] if usage else None

        candidates = [
            name
            for name in sorted(profiles)
            if self.manager.credentials.read_profile(name) is not None
        ]
        if not candidates:
            raise PoolError(
                "No saved account with a stored credential is available. "
                "Add one with 'colab-account add NAME'."
            )
        primary = registry["primary"]
        threshold = registry["threshold"]
        if primary in candidates:
            current = balance(primary)
            if current is not None and current >= threshold:
                return Selection(primary, None, current)
        best = max(candidates, key=lambda name: balance(name) or float("-inf"))
        if balance(best) is None:
            raise PoolError(
                "No account has a known compute-unit balance. Refresh usage "
                "(omit --cached) or pass --account explicitly."
            )
        if best != primary:
            updated = copy.deepcopy(registry)
            updated["primary"] = best
            self.store.save(updated)
            return Selection(best, primary, balance(best))
        return Selection(best, None, balance(best))

    # Session lifecycle --------------------------------------------------

    def create(
        self,
        name: str,
        args: Sequence[str] = (),
        *,
        account: str | None = None,
        refresh: bool = True,
        if_missing: bool = False,
    ) -> tuple[PoolSession, Selection]:
        """Create and register a session, or with ``if_missing`` reuse it.

        With ``if_missing``, a registered session whose endpoint is still
        assigned is returned unchanged (``Selection.existing``); one confirmed
        gone is cleaned up and created again. Pool limits are enforced before
        anything is allocated.
        """
        name = validate_session_name(name)
        with self.hold():
            existing = self.store.load()["sessions"].get(name)
            if existing is not None:
                if not if_missing:
                    raise PoolError(
                        f"Session {name!r} is already pooled on account "
                        f"{existing['account']!r}. Use it, 'pool stop {name}' "
                        "first, or pass --if-missing to reuse it."
                    )
                session = self._session(name, existing)
                if self._still_assigned(session):
                    return session, Selection(session.account, None, None, True)
                self._retire(session)
                self.notify(
                    f"Session {name!r} was registered on account "
                    f"{session.account!r}, but endpoint {session.endpoint} is no "
                    "longer assigned; removed it and creating it again."
                )
            if account is not None:
                selection = Selection(self._require_account(account), None, None)
            else:
                if refresh:
                    for failed, message in self.refresh_usage().items():
                        self.notify(f"Usage unavailable for {failed}: {message}")
                selection = self._select()
                threshold = self.store.load()["threshold"]
                if selection.repinned_from is not None:
                    self.notify(
                        f"Primary account {selection.repinned_from or '(unset)'} -> "
                        f"{selection.account} ({selection.balance:.2f} CU)"
                    )
                if selection.balance is not None and selection.balance < threshold:
                    self.notify(
                        f"Warning: every account is below {threshold:g} CU; using "
                        f"{selection.account} ({selection.balance:.2f} CU)"
                    )
            config = self.state_file(selection.account)
            self._ensure_state_dir()
            self._check_limits(selection.account, None)
            listed: list[tuple[str, str]] | None
            probe_error = None
            try:
                listed = self._server_sessions(selection.account)
            except AccountChangerError as exc:
                listed, probe_error = None, str(exc)
            self._clear_stale_record(
                selection.account, config, name, listed, probe_error
            )
            if listed is None:
                self.notify(
                    f"Could not inspect account {selection.account!r} before "
                    f"creating ({probe_error}); pool limits count registered "
                    "sessions only."
                )
            else:
                self._check_limits(selection.account, listed)
            self._activate(selection.account)
            result = self._colab(config, ["new", "-s", name, *args], capture="stderr")
            if result.stderr:
                sys.stderr.write(result.stderr)
                sys.stderr.flush()
            endpoint = self.recorded_endpoint(config, name)
            entry = {
                "account": selection.account,
                "config": str(config.resolve()),
                "hardware": hardware_from_args(args),
                "created_at": _now(),
            }
            if result.returncode != 0 and endpoint is not None:
                self._release_partial(name, entry, endpoint, result.returncode)
            if endpoint is None:
                hint = ""
                if any(word in (result.stderr or "") for word in QUOTA_HINTS):
                    hint = (
                        " Colab refused the allocation on this account (quota, "
                        "entitlement, or capacity). Try another accelerator or "
                        "'--account ALIAS' for a different account."
                    )
                if result.returncode == 0:
                    message = (
                        f"Colab reported success for session {name!r} on account "
                        f"{selection.account!r} but wrote no session record, so "
                        "the pool cannot manage it."
                    )
                else:
                    message = (
                        f"Colab could not create session {name!r} on account "
                        f"{selection.account!r} (exit code {result.returncode})."
                        f"{hint}"
                    )
                raise PoolError(message + self._leak_hint(selection.account, listed))
            self._register(name, entry)
            return self.get(name), selection

    def _still_assigned(self, session: PoolSession) -> bool:
        """Whether a registered session's endpoint is on the server.

        Raises when that cannot be decided, so ``--if-missing`` never creates
        a duplicate of a session that might still be running.
        """
        self._require_account(session.account)
        if session.endpoint is None:
            raise PoolError(
                f"Session {session.name!r} is registered on account "
                f"{session.account!r} but has no local Colab record, so it cannot "
                "be checked. Run 'pool reconcile'; once it is confirmed gone, "
                f"'pool stop {session.name}' removes the registration."
            )
        try:
            listed = self._server_sessions(session.account)
        except AccountChangerError as exc:
            raise PoolError(
                f"Session {session.name!r} is registered on account "
                f"{session.account!r}, but the server could not be checked "
                f"({exc}). Not creating a duplicate; retry later."
            ) from exc
        return any(endpoint == session.endpoint for endpoint, _ in listed)

    def _check_limits(self, account: str, listed: list[tuple[str, str]] | None) -> None:
        """Refuse a new session at the pool or per-account limit.

        Counts registered sessions and, when ``listed`` is a trustworthy probe
        of ``account``, that account's unregistered server assignments.
        """
        registry = self.store.load()
        limits = self.limits(registry)
        sessions = [
            self._session(name, entry)
            for name, entry in sorted(registry["sessions"].items())
        ]
        mine = [session for session in sessions if session.account == account]
        known = {session.endpoint for session in mine if session.endpoint}
        orphans = [endpoint for endpoint, _ in (listed or []) if endpoint not in known]
        total = len(sessions) + len(orphans)
        per_account = len(mine) + len(orphans)
        problems = []
        if total >= limits["max_sessions"]:
            problems.append(
                f"the pool already has {total} session(s) (limit "
                f"{limits['max_sessions']})"
            )
        if per_account >= limits["max_per_account"]:
            problems.append(
                f"account {account!r} already has {per_account} (limit "
                f"{limits['max_per_account']})"
            )
        if not problems:
            return
        current = ", ".join(
            f"{session.name} ({session.account})" for session in sessions
        )
        unregistered = (
            f" Unregistered VMs on {account!r}: {', '.join(orphans)}."
            if orphans
            else ""
        )
        raise PoolError(
            f"Refusing to create a session: {' and '.join(problems)}. "
            f"Registered sessions: {current or 'none'}.{unregistered} Reuse one "
            "('pool new --if-missing NAME'), stop one ('pool stop NAME', "
            "'pool reap'), or raise the limits with 'pool limits'."
        )

    def _leak_hint(self, account: str, before: list[tuple[str, str]] | None) -> str:
        """After a failed ``colab new``, name any VM it allocated anyway."""
        if before is None:
            return (
                " A VM may still have been allocated: run 'pool reconcile' "
                "before retrying."
            )
        try:
            after = self._server_sessions(account)
        except AccountChangerError:
            return (
                " The server could not be checked afterwards: run 'pool "
                "reconcile' before retrying."
            )
        previous = {endpoint for endpoint, _ in before}
        leaked = [endpoint for endpoint, _ in after if endpoint not in previous]
        if not leaked:
            return ""
        return (
            f" A new unregistered VM appeared on account {account!r} during the "
            f"attempt: {', '.join(leaked)}. Stop it with 'pool reap {account} "
            "ENDPOINT' before retrying."
        )

    def _release_partial(
        self, name: str, entry: dict[str, Any], endpoint: str, returncode: int
    ) -> None:
        """Handle ``colab new`` failing after the VM was already allocated.

        Try to release the VM and confirm with a trustworthy probe. If either
        step fails, register the session instead so it never becomes an
        unmanaged orphan. Always raises; the caller holds the account lock.
        """
        account = entry["account"]
        failure = (
            f"Colab could not create session {name!r} on account {account!r} "
            f"(exit code {returncode}), but a VM had already been allocated "
            f"(endpoint {endpoint})."
        )
        self.notify(f"{failure} Releasing it.")
        stop = self._colab(
            entry["config"],
            ["stop", "-s", name],
            capture="both",
            interactive=False,
        )
        try:
            listed = self._server_sessions(account)
        except AccountChangerError as exc:
            reason = f"its release could not be verified ({exc})"
        else:
            if all(found != endpoint for found, _ in listed):
                try:
                    self._drop_record(entry["config"], name, endpoint)
                except AccountChangerError as exc:
                    self.notify(
                        f"Could not remove the Colab record of {name!r} ({exc}); "
                        "'pool prune-state --yes' can remove it later."
                    )
                raise PoolError(
                    f"{failure} It was released and verified gone; nothing was "
                    "registered."
                )
            reason = f"'colab stop' could not release it (exit code {stop.returncode})"
        self._register(name, entry)
        raise PoolError(
            f"{failure} The pool tried to release it, but {reason}. The VM is "
            f"registered as {name!r} so it is not orphaned; release it with "
            f"'pool stop {name}'."
        )

    def _clear_stale_record(
        self,
        account: str,
        config: Path,
        name: str,
        listed: list[tuple[str, str]] | None,
        probe_error: str | None,
    ) -> None:
        """Resolve an unpooled same-name record before ``colab new`` reuses it.

        ``listed`` is a trustworthy probe of ``account`` taken under the
        account lock, or ``None`` with ``probe_error``. A record whose endpoint
        is confirmed gone is removed. A live endpoint, or one that cannot be
        checked, keeps the record and refuses creation.
        """
        stale = self.recorded_endpoint(config, name)
        if stale is None:
            return
        if listed is None:
            raise PoolError(
                f"Account {account!r} already has an unpooled Colab record named "
                f"{name!r} (endpoint {stale}), and the server could not be checked "
                f"({probe_error}). Not creating another session under that name; "
                "retry later or run 'pool reconcile'."
            )
        if any(endpoint == stale for endpoint, _ in listed):
            raise PoolError(
                f"Account {account!r} already has an unpooled Colab record named "
                f"{name!r} whose endpoint {stale} is still assigned on the server. "
                f"Register it with 'pool adopt {name} {account} {config}', or stop "
                f"it with 'pool reap {account} {stale}'."
            )
        if self._drop_record(config, name, stale):
            self.notify(
                f"Removed the stale Colab record of {name!r} on account "
                f"{account!r}; endpoint {stale} is no longer assigned."
            )

    def _handle_pruned_session(self, session: PoolSession) -> None:
        """Reconcile a record that a failed Colab command removed locally.

        Colab CLI prunes a session after a terminal 404/401. Deregister only
        after the owning account confirms that the endpoint is gone; a live
        endpoint must remain registered so it does not become an unmanaged VM.
        The caller holds the account lock.
        """
        if (
            session.endpoint is None
            or self.recorded_endpoint(session.config, session.name) is not None
        ):
            return
        try:
            assignments = self._server_sessions(session.account)
        except AccountChangerError as exc:
            self.notify(
                f"Colab removed the local record for {session.name!r}, but its "
                f"server state could not be verified ({exc}). Keeping the pool "
                "registration; run 'pool reconcile'."
            )
            return
        if any(endpoint == session.endpoint for endpoint, _ in assignments):
            self.notify(
                f"Colab removed the local record for {session.name!r}, but "
                f"endpoint {session.endpoint} is still assigned on account "
                f"{session.account!r}. Keeping the pool registration; run "
                "'pool reconcile' before stopping or recreating it."
            )
            return
        self._retire(session)
        self.notify(
            f"Session {session.name!r} vanished: endpoint {session.endpoint} is "
            f"no longer assigned on account {session.account!r}, so its pool "
            "registration and local Colab record were removed. Work on that VM "
            "is gone. Check 'pool reconcile' before deciding whether a "
            "replacement is needed."
        )

    def run(self, subcommand: str, name: str, args: Sequence[str] = ()) -> int:
        """Run ``colab <subcommand> -s NAME ...`` under the owning account.

        Standard input, output, and error are inherited, so pipes work and the
        Colab exit code is returned unchanged. If Colab prunes the local record
        after a terminal error, a confirmed server-side loss is also removed
        from the pool registry.
        """
        if subcommand in {"new", "stop", "sessions", "run"}:
            raise PoolError(
                f"'colab {subcommand}' is not a per-session command; use the "
                "matching 'pool' command instead."
            )
        with self.hold():
            session = self.get(name)
            self._require_account(session.account)
            self._activate(session.account)
            result = self._colab(
                session.config, [subcommand, "-s", session.name, *args]
            )
            if result.returncode != 0:
                self._handle_pruned_session(session)
            return result.returncode

    def stop(self, name: str, *, forget: bool = False) -> int:
        with self.hold():
            session = self.get(name)
            if forget:
                self._deregister(session.name)
                return 0
            if session.account not in self._profiles():
                raise PoolError(
                    f"Account {session.account!r} no longer exists. Use "
                    f"'pool stop {session.name} --forget' to deregister only."
                )
            self._activate(session.account)
            result = self._colab(session.config, ["stop", "-s", session.name])
            if result.returncode != 0:
                if session.endpoint is not None and self._confirmed_gone(
                    session.account, session.endpoint
                ):
                    self._retire(session)
                    self.notify(
                        f"'colab stop' failed, but endpoint {session.endpoint} is "
                        f"no longer assigned on account {session.account!r}; "
                        f"removed the stale registration of {session.name!r}."
                    )
                    return 0
                raise PoolError(
                    f"'colab stop' failed with exit code {result.returncode}; "
                    f"{session.name!r} is still registered. Retry, or use "
                    "--forget to deregister without stopping."
                )
            self._deregister(session.name)
            return 0

    def adopt(
        self,
        name: str,
        account: str,
        config: str | Path,
        *,
        force: bool = False,
        hardware: str | None = None,
    ) -> PoolSession:
        name = validate_session_name(name)
        path = Path(config).expanduser().resolve()
        with self.hold():
            account = self._require_account(account)
            existing = self.store.load()["sessions"].get(name)
            if existing is not None:
                raise PoolError(
                    f"Session {name!r} is already pooled on account "
                    f"{existing['account']!r}."
                )
            if not force and self.recorded_endpoint(path, name) is None:
                raise PoolError(
                    f"{path} has no Colab session record named {name!r}. "
                    "Use --force to register it anyway."
                )
            accelerator = None
            try:
                record = json.loads(path.read_text(encoding="utf-8")).get(name, {})
                accelerator = record.get("accelerator")
            except (OSError, ValueError, AttributeError):
                pass
            label = hardware or (
                "cpu" if accelerator in (None, "NONE") else str(accelerator).lower()
            )
            self._register(
                name,
                {
                    "account": account,
                    "config": str(path),
                    "hardware": label,
                    "created_at": _now(),
                    "adopted": True,
                },
            )
            return self.get(name)

    def import_legacy(
        self, source: str | Path, primary_file: str | Path | None = None
    ) -> ImportResult:
        """Adopt every session of a prototype ``pool.json`` (read-only)."""
        path = Path(source).expanduser()
        registry_path = path / "pool.json" if path.is_dir() else path
        try:
            entries = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PoolError(f"Could not read {registry_path}: {exc}") from exc
        if not isinstance(entries, dict):
            raise PoolError(f"{registry_path} is not a pool registry object.")
        imported: list[str] = []
        skipped: dict[str, str] = {}
        for name, entry in sorted(entries.items()):
            if not isinstance(entry, dict):
                skipped[str(name)] = "invalid entry"
                continue
            try:
                self.adopt(
                    name,
                    str(entry.get("account", "")),
                    str(entry.get("config", "")),
                    hardware=entry.get("hw") if entry.get("hw") != "adopted" else None,
                )
            except AccountChangerError as exc:
                skipped[name] = str(exc)
                continue
            imported.append(name)
        pin = (
            Path(primary_file).expanduser()
            if primary_file
            else registry_path.parent / ".colab-primary"
        )
        primary = None
        if pin.exists():
            try:
                candidate = pin.read_text(encoding="utf-8").strip()
                if candidate:
                    primary = self.set_primary(candidate)
            except (OSError, AccountChangerError) as exc:
                skipped[f"primary ({pin.name})"] = str(exc)
        return ImportResult(imported, skipped, primary)

    # Server inspection --------------------------------------------------

    def _server_sessions(self, account: str) -> list[tuple[str, str]]:
        """List (endpoint, hardware) on the server; the caller holds the lock.

        A throw-away state file guarantees no real session record is pruned.
        The result is either a trustworthy listing or a ``PoolError``: an empty
        listing is accepted only after the account's own usage endpoint
        independently reports zero assignments, because Colab CLI turns an
        authentication failure into "no sessions" when its state file is empty.
        """
        self._ensure_state_dir()
        self._activate(account)
        with tempfile.TemporaryDirectory(prefix=".probe-", dir=self.state_dir) as tmp:
            result = self._colab(
                Path(tmp) / "sessions.json",
                ["sessions"],
                capture="both",
                interactive=False,
            )
        if result.returncode != 0:
            raise PoolError(f"'colab sessions' failed (exit {result.returncode}).")
        found = []
        claimed_empty = False
        unparsed = 0
        for line in (result.stdout or "").splitlines():
            text = line.strip()
            match = SESSION_LINE_RE.match(text)
            if match:
                found.append((match["endpoint"], match["hardware"].strip().lower()))
            elif NO_SESSIONS_MESSAGE in text:
                claimed_empty = True
            elif "| Hardware:" in text:
                unparsed += 1
        if unparsed or (found and claimed_empty):
            raise PoolError(
                "'colab sessions' printed output in an unrecognized format; "
                "server state is unknown."
            )
        if found:
            return found
        if not claimed_empty:
            raise PoolError(
                "'colab sessions' printed neither session lines nor its "
                "no-sessions message; refusing to treat that as an empty server."
            )
        self._confirm_no_assignments(account)
        return []

    def _confirm_no_assignments(self, account: str) -> None:
        """Cross-check an empty listing with the account's ccu-info counter."""
        raw = self.manager.credentials.read_profile(account)
        if raw is None:
            raise PoolError(
                f"'colab sessions' listed nothing on account {account!r}, and the "
                "account has no saved credential to confirm it; server state is "
                "unknown."
            )
        for attempt in range(EMPTY_CONFIRM_ATTEMPTS):
            try:
                usage = self.manager.usage_service.fetch(raw)
            except AccountChangerError as exc:
                raise PoolError(
                    f"'colab sessions' listed nothing on account {account!r}, but "
                    f"that could not be cross-checked ({exc}); server state is "
                    "unknown (authentication or network failure?)."
                ) from exc
            if usage.assignments == 0:
                return
            if attempt + 1 < EMPTY_CONFIRM_ATTEMPTS:
                time.sleep(EMPTY_CONFIRM_RETRY_DELAY)
        raise PoolError(
            f"'colab sessions' listed nothing on account {account!r}, but Colab "
            f"usage reports {usage.assignments} active assignment(s); treating "
            "the listing as failed (authentication or network failure?)."
        )

    def reconcile(
        self, *, all_accounts: bool = False, refresh: bool = True
    ) -> ReconcileReport:
        with self.hold():
            sessions = self.sessions()
            profiles = self._profiles()
            accounts = {session.account for session in sessions}
            if all_accounts:
                accounts |= set(profiles)
            else:
                if refresh:
                    self.refresh_usage()
                    profiles = self._profiles()
                accounts |= {
                    name
                    for name, metadata in profiles.items()
                    if (metadata.get("usage") or {}).get("assignments", 0)
                }
            report = ReconcileReport(accounts=sorted(accounts))
            for account in report.accounts:
                mine = {
                    session.endpoint: session
                    for session in sessions
                    if session.account == account and session.endpoint
                }
                if account not in profiles:
                    report.errors[account] = "account is not a saved profile"
                    continue
                try:
                    listed = self._server_sessions(account)
                except AccountChangerError as exc:
                    report.errors[account] = str(exc)
                    continue
                endpoints = set()
                for endpoint, hardware in listed:
                    endpoints.add(endpoint)
                    owner = mine.get(endpoint)
                    report.server.append(
                        ServerSession(
                            account, endpoint, hardware, owner.name if owner else None
                        )
                    )
                report.vanished.extend(
                    session
                    for session in sessions
                    if session.account == account and session.endpoint not in endpoints
                )
            return report

    def prune_state(self, *, apply: bool = False) -> PruneStateReport:
        """Find (and with ``apply``, delete) records nobody can use any more.

        A record in ``pool-state/ACCOUNT.json`` that no pool registration
        points at is *stale* when a trustworthy probe of ACCOUNT shows its
        endpoint is unassigned, and *live* otherwise. Live records are never
        touched; accounts that cannot be probed are reported under ``errors``.
        """
        report = PruneStateReport()
        with self.hold():
            if not self.state_dir.is_dir():
                return report
            registered = {
                (Path(entry["config"]).resolve(), name)
                for name, entry in self.store.load()["sessions"].items()
            }
            profiles = self._profiles()
            for path in sorted(self.state_dir.glob("*.json")):
                if path.name.startswith("."):
                    continue
                account = path.stem
                resolved = path.resolve()
                candidates = [
                    StateRecord(account, name, endpoint)
                    for name, endpoint in sorted(self.read_records(path).items())
                    if (resolved, name) not in registered
                ]
                if not candidates:
                    continue
                if account not in profiles:
                    report.errors[account] = "account is not a saved profile"
                    continue
                try:
                    listed = {
                        endpoint for endpoint, _ in self._server_sessions(account)
                    }
                except AccountChangerError as exc:
                    report.errors[account] = str(exc)
                    continue
                for record in candidates:
                    if record.endpoint in listed:
                        report.live.append(record)
                        continue
                    report.stale.append(record)
                    if not apply:
                        continue
                    try:
                        if self._drop_record(path, record.name, record.endpoint):
                            report.removed.append(record)
                    except AccountChangerError as exc:
                        report.errors[f"{account}/{record.name}"] = str(exc)
        return report

    def reap(self, account: str, endpoint: str) -> None:
        """Stop an unpooled server-side assignment by endpoint.

        ``colab stop`` resolves a session from its state file, refreshes the
        proxy information from the server's assignment list, shuts the kernel
        down (ignoring failures), and unassigns the endpoint. A throw-away state
        file containing only the real endpoint and placeholder proxy fields is
        enough; if the endpoint is not assigned on this account, Colab prunes
        the placeholder and reports "not found" without unassigning anything.
        """
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", endpoint):
            raise PoolError(f"Invalid endpoint {endpoint!r}.")
        with self.hold():
            account = self._require_account(account)
            for session in self.sessions():
                if session.endpoint == endpoint:
                    raise PoolError(
                        f"Endpoint {endpoint} belongs to pooled session "
                        f"{session.name!r}; use 'pool stop {session.name}'."
                    )
            self._ensure_state_dir()
            self._activate(account)
            placeholder = f"pool-reap-{secrets.token_hex(4)}"
            with tempfile.TemporaryDirectory(
                prefix=".reap-", dir=self.state_dir
            ) as tmp:
                config = Path(tmp) / "sessions.json"
                write_private_json(
                    config,
                    {
                        placeholder: {
                            "name": placeholder,
                            "token": "placeholder",
                            "url": "http://127.0.0.1:9/",
                            "endpoint": endpoint,
                        }
                    },
                )
                result = self._colab(
                    config, ["stop", "-s", placeholder], capture="stdout"
                )
            if result.returncode != 0:
                raise PoolError(
                    f"'colab stop' failed with exit code {result.returncode}."
                )
            if "Session terminated" not in (result.stdout or ""):
                raise PoolError(
                    f"Endpoint {endpoint} is not assigned on account {account!r}."
                )

    def stop_all(self, *, max_passes: int = 3) -> StopAllResult:
        """Stop pooled and orphan instances, then verify server and local state."""
        if max_passes < 1:
            raise ValueError("max_passes must be at least 1")
        stopped_sessions: list[PoolSession] = []
        reaped_orphans: list[ServerSession] = []
        errors: dict[str, str] = {}
        with self.hold():
            for _ in range(max_passes):
                report = self.reconcile(all_accounts=True, refresh=False)
                registered = self.sessions()
                if not registered and not report.orphans and not report.errors:
                    break
                for session in registered:
                    key = f"session {session.name}"
                    try:
                        self.stop(session.name)
                    except AccountChangerError as exc:
                        errors[key] = str(exc)
                    else:
                        stopped_sessions.append(session)
                        errors.pop(key, None)
                for orphan in report.orphans:
                    key = f"endpoint {orphan.endpoint} on {orphan.account}"
                    try:
                        self.reap(orphan.account, orphan.endpoint)
                    except AccountChangerError as exc:
                        errors[key] = str(exc)
                    else:
                        reaped_orphans.append(orphan)
                        errors.pop(key, None)
            final = self.reconcile(all_accounts=True, refresh=False)
            remaining = self.sessions()
        return StopAllResult(
            final=final,
            remaining_sessions=remaining,
            stopped_sessions=stopped_sessions,
            reaped_orphans=reaped_orphans,
            errors=errors,
        )

    # Overview -----------------------------------------------------------

    def snapshot(self, *, refresh: bool = True) -> Snapshot:
        errors: dict[str, str] = {}
        if refresh:
            with self.hold():
                errors = self.refresh_usage()
        registry = self.store.load()
        config = self.manager.config.load()
        sessions = self.sessions()
        counts: dict[str, int] = {}
        for session in sessions:
            counts[session.account] = counts.get(session.account, 0) + 1
        accounts = []
        for profile in self.manager.profiles():
            accounts.append(
                AccountState(
                    name=profile.name,
                    email=profile.email,
                    usage=profile.usage,
                    usage_fetched_at=profile.usage_fetched_at,
                    stored=profile.stored,
                    sessions=counts.get(profile.name, 0),
                    is_primary=profile.name == registry["primary"],
                    is_active=profile.is_active,
                )
            )
        return Snapshot(
            primary=registry["primary"],
            threshold=registry["threshold"],
            active=config["active_profile"],
            sessions=sessions,
            accounts=accounts,
            errors=errors,
            usage_refreshed=refresh,
        )


def snapshot_usages(snapshot: Snapshot) -> list[Usage | None]:
    """Return per-account values eligible for the aggregate total.

    Live snapshots exclude stale values from failed refreshes and profiles whose
    saved credential is missing. Cached snapshots intentionally use every cache.
    """
    return [
        account.usage
        if not snapshot.usage_refreshed
        or (account.stored and account.name not in snapshot.errors)
        else None
        for account in snapshot.accounts
    ]


def snapshot_json(
    snapshot: Snapshot, server_report: ReconcileReport | None = None
) -> dict[str, Any]:
    usages = snapshot_usages(snapshot)
    total = total_usage(usages)
    result = {
        "primary": snapshot.primary,
        "usage_mode": "live" if snapshot.usage_refreshed else "cached",
        "threshold": snapshot.threshold,
        "active": snapshot.active,
        "sessions": [asdict(session) for session in snapshot.sessions],
        "accounts": [
            {
                "name": account.name,
                "email": account.email,
                "balance": account.usage.balance if account.usage else None,
                "hourly_rate": account.usage.hourly_rate if account.usage else None,
                "hours_remaining": (
                    account.usage.hours_remaining if account.usage else None
                ),
                "assignments": account.usage.assignments if account.usage else None,
                "usage_fetched_at": account.usage_fetched_at,
                "usage_in_total": included_usage is not None,
                "stored": account.stored,
                "pool_sessions": account.sessions,
                "primary": account.is_primary,
                "active": account.is_active,
            }
            for account, included_usage in zip(snapshot.accounts, usages, strict=True)
        ],
        "total": {
            "balance": total.balance if total else None,
            "hourly_rate": total.hourly_rate if total else None,
            "hours_remaining": total.hours_remaining if total else None,
            "assignments": total.assignments if total else None,
            "accounts_with_usage": sum(usage is not None for usage in usages),
            "account_count": len(usages),
            "complete": bool(usages) and all(usage is not None for usage in usages),
        },
        "errors": snapshot.errors,
    }
    if server_report is not None:
        result["server"] = [asdict(entry) for entry in server_report.server]
        result["orphans"] = [asdict(entry) for entry in server_report.orphans]
        result["vanished"] = [asdict(session) for session in server_report.vanished]
        result["server_inspection"] = {
            "accounts": server_report.accounts,
            "accounts_succeeded": len(server_report.accounts)
            - len(server_report.errors),
            "complete": not server_report.errors,
            "errors": server_report.errors,
        }
    return result
