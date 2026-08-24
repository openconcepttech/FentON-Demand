# Architecture

FentON Demand runs on an **Intel NUC** — Ubuntu 24.04, Intel i3‑7100U (**2 cores / 4 threads**), **7.6 GB RAM**. Everything is Docker Compose (`docker-compose.yml`); app configs live under `/opt/media/config/<app>`, custom images build from `/opt/media/build/`, cron jobs from `/opt/media/scripts/`.

The 2‑core CPU is the defining constraint — see [tuning.md](tuning.md).

## Containers

| Role | Containers |
|---|---|
| Media servers | `jellyfin`, `plex` |
| *arr suite | `sonarr` (TV), `radarr` (movies), `lidarr` (music), `prowlarr` (indexers), `bazarr` (subtitles) |
| Download clients | `nzbget` (Usenet), `qbittorrent` (torrents), `slskd` (Soulseek), `airdcpp` (Direct Connect) |
| VPN | `gluetun` (Privado / OpenVPN) |
| Custom | `torrent-reaper`, `binsearch-shim`, `nzbking-shim`, `slskd-shim`, `slskd-bridge`, `soularr`, `dashboard-stats` |
| Requests / UI | `jellyseerr`, `homepage`, `homepage-auth` |

## Network topology

- Docker network: **`compose_media_net`**.
- **Behind the VPN** (`network_mode: service:gluetun`): `slskd`, `airdcpp`. Their ports are published *on gluetun*.
- **NOT behind the VPN** (direct on `media_net`, real home IP): `nzbget` (Usenet is authenticated SSL, no VPN needed) and — as of 2026‑08‑24 — `qbittorrent` (moved out for connectivity; see [vpn-networking.md](vpn-networking.md)).
- Everything else (*arr, media servers) is on `media_net` and reaches clients by container name.

## Storage

- **`/mnt/drive1/Mount`** — 9.1 TB USB drive (ntfs3). Holds media (`Videos/TV`, `Videos/Music`) and download staging (`Videos/_downloads`). Slow random writes; **hardlinks work within a single bind mount but fail across separate bind mounts** (see [foreign-audio.md](foreign-audio.md)).
- **NVMe (228 GB)** — OS, container configs, transcode cache.

## Data flow (TV example)

1. Sonarr searches indexers via **Prowlarr** (real Usenet/torrent indexers + the custom shims).
2. Grabs go to **NZBGet** (Usenet, fast/direct) or **qBittorrent** (torrents).
3. **`torrent-reaper`** culls torrents that can't finish and triggers replacement searches ([reaper.md](reaper.md)).
4. On completion, Sonarr imports (hardlink where possible) into `/mnt/drive1/Mount/Videos/TV`.
5. Jellyfin/Plex serve it.
