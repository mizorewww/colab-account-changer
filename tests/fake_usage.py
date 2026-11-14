"""A fake ccu-info endpoint for pool CLI subprocess tests.

``fake_keyring_backend`` installs it when ``FAKE_COLAB_USAGE=1`` so that no
test subprocess sends a request to Google. The assignment count comes from the
fake ``colab`` server file; ``FAKE_COLAB_USAGE_FAIL=ACCOUNT[,...]`` makes the
request fail for those accounts.
"""

import json
import os
import sys
from pathlib import Path


def install() -> bool:
    module = sys.modules.get("colab_account_changer.usage")
    if module is None:
        return False
    errors = sys.modules["colab_account_changer.errors"]

    def fetch(self, token_json):
        account = json.loads(token_json)["refresh_token"].removeprefix("refresh-")
        if account in os.environ.get("FAKE_COLAB_USAGE_FAIL", "").split(","):
            raise errors.AccountError("Colab usage request failed (HTTP 401).")
        server_path = Path(os.environ["FAKE_COLAB_SERVER"])
        server = json.loads(server_path.read_text()) if server_path.exists() else {}
        return module.Usage(100.0, 0.0, len(server.get(account, {})))

    module.UsageService.fetch = fetch
    return True
