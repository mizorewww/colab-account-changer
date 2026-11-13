"""Transactional multi-account operations for google-colab-cli."""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from colab_account_changer.errors import AccountChangerError, AccountError
from colab_account_changer.storage import (
    ConfigStore,
    CredentialStore,
    validate_profile_name,
)
from colab_account_changer.tokens import inspect_token, token_fingerprint


@dataclass(frozen=True)
class Profile:
    name: str
    email: str | None
    created_at: str
    updated_at: str
    token_sha256: str
    is_active: bool = False
    stored: bool = True


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
        colab_command: str = "colab",
        legacy_token_path: Callable[[], Path] | None = None,
    ) -> None:
        self.config = config_store or ConfigStore()
        self.credentials = credentials or CredentialStore()
        self.runner = runner or subprocess.run
        self.colab_command = colab_command
        self.legacy_token_path = legacy_token_path or (
            lambda: Path.home() / ".config" / "colab-cli" / "token.json"
        )

    @contextmanager
    def _locked(self):
        self.config.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = FileLock(self.config.path.with_suffix(".lock"), timeout=10)
        try:
            with lock:
                yield
        except Timeout as exc:
            raise AccountError(
                "Another account operation is still running. Try again later."
            ) from exc

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
    def _profile(name: str, metadata: dict[str, Any], **extra: bool) -> Profile:
        return Profile(
            name=name,
            email=metadata.get("email"),
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            token_sha256=metadata["token_sha256"],
            **extra,
        )

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

    def save(
        self,
        name: str | None = None,
        *,
        email: str | None = None,
        verify: bool = True,
        force: bool = False,
    ) -> Profile:
        with self._locked():
            return self._save_unlocked(
                name, email=email, verify=verify, force=force
            )

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
        previous_raw = self.credentials.read_profile(name) if existing else None
        self.credentials.write_profile(name, inspected.token_json)
        updated = copy.deepcopy(config)
        updated["profiles"][name] = {
            "email": resolved_email,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "token_sha256": token_fingerprint(inspected.token_json),
        }
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
        with self._locked():
            config = self.config.load()
            if name not in config["profiles"]:
                raise AccountError(f"Profile {name!r} does not exist.")
            original = self.credentials.read_colab()
            active = config["active_profile"]

            if original is not None and active == name:
                # Avoid replacing a newly refreshed upstream token with an older copy.
                if save_current:
                    self._save_unlocked(
                        name, email=None, verify=verify, force=False
                    )
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
                if not save_current:
                    raise AccountError(
                        "Saving the current profile was disabled. Re-run without "
                        "--no-save, or explicitly pass --discard-unsaved if the "
                        "current credential is disposable."
                    )
                if active is None:
                    raise AccountError(
                        "The current Colab credential is not tracked. Please save "
                        "it first with 'colab-account save NAME'."
                    )
                self._save_unlocked(
                    active, email=None, verify=verify, force=False
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
                self._restore_colab(original, exc)
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
        with self._locked():
            config = self.config.load()
            if name in config["profiles"]:
                raise AccountError(f"Profile {name!r} already exists.")
            original = self.credentials.read_colab()
            if original is not None:
                active = config["active_profile"]
                if active is None:
                    raise AccountError(
                        "The current Colab credential is not tracked. Please save "
                        "it first with 'colab-account save NAME'."
                    )
                self._save_unlocked(
                    active, email=None, verify=verify, force=False
                )
            legacy = self.legacy_token_path()
            if legacy.exists():
                raise AccountError(
                    f"A legacy token exists at {legacy}. Migrate it with the "
                    "installed google-colab-cli version before adding an account; "
                    "the file was left untouched."
                )

            created_profile = False
            try:
                self.credentials.delete_colab()
                try:
                    result = self.runner(
                        [self.colab_command, "--auth=oauth2", "whoami"],
                        check=False,
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
                raw = self.credentials.read_colab()
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
                return self._profile(
                    name, updated["profiles"][name], is_active=True
                )
            except BaseException as exc:
                self._restore_colab(original, exc)
                if created_profile:
                    self.credentials.delete_profile(name)
                raise

    def remove(self, name: str) -> Profile:
        name = self._name(name)
        with self._locked():
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

    def _restore_colab(self, original: str | None, error: BaseException) -> None:
        try:
            if original is None:
                self.credentials.delete_colab()
            else:
                self.credentials.write_colab(original)
        except AccountChangerError:
            raise AccountError(
                "The account operation failed and the previous Colab credential "
                "could not be restored automatically. Its saved profile is still "
                "in the OS keyring; use 'colab-account list' and restore it."
            ) from error
