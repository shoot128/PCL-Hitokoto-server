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
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE = BASE_DIR / "templates" / "Custom.xaml"

# Search results are rendered in pages so a broad keyword does not create a
# multi-thousand-row page in one go.  All matches stay on the server for this
# request; the "load more" button asks for a larger prefix of the same list.
SEARCH_PAGE_SIZE = 100
SEARCH_PAGE_STEP = 200

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


def _search_status(query: str, total: int, shown: int) -> str:
    if not query:
        return (
            "输入关键词后点“搜索”；服务端会检索整个句库。"
            f"先显示前 {SEARCH_PAGE_SIZE} 条，点“显示更多”继续加载，所有命中都会保留；"
            "点每句下方的出处行可以复制整句。"
        )
    if total <= 0:
        return f"搜索“{query}” · 没有找到匹配的一言。"
    if shown >= total:
        return (
            f"搜索“{query}” · 共 {total} 条，已全部列出；"
            "点每句下方的出处行即可复制。"
        )
    return (
        f"搜索“{query}” · 共 {total} 条，当前显示 {shown} 条；"
        "点下方“显示更多”继续加载，点出处行复制，不会丢结果。"
    )


def _render_load_more(
    query: str,
    total: int,
    shown: int,
    base_url: str,
) -> str:
    """Render a button that asks for a larger prefix of the same result list.

    The URL is generated on the server with the current query already
    percent-encoded, so the button works on both PCL 2.99 and PCL CE without a
    binding: the collection event runs before the button's own refresh event.
    """

    if not query or shown >= total:
        return ""
    remaining = total - shown
    batch = min(SEARCH_PAGE_STEP, remaining)
    next_shown = shown + batch
    url = f"{base_url.rstrip('/')}/Custom.xaml?n={next_shown}&q={quote(query, safe='')}"
    label = (
        f"显示剩余 {remaining} 条"
        if remaining <= batch
        else f"再显示 {batch} 条（还有 {remaining} 条）"
    )
    return f"""<WrapPanel Margin="0,10,0,0">
    <local:MyButton Height="35"
                    Padding="16,0,16,0"
                    Text="{_xml_text(label)}"
                    ToolTip="只增加显示数量，不会丢掉已经找到的结果"
                    EventType="刷新主页"
                    EventData="-">
        <local:CustomEventService.Events>
            <local:CustomEventCollection>
                <local:CustomEvent Type="修改设置" Data="UiCustomNet|{_xml_text(url)}" />
            </local:CustomEventCollection>
        </local:CustomEventService.Events>
    </local:MyButton>
</WrapPanel>"""


def _render_search_results(
    query: str,
    results: list[dict[str, Any]],
) -> str:
    if not query:
        return ""
    if not results:
        return (
            '<TextBlock FontSize="12.5" Foreground="#777777" TextWrapping="Wrap" '
            'Text="没有找到匹配的一言；换个关键词，或点“清空搜索”回到随机一言。" />'
        )

    items: list[str] = []
    for index, quote in enumerate(results, 1):
        text = _xml_text(quote.get("text") or "（空）")
        meta_text = _quote_meta(quote).removeprefix("—— ")
        meta = _xml_text(f"{index}. {meta_text} · 点击复制")
        items.append(
            '<StackPanel Margin="0,0,0,12">'
            '<TextBlock TextWrapping="Wrap" '
            'FontSize="14" '
            'LineHeight="21" '
            'Foreground="{DynamicResource ColorBrush1}" '
            f'Text="{text}" />'
            f'<local:MyTextButton Text="{meta}" '
            'FontSize="11" '
            'Foreground="#888888" '
            'Background="Transparent" '
            'Padding="0" '
            'Margin="0,3,0,0" '
            'HorizontalAlignment="Left" '
            'ToolTip="点击复制整句" '
            'EventType="复制文本" '
            f'EventData="{text}" />'
            '</StackPanel>'
        )
    return "\n".join(items)


