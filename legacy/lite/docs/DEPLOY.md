# Running this on a free cloud VM, step by step

A complete walkthrough for someone who has never used a server, a terminal or
IBKR's API settings. Nothing is assumed. Every command can be copied and pasted,
and each one says what it does before you run it.

It works on **Oracle Cloud or Google Cloud**. This file is everything the two have
in common; the one part that differs — making the machine and connecting to it —
is §4 and the page it sends you to:
[`docs/ORACLE-CLOUD.md`](ORACLE-CLOUD.md) or [`docs/GOOGLE-CLOUD.md`](GOOGLE-CLOUD.md).

Set aside **about two hours**. Most of it is waiting.

Two names recur in the commands below and are yours to fill in: **`YOUR_IP_ADDRESS`**,
the reserved public address of the machine (§4), and **`YOUR_USER`**, the login
name your provider gave you on it (`ubuntu` on Oracle's Ubuntu image; on Google it
is whatever `whoami` prints once you are connected).

---

## 0. What you are building, and why it has to be a server

This project takes **one photograph of the options market every day** and keeps
it forever.

That sounds unremarkable until you know the constraint underneath it: **IBKR does
not let you ask what an option was worth last month.** Share prices you can
download going back decades whenever you like. Option prices and implied
volatility, you cannot. There is no endpoint. There is no archive. If nobody took
today's photograph today, that day is gone permanently and no amount of money or
effort recovers it.

So the software has to run **every single day, whether or not you are awake,
whether or not your laptop is open, whether or not you remember**. A laptop
sleeps, goes on holiday and runs out of battery. That is why this needs a small
computer somewhere that never sleeps.

Two clouds give one away permanently — Oracle's Always Free tier, and Google's
Always Free `e2-micro` — and that is the only reason either appears in this
document. There is no other allegiance here. Neither is entirely free in
practice: §4 says what each one actually costs.

**What you end up with:** a small computer in a data centre that wakes up once a
day, downloads the day's option and futures data, files it in a database, and
goes back to sleep. It sends you nothing and asks you nothing. You will mostly
forget it exists, which is the intended outcome.

---

## 1. Read this before you spend two hours

**The one prerequisite is IBKR OAuth access, and without it the IBKR half of this
does not work at all.**

The collectors talk to IBKR through something called the **Client Portal Web
API**, and it will not talk back until you have registered for OAuth 1.0a and
been approved. This is a form on IBKR's website. It is free. It is not a
download. But **IBKR review it by hand and that takes days**, so it cannot be
finished in one sitting.

Here is exactly what works before and after approval:

| | Before OAuth | After OAuth |
|---|---|---|
| Macro data (interest rates, inflation, oil inventories, positioning) | **Works** | Works |
| The whole analysis layer — grid, rankings, currency, commodities | **Works** | Works |
| **US option chains** (12 of the 15 collected names) | **Works** — marketdata.app, a free key, no IBKR at all | Works |
| **European option chains** (ASML, SAN, TTE) — no other route reaches them | **No** | Works |
| **Futures curve snapshots** — also irreplaceable | **No** | Works |
| Share price history | **No** | Works |

You can absolutely set the machine up first and let it collect macro data while
you wait for approval — nothing is wasted and step 3 is the long pole, so start
it today.

But be clear-eyed: **until OAuth is approved the photograph is partial.** Macro,
the analysis layer and the US chains all run without it; what waits on IBKR is
share price history, the futures curves and the three European chains no other
route reaches. Treat step 3 as the real task — it is the long pole and the only
one you cannot hurry.

> **A thing not to do.** If you go searching, you will find something called the
> **Client Portal Gateway** (and another called IB Gateway). They look easier —
> you download a program and it logs in. **Do not use them here.** Both need a
> human to type a password into a browser window every day, which defeats the
> whole purpose of a machine that runs unattended. They are deliberately out of
> scope for this project. OAuth is the harder-looking path that actually works.

---

## 2. Words you will meet

Skip this and come back when something is confusing.

| Word | What it actually means |
|---|---|
| **VM** / **instance** | A computer in a data centre. Yours, rented (here, free). Same as a real computer, just elsewhere |
| **SSH** | How you type commands into that computer from yours. A secure remote keyboard |
| **Terminal** / **shell** | The black window where you type commands. "Terminal" on Mac, "PowerShell" on Windows |
| **`sudo`** | "Do this as the administrator." Needed to install things |
| **Package** | A piece of software you install |
| **Python** | The language this project is written in |
| **venv** (virtual environment) | A private box for this project's Python parts, so it cannot break anything else |
| **systemd** | The thing on Linux that runs programs on a schedule and restarts them if they die |
| **timer** / **unit** | systemd's words for "a schedule" and "a job" |
| **repo** / **repository** | The folder of code — this project |
| **conid** | IBKR's internal number for a tradeable thing. SPY is 756733 |
| **OAuth** | A way to let a program use your IBKR account without knowing your password |
| **ARM** / **aarch64** | A type of processor. Oracle's biggest free machine uses it; Google's free machine does not (it is x86_64). Occasionally relevant |
| **Static / reserved IP** | A public address that stays the same when the machine is stopped and started. §4b says why you need one |

