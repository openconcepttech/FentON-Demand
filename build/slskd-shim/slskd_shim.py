#!/usr/bin/env python3
"""slskd-shim — makes Soulseek look like a Torznab indexer + a qBittorrent
download client, so Sonarr/Radarr can search it interactively and show real
progress in their queue.

Why this exists: Sonarr/Radarr have no plugin system (unlike Lidarr) and their
pipeline only speaks Torznab/Newznab + torrent/usenet download clients. Soulseek
is neither. Rather than teach them a new protocol, this speaks the two protocols
they already know:

  * GET /torznab/api            -> Torznab XML built from live slskd searches
  * /api/v2/...                 -> the subset of qBittorrent's WebUI API Sonarr uses

A search result's "magnet" encodes the slskd user + filename. When Sonarr "adds"
it, we enqueue the real download in slskd and then report its live progress back
as a qBittorrent torrent object, so the queue shows genuine percentages.
"""

import os
import re
import json
import time
import base64
import hashlib
import logging
import threading
import urllib.parse
from xml.sax.saxutils import escape as xml_escape

import requests
import slskd_api
from flask import Flask, request, jsonify, Response, make_response

SLSKD_URL = os.environ["SLSKD_URL"]
SLSKD_API_KEY = os.environ["SLSKD_API_KEY"]
# Where slskd writes finished files, expressed as *Sonarr/Radarr* see it.
ARR_SAVE_PATH = os.environ.get("ARR_SAVE_PATH", "/data/_soulseek_downloads")
# Finished transfers are reported to the *arr on every poll forever. At 2.7k
# entries Sonarr's queue filled with Soulseek rows and it stopped tracking its
# own qBittorrent torrents, which then looked orphaned to the reaper and were
# deleted without a blocklist or replacement search. Drop long-completed ones.
PRUNE_DONE_AFTER = float(os.environ.get("PRUNE_DONE_AFTER_HOURS", "6")) * 3600
# A Soulseek peer can hold you in its upload queue indefinitely. Those transfers
# never fail, so they sat in the *arr queue forever - 460+ of them crowded out
# Sonarr's own qBittorrent tracking, its torrents looked orphaned to the reaper,
# and the same dead releases were grabbed again on the next pass. Give up on a
# transfer that has moved no bytes after this long.
EXPIRE_QUEUED_AFTER = float(os.environ.get("EXPIRE_QUEUED_HOURS", "24")) * 3600
# Same directory as this container sees it (to confirm files landed).
LOCAL_SAVE_PATH = os.environ.get("LOCAL_SAVE_PATH", "/downloads")
STATE_FILE = os.environ.get("STATE_FILE", "/data/shim_state.json")
SEARCH_TIMEOUT_MS = int(os.environ.get("SEARCH_TIMEOUT_MS", "12000"))
MIN_SIZE_MB = int(os.environ.get("MIN_SIZE_MB", "20"))

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("shim")

app = Flask(__name__)
slskd = slskd_api.SlskdClient(SLSKD_URL, SLSKD_API_KEY)

_lock = threading.Lock()
# hash -> {username, filename, size, name, category, added_on, cancelled}
_torrents = {}
# Sonarr/Radarr create a category then re-read the list to verify it stuck,
# so these must persist independently of whether any torrent uses them yet.
_categories = {}
# short-id -> payload, so /download/ URLs stay short
_payloads = {}


def load_state():
    global _torrents, _categories
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        _torrents = d.get("torrents", {})
        _categories = d.get("categories", {})
        _payloads.update(d.get("payloads", {}))
        log.info(f"loaded {len(_torrents)} items, {len(_categories)} categories")
    except Exception:
        _torrents, _categories = {}, {}


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"torrents": _torrents, "categories": _categories,
                        "payloads": _payloads}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"state save failed: {e}")


def fake_hash(username, filename):
    """Deterministic 40-hex infohash so Sonarr accepts it as a torrent id."""
    return hashlib.sha1(f"{username}|{filename}".encode("utf-8", "replace")).hexdigest()


def basename(path):
    return path.replace("\\", "/").split("/")[-1]


def dirname(path):
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[:-1]) if len(parts) > 1 else ""


# "CD 01", "CD1", "Disc 2", "Disk 3", "CD-1" ... a disc subfolder is not an album
# name; Lidarr cannot match a release called "CD 01", so fall back to its parent.
_DISC_DIR = re.compile(r"^(cd|disc|disk)[\s._-]*\d+$", re.I)


def album_title_from_dir(directory):
    parts = [x for x in directory.replace("\\", "/").split("/") if x]
    if not parts:
        return ""
    leaf = parts[-1]
    if _DISC_DIR.match(leaf.strip()) and len(parts) > 1:
        return parts[-2]
    return leaf


