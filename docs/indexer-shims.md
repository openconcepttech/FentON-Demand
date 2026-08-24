# Custom indexer shims

Three custom Flask/waitress services present non‑standard sources to Prowlarr/*arr as if they were normal newznab/Torznab indexers. Built from `build/*-shim/`.

## binsearch-shim (`:8300`)

- Scrapes **binsearch.info** and exposes a **newznab** API (`/api?t=caps|search|tvsearch`, `/getnzb`, `/stats`, `/healthz`).
- Relevance filtering: token‑boundary matching (`_relevant`) + a "release‑yness" filter so it doesn't return obfuscated/junk posts; `RSS_TERMS` for empty (RSS) queries.
- **Known pitfall — grab/fail loop:** the shim can hand back the *same* NZB content that NZBGet already has in history. NZBGet instant‑rejects it as a duplicate, the *arr counts it as a failed download, and with `autoRedownloadFailed=True` it immediately re‑grabs → a tight loop. **Fix applied:** set Sonarr/Radarr `autoRedownloadFailed=False` (failures blocklist and are retried by the paced backlog instead of instantly). Only re‑enable BinSearch with that in place.
- Registered in Prowlarr as a Usenet indexer.

## nzbking-shim (`:8301`)

- Scrapes **NZBKing**, richer parsing than BinSearch: completeness (`parts N/M`, `MIN_COMPLETE≈0.95`), password (`NO PASSWORD` required), filetype (video required). NZB fetched from `/nzb:<hex>/`.

## slskd-shim (`:8200`)

- Presents **Soulseek** (via the `slskd` daemon) as a **Torznab** indexer, so Sonarr/Radarr/Lidarr can search Soulseek like any tracker. Music category `lidarr-slskd`. Part of the music pipeline ([music-pipeline.md](music-pipeline.md)).

## General notes

- These are scrapers — coverage is patchy, especially for **old catalogue** (a big backfill leans heavily on torrents because the free Usenet indexers don't index old content well). A paid Usenet indexer (NZBGeek, DrunkenSlug, etc.) would give real Usenet coverage.
- Prowlarr syncs enabled/disabled state to the *arrs. To disable one everywhere, disable it in **Prowlarr** (`/api/v1/indexer`) *and* the *arr copy.
