# Maintenance cron jobs

Installed in `mediauser`'s crontab; scripts live in `scripts/`. Logs under `/opt/media/logs/`.

| Schedule | Script | Purpose |
|---|---|---|
| `*/20 * * * *` | `backlog-cron.sh` → `backlog-search.py` | Paced back‑catalogue search. Advances one page through Sonarr/Radarr's *missing* list per run, **only when qBittorrent has < 45 incomplete** (capacity gate) and no search is already running. Prevents flooding Prowlarr/the queue while still working a 10k‑episode backlog. State/offset in `/opt/media/config/backlog-search.state`. |
| `40 */6 * * *` | `foreign-audio-reaper.sh` | Quarantine non‑English audio files (ffprobe, incremental). See [foreign-audio.md](foreign-audio.md). |
| `25 */3 * * *` | `music-failed-cleanup.py` | Remove + blocklist failed Lidarr music imports. See [music-pipeline.md](music-pipeline.md). |
| `50 4 * * *` | `prowlarr-history-prune.sh` | Trim Prowlarr's History table to the last 2 days + VACUUM (the aggressive reaper logs ~90k search rows/day). See [tuning.md](tuning.md). |
| `0 4 * * *` | `reaper-retry-cron.sh` → `reaper_retry.py` | Retry/clean handling for the reaper. |
| `30 8 * * *` | `unmonitor-cron.sh` → `unmonitor_completed.py` | Unmonitor fully‑grabbed items so the backlog doesn't rework them. |
| `*/10 * * * *` | `move-watchdog.sh` | Watches for stuck moves/imports. |
| `17 */6 * * *` | `slskd-clear-cron.sh` → `clear_slskd.py` | Clear completed Soulseek transfers (slskd needs `?remove=true` to actually drop them). |
| **disabled** | `tdarr-window.sh start` | Was `0 0 * * *` — **commented out** because Tdarr crunching at midnight stacked CPU meltdowns during the busy backfill. `0 9 * * * tdarr-window.sh stop` remains. Re‑enable when the backfill winds down. |

## The reaper is a container, not a cron

`torrent-reaper` runs its own loop (`REAP_INTERVAL`, default 60 s) — see [reaper.md](reaper.md). It is **not** in cron.
