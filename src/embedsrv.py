"""embedsrv.py —— embed 后端的本地 HTTP 服务与播放页。

## 为什么需要它

原来的 `EmbedBackend` 把第三方播放页塞进 iframe（`PLAYER + encodeURIComponent(url)`），
然后用 `webbrowser.open` 打开一个 `file://` 页面。这个结构下 Python 完全看不到播放状态：
iframe 跨域，读不到里面的 `<video>`，也收不到任何播放事件。

实测无损云的 m3u8 与分片都返回 `Access-Control-Allow-Origin: *`
且不加密封装，于是可以换一种结构：**用我们自己的页面 + hls.js 直接播 m3u8**。
`<video>` 在我们手里，播放事件（元数据 / 失败 / 回退 / 选集点击）都能回传给终端。

页面要把事件送回 Python，就需要一个本地 HTTP 服务（`file://` 页面无法可靠回传）。
本模块提供这个服务：既当静态服务器（页面 + hls.js），又当回调收集器（`/event`）。

## 一个标签页播完整部剧

`play()` 原先每次都 `open_in_tab`，手动换集也会开出一排标签页。这里改成：
Python 换集只是改服务端下发的 state（带一个新 token），页面自己切源。
标签页只在「还没有活着的页面」时才开一次（实测换 3 集只开 1 个标签）。

页面拿 state 用 **`/wait` 长轮询**而不是 `setInterval` 轮询 `/state`：
Chrome 会节流后台标签页的定时器（intensive throttling 最狠 1 分钟一次），
用定时轮询的话，用户切到别的标签页时换集指令要等最多 1 分钟才被看到 ——
表现为「点了选集半天不换源」。长轮询是一次挂起的 HTTP 请求，不受节流影响
（实测 token 变化后 0.6s 内页面就切了源）。`/state` 仍保留作慢速兜底。

## 四种 state 模式

* `hls`    —— 裸 m3u8，用 hls.js 直接播，能收到页面回传的播放事件
* `direct` —— 裸 mp4 等浏览器原生就能播的媒体流，能收到播放事件
  （`is_media_url()` 对 mp4 也返回 True，但 hls.js 播不了 mp4，必须分开处理）
* `iframe` —— 第三方 HTML 解析页，只能塞 iframe，收不到任何事件
  （那种页面靠 JS 动态取流，实测 ffplay/mpv 一律 I/O error，hls.js 也无从下手）
* `halt`   —— Python 侧退出：让页面停播并显示一行说明（见下）

## 页面内回退

hls.js 播不了某个地址（CORS 被拦 / 清单 404 / 加密方式不支持）时，页面自己换成
iframe 播 `PLAYER + url` 并回传 `fallback` 事件。**画面优先**：
用户至少能看，Python 侧则据 `fallback` 事件提示用户「这一集已回退到第三方
播放页」，而不是静默失效。
注意分片级的网络错误走的是「startLoad 重试」，不回退 —— 那种多半是抖动。

## 页面关闭的检测

`pagehide` 里用 `navigator.sendBeacon('/bye')` 发信标（专为「卸载时也要送达」设计，
关标签页/关浏览器都能到；普通 fetch 往往还没发出去页面就没了）。
`page_alive()` 先看这个信标（收到过就判死），再看心跳是否超过 `_PAGE_STALE`。
一个坑：长轮询的 handler 线程在页面关掉后还会继续跑最多 `WAIT_LONGPOLL` 秒、
期间一直刷新心跳，所以它**不能**清 `_page_bye`，否则信标会在 0.15s 内被抹掉；
`page_alive()` 也要先看信标再看心跳，否则刚关掉的页面会被判成「还活着」，
`play()` 就不开新标签页了（表现为「按了没反应」）。

## 退出时停播

`stop()` 先下发一个 `halt` state、等页面回 `halted` 回执（最多 2s）再关服务。
直接关服务的话页面还在播、声音继续响，用户会以为终端没退干净。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HLSJS_VERSION = "1.5.17"
HLSJS_CDNS = (
    f"https://cdn.jsdelivr.net/npm/hls.js@{HLSJS_VERSION}/dist/hls.min.js",
    f"https://unpkg.com/hls.js@{HLSJS_VERSION}/dist/hls.min.js",
)

# 页面每隔这么久上报一次心跳；超过 _PAGE_STALE 没心跳就认为页面已经关了
HEARTBEAT_INTERVAL = 2.0
_PAGE_STALE = 8.0
# 页面轮询 /state 的间隔（仅作长轮询不可用时的兜底）
STATE_POLL_INTERVAL = 0.8
# /wait 长轮询一次最多挂这么久（秒）。到点就返回当前 state，页面立刻再发起下一次，
# 所以这个值只影响「服务端重启/连接被掐」时页面多久能重新对上，不影响换集延迟。
WAIT_LONGPOLL = 20.0
# 页面两次 /wait 之间的最小间隔（秒）。正常时长轮询会挂住几十秒，这个下限用不上；
# 但服务关闭时 /wait 会**立刻**返回（_shutting_down），没有这个下限的话
# waitLoop 就退化成热循环 —— 实测 525 次/秒打向一个已经死掉的端口。
WAIT_MIN_GAP = 1.0


class _QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 吞掉「连接被对端中止」的噪音。

    页面关掉时，正挂着的 keep-alive 连接会被浏览器掐断，socketserver 默认会
    调 handle_error() 把整段 traceback 打到 stderr —— 用户终端上凭空多出一坨
    Python 堆栈，看着像程序崩了。这类 OSError 是关页面的正常副产物，静默即可。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            return                    # ConnectionAborted/Reset/BrokenPipe：正常
        super().handle_error(request, client_address)


def ensure_hlsjs(cache_file: Path) -> Optional[Path]:
    """把 hls.js 下载到本地缓存（只需一次）；失败返回 None。

    本地化是为了：① 不依赖 CDN 可用性；② 页面加载更快；③ 离线也能用。
    下载失败不算致命 —— 页面里还有 CDN 兜底。
    """
    if cache_file.exists() and cache_file.stat().st_size > 10000:
        return cache_file
    try:
        import requests
    except ImportError:                      # pragma: no cover
        return None
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        for cdn in HLSJS_CDNS:
            try:
                r = requests.get(cdn, timeout=30)
                if r.status_code == 200 and len(r.content) > 10000:
                    cache_file.write_bytes(r.content)
                    return cache_file
            except Exception:
                continue
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 播放页
# ─────────────────────────────────────────────────────────────────────────────
# 用占位符 + replace 而不是 f-string：页面里全是 JS 的花括号，
# f-string 要写成 {{ }} 的话几百处转义，既难读又极易出错。
PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  html,body{margin:0;padding:0;background:#000;height:100%;
    font-family:-apple-system,"Microsoft YaHei",sans-serif;}
  #__BOX__{position:relative;width:100%;height:100vh;__CONTAINER_CSS__}
  .__WRAPPER__{position:relative;width:100%;height:100%;__WRAPPER_CSS__}
  .__WRAPPER__ iframe,.__WRAPPER__ video{__IFRAME_CSS__}
  #stage{position:absolute;inset:0;background:#000;}
  #v{width:100%;height:100%;background:#000;outline:none;}
  .ep-bar{position:absolute;top:0;right:0;bottom:0;width:170px;overflow-x:hidden;
    overflow-y:auto;z-index:2147483647;display:none;background:rgba(7,24,39,.94);
    padding:8px 6px;box-sizing:border-box;border-radius:10px 0 0 10px;
    border:1px solid rgba(14,165,233,.25);border-right:none;
    box-shadow:-6px 0 16px rgba(0,0,0,.45);}
  .ep{display:block;margin:4px 0;padding:5px 10px;font-size:12px;line-height:20px;
    border-radius:6px;cursor:pointer;user-select:none;text-align:center;overflow:hidden;
    white-space:nowrap;text-overflow:ellipsis;color:#bae6fd;background:#0b2942;
    border:1px solid #155e75;}
  .ep:hover{background:#0e7490;border-color:#38bdf8;}
  .ep-cur,.ep-cur:hover{color:#fff;background:#0369a1;border-color:#38bdf8;
    box-shadow:0 2px 6px rgba(14,165,233,.35);}
  .ep-trigger{position:absolute;top:0;right:0;bottom:0;width:10px;
    z-index:2147483645;background:transparent;}
  .ep-tab{position:absolute;top:10px;right:10px;z-index:2147483647;padding:6px 12px;
    font-size:13px;line-height:18px;background:rgba(7,24,39,.88);color:#7dd3fc;
    border:1px solid #0ea5e9;border-radius:8px;cursor:pointer;user-select:none;
    box-shadow:0 3px 10px rgba(0,0,0,.35);}
  #hint{position:absolute;inset:0;z-index:2147483646;display:none;
    align-items:center;justify-content:center;background:rgba(0,0,0,.72);
    color:#e0f2fe;font-size:16px;cursor:pointer;text-align:center;padding:20px;
    box-sizing:border-box;flex-direction:column;gap:6px;}
  #stat{position:absolute;left:8px;bottom:6px;z-index:2147483644;color:#38bdf8;
    font-size:11px;opacity:.55;pointer-events:none;font-family:monospace;}
</style>
</head>
<body>
<div id="__BOX__">
  <div class="__WRAPPER__">
    <div id="stage">
      <video id="v" playsinline controls></video>
      <iframe id="fr" style="display:none;width:100%;height:100%;border:0"
              allow="autoplay; encrypted-media; fullscreen" allowfullscreen
              referrerpolicy="no-referrer"></iframe>
    </div>
    <div id="hint"><div id="hintMain">▶ 点击此处开始播放</div>
      <div style="font-size:12px;opacity:.7" id="hintSub">（浏览器拦截了自动播放）</div></div>
    <div class="ep-bar" id="epBar"></div>
    <div class="ep-trigger" id="epTrigger"></div>
    <div id="stat"></div>
    __EP_TAB__
  </div>
</div>
<script>
(function(){
  var POLL = __POLL_MS__, BEAT = __BEAT_MS__, MINWAIT = __MINWAIT_MS__;
  var v = document.getElementById('v');
  var fr = document.getElementById('fr');
  var hint = document.getElementById('hint');
  var hintMain = document.getElementById('hintMain');
  var hintSub = document.getElementById('hintSub');
  var stat = document.getElementById('stat');
  var epBar = document.getElementById('epBar');
  var token = null, mode = null, hls = null, barBuilt = '';
  var fallbackUrl = '', epName = '';

  function send(kind, extra){
    var q = new URLSearchParams({kind: kind, token: String(token)});
    if (extra) for (var k in extra) q.set(k, String(extra[k]));
    // 心跳失败说明 Python 端已经退出，没必要一直重试刷屏
    fetch('/event?' + q.toString(), {cache:'no-store'}).catch(function(){});
  }
  function L(s){ stat.textContent = s; }

  function loadHlsJs(cb){
    if (window.Hls) return cb(true);
    var urls = __HLSJS_URLS__;
    var i = 0;
    (function next(){
      if (i >= urls.length) return cb(false);
      var s = document.createElement('script');
      s.src = urls[i++];
      s.onload = function(){ cb(!!window.Hls); };
      s.onerror = function(){ next(); };
      document.head.appendChild(s);
    })();
  }

  function destroyHls(){
    if (hls){ try{ hls.destroy(); }catch(e){} hls = null; }
  }

  function tryPlay(){
    var p = v.play();
    if (p && p.catch) p.catch(function(){
      // 浏览器拦截了带声自动播放。报给 Python，让终端也提示一句 ——
      // 用户点过这一次之后，同一页面后续的 play() 就不会再被拦，
      // 所以手动换集也只需在开头点一下。
      showHint('▶ 点击此处开始播放', '（浏览器拦截了自动播放）');
      send('autoplay_blocked', {});
    });
  }
  function showHint(main, sub){
    hintMain.textContent = main;
    hintSub.textContent = sub || '';
    hint.style.display = 'flex';
  }
  function hideHint(){ hint.style.display = 'none'; }
  hint.addEventListener('click', function(){
    hideHint();
    tryPlay();
  });

  // ── 回退到第三方播放页 ────────────────────────────────────────────────
  // hls.js 播不了（CORS 被拦 / 分片加密方式不支持 / 清单加载失败）时，
  // 画面优先：回退成 iframe 塞 PLAYER 播放页，
  // 用户照样能看（Python 侧会告知这一集已回退到第三方播放页）。
  function useFallback(reason){
    if (!fallbackUrl){
      L('✗ 播放失败：' + reason);
      send('error', {detail: reason, fatal:'true'});
      return;
    }
    destroyHls();
    mode = 'iframe';
    v.style.display = 'none';
    v.removeAttribute('src');
    try { v.load(); } catch(e){}
    fr.style.display = 'block';
    fr.src = fallbackUrl;
    L('⚠ 已回退到第三方播放页（' + reason + '）');
    send('fallback', {detail: reason});
  }

  // ── 切到某一集 ────────────────────────────────────────────────────────
  function applyState(st){
    token = st.token;
    if (st.mode === 'halt'){
      // Python 侧退出了：停掉画面与声音，别让用户以为终端没关干净
      destroyHls();
      try { v.pause(); } catch(e){}
      v.removeAttribute('src');
      try { v.load(); } catch(e){}
      fr.src = 'about:blank';
      mode = null;
      L('已停止');
      showHint('■ 已停止播放', '（终端已退出，可以关掉这个标签页）');
      send('halted', {});
      return;
    }
    fallbackUrl = st.fallback || '';
    epName = (st.episodes && st.episodes[st.idx] && st.episodes[st.idx].name) || '';
    // 标签页标题：片名 + 集名，用户在任务栏/标签栏能看出正在播哪一集
    document.title = [st.title, epName].filter(Boolean).join(' · ') || 'VIP 解析';
    hideHint();
    if (st.mode === 'iframe'){
      mode = 'iframe';
      destroyHls();
      v.style.display = 'none';
      v.removeAttribute('src');
      fr.style.display = 'block';
      if (fr.src !== st.url) fr.src = st.url;
      L('iframe 模式（第三方解析页，无法判断播完）');
      send('applied', {mode:'iframe'});
      return;
    }
    fr.style.display = 'none';
    fr.src = 'about:blank';
    v.style.display = 'block';
    v.removeAttribute('src');
    v.load();

    // 裸 mp4 之类：浏览器原生就能播，不需要 hls.js
    // （is_media_url() 对 mp4 也返回 True，但 hls.js 播不了 mp4，必须分开处理）
    if (st.mode === 'direct'){
      mode = 'direct';
      destroyHls();
      v.src = st.url;
      L('直接播放');
      send('applied', {mode:'direct'});
      tryPlay();
      return;
    }

    mode = 'hls';
    loadHlsJs(function(ok){
      if (!ok){
        // 没有 hls.js：Safari 之类原生支持 HLS 的浏览器还能直接播
        if (v.canPlayType('application/vnd.apple.mpegurl')){
          v.src = st.url; tryPlay();
          send('applied', {mode:'native'});
          return;
        }
        useFallback('hlsjs_load_failed');
        return;
      }
      destroyHls();
      hls = new Hls({ enableWorker:true, lowLatencyMode:false });
      hls.on(Hls.Events.MANIFEST_PARSED, function(e,d){
        send('manifest', {levels: d.levels.length});
        send('applied', {mode:'hls'});
        tryPlay();
      });
      hls.on(Hls.Events.ERROR, function(e,d){
        if (!d.fatal) return;
        // 网络类致命错误先尝试恢复，恢复不了才回退
        if (d.type === Hls.ErrorTypes.NETWORK_ERROR && d.details !== 'manifestLoadError'){
          try { hls.startLoad(); } catch(err){}
          return;
        }
        useFallback(d.details);
      });
      hls.loadSource(st.url);
      hls.attachMedia(v);
    });
  }

  // ── 选集栏 ────────────────────────────────────────────────────────────
  function buildBar(st){
    var eps = st.episodes || [];
    var sig = eps.length + ':' + st.idx;
    if (sig === barBuilt) return;
    barBuilt = sig;
    var h = '';
    for (var i=0;i<eps.length;i++){
      h += '<span class="ep' + (i===st.idx?' ep-cur':'') + '" data-idx="' + i + '">'
         + escapeHtml(eps[i].name || ('第'+(i+1)+'集')) + '</span>';
    }
    epBar.innerHTML = h;
    Array.prototype.forEach.call(epBar.querySelectorAll('.ep'), function(el){
      el.addEventListener('click', function(){
        var n = parseInt(el.getAttribute('data-idx'), 10);
        // 换集由 Python 侧执行（它要同步终端里的集号与选集栏高亮），
        // 页面只上报意图，等下一轮 /state 轮询把新源拿回来。
        fetch('/pick?idx=' + n, {cache:'no-store'}).catch(function(){});
      });
    });
    var cur = epBar.querySelector('.ep-cur');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({block:'nearest'});
  }
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  document.getElementById('epTrigger').addEventListener('mouseenter', function(){
    epBar.style.display = 'block';
  });
  document.getElementById('epTrigger').addEventListener('mouseleave', function(){
    epBar.style.display = 'none';
  });
  var tab = document.getElementById('epTab');
  if (tab) tab.addEventListener('click', function(){
    epBar.style.display = (epBar.style.display === 'block') ? 'none' : 'block';
  });

  // ── video 事件 → 回传 Python ──────────────────────────────────────────
  v.addEventListener('loadedmetadata', function(){
    send('metadata', {duration: v.duration});
  });
  v.addEventListener('playing', function(){
    hideHint();
    send('playing', {});
  });
  v.addEventListener('ended', function(){
    L('✓ 本集播完');
    // 播完后不会自动换下集：提示用户如何手动接着看
    showHint('✓ 本集播完', '若要接着看下一集，请在终端按 n 或点上方选集栏');
    send('ended', {currentTime: v.currentTime, duration: v.duration});
  });
  v.addEventListener('error', function(){
    var code = v.error ? v.error.code : '?';
    // hls/direct 模式下 video 自身的错误才算致命（iframe 模式的 video 是空的，
    // 切源瞬间的旧错误也会冒出来）；能回退就回退到第三方播放页，
    // 没有回退地址（本来就是解析页）才上报失败。
    if (mode === 'hls' || mode === 'direct'){
      useFallback('video_error_' + code);
    }
  });

  // ── 拿 state：长轮询 + 心跳 ───────────────────────────────────────────
  // 为什么用长轮询而不是 setInterval 轮询 /state：
  // Chrome 会节流**后台标签页**的定时器（intensive throttling 最狠 1 分钟一次），
  // 用户切到别的标签页时，终端里的换集指令要等最多 1 分钟才被页面看到。
  // 长轮询是一次挂起的 HTTP 请求，不受定时器节流影响，token 一变立刻返回。
  var stopped = false, lastWait = 0;
  function handleState(st){
    buildBar(st);
    if (st.token !== token){
      L('载入：' + (st.title || ''));
      applyState(st);
    }
  }
  function waitLoop(){
    if (stopped) return;
    // 两次长轮询之间至少隔 MINWAIT：正常情况下 /wait 会挂住几十秒，这个下限
    // 用不上；但服务关闭时它会**立刻**返回，若不加这个下限，waitLoop 就变成
    // 每秒几百次的热循环（实测 525 次/秒，白烧 CPU）。
    var gap = MINWAIT - (Date.now() - lastWait);
    if (gap > 0){ setTimeout(waitLoop, gap); return; }
    lastWait = Date.now();
    fetch('/wait?token=' + (token === null ? -1 : token), {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(st){
        handleState(st);
        // 服务要走了（Python 退出）：停止轮询，别再对着一个死服务发请求
        if (st.mode === 'halt'){ stopped = true; return; }
        waitLoop();
      })
      .catch(function(){
        // 服务断了（Python 被强杀/网络抖动）：退化成慢轮询，别把 CPU 打满
        setTimeout(waitLoop, 1500);
      });
  }
  // 兜底：万一长轮询链条因为异常断了，慢速轮询还能把 state 捞回来
  setInterval(function(){
    if (stopped) return;
    fetch('/state', {cache:'no-store'}).then(function(r){ return r.json(); })
      .then(function(st){
        handleState(st);
        if (st.mode === 'halt') stopped = true;
      }).catch(function(){});
  }, POLL * 8);
  setInterval(function(){
    // 服务走了就别再发心跳：对着死端口每 2s 发一次没有意义，
    // 而且浏览器控制台会攒一堆 net::ERR_CONNECTION_REFUSED
    if (stopped) return;
    var s = '';
    if ((mode === 'hls' || mode === 'direct') && v.duration)
      s = Math.floor(v.currentTime) + '/' + Math.floor(v.duration) + 's';
    send('beat', {t: s, mode: mode || '', cur: v.currentTime || 0,
                  dur: v.duration || 0, paused: v.paused});
  }, BEAT);

  // 页面被关/切走时给 Python 一个信标，让它知道「播放页没了」。
  // 用 sendBeacon 而不是 fetch：它专为卸载时也要送达设计，关标签页也能到；
  // 普通 fetch 在 pagehide 里往往还没发出去页面就没了。
  window.addEventListener('pagehide', function(){
    try {
      if (navigator.sendBeacon)
        navigator.sendBeacon('/bye?token=' + String(token));
    } catch(e){}
  });

  waitLoop();
  send('pageload', {});
})();
</script>
</body>
</html>
"""