def _render_search_card_pcl299() -> str:
    """One-click search card for PCL 2.99.

    PCL 2.99 supports Bindings on ``CustomEvent.Data``, so the search text can
    be written directly into ``UiCustomNet``.  Avoid the ``修改变量`` +
    ``{variable:HitokotoQ}`` indirection: it adds a registry write and a second
    async event that can prevent the attached refresh from running.  PCL CE
    2.15.1 cannot bind ``CustomEvent.Data`` and receives the two-button card
    below instead.
    """

    return """<local:MyCard Title="搜索一言库" Margin="0,0,0,15">
    <StackPanel Margin="25,40,23,15">
        <TextBlock FontSize="12.5"
                   Foreground="#555555"
                   TextWrapping="Wrap"
                   Margin="0,0,0,10"
                   Text="PCL 2.99：输入关键词后点一次“搜索”即可。按钮会把关键词直接写进本机联网主页地址并刷新；服务端按本次请求检索句库，先把前 100 条写回页面，点“显示更多”继续加载，所有命中都会保留；点每句下方的出处行可复制整句。每个 PCL 保存自己的链接，互不覆盖。如果当前主页是“预设”，搜索会自动切换到“联网更新”。" />

        <TextBox x:Name="SearchBox"
                 Height="34"
                 Padding="8,0,8,0"
                 VerticalContentAlignment="Center"
                 Text="__SEARCH_QUERY__"
                 ToolTip="中文、英文、空格和 &amp; 都可以；不要包含竖线。" />

        <WrapPanel Margin="0,10,0,0">
            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="18,0,18,0"
                            ColorType="Highlight"
                            Text="搜索"
                            EventType="刷新主页"
                            EventData="-">
                <local:CustomEventService.Events>
                    <local:CustomEventCollection>
                        <local:CustomEvent Type="修改设置" Data="UiCustomType|2" />
                        <local:CustomEvent Type="修改设置" Data="{Binding Text, Source={x:Reference SearchBox}, StringFormat='UiCustomNet|__BASE_URL__/Custom.xaml?q={0}'}" />
                    </local:CustomEventCollection>
                </local:CustomEventService.Events>
            </local:MyButton>

            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="16,0,16,0"
                            Text="刷新结果"
                            ToolTip="通常不需要点；如果搜索后页面仍未更新，点这里重试"
                            EventType="刷新主页"
                            EventData="-" />

            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="16,0,16,0"
                            Text="清空搜索"
                            EventType="刷新主页"
                            EventData="-">
                <local:CustomEventService.Events>
                    <local:CustomEventCollection>
                        <local:CustomEvent Type="修改设置" Data="UiCustomNet|__BASE_URL__/Custom.xaml" />
                    </local:CustomEventCollection>
                </local:CustomEventService.Events>
            </local:MyButton>
        </WrapPanel>

        <TextBlock FontSize="12"
                   Margin="0,2,0,6"
                   Foreground="#666666"
                   TextWrapping="Wrap">
            <TextBlock.Text>__SEARCH_STATUS__</TextBlock.Text>
        </TextBlock>

        <StackPanel Margin="0,2,0,0">__SEARCH_RESULTS__</StackPanel>
        __SEARCH_MORE__
    </StackPanel>
</local:MyCard>

"""


