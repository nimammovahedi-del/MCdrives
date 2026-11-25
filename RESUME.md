# Resume: bridge levels (branch bridge-levels)

Goal: where a route passes under a bridge the car and ribbon stay on the ground road; on a bridge edge (`dk`) they ride the deck.

## Found so far
- Main cause (viewer): `groundY`'s floor-leak fallback. Where layer 0 sits on the world floor over a wide area (about 20% of
  major-road samples in Irvine; sky leaked under the terrain) it returns layer 1, the top surface. Under a bridge that is the
  deck, so a non-bridge route hops onto it for the deck's width (I 5 Truck at (12971,15139): 90 -> 96 over 24 m; San Diego
  Freeway under overpasses at (8501,13043) and (10107,13698); Harrier, Moffett Drive and others). The 9-sample median only hides
  hops shorter than about 8 m.
- Second cause (build data): a solid embankment or abutment under a non-road bridge (railway?) seals the underpass. Example:
  Culver Drive at (7106,8437): ground 29, deck 34, a 33 block sits on the road, and the car rose 4 blocks for about 10 m.
- Airport: Arrivals (dk=0) runs about 600 m under Departures (dk=1) where layer 0 is unknown. The car rode the upper deck.
- Not defects: the reported 3-block bump at (8953..8961, 9980..9990) is a raised strip at the same level in both layers on
  Jeffrey Road; no bridge is involved. Cadence at (12956,10304) crosses a 4-wide, 11-deep channel (culvert or untagged bridge),
  and the car correctly stays on top.
- No wrong on-bridge (`dk`) cases were found: 0 of 89.

## Changed
- web/index.html, `startTrip`: before the gap fill, a step up of 3 or more blocks within 4 m that comes back down as sharply
  within 800 m, all under one `dk` flag, is set to NaN so the gap fill carries the road under it. The rise onto the route's own
  bridge is kept because the `dk` flag changes there. The groundY comment was updated. No coordinates are special-cased.
- tools/audit_bridges.js: the detector. It reads the height tiles itself in node over seeded long routes and classes each
  stretch as bridge, under or untagged (culvert).
- tools/shoot_crossings.js: shoots a list of crossings in nav view with the car paused. Written; not yet run.

## Verified (detector before the culvert class and the 800 m cap, 41 routes, seed 4242 plus the two known routes)
- Before: 493 two-level stretches (60 with layer 0 unknown); 89 on bridge edges, 0 wrong; 404 under, 17 wrong.
- After, with a 200 m cap: 3 wrong. These were the Cadence culvert, a false positive now classed `untagged`, and Arrivals twice,
  which needed the cap raised to 800 m (the file has 800 m now, `i + 400`).
- With the 800 m cap, Arrivals goes to the lower level (y 23.9 under a deck at 30). The detector then flagged it only because
  its ground estimate is poor under a long deck; that is fixed in the detector since.

## Half done / next steps
1. Serve the unchanged city: `cd /home/claude/irv-A && python3 -m http.server 8811`. Serve the fixed copy too:
   `cp -al irv-A irv-B; rm irv-B/index.html; cp wa/web/index.html irv-B/` and serve irv-B on 8812.
2. Re-run the final detector on both, about 5 min each:
   `URL=http://localhost:881X ROUTES="Walnut Avenue|Antares;Exploration|Chardonnay" node tools/audit_bridges.js OUT.json 40 4242`
   Expect before about 15 wrong (under), after 0 (Cadence becomes `untagged`).
3. audit_routes, 8 routes, before and after:
   `URL=... node tools/audit_routes.js DIR 8 2`. The before run had only finished route 0 (0 jumps). Check that the after run
   shows no new jumps.
4. Screenshots: build a list of 24 crossings from after.json. Include every before-wrong case and about 6 on-bridge cases, with
   fields file bNN.png, fromNode, toNode and distance (the worst d). Run
   `URL=http://localhost:8812 node tools/shoot_crossings.js list.json /home/claude/bridge-audit 0 6` in batches of 6
   (each under 8 min). Write /home/claude/bridge-audit/index.json with file, expected level, routes, distance and position.
   Check several shots by eye.
5. Build-side follow-up, not done: mca2web.py layer 0 is the world floor wherever the sky leaks under the terrain. Seal the
   terrain shell (for example, columns below the terrain top that are not reachable from the side), or store the lowest surface
   under each deck separately (a third layer, or layer 0 restricted to sky-reachable air above the terrain shell). Then the
   ground under a deck is known instead of interpolated. Solid embankment blocks under bridges (Culver Drive) come from the
   Arnis bake.
