#!/usr/bin/env python3
"""Add an HPC account to HPC_mentor — optionally on a different cluster/host.

Run via the `add-account` wrapper. Appends one account to your existing
~/.config/hpc_mentor/cluster.json. An account is a press-key, a label, the
usernames to query with `squeue -u`, and (optionally) its own SSH host + login
user. If you don't give a host it reuses your default cluster; give a different
host and HPC_mentor will connect to that cluster for this account.
"""
import json
import os
import sys

CFG = os.path.expanduser("~/.config/hpc_mentor/cluster.json")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def main() -> int:
    print("=== HPC_mentor: add an account ===\n")
    try:
        with open(CFG) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"No cluster config yet at {CFG}.")
        print("Run  ./set-cluster  first to create it.")
        return 1
    except Exception as exc:
        print(f"Couldn't read {CFG}: {exc}")
        return 1

    default_host = data.get("ssh_host", "")
    default_user = (data.get("ssh_user") or "").strip()
    accounts = data.setdefault("accounts", [])

    print(f"Default cluster: {default_user or '(ssh config)'}@{default_host}\n")
    print("Current accounts:")
    for a in accounts:
        h = a.get("ssh_host", default_host)
        u = a.get("ssh_user", default_user)
        print(f"  [{a.get('key')}] {a.get('label')}  ({u}@{h})  "
              f"users: {', '.join(a.get('users', []))}")
    print()

    label = ask("Label for the new account (e.g. Bob, or GPU-cluster)")
    if not label:
        print("No label entered — nothing added.")
        return 1

    host = ask("Cluster SSH host (Enter = same as default)", default_host)
    if host == default_host:
        # Same cluster: log in as yourself and just query the other user(s) with
        # `squeue -u` — no separate login needed.
        login = default_user
        users = ask("Username(s) to show with squeue -u (space-separated)").split()
    else:
        # Different cluster: needs its own login account.
        login = ask("Login username for this host")
        users = ask("Username(s) to show with squeue -u (space-separated)",
                    login).split()
    if not users:
        print("No usernames entered — nothing added.")
        return 1

    # Pick the next free numeric press-key ("a" is reserved for the all view).
    used = {str(a.get("key")) for a in accounts}
    key = 1
    while str(key) in used:
        key += 1

    entry = {"key": str(key), "label": label, "users": users}
    # Only store per-account host/user when they differ from the defaults, so
    # single-cluster configs stay clean and keep working as before.
    if host and host != default_host:
        entry["ssh_host"] = host
    if login and login != default_user:
        entry["ssh_user"] = login

    accounts.append(entry)
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(CFG, 0o600)

    eff_host = entry.get("ssh_host", default_host)
    eff_user = entry.get("ssh_user", default_user)
    print(f"\nAdded [{key}] {label}: {', '.join(users)}  on  {eff_user}@{eff_host}")
    print(f"In ./jobs, press {key} to view it (or 'a' for all accounts together).")

    if host != default_host:
        print("\nThis account is on a NEW host. Set up login to it once:")
        print(f"  ssh {eff_user}@{eff_host}          # confirm it works / accept the host key")
        print(f"  ssh-copy-id {eff_user}@{eff_host}  # optional: passwordless login")
        print("(./jobs will also prompt for this host's password at startup until then.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