def render_page(
    box_id: str,
    wrapper_class: str,
    container_css: str,
    wrapper_css: str,
    iframe_css: str,
    ep_tab_html: str,
    title: str,
    hlsjs_urls: List[str],
) -> str:
    """把 PAGE_TEMPLATE 里的占位符填成实际值。"""
    out = PAGE_TEMPLATE
    for k, val in (
        ("__TITLE__", title),
        ("__BOX__", box_id),
        ("__WRAPPER__", wrapper_class),
        ("__CONTAINER_CSS__", container_css),
        ("__WRAPPER_CSS__", wrapper_css),
        ("__IFRAME_CSS__", iframe_css),
        ("__EP_TAB__", ep_tab_html),
        ("__POLL_MS__", str(int(STATE_POLL_INTERVAL * 1000))),
        ("__BEAT_MS__", str(int(HEARTBEAT_INTERVAL * 1000))),
        ("__MINWAIT_MS__", str(int(WAIT_MIN_GAP * 1000))),
        ("__HLSJS_URLS__", json.dumps(list(hlsjs_urls) + ["/hls.min.js"])),
    ):
        out = out.replace(k, val)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 本地服务
# ─────────────────────────────────────────────────────────────────────────────
class EmbedServer:
    """本地 HTTP 服务：托管播放页、下发 state、收集页面事件。

    线程模型：HTTP 服务跑在后台线程；`record_event` 由服务线程调用，
    `state_json()` / `events` 由页面选集轮询线程和 CLI 主线程读取，故全程加锁。
    """

    def __init__(self, hlsjs_cache: Path, host: str = "127.0.0.1"):
        self._hlsjs_cache = Path(hlsjs_cache)
        self._host = host
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

        # 下发给页面的状态
        self._token = 0
        self._idx = 0
        self._url = ""
        self._mode = "hls"
        self._title = ""
        self._fallback = ""          # hls.js 播不了时页面回退用的第三方播放页
        self._episodes: List[Dict[str, str]] = []
        self._page_html = ""

        # 页面回传的事件
        self._events: List[Dict[str, Any]] = []
        self._last_beat = 0.0
        self._page_bye = False       # 是否收到过 pagehide 信标（页面被关/切走）
        # 一次性提示（回退、自动播放被拦等），由 CLI 取走后打印
        self._notices: List[str] = []
        self._picked_idx: Optional[int] = None
        # 页面是否已回应 halt（stop() 时等它真的停下来）
        self._halted: bool = False
        # 服务正在关闭：让挂在 /wait 长轮询里的请求立刻返回，别拖住 shutdown
        self._shutting_down: bool = False

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> bool:
        """起服务；失败返回 False（调用方据此退回旧的 file:// 方案）。"""
        with self._lock:
            if self._httpd is not None:
                return True
            try:
                self._httpd = _QuietHTTPServer((self._host, 0), self._make_handler())
            except OSError:
                self._httpd = None
                return False
            port = self._httpd.server_address[1]
            self._port = port
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name="embedsrv", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            # 先置关闭标记：挂在 /wait 长轮询里的 handler 线程会立刻返回，
            # 不必干等到 20s 超时（它们是 daemon，不至于卡住进程退出，但早点收干净）
            self._shutting_down = True
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    @property
    def base_url(self) -> Optional[str]:
        with self._lock:
            if self._httpd is None:
                return None
            return f"http://{self._host}:{self._port}"

    @property
    def running(self) -> bool:
        with self._lock:
            return self._httpd is not None

    def set_page_html(self, html_text: str) -> None:
        with self._lock:
            self._page_html = html_text

    # ── 状态下发 ──────────────────────────────────────────────────────────
    def set_episode(self, idx: int, url: str, mode: str, title: str,
                    episodes: List[Dict[str, str]], fallback: str = "") -> int:
        """换集：bump token，页面轮询到就会切源。返回新 token。"""
        with self._lock:
            self._token += 1
            self._idx = idx
            self._url = url
            self._mode = mode
            self._title = title
            self._fallback = fallback
            self._episodes = list(episodes)
            return self._token

    def update_playlist(self, episodes: List[Dict[str, str]], idx: Optional[int] = None,
                        title: Optional[str] = None) -> None:
        """只更新选集表（不 bump token，不打断正在播的这一集）。

        页面每轮 /state 都会重建选集栏，所以这里改完页面自然会跟上；
        token 不变意味着 `applyState` 不会被触发 —— 正在播的画面不受影响。
        """
        with self._lock:
            self._episodes = list(episodes)
            if idx is not None:
                self._idx = max(0, min(int(idx), len(episodes) - 1)) if episodes else 0
            if title is not None:
                self._title = title

    def halt(self) -> int:
        """通知页面停播（Python 侧退出 / 用户按 q）。返回新 token。

        为什么要专门发一次：直接关服务的话页面还在播，声音会继续响，
        用户以为终端没退干净。页面收到 halt 就 pause + 清空 src + 显示一行说明。
        """
        with self._lock:
            self._token += 1
            self._mode = "halt"
            self._url = ""
            self._fallback = ""
            self._halted = False
            return self._token

    def wait_halted(self, timeout: float = 2.0, poll: float = 0.05) -> bool:
        """等页面回一个 `halted`（它真的停了）。等不到就算了 —— 页面可能已经关了。"""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            with self._lock:
                if getattr(self, "_halted", False):
                    return True
            time.sleep(poll)
        with self._lock:
            return bool(getattr(self, "_halted", False))

    def wait_state(self, last_token: int, timeout: float = 20.0,
                   poll: float = 0.15) -> str:
        """长轮询：阻塞到 state 的 token 变化（或超时），返回 state JSON。

        为什么不用页面 setInterval 轮询 /state：Chrome 对**后台标签页**的定时器
        有节流（intensive throttling 最狠 1 分钟一次），用户切到别的标签页时，
        终端里的换集指令要等最多 1 分钟才被页面看到 —— 表现为「点了选集半天不换源」。
        长轮询是一次挂起的 HTTP 请求，不受定时器节流影响，token 一变立刻返回。

        每次返回都顺手刷新心跳：长轮询本身就是「页面还活着」的证据，
        这样即使后台标签页的 beat 定时器被节流，page_alive 也不会误判成已关闭。
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._shutting_down:
                    # 服务要关了：立刻返回，别让 handler 线程挂在长轮询里
                    return json.dumps(self._state_payload_locked(), ensure_ascii=False)
                # 长轮询挂着 = 页面还活着，刷新心跳。
                # 但**不能**在这里清 _page_bye：这个请求是页面关闭**之前**发起的，
                # 它的 handler 会在页面关掉后继续跑最多 timeout 秒；若它清掉 bye，
                # pagehide 信标就会在 0.15s 内被抹掉，page_alive() 就测不出页面已关。
                # bye 只由页面自己发的 beat/pageload 清除（回到前台/从 bfcache 恢复）。
                self._last_beat = time.time()
                changed = self._token != last_token
                payload = self._state_payload_locked()
            if changed:
                return json.dumps(payload, ensure_ascii=False)
            if time.time() >= deadline:
                return json.dumps(payload, ensure_ascii=False)
            time.sleep(poll)

    def _state_payload_locked(self) -> Dict[str, Any]:
        """组装 state 字典（调用方需持锁）。"""
        return {
            "token": self._token,
            "idx": self._idx,
            "url": self._url,
            "mode": self._mode,
            "title": self._title,
            "fallback": self._fallback,
            "episodes": [
                {"name": e.get("name") or f"第{i + 1}集"}
                for i, e in enumerate(self._episodes)
            ],
        }

    def state_json(self) -> str:
        with self._lock:
            payload = self._state_payload_locked()
        return json.dumps(payload, ensure_ascii=False)

    # ── 事件收集 ──────────────────────────────────────────────────────────
    def record_event(self, ev: Dict[str, str]) -> None:
        kind = ev.get("kind") or ""
        try:
            tok = int(ev.get("token") or -1)
        except (TypeError, ValueError):
            tok = -1
        with self._lock:
            self._events.append({"kind": kind, "token": tok, "at": time.time(),
                                 "raw": dict(ev)})
            # 只留最近 200 条，长时间播放不至于无限涨
            if len(self._events) > 200:
                del self._events[:-200]
            if kind == "beat":
                self._last_beat = time.time()
                self._page_bye = False      # 页面又活了，清掉之前的关闭信标
            elif kind == "pageload":
                self._last_beat = time.time()
                self._page_bye = False
            elif kind == "bye":
                # pagehide 信标：页面被关或切到后台。重新出现心跳会清掉它。
                self._page_bye = True
            elif kind == "playing":
                # 起播也是一次「页面还活着」的证据，顺手刷新心跳
                self._last_beat = time.time()
            elif kind == "halted":
                # 页面确认已停播（stop() 里等这个回执再关服务）
                self._halted = True
            elif kind == "fallback":
                if tok == self._token:
                    detail = ev.get("detail") or "unknown"
                    self._notices.append(
                        f"⚠ 浏览器里 hls.js 播不了这一集（{detail}），"
                        f"已回退到第三方播放页 —— 画面正常"
                    )
            elif kind == "autoplay_blocked":
                if tok == self._token:
                    self._notices.append(
                        "⚠ 浏览器拦截了自动播放：请在播放页上点一下「▶ 点击此处开始播放」"
                        "（点过一次之后，后续换集不会再被拦）"
                    )

    def pick(self, idx: int) -> None:
        """页面里点了选集栏：记下用户意图，由 Python 侧读取后走正常换集流程。"""
        with self._lock:
            self._picked_idx = idx

    def take_picked(self) -> Optional[int]:
        """取出并清掉「页面里选的集号」（一次性）。"""
        with self._lock:
            idx = self._picked_idx
            self._picked_idx = None
            return idx

    def take_notices(self) -> List[str]:
        """取出并清空待打印的一次性提示（回退 / 自动播放被拦）。"""
        with self._lock:
            out, self._notices = self._notices, []
            return out

    # ── 给后端查询 ────────────────────────────────────────────────────────
    def current_token(self) -> int:
        with self._lock:
            return self._token

    def current_idx(self) -> int:
        with self._lock:
            return self._idx

    def page_alive(self, within: float = _PAGE_STALE) -> bool:
        """页面还活着吗。

        收到过 pagehide 信标就直接判死：长轮询的 handler 线程在页面关掉后还会
        继续跑最多 WAIT_LONGPOLL 秒、期间一直刷新 `_last_beat`，只看心跳的话
        会把「刚关掉的页面」误判成活着 —— 那样 play() 就不会开新标签页，
        用户重开影片时会看到「什么都没发生」。
        """
        with self._lock:
            if self._page_bye:
                return False
            beat = self._last_beat
        return bool(beat) and (time.time() - beat) <= within

    def wait_for_page(self, timeout: float = 25.0, poll: float = 0.15) -> bool:
        """阻塞等页面第一次心跳（浏览器已经打开并开始播）。

        为什么需要：`play()` 之后浏览器起页面要几秒，等它真的活了再往下走，
        提示才不会打在起播之前。
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self.page_alive(within=_PAGE_STALE):
                return True
            time.sleep(poll)
        return self.page_alive(within=_PAGE_STALE)

    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    # ── HTTP handler ─────────────────────────────────────────────────────
    def _make_handler(self):
        server = self                      # 闭包捕获，handler 里回调用

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "video_vip_embed/1.0"

            def log_message(self, *a):     # 静音：每片一次日志会刷屏
                pass

            def _send(self, code: int, body: bytes, ctype: str,
                      no_cache: bool = False):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                if no_cache:
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    # 页面在响应写回之前就没了（关标签页时正挂着 /wait 长轮询）。
                    # Windows 上抛的是 ConnectionAbortedError(10053)，
                    # 只捕 BrokenPipe/ConnectionReset 会漏掉它，
                    # 于是 socketserver 把整段 traceback 打到用户终端上。
                    # 这是正常现象，静默即可。
                    pass

            def do_GET(self):
                self._route()

            def do_POST(self):
                # navigator.sendBeacon 走 POST（pagehide 信标用它，卸载时也能送达）。
                # 先把请求体读掉，否则 HTTP/1.1 keep-alive 下残留字节会串到下一个请求。
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 0:
                        self.rfile.read(length)
                except (TypeError, ValueError):
                    pass
                self._route()

            def _route(self):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = dict(urllib.parse.parse_qsl(parsed.query))

                if path in ("/", "/index.html", "/player.html"):
                    with server._lock:
                        page = server._page_html
                    if not page:
                        return self._send(503, b"page not ready", "text/plain; charset=utf-8")
                    return self._send(200, page.encode("utf-8"),
                                      "text/html; charset=utf-8", no_cache=True)

                if path == "/hls.min.js":
                    try:
                        data = ensure_hlsjs(server._hlsjs_cache)
                        if data and data.exists():
                            return self._send(200, data.read_bytes(),
                                              "application/javascript; charset=utf-8")
                    except Exception:
                        pass
                    return self._send(404, b"not cached", "text/plain")

                if path == "/state":
                    return self._send(200, server.state_json().encode("utf-8"),
                                      "application/json; charset=utf-8", no_cache=True)

                if path == "/wait":
                    # 长轮询：挂起到 token 变化或超时（不受后台标签页定时器节流影响）
                    try:
                        last = int(qs.get("token") or -1)
                    except (TypeError, ValueError):
                        last = -1
                    body = server.wait_state(last, timeout=WAIT_LONGPOLL)
                    return self._send(200, body.encode("utf-8"),
                                      "application/json; charset=utf-8", no_cache=True)

                if path == "/event":
                    server.record_event(qs)
                    return self._send(200, b"ok", "text/plain", no_cache=True)

                if path == "/bye":
                    # pagehide 信标：页面被关或切到后台
                    server.record_event({"kind": "bye", "token": qs.get("token", "-1")})
                    return self._send(200, b"ok", "text/plain", no_cache=True)

                if path == "/pick":
                    try:
                        server.pick(int(qs.get("idx") or -1))
                    except (TypeError, ValueError):
                        pass
                    return self._send(200, b"ok", "text/plain", no_cache=True)

                return self._send(404, b"not found", "text/plain")

        return Handler
