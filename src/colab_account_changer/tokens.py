"""Validation and identity lookup for google-colab-cli OAuth tokens."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from colab_account_changer.errors import TokenError

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


@dataclass(frozen=True)
class Identity:
    email: str | None = None
    expires_in: int | None = None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class InspectedToken:
    token_json: str
    identity: Identity


def token_fingerprint(token_json: str) -> str:
    """A local change-detection digest; never a substitute for encryption."""
    return hashlib.sha256(token_json.encode("utf-8")).hexdigest()


def parse_token(token_json: str) -> dict[str, Any]:
    try:
        value = json.loads(token_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TokenError("The OAuth credential is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise TokenError("The OAuth credential must be a JSON object.")
    for field in ("refresh_token", "token_uri", "client_id", "client_secret"):
        if not isinstance(value.get(field), str) or not value[field]:
            if field == "refresh_token":
                raise TokenError(
                    "The OAuth credential has no refresh token. Sign in again to "
                    "create a reusable credential."
                )
            raise TokenError(f"The OAuth credential is missing {field!r}.")
    try:
        Credentials.from_authorized_user_info(value)
    except (TypeError, ValueError) as exc:
        raise TokenError("The OAuth credential has an invalid format.") from exc
    return value


def _lookup_identity(access_token: str) -> Identity:
    try:
        response = requests.get(
            TOKENINFO_URL,
            params={"access_token": access_token},
            timeout=10,
        )
    except requests.RequestException as exc:
        # Requests exceptions may include the URL (and therefore the token).
        raise TokenError(
            f"Could not verify the Google account ({type(exc).__name__})."
        ) from exc
    if response.status_code != 200:
        raise TokenError(
            f"Google rejected the OAuth access token (HTTP {response.status_code})."
        )
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("Google returned an invalid token identity response.") from exc
    email = value.get("email")
    if not isinstance(email, str) or not email:
        raise TokenError(
            "The token has no verified email. Re-authenticate with the "
            "https://www.googleapis.com/auth/userinfo.email scope."
        )
    try:
        expires_in = int(value.get("expires_in", ""))
    except (TypeError, ValueError):
        expires_in = None
    scopes = value.get("scope", "")
    return Identity(
        email=email,
        expires_in=expires_in,
        scopes=tuple(scopes.split()) if isinstance(scopes, str) else (),
    )


def inspect_token(token_json: str, *, verify: bool = True) -> InspectedToken:
    """Validate a token, refresh if needed, and optionally resolve its email."""
    value = parse_token(token_json)
    if not verify:
        return InspectedToken(token_json=token_json, identity=Identity())

    credentials = Credentials.from_authorized_user_info(value)
    refreshed = not credentials.valid
    if refreshed:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            # google-auth errors can contain response data; do not echo it.
            raise TokenError(
                f"Could not refresh the OAuth token ({type(exc).__name__}). "
                "Sign in to this account again."
            ) from exc
    if not credentials.token:
        raise TokenError("The OAuth credential has no access token after refresh.")
    return InspectedToken(
        token_json=credentials.to_json() if refreshed else token_json,
        identity=_lookup_identity(credentials.token),
    )
