# MCdrives

A driving-navigation view of the real world drawn as a block world. Real map data goes in, a
browser viewer with a tilted, heading-up navigation camera comes out.

## One command

    pip install -r requirements.txt
    python build.py areas/spectrum.json --serve

That runs these steps and skips any whose output already exists:

| Step | What it does | Tool |
| --- | --- | --- |
| bake | OpenStreetMap, terrain, USGS LiDAR and Overture buildings into a block world | [Arnis](https://github.com/louis-e/arnis) (unmodified) |
| convert | World files into greedy-meshed tiles with our own block palette, grouped into one streamable pack per 512 x 512 blocks | `tools/mca2web.py` |
| roads | Drivable road graph, in block coordinates, from the same OSM data the bake used, plus `places.json` (named destinations for search) | `tools/roadgraph.py`, `tools/places.py` |
| site | Copy the viewer next to the data in `build/<area>/<chunk>/site/` | `build.py` |
| join | Place every chunk in one world and write `build/<area>/site/`, then drive every road of it and write `roadcheck.json` (steps, gaps, broken bridge decks, unpaved stretches, with coordinates) and `roadcheck-spots.json` (the worst of them, as audit.yml `spots`) into the site; never fails the build | `build.py`, `tools/roadcheck.py` |

Useful flags: `--plan` (show the chunk grid, each chunk's offset and its projection error),
`--plan-ids` (just the ids, as JSON), `--only r0c0,r0c1`, `--from convert`, `--force`,
`--world DIR --osm FILE` (reuse a bake you already have; with `--only` on one chunk of a big area),
`--join-only` (join chunk outputs that are already there), `--no-join`, `--seam-tol`, `--arnis PATH`.

On Windows the Arnis release binary is downloaded into `bin/` on first use. Elsewhere, build it
with `cargo build --release --no-default-features` and pass `--arnis` or set `ARNIS_BIN`.

## Building in GitHub Actions

`.github/workflows/build.yml` does the same build on a GitHub runner and publishes the viewer. It runs when the `irvine-core` config, the tools or the viewer change, or by hand from
the Actions tab with any area name. The site is served from Cloudflare Pages (project `mcdrives-<area>`) once the repository secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set; GitHub Pages caps a site at 1 GB, and all of Irvine is larger. Each build also force-pushes the finished site to a `built/<area>` branch, so the output can be fetched with git.

A big area is built in parallel: one job plans the chunk grid, one matrix job bakes and converts
each chunk and uploads its site as an artifact, and a last job downloads them all, joins them and
publishes. `areas/irvine.json` is 25 jobs, well inside GitHub's 256 per matrix, and each chunk job
has its own 6 hour budget instead of sharing one.

## Areas and scale

An area is a small JSON file in `areas/`: a name, a slug, a bounding box `[south, west, north, east]`
and `max_chunk_km2`.

Three things keep cost flat as the area grows:

- The converter decodes one 512 x 512 region at a time and meshes it with a margin borrowed from
  its neighbours, so memory does not depend on the size of the world.
- The viewer streams packs in around the car and drops them behind it, so the browser only ever
  holds about a kilometre of world in each direction. Heights come in one 1 MB tile per pack
  (`h_<i>_<j>.bin`), loaded with the pack and, when a trip starts, along the route; at most 48
  are kept beyond the ones under loaded packs.
- `build/<area>/report.json` records size, quad count and seconds per step, which is the data for
  estimating a bigger build.

One Arnis run covers up to 16 km2 with LiDAR (`areas/irvine-core.json` is 15 km2 and one continuous
drive). Areas larger than `max_chunk_km2` are cut into a grid of separate bakes;
`areas/irvine.json` is the whole city, about 343 km2.

## Joining the chunks

Each chunk is its own Arnis bake, starting at block (0, 0) with its own ground level. The join step
puts them back together into one world at `build/<area>/site/`:

- Arnis's projection is linear inside a bbox, so a chunk's blocks differ from the area's by a
  translation. `build.py --plan` prints each chunk's offset and the error at its corners: the
  metres per degree of longitude depend on a bbox's mean latitude, so a chunk row's scale is
  slightly off the area's. Over the 343 km2 of `areas/irvine.json` the worst corner is 4.2 blocks out.
  That error grows with the area's height (up to 9 blocks over the 63 km of Orange County), so a tall
  area sets `"conformal": true`: its frame is then Mercator, 1 block = 1 metre in the chunk row
  nearest the equator, and every other row is baked with Arnis `--scale` a fraction above 1 (at most
  1.0064 for the county) so that its blocks fill its slot. The worst corner is then under 1.5 blocks
  whatever the height. Turning it on or off for an area means re-baking its chunks.
- Pack files are copied, never re-encoded: each keeps its bytes and gains a unique id and the
  offset of its chunk, which the viewer applies when it places the tile meshes. Material ids are
  the same in every world (`tools/mca2web.py` registers the whole palette up front), so one
  palette covers the area.
- Height tiles (one per pack, the surface under every column) are linked the same way and carry
  their chunk's vertical offset with the pack. Where two chunks' packs overlap, the viewer reads a
  column from the later chunk when it has a surface there, else from the earlier one.
- Road graphs are merged. A chunk's graph reaches 32 blocks past its bbox (`roadgraph.py
  --overhang`), so neighbours hold the same OSM nodes either side of their seam; the join merges
  those nodes, then any remaining pair within `--seam-tol` blocks, and keeps the largest connected
  component of the result. Routes cross chunk edges as if the area had been baked in one go.

- Places are merged the same way: each chunk's `places.json` is shifted by its offset, `node` is
  remapped to the joined graph (re-snapped if that node was dropped), and entries with the same name
  within 150 blocks, which is what the overlap between chunks produces, are reduced to one. A chunk
  without a `places.json` counts as empty.

A single-chunk area is already one world, so nothing is joined and the site stays where it was.

## Places

`site/places.json` lists named destinations for search, as rows `[name, kind, x, z, node, aliases]`
sorted by name. `x`, `z` are block coordinates in the same frame as `roads.json`; `node` is the
index into `roads.json` `nodes` of the nearest node on a named road that is not a motorway or ramp
within 300 blocks (`null` if none); `kind` is one of civic, school, university, hospital, shop,
mall, food, park, office, worship, hotel, fuel, station, neighbourhood, other; `aliases` holds
`alt_name`, `short_name`, `official_name`, `old_name`, `loc_name` and a `brand` that differs from
`name`. Sources are named OSM nodes and ways (a way is placed at its centroid) tagged amenity, shop,
tourism, leisure, office, building, place, station or landuse retail/commercial; parking, benches
and the like are left out.

## Optional street photos

Arnis can colour facades from Mapillary photos, only for areas up to 0.10 km2. Set `"mapillary": true`
in the area file and point `MAPILLARY_TOKEN_FILE` at a text file holding the token. Keep that file
outside this repository. The token is never printed or written by the build.

## Optional building enrichment

`"enrich": true` in an area file (or a list of `"roof_colour"` and `"heights"`) fills building tags
OpenStreetMap leaves empty before each chunk is baked, with `tools/enrich_osm.py`: `roof:colour`
from USDA NAIP aerial photos (the USGS NAIP Plus service Arnis reads for its yard detail), and
`building:levels` or `height` from Overture Maps. OSM values are never overwritten. Arnis first
fetches the chunk's OSM data in a `--prewarm` run (which also caches every other input), the tool
writes `osm_enriched.json` and `enrich.json` (counts and which sources answered) next to `osm.json`,
and the bake reads the enriched file with `--file`. Any failure falls back to the plain bake. Off
unless set; `areas/irvine-enrich.json` is the city with it on, for trying one chunk.

## Credits

Map data © OpenStreetMap contributors (ODbL). Buildings from Overture Maps. Elevation and LiDAR
from USGS 3DEP and Mapterhorn. World generation by Arnis (Apache-2.0). Rendering with three.js (MIT).
The block art and palette are original.