def _render_search_card() -> str:
    """Search card for PCL CE 2.15.1.

    PCL CE does not substitute URL markers in the custom homepage address.
    The search button writes the per-user setting ``UiCustomNet`` to a full
    URL containing the query; a second button refreshes the homepage. PCL CE
    2.15.1 ``CustomEvent.Data`` is a plain CLR property and cannot take a
    Binding, so the dynamic value must live on ``MyButton.EventData`` (an
    attached DependencyProperty). Collection events run before the button's
    own event, so a one-click collection cannot set the URL before refreshing.
    """

    return """<local:MyCard Title="搜索一言库" Margin="0,0,0,15">
    <StackPanel Margin="25,40,23,15">
        <TextBlock FontSize="12.5"
                   Foreground="#555555"
                   TextWrapping="Wrap"
                   Margin="0,0,0,10"
                   Text="输入关键词后点“搜索”，把搜索条件写进本机 PCL 的联网主页设置（UiCustomNet）；再点“刷新结果”下载。服务端按本次请求检索句库，先把前 100 条写回页面，点“显示更多”会自动刷新并继续加载，所有命中都会保留；点每句下方的出处行可复制整句。每个 PCL 各自保存自己的链接，互不覆盖。" />

        <TextBox x:Name="SearchBox"
                 Height="34"
                 Padding="8,0,8,0"
                 VerticalContentAlignment="Center"
                 Text="__SEARCH_QUERY__"
                 ToolTip="中文、英文、空格和 &amp; 都可以；不要包含竖线或井号。" />

        <WrapPanel Margin="0,10,0,0">
            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="18,0,18,0"
                            ColorType="Highlight"
                            Text="搜索"
                            EventType="修改设置"
                            EventData="{Binding Text, Source={x:Reference SearchBox}, StringFormat='UiCustomNet|__BASE_URL__/Custom.xaml?q={0}'}">
                <local:CustomEventService.Events>
                    <local:CustomEventCollection>
                        <local:CustomEvent Type="修改设置" Data="UiCustomType|2" />
                    </local:CustomEventCollection>
                </local:CustomEventService.Events>
            </local:MyButton>

            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="18,0,18,0"
                            Text="刷新结果"
                            EventType="刷新主页"
                            EventData="-" />

            <local:MyButton Margin="0,0,10,8"
                            Height="35"
                            Padding="16,0,16,0"
                            Text="清空搜索"
                            EventType="刷新主页"
                            EventData="-">
                <local:CustomEventService.Events>
                    <local:CustomEventCollection>
                        <local:CustomEvent Type="修改设置" Data="UiCustomNet|__BASE_URL__/Custom.xaml|-" />
                    </local:CustomEventCollection>
                </local:CustomEventService.Events>
            </local:MyButton>
        </WrapPanel>

        <TextBlock FontSize="12"
                   Margin="0,2,0,6"
                   Foreground="#666666"
                   TextWrapping="Wrap">
            <TextBlock.Text>__SEARCH_STATUS__</TextBlock.Text>
        </TextBlock>

        <StackPanel Margin="0,2,0,0">__SEARCH_RESULTS__</StackPanel>
        __SEARCH_MORE__
    </StackPanel>
</local:MyCard>

"""


def _render_static_search_notice() -> str:
    """Shown in the static snapshot, where there is no server to search."""

    return """<local:MyCard Title="搜索需要动态版" Margin="0,0,0,15">
    <StackPanel Margin="25,40,23,15">
        <TextBlock FontSize="12.5"
                   Foreground="#555555"
                   TextWrapping="Wrap"
                   Text="当前是静态快照，只能展示固定的一句，不能搜索。使用服务器动态链接后，输入关键词即可由服务端检索句库并把结果写回页面。" />
    </StackPanel>
</local:MyCard>

"""


