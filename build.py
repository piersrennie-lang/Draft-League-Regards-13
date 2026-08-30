"""Render the site: transfers and standings, straight from the numbers.

Reads data/derived/gw{n}.json, which is the only input. kpis.py computes
everything on the page; nothing here is hand-edited.

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
DIST = ROOT / "dist"


def latest_gw():
    weeks = [int(p.stem[2:]) for p in DERIVED.glob("gw*.json")
             if p.stem[2:].isdigit()]
    if not weeks:
        raise SystemExit("Nothing in data/derived. Run kpis.py first.")
    return max(weeks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int)
    args = ap.parse_args()
    gw = args.gw or latest_gw()

    data = json.loads((DERIVED / f"gw{gw}.json").read_text())
    next_gw = gw + 1

    transfers_current = []
    for le, m in data["managers"].items():
        cur = data["transfers"].get(le, {"in": [], "out": [], "count": 0, "source": "none"})
        transfers_current.append({"manager": m["manager"], "team": m["team"], **cur})
    transfers_current.sort(key=lambda t: t["manager"])

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["signed"] = lambda n: "" if not n else f"{'+' if n > 0 else ''}{n}"

    html = env.get_template("roundup.html").render(
        d=data, gw=gw, next_gw=next_gw, transfers_current=transfers_current,
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


if __name__ == "__main__":
    main()
