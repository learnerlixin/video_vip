"""video_vip —— 全网 VIP 视频解析播放的命令行工具。

在爱奇艺/腾讯/优酷等视频页上识别片名，调用第三方解析源或无损云直连拿到
m3u8/mp4 真流，再用本地播放器或浏览器播放；支持整季选集、自动连播、保存 mp4。

目录约定：本层只放与源码无关的东西（README / __init__ / __main__ / .gitignore
以及 tests/），**全部源码在 `src/` 下**（含随包的 `src/bdjs/`）。这里是包入口，
只做再导出：`from video_vip import WsyzyDirect` 依旧可用，子模块请写
`from video_vip.src.title import ...`。

    模块                    职责
    --------------------    ------------------------------------------
    src/config.py           解析源列表 / 站点容器 / @include 规则
    src/gm.py               持久化存储 / 打开链接 / 样式收集
    src/net.py              HTTP 请求层
    src/page.py             window / document / location 的最小 DOM 兼容层
    src/util.py             DOM 工具 + 页面会话
    src/title.py            片名识别与清洗
    src/wsyzy.py            无损云直连（搜索/排序/详情/选集/播放/中止）
    src/site_search.py      片名/集数 → 官方视频页 URL
    src/jx_resolve.py       第三方解析页 → 真·媒体直链
    src/player.py           播放页布局 + 播放后端
    src/embedsrv.py         embed 后端的本地 HTTP 服务与播放页
    src/prefetch.py         HLS 并发预取引擎
    src/download.py         保存为 mp4
    src/ui.py               终端 UI（toast / 菜单 / 选集栏 / 进度条）
    src/consumer.py         解析源菜单与播放调度
    src/autonext.py         自动连播监视器
    src/cli.py              命令行入口
"""
from __future__ import annotations

from . import src
from .src import *  # noqa: F401,F403  公开 API 名单由 src/__init__.py 的 __all__ 决定

__version__ = src.__version__
__source_script__ = src.__source_script__
__source_version__ = src.__source_version__
__all__ = list(src.__all__) + ["__version__", "__source_script__", "__source_version__"]
