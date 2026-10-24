#!/usr/bin/env python3
"""Import the official Hitokoto sentences-bundle into data/quotes.json.

Source: https://github.com/hitokoto-osc/sentences-bundle (AGPL-3.0)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "https://raw.githubusercontent.com/hitokoto-osc/sentences-bundle/master/"
USER_AGENT = "PCL-Hitokoto-Library-Importer/1.0"


def fetch(url: str, attempts: int = 4) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {last_error}")


def iso_from_unix(value: object) -> str:
    try:
        timestamp = int(str(value).strip())
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Import official Hitokoto sentences-bundle.")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parents[1] / "data"),
        help="target data directory (default: ../data)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SENTENCES_BUNDLE_BASE", DEFAULT_BASE),
        help="sentences-bundle base URL",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/") + "/"
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_file = data_dir / "quotes.json"

    categories = json.loads(fetch(base + "categories.json").decode("utf-8-sig"))
    version = json.loads(fetch(base + "version.json").decode("utf-8-sig"))

    quotes: list[dict[str, str]] = []
    seen_text: set[str] = set()
    seen_ids: set[str] = set()
    duplicate_count = 0
    missing_created = 0
    per_category: dict[str, dict[str, object]] = {}

    for category in categories:
        if not isinstance(category, dict):
            continue
        key = str(category.get("key") or "")
        name = str(category.get("name") or key)
        path = str(category.get("path") or f"./sentences/{key}.json").lstrip("./")
        if not key:
            continue

        items = json.loads(fetch(base + path).decode("utf-8-sig"))
        if not isinstance(items, list):
            raise RuntimeError(f"category {key}: expected a JSON array")

        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("hitokoto") or "").strip()
            if not text:
                continue
            text_key = text.casefold()
            if text_key in seen_text:
                duplicate_count += 1
                continue

            quote_id = str(item.get("uuid") or "").strip()
            if not quote_id:
                raw_id = str(item.get("id") or "").strip()
                quote_id = f"official-{key}-{raw_id}" if raw_id else f"official-{key}-{len(quotes) + 1}"
            if quote_id in seen_ids:
                quote_id = f"{quote_id}-{key}"
            seen_ids.add(quote_id)
            seen_text.add(text_key)

            created_at = iso_from_unix(item.get("created_at"))
            if not created_at:
                missing_created += 1

            quotes.append(
                {
                    "id": quote_id,
                    "text": text,
                    "source": str(item.get("from") or "").strip(),
                    "author": str(item.get("from_who") or "").strip(),
                    "type": name,
                    "typeCode": key,
                    "createdAt": created_at,
                }
            )
            added += 1

        per_category[key] = {"name": name, "count": len(items), "added": added}
        print(f"category {key} {name}: read={len(items)} added={added}")

    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {
        "name": "PCL Hitokoto Library",
        "updatedAt": updated_at,
        "count": len(quotes),
        "quotes": quotes,
    }

    if output_file.exists():
        shutil.copy2(output_file, data_dir / "quotes.json.bak")

    temp_file = output_file.with_name(output_file.name + ".tmp")
    temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, output_file)

    (data_dir / "LICENSE-AGPL-3.0.txt").write_bytes(fetch(base + "LICENSE"))
    (data_dir / "SOURCE.json").write_text(
        json.dumps(
            {
                "source": "https://github.com/hitokoto-osc/sentences-bundle",
                "source_raw": base,
                "license": "AGPL-3.0",
                "license_file": "LICENSE-AGPL-3.0.txt",
                "bundleVersion": version.get("bundle_version"),
                "bundleUpdatedAt": version.get("updated_at"),
                "importedAt": updated_at,
                "count": len(quotes),
                "duplicatesRemoved": duplicate_count,
                "missingCreatedAt": missing_created,
                "categories": per_category,
                "notes": [
                    "Derived from the official Hitokoto sentences-bundle.",
                    "Fields were renamed: hitokoto->text, from->source, from_who->author.",
                    "type was replaced with the Chinese category name; typeCode keeps the original key.",
                    "Items were de-duplicated by text (case-insensitive).",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"total={len(quotes)} duplicates_removed={duplicate_count} missing_created_at={missing_created}")
    print(f"output={output_file} bytes={output_file.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
