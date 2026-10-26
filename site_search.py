"""site_search.py —— 路线 A：只给片名/集数时，为第三方解析源找到「一个具体视频页 URL」。

背景：第三方解析源（LX / 七哥 / TXNQ / 789 / 冰豆 / 麒麟）拼的是
`source.url + 视频页URL`（见 consumer.show_player_window），它们解析的是**一个具体
视频页**，不是片名 —— 所以 `--title` 只给片名时物理上用不了（README 里那段告警）。
这个模块补上「片名 → 官方视频页 URL」这一步，让 `--title … -s 2` 也能走第三方源。

为什么用 Bing 站内搜索而不是各家原生搜索接口：实测本机环境下三家的原生搜索都不可用
（腾讯视频 vd.l.qq.com/proxyhttp 要未公开的 buid；爱奇艺 suggest 子域连不上、主站是
纯 JS 壳；优酷 so.youku.com 返回 JS 跳转壳）。而 Bing 的 `site:` 站内搜索能稳定返回
真实官方页 URL（腾讯 /cover/、爱奇艺 /a_、优酷 /v_show/），且不依赖额外鉴权。

三家覆盖（都是 INCLUDE_PATTERNS / PRECISE 已支持的官方主站）：
    腾讯视频  v.qq.com/x/cover/<cid>/<vid>.html
    爱奇艺    www.iqiyi.com/a_<album>.html（专辑页）→ /v_<vid>.html（单集页）
    优酷      v.youku.com/v_show/id_<vid>.html

集数处理分三档（能力不同，落地路径也不同）：
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
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from . import net
from .config import url_supported

BING_SEARCH = "https://www.bing.com/search"

# 每家：结果页 URL 的识别正则（要能同时容纳腾讯 /cover、爱奇艺 /a_ /v_、优酷 id_X==）
_SITE_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "腾讯视频": re.compile(
        r"https?://v\.qq\.com/x/(?:cover|page)/[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?(?:\.html)?"
    ),
    "爱奇艺": re.compile(
        r"https?://www\.iqiyi\.com/[va]_[A-Za-z0-9]+\.html"
    ),
    "优酷": re.compile(
        # 优酷 v_show 内容同时挂在 play.tudou.com（阿里大文娱同库、同 vid）；实测
        # 2026-10 起 Bing 对 site:youku.com 的 v_show 结果整体改返回 play.tudou.com
        # 镜像域（含《难哄》这类优酷独播剧），故两域都认，抽出的 vid 再拼回 v.youku.com。
        r"https?://(?:v\.youku\.com|play\.tudou\.com)/v_show/id_[A-Za-z0-9=_]+\.html"
    ),
    "芒果TV": re.compile(
        r"https?://www\.mgtv\.com/b/\d+/\d+\.html"
    ),
}

# 爱奇艺专辑页里的单集链接（用于按集号精确定位）
_IQIYI_EPS_RE = re.compile(r"https?://www\.iqiyi\.com/(v_[A-Za-z0-9]+)\.html")
_IQY_ALBUM_RE = re.compile(r"/a_([A-Za-z0-9]+)\.html")
# 专辑页里的 <a href="/v_xxx.html">…</a>：连同锚文本一起抓，用于区分正片与噪声。
_IQY_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?href="[^"]*?(v_[A-Za-z0-9]+)\.html"[^>]*?>(.*?)</a>', re.S | re.I)
# 正片锚文本恰为「第N集」（去掉嵌套标签与空白后整体匹配）
_IQY_EP_LABEL_RE = re.compile(r"第(\d{1,4})集")


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
# Bing 结果提取：同时处理直链 href 与 ck/a base64 重定向，外加整页兜底
# ─────────────────────────────────────────────────────────────────────────────
def _decode_bing_href(href: str) -> str:
    """Bing 有时把结果链接包成 `/ck/a?...&u=a1<base64url>`，`a1` 前缀后跟的是
    真实 URL 的 base64url 编码。不是这种形式就原样返回。"""
    m = re.search(r"[?&]u=(a1[^&\"]+)", href)
    if not m:
        return href
    b64 = m.group(1)[2:]                 # 去掉 "a1" 前缀
    b64 += "=" * (-len(b64) % 4)         # 补 base64 padding
    try:
        return base64.urlsafe_b64decode(b64).decode("utf-8", "ignore")
    except Exception:
        return href


def _extract_urls(html: str, pattern: "re.Pattern[str]") -> List[str]:
    """从 Bing 结果 HTML 里提取匹配 pattern 的官方页 URL。

    两条来源合并：① 结果标题链接 `<h2>...<a href=...>`（可能直链、可能 ck/a 重定向，
    统一解码后匹配）；② 整页正则兜底（Bing 偶尔把 URL 直接写进跳转脚本里）。
    保序去重。
    """
    out: List[str] = []

    def add(u: str) -> None:
        u = u.rstrip("\\/").rstrip("/") if u.endswith("/") else u
        # 腾讯 cover 页统一补成 .html（Bing 有时截掉后缀）
        if "v.qq.com/x/cover/" in u and not u.endswith(".html"):
            u = u + ".html"
        if pattern.fullmatch(u) or pattern.search(u):
            m = pattern.search(u)
            if m and m.group(0) not in out:
                out.append(m.group(0))

    for raw in re.findall(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"", html):
        add(_decode_bing_href(raw))
    for m in pattern.finditer(html):
        add(_decode_bing_href(m.group(0)))
    return out


def _bing_search(query: str, retries: int = 3, pause: float = 1.5) -> str:
    """拉一次 Bing 搜索页；限流（代理重置 / 空结果）时退避重试，返回 HTML。"""
    session = net.get_session()
    last = ""
    for attempt in range(retries):
        try:
            resp = session.get(
                BING_SEARCH,
                params={"q": query, "count": "15"},
                timeout=15,
            )
            last = resp.text
        except Exception:
            last = ""
        if last and "<h2" in last:
            return last
        time.sleep(pause)
    return last


# ─────────────────────────────────────────────────────────────────────────────
# 选集枚举：三家均可纯 requests 按集号精确定位单集页
# ─────────────────────────────────────────────────────────────────────────────
def _iqiyi_episode_url(album_url: str, episode: int) -> Optional[str]:
    """爱奇艺按集号精确定位单集页。专辑页 HTML 里除正片还混有推荐/预告，
    故只认锚文本「第N集」的正片映射，按集号取；页面不用该锚文本时退回顺序取。"""
    if episode <= 0:
        return None
    try:
        html = _iqy_fetch_html(album_url)
    except Exception:
        return None
    eps = _iqy_positive_vids(html)
    if eps:
        vid = eps.get(episode)
        return f"https://www.iqiyi.com/{vid}.html" if vid else None
    # 兜底：页面结构不含「第N集」锚文本时，按出现顺序取第 N 个 /v_ 链接
    vids = list(dict.fromkeys(_IQIYI_EPS_RE.findall(html)))
    if vids and episode <= len(vids):
        return f"https://www.iqiyi.com/{vids[episode - 1]}.html"
    return None


def _youku_episode_url(entry_url: str, episode: int) -> Optional[str]:
    """优酷按集号精确定位。桌面页 v.youku.com 会吃 x5sec 反爬盾（返 1.5KB 空壳），
    但**移动端 m.youku.com 不挡**，页面 window.__INITIAL_DATA__ 里内嵌整季
    (stage, vid) 配对；抓一次移动端页面，按 stage==episode 取 vid，再拼回桌面
    版单集 URL 交给下游解析源使用。
    """
    if episode <= 0:
        return None
    m = _YK_DESKTOP_VID_RE.search(entry_url) or _YK_MOBILE_VID_RE.search(entry_url)
    if not m:
        return None
    vid = m.group(1)
    mobile = f"https://m.youku.com/video/id_{vid}.html"
    try:
        session = net.get_session()
        resp = session.get(mobile, timeout=15,
                           headers={"User-Agent": _YK_MOBILE_UA,
                                    "Referer": "https://m.youku.com/"})
        pairs: dict = {}
        for vt, s, v in _YK_STAGE_VID_RE.findall(resp.text):
            # 只认正片；同一 stage 万一重复出现正片，保留第一条
            if vt == "正片":
                pairs.setdefault(int(s), v)
    except Exception:
        return None
    target = pairs.get(episode)
    if not target:
        return None
    return f"https://v.youku.com/v_show/id_{target}.html"


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
    tabs = json.loads(card.get("params", {}).get("tabs", "[]"))
    for tab in tabs:
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
    # 爱奇艺单集页 /v_ 无法反推整季，只给这一集（保持「1 集」而非报错）
    if re.search(r'www\.iqiyi\.com/(v_[A-Za-z0-9]+)\.html', entry_url):
        return [("第1集", entry_url)]
    if _MG_PAGE_RE.search(entry_url):
        return _mg_list_eps(entry_url)
    return None


def _iqy_list_eps(album_url: str) -> Optional[List[Tuple[str, str]]]:
    try:
        html = _iqy_fetch_html(album_url)
    except Exception:
        return None
    eps = _iqy_positive_vids(html)
    if eps:
        return [(f"第{n}集", f"https://www.iqiyi.com/{eps[n]}.html")
                for n in sorted(eps)]
    # 兜底：页面结构不含「第N集」锚文本时，退回按出现顺序枚举全部 /v_ 链接
    vids = list(dict.fromkeys(_IQIYI_EPS_RE.findall(html)))
    if not vids:
        return None
    return [(f"第{i + 1}集", f"https://www.iqiyi.com/{v}.html")
            for i, v in enumerate(vids)]


def _yk_list_eps(vid: str) -> Optional[List[Tuple[str, str]]]:
    pairs = _yk_positive_pairs(vid)
    if not pairs:
        return None
    return [(f"第{s}集", f"https://v.youku.com/v_show/id_{pairs[s]}.html")
            for s in sorted(pairs)]


def _yk_positive_pairs(vid: str) -> Dict[int, str]:
    """抓 m.youku.com 移动页，返回 {stage: vid}（仅 videoType==正片，同 stage 取首条）。"""
    mobile = f"https://m.youku.com/video/id_{vid}.html"
    pairs: Dict[int, str] = {}
    try:
        session = net.get_session()
        resp = session.get(mobile, timeout=15,
                           headers={"User-Agent": _YK_MOBILE_UA,
                                    "Referer": "https://m.youku.com/"})
        for vt, s, v in _YK_STAGE_VID_RE.findall(resp.text):
            if vt == "正片":
                pairs.setdefault(int(s), v)
    except Exception:
        return {}
    return pairs


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
    try:
        tabs = json.loads(card.get("params", {}).get("tabs", "[]"))
    except Exception:
        tabs = []
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
    """片名（可带集数）→ 官方视频页 URL。

    策略：先用**不带集数**的查询稳定拿到 cover/专辑页（实测带「第N集」的窄查询 Bing
    经常一条不返回，不能拿它当主干），再按各家能力定位到那一集：
      爱奇艺  解析专辑页里静态内嵌的整季 /v_xxx.html → 精确取第 N 集单集页；
      优酷    借道 m.youku.com 移动端页面（不吃 x5sec 盾），从 __INITIAL_DATA__
              内嵌 JSON 里按 stage 精确取第 N 集 vid → 拼回桌面 v_show URL；
      腾讯    纯 requests POST pbaccess.getPage（逆向 JS bundle 得到精确请求体），
              从返回 CardList 里 pc_web_episode_list 卡按集号取 vid → 拼桌面 cover URL。
              超 30 集走 tabs 翻页。
    三家均可精确到集，枚举失败时退回 cover 页 —— 绝不因集数搜不到就返回 None。

    是否**真的**定位到了那一集，用 SiteHit.episode_pinned 如实标出：三家枚举命中 → True；
    枚举失败（网络异常/超总集数）退回整季 cover 页 → False。

    `prefer` 指定优先站点（"腾讯视频"/"爱奇艺"/"优酷"），None 时三家按顺序尝试。
    `on_note(text)` 把「正在搜哪家」这类进度回吐给调用方（CLI 走 ui.toast）。
    """
    note = on_note or (lambda _t: None)
    sites = list(_SITE_PATTERNS.keys())
    if prefer in _SITE_PATTERNS:
        sites.remove(prefer)
        sites.insert(0, prefer)

    for site in sites:
        pattern = _SITE_PATTERNS[site]
        note(f"正在 {site} 站内搜索「{title}」...")
        # 主干：不带集数，先稳稳拿到 cover/专辑页
        base = _site_urls(title, site, pattern)
        if not base:
            continue

        album = base[0]

        # 爱奇艺：只有专辑页 /a_ 静态内嵌整季「第N集」选集、且 <title> 是真剧名；
        # 单集页 /v_ 用 requests 抓到的是 JS 空壳（无选集、无 og:title、无 /a_ 反推线索）。
        # Bing 可能把 /v_ 排在 /a_ 前面，故这里在同批结果里优先挑 /a_ 当整季入口，
        # 找不到 /a_ 才退回 base[0]（此时枚举最多给 1 集，但不至于完全解析不出）。
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


def _site_urls(query: str, site: str, pattern: "re.Pattern[str]") -> List[str]:
    """一次 Bing 搜索 → 该站点 url_supported 认可的官方页列表（保序去重）。"""
    html = _bing_search(f"{query} site:{_site_domain(site)}")
    return list(dict.fromkeys(u for u in _extract_urls(html, pattern) if url_supported(u)))



def _site_domain(site: str) -> str:
    return {
        "腾讯视频": "v.qq.com",
        "爱奇艺": "iqiyi.com",
        "优酷": "youku.com",
        "芒果TV": "mgtv.com",
    }.get(site, "v.qq.com")
