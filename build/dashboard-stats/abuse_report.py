"""Abuse-report helper: find the network owner's abuse contact for an attacking
IP and build an evidence-backed report.

Deliberately does NOT send anything. Mailing a third party is an outward-facing
act with real consequences (and a malformed or misdirected report is just spam),
so this prepares the message and the human presses send.
"""

import os
import re
import json
import time
import requests

AUTH_LOG = os.environ.get("AUTH_LOG", "/host/varlog/auth.log")
CACHE_FILE = os.environ.get("RDAP_CACHE", "/data/rdap_cache.json")
SERVER_NAME = os.environ.get("REPORT_SERVER_NAME", "my server")
REPORT_TZ = os.environ.get("TZ", "UTC")

# rdap.org is a convenient redirector but frequently times out; fall back to the
# RIRs directly. Whichever answers first with an abuse address wins.
RDAP_ENDPOINTS = [
    "https://rdap.org/ip/{ip}",
    "https://rdap.db.ripe.net/ip/{ip}",
    "https://rdap.arin.net/registry/ip/{ip}",
    "https://rdap.apnic.net/ip/{ip}",
    "https://rdap.lacnic.net/rdap/ip/{ip}",
    "https://rdap.afrinic.net/rdap/ip/{ip}",
]


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _extract(d):
    """Pull abuse emails + network description out of an RDAP response."""
    emails, all_emails = set(), set()

    def walk(entities):
        for e in entities or []:
            roles = [r.lower() for r in (e.get("roles") or [])]
            v = e.get("vcardArray")
            found_here = []
            if v and len(v) > 1:
                for item in v[1]:
                    if isinstance(item, list) and len(item) > 3 and item[0] == "email":
                        found_here.append(item[3])
            for addr in found_here:
                all_emails.add(addr)
                if "abuse" in roles or "abuse" in addr.lower():
                    emails.add(addr)
            walk(e.get("entities"))

    walk(d.get("entities"))
    if not emails:
        emails = {a for a in all_emails if "abuse" in a.lower()}
    return {
        "abuse_emails": sorted(emails),
        "other_emails": sorted(all_emails - emails),
        "network": d.get("name") or "",
        "country": d.get("country") or "",
        "handle": d.get("handle") or "",
        "cidr": _cidr(d),
    }


def _cidr(d):
    try:
        s, e = d.get("startAddress"), d.get("endAddress")
        if s and e:
            return f"{s} - {e}"
    except Exception:
        pass
    return ""


def lookup(ip, force=False):
    cache = _load_cache()
    hit = cache.get(ip)
    if hit and not force and time.time() - hit.get("_ts", 0) < 30 * 86400:
        return hit
    result = {"abuse_emails": [], "other_emails": [], "network": "",
              "country": "", "handle": "", "cidr": "", "error": ""}
    for tpl in RDAP_ENDPOINTS:
        try:
            r = requests.get(tpl.format(ip=ip), timeout=25,
                             headers={"Accept": "application/rdap+json"})
            if r.status_code != 200:
                continue
            got = _extract(r.json())
            if got["abuse_emails"] or got["network"]:
                result = got
                result["error"] = ""
                break
        except Exception as e:
            result["error"] = str(e)[:120]
            continue
    result["_ts"] = time.time()
    cache[ip] = result
    _save_cache(cache)
    return result


def evidence(ip, max_lines=12):
    """Real log lines for this IP, plus a summary, to attach to the report."""
    lines, first, last, count = [], None, None, 0
    users = {}
    try:
        size = os.path.getsize(AUTH_LOG)
        with open(AUTH_LOG, "rb") as f:
            if size > 8 * 1024 * 1024:
                f.seek(size - 8 * 1024 * 1024)
                f.readline()
            for raw in f:
                s = raw.decode("utf-8", "replace").rstrip()
                if ip not in s:
                    continue
                # sshd records ONE failure twice: "Failed password ..." from sshd
                # and "pam_unix(sshd:auth): authentication failure ..." from PAM.
                # Counting both (as an earlier version did) inflated 853 -> 1351.
                # Overstating evidence in a report to a third party is worse than
                # understating it, so count only the sshd lines.
                if "Failed password" not in s and "Invalid user" not in s:
                    continue
                count += 1
                ts = s.split()[0] if s else ""
                first = first or ts
                last = ts
                u = re.search(r"(?:Invalid user|password for(?: invalid user)?)\s+(\S+)", s)
                if u:
                    users[u.group(1)] = users.get(u.group(1), 0) + 1
                if len(lines) < max_lines:
                    lines.append(s)
    except Exception:
        pass
    top_users = sorted(users.items(), key=lambda kv: -kv[1])[:8]
    return {"count": count, "first": first or "?", "last": last or "?",
            "lines": lines, "usernames": top_users}


def build_report(ip, my_ip=""):
    who = lookup(ip)
    ev = evidence(ip)
    users = ", ".join(f"{u} ({n}x)" for u, n in ev["usernames"]) or "various"
    subject = f"Abuse report: SSH brute-force from {ip}"
    body = f"""Hello,

I am reporting sustained unauthorised SSH login attempts originating from an IP
address in your network.

  Offending IP : {ip}
  Network      : {who.get('network') or 'unknown'} {('(' + who['cidr'] + ')') if who.get('cidr') else ''}
  Country      : {who.get('country') or 'unknown'}

  Failed login attempts observed : {ev['count']}
  First seen : {ev['first']}
  Last seen  : {ev['last']}
  Usernames targeted : {users}

These are automated credential-guessing attempts against a private server. The
attempts are unsolicited and ongoing. Timestamps below are in {REPORT_TZ}.

Sample log entries (sshd, auth.log):

{chr(10).join('  ' + l for l in ev['lines']) or '  (no sample lines available)'}

Please investigate and take appropriate action against the customer or system
responsible. I am happy to supply additional logs on request.

Regards,
{SERVER_NAME} administrator
"""
    return {"ip": ip, "subject": subject, "body": body,
            "abuse_emails": who.get("abuse_emails", []),
            "other_emails": who.get("other_emails", []),
            "network": who.get("network", ""), "country": who.get("country", ""),
            "cidr": who.get("cidr", ""), "attempts": ev["count"],
            "lookup_error": who.get("error", "")}
