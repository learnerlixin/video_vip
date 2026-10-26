"""jx_resolve.py —— 把第三方解析源「抽」成可下载的真·媒体直链。

背景：第三方源每条每集链接是 `source.url + 官方页URL`，那本身是一个 HTML 播放页
（不是流），直接喂给 download.py 的 parse_hls 会失败。真正出流的那步发生在浏览器
JS 里——本模块不跑浏览器，只把播放器页自己的请求照抄出来：

resolve() 按「站点 host → 抽流函数」映射(_HOST_RESOLVERS)分发，每个 URL 只走匹配它
的那一条，已接入四条家族：
  · 789/M3U8/PlayM3U8/CK：api-api.789jiexi.net 的 ArtPlayer 家族——逐层跟 <iframe>
    下钻到含 config{url,time,key} 的播放器页，POST 同域 api.php{url,time,key}，把
    响应 data.url(AES-CBC-128/PKCS7 密文，key/iv 写在同站 setting.js) 解密得明文 m3u8；
  · 公益(jiexi.site)：苹果CMS/xyplayer 明文 JSON，GET api.php?tp=play&url=官方页直回 m3u8；
  · LX(jx.lxyy.club)：壳页取 data-video/data-code，POST public.php{url,code} 回 ticket m3u8；
  · 冰豆(bd.jx.cn)：三层跳转(bd.jx.cn → jiexi.bot.cd → /jx/)后进 Index.php 播放器页，
    取流核心是两段 TinyGo WASM 导出的 hxm_encrypt/hxm_decrypt/hxm_aesencrypt/
    hxm_aesdecrypt，纯 Python 跑不了，故随包带一段 Node 脚本 `bdjs/bd_run.js` 复现整条链
    （预热→Index.php 抽页面全局→沙箱跑 khd_play.js→POST report-uuid(protobuf)→
    AES-128-CBC(md5 派生 key/iv)→hxm_aesdecrypt→hxm_decrypt→直链），本模块只做
    subprocess 调 node + 解析 RESULT 行的胶水（需本机装有 node）。
    注意冰豆**解析失败时不报错**，而是回一个固定的兜底宣传片 mp4，故必须靠 msg 字段
    识别（成功必带 "正常"/"数据正常"，兜底没有）；且其直链的 Content-Type 与扩展名都
    会骗人（HLS 端点回 image/vnd.microsoft.icon、真 mp4 挂在 .jpg 路径下回 image/jpeg），
    一律嗅探首字节判定，见 _sniff_media。
未接入的源视为「不支持」：is_supported() 返回 False、resolve() 不发任何请求直接返回
None，由调用方如实报错（改用无损云下载），绝不盲目下钻或臆造地址。除冰豆走随包 Node
脚本外，其余均为纯 requests + cryptography（AES），零浏览器。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests

from . import net

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_IFRAME_RE = re.compile(r'<iframe[^>]*\bsrc=["\']([^"\']+)["\']', re.I)
# config 页特征："key":"<32hex>" 且 "time":"<digits>"（stray.start 用来签名 api.php）
_KEY_RE = re.compile(r'"key"\s*:\s*"([0-9a-fA-F]{16,})"')
_TIME_RE = re.compile(r'"time"\s*:\s*"(\d+)"')
# config.url 可能是解码(https://…) 也可能是经 iframe query 传来的编码形式(https%3A%2F%2F…)
_CFGURL_RE = re.compile(r'"url"\s*:\s*"((?:https?://|https?%3A)[^"]+)"')
# setting.js 里 CryptoJS.enc.Utf8.parse('<16位key>'),{iv: CryptoJS.enc.Utf8.parse('<16位iv>')
_PARSE16_RE = re.compile(r"CryptoJS\.enc\.Utf8\.parse\(['\"]([^'\"]{16})['\"]\)")

# 789 家族已知常量（setting.js 抓不到时的兜底）
_FALLBACK_KEY = "ARTPLAYERliUlanG"
_FALLBACK_IV = "ArtplayerliUlanG"


@dataclass
class StreamInfo:
    """抽流结果：可下载的 m3u8 直链 + 拉它必须携带的请求头。"""

    m3u8: str
    user_agent: str = _UA
    referer: str = ""

    def headers(self) -> dict:
        h = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if self.referer:
            h["Referer"] = self.referer
        return h


def _get(sess: requests.Session, url: str, referer: str = "") -> Optional[str]:
    try:
        r = sess.get(url, timeout=25, headers={"Referer": referer} if referer else None)
    except Exception:
        return None
    if r.status_code >= 400:
        return None
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1"):
        r.encoding = "utf-8"
    return r.text


def _descend_to_config(sess: requests.Session, start_url: str) -> Optional[tuple]:
    """从壳页逐层跟随 iframe，返回 (config_page_url, config_html)；找不到 None。"""
    url = start_url
    referer = ""
    for _ in range(5):                                  # iframe 嵌套不深，5 层封顶
        html = _get(sess, url, referer)
        if html is None:
            return None
        km, tm = _KEY_RE.search(html), _TIME_RE.search(html)
        if km and tm:                                    # 命中播放器 config 页
            return url, html
        m = _IFRAME_RE.search(html)                      # 否则跟随下一层 iframe
        if not m:
            return None
        referer = url
        url = urljoin(url, m.group(1))
    return None


def _aes_cbc_decrypt(ct_b64: str, key: str, iv: str) -> str:
    """CryptoJS AES-CBC/Pkcs7（key/iv 为 16 字节裸串，非口令 KDF）的等价实现。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = base64.b64decode(ct_b64)
    d = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
    p = d.update(raw) + d.finalize()
    pad = p[-1]
    if 1 <= pad <= 16:
        p = p[:-pad]
    return p.decode("utf-8", "replace").strip()


