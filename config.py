"""config.py —— `_CONFIG_` 常量表 + `@include` 匹配规则。

包含：
  * `// @include *://v.qq.com/x/page/*` 等 40 余条 URL 匹配规则  → INCLUDE_PATTERNS / url_supported()
  * `_CONFIG_.videoParseList`                                  → VIDEO_PARSE_LIST
  * `_CONFIG_.playerContainers`                                → PLAYER_CONTAINERS
  * `_CONFIG_` 里的运行时可变字段（directMode / cleanupTimer…）  → Config 类

拆成「静态表（模块级常量）」+「运行时状态（Config 实例）」两部分。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────────────────────
# @include 列表：通配符规则（* 匹配任意字符），转成正则
# ─────────────────────────────────────────────────────────────────────────────
INCLUDE_PATTERNS: List[str] = [
    "*://v.qq.com/x/page/*",
    "*://v.qq.com/x/cover/*",
    "*://v.qq.com/tv/*",
    "*://*.iqiyi.com/v_*",
    "*://*.iqiyi.com/a_*",
    "*://*.iqiyi.com/w_*",
    "*://*.iq.com/play/*",
    "*://*.youku.com/v_*",
    "*://*.youku.com/video*",
    "*://*.youku.com/*?vid=*",
    "*://*.mgtv.com/b/*",
    "*://*.tudou.com/v_*",
    "*://tv.sohu.com/v/*",
    "*://*.bilibili.com/video/*",
    "*://*.bilibili.com/bangumi/play/*",
    "*://v.pptv.com/show/*",
    "*://vip.pptv.com/show/*",
    "*://www.wasu.cn/Play/show/*",
    "*://*.le.com/ptv/vplay/*",
    "*://*.acfun.cn/v/*",
    "*://*.acfun.cn/bangumi/*",
    "*://*.1905.com/play/*",
    "*://m.v.qq.com/x/m/*",
    "*://m.v.qq.com/*",
    "*://m.iqiyi.com/*",
    "*://m.iqiyi.com/v_*",
    "*://m.youku.com/video/*",
    "*://m.youku.com/alipay_*",
    "*://m.mgtv.com/b/*",
    "*://m.tv.sohu.com/v/*",
    "*://m.tv.sohu.com/album/*",
    "*://m.pptv.com/show/*",
    "*://m.bilibili.com/anime/*",
    "*://m.bilibili.com/video/*",
    "*://m.bilibili.com/bangumi/play/*",
]


def include_to_regex(pattern: str) -> "re.Pattern[str]":
    """把 @include 通配符规则编译成正则（`*` → `.*`，其余字符转义）。"""
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile("^" + body + "$", re.IGNORECASE)


_INCLUDE_RES = [include_to_regex(p) for p in INCLUDE_PATTERNS]


def url_supported(url: str) -> bool:
    """@include 命中判断：这个 URL 会不会被处理。"""
    return any(r.match(url) for r in _INCLUDE_RES)


# ─────────────────────────────────────────────────────────────────────────────
# _CONFIG_.videoParseList：解析源清单
#   type 含 "1" → 内嵌播放（本地播放器 / 生成的内嵌 HTML 页）
#   type 含 "3" → 弹窗播放不带选集（直接开浏览器新标签）
#   wsyzy=True  → 无损云直连（suggest 搜索 → API 取 m3u8）
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ParseSource:
    name: str
    type: str
    url: str = ""
    wsyzy: bool = False


VIDEO_PARSE_LIST: List[ParseSource] = [
    ParseSource(name="无损云解析", type="1", wsyzy=True),
    ParseSource(name="LX解析", type="1,3", url="https://jx.lxyy.club/index.php?url="),
    ParseSource(name="789解析", type="1,3", url="https://jiexi.789jiexi.com/?url="),
    ParseSource(name="冰豆解析", type="1,3", url="https://bd.jx.cn/?url="),
    ParseSource(name="麒麟解析", type="1,3", url="https://free.maccms.xyz/?url="),
    # 以下 4 条为 2026-10 调研补充：逐个 requests 实测（拼真实优酷/腾讯官方页）确认可达、
    # 有效 SSL、返回真·播放器页（非域名停放/反爬占位/关停页），据 <title>/播放器库命名。
    ParseSource(name="云解析", type="1,3", url="https://yparse.ik9.cc/index.php?url="),
    ParseSource(name="CK播放器解析", type="1,3", url="https://www.ckplayer.vip/jiexi/?url="),
    ParseSource(name="M3U8解析", type="1,3", url="https://jx.m3u8.tv/jiexi/?url="),
    ParseSource(name="PlayM3U8解析", type="1,3", url="https://www.playm3u8.cn/jiexi.php?url="),
]

# 默认解析源：未显式 -s、交互菜单回车、--no-interact、bootstrap 回车都取它。
# 按名称定位（而非硬编码下标），列表增删导致编号漂移时仍指向 LX解析；找不到则回退 0。
DEFAULT_SOURCE_NAME = "LX解析"
DEFAULT_SOURCE_INDEX: int = next(
    (i for i, s in enumerate(VIDEO_PARSE_LIST) if s.name == DEFAULT_SOURCE_NAME), 0
)


# ─────────────────────────────────────────────────────────────────────────────
# _CONFIG_.playerContainers：各站点的播放器容器 / 需要隐藏的遮罩节点
# 字段名沿用驼峰写法（displayNodes / cleanupNodes）
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PlayerContainer:
    host: str
    container: str
    name: str = "Default"
    displayNodes: List[str] = field(default_factory=list)
    cleanupNodes: List[str] = field(default_factory=list)


PLAYER_CONTAINERS: List[PlayerContainer] = [
    PlayerContainer(
        host="v.qq.com",
        container="#mod_player,#player-container,.container-player",
        displayNodes=["#mask_layer", ".mod_vip_popup", "#mask_layer", ".panel-tip-pay"],
    ),
    PlayerContainer(
        host="m.v.qq.com",
        container=".mod_player,#player",
        displayNodes=[
            ".mod_vip_popup",
            "[class^=app_],[class^=app-],[class*=_app_],[class*=-app-],[class$=_app],[class$=-app]",
            "div[dt-eid=open_app_bottom]",
            "div.video_function.video_function_new",
            "a[open-app]",
            "section.mod_source",
            "section.mod_box.mod_sideslip_h.mod_multi_figures_h,section.mod_sideslip_privileges,section.mod_game_rec",
        ],
    ),
    PlayerContainer(host="w.mgtv.com", container="#mgtv-player-wrap"),
    PlayerContainer(host="www.mgtv.com", container="#mgtv-player-wrap"),
    PlayerContainer(
        host="m.mgtv.com",
        container=".video-area",
        displayNodes=[
            "div.adFixedContain,div.ad-banner,div.m-list-graphicxcy.fstp-mark",
            "div[class^=mg-app],div#comment-id.video-comment div.ft,div.bd.clearfix,div.v-follower-info",
            "div.ht.mgui-btn.mgui-btn-nowelt",
            "div.personal",
            "div[data-v-41c9a64e]",
        ],
    ),
    PlayerContainer(
        host="www.bilibili.com",
        container="#player_module,#bilibiliPlayer,#bilibili-player",
    ),
    PlayerContainer(host="m.bilibili.com", container=".player-wrapper,.player-container,.mplayer"),
    PlayerContainer(
        host="www.iqiyi.com",
        container="#areaLeftContainer,#outlayer,.iqp-player-videolayer",
        displayNodes=[
            "#playerPopup",
            "#vipCoversBox",
            "div.iqp-player-vipmask",
            "div.iqp-player-paymask",
            "div.iqp-player-loginmask",
            "div[class^=qy-header-login-pop]",
            ".covers_cloudCover__ILy8R",
            "#videoContent > div.loading_loading__vzq4j",
            ".iqp-player-guide",
            "#player-loading-layer",
            ".player_outer_video",
        ],
        cleanupNodes=["#player-loading-layer", ".player_outer_video"],
    ),
    PlayerContainer(
        host="m.iqiyi.com",
        container=".m-video-player-wrap, .iqp-player-videolayer",
        displayNodes=[
            "div.m-iqyGuide-layer",
            "a[down-app-android-url]",
            "div.iqp-player-vipmask",
            ".loading_loading__vzq4j",
            "[name=m-extendBar]",
            "[class*=ChannelHomeBanner]",
            "section.m-hotWords-bottom",
        ],
    ),
    PlayerContainer(host="www.iq.com", container=".intl-video-wrap"),
    PlayerContainer(
        host="v.youku.com",
        container=".player-container,#ykPlayer,#playerMouseWheel",
        displayNodes=[
            "#iframaWrapper",
            "#video_side_cashier",
            ".secondary-container.video_side_cashier_wrapper",
            "#youku-dashboard",
        ],
        cleanupNodes=[
            "#youku-dashboard > div.kui-dashboard-dashboard-panel",
            "#youku-dashboard > div.kui-dashboard-dashboard-background",
            "#youku-dashboard > div.kui-dashboard-bar-container",
            "#youku-dashboard > div.kui-dashboard-timer-container",
            "#video_side_cashier",
            ".secondary-container.video_side_cashier_wrapper",
        ],
    ),
    PlayerContainer(host="m.youku.com", container="#playerMouseWheel,.h5-detail-player"),
    PlayerContainer(host="tv.sohu.com", container="#player"),
    PlayerContainer(host="film.sohu.com", container="#playerWrap"),
    PlayerContainer(host="www.le.com", container="#le_playbox"),
    PlayerContainer(host="video.tudou.com", container=".td-playbox"),
    PlayerContainer(host="v.pptv.com", container="#pptv_playpage_box"),
    PlayerContainer(host="vip.pptv.com", container=".w-video"),
    PlayerContainer(host="www.wasu.cn", container="#flashContent"),
    PlayerContainer(host="www.acfun.cn", container="#player"),
    PlayerContainer(host="vip.1905.com", container="#player,#vodPlayer"),
    PlayerContainer(host="www.1905.com", container="#player,#vodPlayer"),
]


def find_player_node(host: str) -> Optional[PlayerContainer]:
    """对应 `_CONFIG_.playerContainers.filter(v => v.host === location.host)[0]`。"""
    for node in PLAYER_CONTAINERS:
        if node.host == host:
            return node
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 运行时状态：挂在 _CONFIG_ 上的那些可变字段
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MOBILE_UA_RE = re.compile(
    r"(Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini)", re.IGNORECASE
)


class Config:
    """`_CONFIG_` 的运行时部分。

    字段：isMobile / currentPlayerNode / vipBoxId / iframeWrapperClass / flag /
         autoPlayerKey / autoPlayerVal / directMode / manualPicked /
         cleanupTimer / wsyzyFsbBound / fullscreenCleanupBound
    """

    iframeWrapperClass = "vip_jx_iframe_wrapper"
    flag = "flag_vip"

    def __init__(self, url: str, user_agent: str = DEFAULT_UA, is_mobile: Optional[bool] = None):
        parsed = urlparse(url)
        self.href: str = url                      # window.location.href
        self.host: str = parsed.netloc            # window.location.host
        self.hostname: str = parsed.hostname or ""  # window.location.hostname
        self.userAgent: str = user_agent          # navigator.userAgent
        # navigator.userAgent.match(/(Android|webOS|iPhone|...)/i)
        self.isMobile: bool = (
            bool(MOBILE_UA_RE.search(user_agent)) if is_mobile is None else is_mobile
        )
        self.vipBoxId: str = "vip_jx_box" + str(random.randint(1, 100000000))
        self.autoPlayerKey: str = "auto_player_key" + self.host
        self.autoPlayerVal: str = "auto_player_value_" + self.host

        self.currentPlayerNode: Optional[PlayerContainer] = None
        self.directMode: bool = False
        self.manualPicked: bool = False
        self.cleanupTimer = None            # util.Repeater（setInterval 的等价物）
        self.wsyzyFsbBound: bool = False
        self.fullscreenCleanupBound: bool = False

        self.videoParseList = VIDEO_PARSE_LIST

    def cleanup_selectors(self) -> List[str]:
        """`[...new Set((displayNodes||[]).concat(cleanupNodes||[]).filter(Boolean))]`"""
        node = self.currentPlayerNode
        if node is None:
            return []
        seen: List[str] = []
        for sel in list(node.displayNodes) + list(node.cleanupNodes):
            if sel and sel not in seen:
                seen.append(sel)
        return seen
