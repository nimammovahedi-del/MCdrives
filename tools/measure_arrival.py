"""Measure where addresses arrive under the viewer's old rule and the new one.

    measure_arrival.py roads.json addresses.json out.json [--limit N] [--reach 150]

Writes per-address rows (compact) and a summary; prints the summary.
"""
import sys, json, math, time
from collections import Counter
from multiprocessing import Pool
sys.path.insert(0, __import__("os").path.dirname(__file__))
import arrival

ROADS, ADDR, OUT = sys.argv[1:4]
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
REACH = float(sys.argv[sys.argv.index("--reach") + 1]) if "--reach" in sys.argv else 150.0
G = None; STREETS = None; EXISTS = None


def init():
    global G, STREETS, EXISTS
    G = arrival.Graph(json.load(open(ROADS)))
    STREETS = json.load(open(ADDR))["streets"]
    EXISTS = {}


def exists(street):
    """is the street in the graph at all (drivable edges), by any tier"""
    if street not in EXISTS:
        k = arrival.keys(street); found = None
        for i, nk in enumerate(G.nkeys):
            if not nk[0]: continue
            t = arrival.same_street(k, nk)
            if t is not None and (found is None or t < found): found = t
        EXISTS[street] = found
    return EXISTS[street]


def one(row):
    idx, si, num, x, z = row
    street = STREETS[si]; k = arrival.keys(street)
    o = G.old_rule(x, z); oe = G.edges[o["edge"]]; onm = G.names[oe[2]]
    otier = arrival.same_street(k, G.nkeys[oe[2]]) if onm else None
    on = G.nodes[o["node"]]
    n = G.snap(x, z, street, reach=REACH)
    ns = G.snap(x, z, street, reach=REACH, lots=False, margin=None)   # the own street within 150 m, for the causes
    ne = G.edges[n["edge"]]
    r = {"i": idx, "s": si, "n": num, "x": x, "z": z,
         "oe": o["edge"], "on": o["node"], "onm": onm, "ocl": oe[3], "otier": otier,
         "od": round(math.hypot(on[0] - x, on[1] - z), 1), "opd": round(o["proj_d"], 1),
         "ne": n["edge"], "nt": round(n["t"], 4), "nx": round(n["x"], 1), "nz": round(n["z"], 1), "nd": round(n["d"], 1),
         "nnm": G.names[ne[2]], "ncl": ne[3], "how": n["how"], "side": n["side"], "same_edge": n["edge"] == o["edge"]}
    def own(ei):   # own street, or an unnamed service road leading to it (its lot)
        nk = G.nkeys[G.edges[ei][2]]
        if nk[0]: return arrival.same_street(k, nk) is not None
        return G.edges[ei][3] == 7 and G.reaches(ei, k)
    r["oown"] = otier is not None or own(o["edge"]); r["nown"] = n["how"] in ("street", "street~", "street-far", "lot") or own(n["edge"])
    r["cov"] = any(G.project(i, x, z)[0] <= 250 for i in G.near(x, z, 250))
    if otier is None:
        # why the old rule missed: classify
        ex = exists(street)
        cause = None
        if ex is None: cause = "street_not_in_graph"
        elif oe[3] == 0 or onm == "ramp": cause = "motorway_or_ramp"
        elif ns["how"].startswith("fallback"): cause = "street_beyond_150m"
        else:
            # named street is within reach. corner: a node near the address joins the snapped road and the street
            want, _ = G.street_names(street)
            corner = False
            for i in G.near(x, z, 60):
                e = G.edges[i]
                for nd in (e[0], e[1]):
                    p = G.nodes[nd]
                    if math.hypot(p[0] - x, p[1] - z) > 60: continue
                    nms = {G.edges[j][2] for j in G.node_edges[nd]}
                    if oe[2] in nms and (any(m in want for m in nms) or G.edges[ns["edge"]][2] in nms): corner = True
            # back side: the old arrival and the named street lie on opposite sides of the address
            _, _, opx, opz = G.project(o["edge"], x, z)
            v1 = (opx - x, opz - z); v2 = (ns["x"] - x, ns["z"] - z)
            l1, l2 = math.hypot(*v1) or 1, math.hypot(*v2) or 1
            cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
            r["cos"] = round(cos, 2)
            if not onm or oe[3] == 7: cause = "service_or_unnamed" + ("_behind" if cos < -0.5 else "")
            elif corner: cause = "corner_lot"
            elif cos < -0.5: cause = "back_side_street"
            else: cause = "other_named_road_closer"
        r["cause"] = cause; r["exists"] = ex
    return r