def _key_iv(sess: requests.Session, config_url: str, config_html: str) -> tuple:
    """优先从同站 setting.js 动态抓 key/iv（常量轮换也能跟上），失败退回已知常量。"""
    js_url = urljoin(config_url, "artplayer/js/setting.js")
    js = _get(sess, js_url, referer=config_url) or ""
    found = _PARSE16_RE.findall(js)
    if len(found) >= 2:
        return found[0], found[1]
    return _FALLBACK_KEY, _FALLBACK_IV


def _looks_like_m3u8(sess: requests.Session, info: StreamInfo) -> bool:
    try:
        r = sess.get(info.m3u8, timeout=20, headers=info.headers())
    except Exception:
        return False
    body = (r.text or "")[:200]
    return "#EXTM3U" in body or "#EXTINF" in body


def _resolve_789(parse_url: str) -> Optional[StreamInfo]:
    """789/M3U8/PlayM3U8/CK 家族：壳→iframe→config→api.php→AES-CBC→m3u8。"""
    if not parse_url:
        return None
    sess = net.get_session()
    hop = _descend_to_config(sess, parse_url)
    if not hop:
        return None
    config_url, config_html = hop

    # 取 config.url（被解析的官方页）/ time / key ——照抄 stray.start 的 $.post 入参
    cu = _CFGURL_RE.search(config_html)
    km, tm = _KEY_RE.search(config_html), _TIME_RE.search(config_html)
    if not (cu and km and tm):
        return None
    api = urljoin(config_url, "api.php")                # 与播放器页同域
    origin = f"{urlsplit(config_url).scheme}://{urlsplit(config_url).netloc}/"
    page = unquote(cu.group(1))                         # config.url 可能是编码形式
    try:
        pr = sess.post(api, data={"url": page, "time": tm.group(1),
                                  "key": km.group(1)},
                       timeout=30, headers={"Referer": config_url,
                                            "User-Agent": _UA,
                                            "X-Requested-With": "XMLHttpRequest"})
        if not pr.encoding or pr.encoding.lower() in ("iso-8859-1", "latin-1"):
            pr.encoding = "utf-8"
        d = pr.json()
    except Exception:
        return None
    if str(d.get("code")) != "200" or not d.get("url"):
        return None

    key, iv = _key_iv(sess, config_url, config_html)
    try:
        plain = _aes_cbc_decrypt(d["url"], key, iv)
    except Exception:
        return None
    if not plain.lower().startswith("http"):
        return None

    info = StreamInfo(m3u8=plain,
                      user_agent=(d.get("user-agent") or _UA),
                      referer=origin)
    # 校验：能拉到 #EXTM3U 才算真打通；拉不到退回默认头再试一次
    if _looks_like_m3u8(sess, info):
        return info
    info.referer = ""
    if _looks_like_m3u8(sess, info):
        return info
    # 校验失败但确实是 m3u8 形态：仍返回，交给 ffmpeg 尝试（诚实起见由调用方兜底）
    return info if ".m3u8" in plain.lower() else None


