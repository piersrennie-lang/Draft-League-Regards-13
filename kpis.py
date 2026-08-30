"""Turn raw API JSON into the roundup's metric set.

Every number that appears on the site is computed here and nowhere else, so
there is one place to check when a figure looks wrong. Sections whose inputs
are missing are marked with a gap rather than estimated.

Usage:
    python kpis.py --gw 2
"""

import argparse
import json
import pathlib

import config

ROOT = pathlib.Path(__file__).parent
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"


def load(name, default=None):
    path = RAW / f"{name}.json"
    if not path.exists():
        return default
    return json.loads(path.read_text())


# --------------------------------------------------------------------------
# Managers
# --------------------------------------------------------------------------

def build_managers(details):
    """Keyed by league_entry id, which is what matches and standings use.

    Note the two-id trap: league_entries carry both `id` (the league entry,
    used by matches and standings) and `entry_id` (the team, used by squad and
    transaction endpoints). Mixing them up is the most common way this breaks.
    """
    out = {}
    for e in details["league_entries"]:
        out[e["id"]] = {
            "league_entry": e["id"],
            "entry_id": e["entry_id"],
            "manager": f"{e['player_first_name']} {e['player_last_name']}",
            "team": e["entry_name"],
            "short": e["short_name"],
            "waiver_pick": e.get("waiver_pick"),
        }
    return out


# --------------------------------------------------------------------------
# Squads: active XI, bench, blanks
# --------------------------------------------------------------------------

def player_index(bootstrap):
    if not bootstrap:
        return {}
    types = {t["id"]: t.get("singular_name_short", "") for t in bootstrap.get("element_types", [])}
    teams = {t["id"]: t.get("short_name", "") for t in bootstrap.get("teams", [])}
    return {
        p["id"]: {
            "name": p.get("web_name", str(p["id"])),
            "pos": types.get(p.get("element_type"), ""),
            "club": teams.get(p.get("team"), ""),
            "photo": (f"https://resources.premierleague.com/premierleague/"
                      f"photos/players/110x140/p{p['code']}.png") if p.get("code") else "",
        }
        for p in bootstrap.get("elements", [])
    }


def describe_player(eid, players):
    meta = players.get(eid, {"name": str(eid), "club": "", "photo": ""})
    return {"name": meta["name"], "club": meta.get("club", ""), "photo": meta.get("photo", "")}


def live_points(live):
    if not live:
        return {}
    return {
        int(pid): (data.get("stats") or {}).get("total_points", 0)
        for pid, data in (live.get("elements") or {}).items()
    }


def build_squads(managers, squads_raw, live, players):
    """Split each squad into XI and bench and attach points.

    Draft uses picks position 1-11 for the active eleven and 12-15 for the
    bench, so the Highest Scorer Rule only ever looks at positions 1-11.
    """
    pts = live_points(live)
    out = {}
    if not squads_raw:
        return out

    by_entry = {m["entry_id"]: le for le, m in managers.items()}

    for entry_str, payload in squads_raw.items():
        le = by_entry.get(int(entry_str))
        if le is None:
            continue
        xi, bench = [], []
        for pick in payload.get("picks", []):
            eid = pick["element"]
            meta = players.get(eid, {"name": str(eid), "pos": "", "club": "", "photo": ""})
            row = {
                "element": eid,
                "name": meta["name"],
                "pos": meta["pos"],
                "club": meta["club"],
                "photo": meta.get("photo", ""),
                "points": pts.get(eid, 0),
            }
            (xi if pick.get("position", 99) <= 11 else bench).append(row)

        xi.sort(key=lambda r: -r["points"])
        bench.sort(key=lambda r: -r["points"])
        out[le] = {
            "xi": xi,
            "bench": bench,
            "xi_points": sum(r["points"] for r in xi),
            "bench_points": sum(r["points"] for r in bench),
            "best_bench": bench[0] if bench else None,
            "blanks": sum(1 for r in xi if r["points"] == 0),
            "wasted": wasted_bench_points(xi, bench),
        }
    return out


def wasted_bench_points(xi, bench):
    """Bench points that beat a starter, which is the only kind that stings.

    Sums the gap for each bench player who outscored the starter he would have
    replaced, pairing best bench against worst starter downwards. Positional
    legality is ignored, so read it as an upper bound.
    """
    starters = sorted(r["points"] for r in xi)
    total = 0
    for i, sub in enumerate(bench):
        if i < len(starters) and sub["points"] > starters[i]:
            total += sub["points"] - starters[i]
    return total


