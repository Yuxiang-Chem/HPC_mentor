#!/usr/bin/env python3
"""Live SLURM job monitor for the XJTLU HPC cluster.

Runs locally on the Mac, SSHes into the cluster over a single multiplexed
connection, and shows a self-refreshing `squeue` table so you don't have to
type `squeue -u <name>` over and over. Switch between the group's accounts
with a keypress.

Usage:
    python tools/hpc_monitor.py [account]
    # account is one of: me | minghao | yue | all   (default: me)

Keys (while running):
    1  my jobs (yuxiangchen23)   2  Minghao   3  Yue   a  all three
    r  refresh now   +/-  change interval   q  quit
"""

from __future__ import annotations

import subprocess
import sys
import termios
import threading
import time
import tty
from collections import Counter
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# --- Configuration ----------------------------------------------------------

# SSH host alias we actually connect through (your own login). Any account's
# jobs are then queried with `squeue -u <user>` over this one connection.
SSH_HOST = "login.hpc.xjtlu.edu.cn"

# Selectable views: key -> (label, list of usernames to query).
ACCOUNTS: dict[str, tuple[str, list[str]]] = {
    "me": ("yuxiangchen23 (me)", ["yuxiangchen23"]),
    "minghao": ("minghaodeng23 (Minghao)", ["minghaodeng23"]),
    "yue": ("yuesong21 (Yue)", ["yuesong21"]),
    "all": ("all three", ["yuxiangchen23", "minghaodeng23", "yuesong21"]),
}

# Number key -> account key.
KEY_TO_ACCOUNT = {"1": "me", "2": "minghao", "3": "yue", "a": "all"}

# SSH options for connection multiplexing: authenticate once, reuse the socket
# for every poll so refreshes are fast and never re-prompt for credentials.
SSH_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/cm-hpcmon-%r@%h:%p",
    "-o", "ControlPersist=10m",
    "-o", "ServerAliveInterval=30",
    "-o", "BatchMode=no",
]

# squeue output columns, pipe-delimited. %u (User) is always included; the
# column is only shown in the table when viewing more than one user.
SQUEUE_FORMAT = "%i|%u|%j|%T|%M|%l|%D|%P|%R"
COLUMNS = ["JobID", "User", "Name", "State", "Time", "TimeLimit", "Nodes", "Partition", "Reason"]

STATE_STYLES = {
    "RUNNING": "bold green",
    "PENDING": "yellow",
    "COMPLETING": "cyan",
    "CONFIGURING": "cyan",
    "COMPLETED": "dim green",
    "FAILED": "bold red",
    "NODE_FAIL": "bold red",
    "TIMEOUT": "bold red",
    "CANCELLED": "red",
    "PREEMPTED": "red",
}

MIN_INTERVAL = 5
MAX_INTERVAL = 120


# --- Shared state shared between the keyboard thread and the main loop -------

class State:
    def __init__(self, account: str, interval: int) -> None:
        self.account = account
        self.interval = interval
        self.force_refresh = False
        self.quit = False
        self.lock = threading.Lock()


# --- SSH / squeue -----------------------------------------------------------

