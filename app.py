#!/usr/bin/env python3
"""Linux-side Hitokoto quote library service.

This service is the server counterpart of the PCL custom homepage:

  GET  /healthz          health check
  GET  /Custom.xaml      single-file PCL homepage with one quote baked in
                         (``u`` is the PCL user id, ``q`` is a per-request search)
  GET  /Custom.xaml.ini  homepage version check for PCL
  GET  /hitokoto.json    full library; use it as PCL "下载完整句库" URL
  GET  /api/quotes       same data as /hitokoto.json
  GET  /api/hitokoto     one random quote; use it as PCL "自定义 API" URL
  POST /api/quotes       add one quote (requires admin token)
  POST /api/quotes/bulk  add many quotes (requires admin token)

The process is intended to listen on loopback (IPv4 and IPv6) and be
exposed by Nginx. Set HITOKOTO_HOST to a comma-separated list such as
``127.0.0.1,::1`` for dual-stack loopback, or ``::`` for dual-stack wildcard.
The official sentence bundle is refreshed in-process every 8 hours by
default; failures keep the current library and retry later.
Only a single process/worker should write the JSON file.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import os
import secrets
import shutil
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator

import homepage as homepage_xaml
import library_source

BASE_DIR = Path(__file__).resolve().parent

def _parse_hosts(raw: str) -> list[str]:
    """Parse a comma-separated bind list; ``::`` means dual-stack wildcard."""
    hosts: list[str] = []
    for part in raw.split(","):
        host = part.strip()
        if not host:
            continue
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        # Bind two explicit wildcard sockets instead of relying on the OS
        # bindv6only setting. This works even when net.ipv6.bindv6only=1.
        expanded = ("0.0.0.0", "::") if host == "::" else (host,)
        for item in expanded:
            if item not in hosts:
                hosts.append(item)
    # A wildcard already covers every address of the same family; keeping a
    # more specific address would make the second bind fail with EADDRINUSE.
    if "0.0.0.0" in hosts:
        hosts = [host for host in hosts if host == "0.0.0.0" or ":" in host]
    if "::" in hosts:
        hosts = [host for host in hosts if host == "::" or ":" not in host]
    return hosts or ["127.0.0.1", "::1"]


HOSTS = _parse_hosts(os.getenv("HITOKOTO_HOST", "127.0.0.1,::1"))
try:
    PORT = int(os.getenv("HITOKOTO_PORT", "13469"))
except ValueError as exc:
    raise SystemExit("HITOKOTO_PORT must be an integer") from exc
if not 10000 <= PORT <= 65535:
    raise SystemExit("HITOKOTO_PORT must be between 10000 and 65535")

LOG_LEVEL = (os.getenv("HITOKOTO_LOG_LEVEL", "info").strip().lower() or "info")
if LOG_LEVEL not in {"critical", "error", "warning", "info", "debug", "trace"}:
    LOG_LEVEL = "info"

ADMIN_TOKEN = os.getenv("HITOKOTO_ADMIN_TOKEN", "").strip()
HOME_BASE_URL = (
    os.getenv("HITOKOTO_PUBLIC_BASE_URL", "https://yiyan.overpass.top").strip()
    or "https://yiyan.overpass.top"
).rstrip("/")

# Hosts that may render their own origin into the generated search action.
# The canonical domain and the current origin hostname are both accepted so
# the page can be tested before the DNS CNAME is switched.
_RENDER_HOSTS = frozenset({"yiyan.overpass.top", "xa-1.overpass.top"})


def _request_base_url(request: Request) -> str:
    """Return the scheme+host to embed in the page, with a safe fallback."""

    host = request.headers.get("host", "").split(",")[0].strip().lower()
    hostname = host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host
    proto = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    )
    if proto not in {"http", "https"}:
        proto = request.url.scheme
    if hostname in _RENDER_HOSTS:
        return f"{proto}://{hostname}"
    return HOME_BASE_URL


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc


# Download the official bundle every 8 hours by default. Set to 0 to disable
# the in-process scheduler (for example when an external timer runs the CLI).
REFRESH_HOURS = _env_float("HITOKOTO_REFRESH_HOURS", 8.0)
if not math.isfinite(REFRESH_HOURS):
    raise SystemExit("HITOKOTO_REFRESH_HOURS must be finite")
if REFRESH_HOURS < 0:
    raise SystemExit("HITOKOTO_REFRESH_HOURS must be >= 0")
if 0 < REFRESH_HOURS < 1 / 60:
    raise SystemExit("HITOKOTO_REFRESH_HOURS must be 0 or at least 1 minute")
REFRESH_INTERVAL_SECONDS = int(round(REFRESH_HOURS * 3600))
REFRESH_RETRY_SECONDS = max(60, _env_int("HITOKOTO_REFRESH_RETRY_SECONDS", 600))
MIN_REFRESH_QUOTES = 100

BUNDLE_BASE_URL = (
    os.getenv("HITOKOTO_BUNDLE_BASE", library_source.DEFAULT_BASE_URL).strip()
    or library_source.DEFAULT_BASE_URL
).rstrip("/") + "/"
if not BUNDLE_BASE_URL.startswith(("https://", "http://")):
    raise SystemExit("HITOKOTO_BUNDLE_BASE must be an http(s) URL")

LIBRARY_REFRESH_LOCK = threading.Lock()


def _resolve_data_file() -> Path:
    raw = os.getenv("HITOKOTO_DATA_FILE", "data/quotes.json").strip() or "data/quotes.json"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATA_FILE = _resolve_data_file()
LOCK = threading.RLock()
QUOTES: list[dict[str, Any]] = []
SEARCH_FIELDS = ("text", "source", "author", "type", "typeCode")
SEARCH_INDEX: list[str] = []
DEFAULT_SEARCH_PAGE = 100
UPDATED_AT = ""

logger = logging.getLogger("hitokoto")


def _display_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _create_listen_socket(host: str, port: int) -> socket.socket:
    """Create one listening socket for an IPv4 or IPv6 literal."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
        # IPv4 is handled by its own wildcard socket when HITOKOTO_HOST=::.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def _create_listen_sockets() -> list[socket.socket]:
    sockets: list[socket.socket] = []
    errors: list[str] = []
    for host in HOSTS:
        try:
            sock = _create_listen_socket(host, PORT)
        except OSError as exc:
            errors.append(f"{_display_host(host)}: {exc}")
            logger.warning(
                "cannot listen on %s:%d: %s", _display_host(host), PORT, exc
            )
            continue
        sockets.append(sock)
        logger.info("listening on %s:%d", _display_host(host), PORT)
    if not sockets:
        raise SystemExit("could not bind any listen address: " + "; ".join(errors))
    return sockets


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_unix_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, OSError):
        return 0


