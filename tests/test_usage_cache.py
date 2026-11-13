import json

import pytest
from conftest import make_token, refreshed_token

from colab_account_changer.errors import ConfigError
from colab_account_changer.storage import ConfigStore
from colab_account_changer.usage import Usage


def test_usage_success_is_cached_in_config_and_profile(manager, mocker):
    manager.save("personal", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(298.53, 0.07, 2)

    result = manager.usage("personal")

    assert result == Usage(298.53, 0.07, 2)
    cached = manager.config.load()["profiles"]["personal"]["usage"]
    assert cached["balance"] == 298.53
    assert cached["hourly_rate"] == 0.07
    assert cached["assignments"] == 2
    assert cached["fetched_at"]
    profile = manager.profiles()[0]
    assert profile.usage == result
    assert profile.usage_fetched_at == cached["fetched_at"]


def test_usage_cache_is_replaced_by_latest_success(manager, mocker):
    manager.save("personal", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.side_effect = [Usage(10.0, 1.0, 1), Usage(8.0, 0.5, 0)]

    manager.usage("personal")
    manager.usage("personal")

    cached = manager.config.load()["profiles"]["personal"]["usage"]
    assert cached["balance"] == 8.0
    assert cached["hourly_rate"] == 0.5
    assert cached["assignments"] == 0


def test_discovery_preserves_cache_for_same_refresh_credential(
    manager, credentials, mocker
):
    manager.save("personal", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(10.0, 1.0, 1)
    manager.usage("personal")
    credentials.write_colab(refreshed_token("personal", "new-access"))

    manager.discover(verify=False, include_gcloud=False)

    assert manager.config.load()["profiles"]["personal"]["usage"]["balance"] == 10.0


def test_save_preserves_cache_for_same_refresh_credential(manager, credentials, mocker):
    manager.save("personal", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(10.0, 1.0, 1)
    manager.usage("personal")
    credentials.write_colab(refreshed_token("personal", "new-access"))

    manager.save("personal", verify=False)

    assert manager.config.load()["profiles"]["personal"]["usage"]["balance"] == 10.0


def test_force_replacing_identity_clears_cached_usage(manager, credentials, mocker):
    manager.save("personal", email="old@example.com", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(10.0, 1.0, 1)
    manager.usage("personal")
    credentials.write_colab(make_token("different"))

    manager.save("personal", email="new@example.com", verify=False, force=True)

    assert "usage" not in manager.config.load()["profiles"]["personal"]


def test_usage_failure_does_not_replace_previous_cache(manager, mocker):
    manager.save("personal", verify=False)
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.return_value = Usage(10.0, 1.0, 1)
    manager.usage("personal")
    manager.usage_service.fetch.side_effect = ConfigError("request failed")

    with pytest.raises(ConfigError):
        manager.usage("personal")

    assert manager.config.load()["profiles"]["personal"]["usage"]["balance"] == 10.0


def test_invalid_cached_usage_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": None,
                "profiles": {
                    "personal": {
                        "email": None,
                        "created_at": "now",
                        "updated_at": "now",
                        "token_sha256": "0" * 64,
                        "usage": {
                            "balance": "not-a-number",
                            "hourly_rate": 1.0,
                            "assignments": 1,
                            "fetched_at": "now",
                        },
                    }
                },
            }
        )
    )

    with pytest.raises(ConfigError, match="usage"):
        ConfigStore(path).load()
