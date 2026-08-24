#!/usr/bin/env python3
"""Bridge: watches Radarr/Sonarr missing lists, searches slskd (Soulseek),
downloads confident matches, imports them via Radarr/Sonarr's own Manual Import API.

No community project exists for this (Soularr is Lidarr/music-only), so this
was custom-built. Mirrors the safety-net philosophy used for the King of the
Hill / Movies library cleanup: never link a file to a title unless the match
is verified with reasonable confidence -- ambiguous results are left for a
human, not guessed.
"""

import os
import re
import json
import time
import shutil
import difflib
import logging
import requests
import slskd_api

# ---------------------------------------------------------------- config --
RADARR_URL = os.environ["RADARR_URL"]
RADARR_API_KEY = os.environ["RADARR_API_KEY"]
SONARR_URL = os.environ["SONARR_URL"]
SONARR_API_KEY = os.environ["SONARR_API_KEY"]
SLSKD_URL = os.environ["SLSKD_URL"]
SLSKD_API_KEY = os.environ["SLSKD_API_KEY"]

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")          # this container's view
RADARR_STAGING_PREFIX = os.environ.get("RADARR_STAGING_PREFIX", "/data/_soulseek_video_staging")
SONARR_STAGING_PREFIX = os.environ.get("SONARR_STAGING_PREFIX", "/data/_soulseek_video_staging")
STAGING_DIR = os.environ.get("STAGING_DIR", "/staging")              # this container's view of the same folder

STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "900"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
RETRY_COOLDOWN = int(os.environ.get("RETRY_COOLDOWN", str(6 * 3600)))
BATCH_SIZE_MOVIES = int(os.environ.get("BATCH_SIZE_MOVIES", "5"))
BATCH_SIZE_EPISODES = int(os.environ.get("BATCH_SIZE_EPISODES", "10"))
STALLED_TIMEOUT = int(os.environ.get("STALLED_TIMEOUT", str(2 * 3600)))
MIN_CONFIDENCE_MOVIE = float(os.environ.get("MIN_CONFIDENCE_MOVIE", "0.75"))
MIN_CONFIDENCE_EPISODE = float(os.environ.get("MIN_CONFIDENCE_EPISODE", "0.5"))

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".mov", ".ts"}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("bridge")

slskd = slskd_api.SlskdClient(SLSKD_URL, SLSKD_API_KEY)


# ------------------------------------------------------------------ state --
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------- matching --
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTS


def movie_confidence(filename, title, year):
    nf = norm(filename)
    nt = norm(title)
    sim = difflib.SequenceMatcher(None, nt, nf).ratio()
    substr = nt in nf
    year_hit = any(str(y) in filename for y in (year - 1, year, year + 1))
    score = sim + (0.3 if substr else 0) + (0.25 if year_hit else -0.3)
    return max(score, 0)


def episode_confidence(filename, series_title, season, episode):
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", filename)
    if not m or int(m.group(1)) != season or int(m.group(2)) != episode:
        return 0
    nf = norm(filename)
    nt = norm(series_title)
    words = [w for w in nt.split() if len(w) > 2]
    if not words:
        return 0.6
    overlap = sum(1 for w in words if w in nf) / len(words)
    return 0.4 + 0.6 * overlap


def reasonable_size(size_bytes, is_episode):
    mb = size_bytes / 1e6
    return (30 <= mb <= 8000) if is_episode else (150 <= mb <= 60000)


def rank_key(resp):
    return (0 if resp.get("hasFreeUploadSlot") else 1, resp.get("queueLength", 0), -resp.get("uploadSpeed", 0))


