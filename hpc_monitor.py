#!/usr/bin/env python3
"""Live SLURM job monitor for the XJTLU HPC cluster.

Runs locally, SSHes into the cluster over a single multiplexed connection, and
shows a self-refreshing `squeue` table so you don't have to type
`squeue -u <name>` over and over. Switch between accounts with a keypress, and
optionally "watch" a job so you get an email the moment it finishes.

Usage:
    python hpc_monitor.py [account]
    # account is one of: yuxiang | minghao | yue | all   (default: yuxiang)

Keys (while running):
    1 Yuxiang   2 Minghao   3 Yue   a all
    w  watch a job (type its JobID, Enter) for a finish email
    c  clear all watched jobs
    r  refresh now   +/-  change interval   q  quit

Email-on-finish: configure ~/.config/hpc_mentor/config.json (see README).
"""

from __future__ import annotations

import json
import os
import select
import smtplib
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import Counter
from datetime import datetime
from email.message import EmailMessage

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# --- Configuration ----------------------------------------------------------

# Cluster host + accounts are read from this local file so the code itself
# contains no specific cluster or usernames. See README ("Configure your
# cluster"). If the file is absent, the generic placeholders below are used.
CLUSTER_PATH = os.path.expanduser("~/.config/hpc_mentor/cluster.json")

DEFAULT_CLUSTER = {
    "ssh_host": "login.your-hpc.example.edu",
    "ssh_user": "",  # login name on the cluster; empty = use your ~/.ssh/config
    "accounts": [
        {"key": "1", "label": "Me", "users": ["myusername"]},
        {"key": "2", "label": "Labmate", "users": ["labmate"]},
    ],
}


def _load_cluster() -> dict:
    try:
        with open(CLUSTER_PATH) as fh:
            data = json.load(fh)
        if data.get("ssh_host") and data.get("accounts"):
            return data
    except Exception:
        pass
    return DEFAULT_CLUSTER


_cluster = _load_cluster()

# SSH host alias we connect through. Any account's jobs are then queried with
# `squeue -u <user>` over this one connection.
SSH_HOST = _cluster["ssh_host"]

# Optional login user. If set in cluster.json we connect as user@host, so you
# don't need a matching ~/.ssh/config entry. If empty, we fall back to plain
# host and let ssh decide the user (your ~/.ssh/config or local username).
SSH_USER = (_cluster.get("ssh_user") or "").strip()
SSH_TARGET = f"{SSH_USER}@{SSH_HOST}" if SSH_USER else SSH_HOST

# True when no usable cluster.json was found and we fell back to the example
# placeholder host — there's nothing real to connect to until the user makes one.
USING_PLACEHOLDER = SSH_HOST == DEFAULT_CLUSTER["ssh_host"]

# Selectable views: press-key -> (label, list of usernames). An "all" view (key
# "a") that aggregates every account is added automatically.
ACCOUNTS: dict[str, tuple[str, list[str]]] = {}
_all_users: list[str] = []
for _a in _cluster["accounts"]:
    ACCOUNTS[_a["key"]] = (_a["label"], list(_a["users"]))
    _all_users.extend(_a["users"])
ACCOUNTS["a"] = ("all", _all_users)

DEFAULT_ACCOUNT = _cluster["accounts"][0]["key"]
KEY_TO_ACCOUNT = {k: k for k in ACCOUNTS}  # press-key selects its account directly

# Connection multiplexing. We open ONE master connection at startup (interactive,
# so a password prompt is visible) and every later poll reuses that socket.
#
# Master options: used once by ensure_connection() BEFORE the live screen takes
# over the terminal. BatchMode=no allows an interactive password/2FA prompt.
SSH_MASTER_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/cm-hpcmon-%r@%h:%p",
    "-o", "ControlPersist=10m",
    "-o", "ServerAliveInterval=30",
    "-o", "BatchMode=no",
]

