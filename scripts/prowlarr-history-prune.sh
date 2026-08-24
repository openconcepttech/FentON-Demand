#!/bin/bash
# Keep Prowlarr History table small (backfill logs ~90k rows/day). Stops prowlarr
# briefly for a safe sqlite trim to the last 2 days + vacuum, then restarts.
DB=/opt/media/config/prowlarr/prowlarr.db
docker stop prowlarr >/dev/null 2>&1
python3 - <<PY
import sqlite3, datetime, os
con=sqlite3.connect("$DB"); c=con.cursor()
cut=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
c.execute("DELETE FROM History WHERE Date < ?",(cut,)); con.commit()
con.isolation_level=None; c.execute("VACUUM"); con.close()
print("prowlarr history pruned, db %.0fMB"%(os.path.getsize("$DB")/1e6))
PY
docker start prowlarr >/dev/null 2>&1
