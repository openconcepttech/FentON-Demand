# Download clients

## NZBGet (Usenet) — the fast lane

- **Not behind the VPN** (Usenet is an authenticated SSL connection to the provider; the VPN adds no privacy and would throttle it under gluetun's CPU cap). Runs direct on `media_net`, real home IP.
- Providers (set in `nzbget.conf`):
  - **Server2 = Giganews** (`news.giganews.com`), Level 0 (**primary**), 40 connections.
  - **Server1 = usenet.farm** (`news.usenet.farm`), Level 1 (**fill** — only used for articles Giganews misses; important for no‑par2 single‑file NZBs).
- Raising Giganews connections (20 → 40) roughly tripled throughput (~3 → ~9 MB/s).
- `DupeCheck=yes` — rejects re‑adds of content already in history ("Skipping duplicate … exactly same content"). This once caused a grab/fail loop with a scraper shim; see [indexer-shims.md](indexer-shims.md).
- Control API: JSON‑RPC on `:6789`.

## qBittorrent (torrents)

- **Out of the VPN** as of 2026‑08‑24 (on the home IP, active mode via router UPnP). See [vpn-networking.md](vpn-networking.md) for the decision and the **`tun0` interface‑binding gotcha** (clear `current_network_interface` after moving, or it has zero connectivity).
- WebUI on host `:8080`; torrent port `42243`.
- Queue: `max_active_downloads=20`, `max_active_torrents=40`, `dont_count_slow_torrents=on`. No speed limit (`dl_limit=0`).
- **Share limits**: seed **24 h** then **remove torrent, keep files** (`max_seeding_time=1440`, `max_ratio=5`, `ratio_action=1`). This self‑cleans the completed queue. Imports are hardlinks, so removing the torrent doesn't touch the library copy. *arr apps warn "set to remove completed downloads" — benign, since import happens long before the 24 h removal.
- Connections: `max_connec=150`, `max_connec_per_torrent=30`. Trimming these lowers CPU/softirq at high throughput.
- Reachable from *arr/reaper/homepage as `qbittorrent:8080` (was `gluetun:8080` while behind the VPN — repoint ALL of them when moving it).

## slskd (Soulseek) — music

- **Behind the VPN.** Torznab‑style access via `slskd-shim` (`:8200`). Feeds Lidarr ([music-pipeline.md](music-pipeline.md)).
- Memory‑sensitive under heavy search load — `mem_limit` raised to **1 GB** (was OOM‑restart‑looping at 512 MB).

## AirDC++ (Direct Connect)

- **Behind the VPN.** Passive mode (Privado has no port forwarding), so search results route back through the hub connection. Auto‑detect connectivity is on so it adapts when the VPN IP rotates. Predominantly Russian/EE hubs (that's where DC still has activity) — weak channel for English content vs Usenet/torrents/Soulseek.
