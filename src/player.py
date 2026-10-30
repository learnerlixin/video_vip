"""player.py —— 播放器后端 + 播放页 DOM/样式构建。

「播放」最终要落到一个能渲染 m3u8 的执行者上（本地播放器 / 浏览器 / 自有播放页），
于是拆成两层：

1) 布局与 DOM 构建
   build_player_frame_layout()  ← buildPlayerFrameLayout()
   apply_inline_styles()        ← applyInlineStyles()
   render_embed_page()          ← takeover() 里那一串 createElement/appendChild，
                                   输出等价的内嵌播放页 HTML（wrapper + iframe + epBar）

2) 播放后端（挑一个执行者来渲染）
   ProcessBackend              —— 本地进程直接播 m3u8（make_mpv() / make_ffplay() 造出）
   PrefetchBackend             —— 并发预取分片到本地，再用本地播放器起播（弱网/卡顿时用）
   BrowserBackend              —— 用系统浏览器打开 PLAYER+url（等价于 iframe.src）
   EmbedBackend                —— 起本地 HTTP 服务托管自有播放页（hls.js 直接播 m3u8），
                                  页面把播放事件回传给终端；
                                  服务起不来时退回 file:// 页面 + 第三方 iframe

detect_backend() 按 mpv → ffplay → embed → browser 的顺序挑第一个可用的；
prefetch 不参与自动探测，需显式 --player prefetch（它会把整集下到本地，开销大）。
所有落盘产物统一放在 CACHE_ROOT（%TEMP%\video_vip\）下，见该常量注释。
"""

from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from . import gm
from .config import Config
from .embedsrv import HLSJS_CDNS, EmbedServer, ensure_hlsjs, render_page
from .prefetch import human_duration, human_size

# 三个常量（PLAYER 也在这里复用，避免循环 import）
PLAYER = "https://wsyzy.vip/m3u8/?url="

# ── 统一缓存根 ────────────────────────────────────────────────────────────────
# 所有落盘产物都收在这一个目录下，方便用户查看与手动清理：
#     %TEMP%\video_vip\
#         embed\player.html              内嵌播放页（EmbedBackend 的 file:// 兜底）
#         hls.min.js                     hls.js 本地缓存（embed 播放页用）
#         prefetch\<hash>_<name>\...     预取的 HLS 分片（PrefetchBackend）
# 之前 embed 用 video_vip_py、prefetch 用 video_vip_cache，两个目录散在 temp 下，
# 不好找也不好清，所以合并。
CACHE_ROOT = Path(tempfile.gettempdir()) / "video_vip"
EMBED_DIR = CACHE_ROOT / "embed"
HLSJS_CACHE = CACHE_ROOT / "hls.min.js"
PREFETCH_ROOT = CACHE_ROOT / "prefetch"


# ─────────────────────────────────────────────────────────────────────────────
# buildPlayerFrameLayout
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Rect:
    """`container.getBoundingClientRect()` 的替身（Python 里没有真实布局）。"""

    width: float = 0.0
    height: float = 0.0
    left: float = 0.0
    top: float = 0.0


@dataclass
class FrameLayout:
    containerStyles: Dict[str, str] = field(default_factory=dict)
    wrapperStyles: Dict[str, str] = field(default_factory=dict)
    iframeStyles: Dict[str, str] = field(default_factory=dict)


# parseFloat 语义：解析字符串开头的数字部分并忽略后面的单位（"211px" → 211），
# Python 的 float() 则会直接抛异常，所以这里用正则复刻前缀解析语义。
_FLOAT_PREFIX_RE = re.compile(
    r"\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
)


def _parse_pixel_value(value: Any) -> float:
    """`parsePixelValue`：Number.parseFloat + Number.isFinite 判定。"""
    if value is None:
        return 0.0
    m = _FLOAT_PREFIX_RE.match(str(value))
    if m is None:
        return 0.0
    try:
        parsed = float(m.group(0))
    except ValueError:                # pragma: no cover - 正则已保证可解析
        return 0.0
    return parsed if parsed == parsed and abs(parsed) != float("inf") else 0.0


def _js_number_str(value: float) -> str:
    """模板字符串里的数字格式：整数值不带小数点（211 → "211"，211.5 → "211.5"）。"""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _js_round(value: float) -> int:
    """`Math.round`：四舍五入且 .5 一律向上（Python 的 round 是银行家舍入，会差 1）。"""
    return math.floor(value + 0.5)


