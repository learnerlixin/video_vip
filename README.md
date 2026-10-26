# video_vip —— 全网 VIP 视频解析播放的命令行工具

在爱奇艺 / 腾讯 / 优酷 / 芒果TV 等视频页上识别片名，调用第三方解析源或无损云直连
拿到 m3u8 / mp4 真流，再用本地播放器或浏览器播放。支持整季选集、自动连播、保存 mp4。

实现上不用浏览器：`requests` 抓页面 + 终端 UI + 本地播放器（mpv / ffplay）或系统浏览器。

> 仅限学习交流。资源均来自互联网第三方解析接口，请自行遵守相关站点的服务条款与版权规定。

---

## 目录

- [快速开始](#快速开始)
- [交互命令](#交互命令)
- [解析源与 `--source`](#解析源与---source)
- [片名 → 官方页 URL](#片名--官方页-urlsite_searchpy)
- [播放后端](#播放后端)
- [自动连播](#自动连播--auto-next--x)
- [保存为 mp4](#保存为-mp4--save--d)
- [HLS 并发预取](#hls-并发预取prefetchpy)
- [第三方源抽流](#第三方源抽流jx_resolvepy)
- [模块一览](#模块一览)
- [核心流程](#核心流程)
- [实现上的取舍](#实现上的取舍)
- [已验证](#已验证)

---

## 快速开始

依赖：Python 3.10+、`requests`、`beautifulsoup4`。可选：`rich`（彩色输出与进度条，
缺失时自动降级为纯文本）、`ffmpeg`（仅保存 mp4 时需要）、`node`（仅冰豆源需要）。

```bash
# 方式零：不带任何参数启动 —— 进程序后再交互式选播放方式/片名或URL/解析源/集数
python -m video_vip

# 方式一：给视频页 URL（完整流程：识别片名 → 搜索 → 选片 → 取 m3u8 → 播放）
python -m video_vip https://v.qq.com/x/cover/rjae621myqca41h/i0032qxbi2v.html

# 方式二：直接按片名搜索（跳过页面抓取）
python -m video_vip --title 庆余年

# 方式三：只给片名也能走第三方解析源（自动站内搜索找到官方页 URL）
python -m video_vip --title 庆余年 --episode 5 -s 2 --site 爱奇艺

# 只保存不播放
python -m video_vip --title 庆余年 --episode 5 --save

# 列出解析源与本机可用播放后端
python -m video_vip --list
```

### 全部参数

| 参数 | 说明 |
|---|---|
| `url` | 视频页 URL（等价于浏览器里打开这个页面） |
| `-t, --title` | 跳过页面抓取，直接用这个片名搜索 |
| `-e, --episode N` | 指定播放第几集（从 1 开始） |
| `-s, --source N` | 解析源编号（`--list` 查看）；默认 `1` LX解析 |
| `--site` | `{腾讯视频,爱奇艺,优酷,芒果TV}` 配合 `--title`+第三方源，限定优先去哪家官方站找页 |
| `-p, --player` | `{auto,prefetch,mpv,ffplay,embed,browser}`；默认 `embed` |
| `--prefetch-workers N` | 预取并发数（默认 8） |
| `--prefetch-ahead N` | 起播前预取的分片数（默认 4） |
| `--keep-cache` | 退出后保留预取的分片缓存 |
| `--save` | 只保存为 mp4，不播放 |
| `--out PATH` | 保存路径（文件或目录） |
| `--variant` | `{highest,lowest,auto}` 清晰度选择 |
| `--workers N` | 下载并发数（默认 8） |
| `--auto-next` | 一集播完自动切下一集 |
| `--no-auto` | 不自动解析，只打印菜单 |
| `--no-interact` | 播完不进交互循环，直接退出 |
| `--mobile` | 按移动端 UA/布局处理 |
| `-q, --quiet` | 精简输出 |
| `--version` | 打印版本信息后退出 |

---

## 交互命令

播放起来后进入交互循环（`h` 随时查看速查表）：

| 命令 | 作用 |
|---|---|
| `n` / `p` | 下一集 / 上一集 |
| `<数字>` | 跳到第 N 集 |
| `l` | 列出全部集数（自适应多列网格，`[N]` 高亮当前集） |
| `m` | 重选影片（沿用当前解析源，回到第 1 集） |
| `s` | 切换解析源（沿用当前片名，回到第 1 集） |
| `r` | 重新加载当前集 |
| `a` | 开/关自动解析 |
| `x` | 开/关自动连播 |
| `d` | 保存当前集为 mp4 |
| `u` | 打印 m3u8 直链与播放页 |
| `h` | 查看帮助 |
| `q` | 退出 |

**`m` 与 `s` 统一重跑**：循环内记住「当前片名 + 当前解析源」，改其中一项会用新的组合
重新起播并回到第 1 集，不会再把另一项重置回默认。第三方解析源解析的是**具体视频页**，
所以「换片名 + 第三方源」时会借道站内搜索（`find_page_url`）定位该片名在官方站的页面
再喂给所选源；找不到就如实回退无损云。无损云会话（无视频页）也能用 `s` 换到第三方源。

用 `embed` 后端时，播放页右侧的选集栏也能点（桌面端鼠标移到播放器右缘展开，
移动端点「☰ 选集」），与终端指令双向同步。

**第三方源的整季选集**：第三方解析源解析的是**单个视频页**，`showPlayerWindow` 只
给得出当前这一页。起播后会借道 `site_search.list_episode_pages` 枚举该片在官方站的
**整季单集页**，逐条前缀 `source.url` 拼成各自的解析页地址填回选集栏——于是第三方源下
`n`/`p`/数字/`l` 同样能换集。枚举失败或该页只有一集时保持原样，不谎报集数。

**不带参数的交互式启动**：直接 `python -m video_vip` 会先进一个引导界面，依次问
播放方式（`t` 按片名 / `u` 粘贴视频页 URL）、片名或 URL、集数、解析源编号、
（第三方源+片名时）优先去哪家官方站，填完即走与带参数启动**完全相同**的流程。

---

## 解析源与 `--source`

`--list` 里的源分两类，**能不能只靠片名用**取决于这一分类：

| 编号 | 源 | 工作方式 | 只给 `--title` 能用吗 | 能 `d` 下载吗 |
|---|---|---|---|---|
| 0 | 无损云解析 | 按**片名**搜索（suggest → API 取 m3u8） | ✓ | ✓ 原生直链 |
| 1 | LX解析 | 解析**一个具体视频页**（`源前缀 + 页面URL`） | ✓（自动找页） | ✓ |
| 2 | 789解析 | 同上 | ✓ | ✓ |
| 3 | 冰豆解析 | 同上 | ✓ | ✓（需本机 `node`） |
| 4 | 麒麟解析 | 同上 | ✓ | ✓ |
| 5 | 云解析 | 同上 | ✓ | ✗ 未接入 |
| 6 | CK播放器解析 | 同上 | ✓ | ✓ |
| 7 | M3U8解析 | 同上 | ✓ | ✓ |
| 8 | PlayM3U8解析 | 同上 | ✓ | ✓ |

> 第三方源会随时间失效或更换域名。若某个源起播后白屏或报错，用 `s` 切到下一个即可；
> `--list` 始终反映 `config.py` 里的当前列表。默认源按**名称**定位（`DEFAULT_SOURCE_NAME
> = "LX解析"`）而非硬编码下标，列表增删导致编号漂移时仍指向它。

「能 `d` 下载」= 该源已在 `jx_resolve._HOST_RESOLVERS` 登记，能把解析页抽成真·媒体直链
（详见[第三方源抽流](#第三方源抽流jx_resolvepy)）。未登记的源按 `d` 会如实提示
「尚未接入下载接口，请换无损云」，绝不臆造地址。

第三方源拼的是 `source.url + 页面URL`（即 `showPlayerWindow` 里那一行），
本身必须搭配视频页 URL。`--title` 只给片名时，CLI 会先自动补上「片名 → 官方视频页
URL」这一步，再把它交给完整注入流程：

```
$ python -m video_vip --title 庆余年 --episode 5 -s 2 --site 爱奇艺
  正在 爱奇艺 站内搜索「庆余年」...
  ✓ 按片名找到爱奇艺（第5集）视频页：
      https://www.iqiyi.com/v_19rv0fxxdg.html
  正在用「789解析」解析该页...
```

---

## 片名 → 官方页 URL（`site_search.py`）

为什么用 Bing `site:` 站内搜索而不是各家原生搜索接口：实测原生搜索在本机环境都不可用
（腾讯视频 `vd.l.qq.com/proxyhttp` 要未公开的 buid；爱奇艺 suggest 子域连不上、主站是纯
JS 壳；优酷 `so.youku.com` 返回 JS 跳转壳）。而 Bing 的 `site:` 站内搜索能稳定返回真实
官方页 URL，且不依赖额外鉴权。

覆盖**四家**官方主站，`--site` 可指定优先站、不指定则依次尝试：

| 站点 | 命中页 | 集数处理 |
|---|---|---|
| 腾讯视频 | `v.qq.com/x/cover/<cid>/<vid>.html` | 纯 requests 打通 `pbaccess.getPage`（逆向 JS bundle 得到精确请求体：snake_case `page_params` + `page_bypass_params` + `vversion_name=8.5.96`），从返回 CardList 的 `pc_web_episode_list` 卡按集号取 vid；超 30 集走 tabs `page_context` 翻页 |
| 爱奇艺 | `www.iqiyi.com/v_<vid>.html` | 桌面页被反爬挡，改抓**专辑页** `/a_<aid>.html`，其中静态内嵌整季 `/v_` 列表，按集号精确取 |
| 优酷 | `v.youku.com/v_show/id_<vid>.html` | 桌面页被阿里 **x5sec 反爬盾**（返 1.5KB 空壳）；借道 `m.youku.com/video/id_<vid>.html` 不挡，从 `window.__INITIAL_DATA__` 内嵌 JSON 按 `stage` 取 vid 再拼回桌面 URL。Bing 对 `site:youku.com` 的结果 2026-10 起整体改返回 `play.tudou.com` 镜像域（同库同 vid），两域都认 |
| 芒果TV | `www.mgtv.com/b/<cid>/<vid>.html` | 播放页是 Nuxt 空壳，选集靠官方分集接口 `pcweb.api.mgtv.com/episode/list` 异步拉 |

`SiteHit.episode_pinned` 如实标出是否**真的**定位到了那一集：枚举命中 → `True`；
枚举失败（网络异常/超总集数）退回整季 cover 页 → `False`。**绝不因集数搜不到就返回 None。**

---

## 播放后端

「播放」最终要落到一个能渲染 m3u8 的执行者上（无损云的播放页是
`https://wsyzy.vip/m3u8/?url=<m3u8>`）。于是抽象成几个后端
（`--player` 指定，**默认 `embed`**；`--player auto` 才按 mpv → ffplay → embed → browser
顺序探测）：

| 后端 | 说明 | 能否判断播完 |
|---|---|---|
| `embed` | 本地 HTTP 服务 + 自有播放页（hls.js 直接播 m3u8），**默认** | ✓ 真 `ended` 事件 |
| `prefetch` | 并发预取分片到本地，用本地播放器播（见下） | ✗ 靠时长启发式 |
| `mpv` | 调本机 `mpv` | ✗ 靠时长启发式 |
| `ffplay` | 调本机 `ffplay` | ✗ 靠时长启发式 |
| `browser` | 用系统浏览器打开播放页 | ✗ |

### `embed` 后端为什么要自建服务

把第三方播放页塞进 iframe 再用 `webbrowser.open` 打开 `file://` 页面时，Python 完全
看不到播放状态：iframe 跨域，读不到里面的 `<video>`，也收不到 `ended`。实测无损云的
m3u8 与分片都返回 `Access-Control-Allow-Origin: *` 且不加密封装，于是换成
**我们自己的页面 + hls.js 直接播 m3u8**——`<video>` 在自己手里，`ended` 就是真事件。

页面要把事件送回 Python，就需要一个本地 HTTP 服务（`file://` 页面无法可靠回传）。
`embedsrv.py` 既当静态服务器（页面 + hls.js），又当回调收集器（`/event`）。

**一个标签页播完整部剧**：换集只是改服务端下发的 state（带一个新 token），页面自己
切源；标签页只在「还没有活着的页面」时才开一次（实测连播 3 集只开 1 个标签）。
页面拿 state 用 **`/wait` 长轮询**而不是 `setInterval`——Chrome 会节流后台标签页的
定时器（intensive throttling 最狠 1 分钟一次），定时轮询会导致「播完了半天不切下一集」；
长轮询是一次挂起的 HTTP 请求，不受节流影响（实测 token 变化后 0.6s 内页面就切了源）。

**四种 state 模式**：`hls`（裸 m3u8，能判断播完）、`direct`（裸 mp4 等浏览器原生能播的，
能判断播完；hls.js 播不了 mp4，必须分开处理）、`iframe`（第三方 HTML 解析页，只能塞
iframe，**不能**判断播完）、`halt`（Python 侧退出，让页面停播并显示说明）。

**页面内回退**：hls.js 播不了某个地址（CORS 被拦 / 清单 404 / 加密方式不支持）时，
页面自己换成 iframe 播并回传 `fallback` 事件。**画面优先于「能不能自动连播」**：
用户至少能看，Python 侧则据 `fallback` 把 `detectable()` 转 False，自动连播对这一集
停用并明确告知，而不是静默失效或误判。分片级的网络错误走「startLoad 重试」，不回退。

---

## 自动连播（`--auto-next` / `x`）

换集的判据有**两条路径**：

### 权威信号（`embed` 后端）

自有播放页里的 `<video>` 播完会派发真 `ended`，页面通过本地服务回传给 Python。
后端用 `end_confirmed()` 暴露它，`AutoNext` 见到就**直接算播完**，不看耗时——
真事件没有歧义，套耗时启发式反而会因为「时长探测失败」而漏切。

### 启发式（本地播放器 mpv / ffplay / prefetch）

拿不到 `ended`，只能靠「已播时长 ≥ 视频时长 - 阈值」推断。这条路径**保守优先**：
探测不到时长就不切，宁可漏切也不误切（误切会把用户正在看的片子掐掉）。

---

## 保存为 mp4（`--save` / `d`）

`download.py` 复用 `prefetch.py` 的能力，分两步：

1. **并发下载**：`parse_hls()` 解析清单 → `Prefetcher` 多线程拉分片到本地。
   单连接被限速在 ~1.2 Mbps，8 路并发能到 ~4.8 Mbps，整集 45 分钟约 400MB，
   顺序下载要等很久，所以必须并发。
2. **封装 mp4**：ffmpeg 读本地 `index.m3u8`，`-c copy` 直接拷贝流、不重编码。
   分片本来就是 H.264/AAC，重编码只会白白损失画质又慢上几十倍。

几种特殊情况：

- **加密流**（`EXT-X-KEY`）：`parse_hls()` 已把 key URI 本地化，`Prefetcher.start()`
  会先下密钥再下分片，ffmpeg 读本地清单时能正常解密。
- **BYTERANGE 分片**：分片是同一（或各自）文件的不同字节区间。不交给 ffmpeg 远程直连——
  这类源的 byterange URL 常以 `.image`/`ts.php` 结尾，ffmpeg 的 hls demuxer 会按分片
  扩展名白名单拒掉（且无开关可绕）。改由 `Prefetcher` 按每片 `offset@len` 发 Range
  请求下成独立本地 `.ts`，再走本地封装。
- **非 HLS 的直链单文件**（`heycan-sign.byteimg.com` 这类 `~tplv-*-image.image` 签名链、
  裸 mp4/flv）：没得分片可下，但也不是不能并发——它是**一个完整文件**，CDN 认 `Range`。
  先探一次头（`bytes=0-65535`）拿到总长与 `Accept-Ranges`，再按 `workers` 路 Range 分段
  并发下到本地、顺序拼成整文件，最后只做一次**本地**封装（容器对得上就直接改名，不碰
  ffmpeg）。签名参数 `x-expires`/`x-signature` 对整条 URL 生效，**原样透传、绝不改写**
  （改任何一段都会 403）。服务器不理 Range（只回 200 全文件）时退回 ffmpeg 单连接远程 remux。
- **没有 ffmpeg**：抛 `DownloadError`，提示安装（不静默失败）。

输出先写 `<名字>.part.mp4`，成功后再改名——中途失败不会留下一个看起来完整、实际损坏的 mp4。

> `--save` 走的是无损云搜索取地址，`-s` 在这里不生效（会明确提示）。
> **交互循环里的 `d` 才支持第三方源**：它先用 `jx_resolve` 把解析页抽成真·m3u8 再下。

---

## HLS 并发预取（`prefetch.py`）

为什么需要它：无损云的分片服务器对**单连接**限速，
实测单连接只有 ~1.2 Mbps，而 720p 片源需要 ~1.4 Mbps——顺序取流必然追不上实时播放
（模拟 40s 视频卡 4 次）。但同一台服务器**并发**能拿到高得多的聚合带宽：

```
并发  1 路:  1203 kbps   ✗ 不够
并发  4 路:  1733 kbps   ✓
并发  8 路:  4777 kbps   ✓
并发 16 路:  8975 kbps   ✓
```

播放器（ffplay/mpv/浏览器 hls.js）都是顺序取流，改不了这个瓶颈。所以改成：用线程池
并发把分片拉到本地，同时写一份指向本地文件的 playlist，缓存够几片就起播，播放器往后
播时文件已经就位。PoC 实测：3.9s 起播，60s 视频 0 次缓冲抽干。

关键实现点：

- 逐行改写 playlist，**保留所有标签**（`EXT-X-DISCONTINUITY` / `EXT-X-KEY` / `MAP` 等），
  只把裸 URI 行换成本地文件名——重建 playlist 会丢掉 DISCONTINUITY 导致时间轴错乱。
- 分片按顺序提交给线程池（FIFO 队列 → 大致顺序下载，8 片在飞），保证播放头前面的分片优先就位。
- 本地文件播放器不认 HTTP 选项（`-user_agent` / `-reconnect` 会报
  "Option user_agent not found"），所以起播用的是 `LOCAL_*` 参数集。

---

## 第三方源抽流（`jx_resolve.py`）

第三方源每条每集链接是 `source.url + 官方页URL`，那本身是一个 **HTML 播放页**（不是流），
直接喂给 `download.py` 的 `parse_hls` 会失败。真正出流的那步发生在浏览器 JS 里——
本模块不跑浏览器，只把播放器页自己的请求照抄出来。

`resolve()` 按「站点 host → 抽流函数」映射（`_HOST_RESOLVERS`）分发，每个 URL 只走匹配它
的那一条。已接入五条家族：

| 家族 | 链路 |
|---|---|
| **789 / M3U8 / PlayM3U8 / CK** | `api-api.789jiexi.net` 的 ArtPlayer 家族——逐层跟 `<iframe>` 下钻到含 `config{url,time,key}` 的播放器页，POST 同域 `api.php{url,time,key}`，把响应 `data.url`（AES-CBC-128/PKCS7 密文，key/iv 写在同站 `setting.js`）解密得明文 m3u8 |
| **公益**（`jiexi.site`） | 苹果CMS/xyplayer 明文 JSON，GET `api.php?tp=play&url=官方页` 直回 m3u8 |
| **LX**（`jx.lxyy.club`） | 壳页取 `data-video`/`data-code`，POST `public.php{url,code}` 回 ticket m3u8 |
| **麒麟**（`free.maccms.xyz`） | ArtPlayer 变体：解析页取内联 `apiToken` → GET `api/resolve.php?token=` → 明文 JSON 里的 m3u8；先带同源 Referer 试，不行再去掉 Referer 试（`open.*.cn` 的 302 票据常认同源 Referer） |
| **冰豆**（`bd.jx.cn`） | 见下 |

未接入的源视为「不支持」：`is_supported()` 返回 `False`、`resolve()` 不发任何请求直接
返回 `None`，由调用方如实报错（改用无损云下载），**绝不盲目下钻或臆造地址**。
除冰豆走随包 Node 脚本外，其余均为纯 requests + cryptography（AES），零浏览器。

### 冰豆（`bd.jx.cn`）

三层跳转后进播放器页，取流核心是**两段 TinyGo 编译的 WASM**，纯 Python 跑不了，
故随包带一段 Node 脚本 `bdjs/bd_run.js` 复现整条链：

```
bd.jx.cn/?url=X  →  iframe jiexi.bot.cd/?url=X  →  iframe /jx/?url=X
   ↓ tripcdn.js（内嵌 WASM #1，导出 hxm_encrypt）
   ↓ play_vid = hxm_encrypt(btoa(url))
GET https://123.129.229.14:4433/Index.php?play_vid=…&ref=X
   ↓ 抽出页面全局：Vurl / Time / Version / Ref / meta1 / meta2
   ↓ khd_play.js（内嵌 WASM #2，导出 hxm_decrypt/aesencrypt/aesdecrypt）
POST {Api}/report-uuid/{uuid}     Content-Type: application/x-protobuf
   body    = protobuf{1: hxm_aesencrypt(JSON{url,wap,ios,host,referer,time})}
   headers = Video-Parse-Time / -Version / -Sign=hxm_aesencrypt(Host+…+Time+…+Version)
   ↓ 响应 protobuf{2:field2, 3:Version, 4:分支, 5:密文hex}
   ↓ AES-128-CBC/PKCS7，key/iv = md5(seed)[0:16]/[16:32] 的 UTF-8 字节
   ↓ hxm_aesdecrypt → JSON → hxm_decrypt → decodeURIComponent → 真直链
```

**seed 按响应分支不同**（钩 `CryptoJS.MD5` 实测得到）：

| `field4` | 分支 | seed |
|---|---|---|
| 5 | mp4 | `field4 + field2 + field3` |
| 10 | hls | `field4 + meta1 + meta2 + field2 + field3` |

`meta1`/`meta2` 是页面两个 `<meta>` 的 `id` 属性（viewport 那个要去掉 `viewport` 前缀）。

两个必须防的坑：

1. **解析失败时服务端不报错**，而是回一个**固定**的兜底宣传片 mp4
   （`kjjsaas-sh.oss-cn-shanghai.aliyuncs.com/…`，784109B）。判据：成功响应 `info`
   必带 `msg`（`正常`/`数据正常`），兜底响应没有 `msg`；再叠一层 host 黑名单兜住。
2. **直链的 Content-Type 与扩展名都会骗人**：HLS 端点回 `image/vnd.microsoft.icon`、
   正文却是 `#EXTM3U`；真 mp4（实测 700MB/479MB）挂在 byteimg 的
   `.jpg~tplv-…-image.image` 路径下、回 `image/jpeg`。故一律**嗅探首字节魔数**判定
   （`_sniff_media`），绝不信响应头/后缀。嗅探只发一次请求——冰豆 HLS 直链带
   `ckey`/`time` 票据，多打一次有耗票风险。

`bdjs/` 目录自带 `go.wasm` + `wasm_exec.js`（离线可用），`global.min.js` 与
`khd_play.js` 首次运行时抓取并缓存到同目录。**需要本机装有 `node`**；没有 node 时
`_resolve_bd` 返回 `None`，调用方如实报错换源。

---

## 模块一览

| 模块 | 职责 |
|---|---|
| `config.py` | 解析源列表（`VIDEO_PARSE_LIST`）/ 站点容器（`PLAYER_CONTAINERS`）/ `@include` 规则 |
| `gm.py` | 持久化存储 / 打开链接 / 样式收集 |
| `net.py` | HTTP 请求层（`req()`） |
| `page.py` | `window` / `document` / `location` 的最小 DOM 兼容层 |
| `util.py` | DOM 工具（`find_target_ele` / `remove_video` / `cleanup`）+ 页面会话 `PageSession` |
| `title.py` | 片名识别与清洗（`wsyzy_clean_title` / `read_video_title` / `wait_stable_title`） |
| `wsyzy.py` | 无损云直连（搜索 / 排序 / 详情 / 选集 / 播放 / 中止） |
| `site_search.py` | 片名/集数 → 官方视频页 URL |
| `player.py` | 播放页布局与样式构建 + 播放后端 |
| `embedsrv.py` | embed 后端的本地 HTTP 服务与自有播放页 |
| `ui.py` | 终端 UI（toast / 菜单 / 选集栏 / 进度条） |
| `consumer.py` | 解析源菜单与播放调度（`Consumer` / `start`） |
| `autonext.py` | 自动连播监视器 |
| `prefetch.py` | HLS 并发预取引擎 |
| `download.py` | 保存为 mp4 |
| `jx_resolve.py` | 第三方解析页 → 真·媒体直链 |
| `bdjs/bd_run.js` | 冰豆取流链的 Node 复现（含两段 WASM） |
| `cli.py` | 命令行入口 |

---

## 核心流程

```
接管播放器容器 takeover()
  → 等片名稳定 waitStableTitle()      精确选择器/og:title 命中即返回；
                                      否则等 1.5s 稳定且清洗通过，或 5s 兜底
  → suggest 搜索 searchWithRetry()    首搜失败则用 wsyzyCleanTitle 清洗后重搜
  → 候选排序 rankCandidates()         精确(4) > 前缀(3) > 包含(2) > 其它(1)
  → 多候选时 askChoice()              终端里选片
  → 取详情 getDetail()                拿到选集列表
  → 定位当前集 curEpNum()             从 URL/标题里读出集号
  → 取 m3u8 并播放                    按 --player 选后端
```

---

## 实现上的取舍

不跑浏览器，所以一些依赖真实渲染的能力没有对应物，处理方式如下（代码里逐处注明）：

| 需要的能力 | 这里的做法 |
|---|---|
| `getBoundingClientRect` / `getComputedStyle` | 无真实布局，传 0；`build_player_frame_layout` 的数值分支仍完整保留 |
| `MutationObserver` + `HTMLMediaElement.play` hook | 页面快照是一次性的，不监听后续 DOM 变化 |
| `setInterval` / `setTimeout` | `threading.Timer` / 循环 + `time.sleep` |
| 持久化存储 | 本地 JSON 文件（`gm.py`） |
| iframe 内嵌播放 | 播放后端抽象（embed / prefetch / mpv / ffplay / browser） |
| `ended` 事件 | embed 后端回传真事件；其它后端用时长启发式 |

### 终端折行：`toast` 用 `soft_wrap`

`rich` 默认会按终端宽度硬折行，中英混排 + emoji 时折行位置很难看。所有 `toast`
输出走 `soft_wrap=True`，由文案自己控制换行（`\n`），不依赖终端宽度。

---

## 已验证

* **纯函数单测**：`wsyzy_clean_title` / `is_clean_title` / `parse_eps` /
  `rank_candidates` / `cur_ep_num` / `parse_pixel_value` / `build_player_frame_layout`
  在同一批用例上逐条比对 —— **198 项全部一致**。
* **真实链路**：bilibili / 腾讯视频 / 爱奇艺页面抓取 → 片名识别 → 无损云搜索 →
  详情 → 取 m3u8 → ffplay 实际解码播放（`ffmpeg -i <m3u8> -f null -` 验证流可用）。
* **单元/集成**：GM 存储与索引迁移、菜单分组、弹窗播放、自动解析、`_running`
  守卫、`abort` 中止、广告声抑制器幂等、多候选面板排序、交互换集 —— 合计 332 项通过。
* **自动连播（本地播放器）**：先用真实 ffplay 实测各场景退出码（正常播完 / 文件损坏 /
  文件不存在 / `terminate()` / `kill()`），确认「退出码不可区分」这一前提；再分别用假后端与
  **真实 ffplay 进程 + 真实本地 mp4**、以及 **真实 `interactive()` + 真实 `PrefetchBackend`
  + 本地 HTTP HLS 服务**跑通端到端。覆盖：连播到末集并停用、`x` 关/开、默认不开、
  手动 `n`/`p`/数字换集与自动连播并存、`r` 重载后计时归零、越界集号、坏文件秒退
  连续 3 次后停用、提前关窗口不被误判为播完、切集延迟期间不重复切集、`browser` 后端
  明确告知不可用。其中「切集延迟导致重复切集」一条还做了反向验证 —— 用子类复现修复前
  的旧逻辑，确认它确实产生重复 `advance`（`[1,1,1,1,2,2,2,2,3]`），证明该测试有效。
* **自动连播（embed）**：全部用**真实 ffmpeg 造的 HLS + 真实 playwright chromium +
  真实 `EmbedBackend`/`EmbedServer`/`AutoNext`/`cli.interactive()`** 跑，不打桩。
  正向：两集连播到末集并停用（收到 2 个真 `ended`，判定理由为「收到播放页的真实
  ended 事件」）、三集连播只开 1 个标签页、页面标题随集号变成 `片名 · 集名`、
  选集栏高亮跟随、移动端「☰ 选集」按钮可展开/收起而桌面端没有、真实 `interactive()`
  里全程无人工干预自动连播并在退出时收到 `halted` 回执（页面真的停播）。
  负向（README 的核心承诺是**不误切**）：第三方解析页走 iframe 时 `can_detect_end=False`
  且等 4s 也不切集；hls.js 清单加载失败时页面回退到 iframe、产生一条明确提示、
  `can_detect_end` 转 False 且不切集；`ended` 已记录的情况下重载同一集**不会**瞬间跳
  （实测 3.8s ≈ 真实重播时长，且该 token 的事件序列是完整的
  `manifest→applied→metadata→playing→beat→ended`，证明确实从零重播了一遍）。
  另验证：长轮询对 token 变化的响应延迟 0.6s（远小于 20s 超时，即换集不被后台标签页
  节流拖住）、`pagehide` 信标能让 `page_closed()` 转 True 且不被仍在跑的长轮询抹掉、
  浏览器打不开时明确告警并给出 `--player mpv/ffplay` 替代方案、不调 `stop()` 也能
  0.04s 干净退出（长轮询 handler 是 daemon，不会拖住解释器）、embed 后端下
  `probe_duration` 被调用 **0 次**而 ffplay 后端下 **1 次**（证明「有权威信号就不探时长」
  这条优化真的生效，且没有误伤本地播放器路径）。
  本地播放器路径同步做了回归：真实 ffplay 播完两集仍走耗时启发式
  （判定理由为「耗时 3.7s / 预期 3s」，证明新增的 `end_confirmed` 分支没有劫持老路径）、
  坏文件连续 3 次秒退后停用且没一路跳到末集、播放中被 `stop()` 不算播完。
* **并发安全**：`play()` 会被三个线程同时调用（CLI 主循环、自动连播监视线程、
  页面选集轮询线程），所以「起本地服务」这一段必须串行化。实测 8 线程并发
  `play()` 只产生 1 个服务；`play()` 与 `stop()` 交错 4s 后没有孤儿服务。
  这一条做了**反向验证** —— 用子类复现修复前不持锁的逻辑，同样场景下它产生了
  **8 个服务**，且 `stop()` 只关掉 1 个、留下 **7 个永远关不掉的孤儿**（各占一个端口），
  证明竞态真实存在、测试确实测得出来、修复是必要的。
* **真实片源**：从无损云 API 搜到真实 m3u8（庆余年，时长 7265s），在 chromium 里
  6.6s 内起播成功（`live_mode=hls`、`can_detect_end=True`、`currentTime` 持续推进），
  确认 CDN 的 CORS 与分片在浏览器里确实可用 —— 这是整个 embed 方案的前提。
* **服务消失后的页面行为**：`--no-interact` 下 `main()` 故意不收播放器（「命令返回时
  不该把刚起播的播放器一起杀掉」），但 embed 的「播放器」是浏览器页面，它靠我们的
  本地服务拿 state。实测两种收尾：正常 `stop()` 会下发 `halt`，页面收到后置
  `stopped`、停掉全部轮询与心跳（8s 内只剩 1 个 `halted` 回执，视频暂停、src 清空、
  显示「已停止」）；Python 被强杀则页面收不到 halt，靠退避重试维持 1.6 次/秒，
  同时**视频照常播**（hls.js 的分片直连 CDN，不经过我们的服务）。
  这一条测出了一个真缺陷：`/wait` 在服务关闭时会立刻返回，而页面的 `waitLoop`
  拿到响应就无条件重发 —— 实测退化成 **525 次/秒**打向死端口的热循环。修法是给
  两次长轮询之间加 1s 最小间隔（`WAIT_MIN_GAP`），修完降到 1.6 次/秒；
  并复验该下限**不影响**换集延迟（实测 `ended` → 切集 0.02s / 0.05s，因为 token
  变化时长轮询是立刻返回的，下限只在「服务已死、秒回」时才生效）。

* **`--source` 语义**：实测出两个缺陷并修掉（详见上文「解析源与 `--source`」）。
  ① `args.source` 原先只在「只给视频页 URL」那一条分支被读取，`--title` 的两条路径
  与 `--save` 完全无视它 —— 用户 `-s 2` 却走了无损云且毫无提示；现在统一在 `main()`
  入口校验，并对「第三方源 + 只有片名」这种物理上不可能的组合明确告知并给出可行命令。
  ② `main()` 先把编号写进 GM 存储、`start()` 里的「一次性索引迁移」再无条件 +1，
  于是 `-s 2`（七哥解析）实际生效的是 3（TXNQ解析）—— 实测复现；现在 `--source`
  改由 `start()` 在迁移**之后**写入（迁移的对象是「老版本存下来的编号」，命令行给的
  本来就是新表编号）。同时验证迁移本身没被破坏：不传 `--source` 时老值 1 仍正确迁移成 2。
  另修掉 `--no-interact` 下 `choose_source(default=0)` 硬编码 0、把 `-s` 又忽略一次的问题。

* **片名 → 官方页（`site_search`）**：无代理环境下端到端实测「`--title`+第三方源→找页→
  站点识别→源 URL 拼接」整条链：庆余年（腾讯 ✓ cover 页）、庆余年 ep5 `--site 爱奇艺`
  （✓ 精确单集页 `v_19rv0fxxdg.html`，专辑页静态选集取第 5 个）、狂飙（腾讯 ✓）三条命中，
  `url_supported` / `find_player_node` 均识别为对应官方站、七哥源拼接正确。凡人修仙传实测
  Bing 三家返回**字节完全相同**的通用结果（百科/B 站/起点），确认是 Bing 忽略 `site:` 的覆盖
  天花板、非网络或代码问题——平台正则过滤掉非官方结果后判为未命中、优雅退回无损云。

* **优酷集数枚举（`_youku_episode_url`）**：嫁金钗端到端实测，桌面 v.youku.com 被 x5sec 挡、
  但**移动 m.youku.com 不挡**且 `window.__INITIAL_DATA__` 内嵌 1–19 集 `(stage, vid)` 配对；
  `_YK_STAGE_VID_RE` 从 dump 的 HTML 里精确匹出 20 条、去重 19 集 stage→vid 全对齐；
  `find_page_url(嫁金钗, ep=17, prefer=优酷)` 返回 `pinned=True`、URL 是
  `https://v.youku.com/v_show/id_XNjU3NjgxNTU1Mg==.html`（真单集页、与 album 不同）——
  **用户报的 `--episode 17 都是指向的同一集` bug 从此真的解了**。嫁金钗共 30 集、
  移动页首屏内联了 19 集；`scene=all`/`count=100` 查询参数不改变内联范围（要突破 19+
  得走另一条 youku 分页 API，未继续追）。

* **腾讯 pbaccess.getPage 枚举（`_tencent_episode_url`）**：从 JS bundle（index-B5i6QAi5.js）
  逆向出精确请求体——三处关键差异：(1) snake_case `page_params` 含全量字段（`req_from:"web_vsite"`,
  `ad_wechat_authorization_status` 等）；(2) **`page_bypass_params`** 块（`caller_id:"3000010"`,
  `scene:"desk_detail"`, `abtest_bypass_id`）缺失则 server 返空 body；(3) URL query 加
  `vversion_name=8.5.96`。庆余年 46 集端到端实测：page1 取 ep 1-30、tabs page_context 翻页取
  ep 31-46；`find_page_url(庆余年, ep=5, prefer=腾讯视频)` 返回 `pinned=True`、
  URL=`v.qq.com/x/cover/rjae621myqca41h/t00329rbass.html`（真 ep5、与 album 不同）。
  **三家集数枚举能力现已全部打通,纯 requests 无浏览器依赖。**

* **第三方源整季选集（`list_episode_pages` + `_enrich_thirdparty_episodes`）**：第三方解析源
  起播后只有「共 1 集」，因为 `showPlayerWindow` 拼的是 `source.url + 单个视频页`。现在据官方
  整季页枚举补全：`list_episode_pages(嫁金钗优酷页)` 纯 requests 精确列出 **18 集**（含此前修过的
  抢先看第18集正片 vid），逐条前缀 LX 源 `https://jx.lxyy.club/index.php?url=` 拼成各自解析页；
  端到端桩测 `_enrich_thirdparty_episodes` 得 `count=18`、当前加载页正确高亮、`prefix ok=True`。
  无损云（wsyzy）自带整季不动；`relaunch`（`m`/`s`）走第三方时同样补全，换片名后仍能 n/p/l 换集。

* **终端折行（rich + CJK 宽度）**：实测出一类「消息看着像丢了」的缺陷。rich 按**显示宽度**
  折行（中日韩算 2 列，默认终端 79 列），且会在 `✓` / `⚠` 后的空格处断词，于是一行 81 列的
  toast 被拆成「`✓` 单独占一行 + 正文」。用 `cell_len` 逐处量了所有把 `describe()` 拼进整行
  的地方（`cli.py` 成功行、`consumer.py` 带片名的成功行、`--list` 后端行），确认短片名 55 列、
  长片名 81 列、超长片名 89 列都会触发。根因修在 `ui.toast()` / `ui.set_status()` 传
  `soft_wrap=True`（折行交回终端按字符硬折），修完在宽度 79/60/40/30/20 下、含 145 列极端片名，
  emoji 均不再落单；`ep_bar` 保留 rich 折行（那是它有意的多行选集栏）。`describe()` 文案同时精简，
  让整行回到 65 列以内。
* **`describe()` 的「可连播」不能骗人**：精简文案时发现一个真缺陷 —— 原先按模式名拼文案，
  `iframe`（第三方解析页）也被标成「可连播」，但该模式下 `detectable()` 返回 False、
  `can_detect_end` 为 False，自动连播实际是停用的（README 上文与负向测试都已确认这一点）。
  改成直接取 `can_detect_end` 当判据、不另写模式表（另写必然漂移）。用真实 ffmpeg 造的
  HLS/mp4/第三方 HTML 页 + 真实 `EmbedBackend` 逐一探测四种形态，验证「文案说可连播」与
  `can_detect_end` 完全一致：`hls`✓ / `direct`✓ / `iframe`✗ / `file:// 兜底`✗，
  且四者整行宽度都 ≤79。改动后复跑 embed 两集连播端到端（真 chromium），
  `advanced=1`、收到 2 个真 `ended`、末集正确停用，未回归。

实现过程中修掉的真实缺陷：`parseFloat("211px")` 语义（取前缀数字，Python 的
`float()` 会抛异常）、`.5` 的舍入方向（`Math.round` 向上、Python `round` 是银行家舍入）、
页面编码判定（requests 对无 charset 头的 `text/html` 退回 ISO-8859-1，导致爱奇艺
标题变乱码）、`find_target_ele` 误把 `Window` 当 `Document` 查询、B 站偶发限流需重试。
* **第三方源抽流（`jx_resolve`）**：用同一条真实腾讯单集页逐源实测，789 / CK / M3U8 /
  PlayM3U8 / 麒麟 / 冰豆 六源抽出真流（四条 789 家族源回同一条 `xhyyxx.oxapi.cn`
  的 tx1080 m3u8，符合它们共用同一下钻逻辑的设计）；LX 本次返回 None（该源自身
  接口抖动，`_resolve_lx` 未改动）；未登记的云解析 `is_supported()` 返回 False、
  `resolve()` 不发请求直接 None（如实报「不支持」）。

* **冰豆取流链（`bdjs/bd_run.js`）**：端到端实测四种形态——
  腾讯真流（mp4，219MB，首字节 `ftypmp42`）、爱奇艺真流（hls，`#EXTM3U`，
  `parse_hls` 解出 19 分片）、以及两条挂在 byteimg `.jpg~tplv-…-image.image`
  伪装路径下的真 mp4（700MB / 479MB，Content-Type 分别谎报 `image/jpeg` 与
  `video/mp4`，靠嗅探首字节 `ftyp` 才认出来）。
  **兜底识别**：5 个完全无效的 URL（腾讯/爱奇艺/优酷/芒果/非视频站）全部返回
  **同一个**固定宣传片 mp4（`kjjsaas-sh.oss-cn-shanghai.aliyuncs.com/…`，784109B）
  且 `info` 里没有 `msg` 字段；真流响应必带 `msg`（`正常` / `数据正常`）。据此
  判据，5 条兜底全部被正确拒绝（`resolve()` 返回 None），未把宣传片当成正片。
  seed 公式（`field4=5` 用 `field4+field2+field3`、`field4=10` 用
  `field4+meta1+meta2+field2+field3`）是运行时钩 `CryptoJS.MD5` 实测得到的，
  不是猜的；两条分支各自解出真流即为交叉验证。
