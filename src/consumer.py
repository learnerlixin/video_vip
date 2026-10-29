"""consumer.py —— 解析源菜单与播放调度（Consumer / start）。

VIP 悬浮框在终端里的等价物是「菜单数据 + 用户输入」：`parse()` 构建菜单，
`bind_event()` 把「点击某个 li」转成 `pick(index)` 方法，交互循环在 cli.py。
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import gm
from .config import (
    DEFAULT_RESOURCE_INDEX,
    RESOURCE,
    Config,
    ParseSource,
    find_player_node,
    url_supported,
)
from .player import BrowserBackend, PlayerBackend, detect_backend, is_media_url
from .ui import Ui
from .util import PageSession, Repeater, find_target_ele, hide_nodes, set_interval
from .wsyzy import PlayResult, WsyzyDirect


@dataclass
class VipMenu:
    """`generateElement` 产出的菜单结构（对应那个 VIP 悬浮框里的两个 ul）。"""

    box_id: str
    embedded: List[Dict[str, Any]] = field(default_factory=list)   # type 含 "1"：内嵌播放
    popup: List[Dict[str, Any]] = field(default_factory=list)      # type 含 "3"：弹窗播放
    auto_play: bool = False
    notes: List[str] = field(default_factory=list)
    css: str = ""          # 注册的那份样式表
    html: str = ""         # $(container).append 的那段悬浮框模板


NOTES = [
    "1、本项目为开源项目，完全免费，请勿上当受骗",
    "2、视频内广告系资源自带，请勿轻信任何广告，可快进跳过",
    "3、资源型源（无损云/暴风/红牛）走片库入库，当日更新有入库延迟；无损云另屏蔽欧美、欧洲线路",
    "4、如遇卡顿/无法加载，可切换不同线路/使用海外网络观看",
    "5、后续更新在 GitHub 仓库：88lin/video_vip",
    "6、资源均来自互联网公开分享，未提供资源上传、存储服务",
]

LINKS = {
    "repo": "https://github.com/88lin/video_vip/",
    "sponsor": "https://blog.88lin.eu.org/coffee",
    "more": "https://blog.88lin.eu.org/article/46",
    "web": "https://go.88lin.eu.org/vip",
}

# 自动解析的默认源：取**默认资源型源**（当前 = 0 无损云），而不是默认播放源 LX解析。
# 理由：LX解析返回的 HTML 播放页只有浏览器类后端能渲染，而默认后端是 ffplay/mpv
# 这类本地播放器，它们只认裸流，喂 HTML 一律
# I/O error（实测 5 个第三方源全部如此）。资源型走片库直连，
# 返回真正的 m3u8，本地播放器能直接播。
# 所以取它：保证「自动解析能出画面」。
DEFAULT_AUTO_SOURCE = DEFAULT_RESOURCE_INDEX


def vip_box_css(box_id: str) -> str:
    """`generateElement` 里那段样式表。

    终端用不上 CSS，但它有两个实际用途：
      1) 注册进 gm.add_style()；
      2) 内嵌播放页（player.render_embed_page）需要同一套配色时才不至于走样。
    """
    b = box_id
    return f"""
