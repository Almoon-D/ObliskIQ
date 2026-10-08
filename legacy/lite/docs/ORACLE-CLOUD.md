# Oracle Cloud: making the machine

This is the **Oracle half** of the deployment. Everything that is the same on any
provider — registering for IBKR OAuth, installing, secrets, the schedule, reading a
run, upgrading, backing up — is in [`docs/DEPLOY.md`](DEPLOY.md), and this page
sends you back there when the machine exists. The Google equivalent is
[`docs/GOOGLE-CLOUD.md`](GOOGLE-CLOUD.md); `DEPLOY.md` §4 compares the two.

> **Status: written down, never followed end to end.** Nobody has yet provisioned
> this machine, so every console path below is Oracle's menu structure as
> recorded in this repository's earlier notes, not a reading taken on a live
> console. Oracle moves things. Where a step does not match what you see, trust
> the console and correct this page — that is the promise this walkthrough makes
> about itself.

---

## What Oracle gives you

| | Free, permanently |
|---|---|
| **The big one** | `VM.Standard.A1.Flex` (ARM, **aarch64**) — up to **4 OCPUs and 24 GB** in total |
| **The fallback** | `VM.Standard.E2.1.Micro` (AMD, x86_64) — 1/8 of a processor and **1 GB** |
| Boot volume | about 47 GB |

The ARM machine is by far the more comfortable one, and it is also the one Oracle
most often has none of — see *If you get "Out of capacity"* below. The AMD micro
is the same class of machine as Google's `e2-micro`; what 1 GB does and does not
mean for this workload is measured in `ROADMAP.md` (*What the second provider
measured*), and the swap step is `docs/DEPLOY.md` §6c.

---

## 1. Sign up

Go to **cloud.oracle.com** and create an account.

- It asks for a credit card. This is **identity verification** — the Always Free
  resources stay free indefinitely and are not billed. It typically places a small
  temporary hold (around €1) which is released.
- Pick the **home region** closest to you and think before you click: **it cannot
  be changed afterwards**, and free capacity varies a lot between regions. If one
  region refuses to give you a machine in step 2, that choice is the reason.
- Signup can take 15 minutes to a few hours to approve.

## 2. Create the instance

In the Oracle console: **Menu ☰ → Compute → Instances → Create instance**.

1. **Name:** `obliskiq`
2. **Image:** click *Edit*, then *Change image*, choose **Canonical Ubuntu 24.04**.
3. **Shape:** click *Change shape* → **Ampere** → **VM.Standard.A1.Flex**, and set
   **4 OCPUs** and **24 GB memory**. This is the whole free ARM allowance and it
   costs nothing.
4. **SSH keys:** choose **Generate a private key for me** and **download it**.
   The browser gives you a file ending `.key` — save it somewhere you will find
   again. **You cannot download it a second time.**
5. Leave the rest of networking at its defaults for now. Click **Create**.
   The address it attaches is temporary — the next step replaces it, and it has to
   happen **before the first collection run**.

> ### If you get "Out of capacity"
>
> You very likely will. The free ARM machines are popular and Oracle runs out
> constantly. It is not something you did wrong.
>
> Things that work, in order of effort:
> 1. **Try again later.** Capacity is released continuously. Different times of
>    day genuinely help.
> 2. **Ask for less.** 1 OCPU and 6 GB is far more likely to be granted than 4
>    and 24, and is still plenty here. You can grow it later.
> 3. **Try a different availability domain** if your region has AD-1, AD-2, AD-3.
> 4. **Use the AMD machine instead** — see below, or take Google's free machine
>    instead (`docs/GOOGLE-CLOUD.md`), which is dependably available.
>
> **The AMD fallback.** Choose shape **VM.Standard.E2.1.Micro** instead. It is
> almost always available. It is also much smaller — 1/8 of a processor and
> **1 GB of memory**. Add the swap file in `docs/DEPLOY.md` §6c; it is cheap
> insurance on a machine this small.

## 3. Reserve the public IP address — before the first collection run

Oracle attaches an **ephemeral** public IP by default, and an ephemeral address
**changes every time the instance is stopped and started**. Reserve it instead.

> **This is not optional, and it fails quietly if you skip it.** The reason is the
> same on every provider and is written once, in `docs/DEPLOY.md` §4: marketdata.app
> permits one egress address per account and answers a changed one with `HTTP 403`,
> the nightly run reports **degraded** rather than failed, and twelve of the
> fifteen chain names are never photographed.

