#!/bin/bash
# Install or update hobbit-agent on the server (Ubuntu 24.04, run as root by deploy/push.sh).
# Expects /tmp/app.tar.gz, /tmp/data.tar.gz and /tmp/hobbit.env, and PUBLIC_IP in the environment.
# Safe to run again: each step is skipped or repeated harmlessly.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${PUBLIC_IP:?}"

if ! command -v fuse-sdl >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v caddy >/dev/null; then
    apt-get update -q
    apt-get install -y -q xvfb fuse-emulator-sdl spectrum-roms ffmpeg caddy python3-venv git curl fonts-dejavu-core
fi
test -f /usr/share/spectrum-roms/48.rom || { echo "the Spectrum ROMs are missing from /usr/share/spectrum-roms"; exit 1; }
if ! command -v google-chrome-stable >/dev/null; then
    curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y -q /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi

id hobbit >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/hobbit --shell /usr/sbin/nologin hobbit
install -d -o hobbit -g hobbit -m 750 /var/lib/hobbit/data /var/lib/hobbit/data/game /var/lib/hobbit/sessions /var/lib/hobbit/logs

# The code belongs to root: the service can read it but not change it.
rm -rf /opt/hobbit/app.new && mkdir -p /opt/hobbit/app.new
tar -xzf /tmp/app.tar.gz -C /opt/hobbit/app.new
rm -rf /opt/hobbit/app.old
[ -d /opt/hobbit/app ] && mv /opt/hobbit/app /opt/hobbit/app.old
mv /opt/hobbit/app.new /opt/hobbit/app
chmod -R u=rwX,go=rX /opt/hobbit/app
[ -d /opt/hobbit/venv ] || python3 -m venv /opt/hobbit/venv
/opt/hobbit/venv/bin/pip install -q --upgrade pip
/opt/hobbit/venv/bin/pip install -q "specbot @ git+https://github.com/alandhay/spec-agent@main" \
    python-xlib Pillow "claude-agent-sdk==0.2.168"

# The tape always; the agent's memory only the first time (the server's own is newer after that).
mkdir -p /tmp/hobbit-data && tar -xzf /tmp/data.tar.gz -C /tmp/hobbit-data
install -o hobbit -g hobbit -m 640 /tmp/hobbit-data/data/game/hobbit.tzx /var/lib/hobbit/data/game/hobbit.tzx
if [ ! -f /var/lib/hobbit/data/stats.json ]; then
    for f in /tmp/hobbit-data/data/*.json; do [ -f "$f" ] && install -o hobbit -g hobbit -m 640 "$f" /var/lib/hobbit/data/; done
fi
rm -rf /tmp/hobbit-data /tmp/data.tar.gz /tmp/app.tar.gz

# Settings: the project's hobbit.toml, pointed at /var/lib/hobbit. Keys: only root and systemd read them.
install -d -m 755 /etc/hobbit
sed -e 's|^tape = .*|tape = "/var/lib/hobbit/data/game/hobbit.tzx"\ndata_dir = "/var/lib/hobbit/data"\nsessions_dir = "/var/lib/hobbit/sessions"|' \
    /opt/hobbit/app/hobbit.toml > /etc/hobbit/hobbit.toml
install -o root -g root -m 600 /tmp/hobbit.env /etc/hobbit/hobbit.env

# Streams only once a YouTube key is set; starts asleep, so nothing plays until the operator presses Wake.
cat >/usr/local/bin/hobbit-run <<'RUN'
#!/bin/bash
args=(run --no-browser --start-asleep)
[ -n "${YOUTUBE_STREAM_KEY:-}${HOBBIT_STREAM_URL:-}" ] && args+=(--stream)
exec /opt/hobbit/venv/bin/python -m hobbit --config /etc/hobbit/hobbit.toml "${args[@]}"
RUN
chmod 755 /usr/local/bin/hobbit-run

cat >/etc/systemd/system/hobbit.service <<'UNIT'
[Unit]
Description=hobbit-agent - an AI agent playing The Hobbit, with its dashboard and broadcast
After=network-online.target
Wants=network-online.target

[Service]
User=hobbit
WorkingDirectory=/opt/hobbit/app
EnvironmentFile=/etc/hobbit/hobbit.env
Environment=HOME=/var/lib/hobbit PYTHONUNBUFFERED=1
ExecStart=/usr/local/bin/hobbit-run
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/hobbit /tmp
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

# HTTPS for the dashboard: a free certificate for <ip>.sslip.io (a name that always points at the IP).
cat >/etc/caddy/Caddyfile <<CADDY
${PUBLIC_IP//./-}.sslip.io {
    reverse_proxy 127.0.0.1:8800 {
        flush_interval -1
    }
}
CADDY
systemctl reload caddy || systemctl restart caddy

systemctl daemon-reload
systemctl enable -q hobbit
systemctl restart hobbit
sleep 8
systemctl is-active hobbit
curl -fsS -o /dev/null -w "dashboard answers locally: HTTP %{http_code}\n" http://127.0.0.1:8800/
