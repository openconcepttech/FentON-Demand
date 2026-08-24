#!/usr/bin/env python3
"""Continuous torrent reaper.

Dead grabs from public trackers reveal themselves within minutes: a magnet that
finds no peers is not going to find them in six hours. Because qBittorrent's
active slots are finite, those corpses starve healthy torrents that are sitting
in queuedDL behind them -- observed live: 26 dead metaDL squatting every slot
while a control torrent that WAS given a slot pulled 73 MB/s.

So: reap aggressively, but only on evidence of deadness, and always hand the
removal to Sonarr/Radarr/Lidarr with blocklist=true + skipRedownload=false so
the same dead release is never re-grabbed and a replacement search fires
immediately.
"""

import os
import time
import json
import logging
import requests

import purge_lib

# --- thresholds (minutes) ----------------------------------------------------
# no metadata + no peers: a magnet with a live swarm resolves in seconds
META_MIN = float(os.environ.get("REAP_META_MIN", "25"))
# has metadata, zero progress, no connected seeds
NOSEED_MIN = float(os.environ.get("REAP_NOSEED_MIN", "60"))
# partially downloaded but no seeds and not moving (be gentler: it may resume)
PARTIAL_MIN = float(os.environ.get("REAP_PARTIAL_MIN", "360"))
INTERVAL = float(os.environ.get("REAP_INTERVAL", "300"))
STALL_BPS = int(os.environ.get("REAP_STALL_BPS", "1024"))
STALL_END_MIN = float(os.environ.get("REAP_STALL_END_MIN", "3"))
_dl_stall = {}   # hash -> ts when download first stalled; persists across reap loops
# Reap torrents that ARE running but crawl. 350 kbps = 43,750 B/s by the literal
# reading; set REAP_SLOW_KBPS=2800 if you meant 350 kB/s.
SLOW_BPS = int(float(os.environ.get("REAP_SLOW_KBPS", "40")) * 1000 / 8)
# ...but only after it has had a fair run. A torrent sitting in queuedDL reports
# 0 B/s simply because qBittorrent has not started it (215 of 241 were in that
# state), so judging on speed alone would kill things that never got a turn.
SLOW_MIN = float(os.environ.get("REAP_SLOW_MIN", "45"))
ACTIVE_STATES = ("downloading", "forcedDL", "stalledDL")
DRY_RUN = os.environ.get("REAP_DRY_RUN", "false").lower() == "true"
# Forcing bypasses max_active_downloads, so promoting every seeded torrent put
# 100+ downloads in flight at once, splitting bandwidth and peer slots across
# mostly-dying swarms. Cap it and spend the slots on the healthiest ones.
PROMOTE_MAX = int(os.environ.get("REAP_PROMOTE_MAX", "10"))
ARCHIVE_DIR = os.environ.get("REAP_ARCHIVE", "/data/reaper-archive")
ARCHIVE_INDEX = os.path.join(ARCHIVE_DIR, "index.json")
LOG_PATH = os.environ.get("REAP_LOG", "/data/reaper.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("reaper")


def classify_dead(torrents):
    """Reap: (a) no seed in swarm can finish it, or (b) download stalled too long."""
    now = time.time()
    dead = []
    present = set()
    for t in torrents:
        h = t["hash"]
        if t.get("progress", 0) >= 1:
            _dl_stall.pop(h, None)
            continue
        present.add(h)
        age_min = (now - t.get("added_on", now)) / 60
        state = t.get("state", "")
        peers = t.get("num_leechs", 0)
        dl = t.get("dlspeed", 0)
        prog = t.get("progress", 0)
        comp = t.get("num_complete", 0)

        if "metaDL" not in state and dl < STALL_BPS:
            _dl_stall.setdefault(h, now)
        else:
            _dl_stall.pop(h, None)
        stall_min = (now - _dl_stall[h]) / 60 if h in _dl_stall else 0

        reason = None
        if comp == 0 and age_min >= (META_MIN if "metaDL" in state else NOSEED_MIN):
            reason = (f"no seed in swarm ({prog*100:.0f}%, peers={peers}) "
                      f"after {int(age_min)}m")
        elif stall_min >= STALL_END_MIN:
            reason = (f"no download for {int(stall_min)}m "
                      f"({prog*100:.0f}%, seeds={comp})")
        if reason:
            dead.append({"hash": h, "name": t.get("name", "")[:70],
                          "state": state, "reason": reason})
    for h in list(_dl_stall):
        if h not in present:
            _dl_stall.pop(h, None)
    return dead


def promote(s, torrents):
    """Pin proven-live torrents so the queue can't starve them; un-pin finished ones.

    A queuedDL torrent reports num_seeds/num_complete/availability all as 0 -- it
    never announces while queued -- so "is it alive?" is unanswerable until it
    gets a slot. What we CAN do is make sure that once a torrent has proven it
    has seeds, it keeps running instead of being rotated back into the queue.

    The un-pin half matters just as much: a force-started torrent ignores share
    limits, so if we left completed ones forced they would seed forever and the
    *arrs would never remove them (this exact mistake stalled cleanup before).
    """
    to_force, to_unforce = [], []
    for t in torrents:
        forced = t.get("state", "").startswith("forced")
        complete = t.get("progress", 0) >= 1
        seeds = t.get("num_seeds", 0)
        if complete and forced:
            to_unforce.append(t["hash"])                    # let share limits apply again
        elif not complete and not forced and seeds >= 1:
            to_force.append((t["hash"], t.get("name", "")[:52], seeds))

    # Best swarms first, and only up to the cap: a torrent with 40 seeds earns a
    # slot far more than one clinging to a single seed. Already-forced torrents
    # count against the budget so we don't creep past it cycle after cycle.
    already = sum(1 for t in torrents
                  if t.get("state", "").startswith("forced")
                  and t.get("progress", 0) < 1)
    budget = max(0, PROMOTE_MAX - already)
    to_force.sort(key=lambda x: -x[2])
    skipped = len(to_force) - budget
    if skipped > 0:
        to_force = to_force[:budget]

    for i in range(0, len(to_force), 40):
        chunk = to_force[i:i + 40]
        try:
            s.post(f"{purge_lib.QBIT_URL}/api/v2/torrents/setForceStart",
                   data={"hashes": "|".join(h for h, _, _ in chunk), "value": "true"},
                   timeout=60)
        except Exception as e:
            log.warning(f"force-start failed: {e}")
    for i in range(0, len(to_unforce), 40):
        chunk = to_unforce[i:i + 40]
        try:
            s.post(f"{purge_lib.QBIT_URL}/api/v2/torrents/setForceStart",
                   data={"hashes": "|".join(chunk), "value": "false"}, timeout=60)
        except Exception as e:
            log.warning(f"un-force failed: {e}")

    if skipped > 0:
        log.info(f"promotion capped: {already} already forced, {len(to_force)} added, "
                 f"{skipped} left queued (limit {PROMOTE_MAX})")
    if to_force:
        log.info(f"promoted {len(to_force)} live torrent(s) past the queue:")
        for _, name, seeds in to_force[:6]:
            log.info(f"   seeds={seeds:<4d} {name}")
    if to_unforce:
        log.info(f"un-pinned {len(to_unforce)} finished torrent(s) so seeding limits apply")
    return len(to_force), len(to_unforce)


def _load_archive():
    try:
        with open(ARCHIVE_INDEX) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_archive(idx):
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        tmp = ARCHIVE_INDEX + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f)
        os.replace(tmp, ARCHIVE_INDEX)
    except Exception as e:
        log.warning(f"archive index save failed: {e}")