def _clean(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:max_len]


def _pick(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _normalize_quote(item: dict[str, Any]) -> dict[str, Any] | None:
    text = _clean(_pick(item, "text", "hitokoto", "content", "quote"), 4000)
    if not text:
        return None

    source = _clean(_pick(item, "source", "from"), 500)
    author = _clean(_pick(item, "author", "from_who"), 500)
    quote_type = _clean(_pick(item, "type", "category"), 100) or "其他"
    quote_id = _clean(item.get("id") or item.get("uuid") or "", 200) or str(uuid.uuid4())
    created_at = _clean(_pick(item, "createdAt", "created_at"), 100) or _now()

    quote: dict[str, Any] = {
        "id": quote_id,
        "text": text,
        "source": source,
        "author": author,
        "type": quote_type,
        "createdAt": created_at,
    }
    for key in ("typeCode", "origin", "fetchedAt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            quote[key] = value.strip()
    return quote


def _extract_items(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("quotes", "data", "results", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested in ("quotes", "data", "results", "items"):
                    if isinstance(value.get(nested), list):
                        return value[nested]
    raise ValueError("JSON root must be an array, or contain quotes/data/results/items")


def _search_haystack(quote: dict[str, Any]) -> str:
    """Return the casefolded searchable text for one quote."""

    return " ".join(str(quote.get(field) or "") for field in SEARCH_FIELDS).casefold()


def _rebuild_search_index_locked() -> None:
    """Rebuild the per-quote search index; caller must hold ``LOCK``."""

    global SEARCH_INDEX
    SEARCH_INDEX = [_search_haystack(quote) for quote in QUOTES]


def _load_library() -> None:
    global QUOTES, UPDATED_AT

    if not DATA_FILE.exists():
        QUOTES = []
        UPDATED_AT = ""
        _rebuild_search_index_locked()
        logger.warning("data file does not exist yet: %s", DATA_FILE)
        return

    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8-sig"))
        items = _extract_items(raw)
    except (OSError, ValueError):
        logger.exception("could not read %s; starting with an empty library", DATA_FILE)
        QUOTES = []
        UPDATED_AT = ""
        _rebuild_search_index_locked()
        return

    seen: set[str] = set()
    quotes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = _normalize_quote(item)
        if quote is None:
            continue
        key = quote["text"].casefold()
        if key in seen:
            continue
        seen.add(key)
        quotes.append(quote)

    QUOTES = quotes
    _rebuild_search_index_locked()
    if not quotes:
        UPDATED_AT = ""
    elif isinstance(raw, dict):
        UPDATED_AT = _clean(raw.get("updatedAt"), 100) or _now()
    else:
        UPDATED_AT = _now()
    logger.info("loaded %d quotes from %s", len(QUOTES), DATA_FILE)


def _write_library_locked() -> None:
    global UPDATED_AT

    UPDATED_AT = _now()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "PCL Hitokoto Library",
        "source": "https://github.com/hitokoto-osc/sentences-bundle",
        "license": "AGPL-3.0",
        "updatedAt": UPDATED_AT,
        "count": len(QUOTES),
        "quotes": QUOTES,
    }
    temp_file = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    temp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temp_file, 0o600)
    except OSError:
        pass
    os.replace(temp_file, DATA_FILE)


def _is_custom_quote(quote: dict[str, Any]) -> bool:
    """Return True for a quote that must survive an official-bundle refresh.

    New admin entries carry ``origin=custom``. Older data predates that marker;
    the official importer is the only writer that sets ``typeCode``, so an
    entry without ``typeCode`` is treated as a local addition too.
    """

    return quote.get("origin") == "custom" or not str(quote.get("typeCode") or "").strip()


def _refresh_library() -> dict[str, Any]:
    """Download the official bundle and atomically replace the live library.

    Network I/O happens before ``LOCK`` is taken. The old library stays intact
    if the download, validation, or write fails. Custom quotes are merged into
    the new official catalog by text, so admin-added entries are not lost.
    """

    global QUOTES, UPDATED_AT

    if not LIBRARY_REFRESH_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "another refresh is already running"}

    try:
        payload = library_source.build_library(BUNDLE_BASE_URL)

        official: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload.get("quotes", []):
            if not isinstance(item, dict):
                continue
            quote = _normalize_quote(item)
            if quote is None:
                continue
            key = quote["text"].casefold()
            if key in seen:
                continue
            seen.add(key)
            official.append(quote)

        if len(official) < MIN_REFRESH_QUOTES:
            raise RuntimeError(
                f"downloaded only {len(official)} quotes; "
                f"refusing to replace the library (minimum {MIN_REFRESH_QUOTES})"
            )

        with LOCK:
            official_texts = {quote["text"].casefold() for quote in official}
            custom: list[dict[str, Any]] = []
            custom_texts: set[str] = set()
            for quote in QUOTES:
                if not _is_custom_quote(quote):
                    continue
                key = str(quote.get("text") or "").casefold()
                if not key or key in official_texts or key in custom_texts:
                    continue
                custom_texts.add(key)
                custom.append(dict(quote))

            merged = official + custom
            updated_at = str(payload.get("updatedAt") or _now())

            out_payload = {
                "name": "PCL Hitokoto Library",
                "source": "https://github.com/hitokoto-osc/sentences-bundle",
                "sourceRaw": payload.get("sourceRaw"),
                "license": "AGPL-3.0",
                "updatedAt": updated_at,
                "count": len(merged),
                "quotes": merged,
                "bundleVersion": payload.get("bundleVersion"),
                "bundleUpdatedAt": payload.get("bundleUpdatedAt"),
                "duplicatesRemoved": payload.get("duplicatesRemoved", 0),
                "missingCreatedAt": payload.get("missingCreatedAt", 0),
                "categories": payload.get("categories", {}),
            }

            if DATA_FILE.exists():
                try:
                    shutil.copy2(DATA_FILE, DATA_FILE.with_name(DATA_FILE.name + ".bak"))
                except OSError as exc:
                    logger.warning("could not back up %s: %s", DATA_FILE, exc)

            library_source.write_json_atomic(DATA_FILE, out_payload, mode=0o600)
            QUOTES = merged
            _rebuild_search_index_locked()
            UPDATED_AT = updated_at

        try:
            library_source.write_source_metadata(DATA_FILE.parent, payload, custom_count=len(custom))
        except Exception as exc:
            logger.warning("library refreshed but SOURCE.json was not updated: %s", exc)

        return {
            "skipped": False,
            "official": len(official),
            "custom": len(custom),
            "total": len(merged),
            "bundleVersion": payload.get("bundleVersion"),
        }
    finally:
        LIBRARY_REFRESH_LOCK.release()


def _initial_refresh_delay() -> int:
    """Schedule the first refresh at the remaining part of the 8h window."""

    if REFRESH_INTERVAL_SECONDS <= 0:
        return 0
    if not UPDATED_AT:
        return min(60, REFRESH_INTERVAL_SECONDS)
    try:
        updated = datetime.fromisoformat(UPDATED_AT.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - updated).total_seconds()))
    except (TypeError, ValueError, OSError):
        return min(60, REFRESH_INTERVAL_SECONDS)
    if age >= REFRESH_INTERVAL_SECONDS:
        return min(60, REFRESH_INTERVAL_SECONDS)
    return max(60, REFRESH_INTERVAL_SECONDS - age)


