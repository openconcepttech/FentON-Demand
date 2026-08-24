#!/bin/bash
# Nightly sweep: unmonitor anything that finished downloading.
# Runs at 08:30, near the end of the Tdarr off-hours window, so a night of
# grabs and imports is reflected before the box gets busy again.
LOG=/opt/media/logs/unmonitor.log
{
  echo "[$(date '+%F %T')] sweep start"
  /usr/bin/python3 /opt/media/scripts/unmonitor_completed.py --go 2>&1 | sed 's/^/  /'
} >> "$LOG" 2>&1
# keep the log from growing without bound
tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