def archive_dead(s, dead):
    """Keep a copy of every torrent before it is destroyed.

    A swarm with no seeds today may have seeds next month, but removal here goes
    through the *arr with blocklist=true, which means the release is never
    considered again. Exporting the real .torrent (rather than just the magnet)
    means a retry needs no re-search and no metadata round trip.
    """
    idx = _load_archive()
    saved = 0
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
    except Exception as e:
        log.warning(f"cannot create archive dir: {e}")
        return 0

    for d in dead:
        h = d["hash"].lower()
        prev = idx.get(h, {})
        path = os.path.join(ARCHIVE_DIR, f"{h}.torrent")
        if not os.path.exists(path):
            try:
                r = s.get(f"{purge_lib.QBIT_URL}/api/v2/torrents/export",
                          params={"hash": h}, timeout=60)
                if r.status_code == 200 and r.content[:1] == b"d":
                    with open(path, "wb") as f:
                        f.write(r.content)
                else:
                    path = ""      # export unavailable; magnet below still works
            except Exception as e:
                log.warning(f"export failed for {d['name'][:40]}: {e}")
                path = ""

        idx[h] = {
            "hash": h,
            "name": d.get("name"),
            "category": d.get("category", ""),
            "size": d.get("size", 0),
            "progress": d.get("progress", 0),
            "state": d.get("state"),
            "reason": d.get("reason"),
            "magnet": f"magnet:?xt=urn:btih:{h}&dn=" + requests.utils.quote(str(d.get("name", ""))),
            "torrent_file": path,
            "first_reaped": prev.get("first_reaped") or time.time(),
            "last_reaped": time.time(),
            "reap_count": prev.get("reap_count", 0) + 1,
            "retried": prev.get("retried", 0),
        }
        saved += 1

    _save_archive(idx)
    log.info(f"archived {saved} torrent(s) before removal "
             f"({len(idx)} total in {ARCHIVE_DIR})")
    return saved


