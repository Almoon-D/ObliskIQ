# Google Cloud: making the machine

This is the **Google half** of the deployment. Everything that is the same on any
provider — registering for IBKR OAuth, installing, secrets, the schedule, reading a
run, upgrading, backing up — is in [`docs/DEPLOY.md`](DEPLOY.md), and this page
sends you back there when the machine exists. The Oracle equivalent is
[`docs/ORACLE-CLOUD.md`](ORACLE-CLOUD.md); `DEPLOY.md` §4 compares the two.

> **Status: written from Google's documentation on 2026-10-04, never followed on a
> console.** Nobody has provisioned this machine. Facts below are tagged with where
> they came from; anything marked **UNVERIFIED** is a belief about Google's console
> or billing that this repository has not checked. Google moves things and changes
> prices — trust what the console shows, and correct this page.

---

## What Google gives you, and what it does not

Google's Always Free tier for Compute Engine
([source](https://docs.cloud.google.com/free/docs/free-cloud-features), read
2026-10-04) is, per month:

| | Free |
|---|---|
| **VM** | 1 non-preemptible **`e2-micro`** — 2 shared vCPUs, **1 GB** of memory, x86_64 |
| **Where** | **only** `us-west1` (Oregon), `us-central1` (Iowa), `us-east1` (South Carolina) |
| **Disk** | 30 GB-months of **standard** persistent disk |
| **Network out** | 1 GB from North America to all destinations except China and Australia |

The allowance is **by time, not by instance**: one `e2-micro` running all month
uses all of it, and a second one is billed. GPUs are never included.

**What is not free, and matters here:**

- **The public IP address.** Google charges **$0.005 per hour** for an external
  IPv4 address in use by a VM, ephemeral or static alike, from 1 February 2024
  ([source](https://cloud.google.com/vpc/pricing-announce-external-ips)) — about
  **$3.65 a month**. That page does not exempt the free-tier VM, and a search
  summary mentioned "one hour per month per account", which is nothing. **So this
  machine is not 100% free on Google; the address is the cost.** Confirm the
  figure in the console's price estimate before you create anything.
- **The address is not optional.** A static address is a hard requirement on
  every provider — `docs/DEPLOY.md` §4 has the reason — and a machine with no
  external address reaches the internet only through extra paid machinery (Cloud
  NAT, not priced here). A static address in use costs the same as an ephemeral
  one, so reserving it adds nothing to the bill. A reserved
  address left **unattached** is billed at a different rate (UNVERIFIED: not
  looked up); never leave one lying around.
- **The CPU.** `e2-micro` is a shared-core type that sustains about a quarter of a
  core across its two vCPUs and bursts above that for short periods
  (UNVERIFIED: read in a search summary of
  [Google's machine-type page](https://docs.cloud.google.com/compute/docs/general-purpose-machines),
  not on the page). This is what limits the machine, not its memory. What that
  means for the compute job is in `ROADMAP.md`, *What the second provider measured*.
- **Network out beyond 1 GB.** Nothing the collectors do comes close, but the
  off-machine backup in `docs/DEPLOY.md` §10a does count, which is why that step
  compresses the file.

**Memory.** 1 GB looks like enough, on measurements that stop short of a real
machine: `ROADMAP.md` (*What the second provider measured*) has them and says what
is missing — the operating system's own share, and the collectors at scale — and
`docs/DECISIONS.md` (*Two providers, one walkthrough*) the decision. That gap is why
the swap file is on this machine's checklist. It is the same
class as Oracle's AMD micro; if you can get Oracle's 24 GB ARM machine, that is
the more comfortable one, and Google is the one that is dependably available.

---

## 1. Account and billing

1. Create a Google Cloud account at **cloud.google.com** and set up a **billing
   account** — the free tier needs one, with a card.
2. A new account usually gets a time-limited trial credit. The Always Free
   `e2-micro` is separate from it. **UNVERIFIED:** when the trial ends Google asks
   you to upgrade the account to keep resources running; do that when asked, or the
   machine may be stopped, and a stopped machine is a missed day that no money
   recovers (`docs/DEPLOY.md` §0).
3. **Set a budget alert now**: *Billing → Budgets & alerts*, a few dollars. It is
   Google's alert, sent by Google, and needs nothing on the machine — which is
   the right place for it, since this project deliberately sends you nothing
   itself.

## 2. Create the instance

In the Google Cloud console: **Menu ☰ → Compute Engine → VM instances → Create
instance** (the first time, it asks you to enable the Compute Engine API).

1. **Name:** `obliskiq`.
2. **Region:** `us-central1`, `us-west1` or `us-east1`, any zone. **Any other
   region is billed.**
3. **Machine configuration:** *General purpose* → series **E2** → machine type
   **`e2-micro`**. The price estimate on the right is the thing to read: apart from
   the address, it should come to a few dollars at most, and the VM line itself
   should be covered by the free tier (UNVERIFIED how the console shows that).
4. **Boot disk:** *Change* →
   - **Operating system:** Ubuntu · **Version:** Ubuntu 24.04 LTS ·
     **Architecture: x86/64**. Not Arm64 — `e2` is x86. Ubuntu 24.04 keeps every
     command in `docs/DEPLOY.md` identical to Oracle's.
   - **Boot disk type: Standard persistent disk.** The console may default to a
     different type, which is **not** in the free allowance (UNVERIFIED which
     default). This is Google's analogue of Oracle's two firewalls: it costs money
     quietly.
   - **Size:** 30 GB. The database grows by roughly a hundred MB a month once
     chains are collecting, so this lasts for years.
5. **Firewall:** leave *Allow HTTP traffic* and *Allow HTTPS traffic* **unticked**.
   The collectors listen on nothing.
6. **Networking → Network interfaces → External IPv4 address:** choose
   **Reserve static external IP address**, name it `obliskiq`, **Reserve**. Doing it
   here, before the first boot, is the whole fix for the problem
   `docs/DEPLOY.md` §4 describes — there is never an ephemeral address to migrate
   away from.
7. **Create.**

**If you already created the machine with an ephemeral address:** *VPC network →
IP addresses → Reserve external static IP address* (same region as the VM), then
*Compute Engine → VM instances → `obliskiq` → Edit → Network interfaces → External
IPv4 address* and pick the reserved one. The address changes at this point.

**Write the external address down.** It is the address marketdata.app will see,
and what `docs/DEPLOY.md` calls `YOUR_IP_ADDRESS`. If `vendor-chain` has already
run from an ephemeral address, the account is associated with that one; the first
call from the reserved address may answer `403`, and the cooldown in
`collector_state` means the retry is minutes away rather than immediate
(`references/marketdata-playbook.md`).

## 3. Connecting

Two ways. Neither needs you to manage a key file.

**In the browser.** On the VM instances page, press **SSH** beside `obliskiq`. A
terminal opens. This is the easiest way to start.

**From your own terminal**, with Google's `gcloud` tool installed
([install guide](https://docs.cloud.google.com/sdk/docs/install)):

```bash
gcloud auth login
gcloud compute ssh obliskiq --zone=YOUR_ZONE     # e.g. us-central1-a
```

**Your username** is not `ubuntu` here: Google creates it from your account or from
the name on your machine. At the prompt, run `whoami` and read it off. That name is
the `YOUR_USER` that `docs/DEPLOY.md` uses everywhere; nothing in the install
assumes a particular one.

Copying files to the machine — `docs/DEPLOY.md` §7a does this — and back:

```bash
gcloud compute scp FILE obliskiq:/tmp/ --zone=YOUR_ZONE
gcloud compute scp "obliskiq:/tmp/obliskiq-*.db.gz" ~/obliskiq-backups/ --zone=YOUR_ZONE
```

(The browser terminal has an upload button, UNVERIFIED, but for the three `.pem`
files `gcloud compute scp` is the way that leaves no copy in your Downloads.)

**Now go back to `docs/DEPLOY.md` §6.** On this machine **do §6c, the swap file**.

---

## Things that only go wrong on Google

### The firewall is one layer here, not two

Oracle's Ubuntu images block ports on the machine itself as well as in the cloud,
which is the trap `docs/ORACLE-CLOUD.md` ends on. Google's firewall is the VPC's
and only that (UNVERIFIED: Google's Ubuntu images are believed to ship no
restrictive host rules; not checked on a live one). The default network allows
SSH and nothing else from outside, which is exactly what the collectors want.

If you run the MCP server, **leave it on `127.0.0.1` and reach it through a
tunnel**, so no firewall rule has to be opened at all:

```bash
gcloud compute ssh obliskiq --zone=YOUR_ZONE -- -L 8000:127.0.0.1:8000
```

It refuses to start on a public address without `OBLISK_MCP_AUTH_TOKEN`, and that
refusal is a feature.

### The ways this machine starts costing money

Each is a quiet one. Check the console's billing page after the first full day.

- a second VM, or a larger type than `e2-micro`
- a region outside the three above
- a disk that is not *Standard persistent*, or larger than 30 GB
- a reserved address left unattached, for instance after deleting the VM —
  release it (*VPC network → IP addresses*)
- network out beyond 1 GB a month: backups copied off uncompressed
- extras you did not need: snapshots, a load balancer, Cloud NAT

### Stopping and starting the machine

Stopping the VM and starting it again hands an **ephemeral** address a new value,
and a static one keeps it — which is the point of reserving it. Nothing on this
machine needs stopping; if you do, `Persistent=true` on the timers
(`docs/DEPLOY.md` §8) catches up the run you missed when it boots.
