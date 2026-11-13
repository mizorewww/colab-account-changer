import json
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import FakeKeyring, make_token

from colab_account_changer.errors import AccountError, ConfigError, CredentialStoreError
from colab_account_changer.storage import ConfigStore, CredentialStore
from colab_account_changer.tokens import Identity, InspectedToken
from colab_account_changer.usage import Usage


def refreshed_copy(raw, access_token):
    value = json.loads(raw)
    value["token"] = access_token
    return json.dumps(value)


def test_offline_discovery_never_overwrites_a_different_refresh_credential(
    manager, credentials
):
    manager.save("personal", email="old@example.com", verify=False)
    original = credentials.read_profile("personal")
    credentials.write_colab(make_token("different-account"))

    result = manager.discover(verify=False, include_gcloud=False)

    assert credentials.read_profile("personal") == original
    assert manager.config.load()["profiles"]["personal"]["email"] == "old@example.com"
    assert result.profiles[0].name == "current"
    assert result.profiles[0].email is None
    assert manager.config.load()["active_profile"] == "current"
    assert credentials.read_profile("current") == make_token("different-account")


def test_online_discovery_verifies_a_previously_offline_alias(manager, mocker):
    manager.discover(verify=False, include_gcloud=False)
    inspect = mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=lambda raw, verify: InspectedToken(
            raw, Identity(email="person@example.com")
        ),
    )

    result = manager.discover(verify=True, include_gcloud=False)

    assert len(result.profiles) == 1
    assert result.profiles[0].name == "current"
    assert result.profiles[0].email == "person@example.com"
    inspect.assert_called_once()


def test_offline_discovery_allows_a_refresh_of_the_same_refresh_token(
    manager, credentials
):
    manager.save("personal", email="old@example.com", verify=False)
    refreshed = refreshed_copy(make_token("personal"), "new-access-token")
    credentials.write_colab(refreshed)

    result = manager.discover(verify=False, include_gcloud=False)

    assert result.profiles[0].name == "personal"
    assert result.profiles[0].email == "old@example.com"
    assert credentials.read_profile("personal") == refreshed


def test_offline_save_rejects_a_different_refresh_credential_without_force(
    manager, credentials
):
    manager.save("personal", email="old@example.com", verify=False)
    original = credentials.read_profile("personal")
    credentials.write_colab(make_token("different-account"))

    with pytest.raises(AccountError, match=r"cannot verify|offline"):
        manager.save("personal", verify=False)

    assert credentials.read_profile("personal") == original
    assert manager.config.load()["profiles"]["personal"]["email"] == "old@example.com"


def test_offline_save_missing_vault_credential_cannot_reuse_old_identity(
    manager, credentials
):
    manager.save("personal", email="old@example.com", verify=False)
    credentials.delete_profile("personal")
    credentials.write_colab(make_token("different-account"))

    with pytest.raises(AccountError, match=r"cannot verify|offline"):
        manager.save("personal", verify=False)

    assert credentials.read_profile("personal") is None
    assert manager.config.load()["profiles"]["personal"]["email"] == "old@example.com"


def test_offline_save_can_restore_exact_missing_credential(manager, credentials):
    manager.save("personal", email="old@example.com", verify=False)
    original = credentials.read_profile("personal")
    credentials.delete_profile("personal")

    manager.save("personal", verify=False)

    assert credentials.read_profile("personal") == original


def test_offline_discovery_ignores_unverified_account_json_field(manager, credentials):
    manager.save("personal", email="old@example.com", verify=False)
    original = credentials.read_profile("personal")
    value = json.loads(make_token("different-account"))
    value["account"] = "old@example.com"
    credentials.write_colab(json.dumps(value))

    result = manager.discover(verify=False, include_gcloud=False)

    assert credentials.read_profile("personal") == original
    assert result.profiles[0].name == "current"
    assert result.profiles[0].email is None


def test_offline_save_accepts_same_refresh_token(manager, credentials):
    manager.save("personal", email="old@example.com", verify=False)
    refreshed = refreshed_copy(make_token("personal"), "new-access-token")
    credentials.write_colab(refreshed)

    manager.save("personal", verify=False)

    assert credentials.read_profile("personal") == refreshed


def test_discovery_prefers_active_alias_for_duplicate_token_digest(
    manager, config_store
):
    manager.save("personal", verify=False)
    manager.save("work", verify=False)

    manager.discover(verify=False, include_gcloud=False)

    assert config_store.load()["active_profile"] == "work"


def test_usage_never_falls_back_to_an_unrelated_current_account(
    manager, credentials, mocker
):
    manager.save("personal", email="person@example.com", verify=False)
    credentials.delete_profile("personal")
    credentials.write_colab(make_token("work"))
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(10.0, 0.0, 0)

    with pytest.raises(AccountError, match="no saved credential"):
        manager.usage("personal")

    manager.usage_service.fetch.assert_not_called()


def test_failed_legacy_only_add_restores_empty_keyring(manager, credentials, tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("legacy"))
    credentials.legacy_path = legacy
    manager.legacy_token_path = lambda: legacy
    credentials.delete_colab()
    manager.discover(verify=False, include_gcloud=False)
    manager.runner.return_value = subprocess.CompletedProcess(["colab"], 1)

    with pytest.raises(AccountError, match="authentication failed"):
        manager.add("work", verify=False)

    assert credentials.read_keyring_colab() is None
    assert credentials.read_colab() == make_token("legacy")
    assert legacy.read_text() == make_token("legacy")


def test_config_rejects_profile_with_missing_email_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": None,
                "profiles": {
                    "personal": {
                        "created_at": "now",
                        "updated_at": "now",
                        "token_sha256": "0" * 64,
                    }
                },
            }
        )
    )

    with pytest.raises(ConfigError, match="email"):
        ConfigStore(path).load()


def test_custom_config_save_does_not_chmod_existing_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    store = ConfigStore(parent / "custom.json")

    store.save(store.load())

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_config_has_no_post_commit_chmod_failure_window(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    original_chmod = Path.chmod

    def fail_on_committed_file(path, mode):
        if path == store.path:
            raise OSError("chmod failed after commit")
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_on_committed_file)

    store.save(store.load())

    assert store.load()["active_profile"] is None


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_keyring_mutations_are_not_abandoned_after_read_timeout(operation):
    class BlockingKeyring(FakeKeyring):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def set_password(self, service, username, value):
            self.started.set()
            self.release.wait(2)
            super().set_password(service, username, value)

        def delete_password(self, service, username):
            self.started.set()
            self.release.wait(2)
            super().delete_password(service, username)

    backend = BlockingKeyring()
    store = CredentialStore(backend=backend, timeout=0.01)
    errors = []
    if operation == "delete":
        backend.release.set()
        store.write_colab("original")
        backend.started.clear()
        backend.release.clear()

    def mutate():
        try:
            if operation == "write":
                store.write_colab("new-value")
            else:
                store.delete_colab()
        except CredentialStoreError as exc:
            errors.append(exc)

    thread = threading.Thread(target=mutate)
    thread.start()
    assert backend.started.wait(1)
    try:
        thread.join(0.05)
        assert thread.is_alive(), "Mutation was abandoned after the read timeout."
    finally:
        backend.release.set()
        thread.join(1)

    assert not thread.is_alive()
    assert errors == []
    if operation == "write":
        assert store.read_keyring_colab() == "new-value"
    else:
        assert store.read_keyring_colab() is None
