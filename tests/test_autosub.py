"""Unit tests for autosub.calculate_effective_lineup().

Player ids below are arbitrary small ints, unique within a test only.
Team ids double as a shorthand for "which real-world fixture this player
belongs to" -- team 1 vs team 2 is one fixture, team 3 vs team 4 another,
and so on, so a test can put any subset of players on a "finished" or
"still to play" footing independently of the others.
"""

from autosub import calculate_effective_lineup


def player(eid, pos, team_id):
    return {"element": eid, "pos": pos, "team_id": team_id}


def fixture(team_h, team_a, finished, finished_provisional=None):
    return {
        "team_h": team_h, "team_a": team_a, "finished": finished,
        "finished_provisional": finished if finished_provisional is None else finished_provisional,
    }


def stats(minutes, points):
    return {"minutes": minutes, "points": points}


# A standard, always-legal 11: 1 GKP + 3 DEF + 4 MID + 3 FWD, unless a
# test overrides the shape to exercise a different formation.
def base_starters(team_id=1):
    return [
        player(1, "GKP", team_id),
        player(2, "DEF", team_id), player(3, "DEF", team_id), player(4, "DEF", team_id),
        player(5, "MID", team_id), player(6, "MID", team_id), player(7, "MID", team_id), player(8, "MID", team_id),
        player(9, "FWD", team_id), player(10, "FWD", team_id), player(11, "FWD", team_id),
    ]


def test_1_no_substitutions():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "DEF", 1), player(14, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert set(result["effective_players"]) == {p["element"] for p in starters}
    assert result["autosubs"] == []
    assert result["points"] == 11 * 4
    assert result["unresolved_players"] == []
    assert result["is_final"] is True


def test_2_one_mid_missing_first_bench_mid_plays():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)  # MID #5 blanked
    live_stats[14] = stats(90, 6)  # bench MID played
    for p in bench:
        live_stats.setdefault(p["element"], stats(0, 0))
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 14, "reason": "starter_did_not_play"} in result["autosubs"]
    assert len(result["autosubs"]) == 1
    assert 5 not in result["effective_players"]
    assert 14 in result["effective_players"]
    assert result["unresolved_players"] == []


def test_3_one_mid_missing_first_bench_forward_legally_replaces():
    # 3 DEF / 5 MID / 2 FWD -- removing one MID and adding a FWD gives
    # 3 DEF / 4 MID / 3 FWD, which is legal (FWD hits its cap of 3, not
    # over it).
    starters = [
        player(1, "GKP", 1),
        player(2, "DEF", 1), player(3, "DEF", 1), player(4, "DEF", 1),
        player(5, "MID", 1), player(6, "MID", 1), player(7, "MID", 1), player(8, "MID", 1), player(9, "MID", 1),
        player(10, "FWD", 1), player(11, "FWD", 1),
    ]
    bench = [player(12, "GKP", 1), player(13, "FWD", 1), player(14, "DEF", 1), player(15, "MID", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    for p in bench:
        live_stats[p["element"]] = stats(0, 0)
    live_stats[13] = stats(90, 8)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 13, "reason": "starter_did_not_play"} in result["autosubs"]
    assert len(result["autosubs"]) == 1


def test_4_def_missing_skip_mid_use_def():
    # 3 DEF / 4 MID / 3 FWD; one DEF blanks. Bench 1 is a MID -- swapping
    # DEF for MID would drop DEF to 2, below the 3-DEF minimum, so it
    # must be skipped in favour of bench 2's DEF.
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[2] = stats(0, 0)  # DEF blanks
    for p in bench:
        live_stats[p["element"]] = stats(90, 5)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 2, "player_in": 13, "reason": "starter_did_not_play"} in result["autosubs"]
    assert len(result["autosubs"]) == 1
    assert 14 not in result["effective_players"]  # bench MID was skipped, stays on the bench


def test_5_starting_goalkeeper_missing():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "DEF", 1), player(14, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[1] = stats(0, 0)
    for p in bench:
        live_stats[p["element"]] = stats(0, 0)
    live_stats[12] = stats(90, 6)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 1, "player_in": 12, "reason": "starter_did_not_play"} in result["autosubs"]
    assert 1 not in result["effective_players"]
    assert 12 in result["effective_players"]
    assert result["unresolved_players"] == []


