#!/usr/bin/env bash
# ClipForge always-on server installer (Ubuntu 24.04 / 22.04, arm64 or x86_64). Idempotent: safe to run again.
#
#   sudo TS_AUTHKEY=tskey-auth-… bash install.sh [--model qwen2.5:7b] [--no-ollama] [--with-bot-api]
#        [--keep-public-ssh] [--branch main] [--repo https://github.com/hamsidhi/clipforge]
#
# What it does: packages (ffmpeg with libass, Python venv, Deno for yt-dlp, age), a non-root `clipforge` user,
# the app in /opt/clipforge/app, Ollama + a model, swap, automatic security updates, time sync, journald limits,
# Tailscale (your auth key is read from the environment, written to a root-only file for one second, never logged),
# a firewall that only allows the tailnet (SSH too, once Tailscale works), systemd services with restart-on-failure,
# `tailscale serve` (HTTPS only inside your tailnet), then `clipforge doctor` and a summary.
set -euo pipefail
set +x  # never echo commands: the auth key must not end up in a log
# keep the auth key in a NON-exported shell variable: child processes (installers piped from curl, builds) never see it
TSKEY="${TS_AUTHKEY:-}"; unset TS_AUTHKEY; export -n TSKEY 2>/dev/null || true

MODEL="qwen2.5:7b"; WITH_OLLAMA=1; WITH_BOT_API=0; KEEP_PUBLIC_SSH=0
BRANCH="main"; REPO="https://github.com/hamsidhi/clipforge"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --no-ollama) WITH_OLLAMA=0; shift ;;
    --with-bot-api) WITH_BOT_API=1; shift ;;
    --keep-public-ssh) KEEP_PUBLIC_SSH=1; shift ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
ARCH=$(uname -m)
APP=/opt/clipforge/app
step() { echo -e "\n==> $*"; }

step "Packages"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates unzip ffmpeg python3 python3-venv python3-dev build-essential \
  ufw unattended-upgrades age sqlite3 chrony espeak-ng >/dev/null
ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass " || { echo "ffmpeg has no libass filter — captions need it"; exit 1; }

step "Automatic security updates + time sync + journald limit"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true
systemctl enable --now chrony >/dev/null 2>&1 || true
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/clipforge.conf
systemctl restart systemd-journald

step "Swap (Whisper + the LLM + a render can overlap)"
if ! swapon --show | grep -q .; then
  MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  SWAP_GB=$(( MEM_GB >= 16 ? 4 : 8 ))
  fallocate -l "${SWAP_GB}G" /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -q vm.swappiness=10; echo 'vm.swappiness=10' > /etc/sysctl.d/99-clipforge.conf
fi

step "Deno (yt-dlp needs a JavaScript runtime for YouTube)"
if ! command -v deno >/dev/null; then
  case "$ARCH" in aarch64|arm64) DZ=deno-aarch64-unknown-linux-gnu.zip ;; *) DZ=deno-x86_64-unknown-linux-gnu.zip ;; esac
  curl -fsSL "https://github.com/denoland/deno/releases/latest/download/$DZ" -o /tmp/deno.zip
  unzip -oq /tmp/deno.zip -d /usr/local/bin && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip
fi

step "Node.js (only to build the web app once)"
if ! command -v node >/dev/null || [[ $(node -v | cut -d. -f1 | tr -d v) -lt 20 ]]; then
  case "$ARCH" in aarch64|arm64) NA=arm64 ;; *) NA=x64 ;; esac
  NV=$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c "import json,sys;print([r['version'] for r in json.load(sys.stdin) if r['lts']][0])")
  curl -fsSL "https://nodejs.org/dist/$NV/node-$NV-linux-$NA.tar.xz" | tar -xJ -C /opt && ln -sfn "/opt/node-$NV-linux-$NA" /opt/node
  ln -sf /opt/node/bin/node /usr/local/bin/node && ln -sf /opt/node/bin/npm /usr/local/bin/npm
fi

step "User + app"
id clipforge >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/clipforge --shell /bin/bash clipforge
install -d -m 750 -o clipforge -g clipforge /opt/clipforge
if [[ -d /opt/clipforge/.ssh ]]; then  # deploy key for a private repo (deploy/README.md step 4)
  chown -R clipforge:clipforge /opt/clipforge/.ssh && chmod 700 /opt/clipforge/.ssh && chmod 600 /opt/clipforge/.ssh/id_* 2>/dev/null || true
fi
if [[ -d $APP/.git ]]; then
  sudo -u clipforge git -C "$APP" fetch -q origin "$BRANCH" && sudo -u clipforge git -C "$APP" checkout -q "$BRANCH" \
    && sudo -u clipforge git -C "$APP" pull -q --ff-only
else
  sudo -u clipforge git clone -q --branch "$BRANCH" "$REPO" "$APP"
fi
cd "$APP"
[[ -x .venv/bin/python ]] || sudo -u clipforge python3 -m venv .venv
sudo -u clipforge .venv/bin/pip install -q --upgrade pip wheel
if ! sudo -u clipforge .venv/bin/pip install -q -r requirements.txt; then
  echo "Some package has no wheel for $ARCH (usually mediapipe) — installing the rest; faces then use OpenCV YuNet only."
  grep -v -i '^mediapipe' requirements.txt > /tmp/req-nomp.txt
  sudo -u clipforge .venv/bin/pip install -q -r /tmp/req-nomp.txt
