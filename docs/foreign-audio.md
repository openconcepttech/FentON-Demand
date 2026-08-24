# Foreign‑audio handling & import mechanics

## The problem

The scraper indexers return releases whose **names don't reveal the audio language** (e.g. "South Park S28E02 SDTV" that is actually a French E‑AC3 file). Sonarr grabs them thinking they're English. A one‑time audit found ~319 foreign‑audio files (biggest cluster: King of the Hill = 228 Dutch episodes) which were deleted; English replacements were re‑fetched.

## Detection = ffprobe, not metadata

Sonarr's cached `audioLanguages` is unreliable (it showed the French file as untagged). **Ground truth is ffprobe** of each file's audio‑stream `language` tag. A file is "foreign" only if it has a real language tag (`fre`, `spa`, `ger`, `rus`, …) and **no** English track. Untagged (`und`/empty) and `cpe` (English‑creole docs) are treated as English and left alone.

## foreign-audio-reaper (`scripts/foreign-audio-reaper.sh`, cron `40 */6 * * *`)

- Incremental: ffprobes only files **newer** than a marker (`.foreign_reaper_last`) — fast.
- **Quarantines** (moves, not deletes) foreign‑only files to `/mnt/drive1/Mount/Videos/.foreign_quarantine/` — recoverable.
- Runs via `docker exec jellyfin` for ffprobe; deletes/moves on the host path.
- Caveat: quarantine → Sonarr sees the episode missing → backlog may re‑grab a foreign copy = churn (bounded to the 6 h cadence). Deeper fix is blocklisting the grab.

## Import mechanics on ntfs3 (important gotcha)

The USB drive **is** hardlink‑capable, but:

- Hardlinks **work within one bind mount** (`/data/Videos/_downloads/tv` → `/data/Videos/TV`, both under `/data`, same dev id).
- Hardlinks **fail across separate bind mounts** (`/downloads` vs `/tv`) with "Cross‑device link" — a Docker bind‑mount quirk, not an ntfs3 limit.

Consequences for **manual** imports via `/api/.../manualimport` + `command` `ManualImport`:

- Use the **`/data/…` path** (source + library root under the same `/data` mount) so it hardlinks; the `/downloads/…` path HANGS trying a cross‑mount copy.
- Manual `importMode:"copy"` silently imported nothing on this setup; only **`"move"`** worked (move breaks that torrent's seed — fine for replacements).
- A hung `started` command can't be `DELETE`d (409) — restart the *arr to clear it (queued command payloads are lost on restart, so re‑submit).

Sonarr's **automatic** import hardlinks fine (imported files show `nlink > 1`), so it preserves seeding at zero extra disk.
