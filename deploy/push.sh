#!/bin/bash
# Install or update hobbit-agent on the server.
#     bash deploy/push.sh <server-ip>            (Git Bash, from the project folder)
#
# Sends the committed code (git HEAD), the settings in deploy/hobbit.env, and your tape.
# The agent's memory (data/*.json) is copied only on the first install, so the server's
# newer memory is never overwritten. Then runs deploy/setup.sh there and restarts it.
set -euo pipefail
IP=${1:?usage: bash deploy/push.sh <server-ip>}
cd "$(dirname "$0")/.."
ENV=deploy/hobbit.env
[ -f "$ENV" ] || { echo "create $ENV first (copy deploy/hobbit.env.example)"; exit 1; }
[ -f data/game/hobbit.tzx ] || { echo "data/game/hobbit.tzx is missing"; exit 1; }
grep -q "^ANTHROPIC_API_KEY=.." "$ENV" || echo "note: no ANTHROPIC_API_KEY in $ENV yet: the agent can sleep, but not play"
sed -i 's/^\([A-Z_]*\)=[[:space:]]*/\1=/; s/[[:space:]]*$//' "$ENV"       # stray spaces around pasted keys
if grep -q '^YOUTUBE_STREAM_KEY=rtmp' "$ENV"; then
    echo "YOUTUBE_STREAM_KEY holds the server URL: it needs the stream key itself (YouTube Studio -> Go live -> Stream key)"
    exit 1
fi
if ! grep -q '^HOBBIT_OPERATOR_PASSWORD=..' "$ENV"; then
    sed -i '/^HOBBIT_OPERATOR_PASSWORD=/d' "$ENV"
    echo "HOBBIT_OPERATOR_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')" >> "$ENV"
    echo "made an operator password; it is in $ENV"
fi
[ -z "$(git status --porcelain -- hobbit prompts deploy hobbit.toml)" ] || echo "note: uncommitted changes are not sent (only git HEAD is)"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git archive --format=tar.gz -o "$WORK/app.tar.gz" HEAD
tar -czf "$WORK/data.tar.gz" data/game/hobbit.tzx $(compgen -G "data/*.json" || true)
cp "$ENV" "$WORK/hobbit.env"
SSH="ssh -o StrictHostKeyChecking=accept-new ubuntu@$IP"
scp -q -o StrictHostKeyChecking=accept-new "$WORK/app.tar.gz" "$WORK/data.tar.gz" "$WORK/hobbit.env" deploy/setup.sh "ubuntu@$IP:/tmp/"
$SSH "sudo PUBLIC_IP=$IP bash /tmp/setup.sh && rm -f /tmp/hobbit.env"
echo
echo "dashboard:  https://${IP//./-}.sslip.io/"
echo "controls:   https://${IP//./-}.sslip.io/?operator   (password: HOBBIT_OPERATOR_PASSWORD in $ENV)"
