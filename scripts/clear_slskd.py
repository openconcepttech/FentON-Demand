#!/usr/bin/env python3
"""Clear failed/stale completed transfers out of slskd's download list.

Soulseek peers routinely reject / time out / go offline, and slskd keeps every
such transfer in its list forever. This removes the ones that are DONE and have
no file to import (Rejected/Errored/Cancelled/TimedOut). It deliberately leaves
'Completed, Succeeded' (may still be pending import by the *arr), plus anything
InProgress or Queued.
"""
import os
import sys
import urllib.parse
import requests

BASE = os.environ.get("SLSKD_URL", "http://gluetun:5030")
KEY = os.environ["SLSKD_API_KEY"]
H = {"X-API-Key": KEY}
DRY = "--go" not in sys.argv

# Failed/terminal states worth pruning (substring match on state string).
CLEAR = ("Rejected", "Errored", "Cancelled", "TimedOut")

r = requests.get(BASE + "/api/v0/transfers/downloads", headers=H, timeout=60)
r.raise_for_status()
data = r.json()

targets = []  # (username, id, state, filename)
for user in data:
    uname = user.get("username")
    for d in user.get("directories", []):
        for f in d.get("files", []):
            st = f.get("state", "")
            if st.startswith("Completed") and any(c in st for c in CLEAR):
                targets.append((uname, f.get("id"), st, f.get("filename", "")))

print("failed/terminal transfers to remove: %d" % len(targets))
from collections import Counter
print("by state:", dict(Counter(t[2] for t in targets)))

if DRY:
    print("\nDRY RUN — pass --go to delete")
    sys.exit(0)

ok = fail = 0
sess = requests.Session()
sess.headers.update(H)
for uname, fid, st, fn in targets:
    try:
        # slskd needs ?remove=true to drop a COMPLETED transfer from the list;
        # a plain DELETE only cancels an active one (no-op once completed).
        uq = urllib.parse.quote(uname, safe="")
        resp = sess.delete("%s/api/v0/transfers/downloads/%s/%s?remove=true"
                           % (BASE, uq, fid), timeout=30)
        if resp.status_code in (200, 204):
            ok += 1
        else:
            fail += 1
    except Exception:
        fail += 1
print("removed=%d failed=%d" % (ok, fail))
