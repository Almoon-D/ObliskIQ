# Deploying ObliskIQ

How to run the bot unattended on a small always-on Linux machine, update it and roll it back,
back it up and restore it. Provider-agnostic: the same steps on Oracle's free ARM machine,
Google's free `e2-micro`, any VPS or a machine at home. Day-to-day operation (shadow, paper, the
digest, recovering from an alert) is `docs/RUNBOOK.md`; the design is MASTER_PLAN §5.20.

## 1. What runs on the machine

One image, `ghcr.io/almoon-d/obliskiq:<version>` (linux/amd64 and linux/arm64), published by
every release, run as two containers under systemd:

| Unit | Container | What it does |
|---|---|---|
| `obliskiq.service` | `obliskiq` | `obliskiq serve`: the bot. With transport `gateway` it also installs IBKR's pinned Client Portal Gateway, starts it and logs the paper username in, inside the same container (DECISIONS D175). |
| `obliskiq-cockpit.service` | `obliskiq-cockpit` | `obliskiq cockpit serve`: the MCP cockpit on `127.0.0.1:8765` and the Telegram bot. It opens no brokerage session. |
| `obliskiq-backup.timer` | (none) | `deploy/backup.sh` each night at 03:17 UTC. |

What lives on the machine (the image holds no state and no secret):

