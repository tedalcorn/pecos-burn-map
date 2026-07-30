#!/usr/bin/env python3
"""Fetch Pecos Wilderness burn/trail data and write GeoJSON for the map.

Sources (all public ArcGIS REST, no auth):
  - USFS EDW Wilderness boundaries      -> wilderness.geojson
  - USFS EDW TrailNFS_Publish           -> trails.geojson
  - NIFC Interagency Fire Perimeter History (all years) -> fires.geojson
  - NIFC WFIGS Year-To-Date perimeters  -> merged into fires.geojson

Run: python3 scripts/fetch_data.py
Writes into docs/data/ plus meta.json with the fetch timestamp.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely.geometry import shape, mapping

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"

WILDERNESS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_Wilderness_01/MapServer/0/query"
TRAILS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0/query"
FIRE_HISTORY_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0/query"
# History layer lags (stops ~2021); this WFIGS layer covers 2021-present incl. year-to-date
FIRE_2021PLUS_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0/query"

# Degrees of margin around the wilderness bbox so approach trails
# (Winsor, Panchuela, Jacks Creek, Iron Gate) are included.
BBOX_MARGIN = 0.06
SIMPLIFY_TOL = 0.0002  # ~20 m; keeps GeoJSON small without visible distortion


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def query_all(url, params):
    """Page through an ArcGIS query endpoint, returning GeoJSON features."""
    features = []
    offset = 0
    while True:
        p = dict(params, f="geojson", resultOffset=offset, resultRecordCount=1000)
        r = requests.get(url, params=p, timeout=120)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{url}: {data['error']}")
        batch = data.get("features", [])
        features.extend(batch)
        if len(batch) < 1000:
            break
        offset += len(batch)
        log(f"  ...{len(features)} features so far")
    return features


def simplify_features(features, tol=SIMPLIFY_TOL):
    out = []
    for f in features:
        try:
            geom = shape(f["geometry"]).simplify(tol, preserve_topology=True)
            if geom.is_empty:
                continue
            f = dict(f, geometry=mapping(geom))
        except Exception:
            pass  # keep original geometry if simplification chokes
        out.append(f)
    return out


def round_coords(obj, ndigits=5):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [round_coords(x, ndigits) for x in obj]
    return obj


def write_geojson(name, features):
    for f in features:
        f["geometry"]["coordinates"] = round_coords(f["geometry"]["coordinates"])
    fc = {"type": "FeatureCollection", "features": features}
    path = OUT / f"{name}.geojson"
    path.write_text(json.dumps(fc, separators=(",", ":")))
    log(f"  wrote {path.name}: {len(features)} features, {path.stat().st_size/1e6:.1f} MB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    log("1/4 Pecos Wilderness boundary")
    wild = query_all(WILDERNESS_URL, {
        "where": "WILDERNESSNAME LIKE 'Pecos%'",
        "outFields": "WILDERNESSNAME,GIS_ACRES",
        "outSR": 4326,
    })
    if not wild:
        sys.exit("No wilderness polygon returned — aborting")
    write_geojson("wilderness", simplify_features(wild))

    # bbox (+margin) drives every other query
    geom = shape(wild[0]["geometry"])
    for f in wild[1:]:
        geom = geom.union(shape(f["geometry"]))
    xmin, ymin, xmax, ymax = geom.bounds
    bbox = f"{xmin-BBOX_MARGIN},{ymin-BBOX_MARGIN},{xmax+BBOX_MARGIN},{ymax+BBOX_MARGIN}"
    log(f"  AOI bbox: {bbox}")
    geo_params = {
        "geometry": bbox,
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": 4326,
    }

    log("2/4 USFS trails")
    trails = query_all(TRAILS_URL, dict(geo_params,
        where="1=1",
        outFields="TRAIL_NAME,TRAIL_NO,TRAIL_TYPE,GIS_MILES,TRAIL_SURFACE",
    ))
    write_geojson("trails", simplify_features(trails, tol=0.0001))

    log("3/4 Fire perimeter history (all years)")
    hist = query_all(FIRE_HISTORY_URL, dict(geo_params,
        where="1=1",
        outFields="INCIDENT,FIRE_YEAR,GIS_ACRES,MAP_METHOD",
    ))
    for f in hist:
        p = f["properties"]
        yr = p.get("FIRE_YEAR")
        try:
            yr = int(str(yr)[:4])
        except (TypeError, ValueError):
            yr = None
        f["properties"] = {
            "name": (p.get("INCIDENT") or "Unknown").title(),
            "year": yr,
            "acres": round(p["GIS_ACRES"]) if p.get("GIS_ACRES") else None,
            "src": "history",
        }

    log("4/4 Fire perimeters 2021-present (WFIGS)")
    recent = query_all(FIRE_2021PLUS_URL, dict(geo_params,
        where="1=1",
        outFields="poly_IncidentName,poly_GISAcres,attr_FireDiscoveryDateTime",
    ))
    for f in recent:
        p = f["properties"]
        disc = p.get("attr_FireDiscoveryDateTime")  # epoch ms
        yr = datetime.fromtimestamp(disc / 1000, tz=timezone.utc).year if disc else None
        f["properties"] = {
            "name": (p.get("poly_IncidentName") or "Unknown").title(),
            "year": yr,
            "acres": round(p["poly_GISAcres"]) if p.get("poly_GISAcres") else None,
            "src": "wfigs",
        }
    # Both layers cover 2021; drop WFIGS perimeters already in history (same name+year).
    seen = {(f["properties"]["name"], f["properties"]["year"]) for f in hist}
    ytd = [f for f in recent if (f["properties"]["name"], f["properties"]["year"]) not in seen]

    fires = simplify_features(hist + ytd)
    fires.sort(key=lambda f: f["properties"]["year"] or 0)
    write_geojson("fires", fires)

    meta = {
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {"trails": len(trails), "fires": len(fires)},
        "bbox": [xmin - BBOX_MARGIN, ymin - BBOX_MARGIN, xmax + BBOX_MARGIN, ymax + BBOX_MARGIN],
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))
    log("done")


if __name__ == "__main__":
    main()
