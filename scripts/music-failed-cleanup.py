#!/usr/bin/env python3
"""Remove Lidarr queue items whose import FAILED (partial/mismatched music), and
blocklist them so soularr/Lidarr retries a DIFFERENT copy instead of re-grabbing
the same junk. Keeps the wide-net Soulseek pipeline from piling up dead files."""
import json, re, urllib.request, urllib.parse
K = re.search(r"<ApiKey>([^<]+)", open("/opt/media/config/lidarr/config.xml").read()).group(1)
BASE = "http://127.0.0.1:8686/api/v1"
def api(path, method="GET"):
    r = urllib.request.Request(BASE + path, headers={"X-Api-Key": K}, method=method)
    with urllib.request.urlopen(r, timeout=60) as x:
        b = x.read().decode(); return json.loads(b) if b.strip() else None
q = api("/queue?pageSize=200&includeUnknownArtistItems=true")
failed = [r for r in q.get("records", []) if r.get("trackedDownloadState") == "importFailed"]
n = 0
for r in failed:
    try:
        api("/queue/%d?removeFromClient=true&blocklist=true&skipRedownload=false" % r["id"], "DELETE")
        n += 1
    except Exception as e:
        print("skip", r.get("title","")[:40], e)
print("cleared+blocklisted %d failed music imports (of %d in queue)" % (n, q.get("totalRecords")))
