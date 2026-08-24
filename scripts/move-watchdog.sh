#!/bin/bash
# move-watchdog: every 10 min, verify completed torrents left the NVMe staging
# area (/opt/media/downloads/incomplete) for the USB drive, and warn on low SSD space.
# Alerts land in /mnt/drive1/Mount/_ALERT_torrent_moves.txt (visible on Z:) + syslog.
INC=/opt/media/downloads/incomplete
LOG=/opt/media/logs/move-watchdog.log
ALERT=/mnt/drive1/Mount/_ALERT_torrent_moves.txt
MIN_FREE_GB=15
STUCK_MIN=30

ts() { date '+%Y-%m-%d %H:%M:%S'; }
issues=""

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "$free_gb" -lt "$MIN_FREE_GB" ] && issues+="LOW NVME SPACE: ${free_gb}G free (< ${MIN_FREE_GB}G) — moves to USB will start failing if this hits 0\n"

# torrent dirs: no .!qB partials left AND untouched for STUCK_MIN => finished but never moved
for d in "$INC"/*/*/; do
  [ -d "$d" ] || continue
  qb=$(find "$d" -name '*.!qB' -print -quit 2>/dev/null)
  [ -n "$qb" ] && continue
  newest=$(find "$d" -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
  [ -z "$newest" ] && continue
  age_min=$(( ($(date +%s) - newest) / 60 ))
  if [ "$age_min" -ge "$STUCK_MIN" ]; then
    sz=$(du -sh "$d" 2>/dev/null | cut -f1)
    issues+="STUCK (complete but not moved to USB, idle ${age_min}m, ${sz}): ${d}\n"
  fi
done

# loose completed single files sitting in category dirs
while IFS= read -r f; do
  issues+="STUCK single file (complete but not moved to USB): ${f}\n"
done < <(find "$INC" -mindepth 2 -maxdepth 2 -type f ! -name '*.!qB' -mmin +$STUCK_MIN 2>/dev/null)

inc_sz=$(du -sh "$INC" 2>/dev/null | cut -f1)
if [ -n "$issues" ]; then
  {
    echo "TORRENT MOVE WATCHDOG ALERT — $(ts)"
    echo "(auto-generated on the NUC every 10 min; delete after fixing, it will return if the problem persists)"
    echo
    echo -e "$issues"
  } > "$ALERT"
  echo "$(ts) ISSUES (staging=${inc_sz}, nvme_free=${free_gb}G):" >> "$LOG"
  echo -e "$issues" | sed 's/^/    /' >> "$LOG"
  logger -t move-watchdog "issues found; see $ALERT"
else
  [ -f "$ALERT" ] && rm -f "$ALERT"
  echo "$(ts) OK (staging=${inc_sz}, nvme_free=${free_gb}G)" >> "$LOG"
fi
# keep log small
[ "$(wc -l < "$LOG")" -gt 2000 ] && tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
