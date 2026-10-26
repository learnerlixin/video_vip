"""title.py —— 片名识别与清洗。

    _SITE_NAMES / _SITE_NAME_RE / _SITE_LEAD_RE / _SITE_TAIL_RE / _SITE_WORD_RE
                                    平台名与噪声词正则
    wsyzy_clean_title(t)            清洗片名（去平台名/集数标记/标点）
    read_video_title(window)        从页面读出片名，返回 TitleResult{title, trusted}
    is_clean_title(t)               判断片名是否已足够干净
    wait_stable_title(session, ...) 等片名稳定（SPA 异步渲染）

`trusted` 的含义：精确选择器 / og:title 命中即视为可信，否则要等稳定。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .util import PageSession
from .page import attr_of, text_of

# ─────────────────────────────────────────────────────────────────────────────
# 平台名正则（re.sub 默认全局替换）
# ─────────────────────────────────────────────────────────────────────────────
_SITE_NAMES = (
    "爱奇艺|腾讯视频|腾讯|优酷|芒果TV|芒果|哔哩哔哩|bilibili|B站|搜狐视频|搜狐|"
    "乐视视频|乐视|PPTV聚力|PPTV|1905电影网|1905|土豆网|土豆|风行网|风行|"
    "西瓜视频|咪咕视频|咪咕|AcFun"
)

_SITE_NAME_RE = re.compile(
    r"(?:^|[\s\-_：:|｜，,。·]+)(?:" + _SITE_NAMES + r")(?=$|[\s\-_：:|｜，,。·]+)",
    re.IGNORECASE,
)
_SITE_LEAD_RE = re.compile(r"^(?:" + _SITE_NAMES + r")+", re.IGNORECASE)
_SITE_TAIL_RE = re.compile(r"(?:[\s\-_:：|｜]+|^)(?:" + _SITE_NAMES + r")$", re.IGNORECASE)
_SITE_WORD_RE = re.compile(r"^(?:" + _SITE_NAMES + r")$", re.IGNORECASE)

_BRACKETS_RE = re.compile(r"[《》【】「」]")
_NOISE_WORDS_RE = re.compile(r"在线观看|高清正版|免费观看|完整版|正片|预告|全集")
_SPLIT_RE = re.compile(r"[-_\s（(|｜]")
_EP_TAG_RE = re.compile(r"第.+[集季部]")
_EP_NUM_RE = re.compile(r"第\d{1,8}[集期话季]")


def wsyzy_clean_title(t: Optional[str]) -> str:
    """`wsyzyCleanTitle(t)`：剥掉书名号、平台名、"在线观看/正片"等噪声，
    再按分隔符切块，取第一个「长度≥2 且不是平台名 且不是第X集」的片段。"""
    t = t or ""
    t = _BRACKETS_RE.sub("", t)
    t = _SITE_NAME_RE.sub(" ", t)
    t = _SITE_LEAD_RE.sub("", t)
    t = _SITE_TAIL_RE.sub("", t)
    t = _NOISE_WORDS_RE.sub("", t)

    parts: List[str] = []
    for s in _SPLIT_RE.split(t):
        s = _EP_TAG_RE.sub("", s, count=1).strip()   # 只替换首个
        if s:
            parts.append(s)

    for p in parts:
        if len(p) >= 2 and not _SITE_WORD_RE.match(p) and not _EP_NUM_RE.search(p):
            return p
    return parts[0] if parts else ""


# ─────────────────────────────────────────────────────────────────────────────
# readVideoTitle
# ─────────────────────────────────────────────────────────────────────────────
PRECISE: Dict[str, str] = {
    "qq.com": ".intro-title[title], .video-title[title], .player-title",
    "iqiyi.com": '[data-ai-entity="视频名称、主标题"], [data-ai-entity*="主标题"], '
    '.album-head-title, [class*="meta_title"]',
    "iq.com": '[data-ai-entity="视频名称、主标题"], [data-ai-entity*="主标题"]',
    "youku.com": '.video-title, a[data-pb-txid="pg_playlist_title"][title]',
    "mgtv.com": 'h2[class*="mgtv-player-aside-info__title"][title]',
    "bilibili.com": '[class*="mediaTitle"][title]',
    "sohu.com": 'a[data-pb-txid="pg_playlist_title"][title]',
}


@dataclass
class TitleResult:
    title: str
    trusted: bool


def read_video_title(page: PageSession) -> TitleResult:
    """`readVideoTitle()`：精确选择器 → og:title → h1 → document.title 四级兜底。"""
    doc = page.document
    hn = page.window.location.hostname if page.window else ""

    if doc is not None:
        for key, selectors in PRECISE.items():
            if key not in hn:
                continue
            for sel in selectors.split(","):
                el = doc.query_selector(sel.strip())
                if el is None:
                    continue
                t = (
                    attr_of(el, "title")
                    or attr_of(el, "content")
                    or text_of(el)
                ).strip()
                # 精确选择器只在影片信息面板渲染完成后存在，命中即视为可信
                if t and wsyzy_clean_title(t):
                    return TitleResult(title=wsyzy_clean_title(t), trusted=True)

        og = doc.query_selector('meta[property="og:title"]')
        if og is not None:
            t = (attr_of(og, "content") or text_of(og)).strip()
            # og:title 是服务端静态输出，不随 SPA 异步加载变化，同样可信
            if t and wsyzy_clean_title(t):
                return TitleResult(title=wsyzy_clean_title(t), trusted=True)

        h1 = doc.query_selector("h1")
        if h1 is not None:
            t = text_of(h1)
            if t and wsyzy_clean_title(t):
                return TitleResult(title=wsyzy_clean_title(t), trusted=False)

    return TitleResult(title=wsyzy_clean_title(doc.title if doc else ""), trusted=False)


# ─────────────────────────────────────────────────────────────────────────────
# isCleanTitle / waitStableTitle
# ─────────────────────────────────────────────────────────────────────────────
_CLEAN_SITE_RE = re.compile(
    r"(?:^|[\s\-_：:|｜，,。·])(?:" + _SITE_NAMES + r")(?:$|[\s\-_：:|｜，,。·])",
    re.IGNORECASE,
)
_CLEAN_LEAD_RE = re.compile(r"^(?:" + _SITE_NAMES + r")", re.IGNORECASE)
_CLEAN_NOISE_RE = re.compile(r"在线观看|高清正版|免费观看|完整版|正片|预告")


def is_clean_title(t: Optional[str]) -> bool:
    """`isCleanTitle(t)`：不含平台名、不含"在线观看/正片"等词，且长度≥2。"""
    if not t or len(t) < 2:
        return False
    if _CLEAN_SITE_RE.search(t) or _CLEAN_LEAD_RE.match(t):
        return False
    if _CLEAN_NOISE_RE.search(t):
        return False
    return True


def wait_stable_title(
    page: PageSession,
    on_tick: Optional[Callable[[int, str], None]] = None,
    max_wait: float = 10000,
    interval: float = 500,
    should_abort: Optional[Callable[[], bool]] = None,
) -> str:
    """`waitStableTitle(onTick, maxWait = 10000, interval = 500)`。

    分级判定：
      1、精确选择器/og:title 命中（trusted）→ 立即返回，不等待；
      2、h1/document.title 兜底：连续 1.5 秒未变化且清洗通过 → 返回；
      3、连续 5 秒未变化但仍清洗不掉 → 不再等待，交给下游清洗+重搜；
      4、maxWait 内仍未定论 → 直接采用当前标题。

    「标题变化」来自 SPA 异步渲染；这里通过 PageSession 定期重抓
    页面来观察 document.title 的变化，判定阈值原样保留。
    `should_abort` 用于外部中断（`if (_aborted) return last;`）。
    """
    start = time.time()
    cur = read_video_title(page)
    if cur.trusted and cur.title:
        return cur.title                       # 快路径：标题已就绪，无需等待

    last = cur.title
    stable_since = time.time()

    while (time.time() - start) * 1000 < max_wait:
        time.sleep(interval / 1000.0)
        if should_abort and should_abort():
            return last                        # 用户已切换解析源，立即交回控制权
        page.refresh_if_stale(max_age=interval / 1000.0)
        cur = read_video_title(page)
        # 秒数取整用 .5 向上取整（与 Python round 的银行家舍入不同）
        elapsed = int(time.time() - start + 0.5)

        if cur.trusted and cur.title:
            if on_tick:
                on_tick(elapsed, cur.title)
            return cur.title

        if cur.title != last:
            last = cur.title
            stable_since = time.time()
        else:
            stable_for = (time.time() - stable_since) * 1000
            if (stable_for >= 1500 and is_clean_title(last)) or (stable_for >= 5000 and last):
                if on_tick:
                    on_tick(elapsed, last)
                return last

        if on_tick:
            on_tick(elapsed, last)

    return last
