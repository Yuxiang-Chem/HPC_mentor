#!/usr/bin/env python3
"""Interactively configure HPC_mentor's job-finished email notifications.

Run via the `set-email` wrapper. Asks for the email address to send from, picks
the right SMTP server automatically for common providers, asks where to send the
alerts, then asks for the password / authorization code (hidden input). Writes
everything to ~/.config/hpc_mentor/config.json with safe permissions.
"""
import getpass
import json
import os

CFG = os.path.expanduser("~/.config/hpc_mentor/config.json")

# domain -> (smtp_host, smtp_port). Port 465 = implicit TLS (SSL).
PROVIDERS = {
    "qq.com": ("smtp.qq.com", 465),
    "163.com": ("smtp.163.com", 465),
    "126.com": ("smtp.126.com", 465),
    "gmail.com": ("smtp.gmail.com", 587),
    "outlook.com": ("smtp.office365.com", 587),
    "hotmail.com": ("smtp.office365.com", 587),
    "live.com": ("smtp.office365.com", 587),
    "icloud.com": ("smtp.mail.me.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 465),
}

CRED_NAME = {
    "qq.com": "QQ authorization code (授权码)",
    "163.com": "NetEase authorization code (授权码)",
    "126.com": "NetEase authorization code (授权码)",
}

# Step-by-step guidance for getting the credential, shown per provider.
GUIDES = {
    "qq.com": [
        "Open QQ Mail in a browser  →  设置 (Settings)  →  账号 (Account).",
        "Find the POP3/IMAP/SMTP service and turn SMTP 服务 on (开启).",
        "It gives you a 授权码 (a string of letters). Copy it.",
    ],
    "gmail.com": [
        "Open this link in your browser: https://myaccount.google.com/apppasswords",
        "Sign in with this Gmail account if asked.",
        'In the "App name" box type anything, e.g. HPC monitor, and click Create.',
        "A yellow box pops up with a 16-character code like 'abcd efgh ijkl mnop'. Copy it.",
        "(If app passwords aren't offered, first enable 2-Step Verification, then retry.)",
    ],
    "163.com": [
        "Open 163 Mail in a browser  →  设置 (Settings)  →  POP3/SMTP/IMAP.",
        "Enable SMTP 服务 and finish the phone (短信) verification.",
        "Copy the 授权码 it shows you.",
    ],
    "126.com": [
        "Open 126 Mail in a browser  →  设置 (Settings)  →  POP3/SMTP/IMAP.",
        "Enable SMTP 服务 and finish the phone (短信) verification.",
        "Copy the 授权码 it shows you.",
    ],
    "outlook.com": [
        "Make sure 2-step verification is ON for your Microsoft account.",
        "Go to https://account.microsoft.com/security → Advanced security options → App passwords.",
        "Create a new app password and copy the code.",
    ],
}
GUIDES["hotmail.com"] = GUIDES["outlook.com"]
GUIDES["live.com"] = GUIDES["outlook.com"]


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def looks_like_email(s: str) -> bool:
    """Basic sanity check: exactly one @, a non-empty name, and a dotted domain."""
    s = s.strip()
    if s.count("@") != 1:
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        return False
    return "." in domain


def ask_email(prompt: str, default: str = "") -> str:
    """Prompt for an email address, re-asking until it looks valid."""
    while True:
        val = ask(prompt, default)
        if looks_like_email(val):
            return val
        print("  That doesn't look like an email address (e.g. name@qq.com) — try again.")


print("=== HPC_mentor email setup ===\n")
sender = ask_email("Email address to send FROM (e.g. 12345678@qq.com)")
domain = sender.split("@", 1)[1].lower()
if domain in PROVIDERS:
    host, port = PROVIDERS[domain]
    print(f"  -> detected server: {host} port {port}")
else:
    print(f"  -> I don't know the server for '{domain}'. Enter it manually:")
    host = ask("  SMTP host")
    port = int(ask("  SMTP port (465 for SSL, 587 for TLS)", "465") or "465")

recipient = ask_email("Send alerts TO (Enter = same as above)", sender)

cred_label = CRED_NAME.get(domain, "app password")

guide = GUIDES.get(domain)
if guide:
    print(f"\nHow to get your {cred_label}:")
    for i, line in enumerate(guide, 1):
        print(f"  {i}. {line}")
else:
    print(f"\nYou'll need an app password / authorization code for {domain}")
    print("  (created in that mail provider's security/SMTP settings — not your login password).")

print(f"\nPaste your {cred_label} below (input is hidden — you won't see it).")
pw = getpass.getpass(f"{cred_label}: ").replace(" ", "").strip()
if not pw:
    print("Nothing entered — nothing saved.")
    raise SystemExit(1)

data = {
    "smtp_host": host,
    "smtp_port": port,
    "smtp_user": sender,
    "smtp_pass": pw,
    "recipient": recipient,
}
os.makedirs(os.path.dirname(CFG), exist_ok=True)
with open(CFG, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.chmod(CFG, 0o600)

print("\nSaved.")
print(f"  From / login : {sender}  via {host}:{port}")
print(f"  Alerts to    : {recipient}")
print("The monitor's header will show:  email on ->", recipient)
