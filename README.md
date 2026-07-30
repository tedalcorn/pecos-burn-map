# Pecos Wilderness — Burn Areas & Trails

Living map of wildfire burn scars and USFS trails in and around the Pecos
Wilderness, NM. **Live at <https://tedalcorn.org/pecos-burn-map/>**

Successor to a 2024 Columbia Lede QGIS project (static shapefiles in
`~/Documents/2024 06 Columbia LEDE/-Burn project/`); this version re-fetches
its data from public APIs so it stays current.

## Data

`scripts/fetch_data.py` writes GeoJSON into `docs/data/`:

| Layer | Source |
|---|---|
| Fire perimeters ≤2021 | NIFC Interagency Fire Perimeter History (ArcGIS REST) |
| Fire perimeters 2021–present | NIFC WFIGS Interagency Perimeters (includes year-to-date) |
| Fire perimeters, gap-fill | USFS EDW MTBS burned areas (fires ≥1,000 ac; deduped by name+year & overlap) |
| Trails | USFS EDW `TrailNFS_Publish` |
| Trailheads | USFS EDW INFRA Recreation Sites (`SITE_SUBTYPE='TRAILHEAD'`, near-wilderness only) |
| Wilderness boundary | USFS EDW Wilderness |

The AOI is the Pecos Wilderness bounding box + 0.06° margin (captures approach
trails and trailheads). Geometries are simplified (~20 m) to keep files small.

A GitHub Action (`.github/workflows/refresh.yml`) re-runs the fetch on the 1st
of each month and commits any changes; GitHub Pages serves `docs/`.

## Caveats

- Perimeters ≠ burn severity: within a perimeter, severity is a mosaic
  (MTBS severity rasters would be the upgrade path).
- The NIFC history layer lags a year or two; the WFIGS layer fills 2021+.
- Map UI: sidebar trail index (click to highlight all segments), trailhead
  flags with hover labels, cabin marker (`CABIN` const in `docs/index.html`),
  hand-maintained closure notes in `TH_NOTES` (prune when expired).
- Closures are NOT on the map — check
  [SFNF alerts](https://www.fs.usda.gov/r03/santafe/alerts) before hiking
  (linked from the map header).

## Ideas / next

- Scrape trail-maintenance reports (e.g. NM Volunteers for the Outdoors,
  Friends of the Pecos Wilderness posts) to flag recently-cleared trails
  (deadfall in burn scars is the real hazard).
- MTBS burn-severity rasters as an optional overlay.
- SFNF closure orders as a layer, if a stable feed exists.
