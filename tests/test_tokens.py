import hashlib
import json
from unittest.mock import MagicMock

import pytest
import requests

from colab_account_changer.errors import TokenError
from colab_account_changer.tokens import inspect_token, parse_token, token_fingerprint


def token_json(**updates):
    value = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": ["openid", "https://www.googleapis.com/auth/userinfo.email"],
        "expiry": "2999-01-01T00:00:00Z",
    }
    value.update(updates)
    return json.dumps(value)


def response(status=200, **body):
    result = MagicMock(status_code=status)
    result.json.return_value = body or {
        "email": "person@example.com",
        "expires_in": "3599",
        "scope": "openid email",
    }
    return result


def test_fingerprint_does_not_reveal_token():
    raw = token_json()
    assert token_fingerprint(raw) == hashlib.sha256(raw.encode()).hexdigest()
    assert "access-token" not in token_fingerprint(raw)


@pytest.mark.parametrize("raw", ["not-json", "[]", "{}"])
def test_malformed_or_incomplete_tokens_are_rejected(raw):
    with pytest.raises(TokenError):
        parse_token(raw)


def test_a_long_lived_refresh_token_is_required():
    with pytest.raises(TokenError, match="refresh token"):
        parse_token(token_json(refresh_token=""))


def test_valid_token_is_identified_without_refresh(mocker):
    get = mocker.patch(
        "colab_account_changer.tokens.requests.get",
        return_value=response(),
    )
    refresh = mocker.patch("google.oauth2.credentials.Credentials.refresh")

    inspected = inspect_token(token_json())

    assert inspected.identity.email == "person@example.com"
    assert inspected.identity.expires_in == 3599
    assert inspected.identity.scopes == ("openid", "email")
    assert inspected.token_json == token_json()
    refresh.assert_not_called()
    assert get.call_args.kwargs["params"] == {"access_token": "access-token"}
    assert get.call_args.kwargs["timeout"] == 10


def test_expired_token_is_refreshed_and_canonical_token_is_saved(mocker):
    credentials = MagicMock(valid=False, token="new-access-token")
    credentials.to_json.return_value = token_json(token="new-access-token")
    mocker.patch(
        "colab_account_changer.tokens.Credentials.from_authorized_user_info",
        return_value=credentials,
    )
    request = mocker.patch("colab_account_changer.tokens.Request")
    mocker.patch("colab_account_changer.tokens.requests.get", return_value=response())

    inspected = inspect_token(token_json(token="old-access-token"))

    credentials.refresh.assert_called_once_with(request.return_value)
    assert json.loads(inspected.token_json)["token"] == "new-access-token"


def test_offline_inspection_does_not_make_network_requests(mocker):
    get = mocker.patch("colab_account_changer.tokens.requests.get")

    inspected = inspect_token(token_json(), verify=False)

    assert inspected.identity.email is None
    assert inspected.token_json == token_json()
    get.assert_not_called()


def test_rejected_token_has_a_safe_error(mocker):
    mocker.patch(
        "colab_account_changer.tokens.requests.get",
        return_value=response(status=400, error="invalid_token"),
    )

    with pytest.raises(TokenError, match="HTTP 400") as error:
        inspect_token(token_json())
    assert "access-token" not in str(error.value)
    assert "refresh-token" not in str(error.value)


def test_network_error_does_not_include_request_url_or_token(mocker):
    mocker.patch(
        "colab_account_changer.tokens.requests.get",
        side_effect=requests.ConnectionError(
            "https://oauth2.googleapis.com/tokeninfo?access_token=access-token"
        ),
    )

    with pytest.raises(TokenError) as error:
        inspect_token(token_json())
    assert "access-token" not in str(error.value)
    assert "refresh-token" not in str(error.value)


def test_refresh_failure_does_not_leak_secrets(mocker):
    credentials = MagicMock(valid=False)
    credentials.refresh.side_effect = ValueError("refresh-token was rejected")
    mocker.patch(
        "colab_account_changer.tokens.Credentials.from_authorized_user_info",
        return_value=credentials,
    )
    mocker.patch("colab_account_changer.tokens.Request")

    with pytest.raises(TokenError, match="refresh") as error:
        inspect_token(token_json())
    assert "refresh-token" not in str(error.value)


def test_identity_requires_an_email(mocker):
    mocker.patch(
        "colab_account_changer.tokens.requests.get",
        return_value=response(email=None),
    )
    with pytest.raises(TokenError, match="email"):
        inspect_token(token_json())