def run_squeue(users: list[str]) -> tuple[list[list[str]] | None, str | None]:
    """Return (rows, None) on success or (None, error_text) on failure."""
    cmd = [
        "ssh", *SSH_OPTS, SSH_HOST,
        f"squeue -u {','.join(users)} -o '{SQUEUE_FORMAT}' --noheader",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, "SSH/squeue timed out after 30s."
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Failed to launch ssh: {exc}"

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"ssh exited {proc.returncode}"
        return None, err

    rows = [line.split("|") for line in proc.stdout.splitlines() if line.strip()]
    return rows, None


# --- Rendering --------------------------------------------------------------

def build_view(state: State, rows, error, last_update: datetime) -> Group:
    label, users = ACCOUNTS[state.account]
    show_user = len(users) > 1

    header = Text()
    header.append("HPC jobs", style="bold")
    header.append(f"  ·  {SSH_HOST}", style="dim")
    header.append("  ·  viewing ", style="dim")
    header.append(label, style="bold cyan")
    header.append(f"  ·  every {state.interval}s", style="dim")
    header.append(f"  ·  updated {last_update:%H:%M:%S}", style="dim")

    if error is not None:
        body = Panel(Text(error, style="red"), title="squeue error", border_style="red")
        counts_line = Text("connection error — retrying on next tick", style="red")
    else:
        table = Table(expand=True, header_style="bold")
        for col in COLUMNS:
            if col == "User" and not show_user:
                continue
            table.add_column(col, overflow="fold")

        counter: Counter[str] = Counter()
        for r in rows:
            # Pad/truncate defensively in case a Reason field contains a pipe.
            r = (r + [""] * len(COLUMNS))[: len(COLUMNS)]
            jobid, user, name, st, t, tl, nodes, part, reason = r
            counter[st] += 1
            style = STATE_STYLES.get(st, "white")
            cells = [jobid]
            if show_user:
                cells.append(user)
            cells += [name, Text(st, style=style), t, tl, nodes, part, reason]
            table.add_row(*cells)

        if not rows:
            body = Panel(Text("No jobs in the queue.", style="dim"), border_style="green")
        else:
            body = table

        running = counter.get("RUNNING", 0)
        pending = counter.get("PENDING", 0)
        other = sum(counter.values()) - running - pending
        counts_line = Text()
        counts_line.append(f"{running} running", style="green")
        counts_line.append(" · ")
        counts_line.append(f"{pending} pending", style="yellow")
        counts_line.append(" · ")
        counts_line.append(f"{other} other", style="cyan")
        counts_line.append(f"   ({sum(counter.values())} total)", style="dim")

    footer = Text(
        "1 me  2 Minghao  3 Yue  a all   |   r refresh  +/- interval  q quit",
        style="dim",
    )

    return Group(
        Panel(header, border_style="cyan"),
        body,
        counts_line,
        footer,
    )


# --- Keyboard thread --------------------------------------------------------

def keyboard_loop(state: State) -> None:
    """Read single keypresses in raw mode and update shared state."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                continue
            with state.lock:
                if ch in ("q", "Q"):
                    state.quit = True
                    break
                elif ch in KEY_TO_ACCOUNT:
                    state.account = KEY_TO_ACCOUNT[ch]
                    state.force_refresh = True
                elif ch == "r":
                    state.force_refresh = True
                elif ch in ("+", "="):
                    state.interval = min(MAX_INTERVAL, state.interval + 5)
                elif ch in ("-", "_"):
                    state.interval = max(MIN_INTERVAL, state.interval - 5)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- Main -------------------------------------------------------------------

def main() -> int:
    start_account = "me"
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg not in ACCOUNTS:
            print(f"Unknown account '{arg}'. Choose from: {', '.join(ACCOUNTS)}")
            return 2
        start_account = arg

    if not sys.stdin.isatty():
        print("This tool needs an interactive terminal (stdin is not a TTY).")
        return 2

    state = State(account=start_account, interval=15)

    kb = threading.Thread(target=keyboard_loop, args=(state,), daemon=True)
    kb.start()

    rows: list[list[str]] | None = []
    error: str | None = None
    last_update = datetime.now()

    print("Connecting to the cluster (you may be prompted for credentials once)...")

    with Live(refresh_per_second=4, screen=True) as live:
        next_poll = 0.0
        while True:
            with state.lock:
                if state.quit:
                    break
                do_poll = state.force_refresh
                state.force_refresh = False
                interval = state.interval
                users = ACCOUNTS[state.account][1]

            now = time.monotonic()
            if do_poll or now >= next_poll:
                rows, error = run_squeue(users)
                last_update = datetime.now()
                next_poll = time.monotonic() + interval

            live.update(build_view(state, rows, error, last_update))
            time.sleep(0.2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
