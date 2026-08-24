# FentON Demand

Self-hosted media-automation stack for an Intel NUC (Ubuntu, 2-core i3, 7.6 GB RAM). Docker Compose orchestrating the *arr suite, download clients, custom indexer shims, and a torrent reaper.

## Architecture
- **Media servers:** Jellyfin, Plex
- **_arr_:** Sonarr (TV), Radarr (movies), Lidarr (music), Prowlarr (indexers), Bazarr (subtitles)
- **Download clients:** NZBGet (Usenet, direct), qBittorrent (torrents), slskd (Soulseek), AirDC++ (Direct Connect)
- **VPN:** gluetun (Privado/OpenVPN) — slskd & AirDC++ route through it; NZBGet runs direct
- **Requests / dashboard:** Jellyseerr, gethomepage

## Custom components (`build/`)
- **dashboard-stats/reaper.py** — torrent reaper: drops torrents that cannot reach 100%% (no seed in swarm) or that stall (no download speed for N minutes), then blocklists + triggers a replacement search. Env-tunable thresholds (REAP_*).
- **binsearch-shim / nzbking-shim** — Flask newznab shims scraping BinSearch / NZBKing into Prowlarr as Usenet indexers.
- **slskd-shim** — presents Soulseek (slskd) as a Torznab indexer to the *arrs.
- **slskd-bridge** — bridges Lidarr wanted albums into Soulseek.
- **dashboard-stats** — Homepage custom-API stats + a security monitor.

## Scripts (`scripts/`, cron-driven)
Paced backlog search, foreign-audio quarantine (ffprobe), Prowlarr history prune, failed-music cleanup, Tdarr off-hours window, and other maintenance jobs.

## Setup
1. `cp .env.example .env` and fill in real values (VPN creds, *arr API keys, client passwords).
2. `docker compose up -d`

_No credentials are committed. All secrets come from `.env`; see `.env.example`._
