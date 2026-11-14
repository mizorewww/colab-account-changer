import subprocess

import pytest
from conftest import make_token
from typer.testing import CliRunner

from colab_account_changer import __version__
from colab_account_changer.cli import app, main
from colab_account_changer.errors import AccountError, ConfigError
from colab_account_changer.manager import Profile
from colab_account_changer.tokens import Identity, InspectedToken
from colab_account_changer.usage import Usage

runner = CliRunner()


@pytest.fixture
def cli_manager(manager, mocker):
    manager.gcloud_runner = mocker.Mock(
        return_value=subprocess.CompletedProcess(["gcloud"], 0, stdout="[]")
    )
    mocker.patch("colab_account_changer.cli.AccountManager", return_value=manager)
    return manager


def test_help_and_version():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Securely switch" in result.stdout
    assert "save" in result.stdout
    assert "use" in result.stdout

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_empty_account_list(cli_manager, credentials):
    credentials.delete_colab()
    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "No saved accounts" in result.stdout
    assert "colab-account add" in result.stdout


def test_save_and_list_accounts(cli_manager):
    result = runner.invoke(
        app,
        ["save", "personal", "--email", "person@example.com", "--offline"],
    )

    assert result.exit_code == 0
    assert "personal" in result.stdout
    assert "person@example.com" in result.stdout
    assert "refresh-personal" not in result.stdout
    assert "access-personal" not in result.stdout

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "personal" in result.stdout
    assert "person@example.com" in result.stdout
    assert "never" in result.stdout


def test_list_auto_discovers_and_imports_existing_account(cli_manager, mocker):
    mocker.patch(
        "colab_account_changer.manager.inspect_token",
        side_effect=lambda raw, verify: InspectedToken(
            raw, Identity(email="person@example.com")
        ),
    )

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "person" in result.stdout
    assert "person@example.com" in result.stdout
    assert cli_manager.config.load()["active_profile"] == "person"
    assert cli_manager.config.load()["profiles"]["person"]["source"] == "keyring"


def test_sync_offline_imports_without_calling_google(cli_manager, mocker):
    get = mocker.patch("colab_account_changer.tokens.requests.get")

    result = runner.invoke(app, ["sync", "--offline"])

    assert result.exit_code == 0
    assert "imported 1 new profile(s)" in result.stdout
    assert "current" in result.stdout
    get.assert_not_called()


def test_usage_displays_live_compute_units(cli_manager, mocker):
    cli_manager.save("personal", email="person@example.com", verify=False)
    cli_manager.usage_service = mocker.Mock()
    cli_manager.usage_service.fetch.return_value = Usage(298.53, 0.07, 2)

    result = runner.invoke(app, ["usage", "personal"])

    assert result.exit_code == 0
    assert "298.53 compute units" in result.stdout
    assert "0.07 CU/hr" in result.stdout
    assert "4264.7" in result.stdout
    assert "2" in result.stdout
    cli_manager.usage_service.fetch.assert_called_once_with(make_token("personal"))


def test_list_usage_queries_each_profile_without_switching(
    cli_manager, credentials, mocker
):
    cli_manager.save("personal", email="person@example.com", verify=False)
    credentials.write_colab(make_token("work"))
    cli_manager.save("work", email="work@example.com", verify=False)

    def fetch(raw):
        if raw == make_token("personal"):
            return Usage(100.0, 0.25, 1)
        raise AccountError("usage denied")

    cli_manager.usage_service = mocker.Mock()
    cli_manager.usage_service.fetch.side_effect = fetch

    result = runner.invoke(app, ["list", "--usage"])

    assert result.exit_code == 0
    assert "100.00" in result.stdout
    assert "0.25" in result.stdout
    assert "400.0" in result.stdout
    assert "Known total (1/2 accounts)" in result.stdout
    assert "error" in result.stdout
    assert "usage denied" in result.stderr
    assert credentials.read_keyring_colab() == make_token("work")
    assert cli_manager.config.load()["active_profile"] == "work"
    assert (
        cli_manager.config.load()["profiles"]["personal"]["usage"]["balance"] == 100.0
    )


