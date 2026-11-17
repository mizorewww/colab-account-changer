---
name: colab-pool
description: Run Google Colab sessions across several accounts with `colab-pool` and `colab-account`. Use when creating, running on, or stopping Colab VMs through the pool, checking compute-unit balances, or recovering from lost sessions, lock timeouts, or capacity errors.
---

# Colab pool operator

`colab-pool` addresses Colab sessions by name and switches to the owning account itself. Always go through it; never run raw `colab` on a pooled session (raw commands skip the pool's account lock).

## Workflow

1. Prepare code and launch scripts first, then `colab-pool new NAME --gpu g4` and upload/exec right away. Idle VMs burn compute units.
2. Prefer `colab-pool new --if-missing NAME ...` in scripts: exit 0 if the session is registered and alive.
3. Run: `colab-pool exec NAME -f script.py`. Pass Colab arguments directly after the name; do not insert `--`.
4. Stop when done: `colab-pool stop NAME`. `colab-pool stop-all --yes` stops everything on every account (unsaved work is lost).
5. Check state: `colab-pool list` (balances plus live instances per account; `--json` for machines).

## Exit codes

- **69**: the primary account is full (Colab refused, or a session limit was hit). Stop a session or retry later. **Do not switch accounts** or pass `--allow-other-account` to get around it.
- **75**: lock timeout; the pool is busy. The error lists the holders. **No session was lost; do not recreate anything.** Retry later.
- **1** from `list`/`reconcile`: some account could not be inspected (or inspection was skipped because the lock was busy). Do not read the instance list as empty.

## Recovery

- `404/401 ... Cleaning up` after a command: the pool rechecks the server. If the endpoint is confirmed gone, the registration is removed; recreate with the same name only if still needed.
- If the server check failed or the endpoint still exists, the registration is kept. Run `colab-pool reconcile --all` before retrying; never blindly recreate.
- A stale registration whose VM is confirmed gone: `colab-pool stop NAME`.
- Leftover Colab records that block a name: `colab-pool prune-state` (add `--yes` to delete).
- A failed `colab new` that still allocated a VM is released or registered automatically; the error says which. Use `colab-pool stop NAME` to release it.

## Locks

- Commands on the already-active account (`exec`, `upload`, `download`, `ls`, `status`) share the lock and run concurrently; anything that switches accounts, plus `new`/`stop`/`reap`/`reconcile`/`list`/`stop-all`, is exclusive.
- `colab-pool status` (no name) shows who holds the lock without waiting.
- Balance queries (`colab-account usage`, `list --usage`) never wait for the lock.
- `exec`, `repl`, `upload`, `download`, `ls`, `rm`, `edit`, `restart-kernel`, and `ssh` (including `--proxy-mode` tunnels, but not `--rm`) hold the lock only while starting (a few seconds); after that they no longer block account switches, `stop`, or `list`. A near-expiry proxy token is refreshed first under the owning account.
- Other session commands (`status`, `install`, `drivemount`, ...) hold the lock until they finish.

## Accounts

- `colab-account add` needs a human to finish Google sign-in; the link is copied to the clipboard. Without a name the alias is the email's local part.
- The pool uses one primary account and moves to the highest-balance account only when the primary is confirmed below the threshold (default 5 CU).
- `colab-pool disable NAME --reason ...` keeps an account out of selection; its existing sessions can still be stopped.
