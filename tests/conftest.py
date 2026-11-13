import json

import pytest
from keyring.errors import PasswordDeleteError

from colab_account_changer.manager import AccountManager
from colab_account_changer.storage import ConfigStore, CredentialStore


def make_token(marker="account"):
    return json.dumps(
        {
            "token": f"access-{marker}",
            "refresh_token": f"refresh-{marker}",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": ["openid"],
            "expiry": "2999-01-01T00:00:00Z",
        }
    )


def refreshed_token(marker="account", access_token="new-access-token"):
    value = json.loads(make_token(marker))
    value["token"] = access_token
    return json.dumps(value)


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


@pytest.fixture
def config_store(tmp_path):
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def manager(credentials, config_store, mocker):
    credentials.write_colab(make_token("personal"))
    runner = mocker.Mock()
    return AccountManager(
        config_store=config_store,
        credentials=credentials,
        runner=runner,
        legacy_token_path=lambda: config_store.path.parent / "missing-legacy.json",
    )
