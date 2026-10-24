#!/usr/bin/env python3
"""Render the single-file PCL homepage XAML.

The server writes one quote into ``templates/Custom.xaml`` and returns the
result. PCL only has to download ``Custom.xaml``; no local PowerShell helper is
needed for this edition.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE = BASE_DIR / "templates" / "Custom.xaml"

BEIJING = timezone(timedelta(hours=8))
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _xml_text(value: Any) -> str:
    """Escape a value for XML text or a double-quoted XML attribute.

    Braces are emitted as character references. This keeps PCL's ``{...}``
    replacement markers from rewriting quote text, and also prevents a quote
    that starts with ``{`` from being parsed as a XAML markup extension.
    """

    text = "" if value is None else str(value)
    text = _SURROGATE.sub("", text)
    text = "".join(
        ch
        for ch in text
        if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
    )
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )
    return text


def _quote_meta(quote: dict[str, Any]) -> str:
    source = str(quote.get("source") or "").strip() or "出处不详"
    author = str(quote.get("author") or "").strip() or "佚名"
    quote_type = str(quote.get("type") or "").strip() or "其他"
    if not (source.startswith("《") and source.endswith("》")):
        source = f"《{source}》"
    return f"—— {source} / {author} / {quote_type}"


def render_homepage(
    *,
    quote: dict[str, Any],
    count: int,
    base_url: str,
    version: str = "1.0",
    now: datetime | None = None,
) -> str:
    """Return a complete Custom.xaml fragment with one quote baked in."""

    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    current = now or datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    else:
        current = current.astimezone(BEIJING)

    values = {
        "__QUOTE__": str(quote.get("text") or "路漫漫其修远兮，吾将上下而求索。"),
        "__META__": _quote_meta(quote),
        "__STATUS__": f"● 在线句库 · 共 {count} 条 · {current:%Y-%m-%d %H:%M}",
        "__INFO__": "本页由服务端动态生成；每次启动 PCL 会重新下载并换一句。断网时 PCL 会显示上次缓存。",
        "__COUNT__": str(count),
        "__DATE__": current.strftime("%Y-%m-%d"),
        "__YEAR__": str(current.year),
        "__VERSION__": version,
        "__BASE_URL__": base_url.rstrip("/"),
    }

    for key, value in values.items():
        template = template.replace(key, _xml_text(value))

    unresolved = sorted(set(_PLACEHOLDER.findall(template)))
    if unresolved:
        raise ValueError("unresolved homepage placeholders: " + ", ".join(unresolved))
    return template


def fallback_homepage(
    title: str = "主页暂时不可用",
    message: str = "服务端正在更新，请稍后点击“刷新主页”重试。",
) -> str:
    """A minimal valid homepage used when the template or quote data is broken."""

    safe_title = _xml_text(title)
    safe_message = _xml_text(message)
    return f"""<local:MyCard Title="{safe_title}" Margin="0,0,0,15" CanSwap="False" IsSwapped="False">
    <StackPanel Margin="25,40,23,15">
        <TextBlock FontSize="15" FontWeight="Bold" TextWrapping="Wrap" Text="{safe_title}" />
        <TextBlock FontSize="12.5" Foreground="#666666" TextWrapping="Wrap" Margin="0,8,0,10" Text="{safe_message}" />
        <local:MyButton Height="35" Padding="18,0,18,0" ColorType="Highlight" Text="刷新主页" EventType="刷新主页" />
    </StackPanel>
</local:MyCard>
"""
