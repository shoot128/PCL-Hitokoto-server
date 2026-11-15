#!/usr/bin/env python3
"""Download and convert the official Hitokoto sentences-bundle.

The same builder is used by the CLI importer and by the running service's
periodic refresh. Keeping it in one place means a manual import and an
automatic refresh produce exactly the same schema.

Source: https://github.com/hitokoto-osc/sentences-bundle (AGPL-3.0)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://raw.githubusercontent.com/hitokoto-osc/sentences-bundle/master/"
USER_AGENT = "PCL-Hitokoto-Library-Importer/1.1"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def fetch(url: str, attempts: int = 4, timeout: int = 90) -> bytes:
    """Download one URL with bounded retries.

    The response is capped so a broken mirror cannot fill memory or disk.
    """

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise RuntimeError(f"response too large: {url}")
                return data
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {last_error}")


def iso_from_unix(value: object) -> str:
    """Convert a Hitokoto Unix timestamp to ISO 8601, or return an empty string."""

    try:
        timestamp = int(str(value).strip())
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def build_library(base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    """Download the bundle and return a quotes.json-shaped payload.

    The returned payload also carries source metadata (bundle version,
    category counts, duplicate count) for ``data/SOURCE.json``. It does not
    touch the filesystem, so the caller can validate the result before
    replacing a live library.
    """

    base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/") + "/"
    categories = json.loads(fetch(base + "categories.json").decode("utf-8-sig"))
    version = json.loads(fetch(base + "version.json").decode("utf-8-sig"))

    if not isinstance(categories, list):
        raise RuntimeError("categories.json must be a JSON array")
    if not isinstance(version, dict):
        version = {}

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
                    # Explicit marker so a refresh can distinguish official
                    # entries from quotes added through the admin API.
                    "origin": "official",
                }
            )
            added += 1

        per_category[key] = {"name": name, "count": len(items), "added": added}

    if not quotes:
        raise RuntimeError("the downloaded bundle contains no quotes")

    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "name": "PCL Hitokoto Library",
        "source": "https://github.com/hitokoto-osc/sentences-bundle",
        "sourceRaw": base,
        "license": "AGPL-3.0",
        "updatedAt": updated_at,
        "count": len(quotes),
        "quotes": quotes,
        "bundleVersion": version.get("bundle_version") or version.get("version"),
        "bundleUpdatedAt": version.get("updated_at"),
        "duplicatesRemoved": duplicate_count,
        "missingCreatedAt": missing_created,
        "categories": per_category,
    }


def write_json_atomic(path: Path, payload: Any, mode: int = 0o600) -> None:
    """Write JSON to a temporary file and atomically replace ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_name(path.name + ".tmp")
    temp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temp_file, mode)
    except OSError:
        pass
    os.replace(temp_file, path)


def write_source_metadata(
    data_dir: Path,
    payload: dict[str, Any],
    *,
    custom_count: int = 0,
) -> None:
    """Write the machine-readable source/version file next to quotes.json."""

    data_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source": payload.get("source") or "https://github.com/hitokoto-osc/sentences-bundle",
        "source_raw": payload.get("sourceRaw") or DEFAULT_BASE_URL,
        "license": payload.get("license") or "AGPL-3.0",
        "license_file": "LICENSE-AGPL-3.0.txt",
        "bundleVersion": payload.get("bundleVersion"),
        "bundleUpdatedAt": payload.get("bundleUpdatedAt"),
        "importedAt": payload.get("updatedAt"),
        "count": payload.get("count", 0),
        "customCount": custom_count,
        "duplicatesRemoved": payload.get("duplicatesRemoved", 0),
        "missingCreatedAt": payload.get("missingCreatedAt", 0),
        "categories": payload.get("categories", {}),
        "notes": [
            "Derived from the official Hitokoto sentences-bundle.",
            "Fields were renamed: hitokoto->text, from->source, from_who->author.",
            "type was replaced with the Chinese category name; typeCode keeps the original key.",
            "Items were de-duplicated by text (case-insensitive).",
            "origin=official marks downloaded entries; origin=custom marks entries added through the admin API.",
        ],
    }
    write_json_atomic(data_dir / "SOURCE.json", metadata, mode=0o644)
