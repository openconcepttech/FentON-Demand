#!/usr/bin/env python3
"""Unmonitor anything that is fully downloaded, across Sonarr/Radarr/Lidarr.

Season completeness is measured against totalEpisodeCount (every episode the
season will ever have) rather than episodeCount (aired only). A currently-airing
season is 100% of *aired* episodes the moment you catch up, and unmonitoring it
then would silently stop future episodes from ever being grabbed.
"""
import json, re, sys, requests

GO = "--go" in sys.argv


def key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"/opt/media/config/{app}/config.xml").read()).group(1)


def api(app, port, ver):
    return f"http://localhost:{port}/api/{ver}", {"X-Api-Key": key(app)}


# ------------------------------------------------------------------ sonarr --
def sonarr():
    base, h = api("sonarr", 8989, "v3")
    series = requests.get(f"{base}/series", headers=h, timeout=180).json()
    changed_seasons = changed_series = 0
    for s in series:
        dirty = False
        for sea in s.get("seasons", []):
            if sea.get("seasonNumber", 0) == 0:          # leave specials alone
                continue
            st = sea.get("statistics") or {}
            total = st.get("totalEpisodeCount", 0)
            have = st.get("episodeFileCount", 0)
            if sea.get("monitored") and total > 0 and have >= total:
                sea["monitored"] = False
                changed_seasons += 1
                dirty = True
        real = [x for x in s.get("seasons", []) if x.get("seasonNumber", 0) != 0]
        if real and not any(x.get("monitored") for x in real) and s.get("monitored"):
            s["monitored"] = False
            changed_series += 1
            dirty = True
        if dirty and GO:
            r = requests.put(f"{base}/series/{s['id']}", headers=h, json=s, timeout=120)
            if r.status_code >= 300:
                print(f"  sonarr FAIL {s['title'][:40]}: {r.status_code}")
    return changed_seasons, changed_series


# ------------------------------------------------------------------ radarr --
def radarr():
    base, h = api("radarr", 7878, "v3")
    movies = requests.get(f"{base}/movie", headers=h, timeout=180).json()
    ids = [m["id"] for m in movies if m.get("hasFile") and m.get("monitored")]
    if ids and GO:
        for i in range(0, len(ids), 100):
            r = requests.put(f"{base}/movie/editor", headers=h, timeout=180,
                             json={"movieIds": ids[i:i + 100], "monitored": False})
            if r.status_code >= 300:
                print(f"  radarr FAIL: {r.status_code} {r.text[:120]}")
    return len(ids)


# ------------------------------------------------------------------ lidarr --
def lidarr():
    base, h = api("lidarr", 8686, "v1")
    artists = requests.get(f"{base}/artist", headers=h, timeout=180).json()
    ids = []
    for a in artists:
        albums = requests.get(f"{base}/album", headers=h, timeout=180,
                              params={"artistId": a["id"]}).json()
        for al in albums:
            st = al.get("statistics") or {}
            tc, tf = st.get("trackCount", 0), st.get("trackFileCount", 0)
            if al.get("monitored") and tc > 0 and tf >= tc:
                ids.append(al["id"])
    if ids and GO:
        for i in range(0, len(ids), 100):
            r = requests.put(f"{base}/album/monitor", headers=h, timeout=180,
                             json={"albumIds": ids[i:i + 100], "monitored": False})
            if r.status_code >= 300:
                print(f"  lidarr FAIL: {r.status_code} {r.text[:120]}")
    return len(ids)


ss, sr = sonarr()
mv = radarr()
al = lidarr()
print(f"  sonarr : {ss} seasons + {sr} series fully downloaded")
print(f"  radarr : {mv} movies with files")
print(f"  lidarr : {al} albums with all tracks")
print(f"\n  {'APPLIED' if GO else 'DRY RUN - nothing changed; re-run with --go'}")