def build_player_frame_layout(
    is_mobile: bool,
    container_rect: Optional[Rect] = None,
    container_style: Optional[Dict[str, Any]] = None,
    viewport_height: float = 0,
) -> FrameLayout:
    """`buildPlayerFrameLayout({isMobile, containerRect, containerStyle, viewportHeight})`

    桌面端返回绝对定位铺满容器的样式；移动端按 16:9 与视口高度算出
    minHeight / aspectRatio。
    """
    container_rect = container_rect or Rect()
    container_style = container_style or {}

    if not is_mobile:
        return FrameLayout(
            containerStyles={"overflow": "hidden"},
            wrapperStyles={
                "position": "absolute",
                "top": "0",
                "left": "0",
                "width": "100%",
                "height": "100%",
                "background": "#000",
                "overflow": "hidden",
                "zIndex": "2147483646",
            },
            iframeStyles={
                "width": "100%",
                "height": "100%",
                "border": "none",
                "display": "block",
                "background": "#000",
            },
        )

    width = _parse_pixel_value(container_rect.width)
    height = _parse_pixel_value(container_rect.height)
    padding_top = _parse_pixel_value(container_style.get("paddingTop", 0))
    ratio_height = _js_round((width * 9) / 16) if width > 0 else 0
    fallback_viewport_height = _js_round(viewport_height * 0.32) if viewport_height > 0 else 180
    raw_height = height or padding_top or ratio_height or fallback_viewport_height
    max_height = (
        max(220, _js_round(viewport_height * 0.7)) if viewport_height > 0 else raw_height
    )
    resolved_height = max(180, min(raw_height, max_height))
    uses_padding_aspect = width > 0 and padding_top > 0 and (padding_top / width) > 0.25
    min_height_css = f"{_js_number_str(resolved_height)}px"

    container_styles: Dict[str, str] = {
        "overflow": "hidden",
        "height": "auto",
        "minHeight": min_height_css,
    }
    if uses_padding_aspect:
        container_styles["paddingTop"] = "0"

    return FrameLayout(
        containerStyles=container_styles,
        wrapperStyles={
            "position": "relative",
            "display": "block",
            "width": "100%",
            "minHeight": min_height_css,
            "aspectRatio": "16 / 9",
            "background": "#000",
            "overflow": "hidden",
            "zIndex": "2147483646",
        },
        iframeStyles={
            "position": "absolute",
            "inset": "0",
            "width": "100%",
            "height": "100%",
            "border": "none",
            "display": "block",
            "background": "#000",
        },
    )


_CAMEL_TO_KEBAB_CACHE: Dict[str, str] = {}


def _camel_to_kebab(name: str) -> str:
    """驼峰样式名 → CSS 短横线名（`zIndex` → `z-index`）。"""
    cached = _CAMEL_TO_KEBAB_CACHE.get(name)
    if cached is None:
        cached = "".join("-" + ch.lower() if ch.isupper() else ch for ch in name)
        _CAMEL_TO_KEBAB_CACHE[name] = cached
    return cached


def styles_to_css(styles: Optional[Dict[str, str]]) -> str:
    """把样式字典序列化成 CSS 内联串。"""
    if not styles:
        return ""
    return ";".join(
        f"{_camel_to_kebab(k)}:{v}" for k, v in styles.items() if v not in (None, "")
    )


def apply_inline_styles(element: Any, styles: Optional[Dict[str, str]]) -> None:
    """`applyInlineStyles(element, styles)`。

    element 可以是 bs4 Tag（写进 style 属性），也可以是普通对象
    （写进 element.style 字典，供测试/HTML 生成使用）。
    空值跳过（undefined / null / 空串都不输出）。
    """
    if element is None:
        return
    for name, value in (styles or {}).items():
        if value is None or value == "":
            continue
        style_attr = getattr(element, "style", None)
        if isinstance(style_attr, dict):
            style_attr[name] = value
            continue
        if hasattr(element, "__setitem__") and hasattr(element, "get"):   # bs4 Tag
            old = element.get("style") or ""
            sep = "" if old.endswith(";") or not old else ";"
            element["style"] = f"{old}{sep}{_camel_to_kebab(name)}:{value}"


