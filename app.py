#!/usr/bin/env python3
"""Linux-side Hitokoto quote library service.

This service is the server counterpart of the PCL custom homepage:

  GET  /healthz          health check
  GET  /hitokoto.json    full library; use it as PCL "下载完整句库" URL
  GET  /api/quotes       same data as /hitokoto.json
  GET  /api/hitokoto     one random quote; use it as PCL "自定义 API" URL
  POST /api/quotes       add one quote (requires admin token)
  POST /api/quotes/bulk  add many quotes (requires admin token)

The process is intended to listen on 127.0.0.1 and be exposed by Nginx.
Only a single process/worker should write the JSON file.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent

HOST = (os.getenv("HITOKOTO_HOST", "127.0.0.1").strip() or "127.0.0.1")
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


def _resolve_data_file() -> Path:
    raw = os.getenv("HITOKOTO_DATA_FILE", "data/quotes.json").strip() or "data/quotes.json"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATA_FILE = _resolve_data_file()
LOCK = threading.RLock()
QUOTES: list[dict[str, Any]] = []
UPDATED_AT = ""

logger = logging.getLogger("hitokoto")


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


def _load_library() -> None:
    global QUOTES, UPDATED_AT

    if not DATA_FILE.exists():
        QUOTES = []
        UPDATED_AT = _now()
        logger.warning("data file does not exist yet: %s", DATA_FILE)
        return

    raw = json.loads(DATA_FILE.read_text(encoding="utf-8-sig"))
    items = _extract_items(raw)

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
    if isinstance(raw, dict):
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

    key = quote["text"].casefold()
    for existing in QUOTES:
        if existing["text"].casefold() == key:
            return None, existing["id"]

    QUOTES.append(quote)
    return quote, None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_library()
    if ADMIN_TOKEN:
        logger.info("POST endpoints are enabled")
    else:
        logger.warning("HITOKOTO_ADMIN_TOKEN is empty; POST endpoints are disabled")
    yield


app = FastAPI(
    title="PCL Hitokoto Library",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


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


@app.get("/")
def index() -> dict[str, Any]:
    with LOCK:
        count = len(QUOTES)
    return {
        "service": "hitokoto-library",
        "source": "https://github.com/hitokoto-osc/sentences-bundle",
        "license": "AGPL-3.0",
        "count": count,
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
    logger.info("starting Hitokoto library service on http://%s:%d", HOST, PORT)
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level=LOG_LEVEL,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=True,
    )
