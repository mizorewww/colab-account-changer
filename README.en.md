# Colab Account Changer

**[中文](README.md) | English**

Multi-account manager for Google Colab CLI: switch accounts, check compute-unit balances, and manage sessions by name across accounts.

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "google-colab-cli>=0.8.0"
uv tool install .
```

## Accounts

```bash
colab-account list --usage   # Import existing accounts and refresh balances
colab-account add            # Sign in (link copied to clipboard); alias defaults to the email name
colab-account add work       # Choose the alias
colab-account use work       # Switch accounts
colab-account current        # Show the active account
colab-account usage work     # Show one account's balance
colab-account remove work    # Remove an inactive account
```

## Session pool

`colab-pool` (same as `colab-account pool`) manages sessions by name and switches to the owning account automatically.

```bash
colab-pool new node-x --gpu g4              # Create a session on the primary account
colab-pool new --if-missing node-x --gpu g4 # Skip if it already exists
colab-pool exec node-x -f train.py          # Run a script
colab-pool upload node-x data.tgz /content/data.tgz
colab-pool list                             # Balances and server instances per account
colab-pool stop node-x                      # Stop and deregister
colab-pool stop-all                         # Stop instances on every account
colab-pool reconcile                        # Find unregistered or vanished sessions
colab-pool primary work --threshold 3       # Set the primary account and threshold
colab-pool limits --max-per-account 6       # Session limits
colab-pool disable old --reason "quota"     # Disable an account
```

See `--help` for all options, the [Wiki](https://github.com/mizorewww/colab-account-changer/wiki) for detailed behavior, and [skills/colab-pool](skills/colab-pool/SKILL.md) for operating rules aimed at AI agents.
