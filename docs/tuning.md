# Resource tuning (the 2‑core reality)

The NUC has **2 physical cores / 4 threads / 7.6 GB RAM**. Load above ~14 has repeatedly caused sluggishness/meltdowns. Everything below is about not overcommitting those 2 cores.

## Container limits (`docker-compose.yml`)

Limits are **caps, not reservations** — the numbers deliberately sum to more than 2 cores; the kernel shares the real cores among whatever's running. Notable values:

| Container | cpus | mem_limit | Why |
|---|---|---|---|
| plex | 3.0 | — | transcode headroom |
| gluetun | 2.0 | 256m | raised from 1.0 for VPN throughput |
| qbittorrent | 4.0 | 1g | hashing at high throughput |
| jellyfin / nzbget | 2.0 | — | playback / post‑processing |
| prowlarr | 1.0 | **2g** | raised from 1g — the stats query OOM'd |
| slskd | 1.0 | **1g** | raised from 512m — was OOM‑restart‑looping |
| lidarr | 1.0 | **768m** | raised from 384m |
| slskd-bridge | — | 256m | OOM‑killed here before — watch it |

Apply live without a restart: `docker update --cpus N --memory Ng <container>` (then persist in compose).

## What to shed when load spikes

Order of "wasted vs productive" CPU:

1. **Plex** — if you're a Jellyfin household, Plex just scans/thumbnails the same library for nobody. `docker stop plex` (stays down via `unless-stopped`).
2. **Tdarr** — HandBrake crunch is optional/off‑hours; its midnight auto‑start is **disabled** in cron because it stacked meltdowns. Re‑enable when quiet.
3. **Bazarr** — subtitle catch‑up on a big reimport pegs a core; pause during surges, resume once load < 5.

Productive load you should *not* kill: qBittorrent (downloads), slskd (music), gluetun (VPN), the *arr searches.

## Diagnosing high load

- `top` `%Cpu` line: high **us/sy/si** = CPU‑bound (torrent hashing + network softirq from many peer connections); high **wa** = disk I/O wait (fast writes to the slow ntfs3 USB).
- Cheap CPU wins at high torrent throughput: **trim peer connections** (`max_connec_per_torrent` 30→15, `max_connec` 150→100) — a torrent maxes out from a few good seeds, not 30 connections.

## Prowlarr history bloat

The aggressive reaper fires a replacement search per reap → ~90k Prowlarr `History` rows/day. Left unchecked the DB hits 250 MB and `/api/v1/indexerstats` throws `OutOfMemoryException`. Mitigations in place: `mem_limit` 2g + nightly `prowlarr-history-prune.sh` (keep 2 days + VACUUM).
