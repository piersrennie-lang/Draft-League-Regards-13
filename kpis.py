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
from autosub import calculate_effective_lineup, _fixture_over

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

DISPLAY_FIRST_NAME = {"Nicolas": "Nic"}


def build_managers(details):
    """Keyed by league_entry id, which is what matches and standings use.

    Note the two-id trap: league_entries carry both `id` (the league entry,
    used by matches and standings) and `entry_id` (the team, used by squad and
    transaction endpoints). Mixing them up is the most common way this breaks.

    `manager` is a display name -- DISPLAY_FIRST_NAME overrides a first name
    for every view that reads it, without touching the API's own fields.
    """
    out = {}
    for e in details["league_entries"]:
        first = DISPLAY_FIRST_NAME.get(e["player_first_name"], e["player_first_name"])
        out[e["id"]] = {
            "league_entry": e["id"],
            "entry_id": e["entry_id"],
            "manager": f"{first} {e['player_last_name']}",
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
            "team_id": p.get("team"),
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


def live_stats(live):
    """Per-player minutes + points this gameweek, for the autosub engine --
    live_points() above only carries points, but calculate_effective_lineup()
    also needs minutes to tell "played" from "hasn't played yet"."""
    if not live:
        return {}
    out = {}
    for pid, data in (live.get("elements") or {}).items():
        s = data.get("stats") or {}
        out[int(pid)] = {"minutes": s.get("minutes", 0), "points": s.get("total_points", 0)}
    return out


def build_squads(managers, squads_raw, live, players):
    """Split each squad into XI and bench and attach points.

    Draft uses picks position 1-11 for the active eleven and 12-15 for the
    bench, so the Highest Scorer Rule only ever looks at positions 1-11.

    Also projects the *effective* (post-autosub) lineup via
    calculate_effective_lineup() -- FPL Draft itself doesn't apply
    automatic substitutions until a gameweek is fully over, so "xi"/
    "xi_points" here stay the manager's literal submitted selection
    (what the Highest Scorer Rule and breach detection care about), while
    "effective_xi"/"effective_xi_points"/"autosubs" are the live-scoring
    projection of what FPL will eventually settle on.
    """
    pts = live_points(live)
    gw_stats = live_stats(live)
    fixtures = (live or {}).get("fixtures") or []
    out = {}
    if not squads_raw:
        return out

    by_entry = {m["entry_id"]: le for le, m in managers.items()}

    for entry_str, payload in squads_raw.items():
        le = by_entry.get(int(entry_str))
        if le is None:
            continue
        xi, bench = [], []
        bench_priority = []  # (pick_position, row) -- the manager's own bench order
        for pick in payload.get("picks", []):
            eid = pick["element"]
            meta = players.get(eid, {"name": str(eid), "pos": "", "club": "", "photo": ""})
            row = {
                "element": eid,
                "name": meta["name"],
                "pos": meta["pos"],
                "club": meta["club"],
                "team_id": meta.get("team_id"),
                "photo": meta.get("photo", ""),
                "points": pts.get(eid, 0),
            }
            position = pick.get("position", 99)
            if position <= 11:
                xi.append(row)
            else:
                bench.append(row)
                bench_priority.append((position, row))

        bench_priority.sort(key=lambda pr: pr[0])
        ordered_bench = [row for _, row in bench_priority]

        if any(r["pos"] == "GKP" for r in xi):
            effective = calculate_effective_lineup(xi, ordered_bench, gw_stats, fixtures)
        else:
            # No goalkeeper found in the starting XI -- almost certainly
            # missing player/position data (e.g. bootstrap_static not
            # fetched yet) rather than a real squad, so fall back to the
            # submitted lineup rather than crash the whole build.
            effective = {
                "effective_players": [r["element"] for r in xi],
                "autosubs": [], "unresolved_players": [],
                "points": sum(r["points"] for r in xi), "is_final": False,
            }
        effective_ids = set(effective["effective_players"])
        effective_xi = [r for r in xi + bench if r["element"] in effective_ids]

        xi.sort(key=lambda r: -r["points"])
        bench.sort(key=lambda r: -r["points"])
        out[le] = {
            "effective_xi": effective_xi,
            "effective_xi_points": effective["points"],
            "autosubs": effective["autosubs"],
            "unresolved_players": effective["unresolved_players"],
            "is_final": effective["is_final"],
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
        if top["points"] <= 0:
            # Nobody's kicked off yet (or everyone's blanked) -- every
            # starter is tied at 0, which reads as "must release your
            # whole team" rather than a real mandate. Wait for a genuine
            # highest scorer before this manager gets a release row.
            continue
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
            "release_club": top["club"],
            "release_points": top["points"],
            "tie": [{"name": r["name"], "photo": r["photo"], "club": r["club"]} for r in tied] if len(tied) > 1 else [],
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
    club_by_name = {p["name"]: p.get("club", "") for p in players.values()}
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
            "release_club": club_by_name.get(r["release"], ""),
            "release_points": r["release_points"],
            "tie": [{"name": n, "photo": photo_by_name.get(n, ""), "club": club_by_name.get(n, "")} for n in r.get("tie", [])],
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
    """A breach is a mandated release nobody satisfied.

    When several players were tied for the mandate, releasing any one of
    them satisfies the rule (see build_releases' "tie" list, which
    already includes the designated "release" player alongside its
    ties) -- so this only flags a breach when every tied candidate is
    still sitting somewhere in the squad, not just the one arbitrarily
    named "release".
    """
    out = []
    for row in prev_releases or []:
        squad = squads.get(row["league_entry"])
        if not squad:
            continue
        candidates = row["tie"] if row.get("tie") else [
            {"name": row["release"], "photo": row.get("release_photo", ""), "club": row.get("release_club", "")}
        ]
        in_xi_names = {p["name"] for p in squad["xi"]}
        bench_names = {p["name"] for p in squad["bench"]}
        if any(c["name"] not in in_xi_names and c["name"] not in bench_names for c in candidates):
            continue  # at least one tied candidate was actually released
        any_in_xi = any(c["name"] in in_xi_names for c in candidates)
        out.append({
            "manager": row["manager"],
            "release": row["release"],
            "release_photo": row.get("release_photo", ""),
            "release_club": row.get("release_club", ""),
            "tie": row.get("tie", []),
            "points": row["release_points"],
            "status": "Still in XI" if any_in_xi else "On bench",
            "fine": config.FINE_NOT_RELEASED + (config.FINE_FIELDED_ANYWAY if any_in_xi else 0),
        })
    return out


# --------------------------------------------------------------------------
# Matches, entertainment, standings
# --------------------------------------------------------------------------

def fixtures_remaining(squads, live):
    """How many of each manager's active-XI players' clubs haven't kicked
    off yet this gameweek -- i.e. how much of their live score could still
    move. Cross-references live_gw{n}.json's own real-world fixture list
    (team_h/team_a, started) against each starter's club.
    """
    real_fixtures = (live or {}).get("fixtures") or []
    not_started_clubs = set()
    for f in real_fixtures:
        if not f.get("started"):
            not_started_clubs.add(f.get("team_h"))
            not_started_clubs.add(f.get("team_a"))
    return {
        le: sum(1 for p in squad["xi"] if p.get("team_id") in not_started_clubs)
        for le, squad in squads.items()
    }


def gw_window(live):
    """(kicked_off, fully_over) for this gameweek's real-world match
    calendar -- kicked_off once any of its fixtures has started, fully_over
    once every one of them has reached full time. "Over" means finished OR
    finished_provisional (see _fixture_over/autosub.py): waiting for the
    stricter "finished" flag would keep this window open for hours or days
    after the real football has actually ended, since that flag only
    flips once bonus/BPS is confirmed.

    Deliberately independent of the FPL Draft league's own match-level
    started/finished (which lags the same way) and of whether the next
    gameweek's waiver or transfer window has opened -- this is purely
    "has this gameweek's actual football been played", nothing else.
    """
    fixtures = (live or {}).get("fixtures") or []
    if not fixtures:
        return False, False
    kicked_off = any(f.get("started") for f in fixtures)
    fully_over = all(_fixture_over(f) for f in fixtures)
    return kicked_off, fully_over


def build_results(details, gw, managers, squads, live):
    """Every head-to-head fixture for the current gameweek, live or not --
    unlike build_matches() this doesn't filter down to finished ones, since
    the whole point is to show what's in progress right now.

    While a match is still live, its score comes from our own projected
    effective_xi_points rather than league_entry_1_points/
    league_entry_2_points -- the FPL Draft API only applies autosubs until
    the whole gameweek settles, so its own live number is just the raw
    submitted-XI sum until then. Once finished, FPL's own number is
    authoritative and used as-is.

    "started"/"finished" here describe the real-world match calendar
    (gw_window), not the FPL Draft match's own flags -- so the Live tag
    disappears the moment the last real fixture ends, rather than staying
    on until bonus points are confirmed league-wide.
    """
    remaining = fixtures_remaining(squads, live)
    kicked_off, fully_over = gw_window(live)
    rows = []
    for m in details["matches"]:
        if m["event"] != gw:
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        if m.get("finished"):
            hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        else:
            hp = squads.get(h, {}).get("effective_xi_points", m["league_entry_1_points"])
            ap = squads.get(a, {}).get("effective_xi_points", m["league_entry_2_points"])
        rows.append({
            "home": h, "away": a,
            "home_name": managers[h]["manager"], "away_name": managers[a]["manager"],
            "home_points": hp, "away_points": ap,
            "home_remaining": remaining.get(h, 0),
            "away_remaining": remaining.get(a, 0),
            "started": kicked_off,
            "finished": fully_over,
            "winner": None if hp == ap else ("home" if hp > ap else "away"),
        })
    return rows


def build_schedule(bootstrap, live, gw):
    """When this gameweek's last real-world fixture kicks off, and when the
    next gameweek's waiver and transfer windows move -- all sourced from
    bootstrap_static's own events list (deadline_time/trades_time/
    waivers_time per gameweek) plus this gameweek's own fixture list.
    """
    teams = {t["id"]: t.get("name", t.get("short_name", ""))
             for t in (bootstrap or {}).get("teams", [])}
    events = {e["id"]: e for e in (bootstrap or {}).get("events", {}).get("data", [])}

    fixtures = [f for f in (live or {}).get("fixtures") or [] if f.get("kickoff_time")]
    last_fixture = None
    if fixtures:
        f = max(fixtures, key=lambda f: f["kickoff_time"])
        last_fixture = {
            "home_team": teams.get(f.get("team_h"), "TBC"),
            "away_team": teams.get(f.get("team_a"), "TBC"),
            "kickoff_time": f["kickoff_time"],
        }

    next_event = events.get(gw + 1, {})
    return {
        "last_fixture": last_fixture,
        "next_gameweek": gw + 1,
        "waivers_time": next_event.get("waivers_time"),
        "deadline_time": next_event.get("deadline_time"),
    }


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


def build_standings(details, managers, upto_gw, live_gw=None, live_squads=None,
                     live_kicked_off=False, live_fully_over=False):
    """Recomputed from finished matches, plus the current gameweek's live
    projection once its real-world fixtures have kicked off (live_kicked_off,
    from gw_window() -- not the FPL Draft league's own match-level "started",
    which is a different signal). A match only needs that to count here, not
    the league's "finished" -- so the table updates continuously through a
    live gameweek instead of freezing until the whole gameweek settles days
    later. While it's live, that gameweek's contribution uses each squad's
    effective_xi_points (the same autosub-aware projected score used
    everywhere else on the site) rather than FPL's own raw number, which
    lags for the same reason explained in autosub.py.

    Once every real fixture this gameweek has ended (live_fully_over), the
    match keeps counting on the same projected score but is no longer
    flagged "live" -- the results are settled even though the league's own
    "finished" flag can take hours or days longer to flip (it waits on
    bonus/BPS confirmation), and that lag has nothing to do with whether
    the *next* gameweek's waiver or transfer window has opened.

    Pass live_squads (this build's build_squads() output) and live_gw (its
    gameweek) to enable any of this; omit them to only ever count finished
    matches, as before.

    The API's own standings block reports matches_played as 38 for every
    manager before a ball is kicked, so it is not trusted for that column.
    """
    table = {le: {"w": 0, "d": 0, "l": 0, "for": 0, "against": 0, "played": 0}
             for le in managers}
    history = {le: [] for le in managers}
    live_entries = set()

    for m in sorted(details["matches"], key=lambda x: x["event"]):
        if m["event"] > upto_gw:
            continue
        h, a = m["league_entry_1"], m["league_entry_2"]
        if m.get("finished"):
            hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        elif m["event"] == live_gw and live_squads is not None and live_kicked_off:
            hp = live_squads.get(h, {}).get("effective_xi_points", m["league_entry_1_points"])
            ap = live_squads.get(a, {}).get("effective_xi_points", m["league_entry_2_points"])
            if not live_fully_over:
                live_entries.update((h, a))
        else:
            continue
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
            history[me].append({"event": m["event"], "pts": mine, "against": theirs, "opp": opp,
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
            "live": le in live_entries,
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


def build_transfer_history(managers, players, raw_transactions, load_fn, gw):
    """Every raw transfer (in/out) each manager has made this season, one
    entry per gameweek it happened. Unlike transfer_log/best_transfer/
    worst_transfer, this isn't filtered by whether a swap already
    qualifies for scoring (both legs played, fixtures finished) -- it's
    just the record of what moved and when, for browsing in full.
    """
    history = {le: [] for le in managers}
    for g in range(2, gw + 1):
        curr_squads_raw = load_fn(f"squads_gw{g}")
        prev_squads_raw = load_fn(f"squads_gw{g - 1}")
        if not curr_squads_raw or not prev_squads_raw:
            continue
        week = build_transfers(managers, players, raw_transactions, g, prev_squads_raw, curr_squads_raw)
        for le, t in week.items():
            if t["count"]:
                history[le].append({"gameweek": g, "in": t["in"], "out": t["out"]})
    return history


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
        for row in squad.get("effective_xi", squad["xi"]):
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


def build_transfer_swaps(managers, players, totw_squads, prev_squads_raw, totw_live, prev_live):
    """Every qualifying transfer swap for the team-of-week gameweek, each
    with the point swing it produced.

    A swap counts regardless of whether the release was mandated by the
    Highest Scorer Rule or entirely voluntary -- what decides whether it
    reads as a best or worst transfer is purely the points swing, not why
    the swap happened.

    The outgoing player must have started (been in the active XI, not the
    bench) the previous gameweek AND actually taken the field for their
    club that gameweek (minutes > 0) -- that's what makes them a genuine
    release rather than a bench-warmer nobody would miss. Once released,
    though, whether they go on to play for their club this gameweek is
    no longer gatekept: a blank because they picked up an injury, got
    dropped, or simply didn't feature is still a real, final result once
    their club's match is over, and it's exactly the "you dropped him and
    he blanked" (or "he still delivered anyway") story this table exists
    to tell.

    The incoming player must have started this gameweek in the fantasy
    XI (not the bench) -- a fantasy-benched pickup isn't a real swap,
    there's nothing to compare their non-existent contribution against.
    But once they're started, zero real minutes (unused sub, injury,
    suspension) still counts as their result for the week, same as the
    outgoing player: a manager who starts someone who then blanks while
    the player they dropped scores is a genuine, often painful, worst
    transfer.

    And since the score being compared for both is this gameweek's, not
    last week's, a swap only counts once BOTH players' own real-world
    club fixtures this gameweek have actually finished (not just kicked
    off) -- a 0 only reads as final once the match is over; bonus points
    aren't final and a match still in progress can still swing, until the
    final whistle, so a swap assessed mid-match could read as a "best
    transfer" that later isn't.

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
    prev_minutes = {eid: s.get("minutes", 0) for eid, s in live_stats(prev_live).items()}
    finished_clubs = {
        team_id
        for f in ((totw_live or {}).get("fixtures") or []) if _fixture_over(f)
        for team_id in (f.get("team_h"), f.get("team_a"))
    }
    by_entry = {m["entry_id"]: le for le, m in managers.items()}

    def describe(eid):
        meta = players.get(eid, {"name": str(eid), "pos": "", "club": "", "team_id": None})
        return {"element": eid, "name": meta["name"], "pos": meta["pos"],
                "club": meta["club"], "team_id": meta.get("team_id"), "points": pts.get(eid, 0)}

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

        outs = [describe(eid) for eid in prev_ids - curr_ids
                if eid in prev_xi_ids and prev_minutes.get(eid, 0) > 0
                and players.get(eid, {}).get("team_id") in finished_clubs]
        ins = [row for row in squad["xi"]
               if row["element"] not in prev_ids
               and row.get("team_id") in finished_clubs]
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
                "out_team_id": o.get("team_id"),
                "in_name": i["name"], "in_club": i["club"], "in_points": i["points"],
                "in_team_id": i.get("team_id"),
                "diff": i["points"] - o["points"],
            })
    return swaps


def best_and_worst_transfers(swaps, limit=3):
    """Split the swap list into top-N gains and top-N losses."""
    best = sorted((s for s in swaps if s["diff"] > 0), key=lambda s: (-s["diff"], s["out_name"]))[:limit]
    worst = sorted((s for s in swaps if s["diff"] < 0), key=lambda s: (s["diff"], s["out_name"]))[:limit]
    return ([{**s, "gain": s["diff"]} for s in best],
            [{**s, "loss": -s["diff"]} for s in worst])


def manager_week_scores(managers, players, gw_squads_raw, gw_live, prev_squads_raw, prev_live):
    """Each manager's score for one gameweek: active-XI points scored,
    plus the full point swing (see build_transfer_swaps) from any
    qualifying transfer that week -- deliberately double-weighting the
    transfer decision, since the incoming player's points already count
    once toward the raw score and the swing is added again on top as a
    bonus for the call itself. "transfer_points" is that swing on its
    own, so callers can show it as a column in its own right.
    """
    if not gw_squads_raw or not gw_live:
        return {}
    squads = build_squads(managers, gw_squads_raw, gw_live, players)
    swaps = (build_transfer_swaps(managers, players, squads, prev_squads_raw, gw_live, prev_live)
             if prev_squads_raw else [])
    swing = {}
    for s in swaps:
        swing[s["manager"]] = swing.get(s["manager"], 0) + s["diff"]
    return {
        managers[le]["manager"]: {
            "points": squad["effective_xi_points"] + swing.get(managers[le]["manager"], 0),
            "transfer_points": swing.get(managers[le]["manager"], 0),
        }
        for le, squad in squads.items()
    }


def build_manager_of_week(managers, players, gw, squads_raw, live, prev_squads_raw, prev_live):
    """Top 5 and worst 5 managers for gw -- live, updating as it plays out
    (unlike Team of the Week, which waits for gw to fully settle)."""
    scores = manager_week_scores(managers, players, squads_raw, live, prev_squads_raw, prev_live)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["points"])
    worst = ranked[-5:][::-1] if len(ranked) >= 5 else []
    return {
        "gameweek": gw,
        "top": [{"manager": n, "points": s["points"], "transfer_points": s["transfer_points"]} for n, s in ranked[:5]],
        "worst": [{"manager": n, "points": s["points"], "transfer_points": s["transfer_points"]} for n, s in worst],
    }


def _block_standings(managers, players, load_fn, block_start, block_end):
    """Summed manager-week scores (and their transfer-points component)
    across gameweeks block_start..block_end. A gameweek with no
    squads/live data on disk yet (not played, or not fetched) simply
    contributes nothing -- callers don't need to worry about how far the
    block has actually progressed.
    """
    totals = {}
    for g in range(block_start, block_end + 1):
        g_squads_raw = load_fn(f"squads_gw{g}")
        g_live = load_fn(f"live_gw{g}")
        if not g_squads_raw or not g_live:
            continue
        g_prev_squads_raw = load_fn(f"squads_gw{g - 1}") if g > 1 else None
        g_prev_live = load_fn(f"live_gw{g - 1}") if g > 1 else None
        for manager_name, s in manager_week_scores(
                managers, players, g_squads_raw, g_live, g_prev_squads_raw, g_prev_live).items():
            acc = totals.setdefault(manager_name, {"points": 0, "transfer_points": 0})
            acc["points"] += s["points"]
            acc["transfer_points"] += s["transfer_points"]
    if not totals:
        return []
    ranked = sorted(totals.items(), key=lambda kv: -kv[1]["points"])
    return [{"manager": n, "points": t["points"], "transfer_points": t["transfer_points"]} for n, t in ranked]


def build_manager_of_month(managers, players, gw, gw_fully_over, load_fn):
    """Manager of the Month: a rolling 4-gameweek competition (GW1-4,
    GW5-8, ...), by the same per-week score as Manager of the Week,
    summed across the block. The block containing gw -- the site's
    actual current gameweek, unlike Team of the Week/Manager of the
    Week/transfers which deliberately lag until a gameweek fully ends --
    is "current" and its standings are shown live, updating gameweek by
    gameweek, and within gw itself kick by kick, as they accumulate
    (a gameweek with no squads/live data on disk yet just contributes
    nothing, so this is safe to call the moment gw's fixtures kick off).
    It's only finalised, and its winner crowned, once gw reaches the
    block's last gameweek (a multiple of 4) AND that gameweek's own
    real-world fixtures have all finished, at which point the next
    block starts fresh from zero.

    Also returns "history": every earlier block that's already finished,
    most recent first, plus a "leaderboard" tally of how many months
    each manager has won -- the record book for a separate page, since
    the live standings above are the only thing that needs to be
    front-and-centre week to week. "season" is the same running tally
    but for the whole season so far (GW1 through gw), live in exactly
    the same way as "current".
    """
    current_end = ((gw + 3) // 4) * 4
    current_start = current_end - 3
    current_standings = _block_standings(managers, players, load_fn, current_start, gw)
    current = None
    if current_standings:
        current = {
            "block_start": current_start,
            "block_end": current_end,
            "is_final": gw == current_end and gw_fully_over,
            "manager": current_standings[0]["manager"],
            "points": current_standings[0]["points"],
            "standings": current_standings,
        }

    season_standings = _block_standings(managers, players, load_fn, 1, gw)

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
    worst_wins = {}
    for h in history:
        wins[h["manager"]] = wins.get(h["manager"], 0) + 1
        worst_manager = h["standings"][-1]["manager"]
        worst_wins[worst_manager] = worst_wins.get(worst_manager, 0) + 1
    leaderboard = sorted(wins.items(), key=lambda kv: (-kv[1], kv[0]))
    worst_leaderboard = sorted(worst_wins.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "current": current,
        "season": season_standings,
        "history": history,
        "leaderboard": [{"manager": n, "wins": w} for n, w in leaderboard],
        "worst_leaderboard": [{"manager": n, "wins": w} for n, w in worst_leaderboard],
    }


def longest_streak(fixtures_asc, result):
    """Longest run of consecutive fixtures (in chronological order) with
    the given result ("W" or "L")."""
    best = cur = 0
    for f in fixtures_asc:
        cur = cur + 1 if f["result"] == result else 0
        best = max(best, cur)
    return best


def group_transfers_by_block(swaps):
    """Group a manager's tagged transfer swaps into the same 4-gameweek
    blocks Manager of the Month uses (GW1-4, GW5-8, ...), most recent
    block first and each block's swaps in gameweek order -- so a
    "Transfer pts" figure for a given block can link straight to just
    the swaps that made it up, instead of the manager's whole history.
    """
    blocks = {}
    for s in swaps:
        block_end = ((s["gameweek"] + 3) // 4) * 4
        block_start = block_end - 3
        blocks.setdefault((block_start, block_end), []).append(s)
    return [
        {
            "block_start": block_start,
            "block_end": block_end,
            "swaps": sorted(block_swaps, key=lambda s: s["gameweek"]),
        }
        for (block_start, block_end), block_swaps in sorted(blocks.items(), reverse=True)
    ]


def build_manager_profiles(details, managers, players, gw, totw_gw, load_fn, manager_of_month_history, squads, standings):
    """Everything a manager's own page needs: their fixture history and
    head-to-head record (from the full season schedule, so this only gets
    more interesting as more rounds are played), their biggest single-match
    win, the best individual player performance their active XI has ever
    produced, their personal best and worst transfer swaps, and their
    trophy count -- Manager of the Week wins, Manager of the Month wins,
    and Team of the Week appearances.

    Fixture history and head-to-head come from standings' own per-match
    history, which is already live -- it uses each squad's projected
    effective_xi_points for the current gameweek once it's kicked off,
    rather than waiting on the FPL Draft league's own "finished" flag
    (which lags for days after a gameweek actually concludes). So Biggest
    win, Highest/Lowest score, and the win/loss streaks below all update
    through a live gameweek exactly like Standings and Team of the Week do,
    instead of freezing until results are officially confirmed.

    Best player performance, transfer swaps, weekly wins and Team of the
    Week appearances are all found by re-running the same per-gameweek
    computations (build_squads, build_transfer_swaps, build_team_of_week,
    manager_week_scores) used elsewhere in this file across every settled
    gameweek 1..totw_gw, rather than reusing a single week's result --
    there's no shortcut, a manager's all-time best is only knowable by
    having looked at all of it.
    """
    history_by_le = {r["league_entry"]: r["history"] for r in standings}
    fixtures = {le: [] for le in managers}
    h2h = {le: {} for le in managers}
    for le in managers:
        for h in history_by_le.get(le, []):
            opp = h["opp"]
            if opp not in managers:
                continue
            fixtures[le].append({
                "gameweek": h["event"], "opponent": managers[opp]["manager"],
                "points": h["pts"], "against": h["against"], "margin": h["pts"] - h["against"],
                "result": h["result"],
            })
            rec = h2h[le].setdefault(opp, {"w": 0, "d": 0, "l": 0})
            rec[h["result"].lower()] += 1

    current_gw_live = load_fn(f"live_gw{gw}")
    current_kicked_off, current_fully_over = gw_window(current_gw_live)

    current_fixture = {le: None for le in managers}
    future_fixtures = {le: [] for le in managers}
    for m in details["matches"]:
        h, a = m["league_entry_1"], m["league_entry_2"]
        hp, ap = m["league_entry_1_points"], m["league_entry_2_points"]
        for me, opp, mine, theirs in ((h, a, hp, ap), (a, h, ap, hp)):
            if me not in managers or opp not in managers:
                continue
            if m["event"] == gw:
                if not m.get("finished"):
                    mine = squads.get(me, {}).get("effective_xi_points", mine)
                    theirs = squads.get(opp, {}).get("effective_xi_points", theirs)
                current_fixture[me] = {
                    "gameweek": gw, "opponent": managers[opp]["manager"],
                    "points": mine, "against": theirs,
                    "started": current_kicked_off,
                    "live": current_kicked_off and not current_fully_over,
                }
            elif m["event"] > gw:
                future_fixtures[me].append({"gameweek": m["event"], "opponent": managers[opp]["manager"]})
    for le in future_fixtures:
        future_fixtures[le].sort(key=lambda f: f["gameweek"])

    best_player = {le: None for le in managers}
    player_perf_log = {le: [] for le in managers}
    best_transfer = {le: None for le in managers}
    worst_transfer = {le: None for le in managers}
    transfer_log = {le: [] for le in managers}
    motw_wins = {le: 0 for le in managers}
    worst_motw_wins = {le: 0 for le in managers}
    totw_appearances = {le: 0 for le in managers}
    by_manager = {m["manager"]: le for le, m in managers.items()}

    for g in range(1, totw_gw + 1):
        g_squads_raw = load_fn(f"squads_gw{g}")
        g_live = load_fn(f"live_gw{g}")
        if not g_squads_raw or not g_live:
            continue
        squads = build_squads(managers, g_squads_raw, g_live, players)
        for le, squad in squads.items():
            for row in squad.get("effective_xi", squad["xi"]):
                player_perf_log[le].append({"gameweek": g, "name": row["name"],
                                             "club": row["club"], "points": row["points"]})
                cur = best_player[le]
                if cur is None or row["points"] > cur["points"]:
                    best_player[le] = {"gameweek": g, "name": row["name"],
                                        "club": row["club"], "points": row["points"]}

        week_totw = build_team_of_week(managers, squads)
        for p in (week_totw or {}).get("players", []):
            le = by_manager.get(p["manager"])
            if le is not None:
                totw_appearances[le] += 1

        g_prev_squads_raw = load_fn(f"squads_gw{g - 1}") if g > 1 else None
        g_prev_live = load_fn(f"live_gw{g - 1}") if g > 1 else None
        week_scores = manager_week_scores(managers, players, g_squads_raw, g_live, g_prev_squads_raw, g_prev_live)
        if week_scores:
            winner_name = max(week_scores.items(), key=lambda kv: kv[1]["points"])[0]
            le = by_manager.get(winner_name)
            if le is not None:
                motw_wins[le] += 1
            loser_name = min(week_scores.items(), key=lambda kv: kv[1]["points"])[0]
            le = by_manager.get(loser_name)
            if le is not None:
                worst_motw_wins[le] += 1

        if g == 1:
            continue
        if not g_prev_squads_raw:
            continue
        swaps = build_transfer_swaps(managers, players, squads, g_prev_squads_raw, g_live, g_prev_live)
        for s in swaps:
            le = by_manager.get(s["manager"])
            if le is None:
                continue
            tagged = {**s, "gameweek": g}
            transfer_log[le].append(tagged)
            if s["diff"] > 0 and (best_transfer[le] is None or s["diff"] > best_transfer[le]["diff"]):
                best_transfer[le] = tagged
            if s["diff"] < 0 and (worst_transfer[le] is None or s["diff"] < worst_transfer[le]["diff"]):
                worst_transfer[le] = tagged

    # Best individual performance, and best/worst transfer, are running
    # records, not a once-a-week competition like Team of the Week or Manager
    # of the Week (which genuinely need every manager to have played before
    # crowning a winner) -- so unlike the totw_gw loop above, these also scan
    # the current, still-live gameweek, counting a player/swap the moment the
    # real-world fixture(s) involved are finished rather than waiting for
    # every other fixture in the gameweek to catch up too. motw_wins stays
    # non-live above.
    live_squads_raw = load_fn(f"squads_gw{gw}")
    live_gw_data = load_fn(f"live_gw{gw}")
    if live_squads_raw and live_gw_data:
        finished_clubs = {
            team_id
            for f in (live_gw_data.get("fixtures") or []) if _fixture_over(f)
            for team_id in (f.get("team_h"), f.get("team_a"))
        }
        live_squads = build_squads(managers, live_squads_raw, live_gw_data, players)
        for le, squad in live_squads.items():
            for row in squad.get("effective_xi", squad["xi"]):
                if row.get("team_id") not in finished_clubs:
                    continue
                if gw > totw_gw:
                    player_perf_log[le].append({"gameweek": gw, "name": row["name"],
                                                 "club": row["club"], "points": row["points"]})
                cur = best_player[le]
                if cur is None or row["points"] > cur["points"]:
                    best_player[le] = {"gameweek": gw, "name": row["name"],
                                        "club": row["club"], "points": row["points"]}

        # transfer_log also needs the live current gameweek once it's ahead of
        # totw_gw, so its transfer-history breakdown matches the live
        # "Transfer pts" figures shown elsewhere (Manager of the Week/Month/
        # Season, all of which track gw directly). build_transfer_swaps
        # itself already withholds a swap here until both players' club
        # fixtures have finished, so nothing further to gate on below.
        if gw > totw_gw:
            g_prev_squads_raw = load_fn(f"squads_gw{gw - 1}")
            g_prev_live = load_fn(f"live_gw{gw - 1}")
            if g_prev_squads_raw:
                swaps = build_transfer_swaps(managers, players, live_squads, g_prev_squads_raw, live_gw_data, g_prev_live)
                for s in swaps:
                    le = by_manager.get(s["manager"])
                    if le is None:
                        continue
                    tagged = {**s, "gameweek": gw}
                    transfer_log[le].append(tagged)
                    if s["diff"] > 0 and (best_transfer[le] is None or s["diff"] > best_transfer[le]["diff"]):
                        best_transfer[le] = tagged
                    if s["diff"] < 0 and (worst_transfer[le] is None or s["diff"] < worst_transfer[le]["diff"]):
                        worst_transfer[le] = tagged

    mom_wins = {le: 0 for le in managers}
    worst_mom_wins = {le: 0 for le in managers}
    for block in manager_of_month_history:
        le = by_manager.get(block["manager"])
        if le is not None:
            mom_wins[le] += 1
        le = by_manager.get(block["standings"][-1]["manager"])
        if le is not None:
            worst_mom_wins[le] += 1

    profiles = {}
    for le, m in managers.items():
        record = h2h[le]
        most_beaten = max(record.items(), key=lambda kv: kv[1]["w"], default=(None, None))
        most_lost_to = max(record.items(), key=lambda kv: kv[1]["l"], default=(None, None))
        wins = [f for f in fixtures[le] if f["result"] == "W"]
        losses = [f for f in fixtures[le] if f["result"] == "L"]
        biggest_win = max(wins, key=lambda f: f["margin"], default=None)
        biggest_loss = min(losses, key=lambda f: f["margin"], default=None)
        highest_scores = sorted(fixtures[le], key=lambda f: -f["points"])[:5]
        lowest_scores = sorted(fixtures[le], key=lambda f: f["points"])[:5]
        longest_win_streak = longest_streak(fixtures[le], "W")
        longest_loss_streak = longest_streak(fixtures[le], "L")
        qualifying_transfers = [t for t in transfer_log[le] if t["diff"] != 0]
        best_transfers = sorted((t for t in qualifying_transfers if t["diff"] > 0), key=lambda t: -t["diff"])[:5]
        worst_transfers = sorted((t for t in qualifying_transfers if t["diff"] < 0), key=lambda t: t["diff"])[:5]
        profiles[m["manager"]] = {
            "fixtures": sorted(fixtures[le], key=lambda f: -f["gameweek"]),
            "current_fixture": current_fixture[le],
            "future_fixtures": future_fixtures[le],
            "biggest_win": biggest_win,
            "biggest_loss": biggest_loss,
            "highest_scores": highest_scores,
            "lowest_scores": lowest_scores,
            "longest_win_streak": longest_win_streak,
            "longest_loss_streak": longest_loss_streak,
            "best_player": best_player[le],
            "player_performances": player_perf_log[le],
            "most_beaten": {"manager": managers[most_beaten[0]]["manager"], "wins": most_beaten[1]["w"]}
                if most_beaten[0] is not None and most_beaten[1]["w"] > 0 else None,
            "most_lost_to": {"manager": managers[most_lost_to[0]]["manager"], "losses": most_lost_to[1]["l"]}
                if most_lost_to[0] is not None and most_lost_to[1]["l"] > 0 else None,
            "best_transfer": best_transfer[le],
            "worst_transfer": worst_transfer[le],
            "best_transfers": best_transfers,
            "worst_transfers": worst_transfers,
            "qualifying_transfers": qualifying_transfers,
            "transfer_blocks": group_transfers_by_block(transfer_log[le]),
            "motw_wins": motw_wins[le],
            "worst_motw_wins": worst_motw_wins[le],
            "mom_wins": mom_wins[le],
            "worst_mom_wins": worst_mom_wins[le],
            "totw_appearances": totw_appearances[le],
        }
    return profiles


def build_luckiest(standings, limit=5):
    """Every manager ranked by strength of schedule: the average score
    their opponents have put up against them across the season so far --
    not the manager's own score. Sourced from standings' own per-match
    history, which already carries the live gameweek's projected score
    once it's kicked off (the same live_kicked_off/live_squads plumbing
    standings itself uses), so this updates continuously through a live
    gameweek instead of waiting for the FPL Draft league's own delayed
    "finished" flag. "luckiest" is the top-N who have faced the softest
    average opposition (ascending, easiest first); "unluckiest" is the
    top-N who have faced the toughest (descending, hardest first) -- the
    same underlying ranking read from both ends, regardless of each
    manager's own results in those games (a manager can land on either
    list on the back of narrow losses or big wins alike; what places
    them here is the opponent's output, not the scoreline).

    Also returns "average": the league-wide average score across every
    match counted above, so each manager's avg_against can be read
    against a baseline -- e.g. a 48.0 next to a league average of 40
    means a genuinely tough run, not just a high-scoring league.
    """
    rows = []
    all_points = []
    for r in standings:
        history = r.get("history") or []
        if not history:
            continue
        avg_against = sum(f["against"] for f in history) / len(history)
        rows.append({"manager": r["manager"], "avg_against": round(avg_against, 1)})
        all_points.extend(f["pts"] for f in history)
    rows.sort(key=lambda r: r["avg_against"])
    average = round(sum(all_points) / len(all_points), 1) if all_points else 0
    return {
        "luckiest": rows[:limit],
        "unluckiest": list(reversed(rows[-limit:])) if rows else [],
        "average": average,
    }


def build_leaders(manager_profiles, limit=5):
    """Reduces manager_profiles down to a top-N table for every category
    also shown on an individual manager's own profile page -- one row per
    manager's own best/worst instance of that category, ranked against
    each other. Head-to-head-only categories (most beaten, lost to most)
    don't have a single season leaderboard in the same sense, so aren't
    included here.
    """
    def top_n(key, reverse=True, limit=limit):
        pool = [(name, p[key]) for name, p in manager_profiles.items() if p.get(key) is not None]
        pool.sort(key=lambda kv: kv[1], reverse=reverse)
        return [{"manager": name, "value": value} for name, value in pool[:limit]]

    def top_n_by(key, subkey, reverse=True, limit=limit):
        pool = [(name, p[key]) for name, p in manager_profiles.items() if p.get(key)]
        pool.sort(key=lambda kv: kv[1][subkey], reverse=reverse)
        return [{"manager": name, **entry} for name, entry in pool[:limit]]

    def top_n_games(reverse=True, limit=limit):
        """Every individual gameweek score across every manager, not just
        each manager's own best/worst -- so a manager with several huge
        (or dismal) weeks can take multiple spots on the leaderboard,
        rather than being capped at one appearance."""
        pool = [{"manager": name, **f} for name, p in manager_profiles.items() for f in p.get("fixtures", [])]
        pool.sort(key=lambda f: f["points"], reverse=reverse)
        return pool[:limit]

    def top_n_performances(limit=limit):
        """Every individual player performance across every manager's squad
        all season, not just each manager's own best -- so a manager who
        has fielded several huge scorers can take multiple spots."""
        pool = [{"manager": name, **perf} for name, p in manager_profiles.items() for perf in p.get("player_performances", [])]
        pool.sort(key=lambda perf: perf["points"], reverse=True)
        return pool[:limit]

    def top_n_transfers(positive, limit=limit):
        """Every qualifying transfer swap made all season, pooled across
        every manager -- not just each manager's own single best/worst --
        so a manager with several great (or awful) swaps can take multiple
        spots on the leaderboard."""
        pool = [
            {"manager": name, **t}
            for name, p in manager_profiles.items()
            for t in p.get("qualifying_transfers", [])
            if (t["diff"] > 0) == positive
        ]
        pool.sort(key=lambda t: t["diff"], reverse=positive)
        return pool[:limit]

    return {
        "motw_wins": top_n("motw_wins"),
        "worst_motw_wins": top_n("worst_motw_wins"),
        "mom_wins": top_n("mom_wins"),
        "worst_mom_wins": top_n("worst_mom_wins"),
        "totw_appearances": top_n("totw_appearances", limit=10),
        "longest_win_streak": top_n("longest_win_streak"),
        "longest_loss_streak": top_n("longest_loss_streak"),
        "biggest_win": top_n_by("biggest_win", "margin"),
        "highest_score": top_n_games(limit=10),
        "lowest_score": top_n_games(reverse=False, limit=10),
        "best_player": top_n_performances(limit=10),
        "best_transfers": top_n_transfers(True, limit=10),
        "worst_transfers": top_n_transfers(False, limit=10),
    }


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
    gw_kicked_off, gw_fully_over = gw_window(live)
    squads_raw = load(f"squads_gw{gw}")
    prev_squads_raw = load(f"squads_gw{gw - 1}")
    prev_live = load(f"live_gw{gw - 1}")
    raw_transactions = load("transactions")

    # Team of the week always shows the last gameweek whose squads and
    # scores are fully settled -- that's gw itself once all of its
    # real-world fixtures have finished (gw_fully_over, the same
    # real-world fixture calendar standings uses), not the moment they
    # kick off, and not the FPL Draft league's own match-level "finished"
    # flag either, which lags real match completion by hours or days
    # (bonus/BPS confirmation). Team of the Week is deliberately NOT live
    # during the gameweek. Manager of the Week, best/worst transfers and
    # Manager of the Month all track gw directly instead (further down),
    # updating live as the gameweek plays out. Before gw is fully over,
    # fall back to gw-1, floored at 1 since there's no gameweek 0.
    totw_gw = gw if gw_fully_over else max(1, gw - 1)
    totw_squads_raw = load(f"squads_gw{totw_gw}")
    totw_live = load(f"live_gw{totw_gw}")
    prev_totw_squads_raw = load(f"squads_gw{totw_gw - 1}")
    prev_totw_live = load(f"live_gw{totw_gw - 1}")

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
    results = build_results(details, gw, managers, squads, live)
    schedule = build_schedule(bootstrap, live, gw)
    releases = build_releases(managers, squads, results_by_entry)
    source = "computed"
    if not releases:
        releases = manual_releases(gw, managers, results_by_entry, players)
        source = "manual" if releases else "none"

    prev_path = DERIVED / f"gw{gw - 1}_releases.json"
    prev_releases = json.loads(prev_path.read_text()) if prev_path.exists() else None

    totw_squads = build_squads(managers, totw_squads_raw, totw_live, players) if totw_squads_raw and totw_live else {}
    team_of_week = build_team_of_week(managers, totw_squads)

    # Unlike Team of the Week above, best/worst transfers and Manager of
    # the Week track gw directly and live -- using the same live/projected
    # scores standings already shows -- rather than waiting for the
    # gameweek to fully settle.
    transfer_swaps = build_transfer_swaps(managers, players, squads, prev_squads_raw, live, prev_live)
    best_transfers, worst_transfers = best_and_worst_transfers(transfer_swaps, limit=5)

    manager_of_week = build_manager_of_week(
        managers, players, gw, squads_raw, live, prev_squads_raw, prev_live)
    manager_of_month = build_manager_of_month(managers, players, gw, gw_fully_over, load)
    standings = build_standings(details, managers, gw, live_gw=gw, live_squads=squads,
                                 live_kicked_off=gw_kicked_off, live_fully_over=gw_fully_over)
    luck = build_luckiest(standings)
    manager_profiles = build_manager_profiles(
        details, managers, players, gw, totw_gw, load, manager_of_month["history"], squads, standings)
    transfer_history = build_transfer_history(managers, players, raw_transactions, load, gw)
    for le, m in managers.items():
        profile = manager_profiles.get(m["manager"])
        if profile is not None:
            profile["raw_transfers"] = sorted(transfer_history[le], key=lambda t: -t["gameweek"])
    leaders = build_leaders(manager_profiles)

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
            "gw_fully_over": gw_fully_over,
            "fetched_at": meta.get("fetched_at"),
            "transaction_mode": details["league"].get("transaction_mode"),
            "scoring": details["league"].get("scoring"),
        },
        "managers": {str(k): v for k, v in managers.items()},
        "matches": matches,
        "results": results,
        "schedule": schedule,
        "squads": {str(k): v for k, v in squads.items()},
        "releases": releases,
        "release_efficiency": sorted(releases, key=lambda r: r["cost_pct"]),
        "breaches": detect_breaches(prev_releases, squads),
        "standings": standings,
        "next_fixtures": build_next_fixtures(details, managers, gw),
        "transfers": {str(k): v for k, v in build_transfers(
            managers, players, raw_transactions, gw, prev_squads_raw, squads_raw).items()},
        "team_of_week": {
            "gameweek": totw_gw,
            "players": (team_of_week or {}).get("players", []),
            "formation": (team_of_week or {}).get("formation"),
            "total_points": (team_of_week or {}).get("total_points", 0),
        },
        "transfers_of_week": {
            "gameweek": gw,
            "best_transfers": best_transfers,
            "worst_transfers": worst_transfers,
        },
        "manager_of_week": manager_of_week,
        "manager_of_month": manager_of_month,
        "manager_profiles": manager_profiles,
        "leaders": leaders,
        "luck": luck,
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
