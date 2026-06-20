# HPC_mentor

A live SLURM job monitor for the terminal, so you don't have to run
`squeue -u <name>` over and over by hand.

It runs locally and SSHes into the cluster over a single multiplexed connection
(you authenticate once, then every refresh reuses the same socket). Job queries
use `squeue -u <user>`, so you can watch any account you have permission to see
and switch between them with a keypress. You can also "watch" a specific job and
get an email the moment it finishes.

## Run

```bash
cd /Users/chem/tool
./jobs            # start on Yuxiang's jobs
./jobs all        # start on the combined view (yuxiang/minghao/yue)
# or: ./jobs minghao   ./jobs yue
```

Or, after adding the alias (see below), just `hpcjobs` from anywhere.

A self-contained virtualenv lives in `/Users/chem/tool/.venv`; `./jobs` uses it
automatically. To recreate it:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### `hpcjobs` shortcut

Add this line to `~/.zshrc`, then open a new terminal (or `source ~/.zshrc`):

```bash
alias hpcjobs='/Users/chem/tool/jobs'
```

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `j` / `k`) | move the `›` cursor to highlight a job |
| `w` / `space` | toggle email-on-finish for the highlighted job (marks it `●`) |
| `c` | clear all email-watched jobs |
| `e` | change the notification email address (type it, Enter) |
| `1` | Yuxiang (yuxiangchen23) |
| `2` | Minghao (minghaodeng23) |
| `3` | Yue (yuesong21) |
| `a` | all accounts (adds a User column) |
| `r` | refresh now |
| `+` / `-` | change refresh interval (5–120s) |
| `q` | quit |

Default refresh is every 15s. Rows are colored by state (green = running,
yellow = pending, cyan = completing, red = failed). The highlighted row shows a
`›` cursor; jobs set to email you on finish show a magenta `●`. If the SSH
connection drops, an error panel is shown and the tool keeps retrying.

## Email-on-finish setup

When a watched job leaves the queue, the tool checks its final state with
`sacct` and emails you the result (COMPLETED / FAILED / CANCELLED / …).

Easiest way — run the setup helper and answer the prompts:

```bash
cd /Users/chem/tool && ./set-email
```

It asks for the sending address (it auto-detects the SMTP server for QQ, 163/126,
Gmail, Outlook, iCloud, Yahoo), where to send alerts, and the password /
authorization code (hidden). For **QQ Mail** use an *authorization code* (授权码)
from 设置 → 账号 → 开启 SMTP — not your login password.

This writes `~/.config/hpc_mentor/config.json` (perms `600`):

```json
{
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "smtp_user": "you@qq.com",
  "smtp_pass": "your-authorization-code",
  "recipient": "where-to-notify@example.com"
}
```

Port 465 uses SSL; 587 uses STARTTLS — both are handled automatically. You can
also leave `smtp_pass` empty and export `HPC_MENTOR_SMTP_PASS` instead.

The header shows `email on -> …` when configured, or `email off …` otherwise.
If email is off, watched jobs still show a finish message in the UI.

## Email bot — control the monitor by email (read-only)

Email a keyword to a mailbox and the bot replies with cluster status. Useful
when you're away from your terminal.

```bash
cd /Users/chem/tool
./set-bot     # one-time: bot mailbox, allowed senders, secret keyword
./hpc-bot     # run it (keep the window open; Mac awake + on VPN)
```

**Send a command:** email the bot mailbox with your **secret keyword anywhere**
in the subject or body, and a command in the **Subject** (or first body line):

| Command | Reply |
|---------|-------|
| `status` | your jobs (squeue) |
| `squeue all` | everyone's jobs |
| `squeue <username>` | that user's jobs |
| `sacct <jobid>` | a job's final state |
| `sinfo` | partition availability |
| `help` | the command list |

**Safety model (read-only by design):**
- A message is acted on **only if** the `From:` address is in your allowed list
  **and** the secret keyword is present. Anything else is ignored silently.
- The keyword is the real gate — `From:` headers can be spoofed.
- Commands are a fixed whitelist; arguments are strictly sanitized
  (`username` = letters/digits/underscore, `jobid` = digits). There is **no**
  path to arbitrary shell, job submission, or cancellation.
- It runs on your Mac, so it only answers while the Mac is awake and on VPN.

Config: `~/.config/hpc_mentor/bot.json` (perms `600`).

## Configure accounts / host

Edit the settings near the top of `hpc_monitor.py`:

- `SSH_HOST` — the SSH host/alias you connect through (an entry in `~/.ssh/config`).
- `ACCOUNTS` — the selectable views: a label and the usernames each queries with `squeue -u`.
- `KEY_TO_ACCOUNT` / `DEFAULT_ACCOUNT` — which key selects which account, and the startup view.

Mirrors the GitHub repo: https://github.com/Yuxiang-Chem/HPC_mentor