def test_list_uses_cached_usage_without_fetching(cli_manager, mocker):
    cli_manager.save("personal", email="person@example.com", verify=False)
    cli_manager.usage_service = mocker.Mock()
    cli_manager.usage_service.fetch.return_value = Usage(123.45, 0.67, 2)
    cli_manager.usage("personal")
    cli_manager.usage_service.fetch.reset_mock()

    result = runner.invoke(app, ["list", "--no-discover"])

    assert result.exit_code == 0
    assert "123.45" in result.stdout
    assert "0.67" in result.stdout
    assert "never" not in result.stdout
    cli_manager.usage_service.fetch.assert_not_called()


def test_offline_email_labels_are_rendered_literally(cli_manager):
    result = runner.invoke(
        app,
        ["save", "personal", "--email", "[red]not-an-email", "--offline"],
    )

    assert result.exit_code == 0
    assert "[red]not-an-email" in result.stdout


def test_current_auto_discovers_existing_credential_without_network(
    cli_manager, mocker
):
    get = mocker.patch("colab_account_changer.tokens.requests.get")

    result = runner.invoke(app, ["current"])

    assert result.exit_code == 0
    assert "Current Colab account" in result.stdout
    assert "current" in result.stdout
    assert cli_manager.config.load()["active_profile"] == "current"
    get.assert_not_called()


def test_current_shows_active_account(cli_manager):
    cli_manager.save("personal", email="person@example.com", verify=False)

    result = runner.invoke(app, ["current"])

    assert result.exit_code == 0
    assert "personal" in result.stdout
    assert "person@example.com" in result.stdout
    assert "current" in result.stdout


def test_use_forwards_safety_options(cli_manager, mocker):
    use = mocker.patch.object(cli_manager, "use")
    use.return_value = Profile(
        name="work",
        email="work@example.com",
        created_at="now",
        updated_at="now",
        token_sha256="0" * 64,
        is_active=True,
    )

    result = runner.invoke(
        app,
        ["use", "work", "--no-save", "--discard-unsaved", "--offline"],
    )

    assert result.exit_code == 0
    use.assert_called_once_with(
        "work", save_current=False, discard_unsaved=True, verify=False
    )


def test_add_preserves_interactive_colab_authentication(cli_manager):
    def authenticate(command, check):
        cli_manager.credentials.write_colab(make_token("work"))
        return subprocess.CompletedProcess(command, 0)

    cli_manager.runner.side_effect = authenticate
    cli_manager.save("personal", verify=False)

    result = runner.invoke(app, ["add", "work", "--offline"])

    assert result.exit_code == 0
    assert "work" in result.stdout
    command = cli_manager.runner.call_args.args[0]
    assert command == ["colab", "--auth=oauth2", "whoami"]
    assert cli_manager.runner.call_args.kwargs == {"check": False}


def test_remove_requires_confirmation(cli_manager, credentials):
    cli_manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    cli_manager.save("work", verify=False)

    result = runner.invoke(app, ["remove", "personal"], input="n\n")
    assert result.exit_code != 0
    assert credentials.read_profile("personal") is not None

    result = runner.invoke(app, ["remove", "personal"], input="y\n")
    assert result.exit_code == 0
    assert credentials.read_profile("personal") is None


def test_remove_yes_skips_confirmation(cli_manager, credentials):
    cli_manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    cli_manager.save("work", verify=False)

    result = runner.invoke(app, ["remove", "personal", "--yes"])

    assert result.exit_code == 0
    assert credentials.read_profile("personal") is None


def test_main_formats_expected_errors_without_a_traceback(mocker, capsys):
    mocker.patch("colab_account_changer.cli.app", side_effect=ConfigError("bad config"))

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "bad config" in captured.err
    assert "Traceback" not in captured.err


def test_main_handles_ctrl_c(mocker, capsys):
    mocker.patch("colab_account_changer.cli.app", side_effect=KeyboardInterrupt)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 130
    assert "Cancelled" in capsys.readouterr().err
