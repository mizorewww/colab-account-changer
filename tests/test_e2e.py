import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_cli(tmp_path):
    environment = os.environ.copy()
    tests_directory = str(Path(__file__).parent)
    environment.update(
        {
            "COLAB_ACCOUNT_CHANGER_CONFIG": str(tmp_path / "config.json"),
            "FAKE_KEYRING_PATH": str(tmp_path / "keyring.json"),
            "FAKE_COLAB_LOGIN": str(tmp_path / "next-login.txt"),
            "PYTHON_KEYRING_BACKEND": "fake_keyring_backend.FileKeyring",
            "PYTHONPATH": os.pathsep.join(
                filter(None, [tests_directory, environment.get("PYTHONPATH")])
            ),
            "HOME": str(tmp_path / "home"),
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    (tmp_path / "next-login.txt").write_text("personal")
    fake_colab = tmp_path / "fake-colab"
    fake_colab.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

import keyring

if sys.argv[1:] != ["--auth=oauth2", "whoami"]:
    raise SystemExit(2)
marker = Path(os.environ["FAKE_COLAB_LOGIN"]).read_text().strip()
if marker == "FAIL":
    raise SystemExit(17)
token = {{
    "token": "fake-access-" + marker,
    "refresh_token": "fake-refresh-" + marker,
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "fake-client-id",
    "client_secret": "fake-client-secret",
    "scopes": ["openid"],
    "expiry": "2999-01-01T00:00:00Z",
}}
keyring.set_password("google-colab-cli", "oauth_token", json.dumps(token))
print("Email: " + marker + "@example.com")
"""
    )
    fake_colab.chmod(0o700)
    executable = Path(sys.executable).with_name("colab-account")

    def invoke(*arguments, colab=False):
        command = [str(fake_colab if colab else executable), *arguments]
        return subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    return invoke, tmp_path, fake_colab


def test_complete_account_workflow_without_real_credentials(isolated_cli):
    invoke, tmp_path, fake_colab = isolated_cli
    assert invoke("--auth=oauth2", "whoami", colab=True).returncode == 0

    saved = invoke("save", "personal", "--offline", "--email", "person@example.com")
    assert saved.returncode == 0, saved.stderr

    (tmp_path / "next-login.txt").write_text("work")
    added = invoke("add", "work", "--offline", "--colab-command", str(fake_colab))
    assert added.returncode == 0, added.stderr
    assert "work" in added.stdout

    switched = invoke("use", "personal", "--offline")
    assert switched.returncode == 0, switched.stderr

    listed = invoke("list")
    assert listed.returncode == 0
    assert "personal" in listed.stdout
    assert "work" in listed.stdout
    current = invoke("current")
    assert current.returncode == 0
    assert "personal" in current.stdout

    secrets = json.loads((tmp_path / "keyring.json").read_text())
    assert (
        secrets["google-colab-cli\0oauth_token"]
        == secrets["colab-account-changer\0personal"]
    )
    assert "fake-access-work" in secrets["colab-account-changer\0work"]
    config_text = (tmp_path / "config.json").read_text()
    all_output = "".join(
        result.stdout + result.stderr for result in (saved, added, switched, listed)
    )
    assert "fake-access-" not in all_output
    assert "fake-refresh-" not in all_output
    assert "fake-client-secret" not in config_text


def test_failed_new_login_restores_original_in_real_cli_process(isolated_cli):
    invoke, tmp_path, fake_colab = isolated_cli
    assert invoke("--auth=oauth2", "whoami", colab=True).returncode == 0
    assert invoke("save", "personal", "--offline").returncode == 0
    original = json.loads((tmp_path / "keyring.json").read_text())[
        "google-colab-cli\0oauth_token"
    ]

    (tmp_path / "next-login.txt").write_text("FAIL")
    failed = invoke("add", "work", "--offline", "--colab-command", str(fake_colab))

    assert failed.returncode == 1
    assert "authentication failed" in failed.stderr
    assert "Traceback" not in failed.stderr
    secrets = json.loads((tmp_path / "keyring.json").read_text())
    assert secrets["google-colab-cli\0oauth_token"] == original
    assert "colab-account-changer\0work" not in secrets
    assert json.loads((tmp_path / "config.json").read_text())["active_profile"] == (
        "personal"
    )
