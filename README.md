# MCdrives

A driving-navigation view of the real world drawn as a block world. Real map data goes in, a
browser viewer with a tilted, heading-up navigation camera comes out.

## One command

    pip install -r requirements.txt
    python build.py areas/spectrum.json --serve

That runs four steps and skips any whose output already exists:

| Step | What it does | Tool |
| --- | --- | --- |
| bake | OpenStreetMap, terrain, USGS LiDAR and Overture buildings into a block world | [Arnis](https://github.com/louis-e/arnis) (unmodified) |
| convert | World files into greedy-meshed tiles with our own block palette | `tools/mca2web.py` |
| roads | Drivable road graph, in block coordinates, from the same OSM data the bake used | `tools/roadgraph.py` |
| pack | Bundle tiles and copy the viewer into `build/<area>/<chunk>/site/` | `tools/pack.py` |

Useful flags: `--plan` (show the chunk grid only), `--only r0c0,r0c1`, `--from convert`, `--force`,
`--world DIR --osm FILE` (reuse a bake you already have), `--arnis PATH`.

On Windows the Arnis release binary is downloaded into `bin/` on first use. Elsewhere, build it
with `cargo build --release --no-default-features` and pass `--arnis` or set `ARNIS_BIN`.

## Building in GitHub Actions

`.github/workflows/build.yml` does the same build on a GitHub runner and publishes the viewer to
GitHub Pages. It runs when the Spectrum config, the tools or the viewer change, or by hand from
the Actions tab with any area name. Enable it once under Settings, Pages, Source: GitHub Actions. Each build also force-pushes the finished site to a `built/<area>` branch, so the output can be fetched with git.

## Areas and scale

An area is a small JSON file in `areas/`: a name, a slug, a bounding box `[south, west, north, east]`
and `max_chunk_km2`. Areas larger than that are cut into a grid of chunks, each baked and
converted on its own, so memory and time per chunk stay flat as the area grows. LiDAR is only
used for chunks up to 16 km2 (an Arnis limit). `build/<area>/report.json` records size, quad count
and seconds per step for every chunk, which is the data for estimating a bigger build.

`areas/irvine.json` is the whole city: about 343 km2, 90 chunks.

Not done yet: the viewer loads one chunk at a time. Stitching chunks into one continuous world
(streaming neighbours as the car moves, and a road graph that crosses chunk edges) is the next
piece of work.

## Optional street photos

Arnis can colour facades from Mapillary photos, only for areas up to 0.10 km2. Set `"mapillary": true`
in the area file and point `MAPILLARY_TOKEN_FILE` at a text file holding the token. Keep that file
outside this repository. The token is never printed or written by the build.

## Credits

Map data © OpenStreetMap contributors (ODbL). Buildings from Overture Maps. Elevation and LiDAR
from USGS 3DEP and Mapterhorn. World generation by Arnis (Apache-2.0). Rendering with three.js (MIT).
The block art and palette are original.
