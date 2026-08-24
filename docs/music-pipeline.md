# Music pipeline (Lidarr + soularr + Soulseek)

Music is the trickiest part of the stack because Soulseek shares are messy and Lidarr's album matching is strict.

## Components

- **Lidarr** — library + wanted list (~22 artists). Quality/metadata profiles as usual.
- **soularr** (`mrusse08/soularr`, config `/opt/media/config/soularr/config.ini`, web `:8265`) — reads Lidarr's *missing* albums and searches **Soulseek** via `slskd`, downloading matches.
- **slskd** — the Soulseek daemon (behind the VPN). Also exposed to the *arrs as an indexer via `slskd-shim`.
- **slskd-bridge** — helper bridging slskd ↔ Lidarr.

> If music "isn't doing anything", check that **soularr** and **slskd-bridge** are running — `restart: unless-stopped` will NOT restart a container that was deliberately stopped.

## Why imports fail (and it's not a path bug)

Downloads land in `/data/_soulseek_downloads` (Soulseek) or `/downloads/usenet/music` (NZBGet) — both mounted in Lidarr. Failures are Lidarr's **album matching** rejecting:

- **Incomplete albums** — "has missing tracks" (Soulseek users share partial folders).
- **Wrong version** — "album match 73.6 % vs 80 %".
- **Wrong content** — e.g. a comedy album matched to a music artist.

This is inherent Lidarr‑vs‑real‑world friction; it will never be 100 %.

## Tuning (chosen setup)

- `soularr` **`minimum_filename_match_ratio = 0.8`** — a *wide net* (tried 0.9, reverted). More attempts = more chances to land a good copy.
- **`scripts/music-failed-cleanup.py`** (cron `25 */3 * * *`) — deletes Lidarr queue items whose `trackedDownloadState==importFailed` with **`blocklist=true`**. The blocklist is key: it stops re‑grabbing the *same* bad copy, so soularr's next pass tries a **different** copy → cycles toward an importable one without piling up dead files.
- `slskd` `mem_limit` raised to **1 GB**, `lidarr` to **768 MB** (the wide net was OOM‑restarting slskd at 512 MB).

Net: over days it works through Soulseek's copies of the wanted albums, landing the ones that *can* be landed.