# 明文 JSON 接口家族（苹果CMS / xyplayer 系）：<origin>/api.php?tp=play&url=官方页
# 直接回吐 {code:200,url:"<m3u8>",type:"hls"}，无需下钻 iframe、也无 AES。
def _resolve_json_api(parse_url: str) -> Optional[StreamInfo]:
    """公益解析(jiexi.site)：GET <origin>/api.php?tp=play&url=<官方页> → JSON.url 即明文 m3u8。"""
    parts = urlsplit(parse_url)
    host = parts.netloc
    page = (parse_qs(parts.query).get("url") or [""])[0]
    if not page:
        return None
    origin = f"{parts.scheme}://{host}"
    sess = net.get_session()
    sess.headers.update({"User-Agent": _UA})
    try:
        r = sess.get(f"{origin}/api.php?tp=play&url={quote(page, safe='')}",
                     timeout=30, headers={"Referer": origin + "/"})
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1"):
            r.encoding = "utf-8"
        d = r.json()
    except Exception:
        return None
    m3 = str((d or {}).get("url") or "").strip()
    if not m3.lower().startswith("http"):
        return None
    info = StreamInfo(m3u8=m3, referer=origin + "/")
    if _looks_like_m3u8(sess, info):        # 公益 CDN 常要求带同源 Referer
        return info
    info.referer = ""
    if _looks_like_m3u8(sess, info):
        return info
    return info if ".m3u8" in m3.lower() else None


# LX解析(jx.lxyy.club)：React 壳但走 public 模式——壳页容器带 data-video/data-code，
# POST public.php {url,code} 回 {success,url:"…?ticket=<jwt>"}，该 url 即 VOD m3u8 直链。
def _resolve_lx(parse_url: str) -> Optional[StreamInfo]:
    """LX(jx.lxyy.club)：GET 壳页取 data-video/data-code → POST public.php → ticket m3u8 直链。"""
    parts = urlsplit(parse_url)
    host = parts.netloc
    origin = f"{parts.scheme}://{host}"
    base = origin + "/"
    sess = net.get_session()
    sess.headers.update({"User-Agent": _UA})
    try:
        r = sess.get(parse_url, timeout=30, headers={"Referer": base})
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1"):
            r.encoding = "utf-8"
        html = r.text
    except Exception:
        return None
    video = (re.findall(r'data-video="([^"]+)"', html) or [""])[0]
    code = (re.findall(r'data-code="([^"]+)"', html) or [""])[0]
    if not video:                                     # 兜底：直接取 query 里的官方页
        video = unquote((parse_qs(parts.query).get("url") or [""])[0])
    if not video:
        return None
    try:
        pr = sess.post(base + "public.php", json={"url": video, "code": code},
                       timeout=30, headers={"Referer": base, "Origin": origin,
                                             "Content-Type": "application/json"})
        if not pr.encoding or pr.encoding.lower() in ("iso-8859-1", "latin-1"):
            pr.encoding = "utf-8"
        d = pr.json()
    except Exception:
        return None
    m3 = str((d or {}).get("url") or "").strip()
    if not (d.get("success") and m3.lower().startswith("http")):
        return None
    info = StreamInfo(m3u8=m3, referer=base)          # ticket 校验同源 Referer
    if _looks_like_m3u8(sess, info):
        return info
    info.referer = ""
    if _looks_like_m3u8(sess, info):
        return info
    return None


# 冰豆(bd.jx.cn)：取流核心是两段 TinyGo WASM 导出的 hxm_encrypt/hxm_decrypt/
# hxm_aesencrypt/hxm_aesdecrypt，纯 Python 跑不了，故随包带一段 Node 脚本 bdjs/bd_run.js
# 复现整条链，这里只做「subprocess 调 node + 解析 RESULT 行 + 拒兜底 + 嗅探真流」的胶水。
_BD_JS = os.path.join(os.path.dirname(__file__), "bdjs", "bd_run.js")


