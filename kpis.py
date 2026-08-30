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
        }
        for p in bootstrap.get("elements", [])
    }


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
            meta = players.get(eid, {"name": str(eid), "pos": "", "club": ""})
            row = {
                "element": eid,
                "name": meta["name"],
                "pos": meta["pos"],
                "club": meta["club"],
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
            "release_points": top["points"],
            "tie": [r["name"] for r in tied] if len(tied) > 1 else [],
            "next": nxt["name"] if nxt else None,
            "next_points": nxt["points"] if nxt else None,
            "next_tie": [r["name"] for r in nxt_tied] if len(nxt_tied) > 1 else [],
            "score": score,
            "cost_pct": round(100 * top["points"] / score, 1) if score else 0.0,
            "h2h": results_by_entry.get(le, {}).get("result"),
        })
    rows.sort(key=lambda r: -r["release_points"])
    return rows


def manual_releases(gw, managers, results_by_entry):
    """Fallback for weeks where you have the releases but not the picks.

    Drop a list of {manager, release, release_points, next, next_points, score}
    into data/manual/gw{n}_releases.json and the ledger renders from that.
    Percentages and H2H are still computed here, never typed by hand.
    """
    path = ROOT / "data" / "manual" / f"gw{gw}_releases.json"
    if not path.exists():
        return []
    by_name = {m["manager"]: le for le, m in managers.items()}
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
            "release_points": r["release_points"],
            "tie": r.get("tie", []),
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
            out[le]["in"].append(players.get(t.get("element_in"), {}).get("name", t.get("element_in")))
            out[le]["out"].append(players.get(t.get("element_out"), {}).get("name", t.get("element_out")))
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
        name = lambda eid: players.get(eid, {}).get("name", str(eid))
        out[le] = {
            "in": sorted(name(e) for e in ins),
            "out": sorted(name(e) for e in outs),
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
    next_squads_raw = load(f"squads_gw{gw + 1}")
    raw_transactions = load("transactions")

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
        releases = manual_releases(gw, managers, results_by_entry)
        source = "manual" if releases else "none"

    prev_path = DERIVED / f"gw{gw - 1}_releases.json"
    prev_releases = json.loads(prev_path.read_text()) if prev_path.exists() else None

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

    payload = {
        "league": {
            "id": config.LEAGUE_ID,
            "name": details["league"].get("name", config.LEAGUE_NAME),
            "season": config.SEASON,
            "gameweek": gw,
            "next_gameweek": gw + 1,
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
        "transfers": {
            "current": {str(k): v for k, v in build_transfers(
                managers, players, raw_transactions, gw, prev_squads_raw, squads_raw).items()},
            "next": {str(k): v for k, v in build_transfers(
                managers, players, raw_transactions, gw + 1, squads_raw, next_squads_raw).items()},
        },
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
