#!/bin/bash
# Tdarr runs only during off-hours (00:00-09:00 local). On a 2-core i3 a single
# HandBrake job pushes load past 12 and starves qBittorrent and the *arr apps,
# so the container is stopped outright rather than throttled during the day.
# Stopping mid-transcode is safe: Tdarr re-queues an interrupted file, and the
# leftover work dir is cleaned here so /temp does not accumulate partials.
set -u
LOG=/opt/media/logs/tdarr-window.log
log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

case "${1:-}" in
  start)
    if [ "$(docker inspect -f '{{.State.Running}}' tdarr 2>/dev/null)" != "true" ]; then
      docker start tdarr >/dev/null 2>&1 && log "started (off-hours window open)"
    fi
    ;;
  stop)
    if [ "$(docker inspect -f '{{.State.Running}}' tdarr 2>/dev/null)" = "true" ]; then
      docker stop -t 60 tdarr >/dev/null 2>&1 && log "stopped (daytime)"
      find /mnt/drive1/tdarr-cache -maxdepth 2 -name 'tdarr-workDir*' -mmin +5 \
        -exec rm -rf {} + 2>/dev/null && log "cleaned stale work dirs"
    fi
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 2;;
esac
