'use strict';
// bd_run.js —— 端到端复现冰豆取流链（纯 Node）
// 用法: node bd_run.js <目标视频页URL>
// 机制: play_vid=hxm_encrypt(btoaUnicode(url)) → GET Index.php 拿播放器页(meta1/Vurl/Id/Time/Version)
//       → 沙箱里跑 khd_play.js(它自会 POST report-uuid 并注册 hxm_aesdecrypt/hxm_decrypt)
//       → XHR 代理转发真实服务器并截获 protobuf 响应(field2/field5)
//       → 三层解密: AES-128-CBC(md5派生) → hxm_aesdecrypt → hxm_decrypt → 最终直链
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const http = require('http');
const https = require('https');
const { URL } = require('url');

const TARGET = process.argv[2];
if (!TARGET) { console.error('用法: node bd_run.js <url>'); process.exit(1); }

// ---------- 会话 cookie jar ----------
const jar = {};
function cookieHeader() { return Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; '); }
function saveCookies(res) {
  const sc = res.headers['set-cookie']; if (!sc) return;
  for (const c of sc) { const [kv] = c.split(';'); const i = kv.indexOf('='); jar[kv.slice(0, i).trim()] = kv.slice(i + 1).trim(); }
}
function httpReq(urlStr, opts = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const mod = u.protocol === 'http:' ? http : https;
    const req = mod.request(u, {
      method: opts.method || 'GET',
      headers: Object.assign({ 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36', 'Cookie': cookieHeader(), 'Referer': opts.referer || u.origin + '/' }, opts.headers || {}),
      rejectUnauthorized: false, timeout: 30000,
    }, res => {
      saveCookies(res);
      const chunks = []; res.on('data', d => chunks.push(d)); res.on('end', () => {
        let body = Buffer.concat(chunks);
        const enc = (res.headers['content-encoding'] || '').toLowerCase();
        try {
          const zlib = require('zlib');
          if (enc.includes('br')) body = zlib.brotliDecompressSync(body);
          else if (enc.includes('gzip')) body = zlib.gunzipSync(body);
          else if (enc.includes('deflate')) body = zlib.inflateSync(body);
        } catch (e) { /* 解压失败保留原始字节 */ }
        resolve({ status: res.statusCode, headers: res.headers, body });
      });
    });
    req.on('error', reject); req.on('timeout', () => req.destroy(new Error('timeout ' + urlStr)));
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

// ---------- go.wasm: hxm_encrypt ----------
function btoaUnicode(str) { const b = new TextEncoder().encode(str); let s = ''; for (const x of b) s += String.fromCharCode(x); return Buffer.from(s, 'binary').toString('base64'); }
async function makeEncryptor() {
  const def = (k, v) => { try { Object.defineProperty(global, k, { value: v, configurable: true, writable: true }); } catch (e) { } };
  def('self', global); if (!global.crypto) def('crypto', require('crypto').webcrypto);
  def('performance', require('perf_hooks').performance);
  def('TextEncoder', require('util').TextEncoder); def('TextDecoder', require('util').TextDecoder);
  // go.wasm 初始化会读这些浏览器全局，缺失会 panic（与 gen.js 一致）
  const gdoc = { domain: 'jiexi.bot.cd', referrer: '', URL: 'https://jiexi.bot.cd/jx/?url=x', createElement: () => ({ getContext: () => null, style: {} }), cookie: '', title: '' };
  const gscreen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040 };
  const gloc = { href: 'https://jiexi.bot.cd/jx/?url=x', search: '?url=x', host: 'jiexi.bot.cd', hostname: 'jiexi.bot.cd', protocol: 'https:', origin: 'https://jiexi.bot.cd', pathname: '/jx/' };
  const gnav = { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36', platform: 'Win32', language: 'zh-CN', languages: ['zh-CN'], appName: 'Netscape', vendor: 'Google Inc.' };
  def('document', gdoc); def('screen', gscreen); def('location', gloc); def('navigator', gnav); def('window', global);
  const wasmExec = fs.readFileSync(path.join(__dirname, 'wasm_exec.js'), 'utf8');
  const fakeModule = { exports: {} };
  vm.runInThisContext(`(function(module,exports,require,__filename,__dirname){${wasmExec}\n})`, { filename: 'wasm_exec.js' })(fakeModule, fakeModule.exports, require, path.join(__dirname, 'wasm_exec.js'), __dirname);
  const go = new global.Go(); go.importObject.env['syscall/js.finalizeRef'] = () => {};
  const res = await WebAssembly.instantiate(fs.readFileSync(path.join(__dirname, 'go.wasm')), go.importObject);
  go.run(res.instance).catch(() => {});
  await new Promise(r => setTimeout(r, 400));
  return global.hxm_encrypt;
}

function parseGlobals(html) {
  const g = {};
  const m1 = html.match(/<meta[^>]*content="text\/html[^>]*\bid="(\d{6,16})"/i) || html.match(/<meta[^>]*\bid="(\d{8,16})"[^>]*\/?>/);
  g.meta1 = m1 ? m1[1] : null;
  const mv = html.match(/id="viewport([^"]+)"/i); g.meta2 = mv ? mv[1] : null;
  const ver = html.match(/var\s+Version\s*=\s*"([^"]+)"/); g.Version = ver ? ver[1] : 'V3.3';
  const vu = html.match(/var\s+Vurl\s*=\s*"([0-9a-fA-F]+)"/); g.Vurl = vu ? vu[1] : null;
  const id = html.match(/'Id'\s*:\s*"([^"]+)"/); g.Id = id ? id[1] : null;
  const tm = html.match(/var\s+Time\s*=\s*"([^"]+)"/); g.Time = tm ? tm[1] : null;
  const api = html.match(/var\s+Api\s*=\s*"([^"]+)"/); g.Api = api ? api[1] : null;
  const dom = html.match(/var\s+Domain\s*=\s*"([^"]+)"/); g.Domain = dom ? dom[1] : null;
  const ref = html.match(/var\s+Ref\s*=\s*"([^"]+)"/); g.Ref = ref ? ref[1] : null;
  return g;
}