def test_6_starting_gk_missing_bench_gk_also_did_not_play():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "DEF", 1), player(14, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[1] = stats(0, 0)
    live_stats[12] = stats(0, 0)  # bench GK blanked too
    for p in bench[1:]:
        live_stats[p["element"]] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert result["autosubs"] == []
    assert 1 in result["effective_players"]
    assert 12 not in result["effective_players"]
    assert 1 in result["unresolved_players"]
    assert result["points"] == 10 * 4  # every outfield starter, GK contributes 0


def test_7_multiple_starters_missing():
    # 3 DEF / 5 MID / 2 FWD, two MIDs blank. Bench order FWD, DEF, MID.
    starters = [
        player(1, "GKP", 1),
        player(2, "DEF", 1), player(3, "DEF", 1), player(4, "DEF", 1),
        player(5, "MID", 1), player(6, "MID", 1), player(7, "MID", 1), player(8, "MID", 1), player(9, "MID", 1),
        player(10, "FWD", 1), player(11, "FWD", 1),
    ]
    bench = [player(12, "GKP", 1), player(13, "FWD", 1), player(14, "DEF", 1), player(15, "MID", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[6] = stats(0, 0)
    for p in bench:
        live_stats[p["element"]] = stats(90, 5)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    outs = {a["player_out"] for a in result["autosubs"]}
    ins = {a["player_in"] for a in result["autosubs"]}
    assert outs == {5, 6}
    assert ins == {13, 14}
    assert 15 not in result["effective_players"]  # bench MID wasn't needed
    assert result["unresolved_players"] == []


def test_8_first_bench_did_not_play_use_second():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[14] = stats(0, 0)  # bench 1 also blanked
    live_stats[13] = stats(90, 7)  # bench 2 played
    live_stats[12] = stats(0, 0)
    live_stats[15] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 13, "reason": "starter_did_not_play"} in result["autosubs"]
    assert len(result["autosubs"]) == 1


def test_9_first_bench_would_be_illegal_formation_skip():
    # 3 DEF / 4 MID / 3 FWD (FWD already at its cap of 3); one MID
    # blanks. Bench 1 is a FWD -- adding a 4th FWD would breach the cap,
    # so it must be skipped for bench 2's DEF instead.
    starters = [
        player(1, "GKP", 1),
        player(2, "DEF", 1), player(3, "DEF", 1), player(4, "DEF", 1),
        player(5, "MID", 1), player(6, "MID", 1), player(7, "MID", 1), player(8, "MID", 1),
        player(9, "FWD", 1), player(10, "FWD", 1), player(11, "FWD", 1),
    ]
    bench = [player(12, "GKP", 1), player(13, "FWD", 1), player(14, "DEF", 1), player(15, "MID", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    for p in bench:
        live_stats[p["element"]] = stats(90, 5)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 14, "reason": "starter_did_not_play"} in result["autosubs"]
    assert len(result["autosubs"]) == 1
    assert 13 not in result["effective_players"]  # the FWD was correctly skipped


def test_10_bench_player_scored_zero_but_played():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[14] = stats(15, 0)  # played, scored nothing
    for p in [bench[0], bench[2], bench[3]]:
        live_stats[p["element"]] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert 14 in result["effective_players"]
    assert result["points"] == 10 * 4 + 0


def test_11_bench_player_scored_negative_but_played():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[14] = stats(90, -1)
    for p in [bench[0], bench[2], bench[3]]:
        live_stats[p["element"]] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert 14 in result["effective_players"]
    assert result["points"] == 10 * 4 - 1


def test_12_starter_on_bench_irl_but_match_still_live_no_autosub():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "DEF", 1), player(14, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)  # 0 minutes so far, but match not finished
    for p in bench:
        live_stats[p["element"]] = stats(90, 6)  # even though bench players did play
    fixtures = [fixture(1, 2, finished=False)]  # still live

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert result["autosubs"] == []
    assert 5 in result["effective_players"]
    assert result["is_final"] is False


def test_13_starters_match_finished_zero_appearance_autosub_runs():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[14] = stats(90, 5)
    for p in [bench[0], bench[2], bench[3]]:
        live_stats[p["element"]] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=True)]  # match is over

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 14, "reason": "starter_did_not_play"} in result["autosubs"]


def test_14_double_gameweek_player_one_fixture_left_no_autosub_yet():
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "DEF", 1), player(14, "MID", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)  # blanked the first of two fixtures
    for p in bench:
        live_stats[p["element"]] = stats(90, 6)
    # team 1 has two fixtures this gameweek -- one already finished
    # (where player 5 blanked), one still to come.
    fixtures = [fixture(1, 2, finished=True), fixture(1, 3, finished=False)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert result["autosubs"] == []
    assert 5 in result["effective_players"]
    assert result["is_final"] is False


def test_15_three_simultaneous_missing_starters_bench_priority_and_formation():
    # 3 DEF / 4 MID / 3 FWD (FWD at cap); DEF, one MID, and FWD all
    # blank at once. Bench order: FWD, MID, DEF.
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(13, "FWD", 1), player(14, "MID", 1), player(15, "DEF", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[2] = stats(0, 0)  # DEF blanks
    live_stats[5] = stats(0, 0)  # MID blanks
    live_stats[9] = stats(0, 0)  # FWD blanks
    for p in bench:
        live_stats[p["element"]] = stats(90, 6)
    fixtures = [fixture(1, 2, finished=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    # Bench 1 (FWD, id 13): removing the blanked FWD (9) and adding a FWD
    # is a no-op swap, always legal -> used.
    # Bench 2 (MID, id 14): removing the blanked MID (5) and adding a MID
    # is likewise a no-op -> used.
    # Bench 3 (DEF, id 15): removing the blanked DEF (2) and adding a DEF
    # is likewise a no-op -> used. All three gaps filled, one per bench
    # slot, in priority order.
    outs = {a["player_out"] for a in result["autosubs"]}
    ins = {a["player_in"] for a in result["autosubs"]}
    assert outs == {2, 5, 9}
    assert ins == {13, 14, 15}
    assert result["unresolved_players"] == []


def test_16_finished_provisional_true_but_finished_false_still_triggers_autosub():
    # Observed in production: FPL Draft can sit at finished=false /
    # finished_provisional=true for days after full time while bonus
    # points are confirmed. Waiting on the stricter flag meant an
    # autosub that should have fired at the final whistle never did.
    starters = base_starters()
    bench = [player(12, "GKP", 1), player(14, "MID", 1), player(13, "DEF", 1), player(15, "FWD", 1)]
    live_stats = {p["element"]: stats(90, 4) for p in starters}
    live_stats[5] = stats(0, 0)
    live_stats[14] = stats(90, 5)
    for p in [bench[0], bench[2], bench[3]]:
        live_stats[p["element"]] = stats(0, 0)
    fixtures = [fixture(1, 2, finished=False, finished_provisional=True)]

    result = calculate_effective_lineup(starters, bench, live_stats, fixtures)

    assert {"player_out": 5, "player_in": 14, "reason": "starter_did_not_play"} in result["autosubs"]
    assert result["is_final"] is True
