#!/usr/bin/env python3
"""Import the official Hitokoto sentences-bundle into data/quotes.json.

Source: https://github.com/hitokoto-osc/sentences-bundle (AGPL-3.0)

The downloading/conversion logic lives in ``library_source.py`` so the CLI and
the service's periodic refresh share one schema.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# When invoked as ``python3 tools/import_sentences_bundle.py`` the parent
# directory (the server root) is not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library_source import (  # noqa: E402  (path setup must run first)
    DEFAULT_BASE_URL,
    build_library,
    fetch,
    write_json_atomic,
    write_source_metadata,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import official Hitokoto sentences-bundle.")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parents[1] / "data"),
        help="target data directory (default: ../data)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SENTENCES_BUNDLE_BASE")
        or os.getenv("HITOKOTO_BUNDLE_BASE")
        or DEFAULT_BASE_URL,
        help="sentences-bundle base URL",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/") + "/"
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_file = data_dir / "quotes.json"

    payload = build_library(base)
    for key, category in payload["categories"].items():
        print(f"category {key} {category['name']}: read={category['count']} added={category['added']}")

    if output_file.exists():
        shutil.copy2(output_file, data_dir / "quotes.json.bak")

    write_json_atomic(output_file, payload)
    (data_dir / "LICENSE-AGPL-3.0.txt").write_bytes(fetch(base + "LICENSE"))
    write_source_metadata(data_dir, payload)

    print(
        f"total={payload['count']} "
        f"duplicates_removed={payload['duplicatesRemoved']} "
        f"missing_created_at={payload['missingCreatedAt']}"
    )
    print(f"output={output_file} bytes={output_file.stat().st_size}")
    print(f"bundle_version={payload.get('bundleVersion')} bundle_updated_at={payload.get('bundleUpdatedAt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