# --------------------------------------------------------------------------
# Compulsory releases under the house rule
# --------------------------------------------------------------------------

def build_releases(managers, squads, results_by_entry):
    rows = []
    for le, squad in squads.items():
        xi = squad["xi"]
        if not xi:
            continue
        top = xi[0]
        tied = [r for r in xi if r["points"] == top["points"]]
        nxt = next((r for r in xi if r["points"] < top["points"]), None)
        nxt_tied = [r for r in xi if nxt and r["points"] == nxt["points"]]
        score = squad["xi_points"]
        rows.append({
            "league_entry": le,
            "manager": managers[le]["manager"],
            "team": managers[le]["team"],
            "release": top["name"],
            "release_photo": top["photo"],
            "release_points": top["points"],
            "tie": [{"name": r["name"], "photo": r["photo"]} for r in tied] if len(tied) > 1 else [],
            "next": nxt["name"] if nxt else None,
            "next_points": nxt["points"] if nxt else None,
            "next_tie": [r["name"] for r in nxt_tied] if len(nxt_tied) > 1 else [],
            "score": score,
            "cost_pct": round(100 * top["points"] / score, 1) if score else 0.0,
            "h2h": results_by_entry.get(le, {}).get("result"),
        })
    rows.sort(key=lambda r: -r["release_points"])
    return rows


def manual_releases(gw, managers, results_by_entry, players):
    """Fallback for weeks where you have the releases but not the picks.

    Drop a list of {manager, release, release_points, next, next_points, score}
    into data/manual/gw{n}_releases.json and the ledger renders from that.
    Percentages and H2H are still computed here, never typed by hand.
    """
    path = ROOT / "data" / "manual" / f"gw{gw}_releases.json"
    if not path.exists():
        return []
    by_name = {m["manager"]: le for le, m in managers.items()}
    photo_by_name = {p["name"]: p.get("photo", "") for p in players.values()}
    rows = []
    for r in json.loads(path.read_text()):
        le = by_name.get(r["manager"])
        if le is None:
            print(f"  manual: unknown manager {r['manager']!r}, skipped")
            continue
        score = r["score"]
        rows.append({
            "league_entry": le,
            "manager": r["manager"],
            "team": managers[le]["team"],
            "release": r["release"],
            "release_photo": photo_by_name.get(r["release"], ""),
            "release_points": r["release_points"],
            "tie": [{"name": n, "photo": photo_by_name.get(n, "")} for n in r.get("tie", [])],
            "next": r.get("next"),
            "next_points": r.get("next_points"),
            "next_tie": r.get("next_tie", []),
            "score": score,
            "cost_pct": round(100 * r["release_points"] / score, 1) if score else 0.0,
            "h2h": results_by_entry.get(le, {}).get("result"),
            "source": "manual",
        })
    rows.sort(key=lambda r: -r["release_points"])
    return rows


def detect_breaches(prev_releases, squads):
    """A breach is a mandated player still sitting in the active XI."""
    out = []
    for row in prev_releases or []:
        squad = squads.get(row["league_entry"])
        if not squad:
            continue
        in_xi = any(p["name"] == row["release"] for p in squad["xi"])
        on_bench = any(p["name"] == row["release"] for p in squad["bench"])
        if in_xi or on_bench:
            out.append({
                "manager": row["manager"],
                "player": row["release"],
                "points": row["release_points"],
                "status": "Still in XI" if in_xi else "On bench",
                "fine": config.FINE_NOT_RELEASED + (config.FINE_FIELDED_ANYWAY if in_xi else 0),
            })
    return out


# --------------------------------------------------------------------------
# Matches, entertainment, standings
# --------------------------------------------------------------------------

def build_matches(details, gw, managers):
    rows = []
    for m in details["matches"]:
        if m["event"] != gw or not m.get("finished"):
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        rows.append({
            "home": h, "away": a,
            "home_name": managers[h]["manager"], "away_name": managers[a]["manager"],
            "home_team": managers[h]["team"], "away_team": managers[a]["team"],
            "home_points": hp, "away_points": ap,
            "margin": abs(hp - ap),
            "combined": hp + ap,
            "winner": None if hp == ap else (h if hp > ap else a),
        })
    return rank_entertainment(rows)


