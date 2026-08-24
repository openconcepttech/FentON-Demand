#!/bin/bash
# Quarantines TV files whose audio has a real foreign-language tag and NO English track.
# Incremental (only files newer than last run). Keeps und/empty/cpe (ambiguous). Recoverable (moves, not deletes).
ROOT=/mnt/drive1/Mount/Videos/TV
CROOT=/tv
QUAR=/mnt/drive1/Mount/Videos/.foreign_quarantine
MARK=/opt/media/scripts/.foreign_reaper_last
LOG=/opt/media/logs/foreign-reaper.log
mkdir -p "$QUAR"
newer=(); [ -f "$MARK" ] && newer=(-newer "$MARK")
now="$(date '+%F %T')"; checked=0; quar=0
while IFS= read -r -d '' f; do
  checked=$((checked+1))
  cf="${f/#$ROOT/$CROOT}"
  langs=$(docker exec jellyfin /usr/lib/jellyfin-ffmpeg/ffprobe -v error -select_streams a \
          -show_entries stream_tags=language -of csv=p=0 "$cf" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  low=",$(printf '%s' "$langs" | tr 'A-Z' 'a-z'),"
  case "$low" in *,eng,*|*,en,*|*,english,*) continue ;; esac      # has English -> keep
  real=$(printf '%s' "$low" | tr ',' '\n' | grep -vE '^(und|cpe|)$' | head -1)
  [ -z "$real" ] && continue                                       # only und/cpe/empty -> keep
  rel="${f#$ROOT/}"; dest="$QUAR/$rel"; mkdir -p "$(dirname "$dest")"
  if mv "$f" "$dest"; then quar=$((quar+1)); echo "$now QUARANTINED [$langs] $rel" >> "$LOG"; fi
done < <(find "$ROOT" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.m4v' \) "${newer[@]}" -print0)
echo "$now run: checked $checked new/changed files, quarantined $quar" >> "$LOG"
touch "$MARK"