fi
sudo -u clipforge .venv/bin/pip install -q -e .
sudo -u clipforge bash -c "cd frontend && npm ci --silent --no-audit --no-fund && npm run build --silent" >/dev/null

step "Config (server mode; your config.yaml is kept if it exists)"
if ! grep -q "^  mode: server" config.yaml 2>/dev/null; then
  sudo -u clipforge .venv/bin/python - <<'PY'
from clipforge.config import load_config, save_config
c = load_config()
save_config(c.with_overrides({"deployment.mode": "server", "llm.keep_alive": "2m", "whisper.model": "large-v3-turbo"}))
PY
fi

if [[ $WITH_OLLAMA -eq 1 ]]; then
  step "Ollama + $MODEL"
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh >/dev/null
  mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment=OLLAMA_KEEP_ALIVE=2m\nEnvironment=OLLAMA_HOST=127.0.0.1:11434\n' > /etc/systemd/system/ollama.service.d/clipforge.conf
  systemctl daemon-reload && systemctl enable --now ollama >/dev/null 2>&1 && sleep 3
  ollama list | grep -q "^${MODEL%%:*}" || ollama pull "$MODEL"
fi

step "systemd service"
install -m 644 deploy/clipforge.service /etc/systemd/system/clipforge.service
# `clipforge server update` (run as clipforge) may restart/stop only this one service — nothing else
SC=$(command -v systemctl)
printf 'clipforge ALL=(root) NOPASSWD: %s restart clipforge, %s stop clipforge, %s start clipforge\n' "$SC" "$SC" "$SC" \
  > /etc/sudoers.d/clipforge && chmod 440 /etc/sudoers.d/clipforge && visudo -cq
systemctl daemon-reload
systemctl enable clipforge >/dev/null
systemctl restart clipforge

if [[ $WITH_BOT_API -eq 1 ]]; then
  step "Telegram Bot API server (local, 2 GB files) — needs TELEGRAM_API_ID + TELEGRAM_API_HASH in the environment"
  bash deploy/telegram-bot-api.sh
fi

step "Tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
if ! tailscale status >/dev/null 2>&1; then
  [[ -n "$TSKEY" ]] || { echo "Set TS_AUTHKEY (Tailscale admin → Settings → Keys → Generate auth key)."; exit 1; }
  KF=$(mktemp); chmod 600 "$KF"; printf '%s' "$TSKEY" > "$KF"; TSKEY=""
  tailscale up --auth-key="file:$KF" --hostname=clipforge-server --ssh=false >/dev/null; rm -f "$KF"
fi
tailscale serve --bg --https=443 http://127.0.0.1:8000 >/dev/null 2>&1 || true

step "Firewall: nothing from the internet; the tailnet only"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw allow in on tailscale0 >/dev/null
# Close public SSH only when this very session already comes over Tailscale (100.64.0.0/10) — otherwise you could lock
# yourself out. Over the public IP: it stays open; test `ssh ubuntu@clipforge-server` from the laptop, then run
# `sudo ufw delete allow 22/tcp` and remove the port-22 rule in Oracle's security list (deploy/README.md step 5).
FROM_TAILNET=0
if [[ -n "${SSH_CLIENT:-}" ]]; then
  IP=${SSH_CLIENT%% *}; O1=${IP%%.*}; R=${IP#*.}; O2=${R%%.*}
  [[ $O1 == 100 && $O2 -ge 64 && $O2 -le 127 ]] && FROM_TAILNET=1
fi
if [[ $KEEP_PUBLIC_SSH -eq 1 ]] || ! tailscale status >/dev/null 2>&1 || [[ $FROM_TAILNET -eq 0 && -n "${SSH_CLIENT:-}" ]]; then
  ufw allow 22/tcp >/dev/null
  SSH_NOTE="public SSH still OPEN — after checking 'ssh ubuntu@clipforge-server' works: sudo ufw delete allow 22/tcp"
else
  SSH_NOTE="SSH only over Tailscale"
fi
ufw --force enable >/dev/null

step "Check"
sleep 5
sudo -u clipforge .venv/bin/clipforge doctor || true
TSNAME=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "?")
curl -fsS http://127.0.0.1:8000/api/health >/dev/null && HEALTH=ok || HEALTH="NOT RESPONDING (journalctl -u clipforge)"
cat <<EOF

=================== ClipForge server ===================
 App health      : $HEALTH
 Tailnet address : https://$TSNAME   (only devices on your tailnet)
 Firewall        : deny all incoming except tailscale0; $SSH_NOTE
 Ollama model    : $([[ $WITH_OLLAMA -eq 1 ]] && echo "$MODEL" || echo "none (--no-ollama)")
 Next (as clipforge):
   sudo -u clipforge $APP/.venv/bin/clipforge auth set-password
   sudo -u clipforge $APP/.venv/bin/clipforge server add-worker laptop
   … or from the laptop: clipforge server handover --to $TSNAME --dry-run
 Logs: journalctl -u clipforge -f      Update: sudo -u clipforge $APP/.venv/bin/clipforge server update
=========================================================
EOF
