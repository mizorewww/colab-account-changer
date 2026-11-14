"""Transactional multi-account operations for google-colab-cli."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, ReadWriteLock, Timeout

from colab_account_changer.errors import (
    AccountChangerError,
    AccountError,
    LockTimeoutError,
    TokenError,
)
from colab_account_changer.storage import (
    ConfigStore,
    CredentialStore,
    validate_profile_name,
    write_private_json,
)
from colab_account_changer.tokens import (
    inspect_token,
    shares_refresh_token,
    token_fingerprint,
)
from colab_account_changer.usage import Usage, UsageService

HOLDERS_DIRECTORY = "lock-holders"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Profile:
    name: str
    email: str | None
    created_at: str
    updated_at: str
    token_sha256: str
    is_active: bool = False
    stored: bool = True
    source: str = "manual"
    usage: Usage | None = None
    usage_fetched_at: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    profiles: list[Profile]
    imported: list[Profile]
    gcloud_accounts: list[str]
    errors: list[str]


@dataclass(frozen=True)
class CurrentStatus:
    active_profile: str | None
    email: str | None
    has_credential: bool
    matches_saved: bool


class AccountManager:
    """Coordinate the upstream credential slot and named keyring profiles."""

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        credentials: CredentialStore | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        gcloud_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        usage_service: UsageService | None = None,
        colab_command: str = "colab",
        legacy_token_path: Callable[[], Path] | None = None,
    ) -> None:
        self.config = config_store or ConfigStore()
        self.credentials = credentials or CredentialStore()
        self.runner = runner or subprocess.run
        self.gcloud_runner = gcloud_runner or subprocess.run
        self.usage_service = usage_service or UsageService()
        self.colab_command = colab_command
        self.legacy_token_path = legacy_token_path or (
            lambda: Path.home() / ".config" / "colab-cli" / "token.json"
        )
        self._file_lock: FileLock | None = None
        self._rw_lock: ReadWriteLock | None = None
        self._mode: str | None = None
        self._holder_started: str | None = None

    @property
    def lock_path(self) -> Path:
        return self.config.path.with_suffix(".lock")

    @property
    def lock_mode(self) -> str | None:
        """``"shared"``/``"exclusive"`` while this manager holds the lock."""
        return self._mode

    @property
    def rw_lock_path(self) -> Path:
        return self.config.path.with_suffix(".rwlock")

    @property
    def holders_path(self) -> Path:
        return self.config.path.parent / HOLDERS_DIRECTORY

    def _ensure_locks(self) -> None:
        self.config.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._file_lock is None:
            self._file_lock = FileLock(self.lock_path)
        if self._rw_lock is None:
            self._rw_lock = ReadWriteLock(self.rw_lock_path, is_singleton=False)

    @contextmanager
    def exclusive(
        self,
        timeout: float = 10.0,
        message: str | None = None,
        label: str | None = None,
    ):
        """Hold the account lock exclusively; re-entrant within this manager.

        Callers (such as the resource pool) may hold it across several account
        operations and subprocesses; nested manager calls reuse the same lock
        instead of deadlocking on a second file descriptor. See ``access`` for
        how shared holders and the writer-first queue work.
        """
        with self.access(timeout=timeout, message=message, label=label):
            yield

    @contextmanager
    def access(
        self,
        can_share: Callable[[], bool] | None = None,
        *,
        timeout: float = 10.0,
        message: str | None = None,
        label: str | None = None,
    ):
        """Hold the account lock, shared when ``can_share()`` allows it.

        Two locks cooperate:

        - ``config.lock`` (a ``FileLock``) is the queue: every exclusive holder
          keeps it for its whole operation, and every caller passes through it.
        - ``config.rwlock`` (a filelock ``ReadWriteLock``) separates shared
          holders, which may run concurrently, from an exclusive holder.

        A caller takes the queue lock, then evaluates ``can_share`` (which may
        read the configuration safely). Shared callers take the read lock and
        leave the queue at once. Exclusive callers keep the queue lock while
        they wait for the write lock, so later callers line up behind them:
        a waiting writer cannot be starved by a stream of shared holders.

        Yields ``"shared"`` or ``"exclusive"``. Nested calls inside an
        exclusive hold yield ``"exclusive"`` without locking again.
        """
        if self._mode == "shared":
            raise RuntimeError("The account lock is held shared; release it first.")
        if self._mode == "exclusive":
            yield "exclusive"
            return
        self._ensure_locks()
        assert self._file_lock is not None and self._rw_lock is not None
        deadline = time.monotonic() + timeout
        try:
            self._file_lock.acquire(timeout=timeout)
        except Timeout as exc:
            raise self._busy(timeout, message) from exc
        queued = True
        holder: Path | None = None
        try:
            shared = can_share is not None and can_share()
            mode = "shared" if shared else "exclusive"
            holder = self._write_holder(mode, label, waiting=not shared)
            remaining = max(0.0, deadline - time.monotonic())
            try:
                if shared:
                    self._rw_lock.acquire_read(timeout=remaining)
                else:
                    self._rw_lock.acquire_write(timeout=remaining)
            except Timeout as exc:
                raise self._busy(timeout, message) from exc
            try:
                if shared:
                    self._file_lock.release()
                    queued = False
                else:
                    self._write_holder(mode, label, waiting=False, path=holder)
                self._mode = mode
                yield mode
            finally:
                self._mode = None
                self._rw_lock.release()
        finally:
            if holder is not None:
                holder.unlink(missing_ok=True)
            if queued:
                self._file_lock.release()

    def _write_holder(
        self,
        mode: str,
        label: str | None,
        *,
        waiting: bool,
        path: Path | None = None,
    ) -> Path:
        """Describe this holder in a side file (never any secret)."""
        if path is None:
            path = self.holders_path / f"{os.getpid()}-{secrets.token_hex(4)}.json"
            self._holder_started = self._now()
        # Diagnostics only; never fail an operation over them.
        with suppress(AccountChangerError):
            write_private_json(
                path,
                {
                    "pid": os.getpid(),
                    "mode": mode,
                    "waiting": waiting,
                    "label": label or "account operation",
                    "started_at": self._holder_started,
                },
                manage_parent_permissions=True,
            )
        return path

    def lock_holders(self) -> list[dict[str, Any]]:
        """Live holders (and queued exclusive waiters) of the account lock."""
        holders = []
        for path in sorted(self.holders_path.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                pid = int(value["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not _process_alive(pid):
                path.unlink(missing_ok=True)
                continue
            try:
                started = datetime.fromisoformat(value["started_at"])
                seconds = max(0.0, (datetime.now(UTC) - started).total_seconds())
            except (KeyError, TypeError, ValueError):
                seconds = None
            holders.append(
                {
                    "pid": pid,
                    "mode": str(value.get("mode")),
                    "waiting": bool(value.get("waiting")),
                    "label": str(value.get("label")),
                    "started_at": value.get("started_at"),
                    "held_seconds": seconds,
                }
            )
        return holders

    def describe_holders(self) -> str:
        holders = [
            holder for holder in self.lock_holders() if holder["pid"] != os.getpid()
        ]
        if not holders:
            return "The current holder is unknown (no holder information)."
        parts = []
        for holder in holders:
            state = (
                "waiting for shared holders to finish"
                if holder["waiting"]
                else f"held {holder['mode']}"
            )
            seconds = holder["held_seconds"]
            age = "" if seconds is None else f" for {seconds:.0f}s"
            parts.append(f"pid {holder['pid']} '{holder['label']}' ({state}{age})")
        return "Current lock holders: " + "; ".join(parts) + "."

    def _busy(self, timeout: float, message: str | None) -> LockTimeoutError:
        return LockTimeoutError(
            f"{message or 'Another account operation is still running.'} "
            f"The pool is busy: no lock within {timeout:g}s. "
            f"{self.describe_holders()} This does not mean any Colab session "
            "was lost; do not recreate sessions. Retry later (exit code 75)."
        )

    @contextmanager
    def _locked(self, label: str | None = None):
        with self.exclusive(label=label):
            yield

    @staticmethod
    def _name(name: str) -> str:
        try:
            return validate_profile_name(name)
        except ValueError as exc:
            raise AccountError(str(exc)) from exc

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _profile(name: str, metadata: dict[str, Any], **extra: Any) -> Profile:
        cached = metadata.get("usage")
        usage = (
            Usage(
                balance=cached["balance"],
                hourly_rate=cached["hourly_rate"],
                assignments=cached["assignments"],
            )
            if cached
            else None
        )
        return Profile(
            name=name,
            email=metadata.get("email"),
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            token_sha256=metadata["token_sha256"],
            source=metadata.get("source", "manual"),
            usage=usage,
            usage_fetched_at=cached.get("fetched_at") if cached else None,
            **extra,
        )

    def discover(
        self, *, verify: bool = True, include_gcloud: bool = True
    ) -> DiscoveryResult:
        """Import existing keyring/legacy tokens without changing Colab's login."""
        with self._locked("colab-account discover"):
            discovered: list[Profile] = []
            imported: list[Profile] = []
            errors: list[str] = []
            keyring_raw = self.credentials.read_keyring_colab()
            legacy_raw = self.credentials.read_legacy()
            sources = []
            if keyring_raw is not None:
                sources.append(("keyring", keyring_raw, True))
            if legacy_raw is not None and legacy_raw != keyring_raw:
                sources.append(("legacy", legacy_raw, keyring_raw is None))

            for source, raw, is_active in sources:
                config = self.config.load()
                digest = token_fingerprint(raw)
                active = config["active_profile"]
                existing_name = (
                    active
                    if active and config["profiles"][active]["token_sha256"] == digest
                    else next(
                        (
                            name
                            for name, metadata in config["profiles"].items()
                            if metadata["token_sha256"] == digest
                        ),
                        None,
                    )
                )
                if existing_name is not None:
                    metadata = config["profiles"][existing_name]
                    # A token first imported offline still needs one online
                    # identity lookup, even though its digest has not changed.
                    if metadata.get("email") or not verify:
                        if self.credentials.read_profile(existing_name) != raw or (
                            is_active and config["active_profile"] != existing_name
                        ):
                            profile = self._store_discovered(
                                existing_name,
                                raw,
                                metadata.get("email"),
                                source,
                                is_active,
                            )
                        else:
                            profile = self._profile(
                                existing_name,
                                metadata,
                                is_active=is_active
                                or config["active_profile"] == existing_name,
                            )
                        discovered.append(profile)
                        continue

                try:
                    inspected = inspect_token(raw, verify=verify)
                except TokenError as exc:
                    errors.append(f"Could not import {source} credential: {exc}")
                    continue
                # Only Google's verified identity may drive email-based merging.
                # The optional JSON 'account' field is not an identity proof.
                email = inspected.identity.email
                by_email = next(
                    (
                        name
                        for name, metadata in config["profiles"].items()
                        if email
                        and metadata.get("email")
                        and metadata["email"].casefold() == email.casefold()
                    ),
                    None,
                )
                if by_email is not None and not is_active:
                    if all(profile.name != by_email for profile in discovered):
                        discovered.append(
                            self._profile(
                                by_email,
                                config["profiles"][by_email],
                                is_active=config["active_profile"] == by_email,
                            )
                        )
                    continue
                if by_email is not None:
                    name = by_email
                elif existing_name is not None:
                    name = existing_name
                elif (
                    not email
                    and is_active
                    and config["active_profile"]
                    and self.credentials.read_profile(config["active_profile"])
                    and shares_refresh_token(
                        raw,
                        self.credentials.read_profile(config["active_profile"]),
                    )
                ):
                    name = config["active_profile"]
                else:
                    name = self._available_alias(
                        email or ("current" if source == "keyring" else "legacy"),
                        config["profiles"],
                    )
                was_existing = name in config["profiles"]
                profile = self._store_discovered(
                    name, inspected.token_json, email, source, is_active
                )
                discovered.append(profile)
                if not was_existing:
                    imported.append(profile)
            gcloud_accounts = self._read_gcloud_accounts() if include_gcloud else []
            return DiscoveryResult(discovered, imported, gcloud_accounts, errors)

    @staticmethod
    def _available_alias(label: str, profiles: dict[str, Any]) -> str:
        local_part = label.split("@", 1)[0].casefold()
        base = re.sub(r"[^a-z0-9._-]+", "-", local_part).strip("._-")
        base = (base or "account")[:48].strip("._-") or "account"
        candidate = base
        number = 2
        while candidate in profiles:
            suffix = f"-{number}"
            candidate = f"{base[: 64 - len(suffix)]}{suffix}"
            number += 1
        return candidate

    def _store_discovered(
        self,
        name: str,
        raw: str,
        email: str | None,
        source: str,
        is_active: bool,
    ) -> Profile:
        config = self.config.load()
        existing = config["profiles"].get(name)
        previous_raw = self.credentials.read_profile(name) if existing else None
        self.credentials.write_profile(name, raw)
        updated = copy.deepcopy(config)
        now = self._now()
        updated["profiles"][name] = {
            "email": email or (existing.get("email") if existing else None),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "token_sha256": token_fingerprint(raw),
            "source": source,
        }
        if existing and existing.get("usage"):
            updated["profiles"][name]["usage"] = copy.deepcopy(existing["usage"])
        if is_active:
            updated["active_profile"] = name
        try:
            self.config.save(updated)
        except BaseException:
            self._restore_profile(name, previous_raw, existed=existing is not None)
            raise
        return self._profile(
            name,
            updated["profiles"][name],
            is_active=updated["active_profile"] == name,
        )

    def _read_gcloud_accounts(self) -> list[str]:
        try:
            result = self.gcloud_runner(
                ["gcloud", "auth", "list", "--format=json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return []
            records = json.loads(result.stdout)
            if not isinstance(records, list):
                return []
            return list(
                dict.fromkeys(
                    record["account"]
                    for record in records
                    if isinstance(record, dict)
                    and isinstance(record.get("account"), str)
                    and record["account"]
                )
            )
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return []

    def profiles(self) -> list[Profile]:
        config = self.config.load()
        return [
            self._profile(
                name,
                metadata,
                is_active=name == config["active_profile"],
                stored=self.credentials.read_profile(name) is not None,
            )
            for name, metadata in sorted(config["profiles"].items())
        ]

    def status(self) -> CurrentStatus:
        config = self.config.load()
        active = config["active_profile"]
        raw = self.credentials.read_colab()
        email = config["profiles"][active]["email"] if active else None
        if not raw:
            return CurrentStatus(active, email, False, False)
        if not active:
            return CurrentStatus(None, None, True, False)
        saved = self.credentials.read_profile(active)
        return CurrentStatus(active, email, True, saved == raw)

    def usage(self, name: str | None = None) -> Usage:
        """Fetch and cache usage without modifying the active credential."""
        with self._locked("colab-account usage"):
            config = self.config.load()
            name = name or config["active_profile"]
            if name is None:
                raw = self.credentials.read_colab()
                if raw is None:
                    raise AccountError("No active Colab OAuth credential was found.")
                return self.usage_service.fetch(raw)
            name = self._name(name)
            if name not in config["profiles"]:
                raise AccountError(f"Profile {name!r} does not exist.")
            raw = self.credentials.read_profile(name)
            if raw is None:
                raise AccountError(f"Profile {name!r} has no saved credential.")
            usage = self.usage_service.fetch(raw)
            updated = copy.deepcopy(config)
            updated["profiles"][name]["usage"] = {
                "balance": usage.balance,
                "hourly_rate": usage.hourly_rate,
                "assignments": usage.assignments,
                "fetched_at": self._now(),
            }
            self.config.save(updated)
            return usage

    def save(
        self,
        name: str | None = None,
        *,
        email: str | None = None,
        verify: bool = True,
        force: bool = False,
    ) -> Profile:
        with self._locked("colab-account save"):
            return self._save_unlocked(name, email=email, verify=verify, force=force)

    def _save_unlocked(
        self,
        name: str | None,
        *,
        email: str | None,
        verify: bool,
        force: bool,
    ) -> Profile:
        config = self.config.load()
        name = self._name(name or config["active_profile"] or "")
        raw = self.credentials.read_colab()
        if raw is None:
            raise AccountError(
                "No active Colab OAuth credential was found. Run 'colab "
                "--auth=oauth2 whoami' to sign in first."
            )
        inspected = inspect_token(raw, verify=verify)
        existing = config["profiles"].get(name)
        previous_raw = self.credentials.read_profile(name) if existing else None
        if existing and not verify and not force:
            if previous_raw is None:
                unverified_change = (
                    token_fingerprint(inspected.token_json) != existing["token_sha256"]
                )
            else:
                unverified_change = (
                    inspected.token_json != previous_raw
                    and not shares_refresh_token(inspected.token_json, previous_raw)
                )
            if unverified_change:
                raise AccountError(
                    "Offline mode cannot verify that this credential belongs to "
                    f"profile {name!r}. Save online to verify its identity, or use "
                    "--force to replace the saved credential."
                )
        verified_email = inspected.identity.email
        if email and verified_email and email.casefold() != verified_email.casefold():
            raise AccountError(
                f"The supplied email does not match the verified identity "
                f"({verified_email})."
            )
        resolved_email = verified_email or email
        if resolved_email is None and existing:
            resolved_email = existing.get("email")
        if (
            existing
            and existing.get("email")
            and resolved_email
            and existing["email"].casefold() != resolved_email.casefold()
            and not force
        ):
            raise AccountError(
                f"Profile {name!r} belongs to a different Google identity. "
                "Use --force to replace its saved credential."
            )

        now = self._now()
        self.credentials.write_profile(name, inspected.token_json)
        updated = copy.deepcopy(config)
        updated["profiles"][name] = {
            "email": resolved_email,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "token_sha256": token_fingerprint(inspected.token_json),
        }
        if existing and not force and existing.get("usage"):
            updated["profiles"][name]["usage"] = copy.deepcopy(existing["usage"])
        updated["active_profile"] = name
        try:
            self.config.save(updated)
        except BaseException:
            self._restore_profile(name, previous_raw, existed=existing is not None)
            raise
        return self._profile(
            name,
            updated["profiles"][name],
            is_active=True,
            stored=True,
        )

    def use(
        self,
        name: str,
        *,
        save_current: bool = True,
        discard_unsaved: bool = False,
        verify: bool = True,
    ) -> Profile:
        name = self._name(name)
        with self._locked("colab-account use"):
            config = self.config.load()
            if name not in config["profiles"]:
                raise AccountError(f"Profile {name!r} does not exist.")
            original_keyring = self.credentials.read_keyring_colab()
            original = original_keyring or self.credentials.read_legacy()
            original_legacy_only = original_keyring is None and original is not None
            active = config["active_profile"]

            if original is not None and active == name:
                # Avoid replacing a newly refreshed upstream token with an older copy.
                if save_current:
                    self._save_unlocked(name, email=None, verify=verify, force=False)
                return self._profile(
                    name, self.config.load()["profiles"][name], is_active=True
                )

            target_raw = self.credentials.read_profile(name)
            if target_raw is None:
                raise AccountError(
                    f"Profile {name!r} has no saved credential in the OS keyring."
                )
            inspected = inspect_token(target_raw, verify=verify)
            expected_email = config["profiles"][name].get("email")
            if (
                inspected.identity.email
                and expected_email
                and inspected.identity.email.casefold() != expected_email.casefold()
            ):
                raise AccountError(
                    f"Profile {name!r} no longer matches its recorded Google "
                    "identity. Inspect and save it again before switching."
                )

            if original is not None and not discard_unsaved:
                if active is None:
                    raise AccountError(
                        "The current Colab credential is not tracked. Please save "
                        "it first with 'colab-account save NAME'."
                    )
                if save_current:
                    self._save_unlocked(active, email=None, verify=verify, force=False)
                elif self.credentials.read_profile(active) is None:
                    raise AccountError(
                        "The current profile has no saved credential. Save it "
                        "first, or explicitly pass --discard-unsaved."
                    )

            # If inspection refreshed the target, persist that refreshed copy first.
            if inspected.token_json != target_raw:
                self._save_refreshed_target(name, inspected.token_json)

            try:
                self.credentials.write_colab(inspected.token_json)
                if self.credentials.read_colab() != inspected.token_json:
                    raise AccountError(
                        "Keyring verification failed after writing the target "
                        "credential; the previous account was restored."
                    )
                updated = self.config.load()
                updated["active_profile"] = name
                self.config.save(updated)
            except BaseException as exc:
                self._restore_colab(original, exc, legacy_only=original_legacy_only)
                raise
            return self._profile(
                name, self.config.load()["profiles"][name], is_active=True
            )

    def _save_refreshed_target(self, name: str, token_json: str) -> None:
        previous = self.credentials.read_profile(name)
        config = self.config.load()
        self.credentials.write_profile(name, token_json)
        updated = copy.deepcopy(config)
        updated["profiles"][name]["token_sha256"] = token_fingerprint(token_json)
        updated["profiles"][name]["updated_at"] = self._now()
        try:
            self.config.save(updated)
        except BaseException:
            self._restore_profile(name, previous, existed=previous is not None)
            raise

    def add(self, name: str, *, verify: bool = True) -> Profile:
        """Authenticate a new account, restoring the old one on every failure."""
        name = self._name(name)
        with self._locked("colab-account add"):
            config = self.config.load()
            if name in config["profiles"]:
                raise AccountError(f"Profile {name!r} already exists.")
            original_keyring = self.credentials.read_keyring_colab()
            original = original_keyring or self.credentials.read_legacy()
            original_legacy_only = original_keyring is None and original is not None
            if original is not None:
                active = config["active_profile"]
                if active is None:
                    raise AccountError(
                        "The current Colab credential is not tracked. Please save "
                        "it first with 'colab-account save NAME'."
                    )
                self._save_unlocked(active, email=None, verify=verify, force=False)
            legacy = self.legacy_token_path()

            created_profile = False
            try:
                self.credentials.delete_colab()
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="colab-account-auth-"
                    ) as isolated_home:
                        runner_options: dict[str, Any] = {}
                        if legacy.exists():
                            # Upstream migrates ~/.config/colab-cli/token.json when
                            # its keyring is empty. Hide that file during new login
                            # without moving, modifying, or deleting the original.
                            environment = os.environ.copy()
                            environment["HOME"] = isolated_home
                            environment["USERPROFILE"] = isolated_home
                            runner_options["env"] = environment
                        result = self.runner(
                            [self.colab_command, "--auth=oauth2", "whoami"],
                            check=False,
                            **runner_options,
                        )
                except FileNotFoundError as exc:
                    raise AccountError(
                        f"Colab CLI executable {self.colab_command!r} was not found."
                    ) from exc
                if result.returncode != 0:
                    raise AccountError(
                        f"Colab authentication failed with exit code "
                        f"{result.returncode}; the previous account was restored."
                    )
                raw = self.credentials.read_keyring_colab()
                if raw is None:
                    raise AccountError(
                        "Colab authentication did not create an OAuth credential."
                    )
                inspected = inspect_token(raw, verify=verify)
                self.credentials.write_profile(name, inspected.token_json)
                created_profile = True
                if inspected.token_json != raw:
                    self.credentials.write_colab(inspected.token_json)
                    if self.credentials.read_colab() != inspected.token_json:
                        raise AccountError("Could not verify the refreshed credential.")
                updated = self.config.load()
                now = self._now()
                updated["profiles"][name] = {
                    "email": inspected.identity.email,
                    "created_at": now,
                    "updated_at": now,
                    "token_sha256": token_fingerprint(inspected.token_json),
                }
                updated["active_profile"] = name
                self.config.save(updated)
                return self._profile(name, updated["profiles"][name], is_active=True)
            except BaseException as exc:
                self._restore_colab(original, exc, legacy_only=original_legacy_only)
                if created_profile:
                    self.credentials.delete_profile(name)
                raise

    def remove(self, name: str) -> Profile:
        name = self._name(name)
        with self._locked("colab-account remove"):
            config = self.config.load()
            if name not in config["profiles"]:
                raise AccountError(f"Profile {name!r} does not exist.")
            if config["active_profile"] == name:
                raise AccountError(
                    "Cannot remove the active profile. Switch to another account "
                    "first; the active Colab credential was not changed."
                )
            raw = self.credentials.read_profile(name)
            profile = self._profile(
                name, config["profiles"][name], stored=raw is not None
            )
            self.credentials.delete_profile(name)
            updated = copy.deepcopy(config)
            del updated["profiles"][name]
            try:
                self.config.save(updated)
            except BaseException:
                if raw is not None:
                    self.credentials.write_profile(name, raw)
                raise
            return profile

    def _restore_profile(self, name: str, raw: str | None, *, existed: bool) -> None:
        if existed and raw is not None:
            self.credentials.write_profile(name, raw)
        else:
            self.credentials.delete_profile(name)

    def _restore_colab(
        self,
        original: str | None,
        error: BaseException,
        *,
        legacy_only: bool = False,
    ) -> None:
        try:
            if original is None or legacy_only:
                self.credentials.delete_colab()
            else:
                self.credentials.write_colab(original)
        except AccountChangerError:
            raise AccountError(
                "The account operation failed and the previous Colab credential "
                "could not be restored automatically. Its saved profile is still "
                "in the OS keyring; use 'colab-account list' and restore it."
            ) from error
