import json
import os
import stat
from pathlib import Path

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from colab_account_changer.errors import ConfigError, CredentialStoreError
from colab_account_changer.storage import (
    COLAB_SERVICE,
    COLAB_USERNAME,
    VAULT_SERVICE,
    ConfigStore,
    CredentialStore,
    validate_profile_name,
)


class FakeKeyring:
    def __init__(self):
        self.secrets = {}

    def get_password(self, service, username):
        return self.secrets.get((service, username))

    def set_password(self, service, username, value):
        self.secrets[(service, username)] = value

    def delete_password(self, service, username):
        if (service, username) not in self.secrets:
            raise PasswordDeleteError("missing")
        del self.secrets[(service, username)]


@pytest.fixture
def backend():
    return FakeKeyring()


@pytest.fixture
def credentials(backend):
    return CredentialStore(backend=backend, timeout=0.1)


def test_colab_credentials_use_the_upstream_keyring_entry(credentials, backend):
    assert credentials.read_colab() is None

    credentials.write_colab('{"refresh_token": "secret"}')

    assert backend.secrets[(COLAB_SERVICE, COLAB_USERNAME)] == (
        '{"refresh_token": "secret"}'
    )
    assert credentials.read_colab() == '{"refresh_token": "secret"}'

    credentials.delete_colab()
    credentials.delete_colab()  # Deleting an absent credential is idempotent.
    assert credentials.read_colab() is None


def test_profiles_are_separate_from_the_active_colab_credential(credentials, backend):
    credentials.write_profile("personal", "personal-token")
    credentials.write_profile("work", "work-token")
    credentials.write_colab("work-token")

    assert backend.secrets[(VAULT_SERVICE, "personal")] == "personal-token"
    assert credentials.read_profile("personal") == "personal-token"
    assert credentials.read_colab() == "work-token"

    credentials.delete_profile("personal")
    assert credentials.read_profile("personal") is None
    assert credentials.read_colab() == "work-token"


def test_keyring_failures_do_not_expose_token_values(backend):
    class BrokenKeyring(FakeKeyring):
        def set_password(self, service, username, value):
            raise KeyringError("keychain is locked")

    store = CredentialStore(backend=BrokenKeyring(), timeout=0.1)
    with pytest.raises(CredentialStoreError, match="locked") as error:
        store.write_colab("very-secret-token")
    assert "very-secret-token" not in str(error.value)


def test_keyring_timeout_is_reported(backend):
    class SlowKeyring(FakeKeyring):
        def get_password(self, service, username):
            import time

            time.sleep(0.2)

    store = CredentialStore(backend=SlowKeyring(), timeout=0.01)
    with pytest.raises(CredentialStoreError, match="timed out"):
        store.read_colab()


def test_config_round_trip_and_private_permissions(tmp_path):
    path = tmp_path / "nested" / "config.json"
    store = ConfigStore(path)
    config = store.load()
    config["active_profile"] = "personal"
    config["profiles"]["personal"] = {
        "email": "person@example.com",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "token_sha256": "a" * 64,
    }

    store.save(config)

    assert store.load() == config
    assert json.loads(path.read_text()) == config
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_missing_config_has_no_profiles(tmp_path):
    assert ConfigStore(tmp_path / "missing.json").load() == {
        "version": 1,
        "active_profile": None,
        "profiles": {},
    }


def test_corrupt_config_is_never_silently_replaced(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken")
    with pytest.raises(ConfigError, match="Invalid configuration"):
        ConfigStore(path).load()
    assert path.read_text() == "{broken"


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"version": 2, "active_profile": None, "profiles": {}},
        {"version": 1, "active_profile": "ghost", "profiles": {}},
        {"version": 1, "active_profile": None, "profiles": []},
    ],
)
def test_invalid_config_shape_is_rejected(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError):
        ConfigStore(path).load()


@pytest.mark.parametrize(
    "name", ["", "../secret", "has space", "bad/name", "[bold]", "a" * 65]
)
def test_profile_names_cannot_escape_or_inject_markup(name):
    with pytest.raises(ValueError):
        validate_profile_name(name)


@pytest.mark.parametrize("name", ["personal", "work-2", "a.b_c", "A"])
def test_valid_profile_names(name):
    assert validate_profile_name(name) == name


def test_config_path_honours_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("COLAB_ACCOUNT_CHANGER_CONFIG", str(tmp_path / "custom.json"))
    assert ConfigStore().path == tmp_path / "custom.json"


def test_config_save_cleans_up_temporary_files_on_failure(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigError, match="save configuration"):
        store.save(store.load())
    assert list(Path(tmp_path).iterdir()) == []