def render_homepage(
    *,
    quote: dict[str, Any],
    count: int,
    base_url: str,
    version: str = "1.0",
    now: datetime | None = None,
    query: str = "",
    results: list[dict[str, Any]] | None = None,
    total_results: int | None = None,
    search_enabled: bool = True,
    pcl299: bool = False,
) -> str:
    """Return a complete Custom.xaml fragment with one quote baked in.

    ``query`` and ``results`` belong to this request only. ``results`` is the
    prefix currently shown and ``total_results`` is the full match count; the
    page gets a "load more" button while the prefix is shorter than the total.
    The renderer never writes request data to module state, so two PCL users
    searching at the same time cannot overwrite each other's page.
    """

    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    current = now or datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    else:
        current = current.astimezone(BEIJING)
    result_list = list(results or [])
    shown = len(result_list)
    total = max(shown, int(total_results if total_results is not None else shown))
    query = str(query or "").strip()

    template = template.replace(
        "__SEARCH_CARD__",
        (
            _render_search_card_pcl299()
            if search_enabled and pcl299
            else _render_search_card()
            if search_enabled
            else _render_static_search_notice()
        ),
    )
    refresh_events = ""
    if search_enabled:
        safe_base = _xml_text(base_url.rstrip("/"))
        reset_url = f"{safe_base}/Custom.xaml"
        if pcl299:
            refresh_events = (
                "<local:CustomEventService.Events>\n"
                "                    <local:CustomEventCollection>\n"
                '                        <local:CustomEvent Type="修改设置" '
                f'Data="UiCustomNet|{reset_url}" />\n'
                "                    </local:CustomEventCollection>\n"
                "                </local:CustomEventService.Events>"
            )
        else:
            refresh_events = (
                "<local:CustomEventService.Events>\n"
                "                    <local:CustomEventCollection>\n"
                '                        <local:CustomEvent Type="修改设置" '
                f'Data="UiCustomNet|{reset_url}|-" />\n'
                "                    </local:CustomEventCollection>\n"
                "                </local:CustomEventService.Events>"
            )
    template = template.replace("__REFRESH_EVENTS_BLOCK__", refresh_events)

    values = {
        "__QUOTE__": str(quote.get("text") or "路漫漫其修远兮，吾将上下而求索。"),
        "__META__": _quote_meta(quote),
        "__STATUS__": (
            f"● 在线句库 · 共 {count} 条 · {current:%Y-%m-%d %H:%M}"
            if search_enabled
            else f"● 静态快照 · 共 {count} 条 · {current:%Y-%m-%d %H:%M}"
        ),
        "__INFO__": (
            "本页由服务端动态生成；每次启动 PCL 会重新下载并换一句。断网时 PCL 会显示上次缓存。"
            if search_enabled
            else "这是静态快照，由 GitHub / jsDelivr 提供；不会自动换句，也不能搜索。断网时 PCL 会显示上次缓存。"
        ),
        "__COUNT__": str(count),
        "__DATE__": current.strftime("%Y-%m-%d"),
        "__YEAR__": str(current.year),
        "__VERSION__": version,
        "__BASE_URL__": base_url.rstrip("/"),
        "__SEARCH_QUERY__": query,
        "__SEARCH_STATUS__": _search_status(query, total, shown),
        "__SEARCH_USAGE__": (
            "2. 静态快照没有服务端，不能搜索；动态版在 PCL 2.99 输入关键词后点一次“搜索”即可，PCL CE 再点“刷新结果”。每个 PCL 各存各的地址，服务端按本次请求检索，不保存上一位用户的关键词。"
            if not search_enabled
            else (
                "2. 输入关键词后点一次“搜索”：按钮会把关键词直接写进本机 PCL 的联网主页地址并刷新；如果页面仍未更新，再点旁边的“刷新结果”重试。结果先显示前 100 条，点“显示更多”继续加载，所有命中都会保留。每个 PCL 各存各的地址，服务端按本次请求检索，不保存上一位用户的关键词；关键词里不要包含竖线。"
                if pcl299
                else "2. 动态版先点“搜索”，把本机 PCL 的联网主页地址改成 https://你的域名/Custom.xaml?q=关键词；再点“刷新结果”下载。结果先显示前 100 条，点“显示更多”会自动刷新并继续加载，所有命中都会保留。每个 PCL 各存各的地址，服务端按本次请求检索，不保存上一位用户的关键词；关键词里不要包含竖线 | 或井号 #。"
            )
        ),
        "__REFRESH_TOOLTIP__": (
            "清空搜索条件并重新下载 Custom.xaml，服务端会返回新的一句。"
            if search_enabled
            else "静态快照没有服务端；这里只重新读取当前页面，不会换句。"
        ),
        "__REFRESH_USAGE__": (
            "3. 每次启动 PCL 都会重新检查版本；服务端每次返回新的时间戳，因此会重新下载并换一句。页面里的“换一句”和“清空搜索”会把联网主页地址改回不带 q 的地址，再刷新下载，所以会回到随机一言。"
            if search_enabled
            else "3. 静态快照没有服务端；“换一句”只会重新读取当前页面，不能换句，也不会修改联网主页地址。"
        ),
    }

    for key, value in values.items():
        template = template.replace(key, _xml_text(value))

    unresolved = sorted(set(_PLACEHOLDER.findall(template)))
    for deferred in ("__SEARCH_RESULTS__", "__SEARCH_MORE__"):
        if deferred in unresolved:
            unresolved.remove(deferred)
    if unresolved:
        raise ValueError("unresolved homepage placeholders: " + ", ".join(unresolved))

    # Insert result XAML last so a quote containing ``__LIKE_THIS__`` cannot be
    # mistaken for an unresolved template placeholder.
    template = template.replace(
        "__SEARCH_RESULTS__",
        _render_search_results(query, result_list) if search_enabled else "",
    )
    template = template.replace(
        "__SEARCH_MORE__",
        (
            _render_load_more(query, total, shown, base_url)
            if search_enabled
            else ""
        ),
    )
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