#{b} {{cursor:pointer; position:fixed; top:120px; left:0px; z-index:2147483647; text-align:left; display:block !important; visibility:visible !important; opacity:1 !important; pointer-events:auto !important; font-family:-apple-system,BlinkMacSystemFont,"Microsoft YaHei","Segoe UI",sans-serif;}}
#{b} .img_box{{width:32px; height:32px;line-height:32px;text-align:center;color:#fff7ed !important;background:#334155;border:1px solid rgba(255,255,255,.18);box-shadow:0 5px 16px rgba(2,8,23,.3),inset 0 1px 0 rgba(255,255,255,.16);margin:3px 0px;border-radius:9px !important;}}
#{b} .vip_icon > .img_box{{background:#6d28d9;border-color:#a78bfa;box-shadow:0 5px 16px rgba(109,40,217,.34),inset 0 1px 0 rgba(255,255,255,.18);}}
#{b} #vip_auto{{color:#f5f3ff !important;background:#4338ca;border-color:#a5b4fc;box-shadow:0 5px 16px rgba(67,56,202,.3),inset 0 1px 0 rgba(255,255,255,.18);}}
#{b} #vip_reload{{color:#fff1f2 !important;background:#be123c;border-color:#fda4af;box-shadow:0 5px 16px rgba(190,18,60,.28),inset 0 1px 0 rgba(255,255,255,.18);}}
#{b} .vip_icon{{position:relative;}}
#{b} .vip_list {{display:none; position:absolute; border-radius:10px; left:34px; top:-30px; text-align:center; background:#071827; border:1px solid #0ea5e9;box-shadow:0 12px 30px rgba(2,12,27,.5);padding:10px 0px; width:380px; max-height:420px; overflow-y:auto;}}
#{b} .vip_repo_btn{{position:absolute; top:8px; right:10px; display:inline-flex; align-items:center; justify-content:center; height:24px; padding:0 10px; border-radius:999px; border:1px solid rgba(125,211,252,.72); background:#e0f2fe; color:#082f49; font-size:11px; font-weight:700; line-height:24px; cursor:pointer; user-select:none; box-shadow:0 3px 8px rgba(14,165,233,.24); transition:background .18s ease,color .18s ease,border-color .18s ease,transform .18s ease;}}
#{b} .vip_repo_btn:hover{{background:#38bdf8; color:#ffffff; border-color:#bae6fd; transform:translateY(-1px);}}
#{b} .vip_repo_btn:focus-visible{{outline:2px solid #fef08a; outline-offset:2px;}}
#{b} .vip_sponsor_btn{{position:absolute; top:8px; left:10px; display:inline-flex; align-items:center; justify-content:center; height:24px; padding:0 10px; border-radius:999px; border:1px solid rgba(244,114,182,.72); background:#fdf2f8; color:#9d174d; font-size:11px; font-weight:700; line-height:24px; cursor:pointer; user-select:none; box-shadow:0 3px 8px rgba(236,72,153,.24); transition:background .18s ease,color .18s ease,border-color .18s ease,transform .18s ease;}}
#{b} .vip_sponsor_btn:hover{{background:#ec4899; color:#ffffff; border-color:#fbcfe8; transform:translateY(-1px);}}
#{b} .vip_sponsor_btn:focus-visible{{outline:2px solid #f9a8d4; outline-offset:2px;}}
#{b} .vip_sec_head{{position:relative; padding:5px 0px 0px;}}
#{b} .vip_sec_head button{{position:absolute; top:50%; transform:translateY(-50%); display:inline-flex; align-items:center; justify-content:center; height:22px; padding:0 9px; border-radius:999px; font-size:11px; font-weight:700; line-height:22px; cursor:pointer; user-select:none; border:1px solid transparent; transition:background .18s ease,color .18s ease,border-color .18s ease,transform .18s ease;}}
#{b} .vip_sec_head button:hover{{transform:translateY(calc(-50% - 1px));}}
#{b} .vip_more_btn{{left:10px; background:#f5f3ff; color:#5b21b6; border-color:rgba(167,139,250,.72); box-shadow:0 3px 8px rgba(139,92,246,.24);}}
#{b} .vip_more_btn:hover{{background:#8b5cf6; color:#ffffff; border-color:#ddd6fe;}}
#{b} .vip_web_btn{{right:10px; background:#ecfdf5; color:#065f46; border-color:rgba(52,211,153,.72); box-shadow:0 3px 8px rgba(16,185,129,.24);}}
#{b} .vip_web_btn:hover{{background:#10b981; color:#ffffff; border-color:#a7f3d0;}}
#{b} .vip_list li{{border-radius:5px; font-size:12px; color:#e0f7ff; text-align:center; width:calc(25% - 14px); line-height:22px; float:left; border:1px solid #155e75; background:#0b2942; padding:0 4px; margin:4px 2px;overflow:hidden;white-space: nowrap;text-overflow: ellipsis;-o-text-overflow:ellipsis;}}
#{b} .vip_list li:hover{{color:#ffffff; border:1px solid #38bdf8; background:#0e7490;}}
#{b} .vip_list ul{{padding-left: 10px; margin:0 0 4px 0;}}
#{b} .vip_list b{{color:#7dd3fc;}}
#{b} .vip_list::-webkit-scrollbar{{width:5px; height:1px;}}
#{b} .vip_list::-webkit-scrollbar-thumb{{box-shadow:inset 0 0 6px rgba(0, 0, 0, 0.2); background:#0ea5e9;}}
#{b} .vip_list::-webkit-scrollbar-track{{box-shadow:inset 0 0 6px rgba(0, 0, 0, 0.2); background:#03111f;}}
#{b} li.selected{{color:#ffffff; border:1px solid #7dd3fc; background:#075985;}}
@media (max-width: 520px) {{
    #{b} .vip_list {{left:38px; top:-30px; width:calc(100vw - 48px); max-width:360px; max-height:70vh; box-sizing:border-box;}}
    #{b} .vip_list li{{width:calc(50% - 14px); line-height:28px; font-size:13px;}}
    #{b} .vip_list ul{{padding-left:8px;}}
    #{b} .vip_list h3{{font-size:14px !important; padding:4px 0px !important;}}
    #{b} .vip_sec_head{{padding:4px 0px 0px;}}
    #{b} .vip_sec_head h3{{font-size:13px !important;}}
    #{b} .vip_sec_head button{{height:20px; padding:0 7px; font-size:10px; line-height:20px;}}
}}
"""


def vip_box_html(box_id: str, sources: List[ParseSource], auto_play: bool) -> str:
    """`generateElement` 里 `$(container).append(...)` 的那段 VIP 悬浮框模板。

    与浏览器版逐节点对应：VIP 图标 → 展开列表（赞助/开源按钮 + [内嵌播放] ul +
    [弹窗播放不带选集] ul + 必看说明）→ 自动解析开关 → 刷新按钮。
    `.nq-li` / `.tc-li` 的 data-index 就是 videoParseList 的下标，点击行为见
    Consumer.pick / Consumer.pick_popup。
    """
    type_1_str = ""
    type_3_str = ""
    for index, item in enumerate(sources):
        # 直接把 ${item.name} 拼进 HTML 字符串会有注入风险，这里补上转义：
        # 现有源名都不含 HTML 特殊字符，输出逐字节相同，但改列表时更安全。
        name = html_escape(item.name)
        if item.kind == RESOURCE:
            # 资源型按片名搜库、没有解析页可弹窗，所以只进 [内嵌播放] 这个 ul；
            # badge 是站点特性说明（无损云那条写着「屏蔽欧美、欧洲线路」）。
            suffix = f"（{html_escape(item.badge)}）" if item.badge else ""
            type_1_str += (
                f'<li class="nq-li" title="{name}{suffix}" '
                f'data-index="{index}">{name}</li>'
            )
            continue
        if "1" in item.type:
            type_1_str += f'<li class="nq-li" title="{name}" data-index="{index}">{name}</li>'
        if "3" in item.type:
            type_3_str += f'<li class="tc-li" title="{name}" data-index="{index}">{name}</li>'

    auto_label = "开" if auto_play else "关"
    notes_html = "".join(f"<br>&nbsp;&nbsp;{html_escape(n)}" for n in NOTES)

    return f"""
