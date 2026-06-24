#!/usr/bin/env python3
"""Interactively create HPC_mentor's cluster config.

Run via the `set-cluster` wrapper. Asks for your SSH host, your login username,
and the accounts you want to watch (each is a label plus the usernames to query
with `squeue -u`), then writes ~/.config/hpc_mentor/cluster.json with safe
permissions.
"""
import json
import os
import sys

CFG = os.path.expanduser("~/.config/hpc_mentor/cluster.json")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def main() -> int:
    print("=== HPC_mentor cluster setup ===\n")
    print("This creates ~/.config/hpc_mentor/cluster.json so the monitor knows")
    print("which cluster to connect to and which accounts to show.\n")

    host = ask("SSH host (e.g. login.hpc.xjtlu.edu.cn)")
    if not host:
        print("No host entered — nothing saved.")
        return 1

    user = ask("Your login username on the cluster", os.environ.get("USER", ""))
    if not user:
        print("No username entered — nothing saved.")
        return 1

    # First account is you (press key "1").
    accounts = []
    my_label = ask("A short label for your own jobs", user.split("@")[0] or "Me")
    accounts.append({"key": "1", "label": my_label, "users": [user]})

    # Optionally watch more accounts (e.g. labmates).
    print("\nAdd more accounts to watch (e.g. labmates), or press Enter to finish.")
    key = 2
    while True:
        label = ask(f"  Account {key} label (Enter to finish)")
        if not label:
            break
        users = ask(f"  Username(s) for '{label}' (space-separated)").split()
        if not users:
            print("   no usernames — skipped.")
            continue
        accounts.append({"key": str(key), "label": label, "users": users})
        key += 1

    data = {"ssh_host": host, "ssh_user": user, "accounts": accounts}
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(CFG, 0o600)

    print(f"\nSaved {CFG}")
    print(f"  Host : {user}@{host}")
    for a in accounts:
        print(f"  [{a['key']}] {a['label']}: {', '.join(a['users'])}")
    print("\nNext: run  ./jobs   (it asks for your password once).")
    print("To skip the password from now on, run  ./set-ssh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
