# VPN & networking (gluetun)

`gluetun` runs **Privado** over OpenVPN (`VPN_SERVICE_PROVIDER: privado`, `SERVER_COUNTRIES: USA`). Credentials from `.env` (`PRIVADO_USER` / `PRIVADO_PASS`).

## What routes through it

- **Behind gluetun** (`network_mode: service:gluetun`): `slskd`, `airdcpp`. Their ports are published *on the gluetun container* (`5030-5031`, `5600-5601`, `21248-21249`, `50300`).
- **Direct (real home IP)**: `nzbget` (Usenet), `qbittorrent` (torrents, as of 2026‑08‑24).

## The Privado limitation: no port forwarding

Privado (like VyprVPN) does **not** support port forwarding. Behind it, torrent/DC clients are **passive** — unconnectable — so they reach only a fraction of available peers. Symptoms: torrents stuck in `metaDL`, 0 B/s despite seeds existing, only a few transferring at once. **The real fix for fast P2P is a port‑forwarding VPN** (PIA / ProtonVPN / AirVPN).

## qBittorrent VPN history

- Originally behind gluetun. Moved to the home IP once, but the **ISP sent infringement notices**, so it was put back inside on 2026‑08‑16 (documented in the compose comment).
- **2026‑08‑24: moved OUT again** by explicit user decision, accepting the home‑IP exposure, because behind the passive tunnel only ~2 torrents transferred at once. Off‑VPN it immediately reached 5+ transferring and 70+ Mbps aggregate.

### Moving qBittorrent in/out of the VPN — checklist

1. Compose: swap `network_mode: service:gluetun` + `depends_on: gluetun` for its own `ports:` (8080, 42243) + `networks: - media_net`; remove those port publishes from gluetun (or add them back when moving in).
2. Repoint **every** consumer of `gluetun:8080` → `qbittorrent:8080`: **Sonarr, Radarr, Lidarr** download clients, the `torrent-reaper` `QBIT_URL`, and the **Homepage** widget URL.
3. **Clear qBittorrent's `current_network_interface`** (it's bound to `tun0` for leak protection while on the VPN). Off‑VPN, `tun0` doesn't exist → `connection_status=disconnected, dht=0` until you clear it via `setPreferences {"current_network_interface":""}`. When moving back IN, set it to `tun0`.
4. Recreate `gluetun slskd airdcpp qbittorrent torrent-reaper`.

## CPU note

Off‑VPN, qBittorrent's traffic no longer costs gluetun's OpenVPN encryption, but hash‑verifying at 70+ Mbps plus hundreds of peer connections (softirq) still loads the 2‑core box. See [tuning.md](tuning.md).