def rank_entertainment(matches):
    """Rated relative to the week: closeness first, quality of football second."""
    if not matches:
        return matches
    margins = [m["margin"] for m in matches]
    combos = [m["combined"] for m in matches]
    m_lo, m_hi = min(margins), max(margins)
    c_lo, c_hi = min(combos), max(combos)

    for m in matches:
        close = 1.0 if m_hi == m_lo else 1 - (m["margin"] - m_lo) / (m_hi - m_lo)
        qual = 1.0 if c_hi == c_lo else (m["combined"] - c_lo) / (c_hi - c_lo)
        m["_raw"] = 0.62 * close + 0.38 * qual

    order = sorted(matches, key=lambda m: -m["_raw"])
    curve = config.ENTERTAINMENT_CURVE.get(len(order))
    for i, m in enumerate(order):
        if curve:
            m["entertainment"] = curve[i]
        else:
            m["entertainment"] = max(1, min(10, round(1 + 9 * m["_raw"])))
        m["excitement_rank"] = i + 1
        del m["_raw"]
    return order


def build_standings(details, managers, upto_gw):
    """Recomputed from finished matches.

    The API's own standings block reports matches_played as 38 for every
    manager before a ball is kicked, so it is not trusted for that column.
    """
    table = {le: {"w": 0, "d": 0, "l": 0, "for": 0, "against": 0, "played": 0}
             for le in managers}
    history = {le: [] for le in managers}

    for m in sorted(details["matches"], key=lambda x: x["event"]):
        if not m.get("finished") or m["event"] > upto_gw:
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        for me, opp, mine, theirs in ((h, a, hp, ap), (a, h, ap, hp)):
            t = table[me]
            t["played"] += 1
            t["for"] += mine
            t["against"] += theirs
            if mine > theirs:
                t["w"] += 1
            elif mine == theirs:
                t["d"] += 1
            else:
                t["l"] += 1
            history[me].append({"event": m["event"], "pts": mine, "opp": opp,
                                "result": "W" if mine > theirs else "D" if mine == theirs else "L"})

    rows = []
    for le, t in table.items():
        rows.append({
            "league_entry": le,
            "manager": managers[le]["manager"],
            "team": managers[le]["team"],
            **t,
            "points": t["w"] * 3 + t["d"],
            "diff": t["for"] - t["against"],
            "history": history[le],
        })
    rows.sort(key=lambda r: (-r["points"], -r["for"], -r["diff"]))

    # Shared ranks, then movement against the previous gameweek.
    prev = previous_order(details, managers, upto_gw - 1)
    last_pos, last_key = 0, None
    for i, r in enumerate(rows, start=1):
        key = (r["points"], r["for"])
        r["pos"] = last_pos if key == last_key else i
        last_pos, last_key = r["pos"], key
        was = prev.get(r["league_entry"])
        r["move"] = None if was is None else was - r["pos"]
    return rows


def previous_order(details, managers, upto_gw):
    if upto_gw < 1:
        return {}
    prior = build_standings_flat(details, managers, upto_gw)
    return {r["league_entry"]: i for i, r in enumerate(prior, start=1)}


def build_standings_flat(details, managers, upto_gw):
    table = {le: {"w": 0, "d": 0, "for": 0} for le in managers}
    for m in details["matches"]:
        if not m.get("finished") or m["event"] > upto_gw:
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        for me, mine, theirs in ((h, hp, ap), (a, ap, hp)):
            table[me]["for"] += mine
            if mine > theirs:
                table[me]["w"] += 1
            elif mine == theirs:
                table[me]["d"] += 1
    rows = [{"league_entry": le, "points": t["w"] * 3 + t["d"], "for": t["for"]}
            for le, t in table.items()]
    rows.sort(key=lambda r: (-r["points"], -r["for"]))
    return rows


def build_next_fixtures(details, managers, gw):
    nxt = [m for m in details["matches"] if m["event"] == gw + 1]
    return [{
        "event": m["event"],
        "home": managers[m["league_entry_1"]]["manager"],
        "away": managers[m["league_entry_2"]]["manager"],
        "home_team": managers[m["league_entry_1"]]["team"],
        "away_team": managers[m["league_entry_2"]]["team"],
    } for m in nxt]


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