<div id="{box_id}">
    <div class="vip_icon">
        <div class="img_box" title="选择解析源" style="color:white;font-size:16px;font-weight:bold;border-radius:5px;"><span style="color:#ffe4e6;">V</span>I<span style="color:#fde68a;">P</span></div>
        <div class="vip_list">
            <button type="button" class="vip_sponsor_btn" title="赞助支持项目持续维护">赞助我们💗</button>
            <button type="button" class="vip_repo_btn" title="打开 GitHub 开源地址">开源仓库⭐</button>
            <div>
                <h3 style="color:#7dd3fc; font-weight: bold; font-size: 16px; padding:5px 0px;">[内嵌播放]</h3>
                <ul>
                    {type_1_str}
                    <div style="clear:both;"></div>
                </ul>
            </div>
            <div>
                <div class="vip_sec_head">
                    <button type="button" class="vip_more_btn" title="查看项目介绍与更多资源">更多资源🎁</button>
                    <h3 style="color:#7dd3fc; font-weight: bold; font-size: 16px; padding:5px 0px;">[弹窗播放不带选集]</h3>
                    <button type="button" class="vip_web_btn" title="打开在线网页版解析">网页版🌐</button>
                </div>
                <ul>
                    {type_3_str}
                    <div style="clear:both;"></div>
                </ul>
            </div>
            <div style="text-align:left;color:#b7d7e8;font-size:10px;padding:0px 10px;margin-top:10px;">
                <b>👇必看说明👇：</b>
                {notes_html}
            </div>
        </div>
    </div>
    <div class="img_box" id="vip_auto" style="color:white;font-size:16px;font-weight:bold;border-radius:5px;" title="是否打开自动解析。若自动解析失败，请手动选择其它接口尝试！！">{auto_label}</div>
    <div class="img_box" id="vip_reload" style="color:white;font-size:14px;font-weight:bold;border-radius:5px;" title="刷新当前解析画面">刷</div>