async def _library_refresh_loop() -> None:
    """Periodically refresh the official catalog without blocking requests."""

    delay = _initial_refresh_delay()
    logger.info(
        "library auto-refresh enabled: every %d seconds, retry after %d seconds",
        REFRESH_INTERVAL_SECONDS,
        REFRESH_RETRY_SECONDS,
    )
    while True:
        await asyncio.sleep(delay)
        try:
            summary = await asyncio.to_thread(_refresh_library)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automatic library refresh failed; keeping the current library")
            delay = max(60, min(REFRESH_RETRY_SECONDS, REFRESH_INTERVAL_SECONDS))
            continue

        if summary.get("skipped"):
            logger.info("automatic library refresh skipped: %s", summary.get("reason"))
        else:
            logger.info(
                "automatic library refresh complete: official=%d custom=%d total=%d bundle=%s",
                summary.get("official", 0),
                summary.get("custom", 0),
                summary.get("total", 0),
                summary.get("bundleVersion") or "-",
            )
        delay = REFRESH_INTERVAL_SECONDS


def _require_admin(authorization: str | None, x_admin_token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POST endpoints are disabled: HITOKOTO_ADMIN_TOKEN is not set",
        )

    supplied = (x_admin_token or "").strip()
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    if not supplied or not secrets.compare_digest(
        supplied.encode("utf-8"), ADMIN_TOKEN.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _add_one_locked(payload: QuoteIn) -> tuple[dict[str, Any] | None, str | None]:
    quote = _normalize_quote(payload.model_dump())
    if quote is None:
        return None, None

    # Mark entries added through the admin API so an official refresh can
    # keep them while replacing the downloaded catalog.
    quote.setdefault("origin", "custom")
    key = quote["text"].casefold()
    for existing in QUOTES:
        if existing["text"].casefold() == key:
            return None, existing["id"]

    QUOTES.append(quote)
    SEARCH_INDEX.append(_search_haystack(quote))
    return quote, None


def _homepage_quote(seed: str) -> dict[str, Any] | None:
    """Pick one quote for the single-file homepage.

    Without a seed each request gets a random quote. A seed (for example a
    timestamp supplied by PCL) makes the result stable for that seed.
    """

    with LOCK:
        if not QUOTES:
            return None
        if not seed:
            return dict(secrets.choice(QUOTES))
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(QUOTES)
        return dict(QUOTES[index])


def _normalize_search_query(raw: str) -> str:
    """Normalize a PCL search term without turning it into shared state."""

    value = html.unescape(str(raw or ""))
    value = "".join(
        ch
        for ch in value
        if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
    )
    value = " ".join(value.split())
    return value[:80]


def _raw_query_value(raw_query: str, name: str) -> str:
    """Read a query value from the raw URL.

    PCL HTML-encodes variable values before inserting them into the homepage
    URL, so a search term containing ``&`` can arrive as ``&amp;``. Reading the
    raw value and unescaping it after URL-decoding keeps the whole term intact.
    The homepage URL places ``q`` last, so everything after ``q=`` belongs to it.
    """

    if not raw_query:
        return ""
    marker = name + "="
    index = raw_query.find("&" + marker)
    if index >= 0:
        value = raw_query[index + 1 + len(marker):]
    elif raw_query.startswith(marker):
        value = raw_query[len(marker):]
    else:
        return ""
    try:
        value = unquote(value)
    except Exception:
        pass
    return html.unescape(value)


def _search_quotes(query: str) -> list[dict[str, Any]]:
    """Search the in-memory library for one request; never mutate global state.

    Every match is returned. The library is bounded (roughly ten thousand
    quotes), so a request cannot grow without limit; truncating here would
    hide matches the user explicitly asked to see. The client renders all
    returned rows, which keeps a very broad keyword (for example one common
    Chinese character) honest instead of silently dropping results.
    """

    needle = query.casefold()
    if not needle:
        return []
    matches: list[dict[str, Any]] = []
    with LOCK:
        if len(SEARCH_INDEX) != len(QUOTES):
            _rebuild_search_index_locked()
        for quote, haystack in zip(QUOTES, SEARCH_INDEX):
            if needle in haystack:
                matches.append(dict(quote))
    return matches


def _shown_count(raw: str, total: int) -> int:
    """Return how many matches this request should render.

    ``n`` is the number of rows currently requested.  Invalid, missing or
    non-positive values fall back to the first page; the result is always
    clamped to the real match count so a hand-edited URL cannot claim more.
    """

    if total <= 0:
        return 0
    try:
        value = int(str(raw or "").strip())
    except (TypeError, ValueError):
        value = DEFAULT_SEARCH_PAGE
    if value <= 0:
        value = DEFAULT_SEARCH_PAGE
    return min(value, total)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_library()
    if ADMIN_TOKEN:
        logger.info("POST endpoints are enabled")
    else:
        logger.warning("HITOKOTO_ADMIN_TOKEN is empty; POST endpoints are disabled")

    refresh_task = None
    if REFRESH_INTERVAL_SECONDS > 0:
        refresh_task = asyncio.create_task(
            _library_refresh_loop(),
            name="hitokoto-library-refresh",
        )
    else:
        logger.info("library auto-refresh is disabled (HITOKOTO_REFRESH_HOURS=0)")

    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task


app = FastAPI(
    title="PCL Hitokoto Library",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

NO_STORE_HEADERS = {
    # ``private`` matters: a search result belongs to one PCL user and must
    # never be served from a shared proxy cache to another user.
    "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Content-Type-Options": "nosniff",
}


class QuoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    source: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=500)
    type: str = Field(default="其他", max_length=100)

    @field_validator("text", "source", "author", "type", mode="before")
    @classmethod
    def _strip_fields(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    with LOCK:
        count = len(QUOTES)
    return f"ok\nquotes={count}\n"


@app.get("/Custom.xaml")
def custom_xaml(
    request: Request,
    seed: str = Query(default="", max_length=200),
    q: str = Query(default="", max_length=200),
    u: str = Query(default="", max_length=100),
    n: str = Query(default="", max_length=20),
) -> Response:
    """Single-file homepage consumed by PCL's online homepage updater.

    ``u`` is accepted for compatibility with older links that used PCL's
    ``{identify}`` marker, but PCL CE 2.15.1 does not substitute URL markers;
    current pages put the query directly in ``q``. It is used only to derive a
    short key for diagnostics. Search state is per request, never global, so
    concurrent users cannot see or overwrite one another's results.
    """

    query = _normalize_search_query(_raw_query_value(request.url.query, "q") or q)
    results = _search_quotes(query)
    shown = _shown_count(n, len(results))
    visible_results = results[:shown]
    user_key = hashlib.sha256(u.encode("utf-8")).hexdigest()[:12] if u else ""
    logger.debug(
        "homepage render user=%s query_len=%d results=%d shown=%d",
        user_key or "-",
        len(query),
        len(results),
        shown,
    )

    quote = _homepage_quote(seed.strip())
    if quote is None:
        body = homepage_xaml.fallback_homepage(
            "句库暂时为空",
            "服务端还没有载入一言数据，请稍后点击“刷新主页”重试。",
        )
    else:
        with LOCK:
            count = len(QUOTES)
        user_agent = request.headers.get("user-agent") or ""
        # Official PCL builds expose CustomEvent.Data as a DependencyProperty,
        # so the search box can be bound straight into the event. PCL CE 2.15
        # made Data a plain CLR property, which cannot take a Binding and needs
        # the two-button card. Version 2.15 only exists as CE; 2.12, 2.14 and
        # 2.99 are official builds and all support the one-click card.
        # PCL CE's UA is like "PCL2/2.9.4.0 PCLCE/2.15.1": the first token is the
        # upstream version CE forked from, so version matching alone is not enough.
        ce_ua = "PCLCE" in user_agent or "PCL CE" in user_agent or "PCL2/2.15" in user_agent
        pcl_one_click = "PCL2/" in user_agent and not ce_ua
        try:
            body = homepage_xaml.render_homepage(
                quote=quote,
                count=count,
                base_url=_request_base_url(request),
                query=query,
                results=visible_results,
                total_results=len(results),
                pcl299=pcl_one_click,
            )
        except Exception:
            logger.exception("failed to render homepage XAML")
            body = homepage_xaml.fallback_homepage()

    return Response(
        content=body,
        media_type="application/xml",
        headers={**NO_STORE_HEADERS, "Content-Type": "application/xml; charset=utf-8"},
    )


@app.get("/Custom.xaml.ini", response_class=PlainTextResponse)
@app.get("/version", response_class=PlainTextResponse)
def homepage_version() -> PlainTextResponse:
    """Timestamp checked by PCL before downloading Custom.xaml.

    Returning a new value on every request makes PCL download the homepage on
    each launch, which is what gives the single-file edition a fresh quote.
    """

    return PlainTextResponse(
        str(int(time.time() * 1000)),
        headers=NO_STORE_HEADERS,
    )


@app.get("/")
def index() -> dict[str, Any]:
    with LOCK:
        count = len(QUOTES)
    return {
        "service": "hitokoto-library",
        "source": "https://github.com/hitokoto-osc/sentences-bundle",
        "license": "AGPL-3.0",
        "count": count,
        "homepage": "/Custom.xaml",
        "homepage_version": "/Custom.xaml.ini",
        "library": "/hitokoto.json",
        "random": "/api/hitokoto",
        "health": "/healthz",
    }


@app.get("/hitokoto.json")
@app.get("/api/quotes")
def get_library() -> JSONResponse:
    with LOCK:
        payload = {
            "name": "PCL Hitokoto Library",
            "source": "https://github.com/hitokoto-osc/sentences-bundle",
            "license": "AGPL-3.0",
            "updatedAt": UPDATED_AT,
            "count": len(QUOTES),
            "quotes": [dict(quote) for quote in QUOTES],
        }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/hitokoto")
def random_quote() -> JSONResponse:
    with LOCK:
        if not QUOTES:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="quote library is empty",
            )
        quote = secrets.choice(QUOTES)
        payload = {
            "id": quote["id"],
            "uuid": quote["id"],
            "hitokoto": quote["text"],
            "type": quote["type"],
            "from": quote["source"],
            "from_who": quote["author"],
            "source": quote["source"],
            "author": quote["author"],
            "creator": "pcl-hitokoto-linux",
        }
        created_at = _to_unix_timestamp(quote.get("createdAt"))
        if created_at:
            payload["created_at"] = created_at
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/quotes", status_code=status.HTTP_201_CREATED)
def add_quote(
    payload: QuoteIn,
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> JSONResponse:
    _require_admin(authorization, x_admin_token)

    with LOCK:
        quote, duplicate_id = _add_one_locked(payload)
        if duplicate_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"quote already exists: {duplicate_id}",
            )
        _write_library_locked()
        count = len(QUOTES)

    return JSONResponse(
        {"ok": True, "count": count, "quote": quote},
        status_code=status.HTTP_201_CREATED,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/quotes/bulk")
def add_quotes_bulk(
    payload: list[QuoteIn],
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> JSONResponse:
    _require_admin(authorization, x_admin_token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body must contain at least one quote",
        )
    if len(payload) > 500:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="at most 500 quotes per request",
        )

    added = 0
    duplicates = 0
    with LOCK:
        for item in payload:
            quote, duplicate_id = _add_one_locked(item)
            if quote is None:
                duplicates += 1
            else:
                added += 1

        if added:
            _write_library_locked()
        count = len(QUOTES)

    return JSONResponse(
        {
            "ok": True,
            "added": added,
            "duplicates": duplicates,
            "count": count,
        },
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sockets = _create_listen_sockets()
    logger.info(
        "starting Hitokoto library service on %s",
        ", ".join(f"{_display_host(s.getsockname()[0])}:{s.getsockname()[1]}" for s in sockets),
    )
    config = uvicorn.Config(
        app,
        host=HOSTS[0],
        port=PORT,
        log_level=LOG_LEVEL,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1",
        access_log=True,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=sockets)
    finally:
        for sock in sockets:
            sock.close()
