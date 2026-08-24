#!/usr/bin/env python3
"""Newznab front-end for BinSearch.

BinSearch has no API - it is a raw article index with an HTML UI. Sonarr/Radarr
speak newznab, so this translates: scrape /search, emit newznab XML, and proxy
/nzb downloads back out.

Two caveats shape the design:
  * BinSearch indexes raw articles, not curated releases. Results include spam,
    password-protected archives and incomplete posts. Incomplete ones are
    filtered out - grabbing one burns a slot and then fails to unpack.
  * There are no real categories. Everything is reported as the category the
    caller asked for, so Sonarr's own title parsing does the real filtering.
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

BASE = os.environ.get("BINSEARCH_URL", "https://binsearch.info")
PORT = int(os.environ.get("PORT", "8300"))
SELF_URL = os.environ.get("SELF_URL", "http://binsearch-shim:8300")
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "100"))
MIN_SIZE_MB = float(os.environ.get("MIN_SIZE_MB", "20"))
CACHE_TTL = float(os.environ.get("CACHE_TTL", "300"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "45"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("binsearch-shim")
app = Flask(__name__)

_sess = requests.Session()
_sess.headers.update({"User-Agent": UA})

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_ID = re.compile(r'name="([A-Za-z0-9+/=_-]{16,})"')
_TITLE = re.compile(r'href="/details/[^"]+"[^>]*>(.*?)</a>', re.S)
_SIZE_SPAN = re.compile(r"<span[^>]*>([\d.]+)\s*(B|KB|MB|GB|TB)</span>", re.I)
_SIZE_ANY = re.compile(r">([\d.]+)\s*(B|KB|MB|GB|TB)<", re.I)
_AGE = re.compile(r"<td[^>]*>\s*(\d+)\s*(hour|day|month|year)s?\s*</td>", re.I)
_GROUP = re.compile(r'/search\?group=([^"&]+)')
_INCOMPLETE = re.compile(r"incomplete", re.I)

_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_AGE_UNITS = {"hour": 3600, "day": 86400, "month": 2592000, "year": 31536000}

# Newznab RSS ("what's new") has no good BinSearch equivalent. Browsing a group
# returns mostly obfuscated posts - random subjects that no *arr can match - so
# a broad search term is a far better stand-in feed than a group listing.
RSS_TERMS = {
    "5": os.environ.get("RSS_TERM_TV", "1080p WEB"),
    "2": os.environ.get("RSS_TERM_MOVIES", "1080p BluRay"),
    "3": os.environ.get("RSS_TERM_AUDIO", "FLAC"),
}

# A release name Sonarr/Radarr can actually match has a year, a season/episode
# marker or a resolution. Everything else here is obfuscated spam or par2 noise.
_RELEASEY = re.compile(r"S\d{1,2}E\d{1,2}|\b\d{3,4}p\b|\b(19|20)\d{2}\b", re.I)
_JUNK = re.compile(r"\.vol\d+\+\d+|\.par2$|^[A-Za-z0-9+_-]{12,}$", re.I)

_cache = {}
_cache_lock = threading.Lock()

# Lightweight activity counters for the Homepage tile. Kept in-memory (reset on
# restart) - this is a status widget, not accounting.
_stats = {"searches": 0, "results": 0, "nzbs": 0,
          "last_query": "", "last_ts": 0.0, "search_ts": []}
_stats_lock = threading.Lock()


def _bump(**kw):
    now = time.time()
    with _stats_lock:
        for k, v in kw.items():
            if k in ("searches", "results", "nzbs"):
                _stats[k] += v
            else:
                _stats[k] = v
        if kw.get("searches"):
            _stats["search_ts"].append(now)
            cutoff = now - 3600
            _stats["search_ts"] = [t for t in _stats["search_ts"] if t >= cutoff]


def _clean(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _release_title(raw):
    """Pull something Sonarr can parse out of a Usenet subject line.

    Subjects look like:
      [378265]-[FULL]-[#a.b.teevee@EFNet]-[ Show.S01E02.1080p-GRP ]-[08/29] - "f.nfo" yEnc
    The bracketed segment holding the release name, or the quoted filename, is
    the part worth keeping.
    """
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
            # Strip repeatedly: "name.vol000+01.par2" needs two passes.
            for _ in range(3):
                stripped = re.sub(r"\.(nfo|par2|sfv|nzb|jpg|png|txt|rar|7z|zip"
                                  r"|vol\d+\+\d+|\d{3})$", "", best, flags=re.I)
                if stripped == best:
                    break
                best = stripped
    if not best:
        best = re.sub(r"^(\[[^\]]*\]-?)+", "", t).strip() or t
    best = re.sub(r"\s+yEnc.*$", "", best, flags=re.I).strip()
    return re.sub(r"\s{2,}", " ", best)[:250]


def _age_seconds(row):
    m = _AGE.search(row)
    if not m:
        return 0
    return int(m.group(1)) * _AGE_UNITS.get(m.group(2).lower(), 86400)


def _size_bytes(row):
    m = _SIZE_SPAN.search(row) or _SIZE_ANY.search(row)
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).upper(), 1))


def _parse(text):
    out = []
    rows = _ROW.findall(_COMMENT.sub("", text))
    for row in rows:
        if "/details/" not in row:
            continue
        # BinSearch flags partial posts; those never unpack cleanly.
        if _INCOMPLETE.search(row):
            continue
        mid, mt = _ID.search(row), _TITLE.search(row)
        if not (mid and mt):
            continue
        size = _size_bytes(row)
        if size < MIN_SIZE_MB * 1024 * 1024:
            continue
        title = _release_title(mt.group(1))
        # Obfuscated subjects and par2 volumes are grabbable but useless: no
        # *arr can match them to a series or movie, so they only waste slots.
        if _JUNK.search(title) or not _RELEASEY.search(title):
            continue
        mg = _GROUP.search(row)
        out.append({
            "id": mid.group(1),
            "title": title,
            "size": size,
            "age": _age_seconds(row),
            "group": _clean(mg.group(1)) if mg else "",
        })
    return out, len(rows)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _boundaries(title):
    """Collapse a release name to its bare alphanumerics, plus the offsets where
    each word began. Separator style varies wildly (Dune.Part.Two vs Dune Part
    Two vs DunePartTwo), so comparing the collapsed forms is the only reliable
    way - but only at word starts, or "Silo" matches the group tag "EPSiLON"."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", title.lower()) if t]
    joined, offsets, pos = "", set(), 0
    for tok in tokens:
        offsets.add(pos)
        joined += tok
        pos += len(tok)
    return joined, offsets


