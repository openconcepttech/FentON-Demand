#!/usr/bin/env python3
"""Work steadily through Sonarr/Radarr's missing backlog.

Neither app searches its back-catalogue on a schedule: RSS Sync only sees newly
posted releases, so 13k missing episodes sit untouched forever. A single
MissingEpisodeSearch over everything hammers Prowlarr (it pegged a core at 100%
earlier) and floods the queue, so this walks the backlog in small batches and
only when the download client has spare capacity.

State (the rotating offset) lives in /opt/media/config/backlog-search.state so
successive runs advance through the list instead of re-searching page 1 forever.
"""
import json, os, re, sys, time, urllib.parse, requests

BATCH        = int(os.environ.get("BACKLOG_BATCH", "60"))
MAX_INCOMPLETE = int(os.environ.get("BACKLOG_MAX_INCOMPLETE", "45"))
STATE        = "/opt/media/config/backlog-search.state"
# runs on the host via cron, where qbit is reached on the published port
QBIT         = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER    = os.environ.get("QBIT_USER", "mediauser")
QBIT_PASS    = os.environ.get("QBIT_PASS", "CHANGEME")
LOG          = "/opt/media/logs/backlog-search.log"


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def key(app):
    return re.search(r"<ApiKey>([^<]+)",
                     open(f"/opt/media/config/{app}/config.xml").read()).group(1)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    try:
        json.dump(s, open(STATE, "w"))
    except Exception as e:
        log(f"state save failed: {e}")


def qbit_incomplete():
    """Spare capacity check - never pile more on than the client can chew."""
    try:
        s = requests.Session()
        s.post(f"{QBIT}/api/v2/auth/login",
               data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=20)
        r = s.get(f"{QBIT}/api/v2/torrents/info", timeout=30)
        return sum(1 for x in r.json() if x.get("progress", 1) < 1)
    except Exception as e:
        log(f"qbit check failed ({e}); assuming busy")
        return 10 ** 6


def busy(base, h):
    """Another search already running? Don't stack them."""
    try:
        cmds = requests.get(f"{base}/command", headers=h, timeout=60).json()
        return any(c.get("status") in ("started", "queued")
                   and "Search" in str(c.get("name", "")) for c in cmds)
    except Exception:
        return True


def run(app, port, ver, cmd_name, id_field, wanted_path):
    base = f"http://localhost:{port}/api/{ver}"
    h = {"X-Api-Key": key(app)}
    st = load_state()

    if busy(base, h):
        log(f"{app}: a search is already running, skipping")
        return

    try:
        total = requests.get(f"{base}/{wanted_path}", headers=h, timeout=120,
                             params={"pageSize": 1, "monitored": "true"}).json().get("totalRecords", 0)
    except Exception as e:
        log(f"{app}: cannot read backlog: {e}")
        return
    if not total:
        log(f"{app}: backlog empty")
        return

    per_page = BATCH
    pages = max(1, (total + per_page - 1) // per_page)
    page = (st.get(f"{app}_page", 0) % pages) + 1

    try:
        recs = requests.get(f"{base}/{wanted_path}", headers=h, timeout=180,
                            params={"pageSize": per_page, "page": page,
                                    "monitored": "true", "sortKey": "airDateUtc",
                                    "sortDirection": "descending"}).json().get("records", [])
    except Exception as e:
        log(f"{app}: cannot fetch page {page}: {e}")
        return

    ids = [r["id"] for r in recs]
    if not ids:
        log(f"{app}: page {page}/{pages} empty")
        st[f"{app}_page"] = page
        save_state(st)
        return

    try:
        r = requests.post(f"{base}/command", headers=h, timeout=180,
                          json={"name": cmd_name, id_field: ids})
        ok = r.status_code < 300
    except Exception as e:
        log(f"{app}: search command failed: {e}")
        return

    st[f"{app}_page"] = page
    save_state(st)
    log(f"{app}: searched {len(ids)} of {total} missing (page {page}/{pages}) -> "
        f"{'queued' if ok else 'FAILED ' + str(r.status_code)}")


def main():
    inc = qbit_incomplete()
    if inc >= MAX_INCOMPLETE:
        log(f"skip: {inc} incomplete downloads (limit {MAX_INCOMPLETE})")
        return
    log(f"capacity ok: {inc} incomplete (limit {MAX_INCOMPLETE})")
    run("sonarr", 8989, "v3", "EpisodeSearch", "episodeIds", "wanted/missing")
    run("radarr", 7878, "v3", "MoviesSearch",  "movieIds",   "wanted/missing")


if __name__ == "__main__":
    main()