// ---------- protobuf 极简解析：取顶层 field2 / field5 ----------
function readVarint(buf, off) { let r = 0, sh = 0, b; do { b = buf[off++]; r |= (b & 0x7f) << sh; sh += 7; } while (b & 0x80); return [r >>> 0, off]; }
function pbParse(buf) {
  const out = {}; let off = 0;
  while (off < buf.length) {
    let key; [key, off] = readVarint(buf, off);
    const field = key >> 3, wt = key & 7;
    if (wt === 2) { let len; [len, off] = readVarint(buf, off); out[field] = buf.slice(off, off + len); off += len; }
    else if (wt === 0) { let v; [v, off] = readVarint(buf, off); out[field] = v; }
    else if (wt === 5) { out[field] = buf.readUInt32LE(off); off += 4; }
    else break;
  }
  return out;
}

(async () => {
  const hxm_encrypt = await makeEncryptor();
  if (typeof hxm_encrypt !== 'function') { console.error('hxm_encrypt 未就绪'); process.exit(1); }

  // 1) 预热：bd.jx.cn → jiexi.bot.cd → /jx/  (攒会话)
  const enc = encodeURIComponent(TARGET);
  try { await httpReq('https://bd.jx.cn/?url=' + enc, { referer: 'https://bd.jx.cn/' }); } catch (e) { console.error('warm1 ' + e.message); }
  try { await httpReq('https://jiexi.bot.cd/?url=' + enc, { referer: 'https://bd.jx.cn/' }); } catch (e) { console.error('warm2 ' + e.message); }
  try { await httpReq('https://jiexi.bot.cd/jx/?url=' + enc, { referer: 'https://jiexi.bot.cd/' }); } catch (e) { console.error('warm3 ' + e.message); }

  // 2) play_vid + 拉取真实 Index.php 播放器页
  const pv = hxm_encrypt(btoaUnicode(TARGET));
  const idxUrl = `https://123.129.229.14:4433/Index.php?play_vid=${pv}&ref=${TARGET}`;
  const idxRes = await httpReq(idxUrl, { referer: 'https://jiexi.bot.cd/jx/' });
  const html = idxRes.body.toString('utf8');
  try { fs.writeFileSync(path.join(require('os').tmpdir(), 'bd_idx_live.html'), html); } catch (e) { }
  if (/about:blank/.test(html)) { console.error('!! 命中反盗链空页(play_vid 失效/重放)'); process.exit(2); }
  const G = parseGlobals(html);
  console.error('[globals] ' + JSON.stringify(G));
  if (!G.meta1 || !G.meta2) { console.error('!! meta 解析失败'); process.exit(3); }

  // 3) 沙箱跑 khd_play.js：XHR 代理转发真实服务器 + 截获 report-uuid 响应 + 捕获 hxm_*
  const store = { __hxmStore: {} };
  let capturedResp = null;
  const xhrLog = [];

  function makeXHR() {
    return function XMLHttpRequest() {
      const self = this;
      self.readyState = 0; self.status = 0; self.responseText = ''; self.response = null;
      self.onreadystatechange = null; self.onload = null; self.onerror = null;
      let _method = 'GET', _url = '', _hdrs = {};
      self.open = (m, u) => { _method = m; _url = u; };
      self.setRequestHeader = (k, v) => { _hdrs[k] = v; };
      self.getAllResponseHeaders = () => '';
      self.addEventListener = (t, f) => { if (t === 'load') self.onload = f; if (t === 'error') self.onerror = f; };
      self.send = (body) => {
        const abs = /^https?:/.test(_url) ? _url : (G.Api + _url);
        xhrLog.push(_method + ' ' + abs);
        httpReq(abs, { method: _method, body: body, referer: idxUrl, headers: Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }, _hdrs) })
          .then(r => {
            self.status = r.status; self.readyState = 4;
            self.responseText = r.body.toString('latin1'); self.response = r.body;
            if (/report|uuid|api|index|play/i.test(abs) && r.body.length > 60 && !capturedResp) capturedResp = r.body;
            if (typeof self.onreadystatechange === 'function') self.onreadystatechange();
            if (typeof self.onload === 'function') self.onload();
          }).catch(e => { self.readyState = 4; if (typeof self.onerror === 'function') self.onerror(e); });
      };
    };
  }

  // 极简 jQuery 桩：链式 no-op + $.ajax 真实转发到服务器并截获 protobuf 响应
  function makejQuery() {
    // 选择器感知的链式桩：页面用 $('meta[http-equiv="Content-Type"]').attr('id') 和
    // $('meta[name="viewport"]').attr('id') 取 meta1/meta2 参与 seed 计算（field4=10 分支），
    // 一律返回 '' 会让 seed 少两段 → AES 解出乱码。
    function mkChain(sel) {
      const chain = new Proxy(function () { }, {
        get(t, p) {
          if (p === 'length') return 1; if (p === '0') return chain;
          if (p === Symbol.toPrimitive || p === 'toString') return () => '';
          if (p === 'attr') return (a) => {
            if (a !== 'id') return undefined;
            if (/Content-Type/i.test(sel)) return G.meta1 || '';
            if (/viewport/i.test(sel)) return G.meta2 || '';
            return undefined;
          };
          return chain;
        },
        apply() { return chain; }, set() { return true; },
      });
      return chain;
    }
    const $ = function (sel) { return mkChain(typeof sel === 'string' ? sel : ''); };
    $.fn = {}; $.ajaxSettings = {};
    $.extend = Object.assign;
    $.each = (o, fn) => { if (o && o.forEach) o.forEach((v, i) => fn(i, v)); return o; };
    $.ajax = function (settings) {
      settings = settings || {};
      const raw = settings.url || '';
      const abs = /^https?:/.test(raw) ? raw : (G.Api + raw);
      xhrLog.push((settings.type || 'GET') + ' $ ' + abs);
      // 请求体：二进制(Uint8Array/ArrayBuffer/Buffer)原样发送，字符串/对象按表单或 JSON
      let body;
      const d = settings.data;
      if (d == null) body = undefined;
      else if (Buffer.isBuffer(d)) body = d;
      else if (d instanceof Uint8Array) body = Buffer.from(d.buffer, d.byteOffset, d.byteLength);
      else if (d instanceof ArrayBuffer) body = Buffer.from(d);
      else if (typeof d === 'string') body = d;
      else body = JSON.stringify(d);
      const hdrs = Object.assign({ 'Content-Type': settings.contentType || 'application/octet-stream' }, settings.headers || {});
      httpReq(abs, { method: (settings.type || 'GET').toUpperCase(), body, referer: idxUrl, headers: hdrs })
        .then(r => {
          if (r.body.length > 20 && !capturedResp) capturedResp = r.body;
          const u8 = new Uint8Array(r.body.buffer.slice(r.body.byteOffset, r.body.byteOffset + r.body.byteLength));
          const jq = { response: u8.buffer, responseText: r.body.toString('latin1'), status: r.status, readyState: 4 };
          const payload = (settings.dataType === 'json') ? safeJson(r.body.toString('utf8'))
            : (settings.dataType === 'text' || !settings.xhrFields) ? r.body.toString('latin1') : u8;
          if (typeof settings.success === 'function') { try { settings.success(payload, 'success', jq); } catch (e) { console.error('ajax.success err ' + e.message.slice(0, 200)); } }
          if (typeof settings.complete === 'function') { try { settings.complete(jq, 'success'); } catch (e) { } }
        })
        .catch(e => { if (typeof settings.error === 'function') settings.error(null, 'error', e); });
      return mkChain('');
    };
    $.get = (u, cb) => $.ajax({ url: u, success: cb });
    $.post = (u, d, cb) => $.ajax({ url: u, type: 'POST', data: d, success: cb });
    return $;
  }
  function safeJson(s) { try { return JSON.parse(s); } catch (e) { return s; } }

  const fakeDoc = (function () {
    function node() {
      return {
        style: {}, children: [], firstChild: null, innerHTML: '', src: '', href: '', id: '',
        appendChild(c) { return c; }, insertBefore(c) { return c; }, removeChild(c) { return c; },
        setAttribute() { }, getAttribute() { return null; }, removeAttribute() { },
        addEventListener() { }, removeEventListener() { },
        classList: { add() { }, remove() { }, contains() { return false; } },
      };
    }
    return {
      domain: '123.129.229.14', referrer: 'https://jiexi.bot.cd/jx/', title: '404', cookie: '',
      URL: idxUrl, readyState: 'complete',
      head: node(), body: node(), documentElement: node(),
      createElement: () => node(), createTextNode: () => node(),
      getElementsByName: (n) => n === 'viewport' ? [{ id: 'viewport' + G.meta2 }] : [],
      getElementsByTagName: (t) => t === 'meta' ? [{ id: G.meta1 }, { id: 'viewport' + G.meta2 }] : [node()],
      getElementById: (id) => ({ id: id || G.meta1, style: {}, appendChild(c) { return c; }, setAttribute() { } }),
      querySelector: (s) => (/meta/.test(s) ? { id: G.meta1 } : node()),
      querySelectorAll: () => [], addEventListener() { }, removeEventListener() { },
    };
  })();
  const fakeLoc = { href: idxUrl, search: '', host: '123.129.229.14:4433', hostname: '123.129.229.14', protocol: 'https:', origin: 'https://123.129.229.14:4433', pathname: '/Index.php' };
  const fakeNav = { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36', platform: 'Win32', language: 'zh-CN', languages: ['zh-CN'], appName: 'Netscape', vendor: 'Google Inc.', maxTouchPoints: 0 };
  const fakeScreen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040 };

  // 关键：khd_play.js 的内嵌 wasm 通过 Go 运行时把 hxm_* 注册到"真实 global"，
  // 所以必须让 khd_play.js 也在同一个 global 上运行（runInThisContext），不能用独立 vm 沙箱。
  const gset = (k, v) => { try { Object.defineProperty(global, k, { value: v, configurable: true, writable: true }); } catch (e) { global[k] = v; } };
  gset('window', global); gset('self', global); gset('top', global); gset('parent', global);
  gset('document', fakeDoc); gset('navigator', fakeNav); gset('location', fakeLoc); gset('screen', fakeScreen);
  gset('XMLHttpRequest', makeXHR()); gset('alert', () => { });
  gset('$', makejQuery()); gset('jQuery', global.$);
  // $LAB 桩：页面解密成功后才用它加载播放器 JS（任意属性都返回自身），不桩会抛
  // "$LAB is not defined" 打断 success 回调，导致 window.info 写不出来。
  const labProxy = new Proxy(function () { }, { get: () => labProxy, apply: () => labProxy });
  gset('$LAB', labProxy); gset('LAB', labProxy);
  gset('Host', '123.129.229.14'); gset('Domain', G.Domain); gset('Api', G.Api); gset('Ref', G.Ref);
  gset('Time', G.Time); gset('Version', G.Version); gset('Vurl', G.Vurl);
  gset('Isios', false); gset('Iswap', false); gset('Autoplay', true);
  gset('PlayerPath', { art: 'https://p3-tt.byteimg.com/obj/artplayer.min.js', hls: 'https://nim.nosdn.127.net/hls.min.js' });
  gset('PlayConfig', { Id: G.Id, Title: '404', Url: G.Vurl, ThemeColor: '#165DFF' });
  gset('__hxmStore', store.__hxmStore);
  // 捕获 hxm_aesdecrypt / hxm_decrypt 注册（wasm 会 window.xxx=fn）
  for (const fn of ['hxm_aesdecrypt', 'hxm_decrypt', 'hxm_aesencrypt']) {
    try {
      Object.defineProperty(global, fn, {
        configurable: true,
        set(v) { store.__hxmStore[fn] = v; }, get() { return store.__hxmStore[fn]; },
      });
    } catch (e) { }
  }

  // 播放器脚本随 Index.php 现抓（URL 带 ?v= 时间戳会轮换，写死副本会过期），
  // 抓不到再退回随包缓存的副本；成功则顺带刷新本地副本作为下次兜底。
  async function loadPlayerScript(name) {
    const m = html.match(new RegExp('<script[^>]*\\bsrc\\s*=\\s*["\']([^"\']*' + name + '[^"\']*)["\']', 'i'));
    if (m) {
      try {
        const r = await httpReq(m[1], { referer: idxUrl });
        if (r.body && r.body.length > 1000) {
          try { fs.writeFileSync(path.join(__dirname, name), r.body); } catch (e) { }
          console.error('[asset] 现抓 ' + name + ' ' + r.body.length + 'B');
          return r.body.toString('utf8');
        }
      } catch (e) { console.error('[asset] 现抓 ' + name + ' 失败: ' + e.message); }
    }
    console.error('[asset] ' + name + ' 用随包副本');
    return fs.readFileSync(path.join(__dirname, name), 'utf8');
  }
  const gjs = await loadPlayerScript('global.min.js');
  const kjs = await loadPlayerScript('khd_play.js');
  // global.min.js = jQuery + Loadjs + CryptoJS 三合一。jQuery 的 UMD 在桩 DOM 上会抛
  // "Cannot read properties of undefined (reading 'createElement')"，一抛整段就废，
  // CryptoJS 也跟着没注册 → khd_play.js 的 success 回调解密时炸 "CryptoJS is not defined"。
  // 我们只需要 CryptoJS（jQuery 已用桩顶替），故从注释标记处切出 CryptoJS 块单独跑。
  const cjsMark = gjs.indexOf('CryptoJS Javascript Library');
  const cjsStart = cjsMark > 0 ? gjs.lastIndexOf('/*', cjsMark) : -1;
  const cjsSrc = cjsStart >= 0 ? gjs.slice(cjsStart) : null;
  if (cjsSrc) {
    try { vm.runInThisContext(cjsSrc, { filename: 'cryptojs.js' }); }
    catch (e) { console.error('cryptojs err ' + e.message); }
  } else {
    try { vm.runInThisContext(gjs, { filename: 'global.min.js' }); } catch (e) { console.error('global.min.js err ' + e.message); }
  }
  console.error('[CryptoJS] ' + typeof global.CryptoJS);
  try { vm.runInThisContext(kjs, { filename: 'khd_play.js' }); } catch (e) { console.error('khd_play.js err ' + e.message.slice(0, 300)); }

  // 等 khd_play.js 异步流程：优先等它自己写好 window.info（含最终 url），否则等截获的 protobuf 响应
  function infoUrl() {
    const cand = [global.info, global.window && global.window.info, global.__INFO, global.PlayConfig];
    for (const c of cand) { if (c && typeof c === 'object' && c.url && /http|\/|%2F/i.test(String(c.url))) return c; }
    return null;
  }
  let info = null;
  for (let i = 0; i < 60; i++) {
    info = infoUrl();
    if (info) break;
    if (capturedResp && Object.keys(store.__hxmStore).length >= 2) break;
    await new Promise(r => setTimeout(r, 250));
  }
  console.error('[xhr] ' + xhrLog.join('\n      '));
  console.error('[hxm registered] ' + Object.keys(store.__hxmStore).join(','));
  console.error('[captured resp bytes] ' + (capturedResp ? capturedResp.length : 'null'));
  console.error('[window.info] ' + JSON.stringify(global.info || null));

  // 若 khd_play.js 自己已解出，直接用；否则用截获响应手动跑三层解密
  let finalUrl = null;
  if (info && info.url) {
    // 原样保留：window.info.url 已是正确的单重编码 URL。切勿 decodeURIComponent——
    // 那会把 x-signature 里的 %2F/%3D 解成 //、=，byteimg 验签随即 403（表现为“抽流失败”）。
    finalUrl = String(info.url).trim();
    console.error('[khd_play 自解] type=' + info.type + ' code=' + info.code);
  } else if (capturedResp) {
    const crypto = require('crypto');
    const pb = pbParse(capturedResp);
    console.error('[pbfields] ' + Object.keys(pb).map(k => k + ':' + (Buffer.isBuffer(pb[k]) ? pb[k].length + 'B' : JSON.stringify(pb[k]))).join('  '));
    const field2 = pb[2] ? (Buffer.isBuffer(pb[2]) ? pb[2].toString('utf8') : String(pb[2])) : null;
    const field5 = pb[5] && Buffer.isBuffer(pb[5]) ? pb[5].toString('utf8') : null;
    console.error('[pb] field2=' + field2 + '  field5.len=' + (field5 ? field5.length : 'null'));
    if (field5) {
      // L1: AES-128-CBC/PKCS7，key/iv = md5(seed)[0:16]/[16:32] 的 UTF-8 字节
      // seed 按响应分支不同（运行时钩 CryptoJS.MD5 实测）：
      //   field4=5  (mp4 分支) → String(field4) + field2 + field3
      //   field4=10 (hls 分支) → String(field4) + meta1 + meta2 + field2 + field3
      // meta1 = <meta http-equiv=Content-Type> 的 id，meta2 = <meta name=viewport> 的 id 去掉 "viewport" 前缀
      const field3 = pb[3] ? (Buffer.isBuffer(pb[3]) ? pb[3].toString('utf8') : String(pb[3])) : G.Version;
      const field4 = pb[4] != null ? String(pb[4]) : '';
      const seeds = [field4 + field2 + field3,
                     field4 + (G.meta1 || '') + (G.meta2 || '') + field2 + field3];
      const ct = Buffer.from(field5, 'hex');
      let L1 = null;
      let rawDump = [];
      for (const seed of seeds) {
        const md5s = crypto.createHash('md5').update(seed, 'utf8').digest('hex');
        const key = Buffer.from(md5s.slice(0, 16), 'utf8');
        const iv = Buffer.from(md5s.slice(16, 32), 'utf8');
        for (const autopad of [true, false]) {
          try {
            const dc = crypto.createDecipheriv('aes-128-cbc', key, iv);
            dc.setAutoPadding(autopad);
            const out = Buffer.concat([dc.update(ct), dc.final()]);
            const t = out.toString('utf8').replace(/\u0000+$/, '').trim();
            rawDump.push('seed=' + seed.slice(0, 20) + '… pad=' + autopad + ' :: ' + t.slice(0, 80));
            if (/^[0-9a-fA-F]{32,}$/.test(t)) { L1 = t; break; }
          } catch (e) { rawDump.push('seed=' + seed.slice(0, 20) + '… pad=' + autopad + ' ERR ' + e.message); }
        }
        if (L1) break;
      }
      console.error('[L1raw] ' + rawDump.join('\n         '));
      console.error('[L1] ' + (L1 ? L1.slice(0, 40) + '… len=' + L1.length : 'null'));
      if (L1 && store.__hxmStore.hxm_aesdecrypt) {
        const j = store.__hxmStore.hxm_aesdecrypt(L1);
        console.error('[L2] ' + String(j).slice(0, 120));
        let obj = null; try { obj = JSON.parse(j); } catch (e) { }
        if (obj && obj.url) {
          let u = obj.url;
          if (store.__hxmStore.hxm_decrypt) { try { const d = store.__hxmStore.hxm_decrypt(u); if (d && /http|%2F|\//i.test(d)) u = d; } catch (e) { } }
          // 同上：原样保留编码，别 decodeURIComponent（否则签名字节被解坏 → 403）
          finalUrl = String(u).trim();
          // 手动解密分支也要把 info 补上，否则下面的 msg 判据会把真流误判成兜底
          info = obj;
          global.info = obj;
        }
      }
    }
  }

  if (!finalUrl) { console.error('!! 未能还原最终直链'); process.exit(5); }
  // 机器可读结果行：Python 侧靠 msg 区分「真流」与「服务端兜底视频」。
  // 实测：解析成功时 info 必带 msg（"正常" / "数据正常"）；解析失败时服务端返回
  // 一个固定的 OSS 兜底 mp4（kjjsaas-sh.oss-cn-shanghai.aliyuncs.com/u/3401405881/
  // 20240818-936952-….mp4，784109B），且 info 里**没有** msg 字段。
  const result = {
    url: finalUrl,
    type: (info && info.type) || null,
    code: (info && info.code) || null,
    success: (info && info.success) || null,
    msg: (info && info.msg) || null,
  };
  console.log('***RESULT*** ' + JSON.stringify(result));
  console.error('\n***FINAL URL*** ' + finalUrl);
  try { fs.writeFileSync(path.join(require('os').tmpdir(), 'bd_url.txt'), finalUrl); } catch (e) { }
  process.exit(0);
})().catch(e => { console.error('FATAL ' + (e && e.stack || e)); process.exit(9); });
