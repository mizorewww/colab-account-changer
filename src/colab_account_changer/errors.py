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


class LockTimeoutError(AccountError):
    """Another process held the account lock for longer than allowed."""

    exit_code = 75  # EX_TEMPFAIL: safe to retry later.


class PoolError(AccountChangerError):
    """A resource-pool operation cannot be completed safely."""


class PoolCapacityError(PoolError):
    """The account cannot take another session right now (limit or refusal).

    Callers should wait and retry, or stop a session; switching to another
    account is not the remedy.
    """

    exit_code = 69  # EX_UNAVAILABLE: distinct from lock timeouts and failures.
