#!/usr/bin/env bash
# Optional: build + run the official Telegram Bot API server (tdlib/telegram-bot-api) on 127.0.0.1:8081.
# Lets the bot send/receive files up to 2,000 MB. Needs your own api_id/api_hash from https://my.telegram.org.
#   sudo TELEGRAM_API_ID=… TELEGRAM_API_HASH=… bash deploy/telegram-bot-api.sh
# Building takes ~20–40 min on 2 Ampere cores (needs ~2 GB RAM — the swap from install.sh helps).
set -euo pipefail
set +x
[[ -n "${TELEGRAM_API_ID:-}" && -n "${TELEGRAM_API_HASH:-}" ]] || { echo "Set TELEGRAM_API_ID and TELEGRAM_API_HASH."; exit 1; }
apt-get install -y -qq make git zlib1g-dev libssl-dev gperf cmake g++ >/dev/null
if [[ ! -x /usr/local/bin/telegram-bot-api ]]; then
  rm -rf /tmp/tba && git clone -q --recursive https://github.com/tdlib/telegram-bot-api.git /tmp/tba
  mkdir -p /tmp/tba/build && cd /tmp/tba/build
  cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local .. >/dev/null
  cmake --build . --target install -j "$(nproc)" >/dev/null
fi
id tgbotapi >/dev/null 2>&1 || useradd --system --home-dir /var/lib/telegram-bot-api --create-home tgbotapi
install -d -o tgbotapi -g clipforge -m 750 /var/lib/telegram-bot-api
umask 077
printf 'TELEGRAM_API_ID=%s\nTELEGRAM_API_HASH=%s\n' "$TELEGRAM_API_ID" "$TELEGRAM_API_HASH" > /etc/telegram-bot-api.env
cat > /etc/systemd/system/telegram-bot-api.service <<'EOF'
[Unit]
Description=Telegram Bot API server (local, for ClipForge)
After=network-online.target

[Service]
User=tgbotapi
EnvironmentFile=/etc/telegram-bot-api.env
ExecStart=/usr/local/bin/telegram-bot-api --local --http-ip-address=127.0.0.1 --http-port=8081 --dir=/var/lib/telegram-bot-api
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now telegram-bot-api
echo "Running on 127.0.0.1:8081. Next: call https://api.telegram.org/bot<token>/logOut once, then set"
echo "telegram.api_base_url: http://127.0.0.1:8081 in config.yaml and restart clipforge (docs/TELEGRAM.md)."