def reap():
    s = purge_lib.qbit_session()
    torrents = s.get(f"{purge_lib.QBIT_URL}/api/v2/torrents/info", timeout=90).json()
    promote(s, torrents)
    dead = classify_dead(torrents)

    live = [t for t in torrents if t.get("dlspeed", 0) > STALL_BPS]
    total_speed = sum(t.get("dlspeed", 0) for t in torrents) / 1e6
    if not dead:
        log.info(f"nothing to reap | {len(torrents)} torrents, {len(live)} moving, "
                 f"{total_speed:.2f} MB/s")
        return 0

    log.info(f"reaping {len(dead)} dead of {len(torrents)} "
             f"({len(live)} moving, {total_speed:.2f} MB/s)")
    for d in dead[:10]:
        log.info(f"   {d['state']:14s} {d['reason']:38s} {d['name']}")
    if DRY_RUN:
        log.info("DRY_RUN set - nothing removed")
        return 0

    archive_dead(s, dead)

    dead_hashes = {d["hash"].upper() for d in dead}
    matched = set()
    removed_arr = 0
    for name, base, key in (("sonarr", purge_lib.SONARR_URL, purge_lib.SONARR_API_KEY),
                             ("radarr", purge_lib.RADARR_URL, purge_lib.RADARR_API_KEY)):
        if not key:
            continue
        try:
            q = purge_lib.arr_get(base, key, "/queue?pageSize=10000&page=1")
        except Exception as e:
            log.warning(f"{name} queue fetch failed: {e}")
            continue
        recs = q.get("records", [])
        ids = [r["id"] for r in recs if str(r.get("downloadId", "")).upper() in dead_hashes]
        matched |= {str(r["downloadId"]).upper() for r in recs
                    if str(r.get("downloadId", "")).upper() in dead_hashes}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                purge_lib.arr_delete(
                    base, key,
                    "/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false",
                    timeout=180)
                removed_arr += len(chunk)
            except Exception:
                for qid in chunk:
                    try:
                        purge_lib.arr_delete(
                            base, key,
                            f"/queue/{qid}?removeFromClient=true&blocklist=true&skipRedownload=false")
                        removed_arr += 1
                    except Exception:
                        pass

    orphans = [h for h in dead_hashes if h not in matched]
    removed_qbit = 0
    for i in range(0, len(orphans), 50):
        chunk = orphans[i:i + 50]
        try:
            s.post(f"{purge_lib.QBIT_URL}/api/v2/torrents/delete",
                   data={"hashes": "|".join(h.lower() for h in chunk),
                          "deleteFiles": "true"}, timeout=60)
            removed_qbit += len(chunk)
        except Exception as e:
            log.warning(f"qbit delete failed: {e}")

    log.info(f"reaped: {removed_arr} via *arr (blocklisted + replacement search), "
             f"{removed_qbit} orphans direct")
    return removed_arr + removed_qbit


def main():
    log.info(f"reaper starting | meta>{META_MIN}m noseed>{NOSEED_MIN}m "
             f"partial>{PARTIAL_MIN}m slow<{SLOW_BPS/1024:.0f}kB/s after {SLOW_MIN}m "
             f"every {INTERVAL}s dry_run={DRY_RUN}")
    while True:
        try:
            reap()
        except Exception as e:
            log.exception(f"reap cycle failed: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
