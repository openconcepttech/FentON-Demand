#!/usr/bin/env python3
"""Give archived (reaped) torrents another chance once their swarm may have healed.

The reaper removes torrents whose swarm is dead and blocklists the release, so
the *arr will never pick it again. Public-tracker swarms do come back, though,
so this re-adds archived torrents after a cooling-off period and lets the normal
reaper judge them again: if still dead they are simply re-archived, and the
retry counter backs them off further each time.
"""
import os
import json
import time
import logging
import requests

import purge_lib

ARCHIVE_DIR = os.environ.get("REAP_ARCHIVE", "/data/reaper-archive")
ARCHIVE_INDEX = os.path.join(ARCHIVE_DIR, "index.json")
RETRY_BATCH = int(os.environ.get("RETRY_BATCH", "5"))
# wait this long after the last reap before the first retry; doubles each attempt
RETRY_AFTER_H = float(os.environ.get("RETRY_AFTER_HOURS", "72"))
MAX_RETRIES = int(os.environ.get("RETRY_MAX", "3"))
MAX_INCOMPLETE = int(os.environ.get("RETRY_MAX_INCOMPLETE", "18"))
# entries that used up their retries are dropped after this long, so the archive
# stays bounded (it grows ~57/hour while a full backlog search is running)
PRUNE_AFTER_DAYS = float(os.environ.get("RETRY_PRUNE_DAYS", "30"))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("retry")


def load():
    try:
        with open(ARCHIVE_INDEX) as f:
            return json.load(f)
    except Exception:
        return {}


def save(idx):
    tmp = ARCHIVE_INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f)
    os.replace(tmp, ARCHIVE_INDEX)


def due(rec, now):
    n = rec.get("retried", 0)
    if n >= MAX_RETRIES:
        return False
    # 72h, then 144h, then 288h - a swarm that stayed dead gets asked less often
    wait = RETRY_AFTER_H * (2 ** n) * 3600
    return (now - rec.get("last_reaped", 0)) > wait


def prune(idx, now):
    """Drop exhausted, stale entries and their .torrent files."""
    cutoff = PRUNE_AFTER_DAYS * 86400
    gone = []
    for h, r in list(idx.items()):
        exhausted = r.get("retried", 0) >= MAX_RETRIES
        stale = (now - r.get("last_reaped", 0)) > cutoff
        if exhausted and stale:
            tf = r.get("torrent_file") or ""
            if tf and os.path.exists(tf):
                try:
                    os.remove(tf)
                except Exception:
                    pass
            del idx[h]
            gone.append(h)
    if gone:
        log.info(f"pruned {len(gone)} exhausted entries older than "
                 f"{PRUNE_AFTER_DAYS:.0f}d")
    return len(gone)


def main():
    idx = load()
    if not idx:
        log.info("archive empty, nothing to retry")
        return

    now0 = time.time()
    if prune(idx, now0):
        save(idx)

    s = purge_lib.qbit_session()
    try:
        cur = s.get(f"{purge_lib.QBIT_URL}/api/v2/torrents/info", timeout=60).json()
    except Exception as e:
        log.warning(f"cannot reach qbit: {e}")
        return
    incomplete = sum(1 for t in cur if t.get("progress", 1) < 1)
    if incomplete >= MAX_INCOMPLETE:
        log.info(f"skip: {incomplete} incomplete downloads (limit {MAX_INCOMPLETE})")
        return
    present = {t["hash"].lower() for t in cur}

    now = time.time()
    cands = [r for h, r in idx.items()
             if h not in present and due(r, now)]
    cands.sort(key=lambda r: r.get("last_reaped", 0))
    cands = cands[:RETRY_BATCH]
    if not cands:
        log.info(f"nothing due for retry ({len(idx)} archived)")
        return

    added = 0
    for rec in cands:
        h = rec["hash"]
        ok = False
        tf = rec.get("torrent_file") or ""
        try:
            if tf and os.path.exists(tf):
                with open(tf, "rb") as f:
                    r = s.post(f"{purge_lib.QBIT_URL}/api/v2/torrents/add",
                               files={"torrents": (os.path.basename(tf), f,
                                                    "application/x-bittorrent")},
                               data={"category": rec.get("category") or "",
                                     "paused": "false"}, timeout=90)
            else:
                r = s.post(f"{purge_lib.QBIT_URL}/api/v2/torrents/add",
                           data={"urls": rec.get("magnet", ""),
                                 "category": rec.get("category") or "",
                                 "paused": "false"}, timeout=90)
            ok = r.status_code in (200, 204)
        except Exception as e:
            log.warning(f"retry add failed for {str(rec.get('name'))[:40]}: {e}")

        idx[h]["retried"] = rec.get("retried", 0) + 1
        idx[h]["last_retry"] = now
        if ok:
            added += 1
            log.info(f"retrying (attempt {idx[h]['retried']}/{MAX_RETRIES}): "
                     f"{str(rec.get('name'))[:60]}")

    save(idx)
    log.info(f"re-added {added} of {len(cands)} due | archive holds {len(idx)}")


if __name__ == "__main__":
    main()
