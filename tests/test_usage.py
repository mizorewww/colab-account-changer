import json
from unittest.mock import MagicMock

import pytest
import requests
from conftest import make_token

from colab_account_changer.errors import AccountError, TokenError
from colab_account_changer.usage import CCU_INFO_URL, Usage, UsageService


def response(status=200, body=None):
    result = MagicMock(status_code=status)
    result.text = json.dumps(
        body
        or {
            "currentBalance": 298.53,
            "consumptionRateHourly": 0.07,
            "assignmentsCount": 2,
        }
    )
    return result


def test_fetch_usage_uses_the_same_colab_endpoint_and_headers():
    session = MagicMock()
    session.get.return_value = response()
    factory = MagicMock(return_value=session)

    usage = UsageService(session_factory=factory).fetch(make_token())

    assert usage == Usage(balance=298.53, hourly_rate=0.07, assignments=2)
    session.get.assert_called_once_with(
        CCU_INFO_URL,
        headers={"Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"},
        params={"authuser": "0"},
        timeout=15,
    )


def test_fetch_usage_strips_xssi_prefix():
    session = MagicMock()
    result = response()
    result.text = ")]}'\n" + result.text
    session.get.return_value = result

    usage = UsageService(session_factory=MagicMock(return_value=session)).fetch(
        make_token()
    )

    assert usage.balance == 298.53


def test_fetch_usage_supports_paid_balance_alias():
    session = MagicMock()
    session.get.return_value = response(
        body={
            "paidComputeUnitsBalance": 12.5,
            "consumptionRateHourly": 1.25,
            "assignmentsCount": 3,
        }
    )

    usage = UsageService(session_factory=MagicMock(return_value=session)).fetch(
        make_token()
    )

    assert usage == Usage(balance=12.5, hourly_rate=1.25, assignments=3)


def test_usage_http_error_does_not_leak_token():
    session = MagicMock()
    session.get.return_value = response(status=403, body={"error": "access-token"})

    with pytest.raises(AccountError, match="HTTP 403") as error:
        UsageService(session_factory=MagicMock(return_value=session)).fetch(
            make_token()
        )
    assert "access-personal" not in str(error.value)
    assert "refresh-personal" not in str(error.value)


def test_usage_network_error_does_not_leak_token():
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError(
        "failed for access-personal and refresh-personal"
    )

    with pytest.raises(AccountError) as error:
        UsageService(session_factory=MagicMock(return_value=session)).fetch(
            make_token()
        )
    assert "access-personal" not in str(error.value)
    assert "refresh-personal" not in str(error.value)


def test_invalid_usage_response_is_rejected():
    session = MagicMock()
    result = response()
    result.text = "not-json"
    session.get.return_value = result

    with pytest.raises(AccountError, match="invalid response"):
        UsageService(session_factory=MagicMock(return_value=session)).fetch(
            make_token()
        )


def test_invalid_oauth_token_is_rejected_before_creating_session():
    factory = MagicMock()
    with pytest.raises(TokenError):
        UsageService(session_factory=factory).fetch("not-json")
    factory.assert_not_called()


def test_manager_can_query_untracked_current_usage(manager):
    manager.usage_service = MagicMock()
    manager.usage_service.fetch.return_value = Usage(1.0, 0.5, 2)

    result = manager.usage()

    assert result.assignments == 2
    manager.usage_service.fetch.assert_called_once_with(make_token("personal"))


def test_manager_queries_saved_profile_without_switching(manager, credentials):
    manager.save("personal", verify=False)
    credentials.write_colab(make_token("work"))
    manager.save("work", verify=False)
    manager.usage_service = MagicMock()
    manager.usage_service.fetch.return_value = Usage(10.0, 0.0, 0)

    manager.usage("personal")

    manager.usage_service.fetch.assert_called_once_with(make_token("personal"))
    assert credentials.read_keyring_colab() == make_token("work")
    assert manager.config.load()["active_profile"] == "work"


def test_manager_usage_requires_a_credential(manager, credentials):
    credentials.delete_colab()
    with pytest.raises(AccountError, match="No active Colab OAuth credential"):
        manager.usage()
