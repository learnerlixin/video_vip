"""video_vip.src —— 包的全部源码（上层目录只留 README / __main__ / tests 等非源码）。

在爱奇艺/腾讯/优酷等视频页上识别片名，调用第三方解析源或无损云直连拿到
m3u8/mp4 真流，再用本地播放器或浏览器播放；支持整季选集、保存 mp4。

    模块            职责
    ------------    ------------------------------------------
    config.py       解析源列表 / 站点容器 / @include 规则
    gm.py           持久化存储 / 打开链接 / 样式收集
    net.py          HTTP 请求层
    page.py         window / document / location 的最小 DOM 兼容层
    util.py         DOM 工具 + 页面会话
    title.py        片名识别与清洗
    wsyzy.py        资源型站点直连（无损云/暴风/红牛：搜索/排序/详情/选集/播放/中止）
    site_search.py  片名/集数 → 官方视频页 URL
    jx_resolve.py   第三方解析页 → 真·媒体直链
    player.py       播放页布局 + 播放后端
    embedsrv.py     embed 后端的本地 HTTP 服务与播放页
    prefetch.py     HLS 并发预取引擎
    download.py     保存为 mp4
    ui.py           终端 UI（toast / 菜单 / 选集栏 / 进度条）
    consumer.py     解析源菜单与播放调度
    cli.py          命令行入口
"""

from __future__ import annotations

__version__ = "1.0.0"
__source_script__ = "video_vip.user.js"
__source_version__ = "3.2.10"

from .config import Config, ParseSource, PlayerContainer, find_player_node, url_supported
from .consumer import Consumer, start
from .net import WsyzyError
from .player import (
    BrowserBackend,
    EmbedBackend,
    PlayerBackend,
    available_backends,
    build_player_frame_layout,
    detect_backend,
    render_embed_page,
)
from .title import read_video_title, wait_stable_title, wsyzy_clean_title
from .ui import Ui
from .util import PageSession, find_target_ele
from .wsyzy import PlayResult, WsyzyDirect

__all__ = [
    "__version__",
    "Config",
    "ParseSource",
    "PlayerContainer",
    "find_player_node",
    "url_supported",
    "Consumer",
    "start",
    "WsyzyError",
    "PlayerBackend",
    "BrowserBackend",
    "EmbedBackend",
    "available_backends",
    "detect_backend",
    "build_player_frame_layout",
    "render_embed_page",
    "wsyzy_clean_title",
    "read_video_title",
    "wait_stable_title",
    "Ui",
    "PageSession",
    "find_target_ele",
    "WsyzyDirect",
    "PlayResult",
]