def _relevant(title, terms):
    """BinSearch full-text-matches every token, so a search for "Silo S02E01"
    happily returns Supergirl S02E01. Require the caller's own words to appear,
    each starting on a word boundary."""
    joined, offsets = _boundaries(title)
    for term in terms:
        i = joined.find(term)
        while i != -1 and i not in offsets:
            i = joined.find(term, i + 1)
        if i == -1:
            return False
    return True


def search(query, limit, group=""):
    key = (query.lower(), group, min(limit, MAX_RESULTS))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]

    params = {"group": group} if group else {"q": query}
    params["max"] = key[2]
    url = f"{BASE}/search?" + urlencode(params)
    try:
        r = _sess.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("search failed for %r: %s", query, e)
        return []

    items, total = _parse(r.text)
    log.info("search %r -> %d usable of %d rows", group or query, len(items), total)
    _bump(searches=1, results=len(items),
          last_query=(group or query)[:60], last_ts=time.time())
    with _cache_lock:
        _cache[key] = (now, items)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]:
                del _cache[k]
    return items


CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server appversion="1.0" version="0.1" title="BinSearch" strapline="BinSearch newznab shim"/>
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
    <category id="2000" name="Movies">
      <subcat id="2030" name="SD"/>
      <subcat id="2040" name="HD"/>
    </category>
    <category id="5000" name="TV">
      <subcat id="5030" name="SD"/>
      <subcat id="5040" name="HD"/>
    </category>
    <category id="3000" name="Audio">
      <subcat id="3010" name="MP3"/>
    </category>
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
        pass  # daily/anime series send non-numeric seasons; plain q still works
    cat = (request.args.get("cat") or "5000").split(",")[0]
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        limit = 100

    # A bare query is newznab's RSS feed: "what's new". See RSS_TERMS.
    items = search(q or RSS_TERMS.get(cat[:1], RSS_TERMS["5"]), limit)

    if q:
        # Only the caller's own words are required; the SxxExx we appended is
        # already implied by the search, and demanding it would drop packs.
        terms = [_norm(w) for w in re.split(r"[\s.+_-]+", base_q) if len(w) > 1]
        terms = [t for t in terms if t]
        if terms:
            items = [i for i in items if _relevant(i["title"], terms)]

    parts = []
    for it in items:
        pub = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(time.time() - it["age"]))
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
    </item>""" % (xml_escape(it["title"]), xml_escape(it["id"]),
                  xml_escape(link), pub, xml_escape(cat), xml_escape(link),
                  it["size"], xml_escape(cat), it["size"],
                  xml_escape(it["group"]), xml_escape(it["id"])))

    body = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>BinSearch</title>
    <description>BinSearch newznab shim</description>
    <link>%s</link>
    <newznab:response offset="0" total="%d"/>
%s
  </channel>
</rss>""" % (xml_escape(SELF_URL), len(parts), "\n".join(parts))
    return Response(body, mimetype="application/rss+xml")


