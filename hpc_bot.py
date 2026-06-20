#!/usr/bin/env python3
"""Email-controlled HPC status bot (read-only).

Polls a mailbox over IMAP for command emails, and replies with cluster status.
A command email is only acted on when BOTH are true:
  1. the From address is in the allowed-senders list, AND
  2. the secret keyword appears in the subject or body.
Commands are a fixed whitelist (squeue / sacct / sinfo) with strictly
sanitized arguments — there is no way to run arbitrary shell on the cluster.

Setup:  ./set-bot      Run:  ./hpc-bot
Config:  ~/.config/hpc_mentor/bot.json

Send an email to the bot mailbox with the keyword anywhere in it and one of
these in the Subject (or first body line):
  status                 your jobs (squeue)
  squeue all             everyone's jobs
  squeue <username>      that user's jobs
  sacct <jobid>          a job's final state
  sinfo                  partition availability
  help                   this list
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import smtplib
import sys
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr

# Reuse the monitor's SSH plumbing, host, and account map.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hpc_monitor as hm  # noqa: E402

CFG_PATH = os.path.expanduser("~/.config/hpc_mentor/bot.json")
LOG_PATH = os.path.expanduser("~/.config/hpc_mentor/bot.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    if not os.path.exists(CFG_PATH):
        sys.exit("No bot config found. Run  ./set-bot  first.")
    with open(CFG_PATH) as fh:
        return json.load(fh)


# --- The one report: all jobs, grouped by user ------------------------------

def build_status_report(cfg: dict) -> str:
    """Read-only: squeue for all group accounts, grouped into per-user sections."""
    users = list(hm.ACCOUNTS["a"][1])  # the auto-built "all accounts" view
    rows, err = hm.run_squeue(users)
    if err is not None:
        return f"Could not reach the cluster: {err}"

    by_user: dict[str, list] = {u: [] for u in users}
    for r in rows or []:
        r = (r + [""] * 9)[:9]
        by_user.setdefault(r[1], []).append(r)

    total = sum(len(v) for v in by_user.values())
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"HPC jobs — {total} total across {len(by_user)} users   ({when})", ""]
    for u in by_user:
        jobs = by_user[u]
        parts.append(f"===== {u} — {len(jobs)} job(s) =====")
        if not jobs:
            parts.append("  (no jobs)")
        else:
            parts.append(f"  {'JOBID':>9}  {'STATE':<11}{'TIME':<10} NAME")
            for r in jobs:
                parts.append(f"  {r[0]:>9}  {r[3]:<11}{r[4]:<10} {r[2]}")
        parts.append("")
    return "\n".join(parts)


# --- Email plumbing ----------------------------------------------------------

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return msg.get_payload() or ""


def send_reply(cfg: dict, to_addr: str, subject: str, body: str) -> str | None:
    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if int(cfg["smtp_port"]) == 465:
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
                s.login(cfg["address"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
                s.starttls()
                s.login(cfg["address"], cfg["password"])
                s.send_message(msg)
    except Exception as exc:
        return str(exc)
    return None


def _imap_connect(cfg: dict) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg.get("imap_port", 993)))
    # QQ/NetEase often want the client to announce itself via the ID command.
    if any(d in cfg["imap_host"] for d in ("qq.com", "163.com", "126.com")):
        try:
            conn._simple_command(
                "ID", '("name" "hpc_mentor" "version" "1.0" "vendor" "hpc_mentor")')
            conn._untagged_response("OK", [], "ID")
        except Exception:
            pass
    conn.login(cfg["address"], cfg["password"])
    conn.select("INBOX")
    return conn


def process_inbox(conn: imaplib.IMAP4_SSL, cfg: dict) -> int:
    """Handle all unseen messages. Returns how many were answered."""
    allowed = {a.lower() for a in cfg.get("allowed_senders", [])}
    keyword = cfg["keyword"].lower()
    answered = 0

    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK":
        log(f"  IMAP search failed: {typ}")
        return 0
    ids = data[0].split()
    if ids:
        log(f"  {len(ids)} new message(s) to check")
    for num in ids:
        typ, msgdata = conn.fetch(num, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(msgdata[0][1])
        sender = parseaddr(msg.get("From", ""))[1].lower()
        subject = _decode(msg.get("Subject", ""))
        body = _body_text(msg)
        combined = f"{subject}\n{body}".lower()

        # Mark as seen regardless, so we don't reprocess.
        conn.store(num, "+FLAGS", "\\Seen")

        # Security gate: allowed sender AND keyword present. Else ignore.
        ok_sender = sender in allowed
        ok_keyword = keyword in combined
        if not (ok_sender and ok_keyword):
            log(f"  ignored msg from {sender or '?'} subj={subject!r} "
                f"(allowed_sender={ok_sender}, keyword_found={ok_keyword})")
            continue

        log(f"  authorized request from {sender} (subj={subject!r}) -> sending report")
        try:
            result = build_status_report(cfg)
        except Exception as exc:
            result = f"Error building report: {exc}"

        reply_subj = f"Re: {subject}" if subject.strip() else "HPC jobs"
        err = send_reply(cfg, sender, reply_subj,
                         f"{result}\n--\nHPC_mentor bot · {hm.SSH_HOST}\n")
        if err:
            log(f"  REPLY FAILED to {sender}: {err}")
        else:
            log(f"  replied to {sender}")
            answered += 1
    return answered


def main() -> int:
    cfg = load_config()
    interval = int(cfg.get("poll_seconds", 45))
    log(f"=== HPC email bot started. Mailbox {cfg['address']} (IMAP {cfg['imap_host']}) ===")
    log(f"Allowed senders: {', '.join(cfg.get('allowed_senders', []))} | "
        f"keyword length {len(cfg.get('keyword',''))}")
    log(f"Send an email here with the keyword in it to get all jobs back. "
        f"Polling every {interval}s. Keep window open (Mac awake + VPN). Ctrl-C to stop.")

    first = True
    while True:
        try:
            conn = _imap_connect(cfg)
            if first:
                log("IMAP login OK — watching the inbox.")
                first = False
            try:
                process_inbox(conn, cfg)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except KeyboardInterrupt:
            log("Stopped.")
            return 0
        except Exception as exc:
            log(f"ERROR: {type(exc).__name__}: {exc} (will retry in {interval}s)")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("Stopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
