#!/usr/bin/env python3
"""Tiny stats aggregator for the Homepage dashboard.

slskd's API returns deeply nested per-file transfer data; Homepage's
customapi widget can only map flat single fields, so this computes
simple aggregates (active count, combined speed) and exposes them as
a flat JSON endpoint Homepage can point at directly.
"""

import os
import json
import time
from datetime import datetime
import html
import re
import urllib.parse
import requests
from flask import Flask, jsonify, request

import purge_lib
import security_mon
import abuse_report

app = Flask(__name__)

SLSKD_URL = os.environ["SLSKD_URL"]
SLSKD_API_KEY = os.environ["SLSKD_API_KEY"]
AIRDCPP_URL = os.environ.get("AIRDCPP_URL", "")
AIRDCPP_USER = os.environ.get("AIRDCPP_USER", "")
AIRDCPP_PASS = os.environ.get("AIRDCPP_PASS", "")


@app.route("/slskd/stats")
def slskd_stats():
    active = 0
    queued = 0
    completed = 0
    errored = 0
    total_speed = 0.0
    try:
        r = requests.get(
            f"{SLSKD_URL}/api/v0/transfers/downloads",
            headers={"X-API-Key": SLSKD_API_KEY},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        for user in data:
            for d in user.get("directories", []):
                for f in d.get("files", []):
                    st = f.get("state", "")
                    if "InProgress" in st:
                        active += 1
                        total_speed += f.get("averageSpeed", 0) or 0
                    elif "Queued" in st:
                        queued += 1
                    elif "Succeeded" in st:
                        completed += 1
                    elif "Errored" in st or "Cancelled" in st or "Aborted" in st:
                        errored += 1
    except Exception as e:
        return jsonify({"error": str(e), "active": 0, "queued": 0,
                         "completed": 0, "errored": 0, "speed_mbps": 0})

    return jsonify({
        "active": active,
        "queued": queued,
        "completed": completed,
        "errored": errored,
        "speed_mbps": round(total_speed / 1_000_000, 2),
    })


@app.route("/airdcpp/stats")
def airdcpp_stats():
    try:
        r = requests.get(
            f"{AIRDCPP_URL}/api/v1/transfers/stats",
            auth=(AIRDCPP_USER, AIRDCPP_PASS),
            timeout=8,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return jsonify({"error": str(e), "downloads": 0, "download_bundles": 0,
                         "speed_down_mbps": 0, "speed_up_mbps": 0})

    return jsonify({
        "downloads": d.get("downloads", 0),
        "download_bundles": d.get("download_bundles", 0),
        "speed_down_mbps": round(d.get("speed_down", 0) / 1_000_000, 2),
        "speed_up_mbps": round(d.get("speed_up", 0) / 1_000_000, 2),
    })


SYS = os.environ.get("HOST_SYS", "/host/sys")
NIC = os.environ.get("HOST_NIC", "eno1")
WIFI_NIC = os.environ.get("HOST_WIFI_NIC", "wlp58s0")
_net_prev = {}


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


@app.route("/system/stats")
def system_stats():
    out = {}

    # --- wired NIC: link speed + live throughput -------------------------
    speed = _read(f"{SYS}/class/net/{NIC}/speed")
    out["link_mbps"] = int(speed) if speed and speed.lstrip("-").isdigit() else 0
    out["link_type"] = ({10: "10 Mb", 100: "100 Mb", 1000: "1 Gb", 2500: "2.5 Gb",
                          10000: "10 Gb"}.get(out["link_mbps"], f"{out['link_mbps']} Mb")
                         if out["link_mbps"] > 0 else "down")
    out["duplex"] = _read(f"{SYS}/class/net/{NIC}/duplex", "n/a")
    out["link_state"] = _read(f"{SYS}/class/net/{NIC}/operstate", "unknown")

    rx = _read(f"{SYS}/class/net/{NIC}/statistics/rx_bytes")
    tx = _read(f"{SYS}/class/net/{NIC}/statistics/tx_bytes")
    now = time.time()
    if rx and tx:
        rx, tx = int(rx), int(tx)
        prev = _net_prev.get(NIC)
        if prev and now > prev["t"]:
            dt = now - prev["t"]
            out["rx_mbps"] = round((rx - prev["rx"]) * 8 / 1e6 / dt, 2)
            out["tx_mbps"] = round((tx - prev["tx"]) * 8 / 1e6 / dt, 2)
        else:
            out["rx_mbps"] = 0.0
            out["tx_mbps"] = 0.0
        _net_prev[NIC] = {"rx": rx, "tx": tx, "t": now}
    else:
        out["rx_mbps"] = out["tx_mbps"] = 0.0

    # --- wifi (hardware present on this box but normally unused) ---------
    wstate = _read(f"{SYS}/class/net/{WIFI_NIC}/operstate")
    if wstate is None:
        out["wifi"] = "not present"
    elif wstate != "up":
        out["wifi"] = f"{wstate} (unused)"
    else:
        wspeed = _read(f"{SYS}/class/net/{WIFI_NIC}/speed")
        out["wifi"] = f"up {wspeed} Mb" if wspeed and wspeed.isdigit() else "up"

    # --- Intel iGPU: frequency only (intel_gpu_top not installed, so no busy%) ---
    cur = _read(f"{SYS}/class/drm/card1/gt_cur_freq_mhz")
    mx = _read(f"{SYS}/class/drm/card1/gt_max_freq_mhz")
    out["gpu_mhz"] = int(cur) if cur and cur.isdigit() else 0
    out["gpu_max_mhz"] = int(mx) if mx and mx.isdigit() else 0
    out["gpu_pct_of_max"] = (round(100 * out["gpu_mhz"] / out["gpu_max_mhz"])
                              if out["gpu_max_mhz"] else 0)

    # --- CPU temperature -------------------------------------------------
    t = _read(f"{SYS}/class/thermal/thermal_zone0/temp")
    out["cpu_temp_c"] = round(int(t) / 1000) if t and t.isdigit() else 0

    return jsonify(out)


PROC = os.environ.get("HOST_PROC", "/host/proc")
DOCKER_API = os.environ.get("DOCKER_API", "http://docker-proxy:2375")
ZOMBIE_ALERT = int(os.environ.get("ZOMBIE_ALERT", "50"))


def container_health():
    """Read-only: containers, health, and zombie counts attributed to each."""
    # zombie PIDs -> parent pid, from host /proc
    zombie_by_ppid = {}
    try:
        for pid in os.listdir(PROC):
            if not pid.isdigit():
                continue
            stat = _read(f"{PROC}/{pid}/stat")
            if not stat:
                continue
            # state is the field after the (comm) block; comm can contain spaces
            try:
                after = stat[stat.rindex(")") + 2:].split()
                if after[0] == "Z":
                    ppid = int(after[1])
                    zombie_by_ppid[ppid] = zombie_by_ppid.get(ppid, 0) + 1
            except Exception:
                continue
    except Exception:
        pass

    # map parent pid -> container id via its cgroup
    zombies_by_cid = {}
    for ppid, n in zombie_by_ppid.items():
        cg = _read(f"{PROC}/{ppid}/cgroup", "")
        cid = None
        for token in (cg or "").replace("/", " ").replace("-", " ").replace(".", " ").split():
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
                cid = token[:12]
                break
        if cid:
            zombies_by_cid[cid] = zombies_by_cid.get(cid, 0) + n

    out = []
    try:
        r = requests.get(f"{DOCKER_API}/containers/json?all=0", timeout=10)
        r.raise_for_status()
        for c in r.json():
            cid = c["Id"][:12]
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            status = c.get("Status", "")
            state = c.get("State", "")
            health = "healthy"
            if "unhealthy" in status:
                health = "unhealthy"
            elif "health: starting" in status:
                health = "starting"
            elif state != "running":
                health = state
            z = zombies_by_cid.get(cid, 0)
            stale = (health == "unhealthy") or (z >= ZOMBIE_ALERT)
            out.append({"name": name, "id": cid, "status": status,
                         "health": health, "zombies": z, "stale": stale})
    except Exception as e:
        return {"error": str(e), "containers": []}

    out.sort(key=lambda c: (not c["stale"], -c["zombies"], c["name"]))
    return {"containers": out,
            "stale_count": sum(1 for c in out if c["stale"]),
            "total_zombies": sum(c["zombies"] for c in out)}


@app.route("/system/health")
def system_health_json():
    return jsonify(container_health())


@app.route("/apps")
def apps_page():
    h = container_health()
    if h.get("error"):
        return f"{PAGE_CSS}<h1>App health</h1><p class='warn'>Docker proxy unreachable: " \
               f"{html.escape(h['error'])}</p>", 500

    rows = ""
    for c in h["containers"]:
        cls = "warn" if c["stale"] else "ok"
        flag = ""
        if c["health"] == "unhealthy":
            flag = "unhealthy"
        elif c["zombies"] >= ZOMBIE_ALERT:
            flag = f"leaking {c['zombies']} zombies"
        btn = ""
        if c["stale"]:
            btn = (f"<form method='POST' action='/apps/restart' style='margin:0'>"
                   f"<input type='hidden' name='name' value='{html.escape(c['name'])}'>"
                   f"<button class='btn' style='padding:5px 12px;font-size:12px'>Restart</button></form>")
        rows += (f"<tr><td>{html.escape(c['name'])}</td>"
                 f"<td class='{cls}'>{html.escape(c['health'])}</td>"
                 f"<td>{c['zombies'] or ''}</td>"
                 f"<td class='warn'>{flag}</td><td>{btn}</td></tr>")

    bulk = ""
    if h["stale_count"]:
        bulk = ("<form method='POST' action='/apps/restart_all_stale' "
                "onsubmit=\"this.querySelector('button').disabled=true;"
                "this.querySelector('button').textContent='Restarting…';\">"
                f"<button class='btn'>Restart all {h['stale_count']} stale app(s)</button></form>")
    else:
        bulk = "<p class='ok'>All applications healthy — nothing stale.</p>"

    return f"""{PAGE_CSS}
<h1>Application health</h1>
<div class='sub'>Flags apps that are unhealthy or leaking &ge;{ZOMBIE_ALERT} zombie processes.</div>
<div class='card'><div class='stats'>
  <div><div class='big'>{len(h['containers'])}</div><div class='lbl'>Running</div></div>
  <div><div class='big warn'>{h['stale_count']}</div><div class='lbl'>Stale</div></div>
  <div><div class='big'>{h['total_zombies']}</div><div class='lbl'>Zombie procs</div></div>
</div></div>
<div class='card'>{bulk}</div>
<div class='card'><table>
<tr><th>App</th><th>Health</th><th>Zombies</th><th>Issue</th><th></th></tr>
{rows}</table></div>
"""


def _restart(name):
    r = requests.post(f"{DOCKER_API}/containers/{name}/restart", timeout=90)
    return r.status_code in (204, 304)


@app.route("/apps/restart", methods=["POST"])
def apps_restart():
    name = request.form.get("name", "")
    ok = False
    try:
        ok = _restart(name)
    except Exception as e:
        return f"{PAGE_CSS}<h1 class='warn'>Restart failed</h1><p>{html.escape(str(e))}</p>" \
               "<p><a href='/apps'>Back</a></p>", 500
    msg = f"Restarted {html.escape(name)}." if ok else f"Could not restart {html.escape(name)}."
    return f"{PAGE_CSS}<h1 class='{'ok' if ok else 'warn'}'>{msg}</h1>" \
           "<a class='btn grey' href='/apps'>Back</a>"


@app.route("/apps/restart_all_stale", methods=["POST"])
def apps_restart_all():
    h = container_health()
    done, failed = [], []
    for c in h.get("containers", []):
        if not c["stale"]:
            continue
        try:
            (done if _restart(c["name"]) else failed).append(c["name"])
        except Exception:
            failed.append(c["name"])
    return f"""{PAGE_CSS}
<h1 class='ok'>Restarted {len(done)} app(s)</h1>
<div class='card'><p>{html.escape(', '.join(done)) or 'none'}</p>
{"<p class='warn'>Failed: " + html.escape(', '.join(failed)) + "</p>" if failed else ""}</div>
<a class='btn grey' href='/apps'>Re-check</a>
"""


PAGE_CSS = """
<style>
 body{background:#0f172a;color:#e2e8f0;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#94a3b8;margin-bottom:20px}
 .card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:16px}
 .big{font-size:28px;font-weight:600} .lbl{color:#94a3b8;font-size:12px;text-transform:uppercase}
 .stats{display:flex;gap:32px;flex-wrap:wrap}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th{text-align:left;color:#94a3b8;font-weight:500;padding:6px 8px;border-bottom:1px solid #334155}
 td{padding:5px 8px;border-bottom:1px solid #1e293b}
 .btn{display:inline-block;background:#dc2626;color:#fff;border:0;border-radius:8px;
      padding:11px 20px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
 .btn.grey{background:#334155} .ok{color:#4ade80} .warn{color:#fbbf24}
 a{color:#60a5fa}
</style>
"""


@app.route("/purge")
def purge_preview():
    try:
        a = purge_lib.analyze()
    except Exception as e:
        return f"{PAGE_CSS}<h1>Purge</h1><p class='warn'>Could not reach qBittorrent: {html.escape(str(e))}</p>", 500

    rows = "".join(
        f"<tr><td>{html.escape(d['name'])}</td><td>{html.escape(d['state'])}</td>"
        f"<td>{d['progress']}%</td><td>{d['seeds']}</td><td>{d.get('availability','-')}</td>"
        f"<td>{d.get('active_days','-')}d</td>"
        f"<td>{html.escape(d['reason'])}</td></tr>"
        for d in a["dead"][:200]
    ) or "<tr><td colspan=7 class='ok'>Nothing stale found — queue is healthy.</td></tr>"

    more = ""
    if a["dead_count"] > 200:
        more = f"<p class='sub'>…and {a['dead_count']-200} more not shown.</p>"

    button = ""
    if a["dead_count"]:
        button = ("<form method='POST' action='/purge/run' "
                  "onsubmit=\"this.querySelector('button').disabled=true;"
                  "this.querySelector('button').textContent='Working… this can take a minute';\">"
                  f"<button class='btn' type='submit'>Remove {a['dead_count']} stale torrents "
                  "&amp; search for replacements</button></form>")

    return f"""{PAGE_CSS}
<h1>Clear stale / seedless downloads</h1>
<div class='sub'>Preview only — nothing is removed until you press the button.</div>
<div class='card'><div class='stats'>
  <div><div class='big'>{a['total_torrents']}</div><div class='lbl'>Total torrents</div></div>
  <div><div class='big warn'>{a['dead_count']}</div><div class='lbl'>Stale / seedless</div></div>
  <div><div class='big'>{a['reclaimable_mb']:.0f} MB</div><div class='lbl'>Partial data to reclaim</div></div>
</div></div>
<div class='card'>{button}
<p class='sub' style='margin:12px 0 0'>Removed items are blocklisted in Sonarr/Radarr (so the same
dead release isn't grabbed again) and a search for a replacement release is kicked off automatically.
Torrents the *arrs don't track are deleted straight from qBittorrent along with their partial data.</p></div>
<div class='card'><table>
<tr><th>Name</th><th>State</th><th>Done</th><th>Seeds<br><span style='font-weight:400;font-size:10px'>connected</span></th>
<th>Avail<br><span style='font-weight:400;font-size:10px'>&lt;1 = can't finish</span></th><th>Active</th><th>Why</th></tr>
{rows}</table>{more}</div>
"""


@app.route("/purge/run", methods=["POST"])
def purge_run():
    try:
        r = purge_lib.purge()
    except Exception as e:
        return f"{PAGE_CSS}<h1>Purge failed</h1><p class='warn'>{html.escape(str(e))}</p>" \
               "<p><a href='/purge'>Back</a></p>", 500
    return f"""{PAGE_CSS}
<h1 class='ok'>Done</h1>
<div class='card'>
  <p>{html.escape(r['message'])}</p>
  <div class='stats' style='margin-top:12px'>
    <div><div class='big'>{r['removed_via_arr']}</div><div class='lbl'>Removed via *arr</div></div>
    <div><div class='big'>{r['removed_via_qbit']}</div><div class='lbl'>Removed via qBittorrent</div></div>
    <div><div class='big'>{r['researched']}</div><div class='lbl'>Replacement searches</div></div>
  </div>
</div>
<a class='btn grey' href='/purge'>Re-check</a>
"""


@app.route("/security/stats")
def security_stats():
    try:
        d = security_mon.collect()
    except Exception as e:
        return jsonify({"error": str(e), "failed_24h": 0, "failed_1h": 0,
                         "unique_ips_24h": 0, "top_offender": "-"})
    return jsonify({k: d.get(k) for k in
                    ("failed_24h", "failed_1h", "unique_ips_24h", "attacking_now",
                     "top_offender", "top_offender_hits", "banned_24h",
                     "currently_banned")})


@app.route("/security")
def security_page():
    try:
        d = security_mon.collect()
    except Exception as e:
        return f"{PAGE_CSS}<h1>Attack monitor</h1><p class='warn'>{html.escape(str(e))}</p>", 500

    rows = "".join(
        f"<tr><td><code>{html.escape(x['ip'])}</code></td><td class='warn'>{x['attempts']}</td>"
        f"<td>{html.escape(x['last'])}</td>"
        f"<td><a href='/security/report/{html.escape(x['ip'])}'>prepare abuse report</a></td></tr>"
        for x in d["top_ips"]
    ) or "<tr><td colspan=4 class='ok'>No external failed logins in the last 24h.</td></tr>"

    users = "".join(
        f"<tr><td><code>{html.escape(x['user'])}</code></td><td>{x['n']}</td></tr>"
        for x in d["top_usernames"]
    ) or "<tr><td colspan=2>-</td></tr>"

    svc = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
                  for k, v in d["by_service"].items()) or "<tr><td colspan=2>-</td></tr>"

    return f"""{PAGE_CSS}
<h1>Attack monitor</h1>
<div class='sub'>External failed-login attempts against exposed services (last 24h).
Internal LAN/docker addresses are excluded.</div>
<div class='card'><div class='stats'>
  <div><div class='big warn'>{d['failed_24h']}</div><div class='lbl'>Failed logins 24h</div></div>
  <div><div class='big'>{d['failed_1h']}</div><div class='lbl'>Last hour</div></div>
  <div><div class='big'>{d['unique_ips_24h']}</div><div class='lbl'>Unique IPs</div></div>
  <div><div class='big'>{d['attacking_now']}</div><div class='lbl'>Active last hour</div></div>
  <div><div class='big ok'>{d.get('currently_banned',0)}</div><div class='lbl'>Banned now (fail2ban)</div></div>
  <div><div class='big ok'>{d.get('banned_24h',0)}</div><div class='lbl'>Bans issued 24h</div></div>
</div></div>
<div class='card'><h3 style='margin:0 0 8px;font-size:14px'>Worst offenders</h3>
<table><tr><th>Source IP</th><th>Attempts</th><th>Last seen</th><th>Abuse</th></tr>{rows}</table></div>
<div class='card'><h3 style='margin:0 0 8px;font-size:14px'>By service</h3>
<table><tr><th>Service</th><th>Attempts</th></tr>{svc}</table></div>
<div class='card'>
<h3 style='margin:0 0 8px;font-size:14px'>Usernames being guessed
  <span style='font-weight:400;color:#94a3b8'>&mdash; {d.get('unique_usernames',0)} distinct</span></h3>
<table><tr><th>Username</th><th>Attempts</th></tr>{users}</table>
<p style='margin:10px 0 0'><a href='/security/usernames'>View all {d.get('unique_usernames',0)} usernames &rarr;</a></p></div>
"""


@app.route("/security/report/<ip>")
def security_report(ip):
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        return "bad ip", 400
    try:
        r = abuse_report.build_report(ip)
    except Exception as e:
        return f"{PAGE_CSS}<h1>Abuse report</h1><p class='warn'>{html.escape(str(e))}</p>", 500

    to = ",".join(r["abuse_emails"])
    mailto = ("mailto:" + urllib.parse.quote(to) +
              "?subject=" + urllib.parse.quote(r["subject"]) +
              "&body=" + urllib.parse.quote(r["body"]))

    if r["abuse_emails"]:
        contact = ("<b>Abuse contact:</b> " +
                   ", ".join(f"<code>{html.escape(e)}</code>" for e in r["abuse_emails"]))
        btn = f"<a class='btn' href=\"{html.escape(mailto)}\">Open in mail client</a>"
    else:
        alt = (", ".join(f"<code>{html.escape(e)}</code>" for e in r["other_emails"])
               or "none found")
        contact = (f"<span class='warn'>No abuse role address published for this network.</span>"
                   f"<br>Other contacts on record: {alt}")
        btn = ("<span class='warn'>No abuse address — try the RIR's own abuse form, "
               "or report to AbuseIPDB.</span>")

    err = (f"<p class='warn'>Lookup note: {html.escape(r['lookup_error'])}</p>"
           if r.get("lookup_error") else "")

    return f"""{PAGE_CSS}
<h1>Abuse report — {html.escape(ip)}</h1>
<div class='sub'>Nothing is sent automatically. Review the text, then send it yourself.</div>
<div class='card'>
  <div class='stats'>
    <div><div class='big warn'>{r['attempts']}</div><div class='lbl'>Failed attempts logged</div></div>
    <div><div class='big' style='font-size:18px'>{html.escape(r['network'] or '?')}</div><div class='lbl'>Network</div></div>
    <div><div class='big' style='font-size:18px'>{html.escape(r['country'] or '?')}</div><div class='lbl'>Country</div></div>
  </div>
  <p style='margin-top:12px'>{contact}</p>
  {err}
  <p style='margin-top:12px'>{btn}
     <a class='btn grey' href='/security' style='margin-left:8px'>Back</a></p>
</div>
<div class='card'>
  <h3 style='margin:0 0 8px;font-size:14px'>Report text (copy/paste)</h3>
  <textarea style='width:100%;height:340px;background:#0f172a;color:#e2e8f0;
    border:1px solid #334155;border-radius:8px;padding:10px;font:12px/1.5 monospace'
    onclick='this.select()'>{html.escape(r['subject'])}

{html.escape(r['body'])}</textarea>
</div>
"""


SHIM_URL = os.environ.get("SHIM_URL", "http://slskd-shim:8200")


@app.route("/shim/stats")
def shim_stats():
    """Soulseek downloads that Sonarr/Radarr/Lidarr track through the shim."""
    try:
        r = requests.get(f"{SHIM_URL}/api/v2/torrents/info", timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return jsonify({"error": str(e), "tracked": 0, "downloading": 0,
                         "queued": 0, "done": 0, "speed_mbps": 0})
    downloading = [x for x in d if x.get("state") == "downloading"]
    return jsonify({
        "tracked": len(d),
        "downloading": len(downloading),
        "queued": len([x for x in d if x.get("state") == "queuedDL"]),
        "done": len([x for x in d if x.get("state") == "pausedUP"]),
        "speed_mbps": round(sum(x.get("dlspeed", 0) for x in downloading) / 1e6, 2),
    })


@app.route("/security/usernames")
def security_usernames():
    try:
        d = security_mon.collect()
    except Exception as e:
        return f"{PAGE_CSS}<h1>Usernames</h1><p class='warn'>{html.escape(str(e))}</p>", 500

    all_u = d.get("all_usernames", [])
    rows = "".join(
        f"<tr><td>{i+1}</td><td><code>{html.escape(x['user'])}</code></td>"
        f"<td class='warn'>{x['n']}</td></tr>"
        for i, x in enumerate(all_u)
    ) or "<tr><td colspan=3 class='ok'>None recorded.</td></tr>"

    total = sum(x["n"] for x in all_u)
    return f"""{PAGE_CSS}
<h1>All usernames guessed</h1>
<div class='sub'>Every distinct username tried by external hosts in the last 24h,
ordered by attempt count. Type in the box to filter.</div>
<div class='card'><div class='stats'>
  <div><div class='big'>{len(all_u)}</div><div class='lbl'>Distinct usernames</div></div>
  <div><div class='big warn'>{total}</div><div class='lbl'>Total attempts</div></div>
</div>
<p style='margin-top:12px'><a class='btn grey' href='/security'>Back to attack monitor</a></p></div>
<div class='card'>
  <input id='f' placeholder='filter usernames…' oninput="
    var v=this.value.toLowerCase();
    document.querySelectorAll('#ut tbody tr').forEach(function(r){{
      r.style.display = r.cells[1].innerText.toLowerCase().indexOf(v)>-1 ? '' : 'none';
    }});"
    style='width:100%;padding:9px;margin-bottom:10px;background:#0f172a;color:#e2e8f0;
           border:1px solid #334155;border-radius:8px'>
  <div style='max-height:70vh;overflow:auto'>
  <table id='ut'><thead><tr><th>#</th><th>Username</th><th>Attempts</th></tr></thead>
  <tbody>{rows}</tbody></table></div>
</div>
"""


@app.route("/reaper/stats")
def reaper_stats():
    """Reaper health for the Homepage Maintenance panel.

    Reads the log and archive index rather than talking to the container, so a
    stopped reaper still reports (running=false) instead of erroring.
    """
    out = {"running": False, "reaped_1h": 0, "reaped_24h": 0, "archived": 0,
           "archive_mb": 0.0, "forced": 0, "last_run": "", "last_line": ""}

    # "running" is inferred from log freshness: the image has no curl and the
    # docker socket is root-owned, but the reaper writes every REAP_INTERVAL
    # (180s), so a line within 10 minutes means it is alive.

    log = os.environ.get("REAPER_LOG", "/host/reaperlog/reaper.log")
    now = time.time()
    try:
        with open(log, "rb") as f:
            size = os.path.getsize(log)
            if size > 512 * 1024:
                f.seek(size - 512 * 1024)
                f.readline()
            for raw in f:
                line = raw.decode("utf-8", "replace").strip()
                m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]", line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").timestamp()
                except Exception:
                    continue
                out["last_run"] = m.group(1).replace("T", " ")
                if now - ts < 600:
                    out["running"] = True
                n = re.search(r"reaped: (\d+) via", line)
                if n:
                    age = now - ts
                    if age < 3600:
                        out["reaped_1h"] += int(n.group(1))
                    if age < 86400:
                        out["reaped_24h"] += int(n.group(1))
                    out["last_line"] = line[-70:]
    except Exception as e:
        out["error"] = str(e)[:80]

    idx = os.environ.get("REAPER_ARCHIVE_INDEX", "/host/reaperarchive/index.json")
    try:
        with open(idx) as f:
            out["archived"] = len(json.load(f))
        d = os.path.dirname(idx)
        out["archive_mb"] = round(sum(
            os.path.getsize(os.path.join(d, x)) for x in os.listdir(d)
            if os.path.isfile(os.path.join(d, x))) / 1048576.0, 1)
    except Exception as e:
        out["archive_error"] = f"{type(e).__name__}: {str(e)[:70]}"

    try:
        s2 = purge_lib.qbit_session()
        ts_ = s2.get(f"{purge_lib.QBIT_URL}/api/v2/torrents/info", timeout=25).json()
        out["forced"] = sum(1 for t in ts_ if t.get("force_start"))
        out["incomplete"] = sum(1 for t in ts_ if t.get("progress", 1) < 1)
    except Exception:
        pass

    return jsonify(out)


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8100)
