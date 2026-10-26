"""wsyzy.py —— 无损云直连（wsyzyDirect）。

    suggest_search(kw)      联想搜索
    rank_candidates(...)    候选排序
    get_detail(hit)         取详情与选集
    cur_ep_num(...)         定位当前集
    play(...)               取 m3u8 并播放
    abort()                 中止

流程：搜索 → 排序 → 详情 → 选集 → 播放。「播放」这一步把
`PLAYER + encodeURIComponent(m3u8)` 交给选定的播放后端。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from . import net
from .config import Config
from .net import WsyzyError
from .page import Window
from .player import PLAYER, FrameLayout, PlayerBackend, Rect, build_player_frame_layout
from .title import wait_stable_title, wsyzy_clean_title
from .ui import Ui
from .util import (
    FindEleTimeout,
    PageSession,
    find_target_ele,
    hide_nodes,
    mute_page_media,
    remove_video,
    set_interval,
)

SITE = "https://wsyzy.cc"
API = "https://api.wsyzy.net/api.php/provide/vod/"

_EP_URL_RE = re.compile(r"^https?://")
_EP_NUM_IN_NAME_RE = re.compile(r"\d{1,8}")

# curEpNum 的四条匹配规则（顺序即优先级）
_CUR_EP_QUERY_RE = re.compile(r"[?&](?:ep|episode|p|e|cur)=(\d{1,5})(?!\d)", re.IGNORECASE)
_CUR_EP_QQ_RE = re.compile(r"/p(\d{1,5})\.html", re.IGNORECASE)
_CUR_EP_BILI_RE = re.compile(r"/ep(\d{1,5})(?!\d)", re.IGNORECASE)
_CUR_EP_TITLE_RE = re.compile(r"第?\s*(\d{1,8})\s*[集期话]")


@dataclass
class PlayResult:
    """play() 的返回值：CLI 交互循环靠它换集/重播。"""

    title: str
    hit: Dict[str, Any]
    episodes: List[Dict[str, str]]
    index: int
    backend: PlayerBackend
    player_url: str = ""
    embed_page: Optional[Any] = None

    def episode(self, idx: Optional[int] = None) -> Dict[str, str]:
        i = self.index if idx is None else idx
        return self.episodes[max(0, min(i, len(self.episodes) - 1))]


@dataclass
class TakeoverUi:
    """`takeover()` resolve 出来的那个对象：
    `{iframe, epBar, wrapper, container, setStatus, hidePlaceholder, askChoice}`。

    iframe/epBar/wrapper 在 Python 里没有实体，改为记录「本该渲染成什么」
    （layout + wrapper 类名），三个回调转发到终端 Ui。
    """

    container: Any
    layout: FrameLayout
    wrapper_class: str
    ui: Ui
    iframe_src: str = ""
    ep_bar_mounted: bool = False

    def set_status(self, text: str) -> None:
        """`setStatus: (t) => { placeholder.textContent = t; placeholder.style.display = "flex"; }`"""
        self.ui.set_status(text)

    def hide_placeholder(self) -> None:
        """`hidePlaceholder: () => { placeholder.style.display = "none"; }`"""
        self.ui.hide_status()

    def ask_choice(
        self, candidates: List[Dict[str, Any]], detail_map: Dict[Any, Dict[str, Any]]
    ) -> Dict[str, Any]:
        return self.ui.ask_choice(candidates, detail_map)


class WsyzyDirect:
    """无损云直连模块。一个实例对应一次 `wsyzyDirect.play()` 会话。"""

    SITE = SITE
    API = API
    PLAYER = PLAYER

    def __init__(
        self,
        cfg: Config,
        page: PageSession,
        ui: Optional[Ui] = None,
        backend: Optional[PlayerBackend] = None,
    ):
        self.cfg = cfg
        self.page = page
        self.ui = ui or Ui()
        self.backend = backend
        self._running = False
        self._aborted = False
        self._takeover_timeout = False
        self._stop_suppressor: Optional[Callable[[], None]] = None
        self._lock = threading.RLock()

    # ── 状态位（对应模块级 let _running / _aborted / _takeoverTimeout）─────
    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def running(self) -> bool:
        return self._running

    def _is_aborted(self) -> bool:
        return self._aborted

    # ── 搜索 ──────────────────────────────────────────────────────────────
    def search_list(self, title: str) -> List[Dict[str, Any]]:
        """`searchList(title)`：官网联想接口，过滤掉缺 id/name 的项。"""
        url = f"{SITE}/index.php/ajax/suggest?mid=1&wd={quote(title, safe='')}"
        j = net.req_json(url)
        items = (j or {}).get("list") or []
        return [x for x in items if x.get("id") and x.get("name")]

    def search_candidates(self, title: str) -> List[Dict[str, Any]]:
        """`searchCandidates(title)`：官网联想偶发抖动，空结果自动重试一次。"""
        items: List[Dict[str, Any]] = []
        try:
            items = self.search_list(title)
        except WsyzyError:
            pass
        if not items:
            time.sleep(0.6)
            if self._aborted:
                return []
            try:
                items = self.search_list(title)
            except WsyzyError:
                pass
        return items

    def rank_candidates(
        self, items: List[Dict[str, Any]], title: str
    ) -> List[Dict[str, Any]]:
        """`rankCandidates(list, title)`：精确 > 前缀 > 包含 > 反向包含 > 其他。

        单字片名不做反向包含，避免播错片。
        打分写回 `_score` 字段，后续「首候选是否精确命中」的判定要用。
        """

        def score(x: Dict[str, Any]) -> int:
            name = x.get("name") or ""
            if name == title:
                return 4
            if name.startswith(title):
                return 3
            if title in name:
                return 2
            if len(title) >= 2 and name and name in title:
                return 1
            return 0

        # sorted 是稳定排序，同分候选保持原有顺序
        ordered = sorted(items, key=lambda x: -score(x))
        for x in ordered:
            x["_score"] = score(x)
        return ordered

    def search_with_retry(self, title: str) -> List[Dict[str, Any]]:
        """`searchWithRetry(title)`：首搜失败后用清洗过的关键词自动重搜。"""
        items = self.search_candidates(title)
        used_title = title
        if not items and not self._aborted:
            alt = wsyzy_clean_title(title)
            if alt and alt != title and len(alt) >= 2:
                self.ui.toast(f"改用「{alt}」重新搜索...", False)
                used_title = alt
                items = self.search_candidates(alt)
        return self.rank_candidates(items, used_title)

    # ── 详情 / 选集 ───────────────────────────────────────────────────────
    @staticmethod
    def parse_eps(vod_play_url: Optional[str]) -> List[Dict[str, str]]:
        """`parseEps(pu)`：`名$url#名$url` → [{name, url}]，只保留 http(s) 地址。

        `$$$` 分隔多条播放线路，只取第一条（`split('$$$')[0]`）。
        """
        if not vod_play_url:
            return []
        eps: List[Dict[str, str]] = []
        for seg in vod_play_url.split("$$$")[0].split("#"):
            i = seg.find("$")
            ep = (
                {"name": seg[:i], "url": seg[i + 1 :]}
                if i > 0
                else {"name": "", "url": seg}
            )
            if _EP_URL_RE.match(ep["url"]):
                eps.append(ep)
        return eps

    def get_episodes(self, vod_id: Any) -> List[Dict[str, str]]:
        """`getEpisodes(id)`"""
        j = net.req_json(f"{API}?ac=detail&ids={vod_id}")
        lst = (j or {}).get("list") or []
        v = lst[0] if lst else None
        return self.parse_eps(v.get("vod_play_url") if v else None)

    def get_details(self, ids: List[Any]) -> Dict[Any, Dict[str, Any]]:
        """`?ac=detail&ids=1,2,3` 一次拉全，返回 {vod_id: detail}。"""
        j = net.req_json(f"{API}?ac=detail&ids={','.join(str(i) for i in ids)}")
        out: Dict[Any, Dict[str, Any]] = {}
        for d in (j or {}).get("list") or []:
            out[d.get("vod_id")] = d
        return out

    def cur_ep_num(self) -> int:
        """`curEpNum()`：从 URL 与页面标题里推断「当前是第几集」，取不到返回 0。"""
        win: Optional[Window] = self.page.window
        href = win.location.href if win else self.page.url
        doc_title = win.document.title if win else ""

        m = _CUR_EP_QUERY_RE.search(href)          # ?ep=X / ?episode=X / ?p=X / ?e=X / ?cur=X
        if m:
            return int(m.group(1))
        m = _CUR_EP_QQ_RE.search(href)             # 腾讯：/pN.html
        if m:
            return int(m.group(1))
        m = _CUR_EP_BILI_RE.search(href)           # B站/芒果：/epN
        if m:
            return int(m.group(1))
        m = _CUR_EP_TITLE_RE.search(f"{doc_title} {href}")   # 标题里的"第X集/期/话"
        if m:
            return int(m.group(1))
        return 0

    # ── 广告声抑制器 ──────────────────────────────────────────────────────
    def start_ad_sound_suppressor(self) -> None:
        """`startAdSoundSuppressor()`：直连模式期间持续压制页面自带媒体。

        浏览器版靠 play/playing/volumechange 事件 + 400ms 轮询给 video/audio 静音；
        Python 的页面是一次性快照，没有事件，于是保留 400ms 轮询这一半：
        每次快照被重新抓取后（wait_stable_title 会触发），新出现的媒体节点
        会被摘掉 src/source，等价于「不会再出声」。
        """
        with self._lock:
            if self._stop_suppressor is not None:
                return

            def tick() -> None:
                doc = self.page.document
                if doc is None:                    # 页面已不可用 → 自行退出
                    self.stop_ad_sound_suppressor()
                    return
                mute_page_media(doc.soup)

            repeater = set_interval(tick, 400)

            def stop() -> None:
                repeater.stop()
                with self._lock:
                    self._stop_suppressor = None

            self._stop_suppressor = stop

    def stop_ad_sound_suppressor(self) -> None:
        """`stopAdSoundSuppressor()`"""
        with self._lock:
            stop = self._stop_suppressor
        if stop is not None:
            stop()

    # ── 接管播放器容器 ────────────────────────────────────────────────────
    def takeover(self, timeout: float = 10000) -> TakeoverUi:
        """`takeover()` + play() 里那个 10 秒竞速。

        完整流程：找到官方播放器容器 → 隐藏 VIP 遮罩（每 500ms 复查）→ 清空容器
        → 塞进 wrapper/iframe/epBar/placeholder。
        这里：容器只用于「确认站点支持 + 算 iframe 布局」，真正的画面交给
        播放后端，所以不做 `$(container).empty()`（那会毁掉后续还要读的快照）；
        遮罩隐藏与媒体清理照做，因为它们对应内嵌播放页的去广告语义。
        """
        node = self.cfg.currentPlayerNode
        if node is None:
            raise WsyzyError("未找到官方播放器位置")

        box: Dict[str, Any] = {}

        def run() -> None:
            try:
                box["container"] = find_target_ele(
                    self.page, node.container, should_abort=self._is_aborted
                )
            except Exception as exc:               # FindEleTimeout / 抓取失败
                box["err"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout / 1000.0)

        if worker.is_alive():
            # 10s 超时：置 _takeoverTimeout / _aborted 并 reject
            self._takeover_timeout = True
            self._aborted = True
            raise WsyzyError("未找到官方播放器位置")
        if "err" in box:
            raise box["err"]
        if self._aborted:
            raise WsyzyError("已取消")

        container = box.get("container")
        doc = self.page.document

        # cleanup()：隐藏 displayNodes + cleanupNodes，并每 500ms 复查一次
        cleanup_selectors = self.cfg.cleanup_selectors()

        def cleanup() -> None:
            hide_nodes(self.page.document, cleanup_selectors)

        cleanup()
        if self.cfg.cleanupTimer is not None:
            self.cfg.cleanupTimer.stop()           # clearInterval
        self.cfg.cleanupTimer = set_interval(cleanup, 500)
        if not self.cfg.wsyzyFsbBound:
            # document.addEventListener("fullscreenchange", cleanup)
            # 终端没有全屏事件，绑定标记保留以对齐状态机
            self.cfg.wsyzyFsbBound = True

        win = self.page.window
        layout = build_player_frame_layout(
            is_mobile=bool(self.cfg.isMobile),
            container_rect=Rect(),                 # getBoundingClientRect 无对应物 → 0
            container_style={"paddingTop": 0},     # getComputedStyle 同上
            viewport_height=(win.innerHeight if win else 0) or 0,
        )

        remove_video(self.page)                    # util.reomveVideo()

        return TakeoverUi(
            container=container,
            layout=layout,
            wrapper_class=self.cfg.iframeWrapperClass,
            ui=self.ui,
        )

    # ── 主流程 ────────────────────────────────────────────────────────────
    def play(self) -> Optional[PlayResult]:
        """`wsyzyDirect.play()`。失败时返回 None（错误已 toast），与
        「catch 里 toast + setStatus，不向外抛」行为一致。"""
        if self._running:
            self.ui.toast("正在加载中，请稍候...")
            return None

        self._running = True
        self._aborted = False
        self._takeover_timeout = False
        ui_box: Optional[TakeoverUi] = None

        try:
            # 第一步：启动广告声抑制（只静音，不 pause/不删 src，不干扰页面 JS）
            self.start_ad_sound_suppressor()

            ui_box = self.takeover()
            if self._aborted:
                raise WsyzyError("已取消")

            # 第二步：等待官方页面把真实剧名写入标题（SPA站点需数秒~数十秒）
            title = wait_stable_title(
                self.page,
                on_tick=lambda sec, cur: ui_box.set_status(
                    f"「{cur}」等待片名稳定 {sec}s..." if cur else f"等待片名稳定 {sec}s..."
                ),
                should_abort=self._is_aborted,
            )
            if self._aborted:
                raise WsyzyError("已取消")
            if not title or len(title) < 2:
                raise WsyzyError("无法识别片名")

            ui_box.set_status(f"「{title}」无损云搜索中...")
            self.ui.toast(f"「{title}」无损云搜索中...", False)

            candidates = self.search_with_retry(title)
            if self._aborted:
                raise WsyzyError("已取消")
            if not candidates:
                raise WsyzyError(f"无损云未收录「{title}」，请切换其他解析源")

            hit: Optional[Dict[str, Any]] = None
            if len(candidates) == 1:
                hit = candidates[0]
            else:
                # 详情一次拉全：集数判定与选择面板共用；失败不阻塞，降级为仅名称选择
                ui_box.set_status(f"搜到 {len(candidates)} 个结果，正在加载详情...")
                detail_map: Dict[Any, Dict[str, Any]] = {}
                try:
                    detail_map = self.get_details([c["id"] for c in candidates])
                except WsyzyError:
                    pass
                if self._aborted:
                    raise WsyzyError("已取消")

                ep_num = self.cur_ep_num()
                if ep_num > 0:
                    # 硬判定：当前集数只有唯一一个候选的集数足够 → 必然是它
                    viable = [
                        c
                        for c in candidates
                        if detail_map.get(c["id"])
                        and len(self.parse_eps(detail_map[c["id"]].get("vod_play_url")))
                        >= ep_num
                    ]
                    if len(viable) == 1:
                        hit = viable[0]

                if (
                    hit is None
                    and candidates[0].get("_score") == 4
                    and (len(candidates) < 2 or (candidates[1].get("_score") or 0) < 3)
                ):
                    # 名称信号：首候选与搜索标题精确一致，且无前缀竞争者
                    hit = candidates[0]

                if hit is None:
                    self.ui.toast(f"搜到 {len(candidates)} 个结果，请选择要播放的", False)
                    hit = ui_box.ask_choice(candidates, detail_map)
                    if self._aborted:
                        raise WsyzyError("已取消")

            ui_box.set_status(f"命中「{hit['name']}」，获取选集...")
            self.ui.toast(f"命中「{hit['name']}」，获取选集...", False)

            eps = self.get_episodes(hit["id"])
            if self._aborted:
                raise WsyzyError("已取消")
            if not eps:
                raise WsyzyError("未取到播放地址，请切换其他解析源")

            idx = 0
            num = self.cur_ep_num()
            if num > 0:
                for i, ep in enumerate(eps):
                    m = _EP_NUM_IN_NAME_RE.search(ep["name"])
                    if m and int(m.group(0)) == num:
                        idx = i
                        break
            self.ui.toast(f"共{len(eps)}集，正在加载播放器...", False)

            backend = self._ensure_backend()
            # load(ep)：未中止则把 iframe.src 设为 PLAYER + encodeURIComponent(ep.url)
            playlist_setter = getattr(backend, "set_playlist", None)
            if callable(playlist_setter):
                playlist_setter(eps, idx, hit["name"])

            def load(ep: Dict[str, str]) -> None:
                if not self._aborted:
                    backend.play(ep["url"], title=hit["name"])

            load(eps[idx])
            ui_box.hide_placeholder()
            ui_box.ep_bar_mounted = True
            self.ui.ep_bar(eps, idx, load, title=hit["name"])          # mountEpBar(...)

            self.ui.toast(
                "✓ 无损云播放中（点右上角「☰ 选集」可换集）"
                if self.cfg.isMobile
                else f"✓ 无损云播放中（{backend.describe()}）"
            )

            ui_box.iframe_src = PLAYER + quote(eps[idx]["url"], safe="")
            return PlayResult(
                title=title,
                hit=hit,
                episodes=eps,
                index=idx,
                backend=backend,
                player_url=ui_box.iframe_src,
                embed_page=getattr(backend, "page_path", None),
            )

        except KeyboardInterrupt:
            self.ui.toast("✗ 已取消")
            self.stop_ad_sound_suppressor()
            return None
        except (WsyzyError, FindEleTimeout, net.WsyzyError) as exc:
            msg = str(exc)
            if self._aborted and not self._takeover_timeout:
                pass                                # console.log('[无损云直连] 流程已取消')
            else:
                self.ui.toast("✗ " + msg)
                if ui_box is not None:
                    ui_box.set_status("✗ " + msg)
                # 失败时清理 cleanupTimer，停止持续隐藏官方播放器元素
                if not self._aborted and self.cfg.cleanupTimer is not None:
                    self.cfg.cleanupTimer.stop()
                    self.cfg.cleanupTimer = None
            self.stop_ad_sound_suppressor()
            return None
        except Exception as exc:                    # 未预期的异常也要还原状态
            self.ui.toast(f"✗ {exc!r}")
            self.stop_ad_sound_suppressor()
            return None
        finally:
            self._running = False

    def _ensure_backend(self) -> PlayerBackend:
        if self.backend is not None:
            return self.backend
        from .player import detect_backend           # 延迟 import，避免构造期探测

        self.backend = detect_backend(self.cfg)
        return self.backend

    # ── 中止 ──────────────────────────────────────────────────────────────
    def stop(self) -> None:
        """`abort()`：置 _aborted、停抑制器、清 cleanupTimer。"""
        self._aborted = True
        self.stop_ad_sound_suppressor()
        if self.cfg.cleanupTimer is not None:
            self.cfg.cleanupTimer.stop()
            self.cfg.cleanupTimer = None

    abort = stop