# ─────────────────────────────────────────────────────────────────────────────
# 播放后端
# ─────────────────────────────────────────────────────────────────────────────
class PlayerBackend:
    """播放后端接口：play(m3u8_url) 起播，stop() 停掉，is_alive() 查状态。

    另有几个**可选**协议方法，基类故意不定义（CLI 用 getattr 探测，
    只有需要的后端才实现）：
        set_playlist(eps, idx, title)  把选集表交给后端（页面选集栏）
        take_picked()   -> int|None    页面选集栏里点了哪一集（CLI 轮询取走）
        take_notices()  -> [str]       页面回传的一次性提示（CLI 轮询打印）
    """

    name = "base"
    label = "基类"
    # 能否播放「HTML 解析页」。第三方解析源（LX/七哥/TXNQ/789/冰豆/麒麟）返回的是
    # 靠 JS 动态取流的网页，实测 ffplay/mpv 一律 I/O error；只有浏览器类后端能渲染。
    # show_player_window 据此决定把解析页交给谁。
    plays_html = False

    def play(self, url: str, title: str = "") -> None:
        raise NotImplementedError

    def stop(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def wait(self) -> None:
        """阻塞直到播放结束（终端后端用于「播完自动退出」）。"""
        return None

    def describe(self) -> str:
        return self.label


class ProcessBackend(PlayerBackend):
    """本地播放器进程（mpv / ffplay）的公共实现。"""

    def __init__(self, exe: str, args_template: List[str], label: str):
        self.exe = exe
        self.args_template = args_template
        self.label = label
        self.name = Path(exe).stem
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()

    def _build_args(self, url: str, title: str) -> List[str]:
        args = [self.exe]
        for token in self.args_template:
            if token == "{url}":
                args.append(url)
            elif token == "{title}":
                args.append(title or "video_vip")
            else:
                args.append(token)
        return args

    def play(self, url: str, title: str = "") -> None:
        with self._lock:
            self.stop()
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                self._build_args(url, title),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass

    def is_alive(self) -> bool:
        with self._lock:
            proc = self._proc
        return bool(proc and proc.poll() is None)

    def wait(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.wait()
            except (OSError, subprocess.SubprocessError):
                pass

    def describe(self) -> str:
        # 只留可执行文件名：完整路径（WinGet 装的 ffmpeg 尤其长）会把终端行撑爆
        return f"{self.label}（{Path(self.exe).name}）"


# 媒体流地址特征：这类 URL 需要套上无损云播放页（PLAYER 前缀）才能在浏览器里播；
# 而第三方解析源给的本身就是完整 HTML 播放页，必须原样打开，套前缀会把它变成
# `https://wsyzy.vip/m3u8/?url=https%3A%2F%2Fjx.lxyy.club%2F...` 这种错误地址。
_MEDIA_URL_RE = re.compile(r"\.(m3u8|mpd|mp4|flv|mkv|ts)(\?|#|$)", re.IGNORECASE)


def is_media_url(url: str) -> bool:
    """这个 URL 是裸媒体流（需要 PLAYER 包装）还是现成的播放页（直接用）。"""
    return bool(_MEDIA_URL_RE.search(url or ""))


def player_url_for(url: str, player_prefix: str = PLAYER) -> str:
    """`PLAYER + encodeURIComponent(ep.url)` 的等价实现，但对现成播放页原样返回。"""
    if is_media_url(url):
        return player_prefix + quote(url, safe="")
    return url


class BrowserBackend(PlayerBackend):
    """`iframe.src = PLAYER + encodeURIComponent(ep.url)` 的桌面等价：
    用系统浏览器打开播放页。"""

    name = "browser"
    label = "浏览器（播放页）"
    plays_html = True

    def __init__(self, player_prefix: str = PLAYER):
        self.player_prefix = player_prefix
        self._url: Optional[str] = None

    def play(self, url: str, title: str = "") -> None:
        self._url = player_url_for(url, self.player_prefix)
        gm.open_in_tab(self._url, active=True, insert=True, set_parent=True)

    def is_alive(self) -> bool:
        return self._url is not None

    def describe(self) -> str:
        return self.label


EP_BAR_TEMPLATE = """
<div class="ep-bar" id="epBar">
{buttons}
</div>
"""


def render_embed_page(
    cfg: Config,
    layout: FrameLayout,
    episodes: List[Dict[str, str]],
    cur_idx: int,
    title: str = "",
) -> str:
    """把 `takeover()` + `mountEpBar()` 构建的那棵 DOM 输出成等价 HTML。

    结构：container > wrapper(class=iframeWrapperClass) > iframe + epBar，
    外加移动端才有的「☰ 选集」按钮；epBar 的点击行为（换 iframe.src 并高亮当前项）
    用一小段内联 JS 复刻，样式数值沿用 mountEpBar 里的内联样式。
    """
    iframe_src = PLAYER + quote(episodes[cur_idx]["url"], safe="") if episodes else ""

    buttons: List[str] = []
    for idx, ep in enumerate(episodes):
        label = html.escape(ep.get("name") or f"第{idx + 1}集")
        active = idx == cur_idx
        buttons.append(
            f'<span class="ep{" ep-cur" if active else ""}" data-idx="{idx}" '
            f'data-url="{html.escape(PLAYER + quote(ep["url"], safe=""), quote=True)}" '
            f'onclick="pick(this)">{label}</span>'
        )

    ep_tab = ""
    if cfg.isMobile:
        ep_tab = (
            '<div class="ep-tab" onclick="var b=document.getElementById(\'epBar\');'
            "b.style.display=(b.style.display==='block')?'none':'block'\">☰ 选集</div>"
        )

    wrapper_css = styles_to_css(layout.wrapperStyles)
    iframe_css = styles_to_css(layout.iframeStyles)
    container_css = styles_to_css(layout.containerStyles)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title or 'VIP 解析')}</title>
