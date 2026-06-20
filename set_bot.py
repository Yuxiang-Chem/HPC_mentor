#!/usr/bin/env python3
"""Configure the HPC email bot (receives command emails, replies with status).

Run via the `set-bot` wrapper. Asks for the mailbox the bot logs into, the
allowed sender address(es), and a secret keyword that must appear in every
command email. Auto-detects IMAP/SMTP servers for common providers. Writes
~/.config/hpc_mentor/bot.json with safe permissions.
"""
import getpass
import json
import os

CFG = os.path.expanduser("~/.config/hpc_mentor/bot.json")

# domain -> (smtp_host, smtp_port, imap_host, imap_port). Port 465 = SSL SMTP.
PROVIDERS = {
    "qq.com": ("smtp.qq.com", 465, "imap.qq.com", 993),
    "163.com": ("smtp.163.com", 465, "imap.163.com", 993),
    "126.com": ("smtp.126.com", 465, "imap.126.com", 993),
    "gmail.com": ("smtp.gmail.com", 587, "imap.gmail.com", 993),
    "outlook.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "hotmail.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "live.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "icloud.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993),
}

CRED_NAME = {
    "qq.com": "QQ authorization code (授权码)",
    "163.com": "NetEase authorization code (授权码)",
    "126.com": "NetEase authorization code (授权码)",
}


def looks_like_email(s: str) -> bool:
    s = s.strip()
    if s.count("@") != 1:
        return False
    local, _, domain = s.partition("@")
    return bool(local) and bool(domain) and "." in domain and not domain.endswith(".")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def ask_email(prompt: str, default: str = "") -> str:
    while True:
        val = ask(prompt, default)
        if looks_like_email(val):
            return val
        print("  Not a valid email (e.g. name@qq.com) — try again.")


print("=== HPC email bot setup ===\n")
print("This is the mailbox the bot signs into. You email COMMANDS to it,")
print("and it replies with cluster status.\n")

address = ask_email("Bot mailbox address (the bot logs into this)")
domain = address.split("@", 1)[1].lower()
if domain in PROVIDERS:
    smtp_host, smtp_port, imap_host, imap_port = PROVIDERS[domain]
    print(f"  -> servers: IMAP {imap_host}:{imap_port}, SMTP {smtp_host}:{smtp_port}")
else:
    print(f"  -> unknown provider '{domain}'; enter servers manually:")
    imap_host = ask("  IMAP host")
    imap_port = int(ask("  IMAP port", "993") or "993")
    smtp_host = ask("  SMTP host")
    smtp_port = int(ask("  SMTP port (465 SSL / 587 TLS)", "465") or "465")

cred = CRED_NAME.get(domain, "app password / authorization code")
print(f"\nPaste the bot mailbox's {cred} (hidden input).")
if domain in ("qq.com", "163.com", "126.com"):
    print("  (the 授权码 from the mailbox's SMTP/IMAP settings — not the login password)")
password = getpass.getpass(f"{cred}: ").replace(" ", "").strip()
if not password:
    print("Nothing entered — nothing saved.")
    raise SystemExit(1)

print("\nWho is allowed to send commands? (their From address must match)")
senders = []
first = ask_email("Allowed sender #1")
senders.append(first.lower())
while True:
    more = ask("Another allowed sender? (Enter to finish)")
    if not more:
        break
    if looks_like_email(more):
        senders.append(more.lower())
    else:
        print("  Not a valid email — skipped.")

print("\nChoose a SECRET KEYWORD that must appear in every command email.")
print("(This is the real security gate — From addresses can be faked.)")
keyword = ask("Secret keyword")
while len(keyword) < 4:
    keyword = ask("  Please use at least 4 characters. Secret keyword")

poll = ask("Check the mailbox every how many seconds?", "45")
try:
    poll = max(15, int(poll))
except ValueError:
    poll = 45

data = {
    "address": address,
    "password": password,
    "imap_host": imap_host,
    "imap_port": int(imap_port),
    "smtp_host": smtp_host,
    "smtp_port": int(smtp_port),
    "allowed_senders": senders,
    "keyword": keyword,
    "poll_seconds": poll,
}
os.makedirs(os.path.dirname(CFG), exist_ok=True)
with open(CFG, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.chmod(CFG, 0o600)

print("\nSaved.")
print(f"  Bot mailbox : {address}  (IMAP {imap_host}, SMTP {smtp_host}:{smtp_port})")
print(f"  Allowed     : {', '.join(senders)}")
print(f"  Keyword     : (hidden, {len(keyword)} chars)")
print(f"  Poll every  : {poll}s")
print("\nStart it with:  ./hpc-bot")