def build_transactions(managers, raw, event, players):
    """Waiver and free agent moves FPL recorded for one gameweek, per manager.

    Reliably empty: draft/entry/{id}/transactions 403s for every manager
    from a cloud runner, every run. Kept as the preferred source in case
    that ever stops being true; infer_transfers below is what actually
    populates the site today.
    """
    by_entry = {m["entry_id"]: le for le, m in managers.items()}
    out = {le: {"in": [], "out": [], "count": 0} for le in managers}
    if not raw:
        return out
    for entry_str, payload in raw.items():
        le = by_entry.get(int(entry_str))
        if le is None:
            continue
        for t in payload.get("transactions", []):
            if t.get("event") != event or t.get("result") != "a":
                continue
            out[le]["in"].append(describe_player(t.get("element_in"), players))
            out[le]["out"].append(describe_player(t.get("element_out"), players))
            out[le]["count"] += 1
    return out


def infer_transfers(managers, players, prev_squads_raw, curr_squads_raw):
    """Approximate transfers by diffing two gameweeks' full squads.

    The transactions endpoint is unreachable from a cloud runner, but the
    picks endpoint isn't, so a squad change between one deadline and the
    next is read as a transfer -- waiver, free agent or trade, all
    indistinguishable here, but real, unlike the empty transactions feed.
    Requires a complete squad (>=11 picks) on both sides; a partial or
    missing snapshot is skipped rather than read as mass releases.
    """
    by_entry = {m["entry_id"]: le for le, m in managers.items()}
    out = {le: {"in": [], "out": [], "count": 0} for le in managers}
    if not prev_squads_raw or not curr_squads_raw:
        return out
    for entry_str, payload in curr_squads_raw.items():
        le = by_entry.get(int(entry_str))
        if le is None:
            continue
        prev_payload = prev_squads_raw.get(entry_str)
        if prev_payload is None:
            continue
        prev_picks = prev_payload.get("picks", [])
        curr_picks = payload.get("picks", [])
        if len(prev_picks) < 11 or len(curr_picks) < 11:
            continue
        prev_ids = {p["element"] for p in prev_picks}
        curr_ids = {p["element"] for p in curr_picks}
        ins = curr_ids - prev_ids
        outs = prev_ids - curr_ids
        out[le] = {
            "in": sorted((describe_player(e, players) for e in ins), key=lambda p: p["name"]),
            "out": sorted((describe_player(e, players) for e in outs), key=lambda p: p["name"]),
            "count": max(len(ins), len(outs)),
        }
    return out


def build_transfers(managers, players, raw_transactions, event, prev_squads_raw, curr_squads_raw):
    """Prefer FPL's own transaction record; fall back to the squad diff."""
    real = build_transactions(managers, raw_transactions, event, players)
    inferred = infer_transfers(managers, players, prev_squads_raw, curr_squads_raw)
    out = {}
    for le in managers:
        r, i = real[le], inferred[le]
        if r["count"]:
            out[le] = {**r, "source": "recorded"}
        elif i["count"]:
            out[le] = {**i, "source": "inferred"}
        else:
            out[le] = {"in": [], "out": [], "count": 0, "source": "none"}
    return out


def build_team_of_week(managers, totw_squads):
    """Best possible XI pooled from every manager's active XI that week.

    Not a per-manager metric: every player started anywhere in the league
    is eligible. Bench players don't qualify -- same convention as the
    Highest Scorer Rule elsewhere in this file, where bench points are
    exempt: a manager didn't play a benched player, whatever that player
    did in their real match. Formation minimums (1 GK, 3 DEF, 2 MID, 1
    FWD) are filled with the best at each position; the four remaining
    slots go to whoever scored highest among what's left, regardless of
    position. That greedy fill is optimal here -- there's no upper bound
    on any outfield position, only lower bounds, so nothing is ever
    gained by holding back a high scorer to satisfy a minimum a lower
    scorer could have met instead.
    """
    if not totw_squads:
        return None

    pool = {}
    for le, squad in totw_squads.items():
        for row in squad["xi"]:
            pool[row["element"]] = {**row, "manager": managers[le]["manager"]}
    if not pool:
        return None

    by_pos = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    for p in pool.values():
        if p["pos"] in by_pos:
            by_pos[p["pos"]].append(p)
    for group in by_pos.values():
        group.sort(key=lambda p: (-p["points"], p["name"]))

    minimums = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
    selected, selected_ids = [], set()
    for pos, n in minimums.items():
        for p in by_pos[pos][:n]:
            selected.append(p)
            selected_ids.add(p["element"])

    remaining = [p for p in by_pos["DEF"] + by_pos["MID"] + by_pos["FWD"]
                 if p["element"] not in selected_ids]
    remaining.sort(key=lambda p: (-p["points"], p["name"]))
    flex_needed = 11 - len(selected)
    selected.extend(remaining[:flex_needed])

    order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
    selected.sort(key=lambda p: (order[p["pos"]], -p["points"]))

    formation = "-".join(
        str(sum(1 for p in selected if p["pos"] == pos)) for pos in ("DEF", "MID", "FWD"))
    return {
        "players": [{"name": p["name"], "pos": p["pos"], "club": p["club"],
                     "photo": p["photo"], "points": p["points"], "manager": p["manager"]}
                    for p in selected],
        "formation": formation,
        "total_points": sum(p["points"] for p in selected),
    }