# Poll options: used for every squeue/sacct call while the live display owns the
# screen. These attach to the master socket and MUST NEVER prompt — a hidden
# password prompt here would hang the UI. BatchMode=yes makes ssh fail fast (the
# UI shows a retry message) instead of blocking if the socket ever drops.
SSH_OPTS = [
    "-o", "ControlMaster=no",
    "-o", "ControlPath=~/.ssh/cm-hpcmon-%r@%h:%p",
    "-o", "ServerAliveInterval=30",
    "-o", "BatchMode=yes",
]

# squeue output columns, pipe-delimited.
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
    "OUT_OF_MEMORY": "bold red",
}

# A SLURM job is "done" (fire the email) when sacct reports one of these.
TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED",
}

MIN_INTERVAL = 5
MAX_INTERVAL = 120

CONFIG_PATH = os.path.expanduser("~/.config/hpc_mentor/config.json")
# Touched when the user declines first-run email setup, so we don't nag again.
SKIP_MARKER = os.path.expanduser("~/.config/hpc_mentor/.skip_email_setup")
# Same idea for the first-run passwordless-SSH offer.
SSH_SKIP_MARKER = os.path.expanduser("~/.config/hpc_mentor/.skip_ssh_setup")


# --- Email config -----------------------------------------------------------

def load_email_config() -> tuple[dict | None, str]:
    """Return (config, status_text). config is None if not usable."""
    if not os.path.exists(CONFIG_PATH):
        return None, "email off (no config file)"
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
    except Exception as exc:
        return None, f"email off (bad config: {exc})"

    password = os.environ.get("HPC_MENTOR_SMTP_PASS") or data.get("smtp_pass", "")
    cfg = {
        "smtp_host": data.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(data.get("smtp_port", 587)),
        "smtp_user": data.get("smtp_user", ""),
        "smtp_pass": password,
        "recipient": data.get("recipient", "") or data.get("smtp_user", ""),
    }
    if not (cfg["smtp_user"] and cfg["smtp_pass"] and cfg["recipient"]):
        return None, "email off (config missing user/password/recipient)"
    return cfg, f"email on -> {cfg['recipient']}"


def send_email(cfg: dict, subject: str, body: str) -> str | None:
    """Send one email. Return None on success, error string on failure."""
    msg = EmailMessage()
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["recipient"]
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if int(cfg["smtp_port"]) == 465:
            # Implicit TLS (QQ, 163, etc.).
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as srv:
                srv.login(cfg["smtp_user"], cfg["smtp_pass"])
                srv.send_message(msg)
        else:
            # STARTTLS (Gmail, Outlook, …).
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as srv:
                srv.starttls()
                srv.login(cfg["smtp_user"], cfg["smtp_pass"])
                srv.send_message(msg)
    except Exception as exc:
        return str(exc)
    return None


