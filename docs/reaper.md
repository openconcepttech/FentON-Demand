# torrent-reaper

Custom container (`build/dashboard-stats/reaper.py`, shares the dashboard-stats image, `command: ["python","-u","reaper.py"]`). It watches qBittorrent and removes torrents that will **never reach 100 %**, then blocklists them and asks Sonarr/Radarr to search a different release. This keeps the download queue full of *finishable* torrents.

## Reap rules (`classify_dead`)

A torrent is reaped if it is incomplete **and** any of:

1. **No metadata** — `metaDL`, no peers, no seeds, older than `REAP_META_MIN`.
2. **No seed in the swarm** — `num_complete == 0` after `REAP_NOSEED_MIN`. Peers/leechers **do not count**: only a seed (a complete copy) guarantees followthrough to 100 %. This fires even while the torrent is pulling data from leechers.
3. **Download stalled** — `dlspeed < STALL_BPS` continuously for `REAP_STALL_END_MIN`, **even if seeds are present**. A per‑torrent stall timer (`_dl_stall`, in‑memory, resets when speed resumes) tracks this across the loop.

Torrents **with** a seed that are actively transferring are always kept.

## Thresholds (env vars, set in `docker-compose.yml`)

| Var | Meaning | Typical |
|---|---|---|
| `REAP_INTERVAL` | seconds between checks | `60` (aggressive) |
| `REAP_META_MIN` | min for no‑metadata reap | `3` |
| `REAP_NOSEED_MIN` | min for no‑seed reap | `2` |
| `REAP_STALL_END_MIN` | min of zero speed before reap | `3` |
| `REAP_STALL_BPS` | bytes/s below which "stalled" | `1024` |
| `REAP_PROMOTE_MAX` | max torrents force‑started past the queue | `10` |
| `REAP_DRY_RUN` | log only, don't reap | `false` |

Lower thresholds = more aggressive cycling. Behind a slow/passive VPN, very low stall thresholds can reap torrents that were merely slow to connect — raise `REAP_STALL_END_MIN` if that happens.

## promote()

Each cycle the reaper **force‑starts** up to `REAP_PROMOTE_MAX` incomplete torrents that have proven seeds (so live torrents aren't starved in the queue) and **un‑forces** completed ones (so qBittorrent's share limits can apply and the *arrs can remove them). Force‑start bypasses `max_active_downloads`.

## Replacement search

On reap it calls the *arr APIs to **blocklist** the release and trigger a fresh **EpisodeSearch/MoviesSearch**, so a dead release is swapped for a different one automatically. Log line: `reaped: N via *arr (blocklisted + replacement search)`.

Reaped torrents are archived to `/data/reaper-archive` (in the container) before removal.
