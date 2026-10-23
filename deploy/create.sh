#!/bin/bash
# Create the Lightsail server: Ubuntu 24.04, 2 vCPU / 4 GB ($24/month, 4 TB traffic), a static IP,
# and only SSH, HTTP and HTTPS open. Safe to run again: each step is skipped if it is already done.
#     bash deploy/create.sh          (Git Bash; needs the AWS CLI signed in)
set -euo pipefail
NAME=hobbit-agent
REGION=${AWS_REGION:-$(aws configure get region)}
ZONE=${REGION}a
BUNDLE=medium_3_0
KEY=~/.ssh/id_ed25519.pub
# Lightsail calls, tried again when the connection drops (a "not found" answer is final).
lsail() {
    local out code
    for _ in 1 2 3 4 5 6; do
        if out=$(aws lightsail --region "$REGION" --no-cli-pager "$@" 2>&1); then echo "$out"; return 0; fi
        case $out in *"Could not connect"*|*"timed out"*|*"Connection was closed"*) sleep 5 ;; *) echo "$out" >&2; return 1 ;; esac
    done
    echo "$out" >&2; return 1
}

if ! lsail get-key-pair --key-pair-name "$NAME-key" >/dev/null 2>&1; then
    echo "importing your SSH key ($KEY)"
    lsail import-key-pair --key-pair-name "$NAME-key" --public-key-base64 "$(cat $KEY)" >/dev/null
fi

if ! lsail get-instance --instance-name "$NAME" >/dev/null 2>&1; then
    echo "creating $NAME ($BUNDLE, Ubuntu 24.04) in $ZONE"
    lsail create-instances --instance-names "$NAME" --availability-zone "$ZONE" --blueprint-id ubuntu_24_04 \
        --bundle-id "$BUNDLE" --key-pair-name "$NAME-key" --tags key=project,value=hobbit-agent >/dev/null
fi
echo -n "waiting for it to start"
until [ "$(lsail get-instance-state --instance-name "$NAME" --query state.name --output text)" = running ]; do
    echo -n .; sleep 5
done
echo

if ! lsail get-static-ip --static-ip-name "$NAME-ip" >/dev/null 2>&1; then
    lsail allocate-static-ip --static-ip-name "$NAME-ip" >/dev/null
fi
if [ "$(lsail get-static-ip --static-ip-name "$NAME-ip" --query staticIp.isAttached --output text)" != True ]; then
    lsail attach-static-ip --static-ip-name "$NAME-ip" --instance-name "$NAME" >/dev/null
fi

# Only these ports; everything else (the dashboard's own port included) stays closed.
lsail put-instance-public-ports --instance-name "$NAME" --port-infos \
    fromPort=22,toPort=22,protocol=tcp fromPort=80,toPort=80,protocol=tcp fromPort=443,toPort=443,protocol=tcp >/dev/null

IP=$(lsail get-static-ip --static-ip-name "$NAME-ip" --query staticIp.ipAddress --output text)
echo "server: $IP"
echo "next:   bash deploy/push.sh $IP"