def build_transfer_swaps(managers, players, totw_squads, prev_squads_raw, totw_live):
    """Every qualifying transfer swap for the team-of-week gameweek, each
    with the point swing it produced.

    A swap counts regardless of whether the release was mandated by the
    Highest Scorer Rule or entirely voluntary -- what decides whether it
    reads as a best or worst transfer is purely the points swing, not why
    the swap happened.

    Both sides have to have actually played, though: the outgoing player
    must have started (been in the active XI, not the bench) the previous
    gameweek, and the incoming player must have started this one. A
    benched player, either side, isn't a real comparison -- if the
    pickup sat on the bench this week, there's nothing to compare their
    non-existent contribution against.

    diff = in_points - out_points, using each player's real score that
    gameweek independent of who rostered them. Positive is a gain, a
    Best Transfers candidate; negative is a loss, Worst Transfers.

    A manager who made several swaps at once can't be traced to which in
    replaced which out -- the picks endpoint doesn't carry that, only the
    before/after squad. Paired same position first (the likeliest real
    swap), any leftover by score rank.
    """
    if not prev_squads_raw or not totw_squads:
        return []

    pts = live_points(totw_live)
    by_entry = {m["entry_id"]: le for le, m in managers.items()}

    def describe(eid):
        meta = players.get(eid, {"name": str(eid), "pos": "", "club": ""})
        return {"element": eid, "name": meta["name"], "pos": meta["pos"],
                "club": meta["club"], "points": pts.get(eid, 0)}

    swaps = []
    for entry_str, payload in prev_squads_raw.items():
        le = by_entry.get(int(entry_str))
        squad = totw_squads.get(le)
        if le is None or squad is None:
            continue
        manager_name = managers[le]["manager"]

        prev_picks = payload.get("picks", [])
        prev_ids = {p["element"] for p in prev_picks}
        prev_xi_ids = {p["element"] for p in prev_picks if p.get("position", 99) <= 11}
        curr_ids = {row["element"] for row in squad["xi"] + squad["bench"]}

        outs = [describe(eid) for eid in prev_ids - curr_ids if eid in prev_xi_ids]
        ins = [row for row in squad["xi"] if row["element"] not in prev_ids]
        if not outs or not ins:
            continue

        pairs, rem_outs, rem_ins = [], list(outs), list(ins)
        for pos in ("GKP", "DEF", "MID", "FWD"):
            pos_outs = sorted((o for o in rem_outs if o["pos"] == pos), key=lambda o: -o["points"])
            pos_ins = sorted((i for i in rem_ins if i["pos"] == pos), key=lambda i: -i["points"])
            for o, i in zip(pos_outs, pos_ins):
                pairs.append((o, i))
                rem_outs.remove(o)
                rem_ins.remove(i)
        rem_outs.sort(key=lambda o: -o["points"])
        rem_ins.sort(key=lambda i: -i["points"])
        pairs.extend(zip(rem_outs, rem_ins))

        for o, i in pairs:
            swaps.append({
                "manager": manager_name,
                "out_name": o["name"], "out_club": o["club"], "out_points": o["points"],
                "in_name": i["name"], "in_club": i["club"], "in_points": i["points"],
                "diff": i["points"] - o["points"],
            })
    return swaps


def best_and_worst_transfers(swaps, limit=5):
    """Split the swap list into top-5 gains and top-5 losses."""
    best = sorted((s for s in swaps if s["diff"] > 0), key=lambda s: (-s["diff"], s["out_name"]))[:limit]
    worst = sorted((s for s in swaps if s["diff"] < 0), key=lambda s: (s["diff"], s["out_name"]))[:limit]
    return ([{**s, "gain": s["diff"]} for s in best],
            [{**s, "loss": -s["diff"]} for s in worst])


