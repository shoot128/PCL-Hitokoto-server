#!/usr/bin/env python3
"""Render the static preview at client/single/Custom.xaml.

The live homepage is rendered by app.py at request time. This script only
refreshes the GitHub snapshot, so it can be used as a fallback or for preview.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SERVER_DIR.parent
sys.path.insert(0, str(SERVER_DIR))

import homepage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, default=0, help="quote index in data/quotes.json")
    parser.add_argument("--base-url", default="https://yiyan.overpass.top")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_DIR / "client" / "single" / "Custom.xaml",
    )
    args = parser.parse_args()

    data = json.loads((SERVER_DIR / "data" / "quotes.json").read_text(encoding="utf-8-sig"))
    quotes = data.get("quotes") if isinstance(data, dict) else data
    if not isinstance(quotes, list) or not quotes:
        raise SystemExit("data/quotes.json contains no quotes")
    index = args.index % len(quotes)
    quote = quotes[index]

    xaml = homepage.render_homepage(
        quote=quote,
        count=len(quotes),
        base_url=args.base_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(xaml, encoding="utf-8")
    print(f"wrote {args.output} ({len(xaml.encode('utf-8'))} bytes, quote #{index})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
