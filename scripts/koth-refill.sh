#!/bin/bash
KEY=YOUR_API_KEY_HERE
BASE=http://127.0.0.1:8989/api/v3
LOG=/opt/media/logs/koth-refill.log
mapfile -t IDS < /tmp/koth_missing.txt
batch=12; slp=120
echo "$(date): starting paced search of ${#IDS[@]} KotH episodes (batch=$batch, gap=${slp}s)" >> "$LOG"
for ((i=0;i<${#IDS[@]};i+=batch)); do
  chunk=("${IDS[@]:i:batch}")
  ids=$(printf '%s,' "${chunk[@]}"); ids="[${ids%,}]"
  curl -s -o /dev/null -X POST "$BASE/command" -H "X-Api-Key: $KEY" -H "Content-Type: application/json" -d "{\"name\":\"EpisodeSearch\",\"episodeIds\":$ids}"
  echo "$(date): searched ${#chunk[@]} eps (offset $i)" >> "$LOG"
  sleep $slp
done
echo "$(date): KotH paced search COMPLETE" >> "$LOG"