# ------------------------------------------------------------ radarr/sonarr --
def radarr_get(path):
    r = requests.get(f"{RADARR_URL}/api/v3{path}", headers={"X-Api-Key": RADARR_API_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


def radarr_post(path, body):
    r = requests.post(f"{RADARR_URL}/api/v3{path}", headers={"X-Api-Key": RADARR_API_KEY}, json=body, timeout=60)
    r.raise_for_status()
    return r.json() if r.text else None


def sonarr_get(path):
    r = requests.get(f"{SONARR_URL}/api/v3{path}", headers={"X-Api-Key": SONARR_API_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


def sonarr_post(path, body):
    r = requests.post(f"{SONARR_URL}/api/v3{path}", headers={"X-Api-Key": SONARR_API_KEY}, json=body, timeout=60)
    r.raise_for_status()
    return r.json() if r.text else None


def get_missing_movies(limit):
    movies = radarr_get("/movie")
    missing = [m for m in movies if m["monitored"] and not m["hasFile"]]
    return missing[:limit]


def get_missing_episodes(limit):
    records = []
    page = 1
    while len(records) < limit:
        d = sonarr_get(f"/wanted/missing?pageSize=50&page={page}&includeSeries=true")
        recs = d.get("records", [])
        if not recs:
            break
        records.extend(recs)
        page += 1
        if page > 20:
            break
    return records[:limit]


def wait_command(app_get, cmd_id, timeout=40):
    for _ in range(timeout // 2):
        time.sleep(2)
        c = app_get(f"/command/{cmd_id}")
        if c["status"] in ("completed", "failed"):
            return c
    return None


# --------------------------------------------------------------- searching --
def do_search(term, timeout_ms=15000, response_limit=50):
    search = slskd.searches.search_text(
        searchText=term, searchTimeout=timeout_ms, responseLimit=response_limit,
        filterResponses=True, minimumResponseFileCount=1,
    )
    sid = search["id"]
    deadline = time.time() + (timeout_ms / 1000) + 15
    while time.time() < deadline:
        st = slskd.searches.state(sid)
        if st.get("isComplete"):
            break
        time.sleep(2)
    responses = slskd.searches.search_responses(sid)
    try:
        slskd.searches.delete(sid)
    except Exception:
        pass
    return responses


def find_best_candidate(responses, match_fn, is_episode):
    scored = []
    for resp in responses:
        for f in resp.get("files", []):
            fname = f.get("filename", "")
            if not is_video(fname):
                continue
            if not reasonable_size(f.get("size", 0), is_episode):
                continue
            score = match_fn(fname)
            if score <= 0:
                continue
            scored.append((score, resp, f))
    if not scored:
        return None, None, 0
    scored.sort(key=lambda t: (-t[0], rank_key(t[1])))
    best_score, best_resp, best_file = scored[0]
    return best_resp, best_file, best_score


# ---------------------------------------------------------------- attempts --
def attempt_movie(movie, state):
    key = f"movie:{movie['id']}"
    entry = state.get(key, {})
    if entry.get("status") == "downloading":
        return
    if entry.get("attempts", 0) >= MAX_ATTEMPTS:
        return
    if entry.get("last_attempt") and time.time() - entry["last_attempt"] < RETRY_COOLDOWN:
        return

    term = f"{movie['title']} {movie['year']}"
    log.info(f"Searching movie: {term}")
    try:
        responses = do_search(term)
    except Exception as e:
        log.warning(f"Search failed for {term}: {e}")
        return

    resp, f, score = find_best_candidate(
        responses, lambda fn: movie_confidence(fn, movie["title"], movie["year"]), is_episode=False
    )
    attempts = entry.get("attempts", 0) + 1
    if not resp or score < MIN_CONFIDENCE_MOVIE:
        log.info(f"No confident match for {term} (best score {score:.2f})")
        state[key] = {"status": "needs_review" if attempts >= MAX_ATTEMPTS else "no_match",
                       "attempts": attempts, "last_attempt": time.time(), "title": term}
        save_state(state)
        return

    log.info(f"MATCH ({score:.2f}) for {term}: {f['filename']} from {resp['username']}")
    try:
        slskd.transfers.enqueue(username=resp["username"], files=[f])
    except Exception as e:
        log.warning(f"Enqueue failed: {e}")
        state[key] = {"status": "no_match", "attempts": attempts, "last_attempt": time.time(), "title": term}
        save_state(state)
        return

    state[key] = {
        "status": "downloading", "kind": "movie", "movieId": movie["id"],
        "username": resp["username"], "filename": f["filename"], "size": f["size"],
        "attempts": attempts, "last_attempt": time.time(), "enqueued_at": time.time(),
        "title": term,
    }
    save_state(state)


def attempt_episode(ep, state):
    key = f"episode:{ep['id']}"
    entry = state.get(key, {})
    if entry.get("status") == "downloading":
        return
    if entry.get("attempts", 0) >= MAX_ATTEMPTS:
        return
    if entry.get("last_attempt") and time.time() - entry["last_attempt"] < RETRY_COOLDOWN:
        return

    series_title = ep.get("series", {}).get("title", "")
    season, episode = ep["seasonNumber"], ep["episodeNumber"]
    if not series_title:
        return
    term = f"{series_title} S{season:02d}E{episode:02d}"
    log.info(f"Searching episode: {term}")
    try:
        responses = do_search(term)
    except Exception as e:
        log.warning(f"Search failed for {term}: {e}")
        return

    resp, f, score = find_best_candidate(
        responses, lambda fn: episode_confidence(fn, series_title, season, episode), is_episode=True
    )
    attempts = entry.get("attempts", 0) + 1
    if not resp or score < MIN_CONFIDENCE_EPISODE:
        log.info(f"No confident match for {term} (best score {score:.2f})")
        state[key] = {"status": "needs_review" if attempts >= MAX_ATTEMPTS else "no_match",
                       "attempts": attempts, "last_attempt": time.time(), "title": term}
        save_state(state)
        return

    log.info(f"MATCH ({score:.2f}) for {term}: {f['filename']} from {resp['username']}")
    try:
        slskd.transfers.enqueue(username=resp["username"], files=[f])
    except Exception as e:
        log.warning(f"Enqueue failed: {e}")
        state[key] = {"status": "no_match", "attempts": attempts, "last_attempt": time.time(), "title": term}
        save_state(state)
        return

    state[key] = {
        "status": "downloading", "kind": "episode", "seriesId": ep["seriesId"], "episodeId": ep["id"],
        "username": resp["username"], "filename": f["filename"], "size": f["size"],
        "attempts": attempts, "last_attempt": time.time(), "enqueued_at": time.time(),
        "title": term,
    }
    save_state(state)


# ------------------------------------------------------------- completion --
def find_transfer_state(username, filename):
    try:
        dl = slskd.transfers.get_downloads(username)
    except Exception:
        return None
    for d in dl.get("directories", []):
        for f in d.get("files", []):
            if f.get("filename") == filename:
                return f
    return None


def process_downloading(state):
    for key, entry in list(state.items()):
        if entry.get("status") != "downloading":
            continue
        f = find_transfer_state(entry["username"], entry["filename"])
        if f is None:
            if time.time() - entry["enqueued_at"] > STALLED_TIMEOUT:
                log.warning(f"{key}: stalled/vanished, marking failed")
                entry["status"] = "no_match"
                save_state(state)
            continue
        st = f.get("state", "")
        if "Succeeded" in st:
            log.info(f"{key}: download succeeded, importing")
            import_completed(key, entry, f, state)
        elif "Completed" in st and "Succeeded" not in st:
            log.warning(f"{key}: download ended without success ({st})")
            entry["status"] = "no_match"
            save_state(state)
        elif time.time() - entry["enqueued_at"] > STALLED_TIMEOUT:
            log.warning(f"{key}: stalled beyond timeout, cancelling")
            try:
                slskd.transfers.cancel_download(entry["username"], f.get("id"), remove=True)
            except Exception:
                pass
            entry["status"] = "no_match"
            save_state(state)


def import_completed(key, entry, transfer_file, state):
    src_rel = entry["filename"].replace("\\", "/").split("/")[-1]
    src_path = None
    for root, _, files in os.walk(DOWNLOAD_DIR):
        if src_rel in files:
            src_path = os.path.join(root, src_rel)
            break
    if not src_path:
        log.warning(f"{key}: completed file not found on disk under {DOWNLOAD_DIR}")
        return

    if entry["kind"] == "movie":
        movie = radarr_get(f"/movie/{entry['movieId']}")
        folder_name = os.path.basename(movie["path"])
        dest_dir = os.path.join(STAGING_DIR, folder_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(src_path))
        shutil.move(src_path, dest_path)

        radarr_folder = f"{RADARR_STAGING_PREFIX}/{folder_name}"
        preview = radarr_get(f"/manualimport?folder={requests.utils.quote(radarr_folder)}")
        item = preview[0] if preview else None
        body = [{
            "path": item.get("path") if item else f"{radarr_folder}/{os.path.basename(dest_path)}",
            "folderName": item.get("folderName") if item else folder_name,
            "movieId": entry["movieId"],
            "quality": item.get("quality") if item and item.get("quality") else
                {"quality": {"id": 0, "name": "Unknown", "source": "unknown", "resolution": 0, "modifier": "none"},
                 "revision": {"version": 1, "real": 0, "isRepack": False}},
            "languages": item.get("languages") if item and item.get("languages") else [{"id": 1, "name": "English"}],
            "releaseGroup": item.get("releaseGroup", "") if item else "",
            "indexerFlags": 0, "downloadId": None,
        }]
        cmd = radarr_post("/command", {"name": "ManualImport", "files": body, "importMode": "move"})
        result = wait_command(radarr_get, cmd["id"])
        if result and result["status"] == "completed":
            log.info(f"{key}: imported into Radarr successfully")
            radarr_post("/command", {"name": "RenameMovie", "movieIds": [entry["movieId"]]})
            radarr_get(f"/movie/{entry['movieId']}")
            requests.put(f"{RADARR_URL}/api/v3/movie/editor", headers={"X-Api-Key": RADARR_API_KEY},
                         json={"movieIds": [entry["movieId"]], "monitored": False}, timeout=30)
            entry["status"] = "completed"
        else:
            log.warning(f"{key}: Radarr import failed")
            entry["status"] = "needs_review"
        save_state(state)

    else:
        ep = sonarr_get(f"/episode/{entry['episodeId']}")
        series = sonarr_get(f"/series/{entry['seriesId']}")
        dest_dir = os.path.join(STAGING_DIR, f"series_{entry['seriesId']}")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(src_path))
        shutil.move(src_path, dest_path)

        sonarr_folder = f"{SONARR_STAGING_PREFIX}/series_{entry['seriesId']}"
        preview = sonarr_get(f"/manualimport?folder={requests.utils.quote(sonarr_folder)}&seriesId={entry['seriesId']}")
        item = preview[0] if preview else None
        body = [{
            "path": item.get("path") if item else f"{sonarr_folder}/{os.path.basename(dest_path)}",
            "folderName": item.get("folderName") if item else f"series_{entry['seriesId']}",
            "seriesId": entry["seriesId"],
            "seasonNumber": ep["seasonNumber"],
            "episodeIds": [entry["episodeId"]],
            "quality": item.get("quality") if item and item.get("quality") else
                {"quality": {"id": 0, "name": "Unknown", "source": "unknown", "resolution": 0, "modifier": "none"},
                 "revision": {"version": 1, "real": 0, "isRepack": False}},
            "languages": item.get("languages") if item and item.get("languages") else [{"id": 1, "name": "English"}],
            "releaseGroup": item.get("releaseGroup", "") if item else "",
            "indexerFlags": 0, "downloadId": None,
        }]
        cmd = sonarr_post("/command", {"name": "ManualImport", "files": body, "importMode": "move"})
        result = wait_command(sonarr_get, cmd["id"])
        if result and result["status"] == "completed":
            log.info(f"{key}: imported into Sonarr successfully")
            sonarr_post("/command", {"name": "RenameFiles", "seriesId": entry["seriesId"],
                                       "files": [f.get("episodeFileId") for f in [ep] if f.get("episodeFileId")]})
            entry["status"] = "completed"
        else:
            log.warning(f"{key}: Sonarr import failed")
            entry["status"] = "needs_review"
        save_state(state)


# --------------------------------------------------------------------- main --
def main():
    log.info("slskd video bridge starting")
    state = load_state()
    last_search_cycle = 0

    while True:
        try:
            process_downloading(state)
        except Exception as e:
            log.exception(f"process_downloading error: {e}")

        if time.time() - last_search_cycle >= SCAN_INTERVAL:
            last_search_cycle = time.time()
            try:
                movies = get_missing_movies(BATCH_SIZE_MOVIES)
                log.info(f"Checking {len(movies)} missing movies")
                for m in movies:
                    attempt_movie(m, state)
                    process_downloading(state)
            except Exception as e:
                log.exception(f"movie cycle error: {e}")

            try:
                episodes = get_missing_episodes(BATCH_SIZE_EPISODES)
                log.info(f"Checking {len(episodes)} missing episodes")
                for ep in episodes:
                    attempt_episode(ep, state)
                    process_downloading(state)
            except Exception as e:
                log.exception(f"episode cycle error: {e}")

        counts = {}
        for e in state.values():
            counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1
        log.info(f"State summary: {counts}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