<style>
  html,body{{margin:0;padding:0;background:#000;height:100%;}}
  #{cfg.vipBoxId}{{position:relative;width:100%;height:100vh;{container_css}}}
  .{cfg.iframeWrapperClass}{{position:relative;width:100%;height:100%;{wrapper_css}}}
  .{cfg.iframeWrapperClass} iframe{{{iframe_css}}}
  .ep-bar{{position:absolute;top:0;right:0;bottom:0;width:170px;overflow-x:hidden;
    overflow-y:auto;z-index:2147483647;display:none;background:rgba(7,24,39,.94);
    padding:8px 6px;box-sizing:border-box;border-radius:10px 0 0 10px;
    border:1px solid rgba(14,165,233,.25);border-right:none;
    box-shadow:-6px 0 16px rgba(0,0,0,.45);}}
  .ep{{display:block;margin:4px 0;padding:5px 10px;font-size:12px;line-height:20px;
    border-radius:6px;cursor:pointer;user-select:none;text-align:center;overflow:hidden;
    white-space:nowrap;text-overflow:ellipsis;color:#bae6fd;background:#0b2942;
    border:1px solid #155e75;font-family:-apple-system,"Microsoft YaHei",sans-serif;}}
  .ep:hover{{background:#0e7490;border-color:#38bdf8;}}
  .ep-cur,.ep-cur:hover{{color:#fff;background:#0369a1;border-color:#38bdf8;
    box-shadow:0 2px 6px rgba(14,165,233,.35);}}
  .ep-trigger{{position:absolute;top:0;right:0;bottom:0;width:10px;
    z-index:2147483645;background:transparent;}}
  .ep-tab{{position:absolute;top:10px;right:10px;z-index:2147483647;padding:6px 12px;
    font-size:13px;line-height:18px;background:rgba(7,24,39,.88);color:#7dd3fc;
    border:1px solid #0ea5e9;border-radius:8px;cursor:pointer;user-select:none;
    box-shadow:0 3px 10px rgba(0,0,0,.35);}}
</style>
</head>
<body>
<div id="{cfg.vipBoxId}">
  <div class="{cfg.iframeWrapperClass}">
    <iframe id="vipFrame" src="{html.escape(iframe_src, quote=True)}" frameborder="0"
            allow="autoplay; encrypted-media; fullscreen" allowfullscreen
            referrerpolicy="no-referrer"></iframe>
    {EP_BAR_TEMPLATE.format(buttons=chr(10).join(buttons))}
    <div class="ep-trigger"
         onmouseenter="document.getElementById('epBar').style.display='block'"
         onmouseleave="document.getElementById('epBar').style.display='none'"></div>
    {ep_tab}
  </div>
</div>
<script>
function pick(el){{
  document.getElementById('vipFrame').src = el.getAttribute('data-url');
  document.querySelectorAll('.ep').forEach(function(n){{n.classList.remove('ep-cur');}});
  el.classList.add('ep-cur');
}}
</script>
</body>
</html>
"""


class EmbedBackend(PlayerBackend):
    """生成内嵌播放页并用浏览器打开。

    有两种形态：

    1. **本地 HTTP 服务 + 自有页面 + hls.js 直接播 m3u8**（默认，`EmbedServer`）
       `<video>` 元素在我们自己的页面里，播放事件（元数据 / 失败 / 回退 /
       选集点击）能回传给终端。
       换集只是改服务端 state（带新 token），页面轮询到就自己切源 ——
       整部剧用一个标签页播完，不会开出一排标签。
    2. **`file://` 页面 + iframe 塞第三方播放页**（服务起不来时的兜底）
       跨域 iframe 是黑盒，既读不到 `<video>` 也收不到任何事件。

    形态 1 里还有一条页面内的回退：hls.js 播不了某个地址（CORS 被拦、
    加密方式不支持、清单 404）时，页面自己换成 iframe 播 `PLAYER + url`。
    画面优先 —— 用户至少能看，Python 侧会收到 `fallback` 事件并明确告知。
    """

    name = "embed"
    label = "浏览器（内嵌播放页，带选集栏）"
    plays_html = True

    def __init__(
        self,
        cfg: Config,
        layout: Optional[FrameLayout] = None,
        out_dir: Optional[Path] = None,
        hlsjs_cache: Optional[Path] = None,
        use_server: bool = True,
        host: str = "127.0.0.1",
    ):
        self.cfg = cfg
        self.layout = layout or build_player_frame_layout(cfg.isMobile, viewport_height=900)
        self.out_dir = Path(out_dir or EMBED_DIR)
        self.hlsjs_cache = Path(hlsjs_cache or HLSJS_CACHE)
        self.use_server = use_server
        self.host = host

        self._episodes: List[Dict[str, str]] = []
        self._cur_idx = 0
        self._title = ""
        self._path: Optional[Path] = None       # file:// 兜底页
        self._server: Optional[EmbedServer] = None
        self._server_failed = False
        self._lock = threading.RLock()
        # 只保护「起本地服务」这一段（见 _ensure_server 注释）
        self._server_lock = threading.RLock()

        self._page_url: Optional[str] = None
        self._mode: str = ""

    # ── 选集表（对应 mountEpBar 的 eps/curIdx）─────────────────────────────
    def set_playlist(self, episodes: List[Dict[str, str]], cur_idx: int, title: str = "") -> None:
        """对应 mountEpBar(epBar, wrapper, load, eps, curIdx)：先把选集表交给后端。"""
        with self._lock:
            self._episodes = list(episodes)
            self._cur_idx = max(0, min(cur_idx, len(episodes) - 1)) if episodes else 0
            if title:
                self._title = title
            server = self._server
            idx, ttl = self._cur_idx, self._title
        # 页面每轮 /state 都会重建选集栏，所以这里推一次就够；
        # 不 bump token → 正在播的画面不受影响。
        if server is not None:
            server.update_playlist(self._episodes_snapshot(), idx, ttl)

    def _episodes_snapshot(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._episodes)

    # ── 本地服务 ──────────────────────────────────────────────────────────
    def _ensure_server(self) -> Optional[EmbedServer]:
        """起本地服务并灌入播放页；起不来（端口被占等）返回 None → 走 file:// 兜底。

        整个「查 → 建 → 存」过程持 `_server_lock`：`play()` 可能被主循环与
        页面选集轮询线程同时调用，不串行化的话会各建一个服务，
        多出来的那个白占一个端口且没人引用（永远关不掉）。
        """
        with self._server_lock:
            with self._lock:
                if self._server is not None and self._server.running:
                    return self._server
                if not self.use_server or self._server_failed:
                    return None
            server = EmbedServer(self.hlsjs_cache, host=self.host)
            if not server.start():
                with self._lock:
                    self._server_failed = True
                return None
            with self._lock:
                page_title = self._title or "VIP 解析"
            server.set_page_html(self._render_server_page(page_title))
            # 预热 hls.js 本地缓存：页面优先用 CDN，这份是离线/CDN 挂掉时的兜底。
            # 放后台线程，别把起播卡在一次下载上。
            threading.Thread(
                target=lambda: ensure_hlsjs(self.hlsjs_cache),
                name="hlsjs-warm", daemon=True,
            ).start()
            with self._lock:
                self._server = server
            return server

    def _render_server_page(self, title: str = "") -> str:
        """自有播放页（hls.js + 选集栏 + 事件回传）。"""
        ep_tab = ""
        if self.cfg.isMobile:
            ep_tab = '<div class="ep-tab" id="epTab">☰ 选集</div>'
        return render_page(
            box_id=self.cfg.vipBoxId,
            wrapper_class=self.cfg.iframeWrapperClass,
            container_css=styles_to_css(self.layout.containerStyles),
            wrapper_css=styles_to_css(self.layout.wrapperStyles),
            iframe_css=styles_to_css(self.layout.iframeStyles),
            ep_tab_html=ep_tab,
            title=html.escape(title or self._title or "VIP 解析"),
            hlsjs_urls=list(HLSJS_CDNS),
        )

    @staticmethod
    def _mode_for(url: str) -> str:
        """这个地址在自有页面里怎么播：hls.js / 浏览器原生 / 只能塞 iframe。"""
        if not is_media_url(url):
            return "iframe"           # 第三方 HTML 解析页：靠 JS 动态取流，只能 iframe
        if re.search(r"\.m3u8(\?|#|$)", url, re.IGNORECASE):
            return "hls"              # hls.js 只认 HLS
        return "direct"               # 裸 mp4 等：浏览器原生就能播

    # ── 播放 ──────────────────────────────────────────────────────────────
    def play(self, url: str, title: str = "") -> None:
        with self._lock:
            if self._episodes:
                for i, ep in enumerate(self._episodes):
                    if ep.get("url") == url:
                        self._cur_idx = i
                        break
            else:
                self._episodes = [{"name": title or "播放", "url": url}]
                self._cur_idx = 0
            if title:
                self._title = title
            idx = self._cur_idx
            episodes = list(self._episodes)
            page_title = self._title or title

        server = self._ensure_server()
        if server is None:
            self._play_file_fallback(url, episodes, idx, page_title)
            return

        mode = self._mode_for(url)
        # 页面回退用的地址：媒体流套上 PLAYER（即 iframe.src），
        # 解析页本身就是回退目标，不用再套。
        fallback = "" if mode == "iframe" else player_url_for(url)
        server.set_episode(idx, url, mode, page_title, episodes, fallback)

        with self._lock:
            self._mode = mode
            self._path = None
            self._page_url = server.base_url

        # 标签页只在「没有活着的页面」时开一次：换集靠 state 轮询，
        # 否则一集一个标签页，一部剧下来能开出几十页。
        if not server.page_alive():
            gm.open_in_tab(self._page_url, active=True, insert=True, set_parent=True)

    def _play_file_fallback(self, url: str, episodes: List[Dict[str, str]],
                            idx: int, title: str) -> None:
        """本地服务起不来时的老方案：写一个 file:// 页面，iframe 塞第三方播放页。"""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "player.html"
        path.write_text(
            render_embed_page(self.cfg, self.layout, episodes, idx, title),
            encoding="utf-8",
        )
        with self._lock:
            self._path = path
            self._page_url = path.as_uri()
            self._mode = "iframe"
        gm.open_in_tab(path.as_uri(), active=True, insert=True, set_parent=True)

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def stop(self) -> None:
        """停止：给页面下发 halt 并收掉本地服务。

        先给页面下一次 halt 状态、等它回一个 `halted` 再关服务：
        直接关的话页面还在播，用户会以为终端没退出干净。等不到回执（页面已关）
        就不等了 —— 关服务本身不能因为页面不配合而卡住。

        取服务时持 `_server_lock`：与 `_ensure_server()` 串行化，
        否则并发的 `play()` 可能刚建好一个服务就被这里漏掉，留下一个
        永远关不掉的孤儿服务（白占端口）。
        """
        with self._server_lock:
            with self._lock:
                server = self._server
                self._server = None
            if server is None:
                return
            server.halt()
            server.wait_halted(timeout=2.0)
            server.stop()

    def is_alive(self) -> bool:
        with self._lock:
            server = self._server
            path = self._path
        if server is not None:
            return server.running and server.page_alive()
        return path is not None

    def take_picked(self) -> Optional[int]:
        """页面选集栏里点了哪一集（一次性取走）。"""
        with self._lock:
            server = self._server
        return server.take_picked() if server is not None else None

    def take_notices(self) -> List[str]:
        """页面回传的一次性提示（回退到 iframe / 需点击起播）。"""
        with self._lock:
            server = self._server
        return server.take_notices() if server is not None else []

    def wait_for_page(self, timeout: float = 25.0) -> bool:
        """等浏览器把页面打开（第一次心跳到达）。"""
        with self._lock:
            server = self._server
        if server is None:
            return self._path is not None
        return server.wait_for_page(timeout=timeout)

    @property
    def page_path(self) -> Optional[Path]:
        return self._path

    @property
    def page_url(self) -> Optional[str]:
        """当前播放页地址：本地服务的 http://127.0.0.1:PORT，或 file:// 兜底页。"""
        with self._lock:
            return self._page_url

    @property
    def embed_server(self) -> Optional[EmbedServer]:
        with self._lock:
            return self._server

    def describe(self) -> str:
        """一句话说明「现在怎么播」。

        文案要短：它会被拼进「✓ 无损云播放中（...）」这类整行输出，而 rich
        按显示宽度折行（中日韩算 2 列），默认终端只有 79 列。实测长文案会把
        整行撑到 82 列、在词中间断行（「hls.js 直播\nm3u8」）。折行本身已由
        ui.toast 的 soft_wrap=True 兜住，这里保持短是为了窄终端也读得顺。
        """
        with self._lock:
            server = self._server
            mode = self._mode
            file_only = self._server_failed or not self.use_server
        if server is not None and server.running:
            if mode == "iframe":
                return f"{self.label}·第三方解析页"
            kind = {"hls": "hls.js", "direct": "原生"}.get(mode, "自有页")
            return f"{self.label}·{kind}"
        if file_only:
            return f"{self.label}·file:// 兜底"
        # 还没起播：默认会走本地服务 + hls.js
        return f"{self.label}·hls.js"


class PrefetchBackend(PlayerBackend):
    """并发预取后端：多线程把 HLS 分片拉到本地，缓存够几片就用本地播放器起播。

    为什么需要它（实测数据）：无损云的分片服务器对**单连接**限速 ~1.2 Mbps，
    而 720p 片源需要 ~1.4 Mbps，所以任何顺序取流的播放器（ffplay / mpv /
    浏览器 hls.js）都追不上实时，模拟 40s 视频卡 4 次。但同一台服务器**并发**
    能拿到高得多的聚合带宽（8 路 4.8 Mbps，16 路 9 Mbps），于是改成：
    并发下载 → 写本地 playlist → 本地播放。PoC 实测 3.9s 起播、0 次缓冲抽干。

    非 HLS 地址（裸 mp4 等）或预取失败时，自动退回直接播放原 URL。
    """

    name = "prefetch"
    label = "并发预取 + 本地播放"

    def __init__(
        self,
        cfg: Optional[Config] = None,
        workers: int = 8,
        ahead: int = 8,
        cache_root: Optional[Path] = None,
        player: Optional["ProcessBackend"] = None,
        keep_cache: bool = False,
        ready_timeout: float = 90.0,
        on_progress: Optional[Callable[[str], None]] = None,
        make_progress: Optional[Callable[[float, str], Any]] = None,
    ):
        from .prefetch import Prefetcher, parse_hls      # 延迟 import，避免无谓开销

        self._Prefetcher = Prefetcher
        self._parse_hls = parse_hls

        self.cfg = cfg
        self.workers = max(1, workers)
        self.ahead = max(1, ahead)
        self.cache_root = Path(cache_root or PREFETCH_ROOT)
        self.keep_cache = keep_cache
        self.ready_timeout = ready_timeout
        self.on_progress = on_progress
        # 进度条工厂 (total, label) -> handle。只用于「起播前的阻塞预取」阶段。
        self.make_progress = make_progress

        self._player = player
        self._episodes: List[Dict[str, str]] = []
        self._cur_idx = 0
        self._title = ""
        self._prefetcher = None
        self._cache_dir: Optional[Path] = None
        self._source_url: Optional[str] = None
        self._local_playlist: Optional[Path] = None
        self._lock = threading.RLock()

    # ── 与 EmbedBackend 一致的选集接口 ────────────────────────────────────
    def set_playlist(self, episodes: List[Dict[str, str]], cur_idx: int, title: str = "") -> None:
        self._episodes = list(episodes)
        self._cur_idx = max(0, min(cur_idx, len(episodes) - 1)) if episodes else 0
        self._title = title

    def _ensure_player(self) -> "ProcessBackend":
        """挑一个本地播放器，并换上「本地文件」参数集。"""
        if self._player is not None:
            return self._player
        self._player = make_local_player()
        if self._player is None:
            raise RuntimeError("本机没有可用的本地播放器（需要 mpv 或 ffplay）")
        return self._player

    def _notify(self, text: str) -> None:
        if self.on_progress:
            try:
                self.on_progress(text)
            except Exception:
                pass

    # ── 播放 ──────────────────────────────────────────────────────────────
    def play(self, url: str, title: str = "") -> None:
        """预取并起播。失败时退回直接播原 URL（不静默失败）。"""
        with self._lock:
            if self._episodes:
                for i, ep in enumerate(self._episodes):
                    if ep.get("url") == url:
                        self._cur_idx = i
                        break
            self._teardown_locked()

        player = self._ensure_player()

        try:
            stream = self._parse_hls(url)
        except Exception as exc:
            # 不是 HLS（裸 mp4 / 解析页 / 网络问题）→ 直接交给播放器
            self._notify(f"⚠ 无法预取（{type(exc).__name__}），改为直接播放")
            player.play(url, title=title or self._title)
            self._source_url = url
            return

        if stream.has_byterange:
            # EXT-X-BYTERANGE 的分片是同一文件的不同字节区间，整片下载会重复拉取，
            # 这种情况直接播原地址更稳妥。
            self._notify("⚠ 该 playlist 使用 BYTERANGE 分片，改为直接播放")
            player.play(url, title=title or self._title)
            self._source_url = url
            return

        total = len(stream.segments)
        dur = stream.total_duration
        # 先给个体积预估：master playlist 的 BANDWIDTH × 时长，让用户知道要花多少
        # 流量/磁盘（拿不到 BANDWIDTH 时为 0，就不显示，不瞎猜）
        est = stream.estimated_bytes
        est_txt = f"，预计 {human_size(est)}" if est else ""
        self._notify(
            f"预取中：{total} 片 / {human_duration(dur)}{est_txt}，{self.workers} 路并发…"
        )

        cache_dir = self.cache_root / _cache_key(url)
        # 进度条只覆盖「起播前的阻塞预取」：player.play() 是非阻塞的，起播后
        # 下载仍在后台继续，而那时用户正停在 `video_vip>` 提示符上敲命令 ——
        # 活着的进度条会把输入行搅乱。所以起播前必须 close 掉。
        #
        # 关掉之后也不能退回「每片一行」：后台还剩上千片，那样照样刷屏，
        # 只是把噪音挪到了播放期间。所以用 state 标记阶段，起播后一律静默，
        # 需要看进度时用交互循环的 u 指令查 stats。
        state = {"bar": None, "quiet_after": False}
        if self.make_progress is not None:
            try:
                state["bar"] = self.make_progress(total, "预取")
            except Exception:
                state["bar"] = None

        def _on_seg(done: int, tot: int, nbytes: int) -> None:
            bar = state["bar"]
            if bar is not None:
                bar.update(done, human_size(nbytes))
            elif not state["quiet_after"]:
                # 没有进度条可用（如 --quiet 或未注入工厂）才逐行报
                self._notify(f"预取 {done}/{tot} 片（{human_size(nbytes)}）")

        prefetcher = self._Prefetcher(
            stream,
            cache_dir,
            workers=self.workers,
            on_progress=_on_seg,
        )
        prefetcher.start()

        with self._lock:
            self._prefetcher = prefetcher
            self._cache_dir = cache_dir
            self._source_url = url
            self._local_playlist = cache_dir / "index.m3u8"

        # 缓存够 ahead 片就起播，不必等整集下完
        want = min(self.ahead, total)
        try:
            ready = prefetcher.wait_for_segments(want, timeout=self.ready_timeout)
        finally:
            # 无论成功失败，起播前都要收掉进度条（换行），否则后面的提示
            # 会接在进度条尾巴上，且后台下载会继续刷新、搅乱输入行
            bar = state["bar"]
            if bar is not None:
                try:
                    bar.finish()
                    bar.close()
                except Exception:
                    pass
            state["bar"] = None
            state["quiet_after"] = True
        if not ready and prefetcher.ready_prefix == 0:
            self._notify("✗ 预取失败（开头分片一片都没拿到），改为直接播放")
            prefetcher.stop()
            player.play(url, title=title or self._title)
            return

        elapsed = prefetcher.elapsed
        rate = prefetcher.bytes_done * 8 / elapsed / 1000 if elapsed > 0 else 0
        # 报「连续就位」片数：这才是播放器能不间断播的部分（并发下载是乱序完成的）
        prefix = prefetcher.ready_prefix
        self._notify(
            f"✓ 已缓存 {prefix}/{total} 片连续就位（{elapsed:.1f}s, {rate:.0f} kbps），起播"
        )
        if not ready:
            self._notify(f"⚠ 仅 {prefix} 片连续就位（目标 {want}），后台继续下载")
        player.play(str(self._local_playlist), title=title or self._title)

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def _teardown_locked(self) -> None:
        """停掉当前播放与预取（调用方需持锁）。"""
        pf, self._prefetcher = self._prefetcher, None
        cache_dir, self._cache_dir = self._cache_dir, None
        self._local_playlist = None
        if pf is not None:
            pf.stop()
        if self._player is not None:
            self._player.stop()
        if cache_dir is not None and not self.keep_cache:
            from .prefetch import cleanup_dir

            cleanup_dir(cache_dir)

    def stop(self) -> None:
        with self._lock:
            self._teardown_locked()

    def is_alive(self) -> bool:
        with self._lock:
            player_alive = self._player.is_alive() if self._player else False
            pf = self._prefetcher
        return player_alive or bool(pf and not pf.cancelled and pf.done < pf.total)

    def wait(self) -> None:
        with self._lock:
            player = self._player
        if player is not None:
            player.wait()

    def describe(self) -> str:
        inner = self._player.describe() if self._player else "本地播放器"
        return f"{self.label}（{self.workers} 路并发 → {inner}）"

    # ── 状态查询（供 CLI 的 u 命令展示）──────────────────────────────────
    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            pf = self._prefetcher
            cache_dir = self._cache_dir
            playlist = self._local_playlist
        if pf is None:
            return {}
        return {
            "source_url": self._source_url,
            "local_playlist": str(playlist) if playlist else None,
            "cache_dir": str(cache_dir) if cache_dir else None,
            "segments_total": pf.total,
            "segments_done": pf.done,
            "segments_failed": len(pf.failed_indices),
            "bytes": pf.bytes_done,
            "elapsed": pf.elapsed,
            "kbps": pf.bytes_done * 8 / pf.elapsed / 1000 if pf.elapsed > 0 else 0,
            "video_duration": pf.stream.total_duration,
            "encrypted": pf.stream.is_encrypted,
        }


def _cache_key(url: str) -> str:
    """把 URL 映射成安全的缓存子目录名（同一集复用缓存，不同集互不干扰）。"""
    import hashlib

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    tail = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1])[:40]
    return f"{digest}_{tail}" if tail else digest


# ─────────────────────────────────────────────────────────────────────────────
# 后端探测
# ─────────────────────────────────────────────────────────────────────────────
MPV_ARGS = [
    "--no-border",
    "--force-window=yes",
    "--title={title}",
    # 无损云的分片服务器单连接只有 ~0.6-1.2 Mbps，且抖动很大（实测有分片要 14s），
    # 而 720p 片源需要 ~1.0 Mbps。默认缓存太浅会被抽干，所以显式放大预读缓存。
    "--cache=yes",
    "--demuxer-max-bytes=512MiB",
    "--demuxer-readahead-secs=120",
    "--network-timeout=30",
    "{url}",
]
FFPLAY_ARGS = [
    "-autoexit",
    "-window_title", "{title}",
    # -infbuf：取消输入缓冲上限。ffplay 默认只缓 ~9KB 音频，实测 75s 内被抽干
    # 94 次（每次都是一次可听的卡顿）；加上 -infbuf 后中位缓冲 966KB，抽干 0 次。
    "-infbuf",
    # 分片服务器偶发超时/断流，自动重连避免直接卡死
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
    # 起播时不要为了探测而读太多数据（默认会读满 5s/5MB，白等）
    "-analyzeduration", "2000000",
    "-probesize", "1000000",
    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "{url}",
]

# ── 播放「本地文件」用的参数集 ────────────────────────────────────────────────
# 预取后端播的是本地 index.m3u8，而 -user_agent / -reconnect / -headers 都是
# HTTP 协议专属选项，用在本地文件上 ffmpeg 会直接报错退出：
#     Option user_agent not found.  /  Option reconnect not found.
# （实测确认）所以本地播放必须用这套不含网络选项的参数。
LOCAL_FFPLAY_ARGS = [
    "-autoexit",
    "-window_title", "{title}",
    "-infbuf",
    "-analyzeduration", "2000000",
    "-probesize", "1000000",
    "{url}",
]
LOCAL_MPV_ARGS = [
    "--no-border",
    "--force-window=yes",
    "--title={title}",
    "--cache=yes",
    "--demuxer-max-bytes=512MiB",
    "{url}",
]


def _find_mpv() -> Optional[str]:
    """PATH 优先，再探常见安装目录（winget 包装到「MPV Player」）。"""
    exe = shutil.which("mpv") or shutil.which("mpv.exe")
    if exe:
        return exe
    for cand in (
        r"C:\Program Files\MPV Player\mpv.exe",
        r"C:\Program Files\mpv\mpv.exe",
        r"C:\mpv\mpv.exe",
    ):
        if os.path.exists(cand):
            return cand
    return None


def make_mpv() -> Optional[ProcessBackend]:
    exe = _find_mpv()
    return ProcessBackend(exe, MPV_ARGS, "mpv 本地播放") if exe else None


def make_ffplay() -> Optional[ProcessBackend]:
    exe = shutil.which("ffplay") or shutil.which("ffplay.exe")
    return ProcessBackend(exe, FFPLAY_ARGS, "ffplay 本地播放") if exe else None


def make_local_player() -> Optional[ProcessBackend]:
    """挑一个本地播放器，但用「本地文件」参数集（不含 HTTP 专属选项）。

    预取后端播的是本地 index.m3u8，若沿用 FFPLAY_ARGS 里的 -user_agent /
    -reconnect，ffmpeg 会直接报 "Option user_agent not found" 退出（实测确认）。
    """
    mpv_exe = _find_mpv()
    if mpv_exe:
        return ProcessBackend(mpv_exe, LOCAL_MPV_ARGS, "mpv 本地播放")

    ff_exe = shutil.which("ffplay") or shutil.which("ffplay.exe")
    if ff_exe:
        return ProcessBackend(ff_exe, LOCAL_FFPLAY_ARGS, "ffplay 本地播放")
    return None


def make_prefetch(cfg: Config, **kwargs: Any) -> Optional[PrefetchBackend]:
    """并发预取后端：需要本机有 mpv 或 ffplay 才能起播，否则返回 None。"""
    if make_local_player() is None:
        return None
    return PrefetchBackend(cfg, **kwargs)


def available_backends(cfg: Config, **prefetch_kwargs: Any) -> Dict[str, PlayerBackend]:
    """按名字返回本机可用的后端（顺序即优先级）。"""
    out: Dict[str, PlayerBackend] = {}
    prefetch = make_prefetch(cfg, **prefetch_kwargs)
    if prefetch:
        out["prefetch"] = prefetch
    mpv = make_mpv()
    if mpv:
        out["mpv"] = mpv
    ffplay = make_ffplay()
    if ffplay:
        out["ffplay"] = ffplay
    out["embed"] = EmbedBackend(cfg)
    out["browser"] = BrowserBackend()
    return out


def detect_backend(cfg: Config, prefer: Optional[str] = None, **prefetch_kwargs: Any) -> PlayerBackend:
    """挑一个播放后端：`--player` 指定优先，否则 mpv → ffplay → embed → browser。

    `prefetch`（并发预取）**不参与自动探测**，需显式 `--player prefetch` 选用：
    它会把整集分片并发下载到本地（45 分钟 720p 约 400MB），这个流量与磁盘开销
    不该在用户不知情时默认发生。它的价值在于绕开分片服务器的单连接限速
    （实测单连接 ~1.2 Mbps < 片源所需 ~1.4 Mbps，顺序取流必然卡顿；
    8 路并发可达 ~4.8 Mbps，PoC 实测 0 次缓冲抽干）。
    """
    backends = available_backends(cfg, **prefetch_kwargs)
    if prefer and prefer in backends:
        return backends[prefer]
    for key in ("mpv", "ffplay", "embed", "browser"):
        if key in backends:
            return backends[key]
    return BrowserBackend()          # 理论上到不了这里
