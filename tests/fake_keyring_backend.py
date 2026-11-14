"""Process-safe-enough file keyring for isolated CLI tests (fake secrets only)."""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

_fake_usage_installed = False


def _install_fake_usage():
    """Replace usage requests in the process under test (see ``fake_usage``)."""
    global _fake_usage_installed
    if not _fake_usage_installed and os.environ.get("FAKE_COLAB_USAGE") == "1":
        import fake_usage

        _fake_usage_installed = fake_usage.install()


class FileKeyring(KeyringBackend):
    priority = 1

    @property
    def path(self):
        return Path(os.environ["FAKE_KEYRING_PATH"])

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values))
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    @staticmethod
    def _key(service, username):
        return f"{service}\0{username}"

    def get_password(self, service, username):
        _install_fake_usage()
        return self._read().get(self._key(service, username))

    def set_password(self, service, username, password):
        values = self._read()
        values[self._key(service, username)] = password
        self._write(values)

    def delete_password(self, service, username):
        values = self._read()
        try:
            del values[self._key(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError("missing") from exc
        self._write(values)
