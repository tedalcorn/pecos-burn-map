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

## AllTrails conditions layer

`alltrails/` holds a **local-only** pipeline (runs on Ted's Mac, not in CI —
AllTrails sits behind Cloudflare, which blocks headless/cloud clients):

- `scrape_alltrails.py` — anonymous scrape (no AllTrails account involved) of
  every AllTrails route near the wilderness (~96 routes) into
  `alltrails/alltrails.db` (gitignored; raw review text never leaves this
  machine). Works by launching real Chrome un-attached so the Cloudflare
  challenge solves itself, then attaching CDP and calling AllTrails' internal
  JSON API via in-page `fetch()`. Incremental after the first run.
- `analyze_conditions.py` — matches routes to USFS trail numbers (explicit
  numbers in route names → names as words → start-point proximity), tags
  reviews with a condition taxonomy (deadfall, washout, overgrown, hard to
  follow, snow/ice, mud, bugs, burn mentions, + positive "maintained"), and
  compares the last 12 months against same-calendar-month baselines from
  prior years, so seasonal patterns don't fire alerts. Publishes **derived
  signals only** to `docs/data/conditions.json`.
- launchd `com.tedalcorn.pecosalltrails` (runner `~/scripts/pecos_alltrails_update.py`,
  log `~/Library/Logs/pecos-alltrails.log`) re-runs monthly (2nd, 09:30) and
  pushes the refreshed `conditions.json`.

The map shows a ⚠ beside flagged trails in the sidebar and a conditions
summary in trail popups.

## Ideas / next

- Scrape trail-maintenance reports (e.g. NM Volunteers for the Outdoors,
  Friends of the Pecos Wilderness posts) to flag recently-cleared trails
  (deadfall in burn scars is the real hazard).
- MTBS burn-severity rasters as an optional overlay.
- SFNF closure orders as a layer, if a stable feed exists.
- AllTrails route polylines (not exposed anonymously) would upgrade matching
  from name/startpoint heuristics to real geometric overlap.
