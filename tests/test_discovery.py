import json
import subprocess

from conftest import make_token, refreshed_token

from colab_account_changer.tokens import Identity, InspectedToken


def inspected(raw, email):
    return InspectedToken(token_json=raw, identity=Identity(email=email))


def mock_identity(mocker, email):
    return mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=lambda raw, verify: inspected(raw, email),
    )


def test_discover_imports_current_keyring_token_using_email_alias(
    manager, credentials, mocker
):
    mock_identity(mocker, "Person@example.com")

    result = manager.discover(include_gcloud=False)

    assert [profile.name for profile in result.imported] == ["person"]
    profile = result.profiles[0]
    assert profile.email == "Person@example.com"
    assert profile.source == "keyring"
    assert profile.is_active is True
    assert credentials.read_profile("person") == make_token("personal")
    assert credentials.read_keyring_colab() == make_token("personal")


def test_discover_skips_identity_lookup_for_an_already_saved_token(manager, mocker):
    manager.save("personal", email="person@example.com", verify=False)
    inspect = mock_identity(mocker, "person@example.com")

    result = manager.discover(include_gcloud=False)

    assert result.imported == []
    assert [profile.name for profile in result.profiles] == ["personal"]
    inspect.assert_not_called()


def test_discover_imports_legacy_token_without_modifying_file(
    manager, credentials, tmp_path, mocker
):
    credentials.delete_colab()
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("legacy"))
    credentials.legacy_path = legacy
    mock_identity(mocker, "legacy@example.com")

    result = manager.discover(include_gcloud=False)

    assert [profile.name for profile in result.imported] == ["legacy"]
    assert result.profiles[0].source == "legacy"
    assert result.profiles[0].is_active is True
    assert credentials.read_keyring_colab() is None
    assert credentials.read_profile("legacy") == make_token("legacy")
    assert legacy.read_text() == make_token("legacy")


def test_discover_imports_both_sources_and_marks_keyring_active(
    manager, credentials, tmp_path, mocker
):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("work"))
    credentials.legacy_path = legacy
    mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=lambda raw, verify: inspected(
            raw,
            "person@example.com"
            if raw == make_token("personal")
            else "work@example.com",
        ),
    )

    result = manager.discover(include_gcloud=False)

    profiles = {profile.name: profile for profile in result.profiles}
    assert set(profiles) == {"person", "work"}
    assert profiles["person"].is_active is True
    assert profiles["work"].is_active is False
    assert profiles["work"].source == "legacy"


def test_discover_deduplicates_same_google_identity_across_sources(
    manager, credentials, tmp_path, mocker
):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(make_token("older-copy"))
    credentials.legacy_path = legacy
    mock_identity(mocker, "person@example.com")

    result = manager.discover(include_gcloud=False)

    assert len(result.profiles) == 1
    assert result.profiles[0].name == "person"
    assert credentials.read_profile("person") == make_token("personal")


def test_discover_disambiguates_equal_email_local_parts(manager, credentials, mocker):
    manager.save("person", email="person@one.example", verify=False)
    credentials.write_colab(make_token("different"))
    mock_identity(mocker, "person@two.example")

    result = manager.discover(include_gcloud=False)

    assert result.imported[0].name == "person-2"
    assert result.imported[0].email == "person@two.example"
    assert credentials.read_profile("person") == make_token("personal")


def test_discover_offline_reuses_active_alias_without_network(
    manager, credentials, mocker
):
    manager.save("custom", email="person@example.com", verify=False)
    credentials.write_colab(refreshed_token("personal", "refreshed-access"))
    get = mocker.patch("colab_account_changer.tokens.requests.get")

    result = manager.discover(verify=False, include_gcloud=False)

    assert [profile.name for profile in result.profiles] == ["custom"]
    assert credentials.read_profile("custom") == refreshed_token(
        "personal", "refreshed-access"
    )
    assert result.profiles[0].email == "person@example.com"
    get.assert_not_called()


def test_discover_reports_bad_legacy_token_but_imports_valid_current(
    manager, credentials, tmp_path
):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("not-json")
    credentials.legacy_path = legacy

    result = manager.discover(verify=False, include_gcloud=False)

    assert [profile.name for profile in result.profiles] == ["current"]
    assert len(result.errors) == 1
    assert "legacy" in result.errors[0]
    assert "not-json" not in result.errors[0]


def test_discover_reads_gcloud_account_labels_without_exposing_credentials(
    manager, mocker
):
    mock_identity(mocker, "person@example.com")
    manager.gcloud_runner = mocker.Mock(
        return_value=subprocess.CompletedProcess(
            ["gcloud"],
            0,
            stdout=json.dumps(
                [
                    {"account": "person@example.com", "status": "ACTIVE"},
                    {"account": "other@example.com", "status": ""},
                ]
            ),
        )
    )

    result = manager.discover()

    assert result.gcloud_accounts == ["person@example.com", "other@example.com"]
    manager.gcloud_runner.assert_called_once_with(
        ["gcloud", "auth", "list", "--format=json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_discover_ignores_unavailable_gcloud(manager, mocker):
    mock_identity(mocker, "person@example.com")
    manager.gcloud_runner = mocker.Mock(side_effect=FileNotFoundError)

    result = manager.discover()

    assert result.gcloud_accounts == []
    assert len(result.profiles) == 1
