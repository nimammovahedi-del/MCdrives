"""Freeway detector stations in Orange County, from Caltrans PeMS vds_config.xml, for the traffic Worker.

    python worker/stations.py vds_config.xml worker/src/stations.json

Writes {"<station id>": [lat, lon, freeway, direction], ...}. The file's schema isn't documented, so this reads
any element that carries an id plus latitude/longitude attributes, and prints the first one it finds so a
deploy log shows what the file looked like. Stations outside Orange County are dropped.
"""
import sys, json
import xml.etree.ElementTree as ET

src, dst = sys.argv[1], sys.argv[2]
S, W, N, E = 33.38, -118.13, 33.95, -117.40
pick = lambda a, *ks: next((a[k] for k in ks if a.get(k) not in (None, "")), None)
out, shown = {}, False
for _, el in ET.iterparse(src, events=("end",)):
    a = el.attrib
    lat, lon, vid = pick(a, "latitude", "lat", "Latitude"), pick(a, "longitude", "lon", "lng", "Longitude"), pick(a, "id", "ID", "vds_id")
    if lat and lon and vid:
        if not shown: print("first station element:", el.tag, dict(a)); shown = True
        try: la, lo = float(lat), float(lon)
        except ValueError: continue
        if S <= la <= N and W <= lo <= E:
            out[str(vid)] = [round(la, 6), round(lo, 6), pick(a, "freeway_id", "freeway", "fwy") or "", pick(a, "freeway_dir", "direction", "dir") or ""]
    el.clear()
json.dump(out, open(dst, "w"), separators=(",", ":"))
print(f"{len(out)} Orange County stations -> {dst}")