def manager_week_scores(managers, players, gw_squads_raw, gw_live, prev_squads_raw):
    """Each manager's score for one gameweek: active-XI points scored,
    plus the full point swing (see build_transfer_swaps) from any
    qualifying transfer that week -- deliberately double-weighting the
    transfer decision, since the incoming player's points already count
    once toward the raw score and the swing is added again on top as a
    bonus for the call itself.
    """
    if not gw_squads_raw or not gw_live:
        return {}
    squads = build_squads(managers, gw_squads_raw, gw_live, players)
    swaps = build_transfer_swaps(managers, players, squads, prev_squads_raw, gw_live) if prev_squads_raw else []
    swing = {}
    for s in swaps:
        swing[s["manager"]] = swing.get(s["manager"], 0) + s["diff"]
    return {managers[le]["manager"]: squad["xi_points"] + swing.get(managers[le]["manager"], 0)
            for le, squad in squads.items()}


def build_manager_of_week(managers, players, totw_gw, totw_squads_raw, totw_live, prev_totw_squads_raw):
    """Top 3 and worst 3 managers for the team-of-week gameweek."""
    scores = manager_week_scores(managers, players, totw_squads_raw, totw_live, prev_totw_squads_raw)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    worst = ranked[-3:][::-1] if len(ranked) >= 3 else []
    return {
        "gameweek": totw_gw,
        "top": [{"manager": n, "points": p} for n, p in ranked[:3]],
        "worst": [{"manager": n, "points": p} for n, p in worst],
    }


def _block_standings(managers, players, load_fn, block_start, block_end):
    """Summed manager-week scores across gameweeks block_start..block_end.
    A gameweek with no squads/live data on disk yet (not played, or not
    fetched) simply contributes nothing -- callers don't need to worry
    about how far the block has actually progressed.
    """
    totals = {}
    for g in range(block_start, block_end + 1):
        g_squads_raw = load_fn(f"squads_gw{g}")
        g_live = load_fn(f"live_gw{g}")
        if not g_squads_raw or not g_live:
            continue
        g_prev_squads_raw = load_fn(f"squads_gw{g - 1}") if g > 1 else None
        for manager_name, score in manager_week_scores(
                managers, players, g_squads_raw, g_live, g_prev_squads_raw).items():
            totals[manager_name] = totals.get(manager_name, 0) + score
    if not totals:
        return []
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    return [{"manager": n, "points": p} for n, p in ranked]


