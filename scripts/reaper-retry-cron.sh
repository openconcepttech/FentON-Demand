#!/bin/bash
# Re-add archived (previously reaped) torrents whose swarm may have recovered.
# Runs off-peak; the script itself skips if the download queue is already busy.
docker exec torrent-reaper python3 /app/reaper_retry.py >> /opt/media/logs/reaper-retry.log 2>&1
tail -n 2000 /opt/media/logs/reaper-retry.log > /opt/media/logs/reaper-retry.log.tmp 2>/dev/null \
  && mv /opt/media/logs/reaper-retry.log.tmp /opt/media/logs/reaper-retry.log
