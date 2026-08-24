#!/usr/bin/env python3
"""Newznab front-end for NZBKing (nzbking.com).

Like the BinSearch shim, but NZBKing exposes per-post completeness, password
status and filetypes in its search HTML, so this filters out the exact junk
that made BinSearch churn: incomplete posts, password-protected archives, and
fakes / zip-packs / nfo-only posts with no actual video file.
"""
import os
import re
import html as htmllib
import logging
import threading
import time
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

import requests
from flask import Flask, Response, request, jsonify
from waitress import serve as waitress_serve

BASE = os.environ.get("NZBKING_URL", "https://nzbking.com")
PORT = int(os.environ.get("PORT", "8301"))
SELF_URL = os.environ.get("SELF_URL", "http://nzbking-shim:8301")
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "100"))
MIN_SIZE_MB = float(os.environ.get("MIN_SIZE_MB", "20"))
MIN_COMPLETE = float(os.environ.get("MIN_COMPLETE", "0.98"))  # parts ratio
CACHE_TTL = float(os.environ.get("CACHE_TTL", "300"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "45"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("nzbking-shim")
app = Flask(__name__)
_sess = requests.Session()
_sess.headers.update({"User-Agent": UA})

# One search-result block = one <div class='search-result'>...</div>. Split on
# the class marker rather than trying to balance nested divs.
_SPLIT = re.compile(r"<div class='search-result'>")
_ID = re.compile(r'name="nzb"\s+onclick="[^"]*"\s+value="([a-f0-9]{8,})"')
_ID2 = re.compile(r'/nzb:([a-f0-9]{8,})/')
_SUBJECT = re.compile(r"<div class='search-subject'>(.*?)(?:<br>|<a )", re.S)
_PARTS = re.compile(r"parts:\s*<span[^>]*>(\d+)\s*/\s*(\d+)</span>", re.S)
_PASSWORD = re.compile(r"NO PASSWORD", re.I)
_HASPASS = re.compile(r">\s*PASSWORD", re.I)
_SIZE = re.compile(r"size:\s*([\d.]+)\s*(B|KB|MB|GB|TB)", re.I)
_FILETYPES = re.compile(r"filetypes:\s*(.*?)</div>", re.S)
_EXT = re.compile(r"\.([A-Za-z0-9]{2,5})")
_GROUP = re.compile(r"<div class='search-groups'>\s*(.*?)\s*</div>", re.S)
_AGE = re.compile(r"<div class='search-age'>\s*(\d+)\s*([dhwmy])", re.I)

_VIDEO_EXT = {"mkv", "mp4", "avi", "m2ts", "ts", "wmv", "mov", "mpg", "mpeg",
              "m4v", "vob", "iso", "flv", "divx"}
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_AGE_UNITS = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000, "y": 31536000}

RSS_TERMS = {"5": os.environ.get("RSS_TERM_TV", "1080p WEB"),
             "2": os.environ.get("RSS_TERM_MOVIES", "1080p BluRay"),
             "3": os.environ.get("RSS_TERM_AUDIO", "FLAC")}
_RELEASEY = re.compile(r"S\d{1,2}E\d{1,2}|\b\d{3,4}p\b|\b(19|20)\d{2}\b", re.I)

_cache = {}
_lock = threading.Lock()
_stats = {"searches": 0, "results": 0, "nzbs": 0, "last_query": "",
          "last_ts": 0.0, "search_ts": []}
_slock = threading.Lock()


def _bump(**kw):
    now = time.time()
    with _slock:
        for k, v in kw.items():
            _stats[k] = _stats[k] + v if k in ("searches", "results", "nzbs") else v
        if kw.get("searches"):
            _stats["search_ts"] = [t for t in _stats["search_ts"] + [now] if t >= now - 3600]


def _clean(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _release_title(raw):
    t = _clean(raw)
    best = ""
    for seg in re.findall(r"\[\s*([^\]\[]{8,})\s*\]", t):
        if re.search(r"\b(19|20)\d{2}\b|S\d{1,2}E\d{1,2}|\d{3,4}p", seg, re.I):
            best = seg.strip()
            break
    if not best:
        m = re.search(r'"([^"]+)"', t)
        if m:
            best = m.group(1)
            for _ in range(3):
                s2 = re.sub(r"\.(nfo|par2|sfv|nzb|jpg|png|txt|rar|7z|zip|r\d\d|vol\d+\+\d+|\d{3})$",
                            "", best, flags=re.I)
                if s2 == best:
                    break
                best = s2
    if not best:
        best = re.sub(r"^(\[[^\]]*\]-?)+", "", t).strip() or t
    best = re.sub(r"\s+yEnc.*$", "", best, flags=re.I).strip()
    return re.sub(r"\s{2,}", " ", best)[:250]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _boundaries(title):
    toks = [t for t in re.split(r"[^a-z0-9]+", title.lower()) if t]
    joined, offsets, pos = "", set(), 0
    for tok in toks:
        offsets.add(pos)
        joined += tok
        pos += len(tok)
    return joined, offsets


def _relevant(title, terms):
    joined, offsets = _boundaries(title)
    for term in terms:
        i = joined.find(term)
        while i != -1 and i not in offsets:
            i = joined.find(term, i + 1)
        if i == -1:
            return False
    return True


def _parse(text):
    out = []
    chunks = _SPLIT.split(text)[1:]
    total = len(chunks)
    for c in chunks:
        if "search-subject-hdr" in c:  # header row, not a result
            continue
        mid = _ID.search(c) or _ID2.search(c)
        msub = _SUBJECT.search(c)
        if not (mid and msub):
            continue
        # password gate
        if _HASPASS.search(c) and not _PASSWORD.search(c):
            continue
        # completeness gate
        mp = _PARTS.search(c)
        if mp:
            have, tot = int(mp.group(1)), int(mp.group(2))
            if tot and have / tot < MIN_COMPLETE:
                continue
        # require an actual video file among the filetypes -> kills fakes/zip/nfo-only
        mf = _FILETYPES.search(c)
        exts = set(e.lower() for e in _EXT.findall(mf.group(1))) if mf else set()
        if exts and not (exts & _VIDEO_EXT):
            continue
        ms = _SIZE.search(c)
        size = int(float(ms.group(1)) * _UNITS.get(ms.group(2).upper(), 1)) if ms else 0
        if size and size < MIN_SIZE_MB * 1024 * 1024:
            continue
        title = _release_title(msub.group(1))
        if not _RELEASEY.search(title):
            continue
        ma = _AGE.search(c)
        age = int(ma.group(1)) * _AGE_UNITS.get(ma.group(2).lower(), 86400) if ma else 0
        mg = _GROUP.search(c)
        out.append({"id": mid.group(1), "title": title, "size": size,
                    "age": age, "group": _clean(mg.group(1)) if mg else ""})
    return out, total


def search(query, limit):
    key = (query.lower(), min(limit, MAX_RESULTS))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    url = "%s/search/?%s" % (BASE, urlencode({"q": query}))
    try:
        r = _sess.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("search failed %r: %s", query, e)
        return []
    items, total = _parse(r.text)
    items = items[:key[1]]
    log.info("search %r -> %d usable of %d results", query, len(items), total)
    _bump(searches=1, results=len(items), last_query=query[:60], last_ts=now)
    with _lock:
        _cache[key] = (now, items)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]:
                del _cache[k]
    return items


CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server appversion="1.0" version="0.1" title="NZBKing" strapline="NZBKing newznab shim"/>
  <limits max="100" default="100"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q"/>
    <audio-search available="yes" supportedParams="q"/>
    <book-search available="no" supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2030" name="SD"/><subcat id="2040" name="HD"/></category>
    <category id="5000" name="TV"><subcat id="5030" name="SD"/><subcat id="5040" name="HD"/></category>
    <category id="3000" name="Audio"><subcat id="3010" name="MP3"/></category>
  </categories>
</caps>"""


@app.route("/api")
def api():
    t = (request.args.get("t") or "search").lower()
    if t == "caps":
        return Response(CAPS, mimetype="application/xml")
    q = base_q = (request.args.get("q") or "").strip()
    season, ep = request.args.get("season"), request.args.get("ep")
    try:
        if q and season and ep:
            q = "%s S%02dE%02d" % (q, int(season), int(ep))
        elif q and season:
            q = "%s S%02d" % (q, int(season))
    except (TypeError, ValueError):
        pass
    cat = (request.args.get("cat") or "5000").split(",")[0]
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        limit = 100

    items = search(q or RSS_TERMS.get(cat[:1], RSS_TERMS["5"]), limit)
    if base_q:
        terms = [t2 for t2 in (_norm(w) for w in re.split(r"[\s.+_-]+", base_q) if len(w) > 1) if t2]
        if terms:
            items = [i for i in items if _relevant(i["title"], terms)]

    parts = []
    for it in items:
        pub = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - it["age"]))
        link = "%s/getnzb?id=%s" % (SELF_URL, quote(it["id"]))
        parts.append("""    <item>
      <title>%s</title>
      <guid isPermaLink="false">%s</guid>
      <link>%s</link>
      <pubDate>%s</pubDate>
      <category>%s</category>
      <enclosure url="%s" length="%d" type="application/x-nzb"/>
      <newznab:attr name="category" value="%s"/>
      <newznab:attr name="size" value="%d"/>
      <newznab:attr name="group" value="%s"/>
      <newznab:attr name="guid" value="%s"/>
    </item>""" % (xml_escape(it["title"]), xml_escape(it["id"]), xml_escape(link),
                  pub, xml_escape(cat), xml_escape(link), it["size"], xml_escape(cat),
                  it["size"], xml_escape(it["group"]), xml_escape(it["id"])))

    body = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>NZBKing</title>
    <description>NZBKing newznab shim</description>
    <link>%s</link>
    <newznab:response offset="0" total="%d"/>
%s
  </channel>
</rss>""" % (xml_escape(SELF_URL), len(parts), "\n".join(parts))
    return Response(body, mimetype="application/rss+xml")


@app.route("/getnzb")
def getnzb():
    nid = request.args.get("id") or ""
    if not re.fullmatch(r"[a-f0-9]{8,}", nid):
        return "bad id", 400
    try:
        r = _sess.get("%s/nzb:%s/" % (BASE, nid), timeout=TIMEOUT)
    except Exception as e:
        log.warning("nzb fetch failed %s: %s", nid[:12], e)
        return "upstream error", 502
    if r.status_code != 200 or not r.content.lstrip()[:5].lower().startswith(b"<?xml"):
        log.warning("nzb bad response %s: HTTP %s", nid[:12], r.status_code)
        return "not an nzb", 502
    log.info("nzb %s -> %d bytes", nid[:12], len(r.content))
    _bump(nzbs=1)
    return Response(r.content, mimetype="application/x-nzb",
                    headers={"Content-Disposition": 'attachment; filename="%s.nzb"' % nid[:24]})


@app.route("/stats")
def stats():
    with _slock:
        s = dict(_stats)
    with _lock:
        cached = len(_cache)
    last = s.get("last_ts") or 0
    seen = ("just now" if last and (time.time() - last) < 60
            else "%dm ago" % int((time.time() - last) / 60) if last else "idle")
    return jsonify({"up": True, "searches_1h": len(s.get("search_ts", [])),
                    "nzbs_total": s.get("nzbs", 0), "cached": cached,
                    "last_query": s.get("last_query") or "-", "last_seen": seen})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "base": BASE})


if __name__ == "__main__":
    log.info("nzbking shim on :%d -> %s", PORT, BASE)
    waitress_serve(app, host="0.0.0.0", port=PORT, threads=12, channel_timeout=90)