# 冰豆直链的 Content-Type 与扩展名都会骗人（实测）：
#   · HLS 播放列表端点回 `image/vnd.microsoft.icon`，正文却是 #EXTM3U；
#   · 真 mp4 挂在 byteimg 的 `.jpg~tplv-…-image.image` 路径下，回 `image/jpeg`，
#     正文头 8 字节是 `....ftyp`（700MB 的 MP4）。
# 故一律**嗅探首字节**判定，绝不信响应头/后缀。
_MEDIA_MAGICS = (
    (b"#EXTM3U", 0),          # HLS 播放列表
    (b"ftyp", 4),             # MP4/MOV（offset 4 处）
    (b"\x1aE\xdf\xa3", 0),    # MKV/WebM
    (b"RIFF", 0),             # AVI
    (b"FLV", 0),              # FLV
)


def _sniff_media(sess: requests.Session, info: StreamInfo) -> Optional[str]:
    """抓首 64 字节嗅探真身；是媒体返回 "m3u8"/"video"，否则 None。"""
    try:
        r = sess.get(info.m3u8, timeout=25, stream=True, headers=info.headers())
        if r.status_code not in (200, 206):
            r.close()
            return None
        head = next(r.iter_content(64), b"") or b""
        r.close()
    except Exception:
        return None
    for magic, off in _MEDIA_MAGICS:
        if head[off:off + len(magic)] == magic:
            return "m3u8" if magic == b"#EXTM3U" else "video"
    return None


# 冰豆解析失败时服务端不会报错，而是回一个**固定**的兜底 mp4（784109B 的宣传片），
# 直接当成功会把用户骗进去。实测判据：成功响应 info 必带 msg（"正常"/"数据正常"），
# 兜底响应没有 msg；再叠一层 host 黑名单兜住 msg 判据万一失效的情况。
_BD_FALLBACK_HOST = "kjjsaas-sh.oss-cn-shanghai.aliyuncs.com"


