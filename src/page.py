"""page.py —— window / document / location 的最小 DOM 兼容层。

没有真实浏览器，用 requests 抓 HTML + BeautifulSoup 解析，包装出
一套只读的最小 DOM，让上层代码可以按统一的形状访问：

    Window.location.href        当前 URL（含重定向后的最终地址）
    Document.title              <title>
    Document.query_selector(s)  CSS 选择器 → 单个 Tag
    Document.query_selector_all CSS 选择器 → Tag 列表
    Element.get("attr")         属性
    Element.get_text()          文本

抓不到的东西（getBoundingClientRect / getComputedStyle / 事件监听）没有
对应物，相关调用点退化成常量或直接跳过，代码里逐处注明。
"""

from __future__ import annotations

import re
import time
from typing import Any, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .config import DEFAULT_UA
from .net import WsyzyError, get_session

# SPA 站点（腾讯视频、爱奇艺…）的静态 HTML 里没有播放器容器，抓取时带上
# 完整浏览器头，尽量让服务端返回带首屏数据的版本。
PAGE_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}


class Location:
    """`window.location` 的只读快照。"""

    def __init__(self, href: str):
        parsed = urlparse(href)
        self.href: str = href
        self.protocol: str = parsed.scheme + ":" if parsed.scheme else ""
        self.host: str = parsed.netloc
        self.hostname: str = parsed.hostname or ""
        self.pathname: str = parsed.path
        self.search: str = ("?" + parsed.query) if parsed.query else ""

    def reload(self) -> None:
        """`window.location.reload()`：命令行下没有「当前页面」可刷新，空实现。"""
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Location {self.href!r}>"


class Document:
    """`document` 的最小实现：一个 BeautifulSoup 树 + 几个查询方法。"""

    def __init__(self, html: str, url: str = ""):
        self.html = html
        self.url = url
        self.soup = BeautifulSoup(html or "", "html.parser")

    # ── document.title ────────────────────────────────────────────────────
    @property
    def title(self) -> str:
        tag = self.soup.find("title")
        return tag.get_text().strip() if tag else ""

    # ── 选择器 ────────────────────────────────────────────────────────────
    def query_selector(self, selector: str) -> Optional[Any]:
        """`document.querySelector(selector)`；选择器非法时返回 None（不抛）。"""
        try:
            return self.soup.select_one(selector)
        except Exception:
            return None

    def query_selector_all(self, selector: str) -> List[Any]:
        """`document.querySelectorAll(selector)`"""
        try:
            return list(self.soup.select(selector))
        except Exception:
            return []

    def get_element_by_id(self, element_id: str) -> Optional[Any]:
        return self.soup.find(id=element_id)

    def contains(self, node: Any) -> bool:
        """`document.contains(node)`：节点是否还在这棵树里。"""
        if node is None:
            return False
        try:
            return node in self.soup.descendants or node is self.soup
        except Exception:
            return False

    @property
    def body(self) -> Optional[Any]:
        return self.soup.body

    @property
    def document_element(self) -> Optional[Any]:
        return self.soup.html or self.soup


class Window:
    """`window` 的最小实现。"""

    def __init__(self, href: str, html: str = "", user_agent: str = DEFAULT_UA):
        self.location = Location(href)
        self.document = Document(html, href)
        self.navigator_userAgent = user_agent
        # 视口尺寸：Python 里没有真实视口，取桌面常见值，
        # 只被 buildPlayerFrameLayout 用于算 iframe 高度。
        self.innerHeight = 900
        self.innerWidth = 1600

    @property
    def userAgent(self) -> str:
        return self.navigator_userAgent


_TAG_RE = re.compile(r"<[^>]+>")

# 浏览器判定编码的顺序：HTTP 头 charset → <meta charset> / http-equiv → 猜测。
# requests 的 apparent_encoding 依赖 charset_normalizer，装没装、版本不同结果都可能变，
# 单靠它会把 utf-8 页面读成 latin-1（爱奇艺就是这种情况），所以这里自己按声明优先。
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)
_HTTP_EQUIV_RE = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]+content\s*=\s*["'][^"']*charset=([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)
_HEADER_CHARSET_RE = re.compile(r"charset=([a-zA-Z0-9_\-]+)", re.IGNORECASE)


def detect_encoding(resp: "requests.Response") -> str:
    """按浏览器规则挑一个解码用的字符集，永远返回一个可用名字。"""
    content_type = resp.headers.get("content-type", "")
    m = _HEADER_CHARSET_RE.search(content_type)
    if m:                                    # 1) HTTP 头显式声明
        return m.group(1)

    head = resp.content[:4096]               # 2) <meta charset> / http-equiv
    m = _META_CHARSET_RE.search(head) or _HTTP_EQUIV_RE.search(head)
    if m:
        return m.group(1).decode("ascii", "ignore") or "utf-8"

    if resp.encoding and resp.encoding.lower() not in ("iso-8859-1",):
        return resp.encoding                 # 3) requests 已从头部推断出的编码

    apparent = getattr(resp, "apparent_encoding", None)
    if apparent:                             # 4) 内容嗅探（charset_normalizer）
        return apparent

    return "utf-8"                           # 5) 兜底


def strip_tags(text: str) -> str:
    """`el.textContent` 的降级实现（拿到的不是 bs4 Tag 而是原始字符串时用）。"""
    return _TAG_RE.sub("", text or "").strip()


def fetch(
    url: str, timeout: float = 12.0, headers: Optional[dict] = None, retries: int = 2
) -> Window:
    """抓取页面并包成 Window（等价于「浏览器加载完这个 URL」）。

    `retries`：站点偶发限流（B 站会返回 412）或瞬时网络抖动时自动重试——浏览器
    本来就会重发请求，这里显式补上，避免一次抖动就判定页面不可用。
    """
    merged = dict(PAGE_HEADERS)
    if headers:
        merged.update(headers)

    last_err: Optional[WsyzyError] = None
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = get_session().get(url, timeout=timeout, headers=merged)
        except requests.Timeout as exc:
            last_err = WsyzyError("timeout")
            last_err.__cause__ = exc
        except requests.RequestException as exc:
            last_err = WsyzyError("net_err")
            last_err.__cause__ = exc
        else:
            if resp.status_code == 200:
                break
            last_err = WsyzyError("resp_err")
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    else:
        raise last_err or WsyzyError("resp_err")

    if resp.status_code != 200:
        raise last_err or WsyzyError("resp_err")

    resp.encoding = detect_encoding(resp)
    text = resp.text
    # 声明的字符集偶尔是错的（gbk 声明 + utf-8 内容）：出现替换符且 utf-8 能干净
    # 解码时，改按 utf-8 重来一遍，避免片名变成乱码。
    if "\ufffd" in text:
        try:
            candidate = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            candidate = None
        if candidate is not None and "\ufffd" not in candidate:
            text = candidate
            resp.encoding = "utf-8"
    return Window(resp.url or url, text)


def text_of(el: Any) -> str:
    """`(el.textContent || '').trim()`：兼容 bs4 Tag 与裸字符串。"""
    if el is None:
        return ""
    getter = getattr(el, "get_text", None)
    if callable(getter):
        return getter().strip()
    return str(el).strip()


def attr_of(el: Any, name: str) -> str:
    """`(el.getAttribute(name) || '').trim()`"""
    if el is None:
        return ""
    getter = getattr(el, "get", None)
    if callable(getter):
        val = getter(name)
        if isinstance(val, list):          # class 这类多值属性
            val = " ".join(val)
        return (val or "").strip()
    return ""
