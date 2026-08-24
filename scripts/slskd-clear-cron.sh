#!/bin/bash
# Prune failed/terminal Soulseek transfers (Rejected/Errored/Cancelled/TimedOut)
# out of slskd, which never auto-cleans them. Runs the python cleaner inside the
# dashboard-stats container (it has python+requests and can reach slskd via the
# gluetun netns; the host cannot, since slskd is behind the VPN).
LOG=/opt/media/logs/slskd-clear.log
KEY=$(grep -oP "SLSKD_API_KEY=\K.*" /opt/media/compose/.env 2>/dev/null | tr -d "\r")
OUT=$(docker exec -i -e SLSKD_API_KEY="$KEY" -e SLSKD_URL="http://gluetun:5030" dashboard-stats python - --go < /opt/media/scripts/clear_slskd.py 2>&1)
echo "[$(date "+%F %T")] $(echo "$OUT" | tr "\n" " ")" >> "$LOG"
