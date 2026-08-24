"""Attack monitor: aggregate failed-login attempts from every exposed service.

Sources:
  * sshd     -> /host/varlog/auth.log   ("Failed password", "Invalid user")
  * qBittorrent WebUI -> its own app log ("WebAPI login failure ... IP: ...")

auth.log runs to tens of MB, so only the tail is read and results are cached --
this is polled by a dashboard widget every few seconds.
"""

import os
import re
import time
from datetime import datetime
from collections import Counter

AUTH_LOG = os.environ.get("AUTH_LOG", "/host/varlog/auth.log")
QBIT_LOG = os.environ.get("QBIT_LOG", "/host/qbitlog/qbittorrent.log")
F2B_LOG = os.environ.get("F2B_LOG", "/host/varlog/fail2ban.log")
TAIL_BYTES = int(os.environ.get("SEC_TAIL_BYTES", str(6 * 1024 * 1024)))
CACHE_TTL = float(os.environ.get("SEC_CACHE_TTL", "60"))

_cache = {"ts": 0.0, "data": None}

_SSH_FAIL = re.compile(
    r"(?:Failed password for(?: invalid user)?|Invalid user)\s+\S+\s+from\s+"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})")
_QBIT_FAIL = re.compile(r"WebAPI login failure.*?IP:\s*(?:::ffff:)?(?P<ip>\d{1,3}(?:\.\d{1,3}){3})")
# rsyslog writes an offset-aware stamp (…T13:33:58.124625-07:00). Parsing only the
# naive part and calling mktime() interprets it in the CONTAINER's timezone (UTC),
# shifting every event by the host's UTC offset and making "last hour" always 0.
_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)")

PRIVATE = ("10.", "192.168.", "127.", "172.16.", "172.17.", "172.18.", "172.19.",
           "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
           "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")


def _is_external(ip):
    return not ip.startswith(PRIVATE)


def _tail(path, nbytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()          # discard partial line
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _parse_ts(value, anchored=True):
    """Return a POSIX timestamp from an ISO stamp, honouring its UTC offset."""
    m = _TS.match(value) if anchored else _TS.search(value)
    if not m:
        return None
    raw = m.group("ts").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:                      # naive -> assume host local time
        return dt.timestamp()
    return dt.timestamp()


def collect():
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    cutoff_24 = now - 86400
    cutoff_1 = now - 3600
    by_ip = Counter()
    by_ip_1h = Counter()
    by_service = Counter()
    users = Counter()
    last_seen = {}
    total_24 = total_1 = 0

    for line in _tail(AUTH_LOG, TAIL_BYTES):
        m = _SSH_FAIL.search(line)
        if not m:
            continue
        ip = m.group("ip")
        if not _is_external(ip):
            continue
        ts = _parse_ts(line)
        if ts is None or ts < cutoff_24:
            continue
        total_24 += 1
        by_ip[ip] += 1
        by_service["ssh"] += 1
        last_seen[ip] = max(last_seen.get(ip, 0), ts)
        u = re.search(r"(?:Invalid user|password for(?: invalid user)?)\s+(\S+)", line)
        if u:
            users[u.group(1)] += 1
        if ts >= cutoff_1:
            total_1 += 1
            by_ip_1h[ip] += 1

    for line in _tail(QBIT_LOG, 1024 * 1024):
        m = _QBIT_FAIL.search(line)
        if not m:
            continue
        ip = m.group("ip")
        if not _is_external(ip):
            continue
        ts = _parse_ts(line, anchored=False)
        if ts is None or ts < cutoff_24:
            continue
        total_24 += 1
        by_ip[ip] += 1
        by_service["qbittorrent"] += 1
        last_seen[ip] = max(last_seen.get(ip, 0), ts)
        if ts >= cutoff_1:
            total_1 += 1
            by_ip_1h[ip] += 1

    # --- fail2ban activity ---------------------------------------------------
    # Read its log rather than shelling out to fail2ban-client: that needs root
    # and this runs in a container. /var/log is already mounted read-only.
    banned_24h, unbanned_24h = 0, 0
    banned_ips = set()
    for line in _tail(F2B_LOG, 2 * 1024 * 1024):
        if "] Ban " not in line and "] Unban " not in line:
            continue
        mt = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        ts = None
        if mt:
            try:
                ts = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                ts = None
        if ts is None or ts < cutoff_24:
            continue
        mip = re.search(r"\] (?:Un)?Ban (\d{1,3}(?:\.\d{1,3}){3})", line)
        if "] Ban " in line:
            banned_24h += 1
            if mip:
                banned_ips.add(mip.group(1))
        else:
            unbanned_24h += 1
            if mip:
                banned_ips.discard(mip.group(1))

    top = [{"ip": ip, "attempts": n,
             "last": time.strftime("%H:%M", time.localtime(last_seen.get(ip, 0)))}
            for ip, n in by_ip.most_common(15)]

    data = {
        "failed_24h": total_24,
        "failed_1h": total_1,
        "unique_ips_24h": len(by_ip),
        "attacking_now": len(by_ip_1h),
        "top_offender": (top[0]["ip"] if top else "-"),
        "top_offender_hits": (top[0]["attempts"] if top else 0),
        "by_service": dict(by_service),
        "banned_24h": banned_24h,
        "currently_banned": len(banned_ips),
        "top_ips": top,
        "unique_usernames": len(users),
        "top_usernames": [{"user": u, "n": n} for u, n in users.most_common(15)],
        # full list for the dedicated page; capped so the JSON stays sane
        "all_usernames": [{"user": u, "n": n} for u, n in users.most_common(3000)],
    }
    _cache["ts"] = now
    _cache["data"] = data
    return data