</div>"""


def html_escape(text: str) -> str:
    """模板里插入外部文本时转义。"""
    return html.escape(text, quote=False)


class Consumer:
    """`BaseConsumer`（DefaultConsumer 是空子类，合并成一个类）。"""

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
        self.direct: Optional[WsyzyDirect] = None
        self.last_result: Optional[PlayResult] = None
        self.selected_index: Optional[int] = None
        self.menu: Optional[VipMenu] = None
        self._url_watcher: Optional[Repeater] = None

    # ── parse()：构建解析源菜单 ─────────────────────────────────────────
    def parse(self) -> VipMenu:
        """`this.parse()`：findTargetEle('body') → preHandle → generateElement
        → bindEvent → autoPlay → postHandle。

        同步执行并返回菜单，供 CLI 渲染。autoPlay 命中时会自动起播。
        """
        self.pre_handle()
        menu = self.generate_element()
        self.bind_event()
        self.auto_play()
        self.post_handle()
        return menu

    def pre_handle(self) -> int:
        """`preHandle(container)`：把 displayNodes + cleanupNodes 全部隐藏。"""
        return hide_nodes(self.page.document, self.cfg.cleanup_selectors())

    def generate_element(self) -> VipMenu:
        """`generateElement(container)`：拼 VIP 悬浮框 HTML → 产出终端菜单数据。

        按 item.kind / type.includes("1") / type.includes("3") 把解析源
        分到两个 ul（[内嵌播放] 与 [弹窗播放不带选集]），这里保持同样的分组：
        资源型（RESOURCE）只有内嵌，采集型按 type 的两个标记各进一份。
        """
        menu = VipMenu(box_id=self.cfg.vipBoxId)
        for index, item in enumerate(self.cfg.videoParseList):
            entry = {
                "index": index,
                "name": item.name,
                "type": item.type,
                "kind": item.kind,
                "url": item.url,
                # title 属性：带 badge 的源把站点特性写在括号里（无损云「屏蔽欧美、欧洲线路」）
                "title": f"{item.name}（{item.badge}）" if item.badge else item.name,
            }
            if item.kind == RESOURCE:
                menu.embedded.append(entry)
                continue
            if "1" in item.type:
                menu.embedded.append(entry)
            if "3" in item.type:
                menu.popup.append(entry)

        menu.auto_play = bool(gm.get_value(self.cfg.autoPlayerKey, None))
        menu.notes = list(NOTES)

        # 终端不渲染 DOM，
        # 但样式表照样注册进 gm，HTML 模板照样生成，供 --dump-box 与
        # 内嵌播放页复用。
        menu.css = vip_box_css(self.cfg.vipBoxId)
        menu.html = vip_box_html(
            self.cfg.vipBoxId, self.cfg.videoParseList, menu.auto_play
        )
        gm.add_style(menu.css)

        self.menu = menu
        return menu

    def bind_event(self) -> None:
        """`bindEvent(container)`：终端里把「点击」
        落成可直接调用的方法（pick / pick_popup / toggle_auto / reload / open_link）。
        """
        return None

    def auto_play(self) -> Optional[PlayResult]:
        """`autoPlay(container)`：GM 里开了自动解析就直接 selectPlayer()。"""
        if gm.get_value(self.cfg.autoPlayerKey, None):
            return self.select_player()
        return None

    def select_player(self, delay: float = 0.0) -> Optional[PlayResult]:
        """`selectPlayer()`：读 GM 里存的上次选择，1.5 秒后自动播放。

        1500ms 延迟是为了等页面渲染；终端里默认不等（delay=0），
        需要严格对齐时传 1.5。directMode/manualPicked 的检查：
        等待期间用户已手动选过就不再自动播放。
        """
        index = gm.get_value(self.cfg.autoPlayerVal, DEFAULT_AUTO_SOURCE)
        auto_obj = self._source_at(index)
        if auto_obj is None or "1" not in auto_obj.type:
            return None

        if delay > 0:
            time.sleep(delay)

        # directMode/manualPicked 检查必须在定时器触发时进行
        if self.cfg.directMode or self.cfg.manualPicked:
            return None
        # 重新读取：以用户最后的选择为准
        idx = gm.get_value(self.cfg.autoPlayerVal, DEFAULT_AUTO_SOURCE)
        obj = self._source_at(idx)
        if obj is None or "1" not in obj.type:
            return None

        result = self.show_player_window(obj)
        self.selected_index = idx
        return result

    def _source_at(self, index: Any) -> Optional[ParseSource]:
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None
        sources = self.cfg.videoParseList
        return sources[i] if 0 <= i < len(sources) else None

    # ── 交互入口（对应 VIP 框里各个 li 的 click 回调）─────────────────────
    def pick(self, index: int) -> Optional[PlayResult]:
        """`.vip_list .nq-li` 的 click：选内嵌播放源。"""
        source = self._source_at(index)
        if source is None:
            self.ui.toast(f"✗ 没有编号为 {index} 的解析源")
            return None
        self.cfg.manualPicked = True
        gm.set_value(self.cfg.autoPlayerVal, index)
        gm.set_value(self.cfg.flag, "true")
        self.selected_index = index
        return self.show_player_window(source)

    def pick_popup(self, index: int) -> bool:
        """`.vip_list .tc-li` 的 click：弹窗播放不带选集（开浏览器新标签）。"""
        source = self._source_at(index)
        if source is None:
            self.ui.toast(f"✗ 没有编号为 {index} 的解析源")
            return False
        self.cfg.manualPicked = True
        if self.cfg.directMode:
            # 移除页面内直连播放器，避免与弹窗播放双份声音
            self.stop_direct()
        self.cfg.directMode = False
        if self.direct is not None:
            self.direct.stop()               # wsyzyDirect.stop()
        if self.cfg.cleanupTimer is not None:
            self.cfg.cleanupTimer.stop()
            self.cfg.cleanupTimer = None
        url = source.url + self.page.url
        return gm.open_in_tab(url, active=True, insert=True, set_parent=True)

    def toggle_auto(self) -> bool:
        """`#vip_auto` 的 click：开/关自动解析，返回切换后的状态。"""
        if gm.get_value(self.cfg.autoPlayerKey, None):
            gm.set_value(self.cfg.autoPlayerKey, None)
            return False
        gm.set_value(self.cfg.autoPlayerKey, "true")
        return True

    def open_link(self, which: str) -> bool:
        """`.vip_repo_btn` / `.vip_sponsor_btn` / `.vip_more_btn` / `.vip_web_btn`"""
        url = LINKS.get(which)
        if not url:
            return False
        return gm.open_in_tab(url, active=True, insert=True, set_parent=True)

    def reload_current_player(self) -> Optional[PlayResult]:
        """`reloadCurrentPlayer()`：有 iframe 就重设 src，否则重跑当前选中源。"""
        if self.last_result is not None and self.last_result.episodes:
            ep = self.last_result.episode()
            self.last_result.backend.play(ep["url"], title=self.last_result.hit.get("name", ""))
            self.ui.toast(f"↻ 已重新加载：{ep.get('name') or ep['url']}")
            return self.last_result

        if self.selected_index is None:
            return None
        source = self._source_at(self.selected_index)
        if source is not None and "1" in source.type:
            return self.show_player_window(source)
        return None

    # ── showPlayerWindow ──────────────────────────────────────────────────
    def show_player_window(self, video_obj: ParseSource) -> Optional[PlayResult]:
        """`showPlayerWindow(videoObj)`：资源型走片库直连，其余源拼 URL 交给后端/浏览器。"""
        if video_obj.kind == RESOURCE:
            self.cfg.directMode = True
            return self.play_direct(video_obj)

        self.cfg.directMode = False
        if self.direct is not None:
            self.direct.stop()               # 切回解析接口时停止直连模式的广告声抑制

        url = video_obj.url + self.page.url
        if "1" not in video_obj.type:
            return None

        # type 含 "1"：内嵌播放。先 findTargetEle 找到官方播放器容器，
        # 找到容器只为确认站点结构存在，
        # 真正的画面交给播放后端。
        try:
            find_target_ele(self.page, self.cfg.currentPlayerNode.container, max_try_time=1)
        except Exception:
            pass
        hide_nodes(self.page.document, self.cfg.cleanup_selectors())
        if self.cfg.cleanupTimer is not None:
            self.cfg.cleanupTimer.stop()
        self.cfg.cleanupTimer = set_interval(
            lambda: hide_nodes(self.page.document, self.cfg.cleanup_selectors()), 500
        )
        if not self.cfg.fullscreenCleanupBound:
            self.cfg.fullscreenCleanupBound = True

        backend = self._backend_for_parse_page(url)
        backend.play(url, title=video_obj.name)
        self.ui.toast(f"✓ {video_obj.name}：{backend.describe()}")
        self.last_result = PlayResult(
            title=video_obj.name,
            hit={"id": None, "name": video_obj.name},
            episodes=[{"name": video_obj.name, "url": url}],
            index=0,
            backend=backend,
            player_url=url,
        )
        return self.last_result

    def _backend_for_parse_page(self, url: str) -> PlayerBackend:
        """为「第三方解析页」挑一个能播它的后端。

        第三方解析源（LX/七哥/TXNQ/789/冰豆/麒麟）返回的是靠 JS 动态取流的
        HTML 网页，实测交给 ffplay/mpv 一律 `I/O error`——本地播放器只认裸流。
        塞进 iframe 由浏览器渲染就没这个问题；本地播放器后端必须改交给
        浏览器类后端（embed/browser）。

        用户显式选的后端若能播 HTML 就沿用，否则退回 BrowserBackend，
        并且不覆盖 self.backend（资源型直连仍用用户选的本地播放器）。
        """
        preferred = self.backend or detect_backend(self.cfg)
        if getattr(preferred, "plays_html", False):
            return preferred
        if not is_media_url(url):
            # 解析页是 HTML，本地播放器播不了 → 换浏览器后端
            self.ui.toast(
                f"⚠ {url.split('?')[0][:48]} 是网页解析源，本地播放器播不了，改用浏览器打开"
            )
            return BrowserBackend()
        return preferred

    def play_direct(self, source: Optional[ParseSource] = None) -> Optional[PlayResult]:
        """`wsyzyDirect.play()`：资源型站点按片名直连（无损云 / 暴风 / 红牛）。

        `source` 决定连哪家库（api / suggest 都在它身上）；不传就是无损云，
        保留给老的调用路径。
        """
        self.cfg.directMode = True
        self.direct = WsyzyDirect(self.cfg, self.page, self.ui, self.backend,
                                  source=source)
        result = self.direct.play()
        if result is not None:
            self.last_result = result
            self.backend = result.backend
        return result

    def stop_direct(self) -> None:
        if self.direct is not None:
            self.direct.stop()
        if self.last_result is not None:
            self.last_result.backend.stop()

    # ── postHandle ────────────────────────────────────────────────────────
    def post_handle(self) -> Optional[Repeater]:
        """`postHandle(container)`：监听 URL 变化并按需 reload。"""
        if gm.get_value(self.cfg.autoPlayerKey, None):
            from .util import url_change_reload

            self._url_watcher = url_change_reload(self.page)
            return self._url_watcher

        state = {"old": self.page.url}

        def tick() -> None:
            win = self.page.reload()
            new = win.location.href if win else state["old"]
            if state["old"] != new:
                state["old"] = new
                if gm.get_value(self.cfg.flag, None):
                    if self._url_watcher is not None:
                        self._url_watcher.stop()
                        self._url_watcher = None
                    self.page.reload()          # window.location.reload()

        self._url_watcher = set_interval(tick, 1000)
        return self._url_watcher

    def shutdown(self, stop_player: bool = True) -> None:
        """收尾：停掉所有定时器与播放进程。

        `stop_player=False` 用于 `--no-interact`：起播后本来就会继续播放，
        命令返回时不该把刚起播的播放器一起杀掉。
        """
        for repeater in (self.cfg.cleanupTimer, self._url_watcher):
            if repeater is not None:
                repeater.stop()
        self.cfg.cleanupTimer = None
        self._url_watcher = None
        if self.direct is not None:
            self.direct.stop()
        if stop_player and self.last_result is not None:
            self.last_result.backend.stop()


# ─────────────────────────────────────────────────────────────────────────────
# superVip.start()
# ─────────────────────────────────────────────────────────────────────────────
def start(
    url: str,
    ui: Optional[Ui] = None,
    backend: Optional[PlayerBackend] = None,
    user_agent: Optional[str] = None,
    page: Optional[PageSession] = None,
    auto: bool = True,
    strict_include: bool = False,
    source: Optional[int] = None,
) -> Optional[Consumer]:
    """`superVip.start()`：站点匹配 → 建 Config → 建 Consumer → parse()。

    返回 Consumer（已完成自动解析），站点不支持时返回 None 并打印那句
    「该网站暂不支持，请联系作者」的警告。

    `source` 是用户在命令行上显式指定的解析源编号（`--source` / `-s`）。
    它必须在下面那个「一次性索引迁移」**之后**才写入 —— 见该处注释。
    """
    ui = ui or Ui()

    cfg_kwargs: Dict[str, Any] = {"url": url}
    if user_agent:
        cfg_kwargs["user_agent"] = user_agent
    cfg = Config(**cfg_kwargs)

    gm.set_value(cfg.flag, None)

    # 一次性索引迁移：videoParseList 在 index 0 插入了「无损云解析」，老用户存储的索引需要 +1
    mig_key = "wsyzy_idx_migrated_" + cfg.host
    if not gm.get_value(mig_key, False):
        old_val = gm.get_value(cfg.autoPlayerVal, None)
        if isinstance(old_val, int) and not isinstance(old_val, bool) and old_val >= 0:
            gm.set_value(cfg.autoPlayerVal, old_val + 1)
        gm.set_value(mig_key, True)

    # 显式 --source 放在迁移**之后**写：迁移的对象是「老版本存下来的编号」，
    # 而命令行给的编号本来就是新表的编号（用户是从 --list 的输出里抄的），
    # 再 +1 就成了别的源 —— 实测 `-s 2`（七哥解析）会被改成 3（TXNQ解析）。
    if source is not None:
        gm.set_value(cfg.autoPlayerVal, source)
        gm.set_value(cfg.autoPlayerKey, "true")

    player_node = find_player_node(cfg.host)
    if player_node is None:
        supported = url_supported(url)
        if strict_include or not supported:
            ui.toast(
                f"✗ {cfg.host} 该网站暂不支持，请联系作者，作者将会第一时间处理"
                f"（注意：请记得提供有问题的网址）"
            )
            return None
        # @include 命中但 playerContainers 里没有该 host：告警退出
        ui.toast(f"✗ {cfg.host} 该网站暂不支持，请联系作者（注意：请记得提供有问题的网址）")
        return None

    cfg.currentPlayerNode = player_node
    mall_case = player_node.name
    del mall_case                                 # 目前只有 Default 一种消费者

    session = page or PageSession(url, user_agent=user_agent)
    session.load()
    if session.window is None:
        ui.toast(f"✗ 页面抓取失败（{session.fetch_error}），仍可按片名搜索播放")

    consumer = Consumer(cfg, session, ui, backend)
    if not auto:
        consumer.generate_element()
        return consumer
    consumer.parse()
    return consumer