def build_manager_of_month(managers, players, totw_gw, load_fn):
    """Manager of the Month: a rolling 4-gameweek competition (GW1-4,
    GW5-8, ...), by the same per-week score as Manager of the Week,
    summed across the block. The block containing totw_gw is "current"
    and its standings are shown live, updating gameweek by gameweek as
    they accumulate -- it's only finalised, and its winner crowned, once
    totw_gw reaches the block's last gameweek (a multiple of 4), at
    which point the next block starts fresh from zero.

    Also returns "history": every earlier block that's already finished,
    most recent first, plus a "leaderboard" tally of how many months
    each manager has won -- the record book for a separate page, since
    the live standings above are the only thing that needs to be
    front-and-centre week to week.
    """
    current_end = ((totw_gw + 3) // 4) * 4
    current_start = current_end - 3
    current_standings = _block_standings(managers, players, load_fn, current_start, totw_gw)
    current = None
    if current_standings:
        current = {
            "block_start": current_start,
            "block_end": current_end,
            "is_final": totw_gw == current_end,
            "manager": current_standings[0]["manager"],
            "points": current_standings[0]["points"],
            "standings": current_standings,
        }

    history = []
    for block_end in range(4, current_end, 4):
        block_start = block_end - 3
        standings = _block_standings(managers, players, load_fn, block_start, block_end)
        if standings:
            history.append({
                "block_start": block_start, "block_end": block_end,
                "manager": standings[0]["manager"], "points": standings[0]["points"],
                "standings": standings,
            })
    history.reverse()

    wins = {}
    for h in history:
        wins[h["manager"]] = wins.get(h["manager"], 0) + 1
    leaderboard = sorted(wins.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "current": current,
        "history": history,
        "leaderboard": [{"manager": n, "wins": w} for n, w in leaderboard],
    }


def build_manager_profiles(details, managers, players, totw_gw, load_fn):
    """Everything a manager's own page needs: their fixture history and
    head-to-head record (from the full season schedule, so this only gets
    more interesting as more rounds are played), their biggest single-match
    win, the best individual player performance their active XI has ever
    produced, and their personal best and worst transfer swaps.

    Best player performance and transfer swaps are found by re-running the
    same per-gameweek computations (build_squads, build_transfer_swaps)
    used elsewhere in this file across every settled gameweek 1..totw_gw,
    rather than reusing a single week's result -- there's no shortcut, a
    manager's all-time best is only knowable by having looked at all of it.
    """
    fixtures = {le: [] for le in managers}
    h2h = {le: {} for le in managers}
    for m in sorted(details["matches"], key=lambda x: x["event"]):
        if not m.get("finished"):
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        for me, opp, mine, theirs in ((h, a, hp, ap), (a, h, ap, hp)):
            if me not in managers or opp not in managers:
                continue
            result = "W" if mine > theirs else "D" if mine == theirs else "L"
            fixtures[me].append({
                "gameweek": m["event"], "opponent": managers[opp]["manager"],
                "points": mine, "against": theirs, "margin": mine - theirs,
                "result": result,
            })
            rec = h2h[me].setdefault(opp, {"w": 0, "d": 0, "l": 0})
            rec[result.lower()] += 1

    best_player = {le: None for le in managers}
    best_transfer = {le: None for le in managers}
    worst_transfer = {le: None for le in managers}
    by_manager = {m["manager"]: le for le, m in managers.items()}

    for g in range(1, totw_gw + 1):
        g_squads_raw = load_fn(f"squads_gw{g}")
        g_live = load_fn(f"live_gw{g}")
        if not g_squads_raw or not g_live:
            continue
        squads = build_squads(managers, g_squads_raw, g_live, players)
        for le, squad in squads.items():
            for row in squad["xi"]:
                cur = best_player[le]
                if cur is None or row["points"] > cur["points"]:
                    best_player[le] = {"gameweek": g, "name": row["name"],
                                        "club": row["club"], "points": row["points"]}

        if g == 1:
            continue
        g_prev_squads_raw = load_fn(f"squads_gw{g - 1}")
        if not g_prev_squads_raw:
            continue
        swaps = build_transfer_swaps(managers, players, squads, g_prev_squads_raw, g_live)
        for s in swaps:
            le = by_manager.get(s["manager"])
            if le is None:
                continue
            tagged = {**s, "gameweek": g}
            if s["diff"] > 0 and (best_transfer[le] is None or s["diff"] > best_transfer[le]["diff"]):
                best_transfer[le] = tagged
            if s["diff"] < 0 and (worst_transfer[le] is None or s["diff"] < worst_transfer[le]["diff"]):
                worst_transfer[le] = tagged

    profiles = {}
    for le, m in managers.items():
        record = h2h[le]
        most_beaten = max(record.items(), key=lambda kv: kv[1]["w"], default=(None, None))
        most_lost_to = max(record.items(), key=lambda kv: kv[1]["l"], default=(None, None))
        wins = [f for f in fixtures[le] if f["result"] == "W"]
        biggest_win = max(wins, key=lambda f: f["margin"], default=None)
        profiles[m["manager"]] = {
            "fixtures": sorted(fixtures[le], key=lambda f: -f["gameweek"]),
            "biggest_win": biggest_win,
            "best_player": best_player[le],
            "most_beaten": {"manager": managers[most_beaten[0]]["manager"], "wins": most_beaten[1]["w"]}
                if most_beaten[0] is not None and most_beaten[1]["w"] > 0 else None,
            "most_lost_to": {"manager": managers[most_lost_to[0]]["manager"], "losses": most_lost_to[1]["l"]}
                if most_lost_to[0] is not None and most_lost_to[1]["l"] > 0 else None,
            "best_transfer": best_transfer[le],
            "worst_transfer": worst_transfer[le],
        }
    return profiles


# --------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int)
    args = ap.parse_args()

    details = load("league_details")
    if not details:
        raise SystemExit("No data/raw/league_details.json. Run fetch.py first.")

    meta = load("meta", {})
    gw = args.gw or meta.get("gameweek") or max(
        (m["event"] for m in details["matches"] if m.get("finished")), default=1)

    bootstrap = load("bootstrap_static")
    players = player_index(bootstrap)
    live = load(f"live_gw{gw}")
    squads_raw = load(f"squads_gw{gw}")
    prev_squads_raw = load(f"squads_gw{gw - 1}")
    raw_transactions = load("transactions")

    # Team of the week always shows the last gameweek whose squads and
    # scores are fully settled -- one behind gw, since gw itself is still
    # either in progress or just started (per fetch.py's current_event
    # semantics). Floored at 1: there's no gameweek 0 to fall back to.
    totw_gw = max(1, gw - 1)
    totw_squads_raw = load(f"squads_gw{totw_gw}")
    totw_live = load(f"live_gw{totw_gw}")
    prev_totw_squads_raw = load(f"squads_gw{totw_gw - 1}")

    managers = build_managers(details)
    matches = build_matches(details, gw, managers)

    results_by_entry = {}
    for m in matches:
        for me, opp, mine, theirs in ((m["home"], m["away"], m["home_points"], m["away_points"]),
                                      (m["away"], m["home"], m["away_points"], m["home_points"])):
            results_by_entry[me] = {
                "opponent": managers[opp]["manager"],
                "points": mine, "against": theirs,
                "result": "W" if mine > theirs else "D" if mine == theirs else "L",
            }

    squads = build_squads(managers, squads_raw, live, players)
    releases = build_releases(managers, squads, results_by_entry)
    source = "computed"
    if not releases:
        releases = manual_releases(gw, managers, results_by_entry, players)
        source = "manual" if releases else "none"

    prev_path = DERIVED / f"gw{gw - 1}_releases.json"
    prev_releases = json.loads(prev_path.read_text()) if prev_path.exists() else None

    totw_squads = build_squads(managers, totw_squads_raw, totw_live, players) if totw_squads_raw and totw_live else {}
    team_of_week = build_team_of_week(managers, totw_squads)
    transfer_swaps = build_transfer_swaps(managers, players, totw_squads, prev_totw_squads_raw, totw_live)
    best_transfers, worst_transfers = best_and_worst_transfers(transfer_swaps)

    manager_of_week = build_manager_of_week(
        managers, players, totw_gw, totw_squads_raw, totw_live, prev_totw_squads_raw)
    manager_of_month = build_manager_of_month(managers, players, totw_gw, load)
    manager_profiles = build_manager_profiles(details, managers, players, totw_gw, load)

    gaps = []
    if not squads_raw:
        gaps.append(
            f"Squad picks for gameweek {gw} are not on disk, so bench waste, "
            f"blanks and rule-breach detection cannot be computed. Run "
            f"`python fetch.py --gw {gw}` to fill them."
            + (" Releases are taken from data/manual, and percentages are"
               " computed from those figures." if source == "manual" else "")
        )
    if not bootstrap:
        gaps.append("Player names are unavailable without bootstrap_static.json.")
    if not team_of_week:
        gaps.append(f"Squad picks or live scores for gameweek {totw_gw} are not on "
                     f"disk, so Team of the Week cannot be computed.")

    payload = {
        "league": {
            "id": config.LEAGUE_ID,
            "name": details["league"].get("name", config.LEAGUE_NAME),
            "season": config.SEASON,
            "gameweek": gw,
            "next_gameweek": gw + 1,
            "fetched_at": meta.get("fetched_at"),
            "transaction_mode": details["league"].get("transaction_mode"),
            "scoring": details["league"].get("scoring"),
        },
        "managers": {str(k): v for k, v in managers.items()},
        "matches": matches,
        "squads": {str(k): v for k, v in squads.items()},
        "releases": releases,
        "release_efficiency": sorted(releases, key=lambda r: r["cost_pct"]),
        "breaches": detect_breaches(prev_releases, squads),
        "standings": build_standings(details, managers, gw),
        "next_fixtures": build_next_fixtures(details, managers, gw),
        "transfers": {str(k): v for k, v in build_transfers(
            managers, players, raw_transactions, gw, prev_squads_raw, squads_raw).items()},
        "team_of_week": {
            "gameweek": totw_gw,
            "players": (team_of_week or {}).get("players", []),
            "formation": (team_of_week or {}).get("formation"),
            "total_points": (team_of_week or {}).get("total_points", 0),
            "best_transfers": best_transfers,
            "worst_transfers": worst_transfers,
        },
        "manager_of_week": manager_of_week,
        "manager_of_month": manager_of_month,
        "manager_profiles": manager_profiles,
        "pot": {
            "base": config.BASE_POT,
            "prize_share": config.PRIZE_SHARE,
        },
        "gaps": gaps,
    }

    DERIVED.mkdir(parents=True, exist_ok=True)
    (DERIVED / f"gw{gw}.json").write_text(json.dumps(payload, indent=1))
    (DERIVED / f"gw{gw}_releases.json").write_text(json.dumps(releases, indent=1))
    print(f"Wrote data/derived/gw{gw}.json")
    for g in gaps:
        print(f"  gap: {g}")


if __name__ == "__main__":
    main()
