#!/bin/bash
# Control the agent on the server from this PC, over SSH (no web page needed).
#     bash deploy/control.sh <server-ip> status     asleep or awake, and minutes until it sleeps
#     bash deploy/control.sh <server-ip> wake       start: play for another awake period (60 minutes)
#     bash deploy/control.sh <server-ip> sleep      stop: go to sleep after the current move
#     bash deploy/control.sh <server-ip> reset      start the awake period again from now
#     bash deploy/control.sh <server-ip> restart    restart the whole service (it comes back asleep)
#     bash deploy/control.sh <server-ip> logs       follow the agent's log (Ctrl+C to stop)
set -euo pipefail
IP=${1:?usage: bash deploy/control.sh <server-ip> status|wake|sleep|reset|restart|logs}
ACTION=${2:-status}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 ubuntu@$IP"

STATUS='curl -s http://127.0.0.1:8800/api/state | python3 -c "
import json, sys, time
s = json.load(sys.stdin)[\"status\"]
sup = s.get(\"supervision\") or {}
if sup.get(\"asleep\"):
    print(\"asleep\")
else:
    left = (sup.get(\"sleep_at\") or 0) - time.time()
    print(\"awake, %s, attempt %s move %s\" % (\"sleeps in %d:%02d\" % divmod(max(0, int(left)), 60) if sup.get(\"sleep_at\") else \"no sleep timer\",
          s.get(\"attempt\", \"-\"), s.get(\"move\", \"-\")))
"'

case $ACTION in
    status) $SSH "$STATUS" ;;
    wake|sleep|reset)
        $SSH "P=\$(sudo sed -n 's/^HOBBIT_OPERATOR_PASSWORD=//p' /etc/hobbit/hobbit.env)
              curl -s -o /dev/null -w '%{http_code}' -X POST -H \"Authorization: Bearer \$P\" -d '{\"action\": \"$ACTION\"}' \
                  http://127.0.0.1:8800/api/control | grep -q 200 && $STATUS" ;;
    restart) $SSH "sudo systemctl restart hobbit && sleep 6 && $STATUS" ;;
    logs) $SSH "sudo journalctl -u hobbit -f -n 30" ;;
    *) echo "unknown action $ACTION: use status, wake, sleep, reset, restart or logs"; exit 2 ;;
esac
