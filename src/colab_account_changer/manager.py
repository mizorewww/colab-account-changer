"""Transactional multi-account operations for google-colab-cli."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from colab_account_changer.errors import AccountChangerError, AccountError, TokenError
from colab_account_changer.storage import (
    ConfigStore,
    CredentialStore,
    validate_profile_name,
)
from colab_account_changer.tokens import (
    inspect_token,
    shares_refresh_token,
    token_fingerprint,
)
from colab_account_changer.usage import Usage, UsageService


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
        with self._locked():
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
        with self._locked():
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
        with self._locked():
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
        with self._locked():
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
        with self._locked():
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
