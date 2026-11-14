"""A fake ``colab`` for pool tests: accounts, server assignments, and pruning.

The active account is read from the fake keyring's upstream credential. Server
assignments live in ``FAKE_COLAB_SERVER`` as {account: {endpoint: hardware}}.
Like Colab CLI, every per-session command prunes local records whose endpoint
is not assigned to the *current* account. Every call is logged as JSON lines.
"""

import json
import os
import sys
import time
from pathlib import Path

import keyring


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] != ["--config"]:
        return 2
    config = Path(arguments[1])
    subcommand = arguments[2]
    rest = arguments[3:]
    name = None
    if "-s" in rest:
        index = rest.index("-s")
        name = rest[index + 1]
        rest = rest[:index] + rest[index + 2 :]

    raw = keyring.get_password("google-colab-cli", "oauth_token")
    account = json.loads(raw)["token"].removeprefix("fake-access-") if raw else None
    server_path = Path(os.environ["FAKE_COLAB_SERVER"])
    server = json.loads(server_path.read_text()) if server_path.exists() else {}
    mine = server.setdefault(account, {})
    text = config.read_text() if config.exists() else ""
    records = json.loads(text) if text.strip() else {}
    started = time.time()

    def save():
        config.write_text(json.dumps(records))
        server_path.write_text(json.dumps(server))

    code = 0
    if subcommand == "new":
        refused = os.environ.get("FAKE_COLAB_REFUSE", "").split(",")
        if account in refused:
            print("[colab] Allocation refused (precondition failed).", file=sys.stderr)
            code = 1
        else:
            hardware = (
                rest[rest.index("--gpu") + 1].upper() if "--gpu" in rest else "NONE"
            )
            endpoint = f"ep-{name}-{account}"
            mine[endpoint] = hardware
            records[name] = {
                "name": name,
                "token": "proxy-secret-token",
                "url": "https://proxy.invalid",
                "endpoint": endpoint,
                "accelerator": hardware,
            }
            save()
            print("[colab] Session READY.")
    elif subcommand == "sessions":
        if os.environ.get("FAKE_COLAB_SESSIONS_FAIL") == "1":
            print("[colab] Could not list assignments.", file=sys.stderr)
            code = 1
        else:
            names = {record["endpoint"]: key for key, record in records.items()}
            for endpoint, hardware in mine.items():
                label = "CPU" if hardware == "NONE" else hardware
                print(
                    f"[{names.get(endpoint, '?')}] {endpoint} | Hardware: {label} | "
                    "Shape: Standard | Variant: DEFAULT"
                )
    else:
        for key in list(records):
            if records[key]["endpoint"] not in mine:
                del records[key]
        save()
        if subcommand == "stop":
            if name not in records:
                print(f"[colab] Session '{name}' not found.")
            else:
                del mine[records.pop(name)["endpoint"]]
                save()
                print("[colab] Session terminated.")
        elif (
            subcommand == "exec"
            and os.environ.get("FAKE_COLAB_PRUNE_RECORD") == "1"
        ):
            records.pop(name, None)
            save()
            print(
                f"[colab] Session '{name}' appears to be lost (404/401). "
                "Cleaning up."
            )
            code = 1
        elif name not in records:
            print(f"[colab] Session '{name}' not found.")
            code = 1
        else:
            data = "" if sys.stdin.isatty() else sys.stdin.read()
            time.sleep(float(os.environ.get("FAKE_COLAB_DELAY", "0")))
            print(f"{subcommand} {name} on {account} args={rest} stdin={data.strip()}")
            if data.startswith("exit:"):
                code = int(data.split(":", 1)[1])

    with Path(os.environ["FAKE_COLAB_LOG"]).open("a") as log:
        log.write(
            json.dumps(
                {
                    "account": account,
                    "subcommand": subcommand,
                    "name": name,
                    "args": rest,
                    "config": str(config),
                    "start": started,
                    "end": time.time(),
                }
            )
            + "\n"
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