| Path | Owner, mode | What |
|---|---|---|
| `/etc/obliskiq/obliskiq.env` | root, 0600 | the secrets and switches (`deploy/obliskiq.env.example`) |
| `/etc/obliskiq/image.env` | root, 0644 | the image and the tag the units run (`deploy/update.sh` rewrites it) |
| `/etc/obliskiq/owner/owner.yaml` | 10001, 0600 | your settings (`config/owner.yaml`'s format) |
| `/var/lib/obliskiq/` | 10001, 0700 | the store, the settings audit, the heartbeat, the board and dashboard, the Gateway's install |
| `/var/backups/obliskiq/` | root, 0700 | the newest backup archives |
| `/opt/obliskiq-deploy/` | root | a checkout of this repository at the running release (the scripts and units) |

The containers run as uid 10001, never root.

## 2. Choose a machine

At least 1 GB of memory, 20 GB of disk (the image is about 2 GB), x86_64 or ARM64, a stable
outbound address, and Ubuntu 26.04 LTS (DECISIONS D96; 24.04 works the same with Docker).

- **Oracle Cloud Always Free, Ampere A1 (ARM64).** Up to 4 cores and 24 GB: the best free
  machine when Oracle has capacity, which it often does not. Reserve its public address: an
  ephemeral one changes on every stop and start.
- **Google Cloud Always Free, `e2-micro` (x86_64).** 2 shared vCPUs and 1 GB, in three US
  regions; free apart from its external address, which Google bills at about US$3.65 a month
  (US$0.005 an hour, since 2024-02-01; lite's measurement, 2026-10-04). It sustains roughly an
  eighth of a core for one thread, so the daily compute step runs several times slower than on a
  laptop. Add 2 GB of swap.
- **Any VPS** with 2 GB is comfortable. **A machine at home** works if it never sleeps; T1 ran on
  WSL2 Ubuntu.

What is measured and what is not. Lite measured its own processes on a 1 GB-class x86_64 sandbox:
the largest was its install at 127 MB, its compute step 68 MB, its MCP server 85 MB idle
(lite's `ROADMAP.md`, 2026-10-04). This image's cockpit idles at 141 MiB (2026-11-16,
x86_64). Not yet measured on a real 1 GB machine: the service with the Gateway's Java process and
the headless Chromium of a login running beside it (VERIFY-LIVE in `docs/ROADMAP.md`). Until it
is, prefer 2 GB, or give a 1 GB machine 2 GB of swap.

Prefer Oracle's ARM machine when it can be had, otherwise Google or a small VPS: a machine that
exists beats a better one that does not.

## 3. Prepare the machine

As a user with sudo, on the fresh machine:

```bash
sudo apt-get update && sudo apt-get install -y docker.io git rclone
```

```bash
sudo systemctl enable --now docker
```

Swap on a 1 GB machine:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

The deployment's files, at the release you deploy (replace the version):

```bash
sudo git clone --branch v1.0.0 https://github.com/Almoon-D/ObliskIQ.git /opt/obliskiq-deploy
```

The repository is private: give git a GitHub token that can read it when it asks.

```bash
sudo install -d -m 0755 /etc/obliskiq && sudo install -d -m 0700 -o 10001 -g 10001 /etc/obliskiq/owner /var/lib/obliskiq && sudo install -d -m 0700 /var/backups/obliskiq
```

## 4. Secrets

**The environment file.** Copy the template, fill it in, and keep it root's:

```bash
sudo install -m 0600 /opt/obliskiq-deploy/deploy/obliskiq.env.example /etc/obliskiq/obliskiq.env
```

```bash
sudoedit /etc/obliskiq/obliskiq.env
```

- **IBKR: the paper account only.** `OBLISK_IBKR_PROFILE=paper`, `OBLISK_IBKR_TRANSPORT=gateway`,
  the paper account id (it starts with `DU`) and the paper login. The bot refuses every broker
  write unless the account id has a paper prefix. The live account's OAuth keys never go on this
  machine until promotion (DECISIONS D42, MASTER_PLAN §5.19).
- **Telegram.** Create a bot with @BotFather (`/newbot`): its token is `TELEGRAM_BOT_TOKEN`. Send
  it any message, then read your chat id from `https://api.telegram.org/bot<token>/getUpdates`
  (`message.chat.id`): that is `TELEGRAM_CHAT_ID`. The bot serves that chat and no other.
- **The cockpit's token.** `OBLISK_MCP_AUTH_TOKEN`: generate one with
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **The dead man's switch.** `OBLISK_HEALTH_PING_URL`: a check at healthchecks.io (or any URL that
  alerts when it stops being fetched). The service fetches it after every heartbeat, so a dead
  machine is noticed from outside even when Telegram is down with it.
- **The backup's target.** `OBLISK_BACKUP_REMOTE`: an rclone remote and path (§8).
- **Data sources and the analyst**: `FRED_API_KEY`, `EIA_API_KEY`, `SEC_USER_AGENT`,
  `GEMINI_API_KEY`, as in the repository's `.env.example`.

**Pulling the image.** The package is private. Create a GitHub token (classic) with
`read:packages` only, store it as a machine secret, and log Docker in with it:

```bash
sudo install -m 0600 /dev/null /etc/obliskiq/ghcr-token && sudoedit /etc/obliskiq/ghcr-token
```

```bash
sudo sh -c 'docker login ghcr.io -u <your GitHub user> --password-stdin < /etc/obliskiq/ghcr-token'
```

**The image the units run:**

```bash
sudo install -m 0644 /opt/obliskiq-deploy/deploy/image.env.example /etc/obliskiq/image.env && sudoedit /etc/obliskiq/image.env
```

```bash
sudo docker pull ghcr.io/almoon-d/obliskiq:1.0.0
```

**Your settings.** Start from the repository's panel and edit it there; it is the source of truth
from now on (`obliskiq settings set` and the cockpit change it in place, audited):

```bash
sudo install -m 0600 -o 10001 -g 10001 /opt/obliskiq-deploy/config/owner.yaml /etc/obliskiq/owner/owner.yaml
```

Size the paper book like the real account: `capital.mode: cap` with `capital.cap_base` the amount
you will deploy live (MASTER_PLAN §5.15). A large paper balance then trades like the real account:
minimum trades, whole shares and commissions are simulated honestly.

**The command line on the machine:**

```bash
sudo install -m 0755 /opt/obliskiq-deploy/deploy/obliskiq-cli.sh /usr/local/bin/obliskiq
```

`sudo obliskiq <command>` runs any command in a one-off container of the running image, so the
commands in `docs/RUNBOOK.md` work as written (drop their `uv run`).

## 5. First boot, in shadow

The mode starts as `shadow` (`execution.modes.initial`): orders are simulated, never sent.

```bash
sudo obliskiq config validate
```

```bash
sudo obliskiq research data load-universe
```

The second fills the research store the service reads once a day (the selector's tracking
correlations, the swap partners'). Then the full preflight, once, before the bot runs: it installs
the Gateway, logs the paper username in, walks the read path, reads the core vehicle's venue,
previews (never places) a minimum order of it, and sends a Telegram test message:

```bash
sudo obliskiq preflight --full
```

Every step should pass. Then the units:

```bash
sudo cp /opt/obliskiq-deploy/deploy/systemd/obliskiq*.service /opt/obliskiq-deploy/deploy/systemd/obliskiq-backup.timer /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now obliskiq.service obliskiq-cockpit.service obliskiq-backup.timer
```

Within a few minutes:

```bash
sudo obliskiq status
```

shows `mode shadow`, a fresh heartbeat and session `open`; `sudo docker ps` shows `obliskiq` as
`healthy` (its health check is `obliskiq health`). From here, `docs/RUNBOOK.md` takes over: the
ten shadow days, then paper.

**One brokerage session per username.** Once the bot runs, it holds the paper login. A Claude
session, a browser or a second machine that logs in with it takes the session over, and each
breaks the other's work mid-order. So cloud sessions make no IBKR call while the bot runs. Before
a session that needs one, stop the service (a pause is not enough: it keeps the session):

```bash
sudo systemctl stop obliskiq.service
```

and start it again afterwards.

## 6. The cockpit

**From Claude.** The cockpit listens on the machine's loopback. Reach it through an SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 <you>@<machine>
```

then add an MCP server at `http://127.0.0.1:8765/mcp` with the header
`Authorization: Bearer <OBLISK_MCP_AUTH_TOKEN>`. The skill in
`.claude/skills/obliskiq-cockpit/SKILL.md` tells Claude how to read it. To reach it without a
tunnel, put a TLS reverse proxy on the machine (Caddy, for one) in front of `127.0.0.1:8765`; the
token is then the only thing between the internet and the cockpit, so keep it long and private.
`/healthz` answers without the token and says only that the process is alive.

**From Telegram.** Send `/status`, `/positions`, `/intents`, `/theses`, `/questions`,
`/settings`, or write it ("I added 1.000 € to your cash", "en 15 días necesito retirar 500 €").
Anything that records money, answers a question or changes a setting is echoed with a code and
done only on `/confirm <code>`. `/pause` and `/kill` act at once. Open questions arrive by
themselves, with their options and deadline.

**The dashboard.** After each daily digest the service writes `dashboard.html` and sends it on
Telegram; `http://127.0.0.1:8765/dashboard` (with the token) serves the latest one.

**Settings from afar.** Telegram `/set` and the cockpit's `update_setting` may only tighten a risk
limit or move a budget or cash-flow setting inside its bounds (`remote_settings` in
`config/defaults/server.yaml`). Loosening a risk limit, turning the kill switch off and every other
setting are done on the machine: `sudo obliskiq settings set <key> <value> --reason "…" --confirm`.
The service applies a change at its next cycle boundary by starting again on it.

## 7. Updating and rolling back

A release publishes its image a few minutes after the GitHub release appears. Bring the scripts
to the release first, then update:

```bash
sudo git -C /opt/obliskiq-deploy fetch --tags && sudo git -C /opt/obliskiq-deploy checkout v0.29.0
```

```bash
sudo /opt/obliskiq-deploy/deploy/update.sh 0.29.0
```

`update.sh` pulls the image (nothing changes if the pull fails), reads the kill switch, backs the
store up, stops both units, points `image.env` at the new tag and starts them (the store's
migrations run on start). It then waits up to `OBLISK_UPDATE_TIMEOUT` seconds (900) for health: a
heartbeat written after the update began, `obliskiq status` succeeding and the kill switch as it
was. If any of that fails, if a unit will not stop or start, or if the script is interrupted (a
signal, a closed terminal), it stops the units, restores the backup, starts the previous tag and
alerts you on Telegram, saying whether the previous version started and whether the backup was
restored. To go back by hand, run it with the previous version.

If a release changes a unit file, copy it again (§5) and `sudo systemctl daemon-reload` before
`update.sh`.

## 8. Backups

Each night `deploy/backup.sh` copies each database online with SQLite's backup (the service keeps
running), checks the copy with `PRAGMA integrity_check`, adds the state files and your settings,
compresses everything with zstd into `/var/backups/obliskiq/`, pushes the archive off the machine
with rclone to `OBLISK_BACKUP_REMOTE`, and keeps the newest 14 here. A failure alerts you.

Configure the remote once (`sudo rclone config`: any of S3, Backblaze B2, Google Drive, SFTP), set
`OBLISK_BACKUP_REMOTE=<remote>:<path>` in the environment file, then check it:

```bash
sudo systemctl start obliskiq-backup.service && sudo journalctl -u obliskiq-backup.service -n 20
```

Keep the remote's own retention (an rclone or bucket lifecycle rule) longer than the machine's.

## 9. Restore from a backup

Fetch the archive (or pick one in `/var/backups/obliskiq/`) and restore it; the script stops the
units, checks the archive's databases before replacing anything, and starts them again:

```bash
sudo rclone copy <remote>:<path>/obliskiq-20261116T031700Z.tar.zst /var/backups/obliskiq/
```

```bash
sudo /opt/obliskiq-deploy/deploy/restore.sh /var/backups/obliskiq/obliskiq-20261116T031700Z.tar.zst
```

On a new machine: §3 and §4 first, then the restore, then `sudo systemctl enable --now` the units.
After any restore, read `sudo obliskiq status` and run `sudo obliskiq reconcile run` with the
service stopped: the broker is the truth, and the store now remembers an earlier day.

## 10. Logs, rotation and health

Both containers log to the journal (`journalctl -u obliskiq.service`, `-u
obliskiq-cockpit.service`). Cap what it keeps, and rotate the Gateway's own log:

```bash
sudo install -D -m 0644 /opt/obliskiq-deploy/deploy/journald-obliskiq.conf /etc/systemd/journald.conf.d/obliskiq.conf && sudo systemctl restart systemd-journald
```

```bash
sudo install -m 0644 /opt/obliskiq-deploy/deploy/logrotate-obliskiq /etc/logrotate.d/obliskiq
```

Health has three layers: the heartbeat file (`sudo obliskiq health`, the container's health
check), the dead man's switch (`OBLISK_HEALTH_PING_URL`, which notices a machine that is gone),
and Telegram's alerts (a failed re-login reaches you within one heartbeat).

## 11. From the T1 setup to this one

If the bot ran under T1's systemd user unit with `uv run` (`docs/RUNBOOK.md` before this
deployment): stop it (`systemctl --user disable --now obliskiq`), copy its data directory into
`/var/lib/obliskiq/` (`sudo cp -a <OBLISK_DATA_DIR>/. /var/lib/obliskiq/ && sudo chown -R
10001:10001 /var/lib/obliskiq`), copy `config/owner.yaml` as in §4, and continue at §5.