@app.route("/getnzb")
def getnzb():
    nid = request.args.get("id") or ""
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{16,}", nid):
        return "bad id", 400
    try:
        r = _sess.get("%s/nzb" % BASE, params={"id": nid}, timeout=TIMEOUT)
    except Exception as e:
        log.warning("nzb fetch failed %s: %s", nid[:12], e)
        return "upstream error", 502
    if r.status_code != 200 or not r.content.lstrip()[:5].lower().startswith(b"<?xml"):
        log.warning("nzb fetch bad response %s: HTTP %s, %d bytes",
                    nid[:12], r.status_code, len(r.content))
        return "not an nzb", 502
    log.info("nzb %s -> %d bytes", nid[:12], len(r.content))
    _bump(nzbs=1)
    return Response(r.content, mimetype="application/x-nzb",
                    headers={"Content-Disposition":
                             'attachment; filename="%s.nzb"' % nid[:24]})


@app.route("/healthz")
def healthz():
    with _cache_lock:
        cached = len(_cache)
    return jsonify({"ok": True, "base": BASE, "cached_queries": cached})


@app.route("/stats")
def stats():
    """Flat JSON for the Homepage customapi tile."""
    with _stats_lock:
        s = dict(_stats)
    with _cache_lock:
        cached = len(_cache)
    last = s.get("last_ts") or 0
    if last:
        mins = int((time.time() - last) / 60)
        last_seen = "just now" if mins < 1 else "%dm ago" % mins
    else:
        last_seen = "idle"
    return jsonify({
        "up": True,
        "searches_1h": len(s.get("search_ts", [])),
        "searches_total": s.get("searches", 0),
        "nzbs_total": s.get("nzbs", 0),
        "cached": cached,
        "last_query": s.get("last_query") or "-",
        "last_seen": last_seen,
    })


if __name__ == "__main__":
    log.info("binsearch shim listening on :%d -> %s", PORT, BASE)
    waitress_serve(app, host="0.0.0.0", port=PORT, threads=12, channel_timeout=90)