---

## 3. Registering for IBKR OAuth — start this first, it takes days

Approval is manual and slow, so begin here even though it is the hardest part.

### 3a. Make your keys

A "key" here is a small text file of scrambled characters. You make **two pairs**
plus one extra file. Each pair has a **private** half (secret, stays on your
machine, never shared with anyone including IBKR) and a **public** half (safe to
share, and the half you upload).

Open a terminal on **your own computer** and run these. If you are on Windows,
install [Git for Windows](https://git-scm.com/download/win) first and use the
"Git Bash" program it gives you — the commands then work identically.

```bash
# A folder to keep them in, readable only by you.
mkdir -p ~/obliskiq-keys && cd ~/obliskiq-keys && chmod 700 .

# Pair 1 — the SIGNING key. Proves requests really came from you.
openssl genrsa -out signature.pem 2048
openssl rsa -in signature.pem -pubout -out signature.pub

# Pair 2 — the ENCRYPTION key. IBKR encrypts a secret to you with this.
openssl genrsa -out encryption.pem 2048
openssl rsa -in encryption.pem -pubout -out encryption.pub

# The Diffie-Hellman parameters. A shared mathematical starting point.
# This one is slow — one to three minutes, and it will look frozen. It is fine.
openssl dhparam -out dhparam.pem 2048

# Lock the private halves down.
chmod 600 signature.pem encryption.pem dhparam.pem
ls -l
```

You now have six files. The three ending **`.pem`** are secret. The two ending
**`.pub`** get uploaded. `dhparam.pem` is not really secret but keep it with the
others.

> **Never send anyone a `.pem` file.** Not IBKR, not a support agent, not a forum.
> Only the `.pub` files are for sharing. Anyone holding `signature.pem` can act as
> you against your IBKR account.

### 3b. Fill in IBKR's form

1. Log in at **ibkr.com** as you normally would.
2. Go to **Settings** → **Account Settings**.
3. Find the section on API or Third-Party access and look for **OAuth** —
   specifically **"Self-Service OAuth"** or **"Third Party Integration"**. IBKR
   move this around, so if you cannot find it, use the site search for "OAuth"
   or ask IBKR chat support for "self-service OAuth 1.0a registration".
4. Ask for a **consumer key**. You may be asked to name your application —
   anything sensible, e.g. `obliskiq-lite`.
5. **Upload `signature.pub` and `encryption.pub`.** Make sure each goes in the
   right slot — signing key to the signing slot, encryption key to the encryption
   slot. Swapping them produces failures later that are genuinely hard to diagnose.
6. Submit and wait. **This is reviewed by a person and typically takes several
   days.**

### 3c. What comes back

Eventually IBKR give you:

- a **consumer key** — a short piece of text
- an **access token** — another piece of text
- an **access token secret** — a longer scrambled string

Keep all three. They go into a settings file in step 7, like this:

| IBKR gives you | Goes into |
|---|---|
| Consumer key | `OBLISK_IBKR_CONSUMER_KEY` |
| Access token | `OBLISK_IBKR_ACCESS_TOKEN` |
| Access token secret | `OBLISK_IBKR_ACCESS_TOKEN_SECRET` |
| your `signature.pem` file | `OBLISK_IBKR_SIGNATURE_KEY` (the **path** to the file) |
| your `encryption.pem` file | `OBLISK_IBKR_ENCRYPTION_KEY` (the path) |
| your `dhparam.pem` file | `OBLISK_IBKR_DH_PARAM` (the path) |

Note the last three are **file paths**, not the contents. You copy the files onto
the server and write down where they live.

**While you wait, carry on with step 4.** The machine can be built and can start
collecting macro data immediately.

### 3d. The other route, which nobody here has got working — **UNTESTED**

Everything above is OAuth **1.0a**, which is what the collectors use. IBKR also
run an OAuth **2.0** server for the Custom Connector — the same market-data tools
behind a remote endpoint — and it advertises a grant that needs no browser. If
that could be made to work it would replace the form above entirely.

**Nobody has made it work, and nothing in this repository has tried.** Registering
a client creates a real OAuth application on your brokerage account, which is your
decision and not the software's. So this is written down as an experiment, not as
instructions to follow, and every step is unverified.

You can see for yourself what the server accepts, safely — this sends no
credentials, registers nothing, and can be run as often as you like:

```
obliskiq-collect connector
```

The experiment, if you ever want to run it, is:

1. **Register a client** by POSTing to `https://api.ibkr.com/oauth2/register`
   (RFC 7591). Unknown whether IBKR accept an unauthenticated registration.
2. **Authorize once in a browser** at `https://api.ibkr.com/oauth2/authorize`,
   using PKCE (`S256`) and asking for scope `mcp.read` — read-only; `mcp.write`
   places orders and nothing unattended should ever hold it.
3. **Swap the code for a token** at `https://api.ibkr.com/oauth2/api/v1/token`,
   and see whether the `refresh_token` that comes back keeps working on the
   server with nobody present. **That is the whole question** — a token that
   needs a human every few hours is no better than the chat session it replaces.

The `client_credentials` grant would skip step 2 altogether, but IBKR's published
material describes OAuth 2.0 client authentication as `private_key_jwt` for
institutional clients, so whether a retail account may use it is unknown.

If it does work, `docs/DECISIONS.md` says what changes: a connector market-data
route, and the end of the human-present IV-percentile bridge. Until then this
section is a note, and step 3 above is the real path.

---

## 4. Making the machine

This is the one part of the walkthrough that depends on your provider. Choose one,
make the machine on its page, reserve its address, connect, and **come back here at
§6**.

### 4a. Which provider

| | Oracle Cloud | Google Cloud |
|---|---|---|
| **The free machine** | `VM.Standard.A1.Flex`, ARM, up to 4 OCPUs and 24 GB — or an AMD micro with 1 GB | `e2-micro`, x86_64, 2 shared vCPUs, 1 GB |
| **Getting one** | the ARM shape is very often **"Out of capacity"** | dependably available, in three US regions only |
| **Disk** | about 47 GB boot volume | 30 GB of *standard* disk; any other type is billed |
| **CPU** | ARM: real cores · AMD micro: 1/8 of one | about a quarter of one core, sustained |
| **The public address** | reserve it; no charge is stated in this repository's notes (UNVERIFIED) | billed while in use, about **$3.65 a month**, static or ephemeral |
| **Is it free?** | yes, as far as is known | the machine and disk, **not the address** |
| **Its own trap** | ports blocked in two places; capacity | the default disk type; a region outside the three; the address |
| **Page** | [`ORACLE-CLOUD.md`](ORACLE-CLOUD.md) | [`GOOGLE-CLOUD.md`](GOOGLE-CLOUD.md) |

**If Oracle will give you the ARM machine, take it** — 24 GB and real cores is the
comfortable way to run this. **If it says "Out of capacity", do not spend a week
retrying:** a day with no photograph is a day that is gone, and a machine that
exists beats a better one that does not. Take Google's, or Oracle's AMD micro.

**1 GB looks like enough memory, on measurements that stop short of the real thing.**
The install and the analysis were measured on a sandbox and sit far below it;
the collectors on a real 1 GB machine, beside an operating system whose share nobody
has measured, were not. `ROADMAP.md`, *What the second provider measured*, has the
numbers and what is missing, and `docs/DECISIONS.md` (*Two providers, one
walkthrough*) the decision. That gap is why §6c asks for swap. The limit on a 1 GB machine is its slice of a CPU, not its memory; §6c covers the
one step that is worth doing there anyway.

### 4b. The static address — required on both, and it fails quietly

Whatever you choose, **the machine's public address must be reserved before the
first collection run.** Both clouds attach an *ephemeral* public address by
default, and an ephemeral address **changes every time the instance is stopped and
started**.

marketdata.app — the route for twelve of the fifteen chain names — permits **one
egress address per account** and answers a changed one with `HTTP 403`. The
collector recognises that specific refusal, stops the whole `vendor-chain` step at
the first one rather than letting all twelve US names rediscover it, and writes a
cooldown into the store so the *next* run does not rediscover it either. The run
then reports **degraded**, not failed. So the symptom of an ephemeral address is a
nightly job that keeps exiting almost-successfully while collecting **no US option
chains at all**, and nothing in the log says "your address changed".

The click-by-click steps are on your provider's page, and each says when to do it.
Write the reserved address down: it is what you connect to, and it is the address
marketdata.app will see. `references/marketdata-playbook.md` records the observed
behaviour of the rule.

## 5. Connecting to it

Your provider's page ends with how to connect: Oracle with a downloaded key file and
`ssh`, Google from the browser or with `gcloud compute ssh`. When it works you will
land on a line ending `YOUR_USER@obliskiq:~$`.

**You are now typing on the server.** Everything from here happens there, not on your
own machine — except the two places that say so (§7a copies files up from your own
computer, and §10a copies a backup down to it), which take the provider's file-copy
command from the same page.

---

## 6. Installing

### 6a. System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git build-essential python3-dev sqlite3
```

`build-essential` and `python3-dev` are there because on ARM some Python maths
libraries occasionally have to be compiled rather than downloaded ready-made.
Installing them now avoids a confusing failure later. `sqlite3` is the command-line
tool that §10's backups and `docs/COLLECTORS.md`'s checks use; Python carries its
own copy of the database engine, but not this tool, and a minimal Ubuntu image does
not include it.

### 6b. Check your Python

```bash
python3 --version
```

Anything **3.11 or higher** is fine. Ubuntu 24.04 ships 3.12.

### 6c. Swap — worth doing on any machine with 2 GB of memory or less

Skip this on Oracle's 24 GB ARM machine. On a 1 GB machine — Google's `e2-micro`,
or Oracle's AMD micro — add it.

An earlier version of this step said that without swap the install "runs out of
memory and dies partway through". That did not survive being measured: on an x86_64
machine `pip install` stays far below 1 GB, because `numpy` and `scipy` arrive as
ready-made downloads rather than being compiled (`ROADMAP.md`, *What the second
provider measured*, has the numbers and where they were taken). So swap is
not here for the install. It is here as headroom: the operating system and its
agents take a share of a 1 GB machine that nobody has measured on a real one yet,
and the failure it guards against is the kernel killing a collector mid-run — which
costs a day that cannot be recovered, and shows up as an `interrupted` run (§11).

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h            # should now show 2.0Gi of swap
```

### 6d. Get the code

**This repository is private**, so a plain `git clone` will fail — GitHub does not
know who you are. Make a token first:

1. On GitHub: **Settings** → **Developer settings** → **Personal access tokens** →
   **Tokens (classic)** → **Generate new token (classic)**.
2. Tick the **`repo`** box. Set an expiry you are comfortable with.
3. Generate it and **copy it now** — GitHub never shows it again.

Then, on the server:

```bash
sudo mkdir -p /opt/obliskiq
sudo chown "$(id -un):$(id -gn)" /opt/obliskiq
git clone https://github.com/Almoon-D/ObliskIQ-lite-MCP.git /opt/obliskiq
```

When it asks for a **username**, type your GitHub username. When it asks for a
**password**, paste the **token** — not your GitHub password. The characters will
not appear as you paste. That is normal.

### 6e. Install the project

```bash
cd /opt/obliskiq
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

The last command takes a few minutes and prints a great deal. On Oracle's ARM
machine, and only there, it may pause for a long time on `numpy` or `scipy` while it
compiles them. Let it finish.

Check it worked:

```bash
.venv/bin/obliskiq-collect --help
```

A help message means you are done.

### 6f. Prove it runs, without IBKR

You can test the entire pipeline right now against the recorded sample data that
ships with the project. **It needs no IBKR account**, and with the two steps
below skipped it reaches no network at all:

```bash
cd /opt/obliskiq
export OBLISK_DB_PATH=/tmp/test.db

.venv/bin/obliskiq-collect universe --source fixture --fixtures tests/fixtures/ibkr
.venv/bin/obliskiq-collect daily --skip vendor-chain --skip macro --only SPY \
    --source fixture --fixtures tests/fixtures/ibkr
.venv/bin/obliskiq-compute all
```

**`--skip vendor-chain` is not optional here.** `--source fixture` replays
recorded IBKR responses and that step does not read IBKR: it builds a real
marketdata.app client whatever `--source` says, so with `MARKETDATA_API_TOKEN`
already in the environment this "offline" check would reach the network and
**spend credits on the live per-symbol tier** — the expensive one. SPY is one of
the names it collects, so `--only SPY` does not hold it back.
`docs/COLLECTORS.md` owns this rule under *Testing without IBKR*.

You will see a lot of JSON log lines and, at the end, a grid coverage report.
Read the result with `obliskiq-collect status` (§9 explains it) rather than off the
log. What a correct offline run looks like, **read off a run on 2026-10-04**:

- The `universe` step says **`partial`** and exits 1. The sample data deliberately
  covers only some instruments, so four symbols find no match. Expected offline.
- The `daily` sequence says **`partial`** and exits 1 too. `nav` and `backfill`
  report `ok`; `curve` is `skipped`; and **`chain` can be an `error`**, for one
  reason only: the recorded sample **ages**. It holds a single option expiry that was
  in the future when it was recorded, and once that date is too close it no longer
  falls inside the window the collector buys. `status` then says so in these words —
  *SPY lists no option month overlapping the … DTE window*. That exact message says
  nothing about your machine and re-running will not clear it. **Any other `chain`
  error is real**: read it. (A run on 2026-10-04 was already in this state.)
- `status` also reports the daily bars as **STALE**. Also a property of the sample:
  its recorded bars stop on 2026-01-07, months before the day you run it, and will
  not stop being stale.
- The grid reports **0% coverage**. You collected exactly one symbol. Also expected.

What *would* be a problem is an `ERROR` that names your own machine — a missing
`OBLISK_DB_PATH`, a Python import that fails, a file it cannot write — or a `chain`
error that is not the one message above. If you see the things above and nothing else,
the install is working. The live test of the machine is §9.

---

## 7. Settings and secrets

### 7a. Copy your keys up

**From your own computer**, in a new terminal window (not the one connected to
the server). On **Oracle**:

```bash
scp -i ~/Downloads/ssh-key-2026-08-23.key \
    ~/obliskiq-keys/signature.pem \
    ~/obliskiq-keys/encryption.pem \
    ~/obliskiq-keys/dhparam.pem \
    YOUR_USER@YOUR_IP_ADDRESS:/tmp/
```

On **Google**, the same copy goes through `gcloud`, which needs no key file:

```bash
gcloud compute scp \
    ~/obliskiq-keys/signature.pem \
    ~/obliskiq-keys/encryption.pem \
    ~/obliskiq-keys/dhparam.pem \
    obliskiq:/tmp/ --zone=YOUR_ZONE
```

Then **back on the server**:

```bash
sudo mkdir -p /etc/obliskiq/secrets
sudo mv /tmp/signature.pem /tmp/encryption.pem /tmp/dhparam.pem /etc/obliskiq/secrets/
sudo chmod 700 /etc/obliskiq/secrets
sudo chmod 600 /etc/obliskiq/secrets/*.pem
```

### 7b. The settings file

```bash
sudo mkdir -p /etc/obliskiq
sudo cp /opt/obliskiq/deploy/collect.env.example /etc/obliskiq/collect.env
sudo nano /etc/obliskiq/collect.env
```

`nano` is a simple text editor. Arrow keys move, typing types. **Ctrl+O** then
**Enter** saves, **Ctrl+X** exits.

Fill in the blanks after the `=` signs:

- `OBLISK_IBKR_CONSUMER_KEY` — from IBKR (step 3c). Leave empty until approved.
- `OBLISK_IBKR_ACCESS_TOKEN` — from IBKR.
- `OBLISK_IBKR_ACCESS_TOKEN_SECRET` — from IBKR.
- `FRED_API_KEY` — free and instant from
  <https://fredaccount.stlouisfed.org/apikey>
- `EIA_API_KEY` — free and instant from
  <https://www.eia.gov/opendata/register.php>
- `MARKETDATA_API_TOKEN` — free from <https://www.marketdata.app/>. **Do not skip
  this one.** It collects the US option chains and needs no IBKR approval. Left
  blank while `option_vendor.enabled` is true, the `vendor-chain` step now records
  one finding and reports the whole nightly sequence **degraded** — exit code 2,
  and `obliskiq-collect status` naming it — because twelve of the fifteen chain
  names are not being photographed at all. It used to record itself `skipped` at
  exit 0, which looked healthy and meant the same thing. One caveat:
  marketdata.app permits **one IP address per account** and blocks for five
  minutes when the address changes — which is why §4b makes the machine's address
  a reserved one — so run it from the server or your laptop, not both.
  `references/marketdata-playbook.md` has the measured behaviour.

The three `..._KEY=/etc/obliskiq/secrets/....pem` lines already point at the files
you copied in 7a. Leave them.

Lock the file down — it now holds secrets:

```bash
sudo chmod 600 /etc/obliskiq/collect.env
```

The analysis side gets its **own** settings file, and it deliberately holds no
key of any kind:

```bash
sudo cp /opt/obliskiq/deploy/compute.env.example /etc/obliskiq/compute.env
```

There is nothing to fill in. The analysis jobs read the database the collectors
filled and talk to nothing, so they are given no credentials — and their schedule
file puts them in a network namespace where they could not use one anyway. Two
locks on the same door, which is on purpose.

**Do not merge these into one file.** A single `/etc/obliskiq.env` would hand every
key on the machine to the unit that cannot open a socket, and turn two independent
guards into none. `tests/test_deploy_units.py` pins the three paths and that
`compute.env.example` names no credential.

If you will also run the **MCP server** on this machine — most people do not at
first — it gets a **third** file, which holds its auth token and no IBKR key:

```bash
sudo cp /opt/obliskiq/deploy/server.env.example /etc/obliskiq/server.env
sudo nano /etc/obliskiq/server.env       # set OBLISK_MCP_AUTH_TOKEN; the file says how
sudo chmod 600 /etc/obliskiq/server.env
```

`docs/SERVER.md` covers installing its unit and connecting a client. Nothing in the
collectors or the schedule below needs it.

> **Never commit secrets to git.** The repository ignores `.env`, and the file you
> just edited lives outside the repository entirely, which is why it is at
> `/etc/obliskiq/` and not in `/opt/obliskiq/`.
>
> One thing to know: the repository's `.env.template` once **had working FRED and
> EIA keys written into it**, and the marketdata.app token with them. They have
> been removed and the template now ships empty — but git remembers everything
> that was ever committed, so **those three keys are burned. Reissue them rather
> than trying to find them in the history.** FRED and EIA are free and instant.
> This is also why deleting a key from a file is never enough on its own.

### 7c. A user for the collectors to run as

Running scheduled jobs as a dedicated account with no login limits the damage if
anything goes wrong:

```bash
sudo useradd --system --home /var/lib/obliskiq --create-home obliskiq
sudo mkdir -p /var/lib/obliskiq
sudo chown -R obliskiq:obliskiq /var/lib/obliskiq
sudo chown -R obliskiq:obliskiq /etc/obliskiq
```

---

## 8. Putting it on a schedule

Install the ready-made schedule files:

```bash
sudo cp /opt/obliskiq/deploy/obliskiq-collect@.service /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-collect@daily.timer /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-collect@macro.timer /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-collect@universe.timer /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-compute@.service /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-compute@all.timer /etc/systemd/system/
sudo cp /opt/obliskiq/deploy/obliskiq-compute@census.timer /etc/systemd/system/

sudo systemctl daemon-reload

sudo systemctl enable --now obliskiq-collect@daily.timer
sudo systemctl enable --now obliskiq-collect@macro.timer
sudo systemctl enable --now obliskiq-collect@universe.timer
sudo systemctl enable --now obliskiq-compute@all.timer
sudo systemctl enable --now obliskiq-compute@census.timer
```

Check they are armed:

```bash
systemctl list-timers 'obliskiq*'
```

You should see five, each with a next-run time.

**What runs when** (all times UTC):

| Job | When | Doing what |
|---|---|---|
| `collect@daily` | Weekdays 20:15 | The photograph: prices, **option chains**, **futures curves**, macro |
| `collect@macro` | 06:00 and 14:00 daily | Keeps rates and inflation current |
| `collect@universe` | Saturdays 05:00 | Re-reads the instrument list and the account |
| `compute@all` | Weekdays 23:30 | Builds the grid from what was collected |
| `compute@census` | Saturdays 06:00 | Weekly check on how many options clear every filter |

`tests/test_deploy_units.py` reads this table and fails if a time or a day in it
disagrees with the timer file, so it cannot drift again the way it once did.

**The daily hour is measured, not chosen.** US option prices survive the close by hours
and then collapse, and the daily run itself takes the best part of an hour, so it
has to *finish* before the collapse rather than merely start before it.
`deploy/obliskiq-collect@daily.timer` carries the readings and `docs/DECISIONS.md`
the reasoning. Do not move it later so that "the book has settled": an earlier
version of this page said exactly that for a later hour, and that hour was on the
wrong side of the collapse. The collectors are bounded by the unit's own
`TimeoutStartSec`, and the analysis fires well after that.

Two design points worth understanding:

- **`Persistent=true`** means that if the machine was off when a run was due, it
  runs at the next boot instead of being skipped. Given that a missed day is
  permanent, this matters more here than in most projects.
- **The collectors and the analysis are separate jobs on purpose.** The analysis
  touches no network at all — it only reads the database the collectors filled.
  Running them separately means an analysis bug can never cost you a day of
  collection.

---

## 9. Checking it actually works

Do not wait for the evening run. Run one now:

```bash
sudo systemctl start obliskiq-collect@macro.service
sudo journalctl -u obliskiq-collect@macro.service -n 50 --no-pager
```

`journalctl` shows what a job printed. Macro is the right first test because it
needs **no IBKR access** — if this works, the machine, the schedule and your
settings file are all correct.

### Reading the logs

Log lines are JSON — one line per event. The words to look for:

**Good:**
```json
{"level": "INFO", "message": "target collected", "target": "DGS10", "rows": 1250}
{"level": "INFO", "message": "collector finished", "status": "ok"}
```

**Fine, but look at it:**
```json
{"level": "WARNING", "message": "collector run DEGRADED: ..."}
```
Everything was collected but on weaker inputs than intended — usually the price
history falling behind. Not urgent, not ignorable.

**Wrong:**
```json
{"level": "ERROR", "message": "collector could not run", "error": "..."}
```
The `error` text says what happened. Most such messages in this project tell you
the command to run next.

### The six statuses

| Status | Means |
|---|---|
| `ok` | Everything collected |
| `degraded` | Everything collected, but on a weaker input |
| `partial` | Some things collected, some failed |
| `skipped` | Nothing to do — a filter matched none of this step's targets |
| `error` | Nothing collected |
| `interrupted` | The machine killed the job before it could say anything |

### Exit codes

`0` fine · `1` something failed · `2` degraded. The schedule treats `2` as success
deliberately: re-running would spend the same API budget on the same weak inputs.

### The one command worth knowing

```bash
sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect status
```

**Why the `OBLISK_ENV_FILE=` part.** `sudo -u` does not read `EnvironmentFile=` —
that is a systemd directive, and systemd is not involved when you type a command
yourself. Without it the command starts with no settings at all and stops at
*"required environment variable OBLISK_DB_PATH is unset or empty"*. Naming the
file is what makes a hand-run command see the same settings the timer does.

This is the answer to "did last night happen?", and it is the only thing in this
document you need to remember. It reads the database and asks nothing of the
internet, so it works when everything else does not. It prints, for the last week:
which nights ran, how many of the planned steps reported, which step degraded or
failed **and why**, and any job the machine killed before it could finish.

The project sends you nothing on purpose. A program that emails or messages you
when something breaks needs an outbound connection, a password to make it, and
somewhere to keep that password — on the one computer whose entire purpose is to
be forgettable. So it writes down what happened instead and waits to be asked.

### Once OAuth is approved — the first run, in this order

Put the three IBKR values into `/etc/obliskiq/collect.env` (step 7b). Then run
these **in order, by hand, once**. The order is not a style preference: `daily`
walks the `universe` table, and on a machine where nothing has seeded it yet every
step of the sequence fails for want of anything to collect.

```bash
RUN="sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect"

$RUN preflight   # 1. the six OAuth steps, writes nothing, safe to repeat
$RUN universe    # 2. seed the universe — everything else walks it
$RUN backfill    # 3. the full history. SLOW: allow an hour or two, once only
$RUN daily       # 4. the sequence the timer will run from now on
```

**`preflight` first, always.** It walks the handshake one step at a time and tells
you *which* of the six failed. Without it a bad credential surfaces as a bare
`401` from whatever ran first, which says nothing about where the problem is.

**Do not skip step 3.** `backfill` fetches the full daily, weekly and long
commodity history. It is the slowest thing this project does and it is run once;
skip it and every strike band that night is sized from a fallback volatility and
the run reports `degraded` forever.

In step 4's output look for `"collector": "chain"` with a row count above zero.
**That is the first real photograph.** From then on it happens by itself.

### Checking in occasionally

Once a week is plenty:

```bash
sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect status   # what happened
systemctl list-timers 'obliskiq*'                                  # still scheduled?
sudo ls -lh /var/lib/obliskiq/obliskiq.db                          # is it growing?
```

The first command replaces grepping the logs, which was the previous advice here
and only ever found the failures that happened to use the word "error".

(`sudo` because `/var/lib/obliskiq` belongs to the service user and a login user
cannot necessarily look inside it.)

A database that is not growing is the one symptom worth chasing immediately.

---

## 10. Keeping it up to date

**Back up first — database migrations are forward-only and there is no undo.**

This repository is private and §6d typed the GitHub token in by hand, so `git pull`
asks for a username and a token again. Find that out **before** stopping anything: a
timer stopped and never restarted is a missed night, silently.

```bash
cd /opt/obliskiq && git fetch          # asks for the token now, and changes nothing
sudo systemctl stop 'obliskiq-collect@*.timer' 'obliskiq-compute@*.timer'
sudo -u obliskiq sqlite3 /var/lib/obliskiq/obliskiq.db \
     ".backup /var/lib/obliskiq/obliskiq-before-upgrade.db"

cd /opt/obliskiq
git pull
.venv/bin/pip install -e .

# The units were COPIED into /etc/systemd/system, so git pull does not touch
# them. A release that changes a schedule or a hardening setting has no effect
# until they are copied again.
sudo cp deploy/obliskiq-*.service deploy/obliskiq-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start 'obliskiq-collect@*.timer' 'obliskiq-compute@*.timer'
```

If any command after the `stop` fails, run the last line — the `start` — before you
investigate. The upgrade can wait; the timers cannot.

Database changes apply themselves the next time anything runs. Your settings file
is outside the repository, so `git pull` cannot touch it. Once the next scheduled
run has gone well, `sudo rm /var/lib/obliskiq/obliskiq-before-upgrade.db` — it is
uncompressed and nothing else removes it.

---

## 10a. Backing it up

**This is the step nothing else can replace.** A night of option chains that was
not collected cannot be bought, downloaded or recovered — and the whole store is
one SQLite file on one free-tier boot volume.

```bash
sudo -u obliskiq sh -c 'f=/tmp/obliskiq-$(date -u +%Y%m%d).db
    sqlite3 /var/lib/obliskiq/obliskiq.db ".backup $f" && gzip -f "$f"'
```

The file goes in `/tmp`, not beside the database, for two reasons. `/var/lib/obliskiq`
belongs to the service user, and on Ubuntu a directory created for a new account is
not readable by anyone else, so a login user's `scp` from there is refused. And a
backup is not meant to live on this machine at all, so nothing here is kept: you copy
it off and delete it.

Use `.backup`, not `cp`: the file is written under WAL, and a plain copy taken
mid-write is a copy of a half-written database. `.backup` is safe while the
collectors are running. The `gzip` is not decoration: a database compresses well,
and on Google every byte leaving the machine counts against a free allowance of
1 GB a month, which an uncompressed copy of a store that has been collecting for a
year would exceed on its own.

Then get it **off the machine** — a backup that lives on the disk it protects is
not one. From your own computer, not the server. On **Oracle**:

```bash
scp -i ~/Downloads/ssh-key-2026-08-23.key \
    "YOUR_USER@YOUR_IP_ADDRESS:/tmp/obliskiq-*.db.gz" ~/obliskiq-backups/
```

On **Google**:

```bash
gcloud compute scp "obliskiq:/tmp/obliskiq-*.db.gz" ~/obliskiq-backups/ --zone=YOUR_ZONE
```

Check the copy arrived, then delete the one on the server so the next copy does not
fetch it again:

```bash
sudo rm /tmp/obliskiq-*.db.gz          # on the server
```

To restore, `gunzip` the file, stop the timers, put it back at
`/var/lib/obliskiq/obliskiq.db`, `sudo chown obliskiq:obliskiq` it, and remove any
`obliskiq.db-wal` and `obliskiq.db-shm` beside it.

Once a month is enough to start with, plus one before every upgrade. Keep a few
on your own computer and delete the rest.

---

## 11. When things go wrong

### "Out of capacity" when creating the machine

That is Oracle's, and it is covered on its page (`docs/ORACLE-CLOUD.md`). Not your
fault; try a smaller shape, another time of day, the AMD fallback, or Google.

### The install dies partway with "killed" or no message

Out of memory. Confirm it, then add swap:

```bash
free -h                                  # how much memory and swap there is
sudo dmesg | grep -i 'killed process'    # the kernel says what it killed
```

Do step 6c and run the install again. On an x86_64 machine this is unexpected —
the install measured far below 1 GB (step 6c) — so if it happens there, something
else is using the memory, and `free -h` shows what.

### `pip install` takes forever on ARM

Normal, and only on Oracle's ARM machine. See the end of `docs/ORACLE-CLOUD.md`.

### A port looks open and still refuses connections

Only matters if you also run the **MCP server** (the collectors need no open ports at
all). Providers filter traffic in different places — Oracle in **two**, which is the
most common confusion on that platform — and each provider's page says where. The
simplest answer on both is not to open a port at all: leave the server on
`127.0.0.1` and reach it through an SSH tunnel.

> **Do not expose the MCP server to the internet without a token.** It holds your
> trade ledger and can write to it. It refuses to start on a public address
> without `OBLISK_MCP_AUTH_TOKEN` set — that refusal is a feature.

### "the universe table is empty"

You have not seeded the instrument list yet:

```bash
sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect universe
```

### A night is missing, or a job says `interrupted`

`interrupted` means the machine killed the job — a reboot, running out of memory,
the job taking longer than its own time limit — before it could record what it
had done. Nothing writes that status at the time, because a program that has been
killed cannot write anything; the *next* run notices the abandoned record and
closes it out.

It is not, by itself, something to fix. Look at what came after it:

```bash
sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect status
journalctl -u obliskiq-collect@daily.service --since '2 days ago' | tail -50
```

If the next night ran cleanly, one job died and the schedule recovered, which is
what the schedule is for. If it keeps happening, the usual causes are memory (see
step 6c) and a run that has genuinely outgrown `TimeoutStartSec` in the unit file.

### A run says `degraded` every day

The daily price backfill is not keeping up, so strike ranges are being sized from
a weaker volatility estimate. Run `obliskiq-collect backfill` by hand and read the
output. `docs/COLLECTORS.md` explains the fallback ladder.

### Everything IBKR fails with an authentication error

- OAuth not approved yet. Check your IBKR messages.
- The signing and encryption keys were uploaded to the wrong slots (step 3b).
- A `.pem` file was copied incompletely. Each should begin `-----BEGIN` and end
  `-----END`; check with `sudo head -1 /etc/obliskiq/secrets/signature.pem`.

### The times are wrong

Everything is scheduled in **UTC** on purpose, so nothing shifts under daylight
saving. `date -u` shows the server's UTC time. To move a run, edit the
`OnCalendar=` line in the relevant `.timer` file under `/etc/systemd/system/`,
then `sudo systemctl daemon-reload`.

### Running out of disk

```bash
df -h                                       # how full
sudo du -sh /var/lib/obliskiq/              # how big the database is
```

The database grows by roughly a hundred MB a month once chains are collecting. The
free disk is about 47 GB on Oracle and 30 GB on Google, so this should not bite for
a very long time.

---

## 12. What good looks like

A month in, with OAuth approved:

- `systemctl list-timers 'obliskiq*'` shows five timers with future run times.
- `/var/lib/obliskiq/obliskiq.db` is a few hundred MB and growing.
- The logs show `"status": "ok"` most nights and occasionally `"degraded"`.
- You have a month of option-market photographs that **cannot be bought,
  downloaded or recovered from anywhere else**, and which get more valuable every
  day the machine keeps running.

That last point is the whole reason for all of this.

---

## Where to read next

| For | Read |
|---|---|
| Making the machine on Oracle — sign-up, capacity, the reserved address, SSH | `docs/ORACLE-CLOUD.md` |
| Making the machine on Google — the free `e2-micro`, its disk, its address, SSH | `docs/GOOGLE-CLOUD.md` |
| What 1 GB of memory was measured to mean | `ROADMAP.md`, *What the second provider measured* |
| Why two providers, and what is still unverified | `docs/DECISIONS.md`, *Two providers, one walkthrough* |
| How the collectors behave, and reading a run properly | `docs/COLLECTORS.md` |
| The grid, the engines and the rules they enforce | `docs/COMPUTE.md` |
| The MCP server, its tools and its authentication | `docs/SERVER.md` |
| IBKR API quirks found the hard way | `references/ibkr-playbook.md` |
| What has and has not been verified against live IBKR | `references/ibkr-playbook.md`, and `docs/COLLECTORS.md` §UNVERIFIED |
