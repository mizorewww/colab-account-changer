"""Read-only Colab compute-unit usage queries."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from colab_account_changer.errors import AccountError
from colab_account_changer.tokens import parse_token

CCU_INFO_URL = "https://colab.research.google.com/tun/m/ccu-info"
XSSI_PREFIX = ")]}'\n"


@dataclass(frozen=True)
class Usage:
    balance: float
    hourly_rate: float
    assignments: int


class UsageService:
    """Query usage directly with a profile token, without switching accounts."""

    def __init__(self, session_factory: Callable[..., Any] = AuthorizedSession) -> None:
        self.session_factory = session_factory

    def fetch(self, token_json: str) -> Usage:
        credentials = Credentials.from_authorized_user_info(parse_token(token_json))
        session = self.session_factory(credentials)
        try:
            response = session.get(
                CCU_INFO_URL,
                headers={
                    "Accept": "application/json",
                    "X-Colab-Client-Agent": "colab-cli",
                },
                params={"authuser": "0"},
                timeout=15,
            )
        except (requests.RequestException, GoogleAuthError) as exc:
            # Exceptions can contain an authorized request URL or refresh data.
            raise AccountError(
                f"Could not fetch Colab usage ({type(exc).__name__})."
            ) from exc
        if response.status_code != 200:
            raise AccountError(
                f"Colab usage request failed (HTTP {response.status_code})."
            )
        text = response.text
        if text.startswith(XSSI_PREFIX):
            text = text[len(XSSI_PREFIX) :]
        try:
            value = json.loads(text)
            balance = float(
                value.get("currentBalance", value.get("paidComputeUnitsBalance"))
            )
            hourly_rate = float(value["consumptionRateHourly"])
            assignments = int(value["assignmentsCount"])
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AccountError("Colab returned an invalid response for usage.") from exc
        return Usage(
            balance=balance,
            hourly_rate=hourly_rate,
            assignments=assignments,
        )
