# Always-on ClipForge server (free) — setup

Honest version first: **when your laptop is off, nothing on the laptop runs.** "Always online" means a second machine
that is always on and costs nothing. The plan below uses Oracle Cloud's *Always Free* Arm VM; the laptop then becomes a
worker that does heavy jobs faster whenever it's on. Your current setup keeps working unchanged until you run the
handover yourself (`deployment.mode` stays `standalone` by default).

Comparison and reasons: DECISIONS.md → "Phase 15 → Part B". Protocol: docs/WORKER_PROTOCOL.md.

## What you get for free (checked 2026-11-04 on Oracle's docs)
* Ampere A1 (Arm): **1,500 OCPU-hours + 9,000 GB-hours a month ≈ 2 OCPU + 12 GB RAM, always on** (Oracle cut this from
  4 OCPU / 24 GB in 2026 — older guides are out of date), 200 GB block storage, 10 TB outbound traffic a month.
* Always Free resources must be in your **home region**, chosen once at sign-up and never changeable.
* A **card is needed only for identity verification** at sign-up. Always Free resources aren't billed; a Free Tier
  account can't run paid resources unless you upgrade it. The steps below add a budget alert anyway.
* **Idle reclaim:** Oracle may reclaim an Always Free instance that is idle for 7 days (95th-percentile CPU < 20 %,
  network < 20 % and memory < 20 %). ClipForge's normal work (bot, scheduler every minute, jobs) may or may not keep it
  above that — a quiet week without jobs could look idle. ClipForge does **not** fake load (that's against the spirit
  of the free tier). If it gets reclaimed: the boot volume usually survives — create a new instance from it, or restore
  from your nightly backup (`clipforge server restore`). Converting to Pay-As-You-Go stops reclaiming but turns billing
  on: only do it with the budget alert below **and** after checking every resource is an Always Free shape.

## Steps (≈ 45 minutes, most of it waiting)

### 1. Oracle account + budget alert (10 min)
1. <https://signup.cloud.oracle.com> → Free Tier. **Home region:** India South (Hyderabad) or India West (Mumbai) for
   low latency. If you already know A1 capacity is scarce there, Singapore is the next closest. You can't change it later.
2. Card verification (a small refundable authorisation). Don't click "Upgrade to Pay As You Go".
3. ☰ → Billing & Cost Management → **Budgets** → Create Budget: compartment = root, amount **₹1** (or $1), alert rule
   "Actual spend" at 1 % → your email. You'll hear about any charge immediately.
4. ☰ → Governance → **Limits, Quotas and Usage** → filter "A1" to see your Always Free allowance.

### 2. Tailscale auth key (2 min)
<https://login.tailscale.com/admin/settings/keys> → Generate auth key: **one-off**, *pre-approved*, expiry 1 day.
Copy it (`tskey-auth-…`). Also in Admin → DNS: MagicDNS **on**, HTTPS Certificates **on** (you did this in Phase 14).

### 3. Create the VM (5 min + capacity retries)
☰ → Compute → Instances → **Create instance**
1. Name `clipforge-server`. Image: **Canonical Ubuntu 24.04** (not "Minimal"). Shape: **Ampere → VM.Standard.A1.Flex,
   2 OCPU, 12 GB** (the whole free allowance, one VM).
2. Networking: default VCN is fine, **Assign a public IPv4 address: yes** (only to reach Tailscale/updates; the
   firewall closes everything inbound afterwards).
3. SSH keys: upload your public key (or let Oracle generate a pair and download it).
4. Boot volume: **100 GB** (of the 200 GB free) — videos are big.
5. (Optional) Advanced → Management → paste `deploy/cloud-init.yaml` with your auth key filled in, to install
   unattended. Otherwise use step 4.
6. Create. **"Out of host capacity"**: A1 is often full in popular regions. Try another *Availability Domain* in the
   same screen, try again at night / early morning IST, or retry every few minutes. Don't create in another region
   (Always Free is home-region only).

