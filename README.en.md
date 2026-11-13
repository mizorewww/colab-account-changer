# Colab Account Changer

**[中文](README.md) | English**

A multi-account switcher for Google Colab CLI. Discover existing accounts, switch between them, and view compute-unit balances and consumption rates.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `google-colab-cli>=0.8.0`

Upgrade Colab CLI:

```bash
uv tool upgrade google-colab-cli
colab version
```

## Installation

From the project directory:

```bash
uv tool install .
colab-account --help
```

Or run directly from the development directory:

```bash
uv sync
uv run colab-account --help
```

If the Tsinghua PyPI mirror is temporarily unavailable:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple uv sync
```

## Quick start

### 1. Discover accounts and refresh usage

```bash
colab-account list --usage
```

On the first run, the tool reads the Colab CLI keyring and the legacy `~/.config/colab-cli/token.json`, imports existing accounts, and fetches their usage.

### 2. Add an account

```bash
colab-account add work
```

Follow the terminal prompts to authorize the Google account once. The account is saved as `work`, so future switches do not require another login.

If `colab` is not on `PATH`:

```bash
colab-account add work --colab-command /absolute/path/to/colab
```

### 3. Switch accounts

```bash
colab-account use work
```

Show the active account:

```bash
colab-account current
```

### 4. List accounts and usage

```bash
# Read the previous cache without making usage requests
colab-account list

# Refresh every account and update the cache
colab-account list --usage

# Read only local profiles and cached usage, skipping discovery
colab-account list --no-discover

# Refresh the active account
colab-account usage

# Refresh a specific account
colab-account usage work
```

A plain `list` shows the balance, consumption rate, active assignments, and cache age. Missing values are shown as `—` and `never`.

## Resource pool

A Colab session is visible only to the account that created it, and running `colab sessions`/`colab exec` under another account makes Colab CLI prune the "invisible" local record. The resource pool treats your accounts as one pool: create, use, and stop machines by **session name** only. `colab-pool` is the same as `colab-account pool`.

```bash
# Create: pick an account and register (colab new options follow the name)
colab-pool new node-x --gpu g4
colab-pool new node-y --account work      # choose the account

# Use by name: switches to the owning account; args, stdin, exit codes pass through
colab-pool exec node-x -f train.py
echo 'print(1)' | colab-pool exec node-x
colab-pool upload node-x data.tgz /content/data.tgz
colab-pool cmd edit node-x /content/a.py  # any colab subcommand that takes -s

# Stop and deregister (also works if the server already dropped it)
colab-pool stop node-x

# Registry + balance/rate/VMs per account + primary account
colab-pool list
colab-pool list --json --cached
```

Account choice: the pool sticks to a primary account and only moves to the account with the highest balance (and pins it) when the primary drops below a threshold (default 5 CU).

```bash
colab-pool primary                 # show
colab-pool primary work --threshold 3
```

Reconcile and clean up:

```bash
# Compare with the servers: unregistered VMs (orphans) and vanished sessions; exit 1 on mismatch
colab-pool reconcile               # pooled accounts + accounts whose usage shows VMs
colab-pool reconcile --all --json  # probe every account

# Stop an orphan VM (asks first; --yes skips)
colab-pool reap work gpu-t4-s-xxxxxxxx

# Register a session created outside the pool
colab-pool adopt node-z work /path/to/sessions.json

# Import from the prototype pool script (read-only; original files untouched)
colab-pool import /path/to/tools/pool.json
```

The prototype script uses its own separate lock; stop using it after importing.

How it works:

- The registry `pool.json` and one session-state file per account, `pool-state/ACCOUNT.json`, live next to `config.json`, owner-only and written atomically. No tokens are stored by the pool.
- Every command takes the account lock (the same `config.lock` used by `colab-account use`), switches to the owning account, runs `colab --config pool-state/ACCOUNT.json SUB -s NAME ...`, then releases the lock. Concurrent commands queue up and each runs under the right account.
- A lock wait that times out (default 600 s, `--lock-timeout` or `COLAB_POOL_LOCK_TIMEOUT`) exits with code 75, so it is safe to retry.
- Others wait while the lock is held: keep commands short, run long jobs with `nohup` on the VM and poll briefly. `repl`/`ssh` hold the lock for the whole interactive session.
- `--offline` (or `COLAB_POOL_OFFLINE=1`) skips online identity checks when switching; `--cached` on `new`, `list`, and `reconcile` uses cached balances.

## Common commands

| Command | Purpose |
| --- | --- |
| `colab-account list` | List accounts with cached usage |
| `colab-account list --usage` | Refresh usage for all accounts |
| `colab-account current` | Show the active account without contacting Google |
| `colab-account sync` | Import existing Colab OAuth credentials |
| `colab-account add NAME` | Authorize and add an account |
| `colab-account use NAME` | Switch to an account |
| `colab-account save NAME` | Save the active account under an alias |
| `colab-account usage [NAME]` | Refresh usage for the active or named account |
| `colab-account remove NAME` | Remove an inactive account |
| `colab-pool new NAME` | Create a pooled session |
| `colab-pool exec NAME ...` | Run on a session under its owning account |
| `colab-pool stop NAME` | Stop and deregister a session |
| `colab-pool list` | Show sessions, balances, and the primary account |
| `colab-pool reconcile` | Find orphan and vanished sessions |

## Offline operations

```bash
colab-account sync --offline
colab-account list --offline
colab-account save work --offline --email you@example.com
colab-account use work --offline
```

`--offline` skips online identity verification only. Usage queries still require network access. An offline email label is not a verified identity.

If an offline save would replace an existing profile whose identity cannot be confirmed, save online first or explicitly use:

```bash
colab-account save work --force
```

## Additional options

```bash
# Switch without updating the current profile's saved credential
colab-account use work --no-save

# Explicitly allow an unsaved current credential to be discarded (dangerous)
colab-account use work --discard-unsaved

# Remove an account without confirmation
colab-account remove work --yes
```

## Notes

- Only the Colab CLI's default OAuth2 credentials are managed. ADC and gcloud logins are not switched.
- Credentials are stored in the operating system keyring. The legacy `token.json` is read-only and is not modified.
- Switching accounts does not stop, create, or migrate Colab VMs; use the resource pool to manage VMs by name.
- To clean up runtimes before switching, use `colab sessions` and `colab stop` yourself.