def release_title(filename):
    """Sonarr parses the *title* to work out series/season/episode + quality, so
    hand it the raw release-style filename (minus extension)."""
    b = basename(filename)
    return re.sub(r"\.(mkv|mp4|avi|m4v|mov|ts|wmv|flac|mp3|m4a|ogg|opus|wav)$", "", b, flags=re.I)


def encode_payload(username, filename, size):
    raw = json.dumps({"u": username, "f": filename, "s": size}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def encode_album_payload(username, directory, files, total):
    """A whole Soulseek folder as one release. Lidarr grabs an ALBUM and fails
    the import with "Has missing tracks" if it only receives one file, so audio
    results are bundled per-directory instead of per-file."""
    raw = json.dumps({"u": username, "d": directory, "s": total,
                       "fs": [[f["filename"], f["size"]] for f in files]}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_payload(token):
    return json.loads(base64.urlsafe_b64decode(token.encode()))


# --- minimal bencode/bdecode -------------------------------------------------
# Sonarr's Torznab parser NullReferences on magnet-only items (GetDownloadUrl),
# which marks the whole indexer failed. Real trackers serve an http .torrent, so
# we synthesise one; the slskd coordinates ride along in a top-level "slsk" key.
def bencode(o):
    if isinstance(o, bool):
        raise TypeError("bool")
    if isinstance(o, int):
        return b"i" + str(o).encode() + b"e"
    if isinstance(o, str):
        o = o.encode("utf-8", "replace")
    if isinstance(o, bytes):
        return str(len(o)).encode() + b":" + o
    if isinstance(o, list):
        return b"l" + b"".join(bencode(x) for x in o) + b"e"
    if isinstance(o, dict):
        items = sorted((k if isinstance(k, bytes) else k.encode(), v) for k, v in o.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(o))


def bdecode(data, pos=0):
    c = data[pos:pos + 1]
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if c == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            v, pos = bdecode(data, pos)
            out.append(v)
        return out, pos + 1
    if c == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            k, pos = bdecode(data, pos)
            v, pos = bdecode(data, pos)
            out[k.decode("utf-8", "replace") if isinstance(k, bytes) else k] = v
        return out, pos + 1
    colon = data.index(b":", pos)
    n = int(data[pos:colon])
    start = colon + 1
    return data[start:start + n], start + n


PIECE_LEN = 4 * 1024 * 1024


def _info_dict(username, filename, size):
    size = max(int(size), 1)
    npieces = max(1, -(-size // PIECE_LEN))   # ceil; Sonarr rejects a short pieces string
    return {"name": basename(filename), "length": size,
            "piece length": PIECE_LEN, "pieces": b"\x00" * (20 * npieces)}


def torrent_infohash(username, filename, size):
    """Sonarr tracks a download by the torrent's REAL infohash (sha1 of the
    bencoded info dict). Report anything else from /torrents/info and it can
    never match the queue item back to the release it grabbed."""
    return hashlib.sha1(bencode(_info_dict(username, filename, size))).hexdigest()


def build_torrent(username, filename, size, token=None):
    return bencode({
        "announce": "http://slskd-shim.invalid/announce",
        "created by": "slskd-shim",
        "info": _info_dict(username, filename, size),
        "slsk": token or encode_payload(username, filename, size),
    })


# --------------------------------------------------------------- searching --
def slskd_search(term, want="video"):
    exts = VIDEO_EXTS if want == "video" else AUDIO_EXTS
    try:
        s = slskd.searches.search_text(
            searchText=term, searchTimeout=SEARCH_TIMEOUT_MS,
            responseLimit=60, filterResponses=True, minimumResponseFileCount=1,
        )
    except Exception as e:
        log.warning(f"search start failed for {term!r}: {e}")
        return []
    sid = s["id"]
    deadline = time.time() + (SEARCH_TIMEOUT_MS / 1000) + 10
    while time.time() < deadline:
        try:
            if slskd.searches.state(sid).get("isComplete"):
                break
        except Exception:
            break
        time.sleep(1.5)
    try:
        responses = slskd.searches.search_responses(sid)
    except Exception:
        responses = []
    try:
        slskd.searches.delete(sid)
    except Exception:
        pass

    out = []
    for r in responses:
        user = r.get("username")
        free = r.get("hasFreeUploadSlot")
        qlen = r.get("queueLength", 0)
        speed = r.get("uploadSpeed", 0)
        for f in r.get("files", []):
            fn = f.get("filename", "")
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            size = f.get("size", 0)
            if size < MIN_SIZE_MB * 1_000_000:
                continue
            out.append({
                "username": user, "filename": fn, "size": size,
                "free": bool(free), "queue": qlen, "speed": speed,
            })
    # a free slot and a short queue matter more than raw advertised speed
    out.sort(key=lambda x: (not x["free"], x["queue"], -x["speed"]))

    if want != "audio":
        return out[:80]

    # --- music: collapse to one release per (user, album folder) ------------
    albums = {}
    for r in out:
        d = dirname(r["filename"])
        key = (r["username"], d)
        a = albums.setdefault(key, {"username": r["username"], "directory": d,
                                     "files": [], "size": 0, "free": r["free"],
                                     "queue": r["queue"], "speed": r["speed"]})
        a["files"].append({"filename": r["filename"], "size": r["size"]})
        a["size"] += r["size"]
    bundles = [a for a in albums.values() if a["files"]]
    bundles.sort(key=lambda x: (not x["free"], x["queue"], -len(x["files"])))
    return bundles[:60]


# ---------------------------------------------------------------- torznab ---
CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="slskd (Soulseek)"/>
  <limits max="100" default="60"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q"/>
    <music-search available="yes" supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/></category>
    <category id="5000" name="TV"><subcat id="5040" name="TV/HD"/></category>
    <category id="3000" name="Audio"/>
  </categories>
</caps>"""


@app.route("/torznab/api")
def torznab():
    t = request.args.get("t", "search")
    if t == "caps":
        return Response(CAPS_XML, mimetype="application/xml")

    q = (request.args.get("q") or "").strip()
    season = request.args.get("season")
    ep = request.args.get("ep")
    cat = request.args.get("cat", "")

    if t == "tvsearch" and q and season and ep:
        term = f"{q} S{int(season):02d}E{int(ep):02d}"
    elif t == "tvsearch" and q and season:
        term = f"{q} S{int(season):02d}"
    else:
        term = q

    want = "audio" if cat.startswith("3") else "video"
    results = slskd_search(term, want) if term else []
    log.info(f"torznab t={t} q={term!r} -> {len(results)} results")

    # Sonarr/Radarr validate an indexer by firing an EMPTY query and requiring at
    # least one in-category result; Soulseek has no browse/RSS concept, so answer
    # that connectivity probe with a single synthetic item. Real (non-empty)
    # queries never hit this path, and RSS sync is disabled on the indexer.
    if not term:
        probe_cat = (cat.split(",")[0] if cat else "") or ("3000" if want == "audio" else "5000")
        # Must look like a *parseable* release or the app filters it out before
        # counting and still reports "no results in the configured categories".
        probe_title = {
            "2000": "Slskd.Shim.Probe.2020.1080p.WEB-DL.x264-SHIM",
            "3000": "Slskd Shim Probe - Connectivity Check (2020) [FLAC]",
        }.get(probe_cat[:4], "Slskd.Shim.Probe.S01E01.1080p.WEB-DL.x264-SHIM")
        probe_dl = (request.host_url.rstrip("/") + "/download/" +
                    encode_payload("__probe__", "probe.mkv", 1073741824) + ".torrent")
        return Response(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>slskd (Soulseek)</title>
    <torznab:response offset="0" total="1"/>
    <item>
      <title>{xml_escape(probe_title)}</title>
      <guid>slskd-shim-probe</guid>
      <link>{xml_escape(probe_dl)}</link>
      <size>1073741824</size>
      <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}</pubDate>
      <enclosure url="{xml_escape(probe_dl)}" length="1073741824" type="application/x-bittorrent"/>
      <torznab:attr name="category" value="{probe_cat}"/>
      <torznab:attr name="seeders" value="10"/>
      <torznab:attr name="peers" value="11"/>
      <torznab:attr name="downloadvolumefactor" value="0"/>
      <torznab:attr name="uploadvolumefactor" value="1"/>
    </item>
  </channel>
</rss>""", mimetype="application/xml")

    items = []
    base = request.host_url.rstrip("/")
    for r in results:
        if want == "audio":
            # one release per album folder; the folder name is what Lidarr parses
            title = album_title_from_dir(r["directory"]) or release_title(r["files"][0]["filename"])
            h = torrent_infohash(r["username"], r["directory"], r["size"])
            payload = h                      # short id; payload kept server-side
            _payloads[h] = {"u": r["username"], "d": r["directory"], "s": r["size"],
                             "fs": [[f["filename"], f["size"]] for f in r["files"]]}
            r = {**r, "size": r["size"]}
        else:
            h = torrent_infohash(r["username"], r["filename"], r["size"])
            title = release_title(r["filename"])
            payload = encode_payload(r["username"], r["filename"], r["size"])
        dl = f"{base}/download/{payload}.torrent"
        # Advertise peers so Sonarr doesn't discard it as a dead release.
        seeders = 20 if r["free"] else 5
        items.append(f"""    <item>
      <title>{xml_escape(title)}</title>
      <guid>{h}</guid>
      <link>{xml_escape(dl)}</link>
      <size>{r['size']}</size>
      <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}</pubDate>
      <enclosure url="{xml_escape(dl)}" length="{r['size']}" type="application/x-bittorrent"/>
      <torznab:attr name="category" value="{'3000' if want=='audio' else '5000'}"/>
      <torznab:attr name="seeders" value="{seeders}"/>
      <torznab:attr name="peers" value="{seeders + 1}"/>
      <torznab:attr name="downloadvolumefactor" value="0"/>
      <torznab:attr name="uploadvolumefactor" value="1"/>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>slskd (Soulseek)</title>
{chr(10).join(items)}
  </channel>
</rss>"""
    return Response(xml, mimetype="application/xml")


@app.route("/download/<token>.torrent")
def serve_torrent(token):
    # Album payloads embed every track name; encoded inline they produced 4.6 KB
    # URLs (growing with track count). Short ids keep the URL tiny; the real
    # payload is resolved from _payloads, persisted with the rest of the state.
    if token in _payloads:
        p = _payloads[token]
    else:
        try:
            p = decode_payload(token)
        except Exception:
            return "bad token", 400
    if "d" in p:                                  # album bundle
        data = build_torrent(p["u"], p["d"], p["s"], token=token)
        name = basename(p["d"]) or "album"
    else:
        data = build_torrent(p["u"], p["f"], p["s"], token=token)
        name = basename(p["f"])
    # HTTP headers are latin-1 only. Album/track names routinely carry em-dashes
    # and accented letters (U+2014 in "Drake - ICEMAN" 500'd every grab), so the
    # header gets an ASCII-safe name and the real one rides in filename* (RFC 5987).
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "release"
    return Response(data, mimetype="application/x-bittorrent",
                    headers={"Content-Disposition":
                             "attachment; filename=\"%s.torrent\"; filename*=UTF-8''%s.torrent"
                             % (ascii_name, urllib.parse.quote(name, safe=""))})


# ------------------------------------------- qBittorrent-compatible client --
def _ok(body="Ok."):
    return make_response(body, 200)


@app.route("/api/v2/auth/login", methods=["POST", "GET"])
def qb_login():
    r = make_response("Ok.", 200)
    r.set_cookie("SID", "slskd-shim-session")
    return r


@app.route("/api/v2/app/version")
def qb_version():
    return _ok("v4.6.0")


@app.route("/api/v2/app/webapiVersion")
def qb_api_version():
    return _ok("2.9.2")


@app.route("/api/v2/app/preferences")
def qb_prefs():
    return jsonify({
        "save_path": ARR_SAVE_PATH,
        "temp_path_enabled": False,
        "temp_path": ARR_SAVE_PATH,
        "queueing_enabled": False,
        "dht": False,
        "max_ratio_enabled": False,
        "max_ratio": -1,
        "max_seeding_time_enabled": False,
        "max_seeding_time": -1,
        "max_active_downloads": 5,
        "listen_port": 0,
    })


@app.route("/api/v2/torrents/categories")
def qb_categories():
    with _lock:
        cats = dict(_categories)
        for t in _torrents.values():
            c = t.get("category") or ""
            if c and c not in cats:
                cats[c] = {"name": c, "savePath": ARR_SAVE_PATH}
    return jsonify(cats)


@app.route("/api/v2/torrents/createCategory", methods=["POST"])
@app.route("/api/v2/torrents/editCategory", methods=["POST"])
def qb_create_category():
    name = request.form.get("category", "")
    if name:
        with _lock:
            _categories[name] = {"name": name,
                                  "savePath": request.form.get("savePath") or ARR_SAVE_PATH}
            save_state()
        log.info(f"category registered: {name}")
    return _ok()


@app.route("/api/v2/torrents/setCategory", methods=["POST"])
def qb_set_category():
    hashes = (request.form.get("hashes") or "").split("|")
    cat = request.form.get("category", "")
    with _lock:
        for h in hashes:
            if h in _torrents:
                _torrents[h]["category"] = cat
        save_state()
    return _ok()


@app.route("/api/v2/torrents/add", methods=["POST"])
def qb_add():
    category = request.form.get("category", "")
    added = 0
    payloads = []

    # Sonarr/Radarr normally POST the .torrent file itself
    for fh in request.files.getlist("torrents"):
        try:
            meta, _ = bdecode(fh.read())
            tok = meta.get("slsk")
            if isinstance(tok, bytes):
                tok = tok.decode()
            if tok:
                payloads.append(tok)
        except Exception as e:
            log.warning(f"add: could not parse uploaded torrent: {e}")

    # ...but also accept a URL pointing back at our own /download endpoint
    for url in [u.strip() for u in request.form.get("urls", "").splitlines() if u.strip()]:
        m = re.search(r"/download/([A-Za-z0-9_\-=]+)\.torrent", url) or \
            re.search(r"[?&]slsk=([A-Za-z0-9_\-=]+)", url)
        if m:
            payloads.append(m.group(1))
        else:
            log.warning(f"add: unrecognised url {url[:80]}")

    for tok in payloads:
        if tok in _payloads:
            p = _payloads[tok]
        else:
            try:
                p = decode_payload(tok)
            except Exception as e:
                log.warning(f"add: bad payload: {e}")
                continue
        if p["u"] == "__probe__":
            log.info("add: ignoring probe item")
            continue
        username = p["u"]
        if "d" in p:                                  # ---- album bundle ----
            directory, size = p["d"], p["s"]
            files = [{"filename": f, "size": sz} for f, sz in p["fs"]]
            h = torrent_infohash(username, directory, size)
            try:
                slskd.transfers.enqueue(username=username, files=files)
            except Exception as e:
                log.warning(f"add: slskd enqueue failed for album {directory}: {e}")
                continue
            with _lock:
                _torrents[h] = {
                    "username": username, "directory": directory,
                    "filenames": [f["filename"] for f in files],
                    "filename": files[0]["filename"],   # for legacy code paths
                    "size": size, "name": album_title_from_dir(directory) or basename(directory),
                    "category": category, "added_on": int(time.time()),
                    "cancelled": False, "is_album": True,
                }
                save_state()
            added += 1
            log.info(f"add: enqueued ALBUM '{basename(directory)}' "
                     f"({len(files)} tracks) from {username}")
        else:                                          # ---- single file ----
            filename, size = p["f"], p["s"]
            h = torrent_infohash(username, filename, size)
            try:
                slskd.transfers.enqueue(username=username,
                                         files=[{"filename": filename, "size": size}])
            except Exception as e:
                log.warning(f"add: slskd enqueue failed for {filename}: {e}")
                continue
            with _lock:
                _torrents[h] = {
                    "username": username, "filename": filename, "size": size,
                    "name": release_title(filename), "category": category,
                    "added_on": int(time.time()), "cancelled": False,
                }
                save_state()
            added += 1
            log.info(f"add: enqueued {basename(filename)} from {username}")
    return _ok()


# One slskd round-trip per tracked torrent does not scale (1500+ items => the
# /torrents/info poll times out). Fetch every transfer once, index it, reuse for
# a few seconds -- Sonarr polls each client about once a minute.
_dl_cache = {"ts": 0.0, "map": {}}
_DL_CACHE_TTL = float(os.environ.get("DL_CACHE_TTL", "10"))


def _downloads_index():
    now = time.time()
    if now - _dl_cache["ts"] < _DL_CACHE_TTL and _dl_cache["map"]:
        return _dl_cache["map"]
    idx = {}
    try:
        for user in slskd.transfers.get_all_downloads():
            uname = user.get("username")
            for d in user.get("directories", []):
                for f in d.get("files", []):
                    idx[(uname, f.get("filename"))] = f
    except Exception as e:
        log.warning(f"could not index slskd downloads: {e}")
        return _dl_cache["map"]          # serve stale rather than nothing
    _dl_cache["ts"] = now
    _dl_cache["map"] = idx
    return idx


def _live_transfer(username, filename):
    return _downloads_index().get((username, filename))


# Walking the whole download tree per torrent is O(torrents x files) and made
# /torrents/info exceed 60s with ~1500 tracked items. Walk once, index by
# basename, reuse.
_disk_cache = {"ts": 0.0, "map": {}}
_force_refresh = {"ts": 0.0}
_FORCE_INTERVAL = float(os.environ.get("FORCE_REFRESH_INTERVAL", "30"))
_DISK_CACHE_TTL = float(os.environ.get("DISK_CACHE_TTL", "120"))


def _disk_index():
    now = time.time()
    if now - _disk_cache["ts"] < _DISK_CACHE_TTL and _disk_cache["map"]:
        return _disk_cache["map"]
    idx = {}
    try:
        for root, _, files in os.walk(LOCAL_SAVE_PATH):
            for f in files:
                idx.setdefault(f, os.path.join(root, f))
    except Exception as e:
        log.warning(f"disk index failed: {e}")
        return _disk_cache["map"]
    _disk_cache["ts"] = now
    _disk_cache["map"] = idx
    return idx


def _find_completed_file(filename):
    """slskd may sanitise names; match on basename under the download root.

    A file that finished within the cache TTL is not in the index yet. Reporting
    the flat fallback path in that window is not harmless: Sonarr stores the
    first outputPath it sees and retries that dead path forever. So on a miss,
    force one refresh before giving up."""
    base = basename(filename)
    hit = _disk_index().get(base)
    if hit:
        return hit
    # Rebuilding costs a full os.walk of the download tree. Items whose files
    # are gone (imported/moved) miss on EVERY poll, so refreshing per miss meant
    # hundreds of walks per request and the endpoint timed out. Force at most
    # one rebuild per _FORCE_INTERVAL across all misses.
    now = time.time()
    if now - _force_refresh["ts"] < _FORCE_INTERVAL:
        return None
    _force_refresh["ts"] = now
    _disk_cache["ts"] = 0.0
    return _disk_index().get(base)


def _resolve_album_path(h, t):
    """Directory holding the album, as the *arr sees it. Lidarr imports a folder."""
    cached = t.get("resolved_path")
    if cached:
        return cached
    for fn in t.get("filenames", []):
        local = _find_completed_file(fn)
        if local:
            arr_dir = os.path.join(ARR_SAVE_PATH,
                                   os.path.relpath(os.path.dirname(local), LOCAL_SAVE_PATH))
            with _lock:
                if h in _torrents:
                    _torrents[h]["resolved_path"] = arr_dir
                    save_state()
            return arr_dir
    return os.path.join(ARR_SAVE_PATH, basename(t.get("directory", "")))


def _resolve_content_path(h, t):
    """Path of the finished file AS SONARR/RADARR SEE IT.

    slskd preserves the remote user's folder structure (e.g. the file lands in
    "<root>/Season 20/Show - S20E01 ....mkv"), so reporting <root>/<basename>
    makes the *arr report "No files found are eligible for import". Resolve the
    real location and translate it onto their mount. Cached: the os.walk is far
    too expensive to repeat for every torrent on every poll."""
    cached = t.get("resolved_path")
    if cached:
        return cached
    local = _find_completed_file(t["filename"])
    if not local:
        return None                  # caller keeps it "downloading" instead
    rel = os.path.relpath(local, LOCAL_SAVE_PATH)
    arr_path = os.path.join(ARR_SAVE_PATH, rel)
    with _lock:
        if h in _torrents:
            _torrents[h]["resolved_path"] = arr_path
            save_state()
    return arr_path


_last_prune = {"ts": 0.0}
_active_cache = {"ts": 0.0, "names": set()}


def maybe_prune():
    now = time.time()
    if now - _last_prune["ts"] < 1800:
        return 0
    _last_prune["ts"] = now
    try:
        expire_stale_queued()
        return prune_completed()
    except Exception as e:
        log.warning(f"prune failed: {e}")
        return 0


def expire_stale_queued():
    """Cancel transfers that have sat queued with zero progress for too long."""
    now = time.time()
    victims = []
    with _lock:
        for h, t in list(_torrents.items()):
            age = now - (t.get("added_on") or now)
            if age < EXPIRE_QUEUED_AFTER:
                continue
            if t.get("completed_on"):
                continue
            victims.append((h, dict(t)))

    cancelled = dropped = 0
    for h, t in victims:
        names = t.get("filenames") or [t.get("filename")]
        moved = False
        for fn in names:
            if fn and _find_completed_file(fn):
                moved = True          # something actually landed - leave it be
                break
        if moved:
            continue
        for fn in names:
            if not fn:
                continue
            try:
                f = _live_transfer(t["username"], fn)
                if f is not None:
                    slskd.transfers.cancel_download(t["username"], f.get("id"),
                                                     remove=True)
                    cancelled += 1
            except Exception:
                pass
        with _lock:
            if _torrents.pop(h, None) is not None:
                _payloads.pop(h, None)
                dropped += 1
    if dropped:
        with _lock:
            save_state()
        log.info(f"expired {dropped} stale queued transfer(s), "
                 f"cancelled {cancelled} in slskd ({len(_torrents)} remain)")
    return dropped


def prune_completed():
    """Forget transfers that finished long ago; the *arr already imported them."""
    now = time.time()
    dropped = 0
    with _lock:
        for h, t in list(_torrents.items()):
            if t.get("cancelled"):
                continue
            done_at = t.get("completed_on") or t.get("added_on") or now
            if (now - done_at) > PRUNE_DONE_AFTER and _prunable(h, t):
                del _torrents[h]
                _payloads.pop(h, None)
                dropped += 1
        if dropped:
            save_state()
    if dropped:
        log.info(f"pruned {dropped} long-completed entries "
                 f"({len(_torrents)} remain)")
    return dropped


def _prunable(h, t):
    """Safe to forget?

    Checking "are the files still on disk" was wrong: the entries most worth
    dropping are the ones the *arr already imported and moved away, so that test
    excluded exactly the wrong set. What matters is that slskd is no longer
    working on it - anything not in the live transfer list is finished, failed,
    or abandoned, and re-adding is cheap if we are wrong.
    """
    try:
        active = _active_transfer_names()
    except Exception:
        return False                      # cannot tell -> keep it
    names = t.get("filenames") or [t.get("filename")]
    return not any(basename(fn or "") in active for fn in names)


def _active_transfer_names():
    """Basenames slskd currently has in flight (cached briefly)."""
    now = time.time()
    if now - _active_cache["ts"] < 60:
        return _active_cache["names"]
    names = set()
    try:
        for u in slskd.transfers.get_all_downloads():
            for d in u.get("directories", []):
                for f in d.get("files", []):
                    st = str(f.get("state", ""))
                    if "Completed" not in st:
                        names.add(basename(f.get("filename", "")))
    except Exception:
        raise
    _active_cache["ts"] = now
    _active_cache["names"] = names
    return names


@app.route("/api/v2/torrents/info")
def qb_info():
    maybe_prune()
    want_cat = request.args.get("category")
    want_hashes = request.args.get("hashes")
    out = []
    with _lock:
        items = list(_torrents.items())

    for h, t in items:
        if want_cat is not None and t.get("category") != want_cat:
            continue
        if want_hashes and want_hashes != "all" and h not in want_hashes.split("|"):
            continue

        if t.get("is_album"):
            # aggregate every track in the folder into one reported "torrent"
            idx = _downloads_index()
            done_b, speed_b, states = 0, 0, []
            for fn in t.get("filenames", []):
                tf = idx.get((t["username"], fn))
                if tf is None:
                    continue
                done_b += tf.get("bytesTransferred", 0) or 0
                speed_b += int(tf.get("averageSpeed", 0) or 0)
                states.append(tf.get("state", ""))
            size = t["size"] or 1
            n = len(t.get("filenames", []))
            n_done = sum(1 for st in states if "Succeeded" in st)
            if states and n_done == n:
                qstate, pct, speed_b, done_b = "pausedUP", 1.0, 0, size
            elif any("InProgress" in st for st in states):
                qstate = "downloading"
                pct = min(done_b / size, 0.999)
            elif states and all(any(x in st for x in ("Errored", "Cancelled", "Aborted", "Rejected"))
                                 for st in states):
                qstate, pct = "error", done_b / size
            elif not states:
                # nothing live: either finished and cleared, or never started
                local = _find_completed_file(t["filenames"][0]) if t.get("filenames") else None
                if local:
                    qstate, pct, speed_b, done_b = "pausedUP", 1.0, 0, size
                else:
                    qstate, pct = "queuedDL", 0.0
            else:
                qstate = "queuedDL"
                pct = min(done_b / size, 0.999)
            content = (_resolve_album_path(h, t) if pct >= 1
                        else os.path.join(ARR_SAVE_PATH, basename(t.get("directory", ""))))
            eta = int((size - done_b) / speed_b) if speed_b > 0 else 8640000
            out.append({
                "hash": h, "name": t["name"], "size": size, "total_size": size,
                "progress": pct, "downloaded": int(done_b), "completed": int(done_b),
                "amount_left": max(int(size - done_b), 0), "dlspeed": speed_b, "upspeed": 0,
                "eta": eta, "state": qstate, "category": t.get("category", ""), "tags": "",
                "save_path": ARR_SAVE_PATH, "content_path": content,
                "added_on": t["added_on"],
                "completion_on": int(time.time()) if pct >= 1 else 0,
                "ratio": 0.0, "seeding_time": 0, "num_seeds": 1, "num_leechs": 0,
                "priority": 0, "force_start": False, "auto_tmm": False,
                "availability": 1.0 if pct >= 1 else 0.9,
            })
            continue

        f = _live_transfer(t["username"], t["filename"])
        size = t["size"] or 1
        if f is not None:
            state_raw = f.get("state", "")
            done = f.get("bytesTransferred", 0)
            speed = int(f.get("averageSpeed", 0) or 0)
            pct = min(max((f.get("percentComplete", 0) or 0) / 100.0, 0.0), 1.0)
            if "Succeeded" in state_raw:
                qstate, pct, speed = "pausedUP", 1.0, 0
            elif "InProgress" in state_raw:
                qstate = "downloading"
            elif "Queued" in state_raw:
                # Sitting in the remote peer's upload queue is normal on Soulseek.
                # Report queuedDL, NOT stalledDL -- Sonarr surfaces stalledDL as
                # "download is stalled with no connections" and warns on it.
                qstate = "queuedDL"
            elif any(x in state_raw for x in ("Initializing", "Requested")):
                qstate = "downloading"
            elif any(x in state_raw for x in ("Errored", "Cancelled", "Aborted", "Rejected")):
                qstate = "error"
            else:
                qstate = "queuedDL"
        else:
            # not in slskd's active list: either finished-and-cleared, or gone
            local = _find_completed_file(t["filename"])
            if local:
                qstate, pct, speed, done = "pausedUP", 1.0, 0, size
            else:
                qstate, pct, speed, done = "queuedDL", 0.0, 0, 0

        if pct >= 1:
            content = _resolve_content_path(h, t)
            if content is None:
                # finished transferring but not visible on disk yet: stay
                # "downloading" so the *arr does not import a path that
                # does not exist and then cache that failure.
                qstate, pct = "downloading", 0.99
                content = os.path.join(ARR_SAVE_PATH, basename(t["filename"]))
        else:
            content = os.path.join(ARR_SAVE_PATH, basename(t["filename"]))
        eta = int((size - done) / speed) if speed > 0 else 8640000
        out.append({
            "hash": h,
            "name": t["name"],
            "size": size,
            "total_size": size,
            "progress": pct,
            "downloaded": int(done),
            "completed": int(done),
            "amount_left": max(int(size - done), 0),
            "dlspeed": speed,
            "upspeed": 0,
            "eta": eta,
            "state": qstate,
            "category": t.get("category", ""),
            "tags": "",
            "save_path": ARR_SAVE_PATH,
            "content_path": content,
            "added_on": t["added_on"],
            "completion_on": int(time.time()) if pct >= 1 else 0,
            "ratio": 0.0,
            "seeding_time": 0,
            "num_seeds": 1,
            "num_leechs": 0,
            "priority": 0,
            "force_start": False,
            "auto_tmm": False,
            "availability": 1.0 if pct >= 1 else 0.9,
        })
    return jsonify(out)


@app.route("/api/v2/torrents/properties")
def qb_properties():
    h = request.args.get("hash", "")
    with _lock:
        t = _torrents.get(h)
    if not t:
        return jsonify({})
    return jsonify({"save_path": ARR_SAVE_PATH, "piece_size": 0,
                     "created_by": "slskd-shim", "comment": t["filename"]})


@app.route("/api/v2/torrents/files")
def qb_files():
    h = request.args.get("hash", "")
    with _lock:
        t = _torrents.get(h)
    if not t:
        return jsonify([])
    return jsonify([{"name": basename(t["filename"]), "size": t["size"],
                      "progress": 1.0, "priority": 1, "index": 0}])


@app.route("/api/v2/torrents/delete", methods=["POST"])
def qb_delete():
    hashes = (request.form.get("hashes") or "").split("|")
    delete_files = request.form.get("deleteFiles", "false") == "true"
    with _lock:
        for h in hashes:
            t = _torrents.pop(h, None)
            if not t:
                continue
            f = _live_transfer(t["username"], t["filename"])
            if f is not None:
                try:
                    slskd.transfers.cancel_download(t["username"], f.get("id"),
                                                     remove=True)
                except Exception:
                    pass
            if delete_files:
                local = _find_completed_file(t["filename"])
                if local:
                    try:
                        os.remove(local)
                    except Exception:
                        pass
        save_state()
    return _ok()


@app.route("/api/v2/torrents/setForceStart", methods=["POST"])
@app.route("/api/v2/torrents/topPrio", methods=["POST"])
@app.route("/api/v2/torrents/pause", methods=["POST"])
@app.route("/api/v2/torrents/resume", methods=["POST"])
@app.route("/api/v2/torrents/setShareLimits", methods=["POST"])
def qb_noop():
    return _ok()


@app.route("/prune", methods=["POST", "GET"])
def prune_now():
    before = len(_torrents)
    expired = expire_stale_queued()
    dropped = prune_completed()
    return jsonify({"before": before, "expired": expired, "pruned": dropped,
                     "remaining": len(_torrents)})


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    load_state()
    # Flask's dev server starves under concurrent load: a Torznab search holds a
    # worker 12-25s while slskd answers, which made Lidarr's .torrent fetch time
    # out ("Http request timed out" -> "Getting release from indexer failed").
    try:
        from waitress import serve as waitress_serve
        log.info("serving via waitress (threads=24)")
        waitress_serve(app, host="0.0.0.0", port=8200, threads=24, channel_timeout=120)
    except ImportError:
        log.warning("waitress unavailable, using the dev server")
        app.run(host="0.0.0.0", port=8200, threaded=True)
