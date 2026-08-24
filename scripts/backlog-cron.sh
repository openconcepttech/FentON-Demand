#!/bin/bash
# Paced backlog search. Every 20 min it advances one page through the missing
# list, but only when the download client has spare capacity, so the queue and
# Prowlarr are never flooded.
/usr/bin/python3 /opt/media/scripts/backlog-search.py >> /opt/media/logs/backlog-search.log 2>&1
tail -n 3000 /opt/media/logs/backlog-search.log > /opt/media/logs/backlog-search.log.tmp 2>/dev/null \
  && mv /opt/media/logs/backlog-search.log.tmp /opt/media/logs/backlog-search.log
