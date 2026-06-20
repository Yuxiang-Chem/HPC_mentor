# HPC_mentor

A live SLURM job monitor for the terminal, so you don't have to run
`squeue -u <name>` over and over by hand.

It runs locally on your machine and SSHes into the cluster over a single
multiplexed connection (you authenticate once, then every refresh reuses the
same socket). Job queries use `squeue -u <user>`, so you can watch any account
you have permission to see from your own login, and switch between them with a
keypress.

## Install

```bash
git clone https://github.com/Yuxiang-Chem/HPC_mentor.git
cd HPC_mentor
python3 -m venv .venv          # optional but recommended
.venv/bin/pip install -r requirements.txt
```

(Only dependency is [`rich`](https://github.com/Textualize/rich).)

## Configure

Edit the settings near the top of `hpc_monitor.py`:

- `SSH_HOST` — the SSH host/alias you connect through (an entry in `~/.ssh/config`).
- `ACCOUNTS` — the selectable views: a label and the list of usernames each
  one queries with `squeue -u`.
- `KEY_TO_ACCOUNT` — which number/letter key selects which account.

## Run

```bash
./jobs            # start on the first ("me") account
./jobs all        # start on the combined view
# or directly:
python3 hpc_monitor.py [me|minghao|yue|all]
```

## Keys

| Key | Action |
|-----|--------|
| `1` | account 1 (me) |
| `2` | account 2 |
| `3` | account 3 |
| `a` | all accounts (adds a User column) |
| `r` | refresh now |
| `+` / `-` | change refresh interval (5–120s) |
| `q` | quit |

Default refresh is every 15s. Rows are colored by state (green = running,
yellow = pending, cyan = completing, red = failed). If the SSH connection drops,
an error panel is shown and the tool keeps retrying on the next tick.

## Notes

- Connection multiplexing options are set on the `ssh` command itself, so you
  don't need to edit `~/.ssh/config` — but adding a key + `ControlMaster` there
  makes the first connection seamless.
- The "all accounts" view relies on `squeue` accepting a comma-separated `-u`
  list (standard on modern SLURM).
