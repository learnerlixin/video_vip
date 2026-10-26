"""util.py —— DOM 工具 + 页面会话。

    find_target_ele(doc, selector)   在多种节点类型上统一做选择器查询
    remove_video(doc)                停掉页面里的媒体
    url_change_reload(session, ...)  页面换了就重抓
    cleanup(doc, node)               去广告遮罩

另外提供 `PageSession`：「当前标签页」——持有 URL、缓存抓下来的 Window，
需要时重新抓取，用来等 SPA 把播放器渲染出来。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional

from .net import WsyzyError
from .page import Window, fetch


class FindEleTimeout(Exception):
    """`new Error('findTargetEle timeout: ' + selector)`"""

    def __init__(self, selector: str):
        super().__init__("findTargetEle timeout: " + selector)
        self.selector = selector


# ─────────────────────────────────────────────────────────────────────────────
# setInterval / clearInterval
# ─────────────────────────────────────────────────────────────────────────────
class Repeater:
    """`setInterval(fn, ms)` 的等价物：后台守护线程按固定间隔调用 fn。"""

    def __init__(self, fn: Callable[[], None], interval_ms: float):
        self._fn = fn
        self._interval = max(interval_ms, 1.0) / 1000.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Repeater":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._fn()
            except Exception:
                # 回调抛异常不终止定时器（setInterval 语义），这里同样吞掉
                pass

    def stop(self) -> None:
        """clearInterval"""
        self._stop.set()
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()


def set_interval(fn: Callable[[], None], interval_ms: float) -> Repeater:
    return Repeater(fn, interval_ms).start()


# ─────────────────────────────────────────────────────────────────────────────
# PageSession：「当前标签页」
# ─────────────────────────────────────────────────────────────────────────────
class PageSession:
    """持有 URL 与最近一次抓取的 Window 快照。

    * `window`      —— 当前 document（抓不到时为 None，例如离线/被墙）
    * `reload()`    —— `window.location.reload()`：重新抓一次
    * `refresh_if_stale(max_age)` —— 快照超过 max_age 秒才重抓，供轮询用
    """

    def __init__(self, url: str, user_agent: Optional[str] = None, window: Optional[Window] = None):
        self.url = url
        self._ua = user_agent
        self._window: Optional[Window] = window
        self._fetched_at: float = time.time() if window is not None else 0.0
        self._lock = threading.RLock()
        self.fetch_error: Optional[str] = None

    @property
    def window(self) -> Optional[Window]:
        return self._window

    @property
    def document(self) -> Optional[Any]:
        return self._window.document if self._window else None

    @property
    def location(self) -> Optional[Any]:
        return self._window.location if self._window else None

    def load(self) -> Optional[Window]:
        """首次抓取（失败不抛，记录到 fetch_error，返回 None）。"""
        with self._lock:
            if self._window is not None:
                return self._window
            return self.reload()

    def reload(self) -> Optional[Window]:
        with self._lock:
            try:
                headers = {"User-Agent": self._ua} if self._ua else None
                self._window = fetch(self.url, headers=headers)
                self.url = self._window.location.href
                self.fetch_error = None
            except WsyzyError as exc:
                self.fetch_error = str(exc)
            except Exception as exc:  # 解析异常等
                self.fetch_error = repr(exc)
            self._fetched_at = time.time()
            return self._window

    def refresh_if_stale(self, max_age: float = 2.0) -> Optional[Window]:
        with self._lock:
            if self._window is None or (time.time() - self._fetched_at) >= max_age:
                return self.reload()
            return self._window

    @property
    def age(self) -> float:
        return time.time() - self._fetched_at if self._fetched_at else float("inf")


# ─────────────────────────────────────────────────────────────────────────────
# util.findTargetEle
# ─────────────────────────────────────────────────────────────────────────────
def find_target_ele(
    page: PageSession,
    selector: str,
    max_try_time: int = 120,
    interval_ms: float = 500,
    refetch_every: int = 4,
    should_abort: Optional[Callable[[], bool]] = None,
) -> Any:
    """`util.findTargetEle(selector)`：轮询直到选择器命中，超时抛 FindEleTimeout。

    DOM 会自己变（SPA 异步渲染），所以只需反复 querySelector；
    Python 的快照是死的，因此每 `refetch_every` 次轮询重新抓一次页面
    （默认 2 秒一次），其余次数在现有快照上查，避免把站点打爆。
    `should_abort` 对应各处 `if (_aborted) ...` 的中断检查。
    """
    if selector == "body":
        page.load()
        body = page.document.body if page.document else None
        if body is not None:
            return body

    for try_time in range(max_try_time):
        if should_abort and should_abort():
            raise FindEleTimeout(selector)
        win = page.refresh_if_stale(max_age=(interval_ms * refetch_every) / 1000.0)
        doc = win.document if win is not None else None
        if doc is not None:
            el = doc.query_selector(selector)
            if el is not None:
                return el
        if try_time + 1 >= max_try_time:
            break
        time.sleep(interval_ms / 1000.0)
    raise FindEleTimeout(selector)


# ─────────────────────────────────────────────────────────────────────────────
# util.reomveVideo（函数名拼写如此，保留同名别名）
# ─────────────────────────────────────────────────────────────────────────────
_media_cleaner_started = False


def _stop_media(media: Any) -> None:
    """`stopMedia(media)`：pause + 删 src；快照上没有可播放的媒体，
    这里只把 src / autoplay 属性摘掉，语义等价（让这个节点不再指向视频源）。"""
    try:
        for attr in ("src", "autoplay"):
            if media.has_attr(attr):
                del media[attr]
        for node in media.find_all("source"):
            node.decompose()
    except Exception:
        pass


def mute_page_media(root: Any) -> int:
    """`mutePageMedia(root)`：处理 root 下所有 video/audio，返回处理数量。"""
    if root is None or not hasattr(root, "find_all"):
        return 0
    count = 0
    for media in root.find_all(["video", "audio"]):
        _stop_media(media)
        count += 1
    return count


def remove_video(page: PageSession) -> int:
    """`util.reomveVideo()`：清掉页面自带的 video/audio。

    完整实现还会 hook `HTMLMediaElement.prototype.play` 并挂 MutationObserver
    持续压制新出现的媒体；Python 的快照是一次性的，没有后续 mutation，
    所以只保留「扫一遍并清空 src」这一步，并返回被处理的媒体数量。
    """
    global _media_cleaner_started
    _media_cleaner_started = True
    doc = page.document
    return mute_page_media(doc.soup if doc is not None else None)


reomve_video = remove_video      # 拼写保持一致的别名


def media_cleaner_started() -> bool:
    return _media_cleaner_started


# ─────────────────────────────────────────────────────────────────────────────
# util.urlChangeReload
# ─────────────────────────────────────────────────────────────────────────────
def url_change_reload(page: PageSession, interval_ms: float = 1000) -> Repeater:
    """`util.urlChangeReload()`：URL 变了就 reload。

    监听 SPA 的 history 变化；这里改成定时重抓，比较最终 URL
    （含重定向），不一致就把快照换成新的——即「页面换了就重新加载」。
    """
    state = {"old": page.url}

    def tick() -> None:
        old = state["old"]
        win = page.reload()
        new = win.location.href if win else old
        if old != new:
            state["old"] = new

    return set_interval(tick, interval_ms)


def hide_nodes(doc: Any, selectors: List[str]) -> int:
    """`cleanup()`：把 displayNodes/cleanupNodes 命中的节点
    隐藏（display:none !important; opacity:0; pointer-events:none）。

    快照上改样式不会影响真实页面，但这个函数是内嵌 HTML 后端生成播放页时
    真正要用的「去广告遮罩」步骤，也是 preHandle/takeover 的对应实现。
    返回被处理的节点数。
    """
    if doc is None:
        return 0
    hidden = 0
    for selector in selectors:
        for node in doc.query_selector_all(selector):
            try:
                style = node.get("style") or ""
                node["style"] = (
                    style.rstrip(";") + ";" if style and not style.endswith(";") else style
                ) + "display:none !important;opacity:0 !important;pointer-events:none !important;"
                hidden += 1
            except Exception:
                continue
    return hidden
