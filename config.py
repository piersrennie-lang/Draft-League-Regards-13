"""League configuration. The only file you should need to edit."""

LEAGUE_ID = 39805
LEAGUE_NAME = "Draft League Regards"
SEASON = "2026/27"

DRAFT_API = "https://draft.premierleague.com/api"
CLASSIC_API = "https://fantasy.premierleague.com/api"

# Draft API rejects non-browser clients. Do not remove.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"https://draft.premierleague.com/league/{LEAGUE_ID}/standings",
}

# House rule: each manager must release the highest scorer from their active XI
# after every gameweek. Bench points are exempt. Not modelled by the FPL API,
# so it is computed from picks + live scores.
HIGHEST_SCORER_RULE = True

# Fines, in pounds. Used for the pot calculation on the breach panel.
FINE_NOT_RELEASED = 5
FINE_FIELDED_ANYWAY = 5
BASE_POT = 200
PRIZE_SHARE = 0.40  # first prize as a share of the pot

# Season prize pool structure, from the committee's Proposed Prize Pool
# document -- amounts in pounds, one list per award, ordered 1st/2nd/3rd
# place. Edit here if the prize pool changes; build_prize_forecast() in
# kpis.py reads this to rank managers against each award's own metric.
PRIZE_POOL = {
    "h2h": [60, 40, 20],
    "classic": [10],
    "manager_of_season": [10],
    "highest_gw_score": [5, 3, 2],
    "longest_win_streak": [5, 3, 2],
    "manager_of_week": [5, 3, 2],
    "totw_appearances": [5, 3, 2],
    "best_transfers": [5, 3, 2],
    "manager_of_month_per_win": 1,
}

# Entertainment ratings are relative to the week, not absolute. A five-fixture
# week is scored on this curve, best to worst, which is how the roundup reads.
ENTERTAINMENT_CURVE = {5: [9, 8, 7, 5, 3]}
