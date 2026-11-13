import subprocess
from pathlib import Path

import pytest
from conftest import make_token

from colab_account_changer.errors import AccountError, ConfigError, TokenError
from colab_account_changer.storage import COLAB_SERVICE, COLAB_USERNAME


def test_save_tracks_the_current_token_without_writing_secrets_to_config(
    manager, credentials, config_store
):
    raw = make_token("personal")
    credentials.write_colab(raw)

    profile = manager.save("personal", email="person@example.com", verify=False)

    assert profile.name == "personal"
    assert profile.email == "person@example.com"
    assert credentials.read_profile("personal") == raw
    assert config_store.load()["active_profile"] == "personal"
    config_text = config_store.path.read_text()
    assert raw not in config_text
    assert "refresh-personal" not in config_text
    assert "access-personal" not in config_text


def test_save_requires_an_existing_colab_credential(manager, credentials):
    credentials.delete_colab()
    with pytest.raises(AccountError, match="No active Colab OAuth credential"):
        manager.save("personal", verify=False)


def test_save_rejects_a_different_verified_identity_without_force(
    manager, credentials, mocker
):
    manager.save("personal", email="old@example.com", verify=False)
    credentials.write_colab(make_token("new"))
    mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=[
            mocker.Mock(
                token_json=make_token("new"),
                identity=mocker.Mock(email="new@example.com"),
            )
        ],
    )

    with pytest.raises(AccountError, match="different Google identity"):
        manager.save("personal")
    assert credentials.read_profile("personal") == make_token("personal")


def test_save_force_replaces_an_existing_profile(manager, credentials):
    manager.save("personal", email="old@example.com", verify=False)
    credentials.write_colab(make_token("new"))

    profile = manager.save(
        "personal", email="new@example.com", verify=False, force=True
    )

    assert profile.email == "new@example.com"
    assert credentials.read_profile("personal") == make_token("new")


def test_use_switches_and_automatically_saves_the_current_profile(
    manager, credentials, config_store
):
    manager.save("personal", email="person@example.com", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", email="work@example.com", verify=False)
    credentials.write_colab(make_token("personal-refreshed"))
    config = config_store.load()
    config["active_profile"] = "personal"
    config_store.save(config)

    profile = manager.use("work", verify=False)

    assert profile.name == "work"
    assert credentials.read_colab() == make_token("work")
    assert credentials.read_profile("personal") == make_token("personal-refreshed")
    assert config_store.load()["active_profile"] == "work"


def test_use_refuses_to_discard_an_untracked_current_token(manager, credentials):
    credentials.write_colab(make_token("unsaved"))
    credentials.write_profile("work", make_token("work"))
    config = manager.config.load()
    config["profiles"]["work"] = {
        "email": None,
        "created_at": "now",
        "updated_at": "now",
        "token_sha256": "0" * 64,
    }
    manager.config.save(config)

    with pytest.raises(AccountError, match="save it first"):
        manager.use("work", save_current=True, verify=False)
    assert credentials.read_colab() == make_token("unsaved")


def test_use_requires_an_explicit_flag_to_discard_unsaved_token(manager, credentials):
    credentials.write_colab(make_token("unsaved"))
    manager.credentials.write_profile("work", make_token("work"))
    config = manager.config.load()
    config["profiles"]["work"] = {
        "email": "work@example.com",
        "created_at": "now",
        "updated_at": "now",
        "token_sha256": "0" * 64,
    }
    manager.config.save(config)

    manager.use("work", discard_unsaved=True, verify=False)

    assert credentials.read_colab() == make_token("work")


def test_use_rejects_a_missing_profile_credential(manager, config_store):
    config = config_store.load()
    config["profiles"]["work"] = {
        "email": "work@example.com",
        "created_at": "now",
        "updated_at": "now",
        "token_sha256": "0" * 64,
    }
    config_store.save(config)

    with pytest.raises(AccountError, match="has no saved credential"):
        manager.use("work", verify=False)


def test_use_restores_current_token_when_readback_fails(
    manager, credentials, config_store, backend
):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)
    credentials.write_colab(make_token("personal"))
    config = config_store.load()
    config["active_profile"] = "personal"
    config_store.save(config)

    original_set = backend.set_password

    def tamper(service, username, value):
        if service == COLAB_SERVICE and username == COLAB_USERNAME:
            value = make_token("work") if value == make_token("work") else value
            if value == make_token("work"):
                backend.secrets[(service, username)] = make_token("tampered")
                return
        original_set(service, username, value)

    backend.set_password = tamper
    with pytest.raises(AccountError, match="verification failed"):
        manager.use("work", verify=False)
    assert credentials.read_colab() == make_token("personal")


def test_use_restores_current_token_when_config_update_fails(
    manager, credentials, config_store, monkeypatch
):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)
    credentials.write_colab(make_token("personal"))
    config = config_store.load()
    config["active_profile"] = "personal"
    config_store.save(config)
    original_save = config_store.save

    def fail_final_update(value):
        if value["active_profile"] == "work":
            raise ConfigError("disk full")
        original_save(value)

    monkeypatch.setattr(config_store, "save", fail_final_update)
    with pytest.raises(ConfigError, match="disk full"):
        manager.use("work", verify=False)
    assert credentials.read_colab() == make_token("personal")


def test_use_no_save_keeps_tracked_profiles_previous_copy(
    manager, credentials, config_store
):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)
    credentials.write_colab(make_token("personal-unsaved-change"))
    config = config_store.load()
    config["active_profile"] = "personal"
    config_store.save(config)

    manager.use("work", save_current=False, verify=False)

    assert credentials.read_colab() == make_token("work")
    assert credentials.read_profile("personal") == make_token("personal")


