"""Expected errors that can be shown without a traceback."""


class AccountChangerError(Exception):
    """Base class for user-facing errors."""


class ConfigError(AccountChangerError):
    """The account metadata cannot be read or written safely."""


class CredentialStoreError(AccountChangerError):
    """The operating system keyring rejected an operation."""


class TokenError(AccountChangerError):
    """A token is missing, malformed, or cannot be verified."""


class AccountError(AccountChangerError):
    """An account operation cannot be completed safely."""
