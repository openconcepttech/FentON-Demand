# FentON Demand — Documentation

Per‑component docs for the stack. Start with the architecture, then dive into whichever piece you're working on.

- **[architecture.md](architecture.md)** — hardware, container map, network topology, storage, data flow
- **[reaper.md](reaper.md)** — the custom torrent‑reaper: reap rules, thresholds, promote, replacement search
- **[download-clients.md](download-clients.md)** — NZBGet, qBittorrent, slskd, AirDC++
- **[vpn-networking.md](vpn-networking.md)** — gluetun/Privado, what's behind the VPN, the qBittorrent in/out checklist
- **[indexer-shims.md](indexer-shims.md)** — BinSearch / NZBKing / slskd shims and the grab‑loop pitfall
- **[music-pipeline.md](music-pipeline.md)** — Lidarr + soularr + Soulseek, import‑match friction, failed‑cleanup cron
- **[foreign-audio.md](foreign-audio.md)** — ffprobe language audit/quarantine + ntfs3 hardlink/import gotchas
- **[maintenance-crons.md](maintenance-crons.md)** — every scheduled job and what it does
- **[tuning.md](tuning.md)** — the 2‑core reality: container limits, what to shed under load, Prowlarr history bloat