def test_using_current_profile_preserves_a_refreshed_active_token(manager, credentials):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("personal-refreshed"))

    manager.use("personal", verify=False)

    assert credentials.read_colab() == make_token("personal-refreshed")
    assert credentials.read_profile("personal") == make_token("personal-refreshed")


def test_remove_deletes_inactive_profile_but_not_active_one(manager, credentials):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)

    with pytest.raises(AccountError, match="active profile"):
        manager.remove("work")

    manager.remove("personal")
    assert credentials.read_profile("personal") is None
    assert "personal" not in manager.config.load()["profiles"]
    assert credentials.read_colab() == make_token("work")


def test_add_authenticates_in_an_empty_slot_and_keeps_new_account_active(
    manager, credentials
):
    manager.save("personal", verify=False)

    def authenticate(command, check):
        assert command[:2] == ["colab", "--auth=oauth2"]
        assert credentials.read_colab() is None
        credentials.write_colab(make_token("work"))
        return subprocess.CompletedProcess(command, 0)

    manager.runner.side_effect = authenticate
    profile = manager.add("work", verify=False)

    assert profile.email is None
    assert credentials.read_colab() == make_token("work")
    assert credentials.read_profile("work") == make_token("work")
    assert credentials.read_profile("personal") == make_token("personal")
    assert manager.config.load()["active_profile"] == "work"


def test_add_restores_original_account_when_authentication_fails(manager, credentials):
    manager.save("personal", verify=False)
    manager.runner.return_value = subprocess.CompletedProcess(["colab"], 1)

    with pytest.raises(AccountError, match="authentication failed"):
        manager.add("work", verify=False)

    assert credentials.read_colab() == make_token("personal")
    assert credentials.read_profile("work") is None
    assert manager.config.load()["active_profile"] == "personal"


def test_add_restores_original_account_on_ctrl_c(manager, credentials):
    manager.save("personal", verify=False)
    manager.runner.side_effect = KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        manager.add("work", verify=False)
    assert credentials.read_colab() == make_token("personal")


def test_add_refuses_untracked_current_account(manager, credentials):
    credentials.write_colab(make_token("unsaved"))

    with pytest.raises(AccountError, match="save it first"):
        manager.add("work", verify=False)
    manager.runner.assert_not_called()
    assert credentials.read_colab() == make_token("unsaved")


def test_add_restores_account_when_new_token_is_invalid(manager, credentials, mocker):
    manager.save("personal", verify=False)

    def authenticate(command, check):
        credentials.write_colab(make_token("work"))
        return subprocess.CompletedProcess(command, 0)

    manager.runner.side_effect = authenticate
    mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=TokenError("invalid token"),
    )

    with pytest.raises(TokenError, match="invalid token"):
        manager.add("work")
    assert credentials.read_colab() == make_token("personal")
    assert credentials.read_profile("work") is None


def test_add_without_original_cleans_up_failed_new_login(manager, credentials):
    credentials.delete_colab()

    def authenticate(command, check):
        credentials.write_colab(make_token("work"))
        return subprocess.CompletedProcess(command, 0)

    manager.runner.side_effect = authenticate
    manager.credentials.write_profile = lambda name, token: (_ for _ in ()).throw(
        AccountError("keychain failed")
    )

    with pytest.raises(AccountError, match="keychain failed"):
        manager.add("work", verify=False)
    assert credentials.read_colab() is None


def test_add_isolates_legacy_token_from_new_colab_authentication(
    manager, credentials, tmp_path
):
    manager.save("personal", verify=False)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("legacy"))
    manager.legacy_token_path = lambda: legacy
    credentials.legacy_path = legacy
    isolated_homes = []

    def authenticate(command, check, env):
        isolated_home = Path(env["HOME"])
        isolated_homes.append(isolated_home)
        assert isolated_home != Path.home()
        assert env["USERPROFILE"] == str(isolated_home)
        assert not (isolated_home / ".config/colab-cli/token.json").exists()
        assert credentials.read_keyring_colab() is None
        credentials.write_colab(make_token("work"))
        return subprocess.CompletedProcess(command, 0)

    manager.runner.side_effect = authenticate
    manager.add("work", verify=False)

    assert credentials.read_keyring_colab() == make_token("work")
    assert legacy.read_text() == make_token("legacy")
    assert not isolated_homes[0].exists()


def test_add_does_not_mistake_legacy_file_for_successful_new_login(
    manager, credentials, tmp_path
):
    manager.save("personal", verify=False)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("legacy"))
    manager.legacy_token_path = lambda: legacy
    credentials.legacy_path = legacy
    manager.runner.return_value = subprocess.CompletedProcess(["colab"], 0)

    with pytest.raises(AccountError, match="did not create"):
        manager.add("work", verify=False)

    assert credentials.read_keyring_colab() == make_token("personal")
    assert credentials.read_profile("work") is None
    assert legacy.read_text() == make_token("legacy")


def test_profiles_report_active_and_missing_credentials(manager, credentials):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)
    credentials.delete_profile("personal")

    profiles = manager.profiles()

    assert [(p.name, p.is_active, p.stored) for p in profiles] == [
        ("personal", False, False),
        ("work", True, True),
    ]


def test_status_detects_unsaved_current_credential(manager, credentials):
    credentials.write_colab(make_token("unsaved"))

    status = manager.status()

    assert status.active_profile is None
    assert status.has_credential is True
    assert status.matches_saved is False
