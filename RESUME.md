# Resume: roofs and parked cars (branch roofs-cars)

Paused on request. Nothing in tools/ or web/ has changed yet. This branch holds the diagnosis
scripts (diag/roofs-cars/) and these notes.

## Found so far

### Roofs (s26, chunk r5c5, chunk-local x 432, z 2057; meta minx/minz are 0 for r5c5)
- Arnis does generate the roofs. A top-block heightmap of the raw world around s26
  (diag/roofs-cars/s26h.png, 120x120 blocks, made by s26.py + view.py) shows every building
  closed, with gabled roofs. The roofs are mostly slabs: top blocks in that box include
  cobbled_deepslate_slab (1038 bottom, 1005 type=top), polished_granite_slab (487/372),
  spruce_slab (219/214), brick_slab (223/198), dark_oak_slab (151/154), mud_brick_slab,
  cut_sandstone_slab, smooth_stone_slab, plus stairs.
- So the converter drops them. Cause, tools/mca2web.py since commit 6ab4978 ("seal partial-block
  ground"): partial blocks whose shape covers the whole cell (`footprint`/`cover`: every slab
  bottom or top, both stair halves, dirt_path, farmland, snow >= 4 layers) are made non-passable
  (`passable[...] &= ~cover`), so their own cell is never in the sky-connected `reach` set, and
  then `dmat[~reach[...]] = 0` deletes every one of them. Every slab/stair roof vanishes, and
  the full-block top face under a slab is culled too (its upper neighbour is not reached), which
  gives the holes and the dark squares (seeing into the interior / background through
  un-drawn faces). Not yet confirmed by rendering, but it follows directly from the code.
- Whether any building is left really roofless by Arnis is still to be checked after the fix.

### Parked cars
- Arnis (src/structures/car.rs) parks block-built schematics on parking spaces (50 percent
  occupancy, also on parking-garage decks and driveways via place_vehicle): assets/structures/
  car_{camper,fedex,hotrod_blue,hotrod_white,pickup,police,sedan,suv,uhaul,workvan}.schem,
  centred anchor (W//2, L//2), rotations by rotate_xz (k = 0..3, includes the nose flip), forced
  overwrite, unmapped names (wall signs, damaged_anvil, skulls) dropped, names substituted via
  map_structure_block (e.g. mangrove_slab -> red_nether_brick_slab, deepslate_brick_slab ->
  polished_deepslate_slab, black_concrete_powder -> black_concrete, acacia_stairs ->
  granite_stairs, all buttons -> stone_button).
  Unless the bake had extras.cars on Java, in which case parked cars are item_display entities,
  invisible to the converter; this bake has block cars.
- The cars are 48-90 mapped voxels each and mostly slabs and stairs (quartz_slab, quartz_stairs,
  smooth_quartz_slab, polished_andesite_slab, cobblestone_slab, ...), with full-block wheels
  (black_concrete / black_wool) and bodies (smooth_quartz, quartz_block, iron_block, smooth_stone).
  The same cover bug deletes the slabs/stairs, so a car comes out as its few full blocks and
  carpets = the "fragments"; light-blue patches are the culled asphalt top face under a deleted
  slab (sky colour shows through). Cars with many full blocks (sedan, workvan, uhaul) look
  complete.
- diag/roofs-cars/gen_cars.py decodes the schematics with Arnis's name mapping (cars.pkl, not
  committed: regenerate). findcars.py is a first template matcher (by base name, 4 rotations,
  >= 50 percent voxels) over a box of the world; it was stopped before finishing on region
  r.0.4 (box 0 2048 512 2560 is too big for its name-level numpy; use a smaller box or the
  material-id approach below).

## Baseline numbers
- Unchanged converter on r5c5 (partial run, stopped): read 21114 chunks, 25 regions,
  y -64..111; first packs p_0_0 485,200 quads 1.6 MB ... p_1_2 397,590 quads 1.4 MB.
  Log: /home/claude/fix-roofs/work/before.log. Rerun for full totals.

## Next steps
1. Baseline: `cd /home/claude/fix-roofs/work && /home/claude/irvine/venv/bin/python
   /home/claude/wr/tools/mca2web.py "/home/claude/world-r5c5/world/Arnis World 1" before/data`
   (background, a few minutes), then roads with tools/roadgraph.py (bbox from build.py --plan).
2. Fix the cover bug in a new function (few call sites): keep a cover partial block when any of
   its six neighbours is in `reach` (instead of requiring its own cell), i.e. replace the
   condition in `dmat[~reach[...]] = 0` with `~partial_seen(reach, cover)`. Keep cover cells
   non-passable so the under-terrain sealing still works.
3. Car removal, new function called once per region window before the passable/reach step:
   embed compact car templates (from gen_cars.py: per car W, L and voxel list with block names)
   in mca2web.py, map names to material ids with the existing colour()/mat() (palette fixed, no
   new materials), match on material only (full g | partial dm of the whole window incl.
   neighbours' dm, all 4 rotations, anchor = rarest template material), accept >= ~85 percent of
   voxels, and clear only cells whose material equals the template's (wg, window dm, centre
   dmat/dshape). Count removed blocks; print it.
4. Re-convert, render the s26 heightmap view vs screenshots; check for buildings still without
   roofs; only then add roof capping (footprint from enclosed interior at wall-top or osm.json
   building outlines, skip courtyards/parking decks/stadiums).
5. Verify per the task: before/after screenshots (survey/survey_viewer.html with window.__free,
   ?budget=400, ports 8830-8839) of s26 and 5+ more spots into /home/claude/fix-roofs/
   (beforeNN.png, afterNN.png, index.json), quad count and site size before/after, height layers
   and roads unchanged, `URL=... node tools/audit_routes.js OUT 6 0`.
