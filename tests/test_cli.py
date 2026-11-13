import subprocess

import pytest
from conftest import make_token
from typer.testing import CliRunner

from colab_account_changer import __version__
from colab_account_changer.cli import app, main
from colab_account_changer.errors import ConfigError
from colab_account_changer.manager import Profile

runner = CliRunner()


@pytest.fixture
def cli_manager(manager, mocker):
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


def test_empty_account_list(cli_manager):
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
    assert "saved" in result.stdout


def test_offline_email_labels_are_rendered_literally(cli_manager):
    result = runner.invoke(
        app,
        ["save", "personal", "--email", "[red]not-an-email", "--offline"],
    )

    assert result.exit_code == 0
    assert "[red]not-an-email" in result.stdout


def test_current_shows_unsaved_credential(cli_manager):
    result = runner.invoke(app, ["current"])

    assert result.exit_code == 0
    assert "not tracked" in result.stdout
    assert "save NAME" in result.stdout


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
