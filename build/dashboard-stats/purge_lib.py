"""Dead-torrent detection and purge, shared by the web UI and CLI.

Mirrors the manual purge performed on 2026-07-19: identify torrents that are
stalled/seedless/stuck, remove them through Sonarr/Radarr (so the app blocklists
that specific release and searches for a replacement), and delete anything the
*arrs don't know about directly from qBittorrent.

Nothing is removed unless the caller explicitly asks -- analyze() is read-only.
"""

import os
import time
import json
import sqlite3
import requests

QBIT_URL = os.environ.get("QBIT_URL", "http://gluetun:8080")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
QBIT_USER = os.environ.get("QBIT_USER", "mediauser")
QBIT_PASS = os.environ.get("QBIT_PASS", "")

STALE_HOURS = int(os.environ.get("STALE_HOURS", "24"))


def qbit_session():
    s = requests.Session()
    r = s.post(
        f"{QBIT_URL}/api/v2/auth/login",
        data={"username": QBIT_USER, "password": QBIT_PASS},
        headers={"Referer": QBIT_URL},
        timeout=15,
    )
    # qBittorrent answers 200 *or* 204 on success; only the body "Fails." means bad creds
    if r.status_code not in (200, 204) or "Fails" in r.text:
        raise RuntimeError(f"qBittorrent login failed ({r.status_code})")
    return s


# A torrent can only finish if a *complete copy* is reachable. qBittorrent reports
# that as `availability` (1.0 == one full copy among connected peers). `num_complete`
# is what the TRACKER claims exists and is frequently wrong/stale -- an early version
# of this keyed off num_complete and missed hundreds of permanently-stuck torrents
# that had "seeds" on paper but 0 connected and 0 B/s for months. Use num_seeds
# (actually connected) and availability instead.
GRINDING_DAYS = int(os.environ.get("GRINDING_DAYS", "14"))
# Not `dlspeed == 0`: dead torrents commonly trickle at a few BYTES/sec (seen: 35 B/s,
# which the UI rounds to "0.0 KB/s"). Anything under this is stalled for practical purposes.
STALL_BPS = int(os.environ.get("STALL_BPS", "1024"))  # 1 KB/s


def classify(torrents):
    """Split torrents into dead / healthy. Read-only."""
    now = time.time()
    dead = []
    for t in torrents:
        prog = t.get("progress", 0)
        if prog >= 1:
            continue  # complete; leave alone
        age_h = (now - t.get("added_on", now)) / 3600
        if age_h < STALE_HOURS:
            continue  # too new to judge

        state = t.get("state", "")
        dlspeed = t.get("dlspeed", 0)
        conn_seeds = t.get("num_seeds", 0)          # seeds actually CONNECTED
        avail = t.get("availability", 0) or 0        # copies of the file reachable
        active_days = t.get("time_active", 0) / 86400

        stalled = dlspeed < STALL_BPS
        reason = None
        if state == "metaDL":
            reason = "stuck fetching metadata"
        elif stalled and avail < 1.0:
            reason = f"incomplete copy only ({avail:.2f} avail)"
        elif stalled and conn_seeds == 0:
            reason = "no seeds connected"
        elif stalled and active_days >= GRINDING_DAYS:
            reason = f"stalled {int(active_days)}d at {prog*100:.0f}%"

        if reason:
            dead.append({
                "hash": t["hash"],
                "name": t.get("name", "")[:90],
                "state": state,
                "progress": round(prog * 100, 1),
                "seeds": conn_seeds,
                "availability": round(avail, 2),
                "age_hours": round(age_h, 1),
                "active_days": round(active_days, 1),
                "downloaded_mb": round(t.get("downloaded", 0) / 1e6, 1),
                "reason": reason,
            })
    return dead


def arr_get(base, key, path, timeout=60):
    r = requests.get(f"{base}/api/v3{path}", headers={"X-Api-Key": key}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def arr_delete(base, key, path, timeout=120):
    r = requests.delete(f"{base}/api/v3{path}", headers={"X-Api-Key": key}, timeout=timeout)
    r.raise_for_status()
    return True


def analyze():
    s = qbit_session()
    torrents = s.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=60).json()
    dead = classify(torrents)
    return {
        "total_torrents": len(torrents),
        "dead_count": len(dead),
        "dead": dead,
        "reclaimable_mb": round(sum(d["downloaded_mb"] for d in dead), 1),
    }


def purge():
    """Remove dead torrents. Returns a result summary."""
    s = qbit_session()
    torrents = s.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=60).json()
    dead = classify(torrents)
    dead_hashes = {d["hash"].upper() for d in dead}
    if not dead_hashes:
        return {"removed_via_arr": 0, "removed_via_qbit": 0, "researched": 0,
                "dead_count": 0, "message": "Nothing stale found."}

    matched = set()
    removed_via_arr = 0
    for name, base, key in (
        ("sonarr", SONARR_URL, SONARR_API_KEY),
        ("radarr", RADARR_URL, RADARR_API_KEY),
    ):
        if not key:
            continue
        try:
            q = arr_get(base, key, "/queue?pageSize=10000&page=1")
        except Exception:
            continue
        recs = q.get("records", [])
        ids = [r["id"] for r in recs if str(r.get("downloadId", "")).upper() in dead_hashes]
        matched |= {str(r["downloadId"]).upper() for r in recs
                    if str(r.get("downloadId", "")).upper() in dead_hashes}
        # blocklist=true  -> never re-grab this exact release
        # skipRedownload=false -> immediately search for a REPLACEMENT (the "rescan")
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                arr_delete(base, key,
                           "/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false",
                           timeout=180)
                removed_via_arr += len(chunk)
            except Exception:
                for qid in chunk:
                    try:
                        arr_delete(base, key,
                                   f"/queue/{qid}?removeFromClient=true&blocklist=true&skipRedownload=false")
                        removed_via_arr += 1
                    except Exception:
                        pass

    # anything the *arrs never knew about: delete straight from qBittorrent, with data
    orphans = [h for h in dead_hashes if h not in matched]
    removed_via_qbit = 0
    for i in range(0, len(orphans), 50):
        chunk = orphans[i:i + 50]
        try:
            s.post(f"{QBIT_URL}/api/v2/torrents/delete",
                   data={"hashes": "|".join(h.lower() for h in chunk), "deleteFiles": "true"},
                   timeout=60)
            removed_via_qbit += len(chunk)
        except Exception:
            pass

    return {
        "dead_count": len(dead),
        "removed_via_arr": removed_via_arr,
        "removed_via_qbit": removed_via_qbit,
        "researched": removed_via_arr,
        "message": (f"Removed {removed_via_arr} via Sonarr/Radarr (blocklisted + replacement search "
                     f"triggered) and {removed_via_qbit} orphans direct from qBittorrent."),
    }


if __name__ == "__main__":
    import sys
    if "--purge" in sys.argv:
        print(json.dumps(purge(), indent=2))
    else:
        a = analyze()
        print(json.dumps({k: v for k, v in a.items() if k != "dead"}, indent=2))
        for d in a["dead"][:40]:
            print(f"  {d['state']:12s} {d['progress']:5.1f}% seeds={d['seeds']:3d} "
                  f"{d['age_hours']:7.1f}h  {d['reason']:22s} {d['name']}")
