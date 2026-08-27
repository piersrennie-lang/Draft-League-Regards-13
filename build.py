"""Render the roundup.

Two inputs, deliberately separate:
  data/derived/gw{n}.json  computed metrics, never hand-edited
  data/prose/gw{n}.json    editorial copy and rating overrides, hand-written

Missing prose is not an error. Sections fall back to the numbers and the page
says so, which is better than a page that quietly reads as finished.

Usage:
    python build.py            # latest derived gameweek
    python build.py --gw 2
"""

import argparse
import json
import pathlib
import shutil

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = pathlib.Path(__file__).parent
DERIVED = ROOT / "data" / "derived"
PROSE = ROOT / "data" / "prose"
DIST = ROOT / "dist"


def latest_gw():
    weeks = [int(p.stem[2:]) for p in DERIVED.glob("gw*.json")
             if p.stem[2:].isdigit()]
    if not weeks:
        raise SystemExit("Nothing in data/derived. Run kpis.py first.")
    return max(weeks)


def ordinal(n):
    if n is None:
        return ""
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int)
    args = ap.parse_args()
    gw = args.gw or latest_gw()

    data = json.loads((DERIVED / f"gw{gw}.json").read_text())
    prose_path = PROSE / f"gw{gw}.json"
    prose = json.loads(prose_path.read_text()) if prose_path.exists() else {}

    # Editorial may override a rating where the story is not in the numbers,
    # e.g. a benched 17-point defender. Keyed "home_name v away_name".
    overrides = prose.get("entertainment_overrides", {})
    for m in data["matches"]:
        key = f"{m['home_name']} v {m['away_name']}"
        m["prose"] = prose.get("matches", {}).get(key, {})
        if key in overrides:
            m["entertainment"] = overrides[key]
    data["matches"].sort(key=lambda m: (-m["entertainment"], m["margin"]))
    for i, m in enumerate(data["matches"], start=1):
        m["excitement_rank"] = i

    pot = data["pot"]
    fines = sum(b["fine"] for b in data["breaches"])
    pot["fines"] = fines
    pot["total"] = pot["base"] + fines
    pot["first_prize"] = round(pot["total"] * pot["prize_share"])

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ordinal"] = ordinal
    env.filters["signed"] = lambda n: "" if not n else f"{'+' if n > 0 else ''}{n}"

    html = env.get_template("roundup.html").render(
        d=data, prose=prose, gw=gw, has_squads=bool(data["squads"]),
    )

    DIST.mkdir(exist_ok=True)
    (DIST / "index.html").write_text(html)
    shutil.copytree(ROOT / "static", DIST / "static", dirs_exist_ok=True)
    (DIST / f"gw{gw}.html").write_text(html)

    # Single file with the CSS inlined, for sending round the league the way
    # the PDF used to go round.
    css = (ROOT / "static" / "style.css").read_text()
    standalone = html.replace(
        '<link rel="stylesheet" href="static/style.css">',
        f"<style>\n{css}\n</style>",
    )
    (DIST / f"gw{gw}-standalone.html").write_text(standalone)

    print(f"Built dist/index.html for gameweek {gw}")
    print(f"       dist/gw{gw}-standalone.html (single file, CSS inlined)")
    if not prose_path.exists():
        print(f"  no prose at data/prose/gw{gw}.json, numbers only")


if __name__ == "__main__":
    main()