def _resolve_bd(parse_url: str) -> Optional[StreamInfo]:
    """冰豆：subprocess 跑随包 bdjs/bd_run.js 出真流直链（需本机装有 node）。

    无 node / 脚本缺失 / 未出链 / 命中兜底 / 嗅探非媒体一律返回 None，由调用方如实
    报错换源，绝不臆造地址。bd 是服务端预抓取镜像，清晰度取决于对方存了哪一档。
    """
    page = (parse_qs(urlsplit(parse_url).query).get("url") or [""])[0]
    if not page:
        return None
    node = shutil.which("node") or shutil.which("node.exe")
    if not node or not os.path.isfile(_BD_JS):
        return None
    try:
        r = subprocess.run([node, _BD_JS, page], cwd=os.path.dirname(_BD_JS),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception:
        return None
    out = (r.stdout or "") + (r.stderr or "")
    # 首选机器可读的 RESULT 行（带 msg，用于识别兜底）；退回 FINAL URL 行
    direct, msg = "", None
    rm = re.search(r"\*\*\*RESULT\*\*\*\s+(\{.*\})", out)
    if rm:
        try:
            j = json.loads(rm.group(1))
            direct = str(j.get("url") or "").strip()
            msg = j.get("msg")
        except Exception:
            pass
    if not direct.lower().startswith("http"):
        fm = re.search(r"\*\*\*FINAL URL\*\*\*\s+(\S+)", out)
        direct = (fm.group(1).strip() if fm else "")
    if not direct.lower().startswith("http"):
        return None
    # 兜底识别：msg 缺失即失败；再按 host 兜一层
    if not msg:
        return None
    if _BD_FALLBACK_HOST in urlsplit(direct).netloc:
        return None
    info = StreamInfo(m3u8=direct)                    # 镜像 CDN 不校验 Referer
    # 嗅探一次即定生死：拿到 #EXTM3U 或视频容器魔数才算真打通。不再叠第二次请求——
    # 冰豆 HLS 直链带 ckey/time 票据，多打一次有耗票风险（download.py 对此有同样告诫）。
    return info if _sniff_media(net.get_session(), info) else None


# 麒麟解析(free.maccms.xyz)：ArtPlayer 系，全程明文、无 AES、无 iframe 下钻。
# 解析页内联 playerConfig.apiToken（服务端按官方页现生成的一次性令牌）→
# GET 同域 api/resolve.php?token=<令牌> → {code,url}；url 是 open.*.cn/m3u8 直链，
# 302 跳到真 CDN 后正文即 #EXTM3U。麒麟是 maccms 采集站，采不到的片子会回
# code:404「无法解析」——那是内容缺失、非机制故障，据实返回 None 让调用方换源。
_QL_TOKEN_RE = re.compile(r"""apiToken["']?\s*[:=]\s*["']([^"']+)["']""")


def _resolve_qilin(parse_url: str) -> Optional[StreamInfo]:
    """麒麟(free.maccms.xyz)：解析页取内联 apiToken → api/resolve.php?token= → 明文 m3u8。"""
    parts = urlsplit(parse_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    page = (parse_qs(parts.query).get("url") or [""])[0]
    if not page:
        return None
    sess = net.get_session()
    sess.headers.update({"User-Agent": _UA})
    try:
        h = sess.get(f"{origin}/?url={quote(page, safe='')}",
                     timeout=30, headers={"Referer": origin + "/"})
        tm = _QL_TOKEN_RE.search(h.text or "")
        if not tm:
            return None
        r = sess.get(f"{origin}/api/resolve.php?token={quote(tm.group(1), safe='')}",
                     timeout=30, headers={"Referer": origin + "/"})
        d = r.json()
    except Exception:
        return None
    if (d or {}).get("code") != 200:        # 采不到（code:404 等）：内容缺失，非机制故障
        return None
    m3 = str((d or {}).get("url") or "").strip()
    if not m3.lower().startswith("http"):
        return None
    info = StreamInfo(m3u8=m3, referer=origin + "/")   # open.*.cn 的 302 票据常认同源 Referer
    if _looks_like_m3u8(sess, info):
        return info
    info.referer = ""
    if _looks_like_m3u8(sess, info):
        return info
    return info if _sniff_media(sess, info) else None   # 兜：直链落到 ts/mp4 容器的情况


def _base_host(url: str) -> str:
    host = urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else host


# 解析源 host → 抽流函数：一一对应，每个 URL 只跑它匹配的那条（去掉 www. 前缀比较）。
# 789/M3U8/PlayM3U8/CK 四源共用同一下钻逻辑 _resolve_789，故都指向它；
# 未登记的站点即「不支持下载」——resolve 不发任何请求直接返回 None，调用方据
# is_supported() 明确报错，绝不盲目下钻或臆造地址。
_HOST_RESOLVERS = {
    "jx.lxyy.club": _resolve_lx,           # LX解析
    "jiexi.site": _resolve_json_api,       # 公益解析（苹果CMS 明文 JSON）
    "jiexi.789jiexi.com": _resolve_789,    # 789解析
    "jx.m3u8.tv": _resolve_789,            # M3U8解析
    "playm3u8.cn": _resolve_789,           # PlayM3U8解析
    "ckplayer.vip": _resolve_789,          # CK播放器解析
    "bd.jx.cn": _resolve_bd,               # 冰豆解析（走随包 Node 脚本；可能回 mp4 直链或 HLS）
    "free.maccms.xyz": _resolve_qilin,     # 麒麟解析（ArtPlayer：apiToken→api/resolve.php→明文 m3u8）
}


def is_supported(parse_url: str) -> bool:
    """该第三方源是否已接入下载（host 命中映射表）。未接入即不支持，调用方据此报错。"""
    return bool(parse_url) and _base_host(parse_url) in _HOST_RESOLVERS


def resolve(parse_url: str) -> Optional[StreamInfo]:
    """给定第三方源解析页 URL（source.url+官方页），按 host→接口映射抽可下载 m3u8。

    每个 URL 只走映射中匹配的那一条解析函数；未登记的源视为「不支持」，不发任何
    请求直接返回 None（调用方用 is_supported 区分「不支持」与「支持但抽流失败」），
    绝不盲目下钻或臆造地址。
    """
    if not parse_url:
        return None
    resolver = _HOST_RESOLVERS.get(_base_host(parse_url))
    if resolver is None:                    # 未接入的源：不支持，直接返回
        return None
    return resolver(parse_url)