def save_recipient(cfg: dict, new_recipient: str) -> str | None:
    """Update the notification recipient in the config file. Return error or None."""
    cfg["recipient"] = new_recipient
    try:
        with open(CONFIG_PATH, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
    except Exception as exc:
        return str(exc)
    return None


# --- Shared state between the keyboard thread and the main loop --------------

class State:
    def __init__(self, account: str, interval: int) -> None:
        self.account = account
        self.interval = interval
        self.force_refresh = False
        self.quit = False
        self.watch: dict[str, str] = {}        # JobID -> last-known state (email on change)
        self.job_ids: list[str] = []           # JobIDs in current display order
        self.job_states: dict[str, str] = {}   # JobID -> current state (watch baseline)
        self.selected = 0                      # index of the highlighted row
        self.input_mode: str | None = None     # None or 'email'
        self.input_buffer = ""
        self.pending_recipient: str | None = None  # set by keyboard, applied in main
        self.email_configured = False
        self.message = ""                      # last status/notification line
        self.lock = threading.Lock()


# --- SSH / squeue / sacct ----------------------------------------------------

def ensure_connection() -> str | None:
    """Open the multiplexed SSH master connection once, interactively.

    This runs *before* the live display takes over the terminal and before the
    keyboard thread grabs stdin, so if the cluster asks for a password (or 2FA)
    you can see the prompt and type it normally. The connection is then kept open
    (ControlPersist) and every later poll reuses it without prompting again.

    Returns None on success, or a human-readable error string on failure.
    """
    # Already have a live master socket from a previous run? Then we're done.
    try:
        check = subprocess.run(
            ["ssh", *SSH_MASTER_OPTS, "-O", "check", SSH_TARGET],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode == 0:
            return None
    except Exception:
        pass  # fall through and try to establish it

    # Establish it now, inheriting the real terminal so the password prompt (if
    # any) is shown and you can type it. `true` is a no-op that just authenticates.
    try:
        proc = subprocess.run(["ssh", *SSH_MASTER_OPTS, SSH_TARGET, "true"])
    except Exception as exc:  # pragma: no cover - defensive
        return f"failed to launch ssh: {exc}"
    if proc.returncode != 0:
        return (
            f"could not connect to {SSH_TARGET} (ssh exited {proc.returncode}).\n"
            f"Check it works by hand:  ssh {SSH_TARGET}"
        )
    return None


def _ssh(remote_cmd: str, timeout: int = 30) -> tuple[str | None, str | None]:
    cmd = ["ssh", *SSH_OPTS, SSH_TARGET, remote_cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"SSH timed out after {timeout}s."
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Failed to launch ssh: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr.strip() or proc.stdout.strip() or f"ssh exited {proc.returncode}")
    return proc.stdout, None


def run_squeue(users: list[str]) -> tuple[list[list[str]] | None, str | None]:
    out, err = _ssh(f"squeue -u {','.join(users)} -o '{SQUEUE_FORMAT}' --noheader")
    if err is not None:
        return None, err
    rows = [line.split("|") for line in out.splitlines() if line.strip()]
    return rows, None


def run_sacct(jobids: list[str]) -> dict[str, dict]:
    """Look up final state of jobs via sacct. Returns {jobid: {...}}."""
    if not jobids:
        return {}
    fmt = "JobID,JobName,User,State,Elapsed,End"
    out, err = _ssh(f"sacct -j {','.join(jobids)} --format={fmt} --noheader -X -P")
    if err is not None or not out:
        return {}
    result: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        jid, name, user, state, elapsed, end = parts[:6]
        jid = jid.strip()
        if not jid:
            continue
        result[jid] = {
            "name": name,
            "user": user,
            "state": state.split()[0] if state else "",  # 'CANCELLED by 123' -> 'CANCELLED'
            "raw_state": state,
            "elapsed": elapsed,
            "end": end,
        }
    return result


# --- Watched-job processing (runs in the main loop) -------------------------

def process_watches(state: State, cfg: dict | None) -> None:
    """Email whenever a watched job's state changes (e.g. PENDING -> RUNNING),
    and when it leaves the queue (final state via sacct)."""
    with state.lock:
        watched = dict(state.watch)  # jobid -> last-known state
    if not watched:
        return

    # Current states for any watched job still in the queue (all accounts, so it
    # works regardless of which account is being viewed).
    rows, err = run_squeue(_all_users)
    if err is not None:
        return  # connection problem; try again next tick, watches preserved
    current = {r[0]: r for r in rows if r}

    for jid, last in watched.items():
        if jid in current:
            new_state = (current[jid] + [""] * 9)[3]
            if new_state and new_state != last:
                name = (current[jid] + [""] * 9)[2]
                with state.lock:
                    if jid in state.watch:
                        state.watch[jid] = new_state
                # don't email a first-seen baseline (last == "")
                if last:
                    _notify_change(state, cfg, jid, name, last, new_state, final=None)
        else:
            # Left the queue -> ask sacct for the final state.
            rec = run_sacct([jid]).get(jid)
            if not rec or rec["state"] not in TERMINAL_STATES:
                continue  # sacct doesn't know yet; keep watching
            with state.lock:
                state.watch.pop(jid, None)
            _notify_change(state, cfg, jid, rec["name"], last, rec["state"], final=rec)


def _notify_change(state: State, cfg: dict | None, jid: str, name: str,
                   old: str, new: str, final: dict | None) -> None:
    transition = f"{old or '?'} -> {new}"
    tag = "finished" if final else "changed"
    if cfg is None:
        with state.lock:
            state.message = f"job {jid} {transition} -- email not configured"
        return

    subject = f"[HPC] Job {jid} {name}: {transition}"
    lines = [
        f"Watched HPC job {tag}.",
        "",
        f"  Job ID:   {jid}",
        f"  Name:     {name}",
        f"  Change:   {transition}",
    ]
    if final:
        lines += [
            f"  State:    {final['raw_state']}",
            f"  Elapsed:  {final['elapsed']}",
            f"  Ended:    {final['end']}",
        ]
    lines += [f"  Cluster:  {SSH_HOST}", ""]
    body = "\n".join(lines)

    def _send() -> None:
        err = send_email(cfg, subject, body)
        with state.lock:
            if err:
                state.message = f"job {jid} {transition} but email failed: {err}"
            else:
                state.message = f"job {jid}: {transition} -- emailed {cfg['recipient']}"

    threading.Thread(target=_send, daemon=True).start()


# --- Rendering --------------------------------------------------------------

def build_view(state: State, rows, error, last_update, email_status) -> Group:
    label, users = ACCOUNTS[state.account]
    show_user = len(users) > 1
    with state.lock:
        watch = set(state.watch)
        input_mode = state.input_mode
        input_buffer = state.input_buffer
        message = state.message
        selected = state.selected

    n = len(rows) if rows else 0
    if n:
        selected = max(0, min(selected, n - 1))

    header = Text()
    header.append("HPC jobs", style="bold")
    header.append(f"  ·  {SSH_HOST}", style="dim")
    header.append("  ·  ", style="dim")
    header.append(label, style="bold cyan")
    header.append(f"  ·  every {state.interval}s", style="dim")
    header.append(f"  ·  {last_update:%H:%M:%S}", style="dim")
    header.append(f"  ·  {email_status}", style="dim")

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
        for idx, r in enumerate(rows):
            r = (r + [""] * len(COLUMNS))[: len(COLUMNS)]
            jobid, user, name, st, t, tl, nodes, part, reason = r
            counter[st] += 1
            is_selected = idx == selected
            watched = jobid in watch

            if is_selected or watched:
                # Full-row band (both colors explicit, so it stays readable on light
                # AND dark terminals). Cursor = Claude light green; a watched job is
                # marked by a Claude light-red band (no separate marker needed).
                bar = "bold black on #f38ba8" if watched else "bold black on #a6e3a1"
                cells = [jobid]
                if show_user:
                    cells.append(user)
                cells += [name, st, t, tl, nodes, part, reason]
                table.add_row(*cells, style=bar)
            else:
                cells = [jobid]
                if show_user:
                    cells.append(user)
                cells += [name, Text(st, style=STATE_STYLES.get(st, "white")),
                          t, tl, nodes, part, reason]
                table.add_row(*cells)

        body = table if rows else Panel(Text("No jobs in the queue.", style="dim"),
                                        border_style="green")

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

    # Status / watch line.
    status = Text()
    if input_mode == "email":
        status.append("New notification email: ", style="bold yellow")
        status.append(input_buffer + "▌", style="bold")
        status.append("   (Enter = save, Esc = cancel)", style="dim")
    else:
        if watch:
            status.append(f"watching {len(watch)} (red rows) — email on status change: ",
                          style="#f38ba8")
            status.append(" ".join(sorted(watch)), style="#f38ba8")
        else:
            status.append("no job watched — highlight one and press w to email on changes",
                          style="dim")
        if message:
            status.append("    ")
            status.append(message, style="cyan")

    footer1 = Text()
    footer1.append("  j / k move    ", style="bold")
    footer1.append("w / space", style="bold magenta")
    footer1.append(" email me on the selected job's status changes    ", style="dim")
    footer1.append("c", style="bold")
    footer1.append(" clear", style="dim")

    footer2 = Text()
    acct_hint = "  " + "  ".join(f"{k} {ACCOUNTS[k][0]}" for k in ACCOUNTS) + "     "
    footer2.append(acct_hint, style="dim")
    footer2.append("e", style="bold yellow")
    footer2.append(" change email     ", style="dim")
    footer2.append("r", style="bold")
    footer2.append(" refresh   ", style="dim")
    footer2.append("+/-", style="bold")
    footer2.append(" speed   ", style="dim")
    footer2.append("q", style="bold")
    footer2.append(" quit", style="dim")

    return Group(Panel(header, border_style="cyan"), body, counts_line, status, footer1, footer2)


# --- Keyboard thread --------------------------------------------------------

def read_key() -> str:
    """Read one logical keypress, decoding arrow-key escape sequences.

    Arrow keys arrive as a 3-byte sequence: ESC then '[' (or 'O' in application
    cursor mode) then a letter. We wait briefly for the rest of the sequence so a
    real arrow key isn't mistaken for a bare Esc.
    """
    ch = sys.stdin.read(1)
    if ch == "\x1b":  # could be Esc alone or the start of an arrow sequence
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            return "ESC"
        nxt = sys.stdin.read(1)
        if nxt in ("[", "O"):
            code = sys.stdin.read(1)
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(code, "")
        return "ESC"
    return ch


def keyboard_loop(state: State) -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = read_key()
            if not key:
                continue
            with state.lock:
                # --- typing a new notification email ---
                if state.input_mode == "email":
                    if key in ("\r", "\n"):
                        val = state.input_buffer.strip()
                        if val:
                            state.pending_recipient = val
                        state.input_buffer = ""
                        state.input_mode = None
                    elif key == "ESC":
                        state.input_buffer = ""
                        state.input_mode = None
                    elif key in ("\x7f", "\b"):
                        state.input_buffer = state.input_buffer[:-1]
                    elif len(key) == 1 and key.isprintable():
                        state.input_buffer += key
                    continue

                # --- normal navigation ---
                n = len(state.job_ids)
                if key in ("q", "Q"):
                    state.quit = True
                    break
                elif key in KEY_TO_ACCOUNT:
                    state.account = KEY_TO_ACCOUNT[key]
                    state.force_refresh = True
                elif key == "k":
                    if n:
                        state.selected = max(0, state.selected - 1)
                elif key == "j":
                    if n:
                        state.selected = min(n - 1, state.selected + 1)
                elif key in ("w", "W", " "):
                    if 0 <= state.selected < n:
                        jid = state.job_ids[state.selected]
                        if jid in state.watch:
                            state.watch.pop(jid, None)
                            state.message = f"stopped watching {jid}"
                        else:
                            # baseline = current state, so we only email on changes
                            state.watch[jid] = state.job_states.get(jid, "")
                            state.message = f"will email you on {jid}'s status changes"
                elif key in ("c", "C"):
                    state.watch.clear()
                    state.message = "cleared all watched jobs"
                elif key in ("e", "E"):
                    if state.email_configured:
                        state.input_mode = "email"
                        state.input_buffer = ""
                    else:
                        state.message = "email not set up yet — quit and run ./set-email"
                elif key == "r":
                    state.force_refresh = True
                elif key in ("+", "="):
                    state.interval = min(MAX_INTERVAL, state.interval + 5)
                elif key in ("-", "_"):
                    state.interval = max(MIN_INTERVAL, state.interval - 5)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- First-run onboarding ---------------------------------------------------

def handle_missing_cluster_config() -> None:
    """No usable cluster.json was found, so we're on the example placeholder and
    can't connect to anything. Explain it, show a copy-pasteable example, and
    offer to build the file interactively. On success we re-exec so the new
    config is picked up; otherwise we exit."""
    print("=" * 64)
    print(" No cluster configuration found.")
    print("=" * 64)
    print()
    print(f" HPC_mentor needs this file to know which cluster to connect to:")
    print(f"   {CLUSTER_PATH}")
    print()
    print(" It looks like this (host, your login name, and accounts to watch):")
    print()
    print('   {')
    print('     "ssh_host": "login.hpc.example.edu",')
    print('     "ssh_user": "your_login_name",')
    print('     "accounts": [')
    print('       {"key": "1", "label": "Me", "users": ["your_login_name"]}')
    print('     ]')
    print('   }')
    print()
    try:
        ans = input(" Create it now, step by step? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"

    if ans in ("", "y", "yes"):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_cluster.py")
        print()
        subprocess.run([sys.executable, script])
        # If a usable config now exists, restart so all the module-level settings
        # (host, user, accounts) are recomputed from it.
        if _load_cluster() is not DEFAULT_CLUSTER:
            print("\nStarting the monitor with your new configuration...\n")
            os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        print(f"\n Create {CLUSTER_PATH} (see the README), then run ./jobs again.")
        print(" Or run  ./set-cluster  to build it interactively anytime.")


def maybe_first_run_email_setup() -> None:
    """On the very first run (no config, not previously declined), offer to set
    up the job-finished email notifications by launching the set-email helper."""
    if os.path.exists(CONFIG_PATH) or os.path.exists(SKIP_MARKER):
        return

    print("=" * 60)
    print(" Welcome to HPC_mentor — live cluster job monitor")
    print("=" * 60)
    print()
    print(" Optional: get an EMAIL when a job you pick finishes.")
    print(" (You can always set this up later by running:  ./set-email)")
    print()
    try:
        ans = input(" Set up email notifications now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if ans in ("y", "yes"):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_email.py")
        print()
        subprocess.run([sys.executable, script])
        print()
    else:
        try:
            open(SKIP_MARKER, "w").close()  # remember the choice, don't nag
        except OSError:
            pass
        print(" Skipping email setup. Run  ./set-email  anytime to enable it.")
    print(" Starting the monitor...\n")


def _passwordless_works() -> bool:
    """True if we can already log in without being prompted (an installed key or
    a live master socket). Uses BatchMode so it fails fast instead of hanging."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_TARGET, "true"],
            capture_output=True, text=True, timeout=12,
        )
        return r.returncode == 0
    except Exception:
        return False


def maybe_first_run_ssh_setup() -> None:
    """On first run, if passwordless login isn't set up yet, offer to launch the
    set-ssh helper so the user never has to type their cluster password. Declining
    is remembered (a skip marker), and password login still works regardless."""
    if os.path.exists(SSH_SKIP_MARKER):
        return
    # Nothing to connect to yet if the host is still the placeholder.
    if not SSH_HOST or SSH_HOST == "login.your-hpc.example.edu":
        return
    if _passwordless_works():
        return  # already seamless — don't bother the user

    print("=" * 60)
    print(" Passwordless login isn't set up yet.")
    print("=" * 60)
    print()
    print(" You can log in with your password each time, OR set up an SSH key")
    print(" once so ./jobs connects instantly with no password.")
    print(" (You can always do this later by running:  ./set-ssh)")
    print()
    try:
        ans = input(" Set up passwordless SSH login now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if ans in ("y", "yes"):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_ssh.py")
        print()
        subprocess.run([sys.executable, script])
        print()
        # set_ssh.py may have written a login user into cluster.json; pick it up
        # so this same run connects as the right user (globals were computed at
        # import time, before that file existed/changed).
        global SSH_USER, SSH_TARGET
        _c = _load_cluster()
        SSH_USER = (_c.get("ssh_user") or "").strip()
        SSH_TARGET = f"{SSH_USER}@{SSH_HOST}" if SSH_USER else SSH_HOST
    else:
        try:
            open(SSH_SKIP_MARKER, "w").close()  # remember the choice, don't nag
        except OSError:
            pass
        print(" Skipping. You'll be asked for your password at startup.")
        print(" Run  ./set-ssh  anytime to enable passwordless login.\n")


# --- Main -------------------------------------------------------------------

def main() -> int:
    # Before anything else: if there's no real cluster config we're on the
    # placeholder host and can't connect. Guide the user to create one.
    if USING_PLACEHOLDER:
        if not sys.stdin.isatty():
            print(f"No cluster configuration found. Create {CLUSTER_PATH} "
                  "(see the README), or run ./set-cluster.")
            return 2
        handle_missing_cluster_config()
        return 1  # only reached if setup was declined/failed; success re-execs

    start_account = DEFAULT_ACCOUNT
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg == "all":
            arg = "a"
        if arg not in ACCOUNTS:  # also accept a label, e.g. "minghao"
            by_label = [k for k, (lbl, _) in ACCOUNTS.items() if lbl.lower() == arg]
            if by_label:
                arg = by_label[0]
        if arg not in ACCOUNTS:
            choices = ", ".join(f"{k}={ACCOUNTS[k][0]}" for k in ACCOUNTS)
            print(f"Unknown account '{arg}'. Choose from: {choices}")
            return 2
        start_account = arg

    if not sys.stdin.isatty():
        print("This tool needs an interactive terminal (stdin is not a TTY).")
        return 2

    maybe_first_run_email_setup()
    maybe_first_run_ssh_setup()
    cfg, email_status = load_email_config()

    # Authenticate ONCE, here, while we still own a normal terminal — so a
    # password prompt is visible and typeable. (Doing this later, inside the live
    # display, would draw the prompt onto the alternate screen and hang.)
    print("Connecting to the cluster (you may be prompted for your password once)...")
    conn_err = ensure_connection()
    if conn_err:
        print()
        print("Could not establish the SSH connection:")
        for line in conn_err.split("\n"):
            print("  " + line)
        print()
        print("Tip: set up a key once so you never need a password again:  ./set-ssh")
        return 1

    state = State(account=start_account, interval=15)
    state.email_configured = cfg is not None

    threading.Thread(target=keyboard_loop, args=(state,), daemon=True).start()

    rows: list[list[str]] | None = []
    error: str | None = None
    last_update = datetime.now()

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

            # Apply a pending email-recipient change requested from the keyboard.
            with state.lock:
                pending = state.pending_recipient
                state.pending_recipient = None
            if pending is not None:
                if cfg is None:
                    msg = "email not set up — quit and run ./set-email"
                elif "@" not in pending:
                    msg = f"'{pending}' is not a valid email address"
                else:
                    err = save_recipient(cfg, pending)
                    if err:
                        msg = f"couldn't save email: {err}"
                    else:
                        email_status = f"email on -> {cfg['recipient']}"
                        msg = f"notifications now go to {pending}"
                with state.lock:
                    state.message = msg

            now = time.monotonic()
            if do_poll or now >= next_poll:
                rows, error = run_squeue(users)
                last_update = datetime.now()
                next_poll = time.monotonic() + interval
                with state.lock:
                    state.job_ids = [r[0] for r in rows] if rows else []
                    state.job_states = {r[0]: (r + [""] * 9)[3] for r in rows} if rows else {}
                    if state.selected >= len(state.job_ids):
                        state.selected = max(0, len(state.job_ids) - 1)
                if error is None:
                    process_watches(state, cfg)

            live.update(build_view(state, rows, error, last_update, email_status))
            time.sleep(0.2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