In the Oracle console:

1. **Menu ☰ → Networking → IP Management → Reserved public IPs → Reserve public
   IP address.** Name it `obliskiq`, take the default pool, **Reserve**.
2. Go back to the instance: **Compute → Instances → `obliskiq` → Attached VNICs
   → (the VNIC) → IPv4 Addresses**.
3. On the primary private IP, choose **Edit** (the ⋮ menu). Under *Public IP
   type* pick **Reserved public IP**, select the one you just made, and **Update**.
   *(UNVERIFIED: if the console refuses to switch an ephemeral address straight to
   a reserved one, choose* No public IP *first, update, then repeat this step.)*
4. The address changes at this point. **Write the new one down** — it is what you
   `ssh` to below, and it is the address marketdata.app will see. It looks like
   `130.61.42.17`.

If you have already run `vendor-chain` from the ephemeral address, the account is
now associated with it. Re-run after attaching the reserved IP; the first call from
the new address may still answer `403`, and the cooldown in `collector_state` means
the retry is a few minutes away rather than immediate.
`references/marketdata-playbook.md` records the observed behaviour of this rule.

## 4. Connecting

Your username on an Ubuntu machine at Oracle is **`ubuntu`**. That is the
`YOUR_USER` that `docs/DEPLOY.md` uses everywhere.

**Mac / Linux** — open Terminal:

```bash
# Only readable by you, or SSH refuses to use it.
chmod 600 ~/Downloads/ssh-key-2026-08-23.key

ssh -i ~/Downloads/ssh-key-2026-08-23.key ubuntu@YOUR_IP_ADDRESS
```

**Windows** — open PowerShell:

```powershell
ssh -i C:\Users\YOU\Downloads\ssh-key-2026-08-23.key ubuntu@YOUR_IP_ADDRESS
```

Replace the filename with whatever you actually downloaded and `YOUR_IP_ADDRESS`
with the **reserved** address from step 3. The first time it asks whether to trust
the host, type `yes`.

You should land on a line ending `ubuntu@obliskiq:~$`. **You are now typing on
the server.** Everything from here happens there, not on your own machine.

Copying files to it — `docs/DEPLOY.md` §7a does this with `scp` — takes the same
`-i` flag:

```bash
scp -i ~/Downloads/ssh-key-2026-08-23.key  FILE  ubuntu@YOUR_IP_ADDRESS:/tmp/
```

<details>
<summary><b>If it says "Permission denied (publickey)"</b></summary>

- Wrong key file, or the path has a typo.
- On Mac/Linux, you skipped `chmod 600` — SSH ignores keys other people could read.
- Wrong username. Oracle's Ubuntu images use `ubuntu`, not `root` or your name.
</details>

<details>
<summary><b>If it just hangs and eventually times out</b></summary>

The machine is probably still starting — wait two minutes and retry. If it
persists, see *The firewall trap* below.
</details>

**Now go back to `docs/DEPLOY.md` §6.**

---

## Things that only go wrong on Oracle

### "Out of capacity" when creating the machine

Covered in step 2. Not your fault; try a smaller shape, another time of day, the
AMD fallback, or Google.

### `pip install` takes forever on ARM

Normal on the A1 shape, and **specific to it**. On aarch64 `numpy` and `scipy`
sometimes have no ready-made download and have to be compiled; ten minutes is not
unusual. If it genuinely fails, confirm `build-essential` and `python3-dev` are
installed (`docs/DEPLOY.md` §6a). The x86_64 machines — Oracle's AMD micro, and
Google's — install prebuilt wheels and do not compile.

### The firewall trap — Oracle blocks ports in **two** places

This is the single most common Oracle Cloud confusion, and it only matters if you
also run the **MCP server** (the collectors need no open ports at all).

Oracle filters traffic **twice**, and opening one does nothing while the other is
shut:

1. **The cloud firewall.** Console → your instance → *Virtual Cloud Network* →
   *Security Lists* → add an **Ingress Rule** for the port.
2. **The firewall on the machine itself.** Oracle's Ubuntu images ship with
   iptables rules that block nearly everything, which surprises people who have
   used other providers:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

If a port looks open in the console and still refuses connections, it is
essentially always the second one. The simplest answer is not to open a port at all:
`docs/DEPLOY.md` §11 says why the MCP server should stay on `127.0.0.1` behind an SSH
tunnel, and that it will not start on a public address without a token.
