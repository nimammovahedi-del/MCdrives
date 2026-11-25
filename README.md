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
| roads | Drivable road graph, in block coordinates, from the same OSM data the bake used | `tools/roadgraph.py` |
| site | Copy the viewer next to the data in `build/<area>/<chunk>/site/` | `build.py` |

Useful flags: `--plan` (show the chunk grid only), `--only r0c0,r0c1`, `--from convert`, `--force`,
`--world DIR --osm FILE` (reuse a bake you already have), `--arnis PATH`.

On Windows the Arnis release binary is downloaded into `bin/` on first use. Elsewhere, build it
with `cargo build --release --no-default-features` and pass `--arnis` or set `ARNIS_BIN`.

## Building in GitHub Actions

`.github/workflows/build.yml` does the same build on a GitHub runner and publishes the viewer to
GitHub Pages. It runs when the `irvine-core` config, the tools or the viewer change, or by hand from
the Actions tab with any area name. Enable it once under Settings, Pages, Source: GitHub Actions. Each build also force-pushes the finished site to a `built/<area>` branch, so the output can be fetched with git.

## Areas and scale

An area is a small JSON file in `areas/`: a name, a slug, a bounding box `[south, west, north, east]`
and `max_chunk_km2`.

Three things keep cost flat as the area grows:

- The converter decodes one 512 x 512 region at a time and meshes it with a margin borrowed from
  its neighbours, so memory does not depend on the size of the world.
- The viewer streams packs in around the car and drops them behind it, so the browser only ever
  holds about a kilometre of world in each direction.
- `build/<area>/report.json` records size, quad count and seconds per step, which is the data for
  estimating a bigger build.

One Arnis run covers up to 16 km2 with LiDAR (`areas/irvine-core.json` is 15 km2 and one continuous
drive). Areas larger than `max_chunk_km2` are cut into a grid of separate bakes;
`areas/irvine.json` is the whole city, about 343 km2.

Not done yet: separate bakes are separate worlds. Joining them (placing each bake at its offset
in one pack grid and one road graph) is the next piece of work for going past 16 km2.

## Optional street photos

Arnis can colour facades from Mapillary photos, only for areas up to 0.10 km2. Set `"mapillary": true`
in the area file and point `MAPILLARY_TOKEN_FILE` at a text file holding the token. Keep that file
outside this repository. The token is never printed or written by the build.

## Credits

Map data © OpenStreetMap contributors (ODbL). Buildings from Overture Maps. Elevation and LiDAR
from USGS 3DEP and Mapterhorn. World generation by Arnis (Apache-2.0). Rendering with three.js (MIT).
The block art and palette are original.
