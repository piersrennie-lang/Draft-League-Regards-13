"""Live-gameweek autosubstitution engine.

FPL Draft does not process automatic substitutions until a gameweek is
completely over, so a squad's "picks" endpoint mid-gameweek is always the
manager's original locked selection -- a starter who blanked (or hasn't
played yet) is never swapped out for a bench player until the whole
gameweek settles. For live standings, releases, Team of the Week and
manager profiles to mean anything *during* a gameweek, this module
projects what those automatic substitutions will eventually be, from the
same live data FPL itself will use once it catches up.

This is a pure function deliberately kept out of kpis.py's data-shuffling
code: every caller that needs "how many points has this squad actually
scored so far" should go through calculate_effective_lineup() so the
answer can never disagree between the releases page, Team of the Week,
manager profiles, or anywhere else that touches a live score.
"""

# Real-world FPL classic-squad formation bounds. The brief only states the
# minimums (>=3 DEF, >=2 MID, >=1 FWD) but its own worked examples only
# make sense with the standard maximums too -- swapping a MID for a FWD
# to produce 3 DEF / 3 MID / 4 FWD is called illegal there, which is only
# true because 4 FWD exceeds the classic-squad cap of 3. Minimums alone
# would have allowed it.
FORMATION_BOUNDS = {
    "DEF": (3, 5),
    "MID": (2, 5),
    "FWD": (1, 3),
}

PLAYED = "PLAYED"
DEFINITELY_DID_NOT_PLAY = "DEFINITELY_DID_NOT_PLAY"
NOT_PLAYED_YET = "NOT_PLAYED_YET"


def _fixture_over(f):
    """A fixture counts as over once full time is reached, not once FPL
    has locked in bonus points. "finished" only flips after bonus/BPS is
    confirmed, which in practice can lag "finished_provisional" (set at
    the final whistle) by a long time -- observed here sitting at
    finished=false / finished_provisional=true for days after kickoff.
    Waiting on the stricter flag means an autosub that should have
    triggered hours ago never does, so either flag is enough."""
    return bool(f.get("finished") or f.get("finished_provisional"))


def classify_status(team_id, minutes, fixtures):
    """One of PLAYED / DEFINITELY_DID_NOT_PLAY / NOT_PLAYED_YET for a
    player, given their club's id, their minutes played so far this
    gameweek, and the gameweek's real-world fixture list.

    A double-gameweek player only becomes DEFINITELY_DID_NOT_PLAY once
    *every* fixture their club has this gameweek is finished with them
    still on 0 minutes -- one blank fixture with another still to come
    must not trigger an autosub early. A club with no fixture at all this
    gameweek (a blank) is treated the same as a fully-finished one, since
    there is nothing left to wait for.
    """
    if minutes and minutes > 0:
        return PLAYED
    team_fixtures = [f for f in fixtures if f.get("team_h") == team_id or f.get("team_a") == team_id]
    if not team_fixtures or all(_fixture_over(f) for f in team_fixtures):
        return DEFINITELY_DID_NOT_PLAY
    return NOT_PLAYED_YET


def _is_legal(counts):
    if counts.get("GKP", 0) != 1:
        return False
    if sum(counts.values()) != 11:
        return False
    for pos, (lo, hi) in FORMATION_BOUNDS.items():
        if not (lo <= counts.get(pos, 0) <= hi):
            return False
    return True


def calculate_effective_lineup(starters, bench, player_gameweek_stats, fixtures):
    """Project the effective (post-autosub) XI for one manager's squad
    this gameweek.

    starters: the 11 locked-in starters, each a dict with at least
        "element" (id), "pos" ("GKP"/"DEF"/"MID"/"FWD"), "team_id".
    bench: the (up to 4) bench players in the manager's own priority
        order -- list order is the priority, so callers must already
        have this sorted by the squad's own bench-position field.
    player_gameweek_stats: {element_id: {"minutes": int, "points": int}}
        for every player that might appear here, this gameweek.
    fixtures: this gameweek's real-world fixture list, each a dict with
        "team_h", "team_a", "finished" (booleans/ids as used elsewhere
        in this codebase -- see live_gw{n}.json's own "fixtures" key).

    Returns {effective_players, autosubs, points, unresolved_players,
    is_final} -- see module docstring. Never mutates its inputs.
    """
    def stats_for(eid):
        return player_gameweek_stats.get(eid, {})

    def status_of(p):
        return classify_status(p["team_id"], stats_for(p["element"]).get("minutes", 0), fixtures)

    def points_of(eid):
        return stats_for(eid).get("points", 0)

    starter_status = {p["element"]: status_of(p) for p in starters}
    bench_status = {p["element"]: status_of(p) for p in bench}

    starting_gk = next(p for p in starters if p["pos"] == "GKP")
    bench_gk = next((p for p in bench if p["pos"] == "GKP"), None)
    outfield_starters = [p for p in starters if p["pos"] != "GKP"]
    outfield_bench = [p for p in bench if p["pos"] != "GKP"]

    effective = {p["element"]: p for p in starters}
    autosubs = []

    # 1. Goalkeeper substitution -- always independent of outfield subs,
    # and only ever GK-for-GK.
    if starter_status[starting_gk["element"]] == DEFINITELY_DID_NOT_PLAY:
        if bench_gk is not None and bench_status.get(bench_gk["element"]) == PLAYED:
            del effective[starting_gk["element"]]
            effective[bench_gk["element"]] = bench_gk
            autosubs.append({
                "player_out": starting_gk["element"], "player_in": bench_gk["element"],
                "reason": "starter_did_not_play",
            })

    # 2. Outfield substitutions -- bench priority order, each candidate
    # only enters if doing so leaves a legal formation. "missing" is a
    # pool of not-yet-replaced blanked starters, not a fixed per-slot
    # assignment: a bench player can fill *any* of them, whichever keeps
    # the resulting formation legal (see FORMATION_BOUNDS above).
    current_counts = {"GKP": 1}
    for p in outfield_starters:
        current_counts[p["pos"]] = current_counts.get(p["pos"], 0) + 1
    missing = [p for p in outfield_starters if starter_status[p["element"]] == DEFINITELY_DID_NOT_PLAY]

    for bp in outfield_bench:
        if bench_status.get(bp["element"]) != PLAYED:
            continue
        if not missing:
            break
        chosen = None
        for i, gap in enumerate(missing):
            trial = dict(current_counts)
            trial[gap["pos"]] -= 1
            trial[bp["pos"]] = trial.get(bp["pos"], 0) + 1
            if _is_legal(trial):
                chosen = (i, trial)
                break
        if chosen is None:
            continue
        i, trial = chosen
        gap = missing.pop(i)
        current_counts = trial
        del effective[gap["element"]]
        effective[bp["element"]] = bp
        autosubs.append({
            "player_out": gap["element"], "player_in": bp["element"],
            "reason": "starter_did_not_play",
        })

    unresolved_players = [p["element"] for p in missing]
    if (starter_status[starting_gk["element"]] == DEFINITELY_DID_NOT_PLAY
            and starting_gk["element"] in effective):
        unresolved_players.append(starting_gk["element"])

    points = sum(points_of(eid) for eid in effective)
    is_final = all(status_of(p) != NOT_PLAYED_YET for p in list(starters) + list(bench))

    return {
        "effective_players": list(effective.keys()),
        "autosubs": autosubs,
        "points": points,
        "unresolved_players": unresolved_players,
        "is_final": is_final,
    }
