"""Keyring credentials and non-secret local account metadata."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from colab_account_changer.errors import ConfigError, CredentialStoreError

COLAB_SERVICE = "google-colab-cli"
COLAB_USERNAME = "oauth_token"
VAULT_SERVICE = "colab-account-changer"
CONFIG_ENV = "COLAB_ACCOUNT_CHANGER_CONFIG"
PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DEFAULT_LEGACY_TOKEN_PATH = Path.home() / ".config" / "colab-cli" / "token.json"


def validate_profile_name(name: str) -> str:
    """Return a safe keyring/config key, or reject it."""
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError(
            "Profile names must be 1-64 characters: letters, digits, '.', '_', "
            "or '-', starting with a letter or digit."
        )
    return name


def default_config() -> dict[str, Any]:
    return {"version": 1, "active_profile": None, "profiles": {}}


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return config_home / "colab-account-changer" / "config.json"


class ConfigStore:
    """Persist only aliases, email labels, timestamps, and token digests."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self._manage_parent_permissions = path is None and not os.environ.get(
            CONFIG_ENV
        )

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_config()
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid configuration at {self.path}: {exc}") from exc
        self._validate(config)
        return config

    def save(self, config: dict[str, Any]) -> None:
        self._validate(config)
        temporary_path: Path | None = None
        try:
            parent_existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix" and (
                not parent_existed or self._manage_parent_permissions
            ):
                self.path.parent.chmod(0o700)
            descriptor, filename = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            temporary_path = Path(filename)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(config, file, indent=2, sort_keys=True, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if os.name == "posix":
                temporary_path.chmod(0o600)
            # The temporary file is already owner-only. After replace succeeds,
            # do not perform fallible operations that would report a false failure
            # for a configuration that has actually been committed.
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise ConfigError(
                f"Could not save configuration {self.path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate(config: Any) -> None:
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ConfigError("Unsupported configuration format.")
        profiles = config.get("profiles")
        active = config.get("active_profile")
        if not isinstance(profiles, dict):
            raise ConfigError("Configuration 'profiles' must be an object.")
        if active is not None and not isinstance(active, str):
            raise ConfigError(
                "Configuration 'active_profile' must be a string or null."
            )
        if active is not None and active not in profiles:
            raise ConfigError("The active profile does not exist in the configuration.")
        for name, profile in profiles.items():
            try:
                validate_profile_name(name)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"Invalid profile name: {name!r}") from exc
            if not isinstance(profile, dict):
                raise ConfigError(f"Profile {name!r} has invalid metadata.")
            email = profile.get("email")
            if "email" not in profile or (
                email is not None and not isinstance(email, str)
            ):
                raise ConfigError(f"Profile {name!r} has an invalid email.")
            digest = profile.get("token_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ConfigError(f"Profile {name!r} has an invalid token digest.")
            for field in ("created_at", "updated_at"):
                if not isinstance(profile.get(field), str):
                    raise ConfigError(f"Profile {name!r} is missing {field}.")
            usage = profile.get("usage")
            if usage is not None:
                if not isinstance(usage, dict):
                    raise ConfigError(f"Profile {name!r} has invalid usage cache.")
                for field in ("balance", "hourly_rate"):
                    value = usage.get(field)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise ConfigError(
                            f"Profile {name!r} has invalid usage {field}."
                        )
                assignments = usage.get("assignments")
                if (
                    isinstance(assignments, bool)
                    or not isinstance(assignments, int)
                    or assignments < 0
                ):
                    raise ConfigError(
                        f"Profile {name!r} has invalid usage assignments."
                    )
                if not isinstance(usage.get("fetched_at"), str):
                    raise ConfigError(f"Profile {name!r} has invalid usage timestamp.")


class CredentialStore:
    """Keyring adapter with read timeouts and synchronous, safe mutations."""

    def __init__(
        self,
        backend: Any = None,
        timeout: float = 30.0,
        legacy_path: Path | None = None,
    ) -> None:
        self.backend = backend or keyring
        self.timeout = timeout
        # Tests inject a fake backend and must never fall through to the host's
        # real legacy token. Production uses the upstream path by default.
        self.legacy_path = legacy_path or (
            DEFAULT_LEGACY_TOKEN_PATH if backend is None else None
        )

    def _call(self, operation: str, *args: str) -> str | None:
        if operation in {"set_password", "delete_password"}:
            # A timed-out daemon mutation could finish after rollback and undo it.
            # Mutations therefore return only after the backend has actually
            # finished. Read operations retain their timeout protection below.
            try:
                return getattr(self.backend, operation)(*args)
            except PasswordDeleteError:
                return None
            except (KeyringError, OSError, RuntimeError) as error:
                raise CredentialStoreError(
                    f"Keyring operation failed: {error}"
                ) from error

        result: list[str | None] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(getattr(self.backend, operation)(*args))
            except BaseException as exc:  # Re-raised in the calling thread below.
                errors.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            raise CredentialStoreError(f"Keyring operation '{operation}' timed out.")
        if errors:
            error = errors[0]
            if isinstance(error, PasswordDeleteError):
                return None
            if isinstance(error, (KeyringError, OSError, RuntimeError)):
                raise CredentialStoreError(
                    f"Keyring operation failed: {error}"
                ) from error
            raise error
        return result[0]

    def read_keyring_colab(self) -> str | None:
        return self._call("get_password", COLAB_SERVICE, COLAB_USERNAME)

    def read_legacy(self) -> str | None:
        if self.legacy_path is None or not self.legacy_path.exists():
            return None
        try:
            return self.legacy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialStoreError(
                f"Could not read legacy Colab token {self.legacy_path}: {exc}"
            ) from exc

    def read_colab(self) -> str | None:
        return self.read_keyring_colab() or self.read_legacy()

    def write_colab(self, token_json: str) -> None:
        self._call("set_password", COLAB_SERVICE, COLAB_USERNAME, token_json)

    def delete_colab(self) -> None:
        self._call("delete_password", COLAB_SERVICE, COLAB_USERNAME)

    def read_profile(self, name: str) -> str | None:
        return self._call("get_password", VAULT_SERVICE, validate_profile_name(name))

    def write_profile(self, name: str, token_json: str) -> None:
        self._call(
            "set_password", VAULT_SERVICE, validate_profile_name(name), token_json
        )

    def delete_profile(self, name: str) -> None:
        self._call("delete_password", VAULT_SERVICE, validate_profile_name(name))