### 4. Install ClipForge (5 min typing, ~20 min waiting)
Your GitHub repo is **private**, so the server needs a read-only *deploy key* to clone it (2 min):
```bash
# on the laptop (from the clipforge folder): copy the installer to the VM
scp deploy/install.sh ubuntu@<public ip>:
ssh ubuntu@<public ip>
sudo mkdir -p /opt/clipforge/.ssh && sudo ssh-keygen -t ed25519 -N "" -f /opt/clipforge/.ssh/id_ed25519 -C clipforge-server
sudo cat /opt/clipforge/.ssh/id_ed25519.pub
```
GitHub → your repo → Settings → **Deploy keys** → Add deploy key → paste it, **leave "Allow write access" off**. Then:
```bash
sudo bash -c 'ssh-keyscan github.com >> /opt/clipforge/.ssh/known_hosts'
sudo TS_AUTHKEY=tskey-auth-XXXX bash install.sh --repo git@github.com:hamsidhi/clipforge.git   # --model qwen2.5:3b = smaller LLM
```
(If you ever make the repo public, `--repo` isn't needed and cloud-init works as-is.)
The key is read from the environment, written to a root-only file for a second, and never logged. At the end you see
the tailnet address (`https://clipforge-server.<tailnet>.ts.net`), the health check and `clipforge doctor`.

### 5. Close the cloud firewall (3 min)
The VM's own firewall (ufw) already denies everything except your tailnet. Also close Oracle's:
☰ → Networking → Virtual Cloud Networks → your VCN → **Security Lists** → Default Security List →
**delete the ingress rule for port 22 (0.0.0.0/0)**. Keep the egress rule. From now on you SSH over Tailscale:
`ssh ubuntu@clipforge-server`. (If you ever lock yourself out: Instances → Console connection.)

### 6. Set it up (5 min)
```bash
ssh ubuntu@clipforge-server
sudo -u clipforge /opt/clipforge/app/.venv/bin/clipforge auth set-password      # for the web app on your phone
sudo -u clipforge /opt/clipforge/app/.venv/bin/clipforge server backup-keygen --out /tmp/backup.key
# put the printed cfpk1:… key in /opt/clipforge/app/config.yaml → server.backup_recipient, then:
cat /tmp/backup.key   # copy it into your password manager / laptop, then:
rm /tmp/backup.key && sudo systemctl restart clipforge
```

### 7. Move from the laptop (≈ 10 min + copy time) — on the laptop
```bash
clipforge server handover --to clipforge-server.<tailnet>.ts.net --dry-run
clipforge server handover --to clipforge-server.<tailnet>.ts.net --run [--skip-sources]
```
This stops publishing and the Telegram bot on the laptop first, copies DB + workspace + logins over SSH with checksums,
switches the server to `server` mode, creates the laptop's worker key and makes the laptop a worker. Undo at any time:
`clipforge server handover --rollback --to clipforge-server.<tailnet>.ts.net`.

### 8. Laptop worker
```bash
clipforge worker status          # connected?
clipforge worker install-task    # start at logon + on wake (Windows)
```

## Day to day
| | |
|---|---|
| status | Telegram `/server`, or `clipforge server status` on the server |
| logs | `clipforge server logs -f` |
| update | `sudo -u clipforge /opt/clipforge/app/.venv/bin/clipforge server update` (pull → install → DB backup + migrate → restart → health check; rolls back by itself if unhealthy) |
| backups | nightly at `server.backup_time` (encrypted, 7 daily + 4 weekly in `workspace/backups`; `rclone_remote` for an off-site copy to your own Drive via an rclone *crypt* remote). Restore: `clipforge server restore <file> --key backup.key [--apply]` |
| disk | old sources/clips are cleaned nightly by the rules in config (`clipforge server cleanup --dry-run` to see); Telegram warns at 80 %, new jobs pause at 90 % |
| new account | connect it on the laptop (needs a browser), then `clipforge server import-token --to clipforge-server.<tailnet>.ts.net` |
| YouTube blocks the server | Telegram tells you; the laptop downloads it when it's on, or send the file. Optional `server.download_cookies_file` (use a **throwaway** Google account — cookies give full access to that account and YouTube may lock it) |

## Performance on 2 OCPU / 12 GB (estimates — see DEPLOY_TEST.md for what was measured)
| 1 h podcast | server (2 Ampere cores) | laptop worker |
|---|---|---|
| download | 2–5 min (if YouTube allows) | 2–5 min |
| transcribe (Whisper large-v3-turbo int8) | ~2–3 h | ~25–30 min (CPU) / ~5 min (GPU) |
| select (qwen2.5:7b Q4, 8 windows) | ~30–50 min | ~10–15 min |
| render 20 clips | ~40–70 min | ~10–20 min |
These are estimates scaled from the laptop numbers in DECISIONS.md (no Ampere machine was available); the server is
slow but finishes on its own overnight — when the laptop is on, the heavy parts move there automatically.
