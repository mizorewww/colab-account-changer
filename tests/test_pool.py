"""Resource-pool tests with an isolated fake keyring and a fake ``colab``.

Nothing here touches the real Colab CLI, keyring, or account configuration.
"""

import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_token
from filelock import FileLock

from colab_account_changer.errors import AccountError
from colab_account_changer.pool import ColabPool, hardware_from_args, snapshot_json
from colab_account_changer.tokens import token_fingerprint
from colab_account_changer.usage import Usage

TESTS = Path(__file__).parent
POOL = [Path(sys.executable).with_name("colab-pool"), "--offline"]


def fake_token(account):
    value = json.loads(make_token(account))
    value["token"] = f"fake-access-{account}"
    return json.dumps(value)


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.config = tmp_path / "cfg" / "config.json"
        self.keyring = tmp_path / "keyring.json"
        self.server = tmp_path / "server.json"
        self.log = tmp_path / "colab-log.jsonl"
        bin_directory = tmp_path / "bin"
        bin_directory.mkdir()
        colab = bin_directory / "colab"
        colab.write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(TESTS / 'fake_colab_pool.py')!r}, "
            "run_name='__main__')\n"
        )
        colab.chmod(0o700)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "COLAB_ACCOUNT_CHANGER_CONFIG": str(self.config),
                "FAKE_KEYRING_PATH": str(self.keyring),
                "PYTHON_KEYRING_BACKEND": "fake_keyring_backend.FileKeyring",
                "PYTHONPATH": os.pathsep.join(
                    filter(None, [str(TESTS), self.environment.get("PYTHONPATH")])
                ),
                "HOME": str(tmp_path / "home"),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "COLUMNS": "200",
                "PATH": str(bin_directory),
                "FAKE_COLAB_SERVER": str(self.server),
                "FAKE_COLAB_LOG": str(self.log),
                "FAKE_COLAB_USAGE": "1",
            }
        )

    def seed(self, balances, active="alpha"):
        secrets = {"google-colab-cli\0oauth_token": fake_token(active)}
        profiles = {}
        for name, balance in balances.items():
            token = fake_token(name)
            secrets[f"colab-account-changer\0{name}"] = token
            profiles[name] = {
                "email": f"{name}@example.com",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "token_sha256": token_fingerprint(token),
                "usage": {
                    "balance": balance,
                    "hourly_rate": 1.5,
                    "assignments": 0,
                    "fetched_at": "2026-01-01T00:00:00+00:00",
                },
            }
        self.keyring.write_text(json.dumps(secrets))
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            json.dumps({"version": 1, "active_profile": active, "profiles": profiles})
        )

    def pool(self, *arguments, input=None, extra_env=None, background=False):
        environment = dict(self.environment, **(extra_env or {}))
        command = [str(part) for part in (*POOL, *arguments)]
        if background:
            return subprocess.Popen(
                command,
                env=environment,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        result = subprocess.run(
            command,
            env=environment,
            text=True,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.outputs.append(result.stdout + result.stderr)
        return result

    outputs: list

    def active(self):
        return json.loads(self.config.read_text())["active_profile"]

    def registry(self):
        path = self.config.parent / "pool.json"
        if not path.exists():
            return {"primary": None, "sessions": {}}
        return json.loads(path.read_text())

    def records(self, account):
        path = self.config.parent / "pool-state" / f"{account}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def server_state(self):
        return json.loads(self.server.read_text())

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    environment.outputs = []
    environment.seed({"alpha": 50.0, "beta": 100.0})
    yield environment
    combined = "".join(environment.outputs)
    assert "fake-access-" not in combined
    assert "refresh-" not in combined
    assert "proxy-secret-token" not in combined
    assert "Traceback" not in combined


def flat(text):
    """Undo Rich panel wrapping so long messages can be matched."""
    return " ".join(text.replace("\u2502", " ").split())


def created(env, name, account, *extra):
    result = env.pool("new", name, "--cached", "--account", account, *extra)
    assert result.returncode == 0, result.stderr
    return result


def test_new_registers_on_primary_and_rejects_duplicates(env):
    assert env.pool("primary", "alpha", "--threshold", "5").returncode == 0

    result = env.pool("new", "node-a", "--cached", "--gpu", "g4")

    assert result.returncode == 0, result.stderr
    entry = env.registry()["sessions"]["node-a"]
    assert entry["account"] == "alpha"
    assert entry["hardware"] == "g4"
    assert entry["config"] == str(
        (env.config.parent / "pool-state" / "alpha.json").resolve()
    )
    assert env.records("alpha")["node-a"]["endpoint"] == "ep-node-a-alpha"
    assert env.calls()[-1]["args"] == ["--gpu", "g4"]
    mode = (env.config.parent / "pool.json").stat().st_mode & 0o777
    assert mode == 0o600
    assert (env.config.parent / "pool-state").stat().st_mode & 0o777 == 0o700

    calls_before = len(env.calls())
    duplicate = env.pool("new", "node-a", "--cached")
    assert duplicate.returncode == 1
    assert "already pooled" in duplicate.stderr
    assert len(env.calls()) == calls_before


def test_primary_below_threshold_is_repinned_to_richest(env):
    env.seed({"alpha": 2.0, "beta": 100.0, "gamma": 30.0})
    assert env.pool("primary", "alpha").returncode == 0

    result = env.pool("new", "node-a", "--cached")

    assert result.returncode == 0, result.stderr
    assert env.registry()["sessions"]["node-a"]["account"] == "beta"
    assert env.registry()["primary"] == "beta"
    assert "alpha -> beta" in result.stderr
    assert env.active() == "beta"

    # Above the threshold the pool stays on the pinned account.
    env.seed({"alpha": 2.0, "beta": 6.0, "gamma": 30.0}, active="beta")
    created_again = env.pool("new", "node-b", "--cached")
    assert created_again.returncode == 0, created_again.stderr
    assert env.registry()["sessions"]["node-b"]["account"] == "beta"


def test_commands_switch_to_owner_without_pruning_other_accounts(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")
    assert env.active() == "beta"

    first = env.pool("exec", "node-a", input="print(1)")
    assert first.returncode == 0, first.stderr
    assert "exec node-a on alpha" in first.stdout
    assert env.active() == "alpha"

    second = env.pool("status", "node-b")
    assert second.returncode == 0, second.stderr
    assert "status node-b on beta" in second.stdout
    assert env.active() == "beta"

    # Each account's records live in their own state file and survive.
    assert "node-a" in env.records("alpha")
    assert "node-b" in env.records("beta")
    third = env.pool("ls", "node-a", "/content")
    assert third.returncode == 0, third.stderr
    assert "ls node-a on alpha args=['/content']" in third.stdout
    assert [
        call["account"] for call in env.calls() if call["subcommand"] != "sessions"
    ] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
        "alpha",
    ]


def test_arguments_stdin_and_exit_code_pass_through(env):
    created(env, "node-x", "alpha")

    result = env.pool(
        "exec", "node-x", "-f", "a.py", "--timeout", "5", "--help", input="exit:3"
    )

    assert result.returncode == 3
    assert "args=['-f', 'a.py', '--timeout', '5', '--help']" in result.stdout
    assert "stdin=exit:3" in result.stdout

    piped = env.pool("exec", "node-x", input="print('hi')\n")
    assert piped.returncode == 0
    assert "stdin=print('hi')" in piped.stdout

    generic = env.pool("cmd", "url", "node-x", "--qr")
    assert generic.returncode == 0
    assert "url node-x on alpha args=['--qr']" in generic.stdout

    missing = env.pool("exec", "nope")
    assert missing.returncode == 1
    assert "not in the pool" in missing.stderr


def test_pruned_session_is_deregistered_after_server_confirms_loss(env):
    created(env, "node-a", "alpha")
    server = env.server_state()
    del server["alpha"]["ep-node-a-alpha"]
    env.server.write_text(json.dumps(server))

    result = env.pool(
        "exec",
        "node-a",
        input="1",
        extra_env={"FAKE_COLAB_PRUNE_RECORD": "1"},
    )

    assert result.returncode == 1
    assert "appears to be lost (404/401)" in result.stdout
    assert "Session 'node-a' vanished" in flat(result.stderr)
    assert "registration and local Colab record were removed" in flat(result.stderr)
    assert "pool new" not in result.stderr
    assert "pool reconcile" in flat(result.stderr)
    assert env.registry()["sessions"] == {}
    assert "node-a" not in env.records("alpha")
    assert env.calls()[-1]["subcommand"] == "sessions"
    assert not list((env.config.parent / "pool-state").glob(".probe-*"))

    # The same name can be recreated immediately after the confirmed loss.
    created(env, "node-a", "alpha")
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"


def test_pruned_record_is_retained_while_server_assignment_survives(env):
    created(env, "node-a", "alpha")

    result = env.pool(
        "exec",
        "node-a",
        input="1",
        extra_env={"FAKE_COLAB_PRUNE_RECORD": "1"},
    )

    assert result.returncode == 1
    assert "still assigned on account 'alpha'" in result.stderr
    assert "Keeping the pool registration" in result.stderr
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert "node-a" not in env.records("alpha")
    assert "ep-node-a-alpha" in env.server_state()["alpha"]
    assert env.calls()[-1]["subcommand"] == "sessions"


def test_pruned_record_is_retained_when_server_probe_fails(env):
    created(env, "node-a", "alpha")

    result = env.pool(
        "exec",
        "node-a",
        input="1",
        extra_env={
            "FAKE_COLAB_PRUNE_RECORD": "1",
            "FAKE_COLAB_SESSIONS_FAIL": "1",
        },
    )

    assert result.returncode == 1
    assert "server state could not be verified" in result.stderr
    assert "Keeping the pool registration" in result.stderr
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert env.calls()[-1]["subcommand"] == "sessions"


def test_masked_auth_failure_does_not_deregister_pruned_session(env):
    created(env, "node-a", "alpha")

    result = env.pool(
        "exec",
        "node-a",
        input="1",
        extra_env={"FAKE_COLAB_PRUNE_RECORD": "1", "FAKE_COLAB_AUTH_FAIL": "alpha"},
    )

    assert result.returncode == 1
    assert "server state could not be verified" in flat(result.stderr)
    assert "1 active assignment(s)" in flat(result.stderr)
    assert "Keeping the pool registration" in flat(result.stderr)
    assert "vanished" not in flat(result.stderr)
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert "ep-node-a-alpha" in env.server_state()["alpha"]


@pytest.mark.parametrize(
    "extra_env, message",
    [
        ({"FAKE_COLAB_AUTH_FAIL": "alpha"}, "1 active assignment(s)"),
        (
            {"FAKE_COLAB_AUTH_FAIL": "alpha", "FAKE_COLAB_USAGE_FAIL": "alpha"},
            "could not be cross-checked",
        ),
        ({"FAKE_COLAB_SESSIONS_GARBAGE": "1"}, "neither session lines"),
    ],
)
def test_reconcile_reports_untrustworthy_empty_listing(env, extra_env, message):
    created(env, "node-a", "alpha")

    result = env.pool("reconcile", "--all", "--json", extra_env=extra_env)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["consistent"] is False
    assert "alpha" in report["errors"]
    assert message in flat(report["errors"]["alpha"])
    assert [entry["name"] for entry in report["vanished"]] == []
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"


def test_empty_listing_is_trusted_when_usage_confirms_zero(env):
    # beta's login fails, but it really has no VM: ccu-info agrees, so the
    # listing is accepted instead of being reported as an error.
    result = env.pool(
        "reconcile", "--all", "--json", extra_env={"FAKE_COLAB_AUTH_FAIL": "beta"}
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["errors"] == {}


def test_stop_all_does_not_trust_masked_auth_failure(env):
    created(env, "node-b", "beta")
    env.server.write_text(
        json.dumps(dict(env.server_state(), alpha={"ep-orphan": "T4"}))
    )

    result = env.pool("stop-all", "--yes", extra_env={"FAKE_COLAB_AUTH_FAIL": "alpha"})

    assert result.returncode == 1
    assert "Could not inspect alpha" in flat(result.stderr)
    assert "Stop-all incomplete:" in flat(result.stderr)
    assert "Verified: no server instances" not in result.stdout
    assert env.server_state()["alpha"] == {"ep-orphan": "T4"}
    assert env.server_state()["beta"] == {}


def test_stop_deregisters_even_when_server_lost_the_session(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")

    stopped = env.pool("stop", "node-a")
    assert stopped.returncode == 0, stopped.stderr
    assert "node-a" not in env.registry()["sessions"]
    assert env.server_state()["alpha"] == {}

    server = env.server_state()
    server["beta"] = {}
    env.server.write_text(json.dumps(server))
    gone = env.pool("stop", "node-b")
    # 'colab stop' fails to unassign the vanished endpoint; the pool verifies
    # that it is gone and also removes the leftover Colab record.
    assert gone.returncode == 0, gone.stderr
    assert "no longer assigned on account 'beta'" in flat(gone.stderr)
    assert env.registry()["sessions"] == {}
    assert "node-b" not in env.records("beta")

    created(env, "node-c", "alpha")
    forgotten = env.pool("stop", "node-c", "--forget")
    assert forgotten.returncode == 0
    assert env.registry()["sessions"] == {}
    assert "ep-node-c-alpha" in env.server_state()["alpha"]


def forget_leftover(env, name, account, *, vanish):
    """Leave an unpooled record behind, as 'stop --forget' does."""
    created(env, name, account)
    assert env.pool("stop", name, "--forget").returncode == 0
    if vanish:
        server = env.server_state()
        del server[account][f"ep-{name}-{account}"]
        env.server.write_text(json.dumps(server))
    assert name in env.records(account)


def test_new_reuses_name_after_leftover_record_is_confirmed_gone(env):
    forget_leftover(env, "node-x", "alpha", vanish=True)

    result = env.pool("new", "node-x", "--cached", "--account", "alpha")

    assert result.returncode == 0, result.stderr
    assert "Removed the stale Colab record of 'node-x'" in flat(result.stderr)
    assert env.registry()["sessions"]["node-x"]["account"] == "alpha"
    assert env.records("alpha")["node-x"]["endpoint"] == "ep-node-x-alpha"
    assert "ep-node-x-alpha" in env.server_state()["alpha"]


def test_new_refuses_name_whose_leftover_record_is_live_or_unverifiable(env):
    forget_leftover(env, "node-x", "alpha", vanish=False)
    before = env.records("alpha")

    live = env.pool("new", "node-x", "--cached", "--account", "alpha")
    assert live.returncode == 1
    assert "still assigned on the server" in flat(live.stderr)
    assert "pool adopt node-x alpha" in flat(live.stderr)
    assert "pool reap alpha ep-node-x-alpha" in flat(live.stderr)

    unknown = env.pool(
        "new",
        "node-x",
        "--cached",
        "--account",
        "alpha",
        extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"},
    )
    assert unknown.returncode == 1
    assert "could not be checked" in flat(unknown.stderr)
    assert env.records("alpha") == before
    assert env.registry()["sessions"] == {}
    assert [call["subcommand"] for call in env.calls()].count("new") == 1


def test_prune_state_reports_then_removes_only_stale_records(env):
    forget_leftover(env, "node-a", "alpha", vanish=True)
    forget_leftover(env, "node-c", "beta", vanish=False)
    created(env, "node-b", "beta")

    failed = env.pool(
        "prune-state", "--yes", "--json", extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"}
    )
    assert failed.returncode == 1
    assert set(json.loads(failed.stdout)["errors"]) == {"alpha", "beta"}
    assert "node-a" in env.records("alpha")

    report = env.pool("prune-state", "--json")
    assert report.returncode == 0, report.stderr
    payload = json.loads(report.stdout)
    assert payload["stale"] == [
        {"account": "alpha", "name": "node-a", "endpoint": "ep-node-a-alpha"}
    ]
    assert payload["live"] == [
        {"account": "beta", "name": "node-c", "endpoint": "ep-node-c-beta"}
    ]
    assert payload["removed"] == []
    assert "node-a" in env.records("alpha")

    table = env.pool("prune-state")
    assert table.returncode == 0
    assert "stale (gone from server)" in table.stdout
    assert "prune-state --yes" in table.stdout

    applied = env.pool("prune-state", "--yes")
    assert applied.returncode == 0, applied.stderr
    assert "removed" in applied.stdout
    assert "node-a" not in env.records("alpha")
    assert set(env.records("beta")) == {"node-b", "node-c"}
    assert set(env.registry()["sessions"]) == {"node-b"}
    assert env.pool("prune-state", "--json").returncode == 0


def test_stop_all_stops_pooled_orphan_and_vanished_sessions(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")
    server = env.server_state()
    del server["beta"]["ep-node-b-beta"]
    server["beta"]["ep-orphan"] = "T4"
    env.server.write_text(json.dumps(server))

    calls_before = len(env.calls())
    declined = env.pool("stop-all", input="n\n")
    assert declined.returncode != 0
    assert len(env.calls()) == calls_before
    assert env.server_state()["alpha"]

    result = env.pool("stop-all", "--yes")

    assert result.returncode == 0, result.stderr
    assert "node-a" in result.stdout
    assert "node-b" in result.stdout
    assert "ep-orphan" in result.stdout
    assert "Verified: no server instances" in result.stdout
    assert env.registry()["sessions"] == {}
    assert all(not assignments for assignments in env.server_state().values()), (
        env.server_state()
    )
    assert not list((env.config.parent / "pool-state").glob(".reap-*"))


def test_stop_all_continues_after_independent_failures(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")
    server = env.server_state()
    server["beta"]["ep-orphan"] = "T4"
    env.server.write_text(json.dumps(server))

    result = env.pool(
        "stop-all",
        "--yes",
        extra_env={"FAKE_COLAB_STOP_FAIL": "node-b,ep-orphan"},
    )

    assert result.returncode == 1
    assert "node-a" in result.stdout
    assert "ep-orphan" in result.stderr
    assert "Remaining instance:" in result.stderr
    assert "Stop-all incomplete:" in result.stderr
    assert "Verified: no server instances" not in result.stdout
    assert env.server_state()["alpha"] == {}
    assert set(env.server_state()["beta"]) == {"ep-node-b-beta", "ep-orphan"}
    assert set(env.registry()["sessions"]) == {"node-b"}


def test_stop_all_does_not_claim_success_when_verification_fails(env):
    result = env.pool(
        "stop-all",
        "--yes",
        extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "Could not inspect alpha" in result.stderr
    assert "Could not inspect beta" in result.stderr
    assert "Stop-all incomplete:" in result.stderr
    assert "Verified: no server instances" not in result.stdout


def test_new_failure_is_reported_and_not_registered(env):
    result = env.pool(
        "new",
        "node-a",
        "--cached",
        "--account",
        "beta",
        "--gpu",
        "a100",
        extra_env={"FAKE_COLAB_REFUSE": "beta"},
    )

    assert result.returncode == 1
    assert "could not create session 'node-a' on account 'beta'" in result.stderr
    assert "--account" in result.stderr
    assert "Allocation refused" in result.stderr
    assert env.registry()["sessions"] == {}

    unknown = env.pool("new", "node-a", "--cached", "--account", "nobody")
    assert unknown.returncode == 1
    assert "not a saved colab-account profile" in unknown.stderr


def test_new_failing_after_allocation_releases_the_vm(env):
    result = env.pool(
        "new",
        "node-a",
        "--cached",
        "--account",
        "alpha",
        extra_env={"FAKE_COLAB_NEW_FAIL_AFTER_ASSIGN": "1"},
    )

    assert result.returncode == 1
    assert "already been allocated (endpoint ep-node-a-alpha)" in flat(result.stderr)
    assert "released and verified gone" in flat(result.stderr)
    assert env.server_state()["alpha"] == {}
    assert env.registry()["sessions"] == {}
    assert "node-a" not in env.records("alpha")
    # The same name works on the next attempt.
    created(env, "node-a", "alpha")


def test_new_failing_after_allocation_registers_vm_it_cannot_release(env):
    result = env.pool(
        "new",
        "node-a",
        "--cached",
        "--account",
        "alpha",
        extra_env={
            "FAKE_COLAB_NEW_FAIL_AFTER_ASSIGN": "1",
            "FAKE_COLAB_STOP_FAIL": "node-a",
        },
    )

    assert result.returncode == 1
    assert "registered as 'node-a' so it is not orphaned" in flat(result.stderr)
    assert "pool stop node-a" in flat(result.stderr)
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert "ep-node-a-alpha" in env.server_state()["alpha"]

    stopped = env.pool("stop", "node-a")
    assert stopped.returncode == 0, stopped.stderr
    assert env.server_state()["alpha"] == {}
    assert env.registry()["sessions"] == {}


def test_new_success_without_record_is_an_error(env):
    result = env.pool(
        "new",
        "node-a",
        "--cached",
        "--account",
        "alpha",
        extra_env={"FAKE_COLAB_NEW_NO_RECORD": "1"},
    )

    assert result.returncode == 1
    assert "reported success" in flat(result.stderr)
    assert "wrote no session record" in flat(result.stderr)
    assert (
        "A new unregistered VM appeared on account 'alpha' during the attempt: "
        "ep-node-a-alpha"
    ) in flat(result.stderr)
    assert "pool reap alpha ENDPOINT" in flat(result.stderr)
    assert env.registry()["sessions"] == {}


def test_reconcile_finds_orphans_and_vanished_without_pruning(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")
    assert env.pool("reconcile", "--cached").returncode == 0
    probes_from = len(env.calls())

    server = env.server_state()
    server["beta"]["ep-orphan"] = "T4"
    del server["alpha"]["ep-node-a-alpha"]
    env.server.write_text(json.dumps(server))

    result = env.pool("reconcile", "--cached", "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["consistent"] is False
    assert report["orphans"] == [
        {"account": "beta", "endpoint": "ep-orphan", "hardware": "t4", "session": None}
    ]
    assert [entry["name"] for entry in report["vanished"]] == ["node-a"]
    # The probe used a throw-away state file, so no real record was pruned.
    assert "node-a" in env.records("alpha")
    assert "node-b" in env.records("beta")
    assert all(call["subcommand"] == "sessions" for call in env.calls()[probes_from:])
    assert not list((env.config.parent / "pool-state").glob(".probe-*"))

    table = env.pool("reconcile", "--cached")
    assert table.returncode == 1
    assert "orphan" in table.stdout
    assert "gone from server" in table.stdout


def test_reconcile_all_probes_accounts_without_pooled_sessions(env):
    server = {"beta": {"ep-stray": "NONE"}}
    env.server.write_text(json.dumps(server))

    assert env.pool("reconcile", "--cached").returncode == 0
    result = env.pool("reconcile", "--all", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["orphans"][0]["endpoint"] == "ep-stray"


def test_reap_stops_orphan_by_endpoint(env):
    created(env, "node-b", "beta")
    server = env.server_state()
    server["beta"]["ep-orphan"] = "T4"
    env.server.write_text(json.dumps(server))

    refused = env.pool("reap", "beta", "ep-node-b-beta", "--yes")
    assert refused.returncode == 1
    assert "pool stop node-b" in refused.stderr

    declined = env.pool("reap", "beta", "ep-orphan", input="n\n")
    assert declined.returncode != 0
    assert "ep-orphan" in env.server_state()["beta"]

    reaped = env.pool("reap", "beta", "ep-orphan", "--yes")
    assert reaped.returncode == 0, reaped.stderr
    assert env.server_state()["beta"] == {"ep-node-b-beta": "NONE"}
    assert "node-b" in env.records("beta")
    assert not list((env.config.parent / "pool-state").glob(".reap-*"))

    absent = env.pool("reap", "alpha", "ep-orphan", "--yes")
    assert absent.returncode == 1
    assert "not assigned on account 'alpha'" in absent.stderr


def test_adopt_and_import_existing_sessions(env, tmp_path):
    outside = tmp_path / "node" / ".colab.json"
    outside.parent.mkdir()
    outside.write_text(
        json.dumps(
            {
                "old": {
                    "name": "old",
                    "token": "proxy-secret-token",
                    "url": "https://proxy.invalid",
                    "endpoint": "ep-old",
                    "accelerator": "G4",
                }
            }
        )
    )
    env.server.write_text(json.dumps({"beta": {"ep-old": "G4"}}))

    missing = env.pool("adopt", "other", "beta", str(outside))
    assert missing.returncode == 1
    assert "no Colab session record" in missing.stderr

    adopted = env.pool("adopt", "old", "beta", str(outside))
    assert adopted.returncode == 0, adopted.stderr
    entry = env.registry()["sessions"]["old"]
    assert entry["config"] == str(outside.resolve())
    assert entry["hardware"] == "g4"
    used = env.pool("exec", "old", input="1")
    assert used.returncode == 0
    assert "exec old on beta" in used.stdout

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pool.json").write_text(
        json.dumps(
            {
                "old": {"account": "beta", "config": str(outside), "hw": "adopted"},
                "ghost": {"account": "nobody", "config": str(outside)},
            }
        )
    )
    (legacy / ".colab-primary").write_text("beta\n")
    imported = env.pool("import", str(legacy))
    assert imported.returncode == 1
    assert "Skipped ghost" in imported.stderr
    assert "Skipped old" in imported.stderr
    assert env.registry()["primary"] == "beta"

    env.pool("stop", "old", "--forget")
    again = env.pool("import", str(legacy / "pool.json"))
    assert "Imported 1 session(s)" in again.stdout
    assert env.registry()["sessions"]["old"]["account"] == "beta"


def test_list_json_and_table(env):
    created(env, "node-a", "alpha", "--gpu", "t4")
    env.pool("primary", "beta")

    result = env.pool("list", "--cached", "--json")

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["primary"] == "beta"
    assert snapshot["usage_mode"] == "cached"
    assert snapshot["active"] == "beta"
    assert snapshot["threshold"] == 5.0
    assert snapshot["sessions"][0]["endpoint"] == "ep-node-a-alpha"
    assert snapshot["server"] == [
        {
            "account": "alpha",
            "endpoint": "ep-node-a-alpha",
            "hardware": "t4",
            "session": "node-a",
        }
    ]
    assert snapshot["orphans"] == []
    assert snapshot["vanished"] == []
    assert snapshot["server_inspection"] == {
        "accounts": ["alpha", "beta"],
        "accounts_succeeded": 2,
        "complete": True,
        "errors": {},
    }
    accounts = {account["name"]: account for account in snapshot["accounts"]}
    assert accounts["alpha"]["pool_sessions"] == 1
    assert accounts["beta"]["balance"] == 100.0
    assert accounts["beta"]["primary"] is True
    assert accounts["alpha"]["hours_remaining"] == pytest.approx(50.0 / 1.5)
    assert snapshot["total"] == {
        "balance": 150.0,
        "hourly_rate": 3.0,
        "hours_remaining": 50.0,
        "assignments": 0,
        "accounts_with_usage": 2,
        "account_count": 2,
        "complete": True,
    }

    table = env.pool("list", "--cached")
    assert table.returncode == 0
    assert "node-a" in table.stdout
    assert "pooled" in table.stdout
    assert "cached" in table.stdout
    assert "2/2 accounts inspected" in table.stdout
    assert "100.00" in table.stdout
    assert "Total:" in table.stdout
    assert "150.00" in table.stdout
    assert "50.0" in table.stdout

    shown = env.pool("primary", "--json")
    assert json.loads(shown.stdout) == {"primary": "beta", "threshold": 5.0}
    via_main = subprocess.run(
        [Path(sys.executable).with_name("colab-account"), "pool", "primary"],
        env=env.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert via_main.returncode == 0
    assert "beta" in via_main.stdout


def test_list_shows_orphans_and_vanished_across_all_accounts(env):
    created(env, "node-a", "alpha")
    server = env.server_state()
    del server["alpha"]["ep-node-a-alpha"]
    server.setdefault("beta", {})["ep-orphan"] = "T4"
    env.server.write_text(json.dumps(server))

    result = env.pool("list", "--cached", "--json")

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["server"][0]["account"] == "beta"
    assert snapshot["server"][0]["session"] is None
    assert snapshot["orphans"][0]["endpoint"] == "ep-orphan"
    assert [entry["name"] for entry in snapshot["vanished"]] == ["node-a"]
    # Listing uses throw-away probes and does not prune real local records.
    assert "node-a" in env.records("alpha")

    table = env.pool("list", "--cached")
    assert table.returncode == 0
    assert "orphan" in table.stdout
    assert "vanished" in table.stdout


def test_list_reports_partial_server_inspection_failure(env):
    result = env.pool(
        "list",
        "--cached",
        "--json",
        extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"},
    )

    assert result.returncode == 1
    snapshot = json.loads(result.stdout)
    assert snapshot["total"]["balance"] == 150.0
    assert snapshot["server"] == []
    assert snapshot["server_inspection"]["accounts_succeeded"] == 0
    assert snapshot["server_inspection"]["complete"] is False
    assert set(snapshot["server_inspection"]["errors"]) == {"alpha", "beta"}


def test_concurrent_commands_are_serialized_on_the_right_account(env):
    created(env, "node-a", "alpha")
    created(env, "node-b", "beta")
    delay = {"FAKE_COLAB_DELAY": "1"}

    processes = [
        env.pool("exec", name, background=True, extra_env=delay)
        for name in ("node-a", "node-b", "node-a")
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        assert "exec node-" in stdout

    calls = [call for call in env.calls() if call["subcommand"] == "exec"]
    assert len(calls) == 3
    for call in calls:
        assert call["account"] == {"node-a": "alpha", "node-b": "beta"}[call["name"]]
    calls.sort(key=lambda call: call["start"])
    for earlier, later in itertools.pairwise(calls):
        assert earlier["end"] <= later["start"]


def test_lock_timeout_is_reported_with_retry_exit_code(env):
    created(env, "node-a", "alpha")
    lock = FileLock(env.config.with_suffix(".lock"))
    with lock:
        started = time.monotonic()
        result = env.pool("--lock-timeout", "0.5", "exec", "node-a", input="1")
        elapsed = time.monotonic() - started

    assert result.returncode == 75
    assert "Timed out after 0.5s" in result.stderr
    assert elapsed < 30
    assert [call["subcommand"] for call in env.calls()] == ["sessions", "new"]


def test_limits_are_shown_set_and_validated(env):
    shown = env.pool("limits", "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == {"max_sessions": 12, "max_per_account": 4}

    updated = env.pool("limits", "--max-per-account", "2", "--json")
    assert json.loads(updated.stdout) == {"max_sessions": 12, "max_per_account": 2}
    assert env.registry()["limits"] == {"max_per_account": 2}

    table = env.pool("limits", "--max-sessions", "3")
    assert table.returncode == 0
    assert "Max sessions" in table.stdout
    assert env.registry()["limits"] == {"max_sessions": 3, "max_per_account": 2}

    invalid = env.pool("limits", "--max-sessions", "0")
    assert invalid.returncode == 2
    assert env.registry()["limits"]["max_sessions"] == 3


def test_new_refuses_at_limits_and_lists_current_sessions(env):
    limits = env.pool("limits", "--max-sessions", "3", "--max-per-account", "2")
    assert limits.returncode == 0
    created(env, "node-a", "alpha")
    created(env, "node-b", "alpha")

    per_account = env.pool("new", "node-c", "--cached", "--account", "alpha")
    assert per_account.returncode == 1
    message = flat(per_account.stderr)
    assert "account 'alpha' already has 2 (limit 2)" in message
    assert "node-a (alpha), node-b (alpha)" in message
    assert "pool limits" in message

    created(env, "node-c", "beta")
    total = env.pool("new", "node-d", "--cached", "--account", "beta")
    assert total.returncode == 1
    assert "the pool already has 3 session(s) (limit 3)" in flat(total.stderr)
    assert [call["subcommand"] for call in env.calls()].count("new") == 3
    assert set(env.registry()["sessions"]) == {"node-a", "node-b", "node-c"}


def test_limits_count_orphans_only_when_the_probe_is_trusted(env):
    assert env.pool("limits", "--max-per-account", "2").returncode == 0
    created(env, "node-a", "alpha")
    server = env.server_state()
    server["alpha"]["ep-orphan"] = "T4"
    env.server.write_text(json.dumps(server))

    refused = env.pool("new", "node-b", "--cached", "--account", "alpha")
    assert refused.returncode == 1
    assert "account 'alpha' already has 2 (limit 2)" in flat(refused.stderr)
    assert "Unregistered VMs on 'alpha': ep-orphan" in flat(refused.stderr)

    unverified = env.pool(
        "new",
        "node-b",
        "--cached",
        "--account",
        "alpha",
        extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"},
    )
    assert unverified.returncode == 0, unverified.stderr
    assert "limits count registered sessions only" in flat(unverified.stderr)
    assert set(env.registry()["sessions"]) == {"node-a", "node-b"}


def test_retry_loop_with_drifting_names_is_bounded(env):
    # A half-created VM that cannot be released stays registered, so a loop
    # that keeps retrying 'new' under fresh names stops at the account limit.
    names = [f"node-x{suffix}" for suffix in ("", "b", "c", "d", "e", "f")]
    for name in names:
        result = env.pool(
            "new",
            name,
            "--cached",
            "--account",
            "alpha",
            extra_env={
                "FAKE_COLAB_NEW_FAIL_AFTER_ASSIGN": "1",
                "FAKE_COLAB_STOP_FAIL": name,
            },
        )
        assert result.returncode == 1

    assert [call["subcommand"] for call in env.calls()].count("new") == 4
    assert set(env.registry()["sessions"]) == set(names[:4])
    assert len(env.server_state()["alpha"]) == 4
    assert "(limit 4)" in flat(env.outputs[-1])


def test_new_if_missing_reuses_live_and_recreates_vanished(env):
    created(env, "node-a", "alpha", "--gpu", "g4")
    if_missing = ["new", "--if-missing", "node-a", "--cached", "--account", "alpha"]

    reused = env.pool(*if_missing, "--gpu", "g4")
    assert reused.returncode == 0, reused.stderr
    assert "node-a already exists on account alpha" in flat(reused.stdout)
    assert "ep-node-a-alpha" in reused.stdout
    assert [call["subcommand"] for call in env.calls()].count("new") == 1

    duplicate = env.pool("new", "node-a", "--cached", "--account", "alpha")
    assert duplicate.returncode == 1
    assert "--if-missing" in flat(duplicate.stderr)

    unknown = env.pool(*if_missing, extra_env={"FAKE_COLAB_SESSIONS_FAIL": "1"})
    assert unknown.returncode == 1
    assert "Not creating a duplicate" in flat(unknown.stderr)

    server = env.server_state()
    del server["alpha"]["ep-node-a-alpha"]
    env.server.write_text(json.dumps(server))
    recreated = env.pool(*if_missing, "--gpu", "g4")
    assert recreated.returncode == 0, recreated.stderr
    message = flat(recreated.stderr)
    assert "no longer assigned; removed it and creating it again" in message
    assert [call["subcommand"] for call in env.calls()].count("new") == 2
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert env.server_state()["alpha"] == {"ep-node-a-alpha": "G4"}


def test_hardware_label_from_new_arguments():
    assert hardware_from_args(["--gpu", "G4"]) == "g4"
    assert hardware_from_args(["--tpu=v5e1"]) == "v5e1"
    assert hardware_from_args(["--high-mem"]) == "cpu"


@pytest.mark.parametrize("missing_credential", [False, True])
def test_live_total_excludes_stale_and_uncredentialed_usage(
    manager, credentials, mocker, missing_credential
):
    now = "2026-01-01T00:00:00+00:00"
    balances = {"alpha": 50.0, "beta": 100.0}
    for name in balances:
        token = make_token(name)
        credentials.write_profile(name, token)
    config = {
        "version": 1,
        "active_profile": "alpha",
        "profiles": {
            name: {
                "email": f"{name}@example.com",
                "created_at": now,
                "updated_at": now,
                "token_sha256": token_fingerprint(make_token(name)),
                "usage": {
                    "balance": balance,
                    "hourly_rate": 1.5,
                    "assignments": 0,
                    "fetched_at": now,
                },
            }
            for name, balance in balances.items()
        },
    }
    manager.config.save(config)
    if missing_credential:
        credentials.delete_profile("beta")

    def fetch(raw):
        if raw == make_token("alpha"):
            return Usage(49.0, 1.0, 1)
        raise AccountError("usage denied")

    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.side_effect = fetch
    pool = ColabPool(manager, verify=False)

    snapshot = pool.snapshot(refresh=True)
    payload = snapshot_json(snapshot)

    assert snapshot.errors == ({} if missing_credential else {"beta": "usage denied"})
    assert payload["usage_mode"] == "live"
    assert payload["total"] == {
        "balance": 49.0,
        "hourly_rate": 1.0,
        "hours_remaining": 49.0,
        "assignments": 1,
        "accounts_with_usage": 1,
        "account_count": 2,
        "complete": False,
    }
    accounts = {account["name"]: account for account in payload["accounts"]}
    assert accounts["alpha"]["usage_in_total"] is True
    assert accounts["beta"]["balance"] == 100.0  # Last-known-good remains visible.
    assert accounts["beta"]["usage_in_total"] is False
    assert manager.usage_service.fetch.call_count == (1 if missing_credential else 2)

    cached = snapshot_json(pool.snapshot(refresh=False))
    assert cached["usage_mode"] == "cached"
    assert cached["total"]["balance"] == 149.0
    assert cached["total"]["complete"] is True


def test_new_refreshes_usage_before_choosing(manager, credentials, mocker):
    credentials.write_profile("alpha", make_token("alpha"))
    credentials.write_profile("beta", make_token("beta"))
    now = "2026-01-01T00:00:00+00:00"
    config = {
        "version": 1,
        "active_profile": "alpha",
        "profiles": {
            name: {
                "email": f"{name}@example.com",
                "created_at": now,
                "updated_at": now,
                "token_sha256": token_fingerprint(make_token(name)),
                "usage": {
                    "balance": 99.0,
                    "hourly_rate": 0.0,
                    "assignments": 0,
                    "fetched_at": now,
                },
            }
            for name in ("alpha", "beta")
        },
    }
    manager.config.save(config)
    credentials.write_colab(make_token("alpha"))
    balances = {"refresh-alpha": 1.0, "refresh-beta": 40.0}
    manager.usage_service = mocker.Mock()
    manager.usage_service.fetch.side_effect = lambda raw: Usage(
        balances[json.loads(raw)["refresh_token"]], 1.0, 0
    )

    pool = ColabPool(manager, verify=False)
    pool.set_primary("alpha")
    with pool.hold(), pool.hold():  # Re-entrant: nested manager calls do not block.
        pool.refresh_usage()
        selection = pool._select()

    assert selection.account == "beta"
    assert selection.repinned_from == "alpha"
    assert pool.store.load()["primary"] == "beta"
    assert manager.usage_service.fetch.call_count == 2


def test_missing_active_profile_credential_cannot_substitute_current_login(env):
    created(env, "node-a", "alpha")
    original_server = env.server_state()
    secrets = json.loads(env.keyring.read_text())
    del secrets["colab-account-changer\0alpha"]
    secrets["google-colab-cli\0oauth_token"] = fake_token("beta")
    env.keyring.write_text(json.dumps(secrets))

    result = env.pool("stop-all", "--yes")

    assert result.returncode == 1
    assert "Active profile 'alpha' has no saved credential" in result.stderr
    assert "Verified: no server instances" not in result.stdout
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert "node-a" in env.records("alpha")
    assert env.server_state() == original_server
    # No Colab subprocess ran under the unrelated beta login.
    assert [call["subcommand"] for call in env.calls()] == ["sessions", "new"]

    listed = env.pool("list", "--cached", "--json")
    assert listed.returncode == 1
    payload = json.loads(listed.stdout)
    assert payload["server"] == []
    assert set(payload["server_inspection"]["errors"]) == {"alpha", "beta"}
    assert env.registry()["sessions"]["node-a"]["account"] == "alpha"
    assert env.server_state() == original_server
    assert [call["subcommand"] for call in env.calls()] == ["sessions", "new"]


def test_mismatched_upstream_credential_is_not_used(env):
    created(env, "node-a", "alpha")
    secrets = json.loads(env.keyring.read_text())
    secrets["google-colab-cli\0oauth_token"] = fake_token("intruder")
    env.keyring.write_text(json.dumps(secrets))

    result = env.pool("exec", "node-a", input="1")

    assert result.returncode == 1
    assert "no longer matches profile 'alpha'" in result.stderr
    assert [call["subcommand"] for call in env.calls()] == ["sessions", "new"]
