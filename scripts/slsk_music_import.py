#!/usr/bin/env python3
"""Import the Soulseek music backlog into Lidarr.

Lidarr's bulk folder scan matches nothing here because the download folders are
named after the ALBUM only ("ICEMAN (2026)"), so it cannot infer the artist.
Pinning artistId per folder makes it match cleanly -- the artist comes from the
files' own tags, which is more reliable than parsing the folder name.
"""
import json, os, re, sys, time, urllib.parse, requests

API = "http://localhost:8686/api/v1"
KEY = re.search(r"<ApiKey>([^<]+)", open("/opt/media/config/lidarr/config.xml").read()).group(1)
H = {"X-Api-Key": KEY}
ARR_ROOT = "/data/_soulseek_downloads"
DRY = "--go" not in sys.argv


def norm(s):
    s = s.lower()
    s = re.split(r"\s*[;&]\s*| and ", s)[0]
    s = re.sub(r"^(dj)\s+", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


artists = requests.get(f"{API}/artist", headers=H, timeout=120).json()
by_norm = {norm(a["artistName"]): a for a in artists}
rows = json.load(open("/tmp/albums.json"))

stats = {"folders": 0, "no_artist": 0, "no_match": 0, "queued": 0, "tracks": 0, "skipped": 0}
plan = []
for r in rows:
    stats["folders"] += 1
    a = by_norm.get(norm(r["artist"]))
    if not a:
        stats["no_artist"] += 1
        print(f"  SKIP (artist not in Lidarr): {r['artist']} :: {r['folder']}")
        continue
    folder = os.path.join(ARR_ROOT, r["folder"])
    try:
        mi = requests.get(f"{API}/manualimport", headers=H, timeout=300, params={
            "folder": folder, "artistId": a["id"], "filterExistingFiles": "true"}).json()
    except Exception as e:
        print(f"  ERR lookup {r['folder']}: {e}")
        continue
    good = [x for x in mi
            if x.get("album") and x.get("tracks") and not x.get("rejections")]
    if not good:
        stats["no_match"] += 1
        why = set()
        for x in mi:
            for j in x.get("rejections", []):
                why.add(str(j.get("reason"))[:50])
        print(f"  NOMATCH {r['folder'][:48]:48} ({len(mi)} files) {sorted(why)[:2]}")
        continue
    files = [{
        "path": x["path"],
        "artistId": a["id"],
        "albumId": x["album"]["id"],
        "albumReleaseId": (x.get("albumReleaseId")
                           or (x.get("album", {}).get("currentRelease") or {}).get("id")),
        "trackIds": [t["id"] for t in x["tracks"]],
        "quality": x["quality"],
        "disableReleaseSwitching": False,
    } for x in good]
    album_title = good[0]["album"]["title"]
    plan.append((r["folder"], a["artistName"], album_title, files))
    stats["queued"] += 1
    stats["tracks"] += len(files)
    print(f"  OK  {a['artistName'][:18]:18} {album_title[:28]:28} {len(files):2d} tracks  <- {r['folder'][:34]}")

print("\n  === summary ===")
for k, v in stats.items():
    print(f"    {k:12} {v}")

if DRY:
    print("\n  DRY RUN -- nothing moved. re-run with --go to import.")
    json.dump([[p[0], p[1], p[2], len(p[3])] for p in plan], open("/tmp/import_plan.json", "w"))
    sys.exit()

done = 0
for folder, artist, album, files in plan:
    body = {"name": "ManualImport", "importMode": "move", "files": files}
    try:
        cr = requests.post(f"{API}/command", headers=H, json=body, timeout=180)
        if cr.status_code >= 300:
            print(f"  FAIL {folder[:40]}: {cr.status_code} {cr.text[:100]}")
            continue
        done += 1
        print(f"  IMPORT {artist[:16]:16} {album[:26]:26} ({len(files)} tracks)")
    except Exception as e:
        print(f"  FAIL {folder[:40]}: {e}")
    time.sleep(2)
print(f"\n  submitted {done}/{len(plan)} album imports")
