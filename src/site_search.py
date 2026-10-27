"""site_search.py —— 路线 A：只给片名/集数时，为第三方解析源找到「一个具体视频页 URL」。

背景：第三方解析源（LX / 七哥 / TXNQ / 789 / 冰豆 / 麒麟）拼的是
`source.url + 视频页URL`（见 consumer.show_player_window），它们解析的是**一个具体
视频页**，不是片名 —— 所以 `--title` 只给片名时物理上用不了（README 里那段告警）。
这个模块补上「片名 → 官方视频页 URL」这一步，让 `--title … -s 2` 也能走第三方源。

站内搜索走**各家原生接口**，不经任何第三方搜索引擎（2026-10 复测推翻旧结论：当年
「三家原生都不可用」中，爱奇艺/优酷/腾讯的原生 JSON 接口现在都通了，且比 Bing 站内
搜索更准——直接给官方页 URL 与整季元数据，不再有 `site:youku.com` 整体改回
play.tudou.com 镜像域、结果混入别家/过期页的问题）。四站原生都已接入，见
`_NATIVE_SEARCHERS`，Bing `site:` 兜底整条删除。

芒果TV 的「原生」不是它的搜索接口，而是**片库目录全量索引**（pianku.api.mgtv.com，
见 `_mg_lib_index`）：`so.mgtv.com/so` 是纯 CSR、`mobileso` 免签的 `src=intelmgtv` 是
模糊推荐源（实测严格等值只命中 21%，返回的多是花絮/二创页），而片库目录给的是权威
`clipId + playPartId`，可全等匹配到官方播放页。代价是首次搜索要翻页建索引（约 3~5s，
进程内缓存），换来「查不到 = 芒果真没这部剧」是可信结论，不必再靠外部引擎猜。

四家覆盖（都是 INCLUDE_PATTERNS / PRECISE 已支持的官方主站）：
    腾讯视频  v.qq.com/x/cover/<cid>/<vid>.html
    爱奇艺    www.iqiyi.com/a_<album>.html（专辑页）→ /v_<vid>.html（单集页）
    优酷      v.youku.com/v_show/id_<vid>.html
    芒果TV    www.mgtv.com/b/<cid>/<vid>.html

集数处理分四档（能力不同，落地路径也不同）：
    爱奇艺：专辑页 HTML 里静态内嵌整季 /v_xxx.html 列表（实测 46 集全在）
            → 桌面页静态解析，按集号取第 N 个单集页 —— 精确。
    优酷  ：桌面 v.youku.com 被 x5sec 反爬盾（返 1.5KB 空壳），但**移动端
            m.youku.com 不挡且 window.__INITIAL_DATA__ 内嵌整季 (stage, vid)
            配对** → 用给定 vid 打 m.youku.com/video/id_<vid>.html,按 stage
            取 vid,再拼回桌面 URL —— 精确。
    腾讯  ：桌面 HTML 静态只有当前 vid（选集全靠 JS 异步加载），但**纯 requests 可
            打通 pbaccess.getPage**（从 JS bundle 逆向出完整请求体，需 page_bypass_params
            + snake_case page_params 全量字段 + vversion_name=8.5.96）—— 返回 350KB+ JSON
            CardList 里的 pc_web_episode_list 卡包含整季 (集数, vid)。超过 30 集需 tabs
            翻页（page_context 参数）→ 精确。
    芒果  ：播放页是 Nuxt 空壳，整季靠官方分集接口
            pcweb.api.mgtv.com/episode/list 异步拉 → 精确。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import net
from . import title as _title_mod
from .config import url_supported

# 四家官方站的站点名（find_page_url 的尝试顺序 = 这里的顺序；`--site` 指定者提到最前）。
# 每家的 URL 形态：
#     腾讯视频  v.qq.com/x/cover/<cid>/<vid>.html
#     爱奇艺    www.iqiyi.com/a_<album>.html（专辑页）→ /v_<vid>.html（单集页）
#     优酷      v.youku.com/v_show/id_<vid>.html
#     芒果TV    www.mgtv.com/b/<cid>/<vid>.html
# 结果合法性统一由 url_supported 把关，各原生函数自己负责只返回本站域名。
_SITES: Tuple[str, ...] = ("腾讯视频", "爱奇艺", "优酷", "芒果TV")

# 爱奇艺专辑页里的单集链接（用于按集号精确定位）
_IQIYI_EPS_RE = re.compile(r"https?://www\.iqiyi\.com/(v_[A-Za-z0-9]+)\.html")
_IQY_ALBUM_RE = re.compile(r"/a_([A-Za-z0-9]+)\.html")
# 专辑页里的 <a href="/v_xxx.html">…</a>：连同锚文本一起抓，用于区分正片与噪声。
_IQY_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?href="[^"]*?(v_[A-Za-z0-9]+)\.html"[^>]*?>(.*?)</a>', re.S | re.I)
# 正片锚文本恰为「第N集」（去掉嵌套标签与空白后整体匹配）
_IQY_EP_LABEL_RE = re.compile(r"第(\d{1,4})集")
# albumId：桌面 /a_ 页内嵌 JS 写作 albumId: "7410473159536632"（无引号键），
# 移动端 /v_ 页写作 "albumId":7410473159536632（JSON，带引号键、数字无引号）。
# 两种形态一起抓：键可选引号，值可选引号，取纯数字。
_IQY_ALBUM_ID_RE = re.compile(r'"?albumId"?\s*:\s*"?(\d{6,})"?')


def _iqy_positive_vids(html: str) -> Dict[int, str]:
    """爱奇艺专辑页 HTML → {集号: vid}，**只认锚文本恰为「第N集」的 /v_ 链接**。

    专辑页里除了正片，还混有『其它剧推荐』（如"小城良方"）和『00:30 预告/看点短视频』
    等 /v_ 链接；把它们无差别收进选集会让整季虚高——实测《伟大的长征》真 27 集被数成
    35。正片的锚文本恰是「第N集」，据此过滤即只留正片，并按集号天然有序。
    同集号万一重复出现（正片 + 别处引用）保留第一条。
    """
    eps: Dict[int, str] = {}
    for vid, inner in _IQY_ANCHOR_RE.findall(html):
        txt = re.sub(r"<[^>]+>", "", inner)
        txt = re.sub(r"\s+", "", txt)
        m = _IQY_EP_LABEL_RE.fullmatch(txt)
        if m:
            eps.setdefault(int(m.group(1)), vid)
    return eps


def _iqy_fetch_html(album_url: str) -> str:
    """抓专辑页 HTML；爱奇艺偶有无 charset 的 text/html，requests 会退回 latin-1
    把「第N集」中文弄乱，故显式纠正为 utf-8。"""
    resp = net.get_session().get(album_url, timeout=15)
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        resp.encoding = "utf-8"
    return resp.text


def _iqy_extract_album_id(html: str) -> str:
    """从任意爱奇艺页面 HTML 抠 albumId（纯数字串），找不到返回 ''。"""
    m = _IQY_ALBUM_ID_RE.search(html)
    return m.group(1) if m else ""


def _iqy_fetch_mobile_html(url: str) -> str:
    """用 iPhone UA 抓移动端页。桌面 UA 打 m.iqiyi.com 的单集页拿不到 albumId，
    换移动端 UA 才稳定含 "albumId":...。"""
    resp = net.get_session().get(url, timeout=15,
                                 headers={"User-Agent": _YK_MOBILE_UA,
                                          "Referer": "https://m.iqiyi.com/"})
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        resp.encoding = "utf-8"
    return resp.text


def _iqy_album_id_from_vid(vid: str) -> str:
    """单集页 /v_xxx 反推整季 albumId：抓 m.iqiyi.com 移动版，抠 "albumId"。

    桌面 /v_ 页 HTML 不含 albumId，只有移动版有——所以走 m 域。vid 已带 v_ 前缀
    （形如 v_1qfr4asm7bw），拼 m.iqiyi.com/{vid}.html。m 域偶尔回 762B 空壳（实测
    首访易空），故重试 3 次；仍失败返回 ''，调用方退回只给这一集。
    """
    if not vid:
        return ""
    url = f"https://m.iqiyi.com/{vid}.html"
    for _ in range(3):
        try:
            aid = _iqy_extract_album_id(_iqy_fetch_mobile_html(url))
        except Exception:
            continue
        if aid:
            return aid
    return ""


def _iqy_list_eps_api(album_id: str) -> Optional[List[Tuple[str, str]]]:
    """官方分集接口 avlistinfo 按 albumId 枚举正片，返回 [(「第N集」, 单集页URL)]。

    只保留 contentType==1 的正片（预告/花絮/点播等其它 contentType 丢弃），按 order
    排序。接口按 total 翻页——实测 hasMore 不可靠（size=10 时可能 hasMore=False 但
    total=39），故循环到已收集数 >= total 或某页无正片为止。albumId 无效/接口异常/
    无正片均返回 None，交给调用方回退 HTML 枚举。
    """
    if not album_id:
        return None
    eps: Dict[int, str] = {}
    try:
        for page in range(1, 40):
            resp = net.get_session().get(
                "https://pcw-api.iqiyi.com/albums/album/avlistinfo",
                params={"aid": album_id, "page": page, "size": 200},
                timeout=20, headers={"Referer": "https://www.iqiyi.com/"})
            data = resp.json().get("data") or {}
            lst = data.get("epsodelist") or []
            if not lst:
                break
            for it in lst:
                if it.get("contentType") != 1:
                    continue
                m = _IQIYI_EPS_RE.search(it.get("playUrl") or "")
                if not m:
                    continue
                order = it.get("order")
                n = int(order) if isinstance(order, (int, float, str)) and \
                    str(order).strip().isdigit() else len(eps) + 1
                eps[n] = m.group(1)
            total = data.get("total")
            if not isinstance(total, (int, float)) or len(eps) >= int(total):
                break
    except Exception:
        return None
    if not eps:
        return None
    return [(f"第{n}集", f"https://www.iqiyi.com/{eps[n]}.html")
            for n in sorted(eps)]


# 优酷：桌面 URL 抠 vid、移动端不挡反爬、__INITIAL_DATA__ 里内嵌 (stage, vid) 配对
# vid 两域通用（v.youku.com 与 play.tudou.com 是同一套 base64 vid），抠出后拼回 v.youku.com
_YK_DESKTOP_VID_RE = re.compile(r"(?:v\.youku\.com|play\.tudou\.com)/v_show/id_([A-Za-z0-9=]+)\.html")
_YK_MOBILE_VID_RE = re.compile(r"m\.youku\.com/video/id_([A-Za-z0-9=]+)\.html")
# 每集条目大致：{"videoType":"正片","title":"第N集","stage":N,"action":{"type":"JUMP_TO_VIDEO","value":"<vid>"}}
# stage 与 action 之间只有普通字符串字段（无嵌套花括号）；action 里 type 与 value 紧邻。
# videoType 必须一起抓：抢先看剧集的正片和预告片**共用同一个 stage 号**（如嫁金钗
# 第18集 = 1 条正片 + 1 条预告），只按 stage 建映射会让后出现的预告覆盖正片 vid，
# 拼出来的单集页是 1 分钟预告——解析源自然"看不了"。只保留 videoType=="正片"。
_YK_STAGE_VID_RE = re.compile(
    r'"videoType"\s*:\s*"([^"]*)"[^{}]*?'
    r'"stage"\s*:\s*(\d+)\s*,[^{}]*?'
    r'"action"\s*:\s*\{\s*"type"\s*:\s*"JUMP_TO_VIDEO"\s*,\s*"value"\s*:\s*"(X[A-Za-z0-9=]+)"'
)
_YK_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")
# 剧名：移动页 __INITIAL_DATA__ 的 showName 优先，退回 <title>（实测 = 纯剧名「嫁金钗」）
_YK_SHOWNAME_RE = re.compile(r'"showName"\s*:\s*"([^"]+?)"')
_YK_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


@dataclass
class SiteHit:
    """一次站内搜索的命中结果：哪个站点、哪一集、页在哪。"""

    site: str
    url: str
    episode: int = 0
    # 请求了集数时，url 是否**真的**定位到了那一集的单集页。三家均可精确 pinned=True：
    # 爱奇艺走专辑页静态枚举、优酷走 m.youku.com (stage,vid) 配对、
    # 腾讯走 pbaccess.getPage CardList 里 pc_web_episode_list 卡按集号取 vid。
    # 仅在枚举失败（网络异常 / 超出总集数）时退回整季页，episode_pinned=False。
    episode_pinned: bool = True
    # 整季入口页（cover/专辑页），供第三方源枚举全部集数用；单集命中时 url 是单集页、
    # season_url 仍是可枚举整季的那一页。
    season_url: str = ""

    def label(self) -> str:
        ep = f"（第{self.episode}集）" if (self.episode > 0 and self.episode_pinned) else ""
        return f"{self.site}{ep}"


# ─────────────────────────────────────────────────────────────────────────────
# 选集枚举：三家均可纯 requests 按集号精确定位单集页
# ─────────────────────────────────────────────────────────────────────────────
def _iqiyi_episode_url(album_url: str, episode: int) -> Optional[str]:
    """爱奇艺按集号精确定位单集页。走 _iqy_list_eps（avlistinfo 优先、HTML 兜底、
    /v_ 反推 albumId 补全整季）拿到整季，再按集号取——与选集枚举同一套逻辑，避免
    两条路径对「沧元图」这类 HTML 只列 1 集的专辑给出不同结果。"""
    if episode <= 0:
        return None
    eps = _iqy_list_eps(album_url)
    if not eps:
        return None
    for label, url in eps:
        m = _IQY_EP_LABEL_RE.search(label)
        if m and int(m.group(1)) == episode:
            return url
    return None


def _youku_episode_url(entry_url: str, episode: int) -> Optional[str]:
    """优酷按集号精确定位。桌面页 v.youku.com 会吃 x5sec 反爬盾（返 1.5KB 空壳），
    但**移动端 m.youku.com 不挡**，页面内嵌整季 (stage, vid) 配对；抓一次移动端
    页面取正片全集、按顺序重编号后拿第 episode 条，再拼回桌面版单集 URL。

    集号口径与 _yk_list_eps 完全一致（都走 _yk_positive_rows）——综艺的 stage 是
    播出日期（20261021 这种），直接拿 episode 当 stage 查会恒落空。
    """
    if episode <= 0:
        return None
    m = _YK_DESKTOP_VID_RE.search(entry_url) or _YK_MOBILE_VID_RE.search(entry_url)
    if not m:
        return None
    rows = _yk_positive_rows(m.group(1))
    if episode > len(rows):
        return None
    return f"https://v.youku.com/v_show/id_{rows[episode - 1][1]}.html"


def _tencent_episode_url(entry_url: str, episode: int) -> Optional[str]:
    """腾讯视频按集号精确定位。桌面 cover 页 HTML 静态只有当前 vid（选集靠 JS），
    但纯 requests 可打通 pbaccess.getPage —— 从 JS bundle 逆向出精确请求体：
    POST 带 snake_case page_params（含 req_from/cid/vid 等全量字段）
    + page_bypass_params（caller_id/scene/abtest_bypass_id）
    + URL query 含 vversion_name=8.5.96 与动态 vdevice_guid。
    返回 CardList 里 pc_web_episode_list 卡 → children_list 按 c_title_output(集号) 取 vid。
    超过 30 集需从 tabs page_context 翻页。
    """
    if episode <= 0:
        return None
    m = re.search(r'v\.qq\.com/x/(?:cover|page)/([A-Za-z0-9]+)/?([A-Za-z0-9]*)', entry_url)
    if not m:
        return None
    cid, vid = m.group(1), m.group(2)
    card = _tx_fetch_episode_card(cid, vid)
    if not card:
        return None
    eps = _tx_extract_eps(card)
    if episode in eps:
        return f"https://v.qq.com/x/cover/{cid}/{eps[episode]}.html"
    # 超出首页范围（>30），从 tabs 翻页
    for tab in _tx_tabs(card):
        if not tab.get("selected") and tab.get("begin", 0) <= episode <= tab.get("end", 9999):
            pc = tab.get("page_context", "")
            card2 = _tx_fetch_episode_card(cid, vid, page_context=pc)
            if card2:
                eps2 = _tx_extract_eps(card2)
                if episode in eps2:
                    return f"https://v.qq.com/x/cover/{cid}/{eps2[episode]}.html"
    return None


_TX_GP = "https://pbaccess.video.qq.com/trpc.vector_layout.page_view.PageService/getPage"
_TX_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _tx_tabs(card: dict) -> list:
    """pc_web_episode_list 卡的分页 tab 列表。

    card['params']['tabs'] 是**字符串化的 JSON**，且并非所有片子都有该字段——
    电影/单片类（如《哪吒之魔童闹海》）返回的是非 JSON 串，裸 json.loads 会抛
    JSONDecodeError 把整条查找链路打断。统一在这里吞掉，返回 []。
    """
    try:
        tabs = json.loads((card.get("params") or {}).get("tabs", "[]"))
    except Exception:
        return []
    return tabs if isinstance(tabs, list) else []


def _tx_fetch_episode_card(cid: str, vid: str, page_context: str = "") -> Optional[dict]:
    """POST pbaccess.getPage → 返回 pc_web_episode_list card dict（失败 None）。"""
    data = _tx_getpage_data(cid, vid, page_context)
    if not data:
        return None
    for c in data.get("CardList", []):
        if c.get("type") == "pc_web_episode_list":
            return c
    return None


# 单格 memo：同一 (cid, vid, page_context) 的 getPage 响应在「取剧名」与「枚举整季」
# 两处会被先后请求（key 相同），缓存上一条即可省掉一次 356KB 的重复 POST。tab 翻页
# 时 key 变化会自然覆盖，无膨胀风险。
_TX_DATA_MEMO: Tuple[tuple, Optional[dict]] = (("", "", ""), None)


def _tx_getpage_data(cid: str, vid: str, page_context: str = "") -> Optional[dict]:
    """POST pbaccess.getPage → 返回内层 data dict（含 CardList），失败 None。

    CardList[0]（头部播放器模块）的 params.mod_title 就是纯剧名；pc_web_episode_list
    卡是整季选集——剧名与集数来自**同一份响应**，别只取其一。
    """
    global _TX_DATA_MEMO
    key = (cid, vid, page_context)
    if _TX_DATA_MEMO[0] == key and _TX_DATA_MEMO[1] is not None:
        return _TX_DATA_MEMO[1]
    import random as _rnd
    rhex = lambda n: ''.join(_rnd.choice('0123456789abcdef') for _ in range(n))
    url = (f"{_TX_GP}?video_appid=3000010&vversion_platform=2"
           f"&vversion_name=8.5.96&vdevice_guid={rhex(16)}")
    body = {
        "page_params": {
            "ad_wechat_authorization_status": "0", "req_from": "web_vsite",
            "ad_exp_ids": "", "pc_sdk_version": "", "pc_oaid": "",
            "new_mark_label_enabled": "1", "pc_device_info": "",
            "support_pc_yyb_mobile_app_engine": "0", "pc_wegame_version": "",
            "cid": cid, "vid": vid or "",
        },
        "page_bypass_params": {
            "params": {"caller_id": "3000010", "platform_id": "2"},
            "scene": "desk_detail", "app_version": "",
            "abtest_bypass_id": rhex(32),
        },
        "page_context": {},
    }
    if page_context:
        body["page_params"]["page_context"] = page_context
    hdrs = {"User-Agent": _TX_UA,
            "Referer": f"https://v.qq.com/x/cover/{cid}/{vid}.html" if vid else f"https://v.qq.com/x/cover/{cid}.html",
            "Origin": "https://v.qq.com", "Content-Type": "application/json"}
    try:
        session = net.get_session()
        # 先访问封面页拿必要 cookies（qq_domain_video_guid_verify）
        cover = f"https://v.qq.com/x/cover/{cid}/{vid}.html" if vid else f"https://v.qq.com/x/cover/{cid}.html"
        session.get(cover, headers={"User-Agent": _TX_UA}, timeout=15)
        r = session.post(url, data=json.dumps(body), headers=hdrs, timeout=20)
        data = json.loads(r.text).get("data") or None
        if data:
            _TX_DATA_MEMO = (key, data)
        return data
    except Exception:
        return None


def _tencent_show_name(entry_url: str) -> str:
    """腾讯视频剧名：桌面 cover 页是 JS 壳、requests 抓不到 <title>，但 pbaccess.getPage
    的头部卡 params.mod_title 就是纯剧名（实测「庆余年」）。取第一处非空 mod_title。"""
    m = re.search(r'v\.qq\.com/x/(?:cover|page)/([A-Za-z0-9]+)/?([A-Za-z0-9]*)', entry_url)
    if not m:
        return ""
    data = _tx_getpage_data(m.group(1), m.group(2))
    if not data:
        return ""
    for c in data.get("CardList", []):
        t = ((c.get("params") or {}).get("mod_title") or "").strip()
        if t:
            return t
    return ""



def _tx_extract_eps(card: dict) -> Dict[int, str]:
    """从 pc_web_episode_list card 提取 {集数: vid}。"""
    eps: Dict[int, str] = {}
    for group in card.get("children_list", {}).values():
        for ep in group.get("cards", []):
            p = ep.get("params", {})
            title = p.get("c_title_output", "")
            ep_vid = ep.get("id", "")
            if title.isdigit() and ep_vid:
                eps[int(title)] = ep_vid
    return eps


# ─────────────────────────────────────────────────────────────────────────────
# 芒果TV：播放页 /b/<cid>/<pid>.html 本身可解析（第三方源直解官方页），但页面是
#   Nuxt SSR 空壳、静态 HTML 里没有其它集链接——整季靠官方分集接口异步拉：
#   GET pcweb.api.mgtv.com/episode/list?video_id&cid → data.list 每项
#   t4「第N集」/ url「/b/cid/pid.html」/ t1 集号 / t3「剧名 第N集」。
#   实测《玉簟秋》code=200 total=40，一次返回整季；长剧集按 pn 翻页兜底。
# ─────────────────────────────────────────────────────────────────────────────
_MG_PAGE_RE = re.compile(r"mgtv\.com/b/(\d+)/(\d+)")
_MG_EP_API = "https://pcweb.api.mgtv.com/episode/list"
_MG_EP_LABEL_RE = re.compile(r"第\s*(\d{1,4})\s*集")


def _mg_episode_data(cid: str, vid: str, page: int = 1) -> Optional[dict]:
    """GET 芒果分集接口，返回内层 data（含 list/total）；失败 None。
    带 Referer 防反爬；page>1 时用 pn 翻页（若接口不认 pn，调用侧靠去重兜底）。"""
    params = {"version": "6", "is_vip": "1", "video_id": vid,
              "cid": cid, "_src": "mgtv"}
    if page > 1:
        params["pn"] = str(page)
    hdrs = {"User-Agent": "Mozilla/5.0",
            "Referer": f"https://www.mgtv.com/b/{cid}/{vid}.html"}
    try:
        resp = net.get_session().get(_MG_EP_API, params=params,
                                     headers=hdrs, timeout=15)
        j = json.loads(resp.text)
    except Exception:
        return None
    if j.get("code") != 200:
        return None
    return j.get("data") or None


def _mg_eps_from_data(data: dict) -> Dict[int, Tuple[str, str]]:
    """data.list → {集号: (标签, 单集页URL)}，只留**完整正片**（isIntact=="1"）。

    mgtv 对同一集常并列两条：isIntact=3 的预览/看点占位 + isIntact=1 的正片；预览页
    解析源解不动（实测第3集预览 FAIL、同集正片 OK），故必须按 isIntact==1 筛，顺带
    滤掉别剧推荐/花絮（t4 不是「第N集」）。极端情况整季都没标 isIntact，则退回只按
    「第N集」标签收集，避免直接空手。"""
    strict: Dict[int, Tuple[str, str]] = {}
    loose: Dict[int, Tuple[str, str]] = {}
    for it in (data.get("list") or []):
        rel = it.get("url") or ""
        label = (it.get("t4") or "").strip()
        m = _MG_EP_LABEL_RE.fullmatch(label)
        if not (rel and m):
            continue
        pair = (label, "https://www.mgtv.com" + rel)
        n = int(m.group(1))
        loose.setdefault(n, pair)
        if str(it.get("isIntact")) == "1":
            strict.setdefault(n, pair)
    return strict or loose


def _mg_collect_eps(entry_url: str) -> Dict[int, Tuple[str, str]]:
    """给定 mgtv /b 页，取回 {集号: (标签, URL)} 全季（含翻页兜底）。失败空字典。"""
    m = _MG_PAGE_RE.search(entry_url)
    if not m:
        return {}
    cid, vid = m.group(1), m.group(2)
    data = _mg_episode_data(cid, vid)
    if not data:
        return {}
    eps = _mg_eps_from_data(data)
    total = int(data.get("total") or 0)
    page = 2
    while len(eps) < total and page <= 30:
        before = len(eps)
        d2 = _mg_episode_data(cid, vid, page)
        if not d2:
            break
        for n, val in _mg_eps_from_data(d2).items():
            eps.setdefault(n, val)
        page += 1
        if len(eps) == before:      # pn 未被采纳/无新集：停，避免空转
            break
    return eps


def _mg_list_eps(entry_url: str) -> Optional[List[Tuple[str, str]]]:
    eps = _mg_collect_eps(entry_url)
    if not eps:
        return None
    return [eps[n] for n in sorted(eps)]


def _mg_episode_url(entry_url: str, episode: int) -> Optional[str]:
    """按集号取芒果单集页 URL（走分集接口）；越界/失败 None。"""
    if episode <= 0:
        return None
    eps = _mg_collect_eps(entry_url)
    got = eps.get(episode)
    return got[1] if got else None


def _mg_show_name(entry_url: str) -> str:
    """剧名取首集 t3「剧名 第N集」去掉尾部集号（实测最稳；info 结构不稳不依赖）。"""
    eps = _mg_collect_eps(entry_url)
    if not eps:
        return ""
    # 直接用接口原始 list 的 t3：重新取一次带 t3 的 data
    m = _MG_PAGE_RE.search(entry_url)
    if not m:
        return ""
    data = _mg_episode_data(m.group(1), m.group(2))
    for it in ((data or {}).get("list") or []):
        t3 = (it.get("t3") or "").strip()
        if t3:
            return re.sub(r"\s*第\s*\d+\s*集.*$", "", t3).strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 整季枚举：给定官方页 URL，返回全部单集页（第三方源据此列出「共 N 集」）
# ─────────────────────────────────────────────────────────────────────────────
def list_episode_pages(entry_url: str) -> Optional[List[Tuple[str, str]]]:
    """给定官方页 URL，返回整季有序的 [(集标签, 单集页URL), ...]；无法枚举返回 None。

    与单集定位同源的几条纯 requests 路径，只是这里返回**全部**集而非第 N 集：
      腾讯    pbaccess.getPage 首页 + tabs page_context 翻页合并；
      优酷    m.youku.com 移动页 __INITIAL_DATA__ 里 videoType=="正片" 的 (stage,vid)；
      爱奇艺  专辑页 /a_ HTML 静态内嵌整季 /v_ 列表。
      芒果TV  官方分集接口 pcweb.api.mgtv.com/episode/list（播放页是 Nuxt 空壳，选集靠此异步拉）。
    拿到的是官方单集页 URL；第三方源各自再前缀 `source.url` 拼成解析页。
    """
    m = re.search(r'v\.qq\.com/x/(?:cover|page)/([A-Za-z0-9]+)/?([A-Za-z0-9]*)', entry_url)
    if m:
        return _tx_list_eps(m.group(1), m.group(2))
    m = _YK_DESKTOP_VID_RE.search(entry_url) or _YK_MOBILE_VID_RE.search(entry_url)
    if m:
        return _yk_list_eps(m.group(1))
    if _IQY_ALBUM_RE.search(entry_url):
        return _iqy_list_eps(entry_url)
    # 爱奇艺单集页 /v_：抓移动版反推 albumId 补全整季，反推不成再退回只给这一集
    if re.search(r'iqiyi\.com/(v_[A-Za-z0-9]+)\.html', entry_url):
        return _iqy_list_eps(entry_url)
    if _MG_PAGE_RE.search(entry_url):
        return _mg_list_eps(entry_url)
    return None


def _iqy_html_eps(html: str) -> Optional[List[Tuple[str, str]]]:
    """专辑页 HTML → [(「第N集」, 单集页URL)]：只认锚文本「第N集」的正片；
    页面不用该锚文本时退回按出现顺序枚举全部 /v_ 链接。找不到返回 None。"""
    eps = _iqy_positive_vids(html)
    if eps:
        return [(f"第{n}集", f"https://www.iqiyi.com/{eps[n]}.html")
                for n in sorted(eps)]
    vids = list(dict.fromkeys(_IQIYI_EPS_RE.findall(html)))
    if not vids:
        return None
    return [(f"第{i + 1}集", f"https://www.iqiyi.com/{v}.html")
            for i, v in enumerate(vids)]


def _iqy_list_eps(entry_url: str) -> Optional[List[Tuple[str, str]]]:
    """爱奇艺选集枚举：**avlistinfo 优先，HTML 兜底**（两者互补，绝不退化）。

    入口可以是专辑页 /a_xxx.html，也可以是单集页 /v_xxx.html：
      - /a_ 页：先抓 HTML（既供 HTML 兜底，也从内嵌 JS 抠 albumId），拿到 albumId
        就试 avlistinfo；接口给得出正片就用接口（如「沧元图」HTML 只枚举 1 集，
        接口能拉满 52 集），否则用 HTML。
      - /v_ 页：桌面页无 albumId，抓 m.iqiyi.com 移动版反推 albumId 再走接口；
        并要求「当前 vid ∈ 接口结果」才采信（防错 albumId 返回别剧），否则只给这一集。
    """
    m = _IQY_ALBUM_RE.search(entry_url)
    if m:
        try:
            html = _iqy_fetch_html(entry_url)
        except Exception:
            html = ""
        album_id = _iqy_extract_album_id(html)
        api = _iqy_list_eps_api(album_id)
        if api:
            return api
        return _iqy_html_eps(html) if html else None
    m = re.search(r'iqiyi\.com/(v_[A-Za-z0-9]+)\.html', entry_url)
    if m:
        cur_vid = m.group(1)
        album_id = _iqy_album_id_from_vid(cur_vid)
        api = _iqy_list_eps_api(album_id)
        # 只有当前集确实落在接口返回的选集里，才敢把整季交给用户
        if api:
            got = {_IQIYI_EPS_RE.search(u).group(1)
                   for _, u in api if _IQIYI_EPS_RE.search(u)}
            if cur_vid in got:
                return api
        return [("第1集", f"https://www.iqiyi.com/{cur_vid}.html")]
    return None


def _yk_list_eps(vid: str) -> Optional[List[Tuple[str, str]]]:
    rows = _yk_positive_rows(vid)
    if not rows:
        return None
    return [(f"第{i + 1}集", f"https://v.youku.com/v_show/id_{v}.html")
            for i, (_stage, v) in enumerate(rows)]


def _yk_positive_rows(vid: str) -> List[Tuple[int, str]]:
    """抓 m.youku.com 移动页，返回**正片全集** [(stage, vid)]，按 stage 保序。

    只认 videoType=="正片"（预告/花絮等其它值一律丢弃），**不按 stage 去重**：
    综艺的 stage 是播出日期（偏爱之恋 第二季 stage=20261021 有「第1期 下」和
    「偏爱日记」两条正片），去重会悄悄吞掉真视频。集号由调用方按顺序重编
    （第1..N 集），与无损云同源同数。剧集动漫 stage 本就 1..N 不重复，行为不变。

    只按 **vid** 去重：同一条视频在页面里被两个组件各列一次时不该算两集，
    而同一 stage 下的不同 vid 是真·不同条目，必须都留着。
    """
    mobile = f"https://m.youku.com/video/id_{vid}.html"
    rows: List[Tuple[int, str]] = []
    try:
        session = net.get_session()
        resp = session.get(mobile, timeout=15,
                           headers={"User-Agent": _YK_MOBILE_UA,
                                    "Referer": "https://m.youku.com/"})
        seen = set()
        for vt, s, v in _YK_STAGE_VID_RE.findall(resp.text):
            if vt == "正片" and v not in seen:
                seen.add(v)
                rows.append((int(s), v))
    except Exception:
        return []
    rows.sort(key=lambda x: x[0])   # 稳定排序：同 stage 保持页面出现顺序
    return rows


def _youku_show_name(entry_url: str) -> str:
    """优酷桌面页 v.youku.com 是 x5sec 反爬空壳（1.5KB、无 <title>），拿不到剧名；
    但移动页 m.youku.com 不挡，`__INITIAL_DATA__` 里带 showName、<title> 就是纯剧名。
    用 vid 打移动页取 showName，退回 <title>；失败返回 ''。"""
    m = _YK_DESKTOP_VID_RE.search(entry_url) or _YK_MOBILE_VID_RE.search(entry_url)
    if not m:
        return ""
    mobile = f"https://m.youku.com/video/id_{m.group(1)}.html"
    try:
        resp = net.get_session().get(
            mobile, timeout=15,
            headers={"User-Agent": _YK_MOBILE_UA, "Referer": "https://m.youku.com/"})
        # 移动页偶有无 charset 的 text/html，requests 会退回 latin-1 让中文乱码
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
            resp.encoding = "utf-8"
        s = _YK_SHOWNAME_RE.search(resp.text)
        if s:
            return s.group(1).strip()
        t = _YK_HTML_TITLE_RE.search(resp.text)
        return t.group(1).strip() if t else ""
    except Exception:
        return ""


def fetch_show_name(entry_url: str) -> str:
    """给定官方视频页 URL，尽力用纯 requests 返回真实剧名；拿不到返回 ''。

    第三方源解析的是页面本身（其 <title> 常是解析站名或反爬空壳），据此读到的
    标题不是剧名——这里改打官方数据源。优酷抓移动页 showName、腾讯取 pbaccess
    头部卡 mod_title；爱奇艺桌面页 og:title 静态可读，交给调用方的页面标题兜底。
    拿不到的站返回 ''。"""
    u = (entry_url or "").lower()
    if "youku.com" in u:
        return _youku_show_name(entry_url)
    if "v.qq.com" in u:
        return _tencent_show_name(entry_url)
    if "mgtv.com" in u:
        return _mg_show_name(entry_url)
    return ""


def _tx_list_eps(cid: str, vid: str) -> Optional[List[Tuple[str, str]]]:
    """腾讯整季：pbaccess 首页 + 逐 tab page_context 翻页合并 {集号: vid}。"""
    card = _tx_fetch_episode_card(cid, vid)
    if not card:
        return None
    eps: Dict[int, str] = dict(_tx_extract_eps(card))
    tabs = _tx_tabs(card)
    for tab in tabs:
        if tab.get("selected"):
            continue                     # 首页已取
        pc = tab.get("page_context", "")
        if not pc:
            continue
        card2 = _tx_fetch_episode_card(cid, vid, page_context=pc)
        if card2:
            eps.update(_tx_extract_eps(card2))
    if not eps:
        return None
    return [(f"第{n}集", f"https://v.qq.com/x/cover/{cid}/{eps[n]}.html")
            for n in sorted(eps)]



# ─────────────────────────────────────────────────────────────────────────────
# 对外主入口
# ─────────────────────────────────────────────────────────────────────────────
def find_page_url(
    title: str,
    episode: int = 0,
    prefer: Optional[str] = None,
    on_note: Optional[callable] = None,
) -> Optional[SiteHit]:
    """片名（可带集数）→ 官方视频页 URL；`prefer` 是**优先站**，不是硬限定。

    搜索实现在 `_scan_page_url`，这里只多包一层，把一件容易骗人的事说出来：
    指定了站点而那家没命中时，四家顺序会自动兜底到别家（`_scan_page_url` 里
    `sites.insert(0, prefer)` + 搜不到就 `continue`）。不提示的话，用户写
    `site 爱奇艺` 拿到的却是腾讯页，只有成功行里的站名泄露真相 —— 所以补一条 note。
    """
    note = on_note or (lambda _t: None)
    hit = _scan_page_url(title, episode=episode, prefer=prefer, on_note=note)
    if hit is not None and prefer in _SITES and hit.site != prefer:
        note(f"⚠ {prefer} 没搜到「{title}」，改用{hit.site}"
             f"（指定站无版权/未收录时会退回其它三家）")
    return hit


def _scan_page_url(
    title: str,
    episode: int = 0,
    prefer: Optional[str] = None,
    on_note: Optional[callable] = None,
) -> Optional[SiteHit]:
    """四家原生站内搜索：片名 → 官方入口页，并尽量精确定位到指定集。

    策略：先用**不带集数**的查询稳定拿到 cover/专辑页（各家原生接口带集数时召回
    明显变差，窄查询容易只命中花絮/二创页而漏掉整季入口），再按各家能力定位到那一集：
      爱奇艺  解析专辑页里静态内嵌的整季 /v_xxx.html → 精确取第 N 集单集页；
      优酷    借道 m.youku.com 移动端页面（不吃 x5sec 盾），从 __INITIAL_DATA__
              内嵌 JSON 里按 stage 精确取第 N 集 vid → 拼回桌面 v_show URL；
      腾讯    纯 requests POST pbaccess.getPage（逆向 JS bundle 得到精确请求体），
              从返回 CardList 里 pc_web_episode_list 卡按集号取 vid → 拼桌面 cover URL。
              超 30 集走 tabs 翻页。
      芒果    官方分集接口 pcweb.api.mgtv.com/episode/list 按集号取单集页 URL
              （只认 isIntact=="1" 的完整正片）；入口页由片库索引给出（见 _mg_native_urls）。
    四家均可精确到集，枚举失败时退回 cover 页 —— 绝不因集数搜不到就返回 None。

    是否**真的**定位到了那一集，用 SiteHit.episode_pinned 如实标出：枚举命中 → True；
    枚举失败（网络异常/超总集数）退回整季 cover 页 → False。

    `prefer` 指定优先站点（"腾讯视频"/"爱奇艺"/"优酷"/"芒果TV"），None 时四家按顺序尝试。
    `on_note(text)` 把「正在搜哪家」这类进度回吐给调用方（CLI 走 ui.toast）。
    """
    note = on_note or (lambda _t: None)
    sites = list(_SITES)
    if prefer in _SITES:
        sites.remove(prefer)
        sites.insert(0, prefer)

    for site in sites:
        note(f"正在 {site} 站内搜索「{title}」...")
        # 主干：不带集数，先稳稳拿到 cover/专辑页
        base = _site_urls(title, site)
        if not base:
            continue

        album = base[0]

        # 爱奇艺：只有专辑页 /a_ 静态内嵌整季「第N集」选集、且 <title> 是真剧名；
        # 单集页 /v_ 用 requests 抓到的是 JS 空壳（无选集、无 og:title、无 /a_ 反推线索）。
        # 原生接口给的基本都是 /a_，但结果顺序不保证，故这里仍在同批结果里优先挑 /a_
        # 当整季入口，找不到 /a_ 才退回 base[0]（此时枚举最多给 1 集，但不至于完全解析不出）。
        if site == "爱奇艺":
            for a in base:
                if _IQY_ALBUM_RE.search(a):
                    album = a
                    break

        # 爱奇艺：专辑页静态内嵌整季 → 精确取单集页（能真正定位）。
        # 关键：解析源（LX 等）解析的是**具体视频页**，专辑页 /a_ 不是可播页
        # （实测 LX 解析 /a_ 返回 None）。所以即便用户没指定集数（episode==0），
        # 初始播放 URL 也要落到真实的第1集 /v_ 页，season_url 仍留 /a_ 供整季枚举，
        # 否则起播那一下（第1集）会解析失败。
        if site == "爱奇艺":
            target = episode if episode > 0 else 1
            single = _iqiyi_episode_url(album, target)
            if single:
                return SiteHit(site=site, url=single, episode=episode,
                               episode_pinned=(episode > 0), season_url=album)

        # 优酷：借道 m.youku.com（不挡反爬）拿 __INITIAL_DATA__ 里的 (stage,vid) 配对
        # → 精确取单集页 → 拼回桌面 v_show URL。桌面页自己被 x5sec 挡但移动页畅通。
        # 同爱奇艺：解析源解析的是具体视频页，初始 URL 用真实单集页（episode==0 取第1集），
        # season_url 留整季入口页供枚举。
        if site == "优酷":
            target = episode if episode > 0 else 1
            single = _youku_episode_url(album, target)
            if single:
                return SiteHit(site=site, url=single, episode=episode,
                               episode_pinned=(episode > 0), season_url=album)
            if episode > 0:
                note(f"优酷 第{episode}集 枚举未命中（可能超出总集数或页面结构变化）,退回整季页")

        # 腾讯：纯 requests POST pbaccess.getPage → CardList 里 pc_web_episode_list 卡
        # → 按集号精确取 vid → 拼桌面 cover URL。超 30 集走 tabs page_context 翻页。
        # 同爱奇艺：cid-only cover 页不可解析（实测 LX 解析 /x/cover/<cid>.html 返回 None），
        # 初始 URL 必须用带 vid 的真实单集页；season_url 留 cover 页供枚举整季。
        if site == "腾讯视频":
            target = episode if episode > 0 else 1
            single = _tencent_episode_url(album, target)
            if single:
                return SiteHit(site=site, url=single, episode=episode,
                               episode_pinned=(episode > 0), season_url=album)
            if episode > 0:
                note(f"腾讯 第{episode}集 枚举未命中（可能超总集数或 API 异常）,退回整季页")

        # 芒果TV：/b 播放页本身可解析（第三方源直解官方页），整季走分集接口枚举。
        # 同三家：episode==0 起播也落到真实的第1集单集页，season_url 留入口页供枚举。
        if site == "芒果TV":
            target = episode if episode > 0 else 1
            single = _mg_episode_url(album, target)
            if single:
                return SiteHit(site=site, url=single, episode=episode,
                               episode_pinned=(episode > 0), season_url=album)
            if episode > 0:
                note(f"芒果TV 第{episode}集 枚举未命中（可能超总集数或接口异常）,退回整季页")

        # 走到这里：要么没要集数（episode==0，pinned 记 True），
        # 要么没能把某一集精确切出来（退回整季页，pinned=False）。
        return SiteHit(site=site, url=album, episode=episode,
                       episode_pinned=(episode == 0), season_url=album)

    return None


def _site_urls(query: str, site: str) -> List[str]:
    """片名 → 该站点 url_supported 认可的官方页列表（保序去重）。

    只走**各家原生搜索接口**，不经任何第三方搜索引擎：原生 JSON 接口直接给官方页
    URL 与整季元数据，比 Bing `site:` 站内搜索更准，也不会混入别家站/镜像域/过期页。
    因此**空结果是有含义的**——该站确实无此片（芒果走片库全量索引，见 _mg_native_urls），
    调用方据此换站，不再做二次兜底。
    """
    fn = _NATIVE_SEARCHERS.get(site)
    if fn is None:
        return []
    try:
        urls = fn(query)
    except Exception:
        urls = []
    return [u for u in dict.fromkeys(urls) if url_supported(u)]


# ─────────────────────────────────────────────────────────────────────────────
# 原生搜索：四站实测纯 requests 可通（见各函数注释）
# ─────────────────────────────────────────────────────────────────────────────
_NUA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# 标题归一化统一放 title.py（wsyzy 候选排序也用同一套），这里只做名字转发，
# 避免两处各写一份正则导致口径漂移。模块用 _title_mod 别名：本文件里 title
# 是到处都有的局部变量名，直接 import title 会被遮蔽。
_strip_mark = _title_mod.strip_mark
_norm_title = _title_mod.norm_title


# 二创/衍生内容的标题特征词。站内常有「XX 深度解读」这类**带正规年份、
# 也在本站可播**的短视频专辑，光靠域名和年份挡不住，会被误当正片返回。
_NOISE_TITLE_RE = re.compile(
    r"解读|解析|赏析|解说|速看|一口气|花絮|预告|幕后|番外|合集|纯享|"
    r"揭秘|访谈|采访|切片|精彩片段|名场面|盘点|测评|reaction|cut")


def _is_noise_title(norm: str) -> bool:
    return bool(norm) and bool(_NOISE_TITLE_RE.search(norm))


def _rank_by_title(query: str, pairs: "list[tuple[str, str]]") -> List[str]:
    """[(候选标题, url)] → 按匹配度排序的 url 列表。

    分级：归一化后完全相等 → 包含（且非二创）→ 二创/衍生（垫底但保留，
    聊胜于无）。都不沾边的直接剔除——这是**防错配的关键**：实测优酷搜
    「以家人之名」时，站内唯一可播的是另一部剧《以子之名》，只按域名过滤
    会把它当结果交出去。
    """
    nq = _norm_title(query)
    exact, loose, noise = [], [], []
    for cand, url in pairs:
        nc = _norm_title(cand)
        if not nc or not url:
            continue
        if nc == nq:
            exact.append(url)
        elif nq in nc or nc in nq:
            (noise if _is_noise_title(nc) else loose).append(url)
    return exact + loose + noise


def _native_get(url: str, params: dict, headers: Optional[dict] = None) -> dict:
    """GET 原生 JSON 接口 → dict（非 JSON / 异常一律抛给上层兜底）。"""
    resp = net.get_session().get(url, params=params, timeout=15,
                                 headers={"User-Agent": _NUA, **(headers or {})})
    return resp.json()


# ── 爱奇艺 ──────────────────────────────────────────────────────────────────
IQY_SEARCH = "https://search.video.iqiyi.com/o"
# 正片频道：排除教育/短剧/漫剧等噪声频道（实测这些占了大量结果且非可播长片）
_IQY_POS_CH = ("电视剧", "电影", "动漫", "少儿", "综艺", "纪录片")


def _iqy_native_urls(query: str) -> List[str]:
    """爱奇艺原生搜索 search.video.iqiyi.com/o?if=html5。

    返回 data.docinfos[].albumDocInfo，含 albumTitle / albumLink / channel /
    itemTotalNumber —— albumLink 就是专辑页 /a_xxx.html，可直接交给下游枚举。

    两个必须处理的坑：
      1. 该接口是**聚合搜索**，会返回 siteId=qq/youku/mgtv 的外站结果；外站条目的
         albumLink 是 so.iqiyi.com/links/<base64> 短链，实测跟随重定向后**跳回爱奇艺
         首页**，拿不到真实外站 URL —— 所以只认 siteId=="iqiyi"。
      2. videoinfos[] 只给前 20 集（狂飙 39 集只返回 1-20），且其 itemLink 同样是
         死短链 —— 所以整季枚举仍走专辑页 HTML（_iqy_list_eps），这里只负责找入口。
    """
    data = _native_get(IQY_SEARCH, {"if": "html5", "key": query,
                                    "num": "20", "pageNum": "1"})
    docs = ((data.get("data") or {}).get("docinfos")) or []
    pairs: List[Tuple[str, str]] = []
    for doc in docs:
        a = doc.get("albumDocInfo") or {}
        if a.get("data_type") != 1 or a.get("siteId") != "iqiyi":
            continue
        ch = str(a.get("channel") or "")
        if not any(c in ch for c in _IQY_POS_CH):
            continue
        link = str(a.get("albumLink") or "").replace("http://", "https://", 1)
        if link:
            pairs.append((str(a.get("albumTitle") or ""), link))
    return _rank_by_title(query, pairs)


# ── 优酷 ────────────────────────────────────────────────────────────────────
YK_SEARCH = "https://search.youku.com/api/search"


def _yk_native_urls(query: str) -> List[str]:
    """优酷原生搜索 search.youku.com/api/search。

    pageComponentList[].commonData 里 leftButtonDTO.action.value **直接就是完整
    播放页 URL**（实测 v_show/id_X….html 200、不吃 x5sec、url_supported=True），
    一步顶掉旧实现的两跳（Bing 搜入口页 → 抓移动页解析 __INITIAL_DATA__）。

    两道过滤缺一不可：
      1. 域名：commonData.isYouku 为真但 hasYouku 为 0 时（剧只有别家分销、优酷无
         片源），leftButtonDTO 会返回**别家站点的 URL**（实测搜「狂飙」给
         www.iqiyi.com/v_….html），只留含 youku.com 的。
      2. 标题：优酷会把**字面相近的另一部剧**当站内结果给出——实测搜「以家人之名」
         返回《以子之名》(isYouku=1 hasYouku=1)，搜「庆余年第二季」站内只有
         《〈庆余年第二季〉深度解读》。光靠域名挡不住，必须比对 titleDTO.displayName。
    """
    data = _native_get(YK_SEARCH, {"keyword": query, "pg": "1"})
    pairs: List[Tuple[str, str]] = []
    for comp in (data.get("pageComponentList") or []):
        cd = comp.get("commonData") or {}
        if not cd.get("showId"):
            continue
        link = str(((cd.get("leftButtonDTO") or {}).get("action") or {})
                   .get("value") or "")
        if "youku.com" not in link:
            continue
        tdo = cd.get("titleDTO") or {}
        pairs.append((str(tdo.get("displayName") or ""), link))
    return _rank_by_title(query, pairs)


# ── 腾讯视频 ────────────────────────────────────────────────────────────────
TX_SEARCH = ("https://pbaccess.video.qq.com/trpc.videosearch.mobile_search."
             "MultiTerminalSearch/MbSearch?vversion_platform=1")
# episodeInfoList 里的单集页：/x/cover/<cid>/<vid>.html（组1=cid，组0=整条 URL）
_TX_EP_URL_RE = re.compile(
    r"^https://v\.qq\.com/x/cover/([A-Za-z0-9]+)/[A-Za-z0-9]+\.html")


def _tx_native_urls(query: str) -> List[str]:
    """腾讯原生搜索 POST MbSearch（从 v.qq.com/x/search 的 JS bundle 逆向）。

    旧结论「vd.l.qq.com/proxyhttp 要未公开的 buid」已过时：真正的搜索接口是
    trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch，**POST 且无需
    buid/签名**（bundle 里 `SEARCH_URL="...HttpMobileRecall/MbSearchHttp"` 之后
    被重新赋值成这个，请求体用 snake_case：query / pagenum / pagesize）。

    正片不在 normalList 而在 **areaBoxList**（MainNeed 主需求卡，dataType=5000 的
    doc.areaBoxIndex 指向它），两处都要扫。正片项判据三条全中才算：
    doc.dataType==2、videoInfo.title 去标签后与片名相符、videoInfo.year>0（剔 PGC
    搬运号，见下）。命中后 doc.id 即 cid。

    episodeSites/playSites[].episodeInfoList[] 直接给整季「集号 → cover URL」，
    但**只给首页约 24 集**且 enName 混有外站（qq 之外的分销源），故这里只取入口页，
    整季枚举仍交给已打通的 pbaccess.getPage（见 _tx_list_eps）。
    """
    resp = net.get_session().post(
        TX_SEARCH, json={"query": query, "pagenum": 0, "pagesize": 20},
        timeout=15, headers={"User-Agent": _NUA, "Referer": "https://v.qq.com/",
                             "Origin": "https://v.qq.com"})
    data = (resp.json().get("data") or {})
    items: List[dict] = []
    for box in (data.get("areaBoxList") or []):
        items.extend(box.get("itemList") or [])
    items.extend(((data.get("normalList") or {}).get("itemList")) or [])

    pairs: List[Tuple[str, str]] = []
    for it in items:
        doc = it.get("doc") or {}
        if doc.get("dataType") != 2:
            continue
        cid = str(doc.get("id") or "")
        if not cid:
            continue
        vi = it.get("videoInfo") or {}
        title = _strip_mark(vi.get("title"))
        # 只认**官方专辑**：PGC 自频道（粉丝搬运/合集）year 恒为 0，官方专辑必有
        # 上映年。实测《凡人修仙传》腾讯只有 mzc003 开头的 PGC 号（typeName=动漫
        # year=0，127 集且标题是「凡人修仙传元婴开始第一集」这类解说名），
        # 《声生不息》返回的是 year=0 的奥运歌曲合集（typeName=体育）——这类页
        # pbaccess.getPage 取不到选集（mod_title=「专辑列表」、枚举 None），
        # 第三方也解析不出，给出去等于给坏链，故一律剔除。
        try:
            year = int(vi.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if year <= 0:
            continue
        # 取一个**带 vid** 的 cover 页当入口：解析源要的是具体视频页，
        # 光有 cid 的 /x/cover/<cid>.html 第三方解析返回 None（见 find_page_url 注释）。
        # 集号标签形态不一——剧集常为纯数字（'1'），动漫/综艺是长句
        # （'凡人修仙传元婴开始第一集'），电影只有 playSites 的「立即播放」；
        # 故只要求 URL 形如 /x/cover/<cid>/<vid>.html，纯数字的优先（天然是第1集）。
        num_url = any_url = ""
        for fld in ("episodeSites", "playSites"):
            for src in (vi.get(fld) or []):
                if src.get("enName") != "qq":
                    continue                     # 只认站内源，别把分销站 URL 当腾讯结果
                for ep in (src.get("episodeInfoList") or []):
                    m = _TX_EP_URL_RE.match(str(ep.get("url") or ""))
                    if not m or m.group(1) != cid:
                        continue
                    u = m.group(0)
                    if not any_url:
                        any_url = u
                    if str(ep.get("title") or "").strip().isdigit() and not num_url:
                        num_url = u
                if num_url:
                    break
            if num_url:
                break
        url = num_url or any_url
        if not url:
            # 该项没有站内(qq)片源 —— 多为分销/待播/预热（实测《水龙吟》在腾讯只有
            # hunantv/qiyi 源，URL 是 search_redirect.html 跳板）。这种 cid 交给
            # pbaccess.getPage 也枚举不出集（vid 为空 → card=None），给出去只会让
            # 第三方解析失败，不如判定本站无此片，让上层继续试下一家。
            continue
        # 同级别内「集号可枚举」的优先：实测《长相思》返回的多条同名结果里，
        # 排最前那条 getPage 枚举不出集（0 集），第二条却有 62 集；把能枚举的
        # 提前，下游「共 N 集」才不会莫名变空。
        pairs.append((title, url, 0 if num_url else 1))
    pairs.sort(key=lambda p: p[2])          # 稳定排序，同级保持接口原序
    return _rank_by_title(query, [(t, u) for t, u, _k in pairs])


# ── 芒果TV ──────────────────────────────────────────────────────────────────
# 芒果**没有「按片名查专辑」的接口**，所以这里不走搜索，改走官方片库目录：
#   GET pianku.api.mgtv.com/rider/list/pcweb/v3 —— 专辑粒度的权威目录，每条
#   hitDocs 自带 clipId + playPartId（就是可播 vid），拼出 /b/<cid>/<vid>.html。
# 三个实测要点（都是踩出来的）：
#   1. 分页参数是 **pn**。前端自己发的 pagedNo 是假参数：传 2..40 拿到的是同一批。
#   2. pageSize 被服务端硬锁 36（传 1000 也只给 36），所以只能靠 pn 逐页翻。
#   3. 目录不认片名（keyword/q/name/title 等 12 个参数名全部被忽略）——只能整库
#      枚举建索引。三频道 totalHits 合计约 6100 条 / 170 页，8 并发约 3~5s，
#      进程内建一次（_mg_lib_index），此后片名→URL 是纯本地查表。
# 弃用 mobileso.bz.mgtv.com/pc/search/v1（src=intelmgtv 免签）的原因：它是
# **视频粒度的模糊推荐源**，实测 14 部剧里只有 3 部给出可播正片，其余返回花絮/AI
# 二创/无关综艺（《难哄》给快乐大本营、《承欢记》给辟谣新闻），且正片 cid 常常根本
# 不在结果里（《与凤行》626407 片库有、搜索从不给）。
MG_LIB_API = "https://pianku.api.mgtv.com/rider/list/pcweb/v3"
_MG_LIB_PAGE_SIZE = 36                 # 服务端硬锁，调大无效
_MG_LIB_CHANNELS = ("2", "1", "3")     # 电视剧 / 综艺 / 电影
_MG_LIB_MAX_PAGES = 120                # 翻页保险
_MG_LIB_WORKERS = 8
# {归一化片名: (cid, vid)} 的进程内缓存；大写是因为小写名已被同名函数占用
_MG_LIB_INDEX: Optional[Dict[str, Tuple[str, str]]] = None
_MG_LIB_LOCK = threading.Lock()


def _mg_play_url(cid: str, vid: str) -> str:
    return f"https://www.mgtv.com/b/{cid}/{vid}.html"


def _mg_lib_page(channel_id: str, pn: int) -> Tuple[list, int, bool]:
    """片库目录一页 → (hitDocs, totalHits, hasMore)；异常软失败为 ([], 0, False)。"""
    try:
        data = _native_get(
            MG_LIB_API,
            {"partnerId": "8", "channelId": channel_id, "pluginId": "101",
             "kind": "a1", "year": "all", "area": "all", "sort": "c",
             "pageSize": str(_MG_LIB_PAGE_SIZE), "pn": str(pn)},
            headers={"Referer": f"https://www.mgtv.com/lib/{channel_id}"})
    except Exception:
        return [], 0, False
    d = (data.get("data") or {}) if isinstance(data, dict) else {}
    return (d.get("hitDocs") or []), int(d.get("totalHits") or 0), bool(d.get("hasMore"))


def _mg_lib_build() -> Dict[str, Tuple[str, str]]:
    """并发翻完三频道，建 {归一化片名: (cid, vid)}。

    sort=c 是更新时间倒序，同名保留先遇到的（= 最新一季）；频道按 _MG_LIB_CHANNELS
    顺序合并，故电视剧优先。三频道首页全挂即认定接口不可达，直接空手返回、不空转
    翻页；这种失败不进缓存，下次调用会重试。
    """
    first = {ch: _mg_lib_page(ch, 1) for ch in _MG_LIB_CHANNELS}
    if not any(h for h, _t, _m in first.values()):
        return {}
    docs: Dict[Tuple[str, int], list] = {
        (ch, 1): list(h) for ch, (h, _t, _m) in first.items()}
    todo: List[Tuple[str, int]] = []
    for ch, (_h, total, has_more) in first.items():
        if has_more and total > _MG_LIB_PAGE_SIZE:
            pages = min(-(-total // _MG_LIB_PAGE_SIZE), _MG_LIB_MAX_PAGES)
            todo += [(ch, p) for p in range(2, pages + 1)]
    if todo:
        with ThreadPoolExecutor(max_workers=_MG_LIB_WORKERS,
                                thread_name_prefix="mglib") as pool:
            for key, more in pool.map(
                    lambda k: (k, _mg_lib_page(k[0], k[1])[0]), todo):
                docs[key] = more
    idx: Dict[str, Tuple[str, str]] = {}
    for ch in _MG_LIB_CHANNELS:
        for pn in sorted(p for c, p in docs if c == ch):
            for h in docs[(ch, pn)]:
                name = _norm_title(h.get("title"))
                cid, vid = h.get("clipId"), h.get("playPartId")
                if name and cid and vid:
                    idx.setdefault(name, (str(cid), str(vid)))
    return idx


def _mg_lib_index() -> Dict[str, Tuple[str, str]]:
    """片库全量索引，进程内建一次；空结果不进缓存（留给网络恢复后重试）。"""
    global _MG_LIB_INDEX
    if _MG_LIB_INDEX is not None:
        return _MG_LIB_INDEX
    with _MG_LIB_LOCK:
        if _MG_LIB_INDEX is None:
            built = _mg_lib_build()
            if built:
                _MG_LIB_INDEX = built
        return _MG_LIB_INDEX or {}


def _mg_native_urls(query: str) -> List[str]:
    """片名 → 芒果官方播放页：查片库权威索引，不做模糊搜索。

    候选来自目录而非推荐源，所以分级比 `_rank_by_title` **更严**：只认归一化全等，
    外加「目录名以查询词起头」的写法差异（声生不息 → 声生不息·港乐季）。刻意不用
    `_rank_by_title` 的反向内包含（nc in nq）——查询词「庆余年第二季」会因此匹上
    目录里的「庆余年」第一季；也不用双向子串——「狂飙」会匹上短视频专辑《…金币
    狂飙》。全等排在起头之前，故搜「以家人之名」先给正片 333900 而非衍生篇 343225。

    查不到返回空列表。走通这条链路后目录就是全库，这个空结果因此成了**可信判定**：
    芒果确实没这部剧 —— 不再需要 Bing 兜底去猜（旧兜底捞回的正是搜索那些花絮页）。
    """
    idx = _mg_lib_index()
    nq = _norm_title(query)
    if not nq:
        return []
    exact, sequel = [], []
    for name, (cid, vid) in idx.items():
        if name == nq:
            exact.append(_mg_play_url(cid, vid))
        elif name.startswith(nq) and not _is_noise_title(name):
            sequel.append(_mg_play_url(cid, vid))
    return exact + sequel


_NATIVE_SEARCHERS: Dict[str, "callable"] = {
    "爱奇艺": _iqy_native_urls,
    "优酷": _yk_native_urls,
    "腾讯视频": _tx_native_urls,
    "芒果TV": _mg_native_urls,
}
