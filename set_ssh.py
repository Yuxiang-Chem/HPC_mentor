#!/usr/bin/env python3
"""Set up passwordless SSH login to the cluster for HPC_mentor.

Run via the `set-ssh` wrapper. It:
  1. asks for your cluster login username and saves it to cluster.json,
  2. creates an SSH key (ed25519) if you don't have one,
  3. installs your public key on the cluster (you type your password ONCE),
  4. verifies that login no longer asks for a password.

After this, `./jobs` connects instantly with no prompt. (Even without a key, the
monitor still works with password login — this just makes it seamless.)
"""
import json
import os
import subprocess
import sys

CLUSTER_PATH = os.path.expanduser("~/.config/hpc_mentor/cluster.json")
KEY_PATH = os.path.expanduser("~/.ssh/id_ed25519")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def load_cluster() -> dict:
    try:
        with open(CLUSTER_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        print(f"No cluster config found at {CLUSTER_PATH}.")
        print("Create it first (see the README, 'Configure your cluster').")
        raise SystemExit(1)
    except Exception as exc:
        print(f"Couldn't read {CLUSTER_PATH}: {exc}")
        raise SystemExit(1)


def save_cluster(data: dict) -> None:
    os.makedirs(os.path.dirname(CLUSTER_PATH), exist_ok=True)
    with open(CLUSTER_PATH, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(CLUSTER_PATH, 0o600)


def main() -> int:
    print("=== HPC_mentor SSH setup ===\n")

    cluster = load_cluster()
    host = cluster.get("ssh_host", "")
    if not host or host == "login.your-hpc.example.edu":
        print("Your cluster.json has no real ssh_host yet — set that first.")
        return 1

    # 1. Login username -> cluster.json ("ssh_user")
    current_user = (cluster.get("ssh_user") or "").strip()
    user = ask(f"Your login username on {host}", current_user or os.environ.get("USER", ""))
    if not user:
        print("No username entered — nothing changed.")
        return 1
    if user != current_user:
        cluster["ssh_user"] = user
        save_cluster(cluster)
        print(f"  saved login user '{user}' to cluster.json")
    target = f"{user}@{host}"

    # 2. Make a key if there isn't one.
    if os.path.exists(KEY_PATH):
        print(f"\nUsing your existing key: {KEY_PATH}")
    else:
        print(f"\nNo SSH key found — creating one at {KEY_PATH}")
        print("(Press Enter at the passphrase prompts to leave it empty, "
              "which is simplest for automation.)")
        rc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", KEY_PATH]
        ).returncode
        if rc != 0 or not os.path.exists(KEY_PATH):
            print("Key generation failed or was cancelled.")
            return 1

    # 3. Install the public key on the cluster (password needed THIS one time).
    print(f"\nInstalling your public key on {target}.")
    print("You'll be asked for your cluster password one last time:\n")
    installed = subprocess.run(["ssh-copy-id", "-i", KEY_PATH + ".pub", target]).returncode
    if installed != 0:
        # ssh-copy-id may be missing (some macOS setups) — fall back to a manual append.
        print("\nssh-copy-id wasn't available or failed; trying a direct copy instead.")
        with open(KEY_PATH + ".pub") as fh:
            pub = fh.read().strip()
        remote = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{pub}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        )
        installed = subprocess.run(["ssh", target, remote]).returncode
        if installed != 0:
            print("\nCouldn't install the key. You can still use the monitor with "
                  "password login; try ./set-ssh again later.")
            return 1

    # 4. Verify passwordless login works now.
    print("\nVerifying passwordless login...")
    check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "true"],
        capture_output=True, text=True,
    )
    if check.returncode == 0:
        print("Success — login no longer needs a password.")
        print("\nRun  ./jobs  and it will connect instantly.")
        return 0

    print("Key installed, but a test login still asked for credentials:")
    err = (check.stderr or check.stdout).strip()
    if err:
        print("  " + err)
    print("The monitor will still work (it will prompt for your password once at start).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