def pct(a, n): return f"{100 * a / n:.1f}%" if n else "-"


def quant(v, qs=(0.5, 0.9, 0.99)):
    v = sorted(v); return {f"p{int(q * 100)}": v[min(len(v) - 1, int(q * len(v)))] for q in qs} if v else {}


if __name__ == "__main__":
    A = json.load(open(ADDR))["a"]
    rows = [(k // 4, A[k], A[k + 1], A[k + 2], A[k + 3]) for k in range(0, len(A), 4)]
    if LIMIT: rows = rows[:: max(1, len(rows) // LIMIT)][:LIMIT]
    t0 = time.time()
    with Pool(4, initializer=init) as p: res = p.map(one, rows, chunksize=500)
    unc = [r for r in res if not r["cov"]]
    S0 = {"all_addresses": len(res), "no_road_within_250m": len(unc)}
    res_all = res; res = [r for r in res if r["cov"]]
    n = len(res)
    miss = [r for r in res if r["otier"] is None]
    S = {**S0, "addresses_measured": n, "reach": REACH, "seconds": round(time.time() - t0, 1)}
    S["before"] = {
        "on_named_street": pct(n - len(miss), n), "on_named_street_tight": pct(sum(r["otier"] == 0 for r in res), n),
        "different_road": pct(len(miss), n), "different_road_n": len(miss),
        "named_within_150m_but_not_chosen": pct(sum(1 for r in miss if r.get("cause") not in ("street_not_in_graph", "street_beyond_150m", "motorway_or_ramp")), n),
        "snapped_to_motorway_or_ramp": sum(1 for r in res if r["ocl"] == 0 or r["onm"] == "ramp"),
        "dist_addr_to_arrival_node": quant([r["od"] for r in res]),
        "dist_addr_to_arrival_node_when_wrong_road": quant([r["od"] for r in miss]),
        "dist_addr_to_snapped_segment": quant([r["opd"] for r in res]),
        "causes": dict(Counter(r["cause"] for r in miss).most_common()),
    }
    after_ok = [r for r in res if not r["how"].startswith("fallback")]
    S["after"] = {
        "on_named_street_or_its_lot": pct(len(after_ok), n), "tight": pct(sum(r["how"] == "street" for r in res), n),
        "loose_or_typo": pct(sum(r["how"] == "street~" for r in res), n), "street_150_300m": pct(sum(r["how"] == "street-far" for r in res), n), "lot_of_the_street": pct(sum(r["how"] == "lot" for r in res), n), "fallback": pct(n - len(after_ok), n), "fallback_n": n - len(after_ok),
        "dist_addr_to_arrival_point": quant([r["nd"] for r in res]),
        "dist_on_named_street_or_lot": quant([r["nd"] for r in after_ok]),
        "dist_on_named_street_only": quant([r["nd"] for r in after_ok if r["how"] != "lot"]),
        "changed_road": sum(1 for r in res if r["nnm"] != r["onm"]),
        "changed_from_wrong_to_named": sum(1 for r in miss if not r["how"].startswith("fallback")),
        "fallback_facing": pct(sum(r["how"] == "fallback-facing" for r in res), n),
        "new_on_motorway_or_ramp": sum(1 for r in res if r["ncl"] == 0 or r["nnm"] == "ramp"),
    }
    S["quality"] = {
        "before_own_or_lot": pct(sum(r["oown"] for r in res), n), "after_own_or_lot": pct(sum(r["nown"] for r in res), n),
        "before_own_or_lot_and_le80m": pct(sum(r["oown"] and r["od"] <= 80 for r in res), n),
        "after_own_or_lot_and_le80m": pct(sum(r["nown"] and r["nd"] <= 80 for r in res), n),
        "before_gt120m": pct(sum(r["od"] > 120 for r in res), n), "after_gt120m": pct(sum(r["nd"] > 120 for r in res), n)}
    S["closer"] = {"after_closer_than_before_node": pct(sum(r["nd"] < r["od"] - 0.5 for r in res), n),
                   "after_farther": pct(sum(r["nd"] > r["od"] + 0.5 for r in res), n)}
    json.dump({"summary": S, "rows": res_all}, open(OUT, "w"), separators=(",", ":"))
    print(json.dumps(S, indent=1))
