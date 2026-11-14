# Colab Account Changer

**[中文](README.md) | English**

A multi-account manager for Google Colab CLI with account switching, compute-unit usage, and a cross-account session pool.

## Installation

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `google-colab-cli>=0.8.0`. From the project directory, run:

```bash
uv tool install "google-colab-cli>=0.8.0"
uv tool install .
```

If Colab CLI is already installed, upgrade it with `uv tool upgrade google-colab-cli`.

## Usage

### 1. Import accounts and view usage

```bash
# Automatically import existing Colab CLI accounts and refresh usage online
colab-account list --usage

# Show accounts and cached usage without making usage requests
colab-account list

# Refresh usage for the active or a specific account
colab-account usage
colab-account usage work
```

**Total** sums the balances and consumption rates eligible for aggregation; incomplete coverage is labeled **Known total** instead of treating unknown balances as zero. A live refresh includes only accounts that succeeded and have a saved credential. Old values for failed accounts are marked `stale` / `no credential` and excluded, while `--cached` explicitly aggregates local caches. JSON exposes the same policy through `usage_mode` and per-account `usage_in_total`. **ETA** is the linear estimate `balance ÷ current CU/hr`; a positive balance with a zero rate shows `∞`. The estimate changes with assignments and usage and is not a guaranteed depletion time.

### 2. Add and switch accounts

```bash
# Follow the Google authorization prompts and save the account as work
colab-account add work

# Switch accounts
colab-account use work

# Show the active account
colab-account current
```

Other account operations:

```bash
colab-account sync          # Re-import existing Colab OAuth credentials
colab-account save work     # Save the active account as work
colab-account remove work   # Remove an inactive account
```

### 3. Manage Colab sessions by name

The pool automatically switches to the account that owns each session, so you do not need to track account-to-VM mappings yourself. `colab-pool` is equivalent to `colab-account pool`.

```bash
# Create a session with automatic account selection, or use --account
colab-pool new node-x --gpu g4
colab-pool new node-y --account work

# Run a command or upload a file to a session
colab-pool exec node-x -f train.py
colab-pool upload node-x data.tgz /content/data.tgz

# Show live server instances across all saved accounts, usage, and the primary
colab-pool list

# Skip only the usage refresh; server instances are still queried live
colab-pool list --cached

# Confirm once to stop pooled and orphan instances on every account, then verify
colab-pool stop-all

# Explicit confirmation for non-interactive scripts (all unsaved VM work is lost)
colab-pool stop-all --yes

# Check for unregistered or vanished sessions
colab-pool reconcile

# List leftover Colab records that are neither pooled nor on the server; --yes deletes them
colab-pool prune-state
```

`stop-all` processes accounts independently, so one failure does not block other instances. It makes at most three passes to handle failures or newly discovered instances. Success is reported only after the server has no instances, every account can be inspected, and local pool registrations are empty; otherwise remaining endpoints/accounts are listed and the command exits nonzero. Background launchers can still create new instances; this command does not kill local processes.

The `list` instance table shows `pooled` (matched to a local registration), `orphan` (present on the server but unregistered), and `vanished` (a local registration no longer present on the server). Names and registration times are local; endpoints and online hardware come from server queries. The table title reports account coverage, and the command exits nonzero if any account cannot be inspected so a failed query is not mistaken for zero instances. JSON output exposes the same coverage and errors under `server_inspection`. Colab CLI can print "no sessions" and exit 0 after a failed login, so an empty listing is trusted only when the account's usage endpoint (ccu-info) also reports zero assignments. A failed usage request, a nonzero assignment count, or unparseable output counts as a failed inspection: `reconcile` lists it under `errors`, `stop-all` does not report success, and automatic cleanup keeps the registration.

The pool keeps using its primary account and switches to the account with the highest balance when the primary drops below 5 CU. Set the primary account and threshold manually with:

```bash
colab-pool primary work --threshold 3
```

## Lost sessions and recovery

- Prepare task code and launch scripts before creating a VM, then upload and execute immediately instead of leaving a new instance idle.
- Pass Colab arguments directly after the session name, as in `colab-pool exec node-x -f train.py`; do not insert an extra `--` after the name for `exec`.
- `404/401 ... Cleaning up` means Colab CLI could not attach to the runtime and removed its local record. After a failed command, the pool checks the owning account's server assignments. If the endpoint is confirmed gone, it removes the stale registration so the same name can be recreated with `new`.
- If the server check fails or the endpoint is still assigned, the pool preserves the registration to avoid an unmanaged VM. Run `colab-pool reconcile --all` before retrying or recreating it.
- For an old registration already confirmed missing, run `colab-pool stop NAME` to clean it up; even if `colab stop` fails, the registration is removed once the server confirms the endpoint is gone.
- When a session confirmed gone is deregistered (automatic cleanup, `stop`, `stop-all`), the pool also deletes the same-name Colab record from that account's state file, so the name can be reused. `stop --forget` does not contact Colab and keeps the record; a later `new` with that name first checks the server: a vanished endpoint's record is removed and creation continues, a live endpoint is refused with an `adopt`/`reap` hint, and an unverifiable one is refused. `colab-pool prune-state` reports such leftover records on every account and deletes them only with `--yes`; records still assigned on the server are never deleted.
- Do not mix raw `colab` and `colab-pool` commands on the same session; raw commands do not hold the pool's account lock.

## Notes

- Only the Colab CLI's default OAuth2 credentials are managed, not ADC or gcloud logins. Credentials are stored in the operating system keyring.
- Switching accounts does not stop, create, or migrate existing Colab VMs. Use the pool to manage VMs across accounts.
- Run `colab-account --help`, `colab-account COMMAND --help`, or `colab-pool --help` to see all options.
