# video_vip —— 全网 VIP 视频解析播放的命令行工具

在爱奇艺 / 腾讯 / 优酷 / 芒果TV 等视频页上识别片名，调用采集型解析源（解析具体页面）或
资源型片库（无损云 / 暴风 / 红牛，按片名直连）拿到 m3u8 / mp4 真流，再用本地播放器或
浏览器播放。支持整季选集、保存 mp4。

实现上不用浏览器：`requests` 抓页面 + 终端 UI + 本地播放器（mpv / ffplay）或系统浏览器。

> 仅限学习交流。资源均来自互联网第三方解析接口，请自行遵守相关站点的服务条款与版权规定。

---

## 目录

- [快速开始](#快速开始)
- [交互命令](#交互命令)
- [解析源与 `--source`](#解析源与---source)
- [片名 → 官方页 URL](#片名--官方页-urlsrcsite_searchpy)
- [播放后端](#播放后端)
- [保存为 mp4](#保存为-mp4--save--d)
- [HLS 并发预取](#hls-并发预取srcprefetchpy)
- [第三方源抽流](#第三方源抽流srcjx_resolvypy)
- [目录结构](#目录结构)
- [模块一览](#模块一览)
- [测试](#测试)
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

# 方式一：给视频页 URL（注入该页 → 选解析源 → 起播；只认这个页面）
python -m video_vip https://v.qq.com/x/cover/rjae621myqca41h/i0032qxbi2v.html

# 方式一′：URL 与 --title 同时给 —— 只认 URL，--title 忽略（剧名以页面识别为准）
python -m video_vip https://v.qq.com/x/cover/rjae621myqca41h/i0032qxbi2v.html -s 4

# 方式二：直接按片名搜索（跳过页面抓取）
python -m video_vip --title 庆余年

# 方式三：只给片名也能走第三方解析源（自动站内搜索找到官方页 URL）
python -m video_vip --title 庆余年 --episode 5 -s 2 --site 爱奇艺

# 只保存不播放（默认走无损云直连）
python -m video_vip --title 庆余年 --episode 5 --save

# 只保存不播放，且指定用第三方源抽流下载（解析页 → 明文 m3u8）
python -m video_vip --title 庆余年 --episode 5 -s 4 --save

# 列出解析源与本机可用播放后端
python -m video_vip --list
```

### 全部参数

| 参数 | 说明 |
|---|---|
| `url` | 视频页 URL（等价于浏览器里打开这个页面） |
| `-t, --title` | 跳过页面抓取，直接用这个片名搜索（**同时给了 URL 时以 URL 为准，`--title` 忽略**） |
| `-e, --episode N` | 指定播放第几集（从 1 开始） |
| `-s, --source N` | 解析源编号（`--list` 查看）；播放默认 `1` LX解析，`--save` 默认 `0` 无损云 |
| `--site` | `{腾讯视频,爱奇艺,优酷,芒果TV}` 配合 `--title`+第三方源，**优先**去哪家官方站找页（那家没命中会兜底到其它三家并提示） |
| `-p, --player` | `{auto,prefetch,mpv,ffplay,embed,browser}`；默认 `embed` |
| `--prefetch-workers N` | 预取并发数（默认 8） |
| `--prefetch-ahead N` | 起播前预取的分片数（默认 4） |
| `--keep-cache` | 退出后保留预取的分片缓存 |
| `--save` | 只保存为 mp4，不播放；显式 `-s` 第三方源时走「解析页抽流」下载 |
| `--out PATH` | 保存路径（文件或目录） |
| `--variant` | `{highest,lowest}` 清晰度选择（默认 highest） |
| `--prefetch-workers` | 下载/预取并发数（默认 8，`--save` 复用同一个值，不另开旋钮） |
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
| `m [片名]` | 重选影片（沿用当前解析源、**可临时换官方站**，回到第 1 集） |
| `s [编号]` | 切换解析源（同一影片、**保持在播的那一集**；菜单里 `▶` 标当前在用的源） |
| `r` | 重新加载当前集 |
| `a` | 开/关自动解析 |
| `d` | 保存当前集为 mp4 |
| `u` | 打印 m3u8 直链与播放页 |
| `h` | 查看帮助 |
| `q` | 退出 |

**`m` 与 `s` 各自记住的东西不同**：循环内记住「当前片名 + 当前解析源 + 优先官方站 +
整季入口页」。
* `m` 换片名 → 换一部片子，理应从**第 1 集**开始；如果当前是第三方源，会再问一次
  「到哪家官方站找」（回车沿用上次选的站），因为第三方源解析的是官方站的页面，
  同一部片在腾讯/爱奇艺/优酷/芒果TV 都能拿到不同的页；无损云按片名直连、不分官方站，
  所以不问。源本身不变（不会一换片名就被重置回无损云）。
* `s` 换解析源 → 同一影片换个解析接口，**保持在播的那一集**：直接拿当前集的官方页
  （选集栏里那条 `source.url + 官方页` 去掉前缀）重解析，而不是会话最初那一页；
  否则看到第 5 集换个源就被拽回第 1 集。枚举不出整季、定不到那一集时会如实说明。
  只敲 `s` 弹菜单时，**当前在用的那一行以 `▶` + 粗绿标出并注「（当前）」**——换源就是
  在几家接口之间跳，没有基准等于盲选（换源失败时保持不变的也正是这一行）。用 `p+编号`
  弹窗播放过的那个源另外注「（弹窗中）」：弹窗只是浏览器里另开的窗口，**不改**终端会话
  在用的源，所以两个标记可能落在不同的行上。
第三方源借道站内搜索（`find_page_url`）定位官方页；找不到就如实回退无损云。
无损云会话（无视频页）也能用 `s` 换到第三方源（同样带上当前集号）。

### 一行式：把追问折叠进命令

只有 `m` / `s` 支持带参数，其余命令一律不带参数（`n 3` 会被拒绝而不是静默执行 `n`）：

| 写法 | 效果 |
|---|---|
| `m 嫁金钗` | 直接换到该片名，不再追问片名/官方站 |
| `m 嫁金钗 site 爱奇艺` | 换片名**并**把爱奇艺排到站内搜索第一位（简称可写，腾讯/芒果 → 腾讯视频/芒果TV） |
| `m` | 老写法：逐项追问片名、再到哪家官方站找 |
| `s 4` | 直接切到 4 号源（`--list` 里的编号），保持在播那一集 |
| `s 麒麟` | 按源名切：先按前缀唯一命中，再退回包含匹配 |
| `s p4` / `s p 4` | 切到 4 号源并**弹窗**播放（无损云没有可弹窗的解析页，会如实拒绝） |
| `s` | 老写法：列出源菜单挑编号 |

紧凑写法 `m嫁金钗` / `s4`（不带空格）同样认——中文输入法下不打空格是很自然的动作。
三条硬约束值得留意，它们都是踩过坑之后定下来的：

* **参数不参与 `lower()`**：整行小写会把 `m Osiris 之泪` 的片名削成小写，而片名正是
  站内搜索的关键词，字面变了召回就跟着变。
* **整词命令优先于紧凑拆分**：`save` 必须先按整词命中，否则会被拆成 `s` + `ave`，
  切到一个不存在的解析源上（判据是 `_KNOWN_CMDS`）。
* **认不出参数就拒绝执行**：把「哔哩哔哩」当片名硬搜、把一个歧义源名硬切过去，
  都比原地不动更糟，所以一律报错并列出可用值（`_split_site_arg` / `_resolve_source_arg`）。

**`site` 是优先站，不是硬限定**：写 `site 爱奇艺` 只是把爱奇艺提到站内搜索顺序的第一位，
那家没版权/没收录时**照样兜底到其它三家** —— 此时会明确播报
`⚠ 爱奇艺 没搜到「庆余年」，改用腾讯视频（指定站无版权/未收录时会退回其它三家）`，
而不是让你从成功行的站名里猜（实测 `find_page_url(庆余年, prefer=爱奇艺)` 正是落到腾讯）。
它当次就生效（`state["site"]` → `find_page_url(prefer=…)`），并且会一直沿用：之后 `s` 换源、
再 `m` 换片都带着这个站，直到你改写它。无损云会话下写 `site X` 会被忽略并如实告知
（它按片名直连、不分官方站），真要按站点找页得先 `s 4` 换到第三方源。
菜单与一行式共用同一条落地路径（`_apply_source`），免得两种写法的边界条件不同步。

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

| 编号 | 源 | 类别 | 工作方式 | 只给 `--title` 能用吗 | 能下载吗（`d` / `--save`） |
|---|---|---|---|---|---|
| 0 | 无损云解析 | **资源型** | 按**片名**搜片库（官网 suggest → API 取 m3u8） | ✓ 直接搜库 | ✓ 原生直链 |
| 1 | LX解析 | 采集型 | 解析**一个具体视频页**（`源前缀 + 页面URL`） | ✓（先自动找官方页） | ✓ |
| 2 | 789解析 | 采集型 | 同上 | ✓ | ✓ |
| 3 | 冰豆解析 | 采集型 | 同上 | ✓ | ✓（需本机 `node`） |
| 4 | 麒麟解析 | 采集型 | 同上 | ✓ | ✓ |
| 5 | 云解析 | 采集型 | 同上 | ✓ | ✗ 未接入 |
| 6 | CK播放器解析 | 采集型 | 同上 | ✓ | ✓ |
| 7 | M3U8解析 | 采集型 | 同上 | ✓ | ✓ |
| 8 | PlayM3U8解析 | 采集型 | 同上 | ✓ | ✓ |
| 9 | 暴风资源 | **资源型** | 按**片名**搜片库（`ac=detail&wd=` 库内搜索，无官网） | ✓ 直接搜库 | ✓ 原生直链 |
| 10 | 红牛资源 | **资源型** | 同上 | ✓ 直接搜库 | ✓ 原生直链 |

**「类别」不是注释，是代码里的判据**：`config.ParseSource.kind` 只有 `RESOURCE`（资源型）
与 `COLLECT`（采集型）两个取值，**每条源都必须标注**。CLI 的每一个分流 —— `--title` 要不要
先「片名 → 官方页」、`p编号` 能不能弹窗、`--save` 走片库直连还是解析页抽流、换源/换片失败
回退到哪条通路 —— 都只问 `cli._source_needs_page_url(index)`，而它只读 `kind`。判据收在一处，
就不会出现「A 分支当它是资源型、B 分支当它是采集型」的漂移；资源型没有解析页，所以
`p编号` 弹窗会被**直接拒绝并告诉你改写 `s 编号`**，不会被悄悄换成内嵌播放。

> 第三方源会随时间失效或更换域名。若某个源起播后白屏或报错，用 `s` 切到下一个即可；
> `--list` 始终反映 `config.py` 里的当前列表。默认源按**名称**定位（`DEFAULT_SOURCE_NAME
> = "LX解析"`）而非硬编码下标，列表增删导致编号漂移时仍指向它。

「能下载」= 该源已在 `jx_resolve._HOST_RESOLVERS` 登记，能把解析页抽成真·媒体直链
（详见[第三方源抽流](#第三方源抽流srcjx_resolvypy)）。未登记的源，无论按 `d` 还是
`--save -s 该编号`，都**直接报错并列出可用编号**，绝不静默换成无损云、也不臆造地址。
`--save` 不给 `-s` 时默认仍是无损云直连（与播放路径的默认源 LX解析 刻意不同，见
[保存为 mp4](#保存为-mp4--save--d)）。

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

指定的站**没命中**时不会硬停、也不会悄悄换家，而是先把落差讲出来（2026-10 实测输出，
那次爱奇艺站内当天没返回「庆余年」的结果）：

```
$ python -m video_vip --title 庆余年 -s 2 --site 爱奇艺
  正在 爱奇艺 站内搜索「庆余年」...
  正在 腾讯视频 站内搜索「庆余年」...
  ⚠ 爱奇艺 没搜到「庆余年」，改用腾讯视频（指定站无版权/未收录时会退回其它三家）
  ✓ 按片名找到腾讯视频视频页：
      https://v.qq.com/x/cover/rjae621myqca41h/i0032qxbi2v.html
```

### 三条资源型源不等价：入库时刻与收录范围

同一条剧集，各家片库**入库的钟点不一样**（`vod_time`）。今天更新的那一集能不能立刻看到，
取决于你连的是哪一家（2026-10 实测，同一部当日更到第 12 集的剧）：

| 源 | 更到第 12 集的时刻 | 按片名搜索的通路 |
|---|---|---|
| 暴风资源 | 12:07 | `?ac=detail&wd=`（官网 suggest 404） |
| 红牛资源 | 12:20 | `?ac=detail&wd=`（同上） |
| 无损云解析 | 14:13 | 官网 `suggest?mid=1&wd=`（库内搜索回「暂不支持搜索」） |

无损云与最大/牛牛/闪电/丫丫共用同一条片路径（同链路下游），换它们不会变快，所以补的是
更上游的暴风/红牛。两家**键名是 `vod_id`/`vod_name`**、搜索结果自带 `vod_play_url`，
`ac=detail&ids=` 也可用，所以详情/选集/播放整套流程原样复用，只是 `search_list` 里多认
一种字段形状。

收录还**互补**：同一批关键词里《谜林》只有暴风命中（红牛 0 条），红牛返回的 20 条《赘婿》
全是同名衍生短剧、暴风里才有精确同名。所以 `rank_candidates` 的打分不能省，也不能拿
「第一家返回结果」当答案。两家的 m3u8 只带 `User-Agent`（无 Referer）即可下分片；
`src/prefetch.py` 的默认头里写死了 `Referer: https://wsyzy.cc/`，实测这两家的 CDN 不理它
（带与不带，playlist 与分片都 200、字节数一致），所以 `d` / `--save` 通路一行没改。

同一条《赘婿》三家给回来的**选集形状也不一样**（2026-10 实测，`--title 赘婿 -s 0/9/10`）：

| 源 | 选集栏 | 当前集地址 |
|---|---|---|
| 0 无损云 | 1 条「全集」 | `v13.wsyzym3u8.com/.../video/index.m3u8` |
| 9 暴风 | 36 条「第01集…第36集」 | `p.bvvvvvvvvv1f.com/video/zhuixu/第01集/index.m3u8` |
| 10 红牛 | 1 条「全集完结」 | `hn.bfvvs.com/play/epYk3m1a/index.m3u8` |

所以换源时 `n` / `p` / 数字换集的能力会跟着变（打包成「全集」的那两家没有逐集可切），
这不是 bug，是片库本身给的结构。`--episode` 越界时按 `min(N-1, 集数-1)` 落到列表里真实
存在的那一条，并把「共 N 集，已定位到第 M 集」讲出来，不会假装播的是你要的那一集。

> 补一句判别方法：`suggest` 404 且 `ac=detail&wd=` 能搜的站，背后都是一座 maccms 片库，
> 所以它跟无损云一样受**入库延迟**约束（别指望它比官方更快）。真·实时解析的 jx 站是另一回事
> —— 它抽出来的 m3u8 域名会落在官方 CDN 上、带短时效签名。想分清就把当日新集的官方页丢进解析
> 接口，再看抽出的地址归属与首帧时长（`ffprobe` 一比就知道是不是样片）。

---

## 片名 → 官方页 URL（`src/site_search.py`）

站内搜索走**各家原生搜索接口**，不经第三方搜索引擎。旧版这里写的是「实测原生搜索在本
机环境都不可用」（腾讯 `vd.l.qq.com/proxyhttp` 要未公开 buid、爱奇艺 suggest 连不上、
优酷 `so.youku.com` 是 JS 壳），2026-10 复测该结论已过时：

* 爱奇艺原生 JSON 接口 `search.video.iqiyi.com/o` 可直接通；
* 优酷原生 `search.youku.com/api/search` 直接返回完整播放页 URL；
* 腾讯真正的搜索接口是 `pbaccess...MultiTerminalSearch/MbSearch`（POST，**无需 buid**，
  从 `v.qq.com/x/search` 的 JS bundle 逆向得到），当年试的 `proxyhttp` 才是要 buid 的那个。

原生比 Bing 站内搜索更准：直接给官方页 URL 与整季元数据，不再有 `site:youku.com` 整体
改回 `play.tudou.com` 镜像域、结果混入别家站或过期页的问题。**Bing 兜底已整条删除**
（含 `_bing_search` / `_extract_urls` / `_decode_bing_href` / `_BING_FALLBACK_SITES`），
芒果TV 改走下面的片库目录，四站一律只走自家接口。

芒果TV 走的是**片库目录全量索引**，不是它的搜索接口：

* `so.mgtv.com/so` 是纯 CSR（HTML 里剧名出现 0 次、`/b/` 链接 0 条），拿不到东西；
* `mobileso.bz.mgtv.com/pc/search/v1` 只有 `src=intelmgtv` 免签，而它是**视频粒度的模糊
  推荐源**：14 部剧实测只有 3 部给出可播正片，《难哄》给快乐大本营、《承欢记》给辟谣
  新闻，正片 cid 常常根本不在结果里（《与凤行》`cid=626407` 搜索从不返回）；
* 改用专辑粒度的权威目录 `pianku.api.mgtv.com/rider/list/pcweb/v3`（电视剧/综艺/电影
  三频道，约 6100 条 / 170 页），首次使用时 8 并发翻页建进程内索引（约 3~5s），
  此后片名→URL 是纯本地查表，命中即权威 `clipId + playPartId`。

各家原生的**过滤要点**（都是实测踩出来的，缺一条就会给出坏链或错剧）：

| 站点 | 命中页 | 原生接口必须处理的坑 |
|---|---|---|
| 腾讯视频 | `v.qq.com/x/cover/<cid>/<vid>.html` | 正片在 `areaBoxList`（MainNeed 主需求卡）而非 `normalList`；只认 `doc.dataType==2` + 站内 `enName=='qq'` 源 + **`videoInfo.year>0`**（PGC 自频道 year 恒为 0，实测《凡人修仙传》返回 127 集粉丝搬运号、《声生不息》返回奥运歌曲合集）；入口必须带 vid，光 `cid` 的 `/x/cover/<cid>.html` 第三方解析返回 None |
| 爱奇艺 | `www.iqiyi.com/v_<vid>.html` | 该接口是**聚合搜索**，外站条目（`siteId=qq/youku`）的 `albumLink` 是 `so.iqiyi.com/links/…` 短链，跟随重定向跳回首页 —— 只认 `siteId=='iqiyi'`；`videoinfos[]` 只给前 20 集且 `itemLink` 同样是死短链，整季枚举仍走专辑页 HTML |
| 优酷 | `v.youku.com/v_show/id_<vid>.html` | `leftButtonDTO.action.value` 直接就是播放页，但 `isYouku=1 且 hasYouku=0`（别家分销、优酷无片源）时它给的是**别家站点 URL**（实测搜「狂飙」给 `www.iqiyi.com/v_…`）—— 必须按域名过滤，并比对 `titleDTO.displayName` |
| 芒果TV | `www.mgtv.com/b/<cid>/<vid>.html` | 片库目录：分页参数是 **`pn`**（前端自己发的 `pagedNo` 是假参数，传 2..40 拿到同一批 36 条）；`pageSize` 被服务端**硬锁 36**，调大无效，只能逐页翻；目录**不认片名**（keyword/q/name/title 等 12 个参数名全部被忽略），只能整库枚举建索引；`hitDocs[].playPartId` 就是可播 vid，`clipId` 是专辑 id —— 缺一个都拼不出可播页 |

其余三家共用 `_rank_by_title` 做标题分级：归一化（去 `<em>`/`<B>` 高亮、空白、书名号/间隔号）
后**完全相等 → 包含 → 二创/衍生垫底**，都不沾边的一律剔除。这一步是防错配的关键——实测
优酷搜「以家人之名」时站内唯一可播的是**另一部剧**《以子之名》(`isYouku=1 hasYouku=1`)，
只按域名过滤会把它当结果交出去；《庆余年第二季》站内也只有《〈庆余年第二季〉深度解读》。
芒果不走这套模糊分级：候选来自全库目录，只认**归一化全等**外加「目录名以查询词起头」
（声生不息 → 声生不息·港乐季），刻意不用反向包含与双向子串——否则「庆余年第二季」会匹上
第一季、「狂飙」会匹上短视频专辑《…金币狂飙》。代价是它必须全库枚举；收益是**查不到即
可信地判定芒果无此片**（实测《狂飙》《庆余年第二季》在芒果就是无版权），不必再靠外部引擎猜。

覆盖**四家**官方主站，`--site` 可指定优先站、不指定则依次尝试：

| 站点 | 命中页 | 集数处理 |
|---|---|---|
| 腾讯视频 | `v.qq.com/x/cover/<cid>/<vid>.html` | 纯 requests 打通 `pbaccess.getPage`（逆向 JS bundle 得到精确请求体：snake_case `page_params` + `page_bypass_params` + `vversion_name=8.5.96`），从返回 CardList 的 `pc_web_episode_list` 卡按集号取 vid；超 30 集走 tabs `page_context` 翻页 |
| 爱奇艺 | `www.iqiyi.com/v_<vid>.html` | 桌面页被反爬挡，改抓**专辑页** `/a_<aid>.html`，其中静态内嵌整季 `/v_` 列表，按集号精确取 |
| 优酷 | `v.youku.com/v_show/id_<vid>.html` | 桌面页被阿里 **x5sec 反爬盾**（返 1.5KB 空壳）；借道 `m.youku.com/video/id_<vid>.html` 不挡，从 `window.__INITIAL_DATA__` 内嵌 JSON 按 `stage` 取 vid 再拼回桌面 URL |
| 芒果TV | `www.mgtv.com/b/<cid>/<vid>.html` | 播放页是 Nuxt 空壳，选集靠官方分集接口 `pcweb.api.mgtv.com/episode/list` 异步拉；只认 `isIntact=="1"` 的完整正片（同集常并列一条预览占位，预览页解析源解不动） |

`SiteHit.episode_pinned` 如实标出是否**真的**定位到了那一集：枚举命中 → `True`；
枚举失败（网络异常/超总集数）退回整季 cover 页 → `False`。**绝不因集数搜不到就返回 None。**

> 已知缺口：芒果**综艺**的分集接口用**日期**而非「第N集」标注（实测 `t4=2022-04-17`、
> `t1=期号`），`_mg_eps_from_data` 按「第N集」筛会整季为空 —— 页面本身可播（实测
> 《声生不息》`isIntact=1` 正片），只是「共 N 集」列表当前列不出来。电视剧/电影不受影响。

---

## 播放后端

「播放」最终要落到一个能渲染 m3u8 的执行者上（无损云的播放页是
`https://wsyzy.vip/m3u8/?url=<m3u8>`）。于是抽象成几个后端
（`--player` 指定，**默认 `embed`**；`--player auto` 才按 mpv → ffplay → embed → browser
顺序探测）：

| 后端 | 说明 | 能否回传播放事件 |
|---|---|---|
| `embed` | 本地 HTTP 服务 + 自有播放页（hls.js 直接播 m3u8），**默认** | ✓（回退 / 选集等回传终端） |
| `prefetch` | 并发预取分片到本地，用本地播放器播（见下） | ✗ |
| `mpv` | 调本机 `mpv` | ✗ |
| `ffplay` | 调本机 `ffplay` | ✗ |
| `browser` | 用系统浏览器打开播放页 | ✗ |

### `embed` 后端为什么要自建服务

把第三方播放页塞进 iframe 再用 `webbrowser.open` 打开 `file://` 页面时，Python 完全
看不到播放状态：iframe 跨域，读不到里面的 `<video>`，也收不到 `ended`。实测无损云的
m3u8 与分片都返回 `Access-Control-Allow-Origin: *` 且不加密封装，于是换成
**我们自己的页面 + hls.js 直接播 m3u8**——`<video>` 在自己手里，`ended` 就是真事件。

页面要把事件送回 Python，就需要一个本地 HTTP 服务（`file://` 页面无法可靠回传）。
`embedsrv.py` 既当静态服务器（页面 + hls.js），又当回调收集器（`/event`）。

**一个标签页手动换完整部剧**：换集（终端 `n`/`p`、数字跳集、页面选集栏）只是改服务端
下发的 state（带一个新 token），页面自己切源；标签页只在「还没有活着的页面」时才开
一次（实测换 3 集只开 1 个标签）。页面拿 state 用 **`/wait` 长轮询**而不是 `setInterval`——
Chrome 会节流后台标签页的定时器（intensive throttling 最狠 1 分钟一次），定时轮询会导致
「点了选集半天不换源」；长轮询是一次挂起的 HTTP 请求，不受节流影响（实测 token 变化后
0.6s 内页面就切了源）。

**四种 state 模式**：`hls`（裸 m3u8，能收到页面回传的播放事件）、`direct`（裸 mp4 等浏览器原生能播的，
能收到播放事件；hls.js 播不了 mp4，必须分开处理）、`iframe`（第三方 HTML 解析页，只能塞
iframe，收不到任何事件）、`halt`（Python 侧退出，让页面停播并显示说明）。

**页面内回退**：hls.js 播不了某个地址（CORS 被拦 / 清单 404 / 加密方式不支持）时，
页面自己换成 iframe 播并回传 `fallback` 事件。**画面优先**：
用户至少能看，Python 侧则据 `fallback` 事件提示用户「这一集已回退到第三方
播放页」，而不是静默失效。分片级的网络错误走「startLoad 重试」，不回退。

---

## 保存为 mp4（`--save` / `d`）

`src/download.py` 复用 `src/prefetch.py` 的能力，分两步：

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

**取流有两条路**（`--save` 与交互循环里的 `d` 用的是同一套）：

* **无损云直连**（`--save` 不给 `-s` 时的默认）：每集的 url 本身就是 m3u8 直链，直接下。
* **第三方源抽流**（显式 `-s` 某个第三方源）：源给的是 `source.url + 官方页` 的**解析页**
  （HTML、不是流），先由 `jx_resolve` 按「host → 抽流接口」映射抽出明文 m3u8（连带
  它要求的 UA/Referer），再接到**同一条**下载通路上。`--save --title …` 会先站内搜索
  定位官方页；`--save <URL>` 直接解析那一页，粘的是整季入口页或带了 `--episode` 时
  先枚举整季定到单集页。

没给 `-s` 时 `--save` 仍默认无损云，**刻意不跟播放路径共用默认源**（播放默认是 LX解析）：
直连不依赖第三方接口是否存活、还能 `--variant` 选清晰度，对「下载」这件事更稳。

> 未接入下载接口的源（目前只有「云解析」，`jx_resolve._HOST_RESOLVERS` 里没登记）会
> **直接报错并列出可用编号**，绝不静默换成无损云、也不臆造地址。第三方接口今天能不能抽到
> 流有波动（实测同一部剧不同日子只有部分源出链），失败时会提示重试或改 `-s 0`。

---

## HLS 并发预取（`src/prefetch.py`）

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

## 第三方源抽流（`src/jx_resolve.py`）

第三方源每条每集链接是 `source.url + 官方页URL`，那本身是一个 **HTML 播放页**（不是流），
直接喂给 `src/download.py` 的 `parse_hls` 会失败。真正出流的那步发生在浏览器 JS 里——
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
故随包带一段 Node 脚本 `src/bdjs/bd_run.js` 复现整条链：

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

`src/bdjs/` 目录自带 `go.wasm` + `wasm_exec.js`（离线可用），`global.min.js` 与
`khd_play.js` 首次运行时抓取并缓存到同目录。**需要本机装有 `node`**；没有 node 时
`_resolve_bd` 返回 `None`，调用方如实报错换源。

---

## 目录结构

源码全部收在 `src/` 下，包根只留与源码无关的东西：

```
video_vip/
├── README.md          本文档
├── __init__.py        包入口：从 src/ 再导出公开 API（from video_vip import X 照旧可用）
├── __main__.py        `python -m video_vip` 的薄壳，调 src/cli.py:main()
├── .gitignore
├── tests/             回归用例（仍在包根，不进 src）
└── src/               全部源码
    ├── config.py … cli.py
    └── bdjs/          冰豆取流链的 Node 脚本 + WASM（随包，src/jx_resolve.py 按 __file__ 定位）
```

子模块的导入路径多一层：`from video_vip.src.title import …`（或包内相对导入 `from .title import …`
不变）。顶层再导出的公开 API（`Config` / `Consumer` / `WsyzyDirect` / `start` 等）名字与以前一致。

---

## 模块一览

| 模块 | 职责 |
|---|---|
| `src/config.py` | 解析源列表（`VIDEO_PARSE_LIST`，每条源标 `kind` 资源型/采集型）/ 站点容器（`PLAYER_CONTAINERS`）/ `@include` 规则 |
| `src/gm.py` | 持久化存储 / 打开链接 / 样式收集 |
| `src/net.py` | HTTP 请求层（`req()`） |
| `src/page.py` | `window` / `document` / `location` 的最小 DOM 兼容层 |
| `src/util.py` | DOM 工具（`find_target_ele` / `remove_video` / `cleanup`）+ 页面会话 `PageSession` |
| `src/title.py` | 片名识别与清洗（`wsyzy_clean_title` / `read_video_title` / `wait_stable_title`） |
| `src/wsyzy.py` | 资源型站点直连（无损云 / 暴风 / 红牛；搜索 / 排序 / 详情 / 选集 / 播放 / 中止） |
| `src/site_search.py` | 片名/集数 → 官方视频页 URL |
| `src/player.py` | 播放页布局与样式构建 + 播放后端 |
| `src/embedsrv.py` | embed 后端的本地 HTTP 服务与自有播放页 |
| `src/ui.py` | 终端 UI（toast / 菜单 / 选集栏 / 进度条） |
| `src/consumer.py` | 解析源菜单与播放调度（`Consumer` / `start`） |
| `src/prefetch.py` | HLS 并发预取引擎 |
| `src/download.py` | 保存为 mp4 |
| `src/jx_resolve.py` | 第三方解析页 → 真·媒体直链 |
| `src/bdjs/bd_run.js` | 冰豆取流链的 Node 复现（含两段 WASM） |
| `src/cli.py` | 命令行入口（`__main__.py` 在包根，只调它） |
| `tests/` | 回归用例（7 个套件，见下「测试」；在包根，不进 `src/`） |

---

## 测试

用例是**脚本式**的：模块顶层直接跑断言、末尾汇总 `PASS/FAIL`，两种跑法等价：

```
python tests/test_iqy_eps.py                 # 退出码即结果
pytest tests -q                              # 走各文件的 test_no_failures()
pytest tests/test_iqy_eps.py tests/test_yk_eps.py tests/test_cli_flow.py -q   # 只跑离线套件
```

| 套件 | 断言 | 联网 | 覆盖 |
|---|---|---|---|
| `test_iqy_eps.py` | 25 | 否（stub） | 爱奇艺 avlistinfo 优先 / 按 total 翻页 / contentType 过滤 / `/v_` 反推 albumId / 错 aid 退回单集 / HTML 兜底不退化 |
| `test_yk_eps.py` | 23 | 否（stub） | 优酷只按「是否正片」过滤、**不按 stage 去重**、集号顺序重编、换集与枚举同口径 |
| `test_sequel_rank.py` | 24 | 否 | 续集编号降级 + 候选排序（匹配度压过年份） |
| `test_rank_title.py` | 14 | 否 | 标题归一化 / 二创识别 / 分级排序剔除 |
| `test_cli_flow.py` | 93 | 否（stub） | `--save` 三条取流分派（第三方抽流 / 未接入源报错 / 不给 `-s` 仍走无损云）+ `d` 指令抽流、URL 优先 `--title` 忽略、`m` 换片名可指定官方站、`s` 换源保持当前集数、一行式命令（`m 片名 site 站` / `s 编号·源名·p弹窗` / 紧凑写法 / `save` 不被拆 / 参数大小写保护 / 认不出就拒绝）、`site` 兜底到别家时必须提示、输出配色与分块（颜色按首符号、起播+选集网格同块不空行、bg/独立面板留白、命令回显即分隔线）、`s` 菜单 `▶` 标当前在用的源（弹窗另标「弹窗中」且不改变会话源）、资源型/采集型 `kind` 自洽与分流（字段与类别对得上、站点参数注入、两条搜索通路都归一成 `id/name`、采集型接 `--title` 报错、资源型弹窗被拒） |
| `test_native_search.py` | 69 | **是** | 四站原生搜索契约、`find_page_url` 端到端 |
| `test_native_accuracy.py` | 33 | **是** | 片名→官方页命中率实测 |

离线 5 套件合计 179 项，秒级跑完；联网 2 套件约 2 分钟，站点改版时会红——那是它该有的反应。

`tests/conftest.py` 负责把仓库根加进 `sys.path`（`AI_coding` 是命名空间包，
`from AI_coding.video_vip.src import ...` 需要仓库根在路径上）。各文件里另留了一行
`sys.path.insert(0, str(Path(__file__).resolve().parents[3]))`：直接跑脚本时不加载
conftest，那句是那种跑法唯一的引导来源，也因此不再写死盘符。

stub 类套件（`test_iqy_eps` / `test_yk_eps`）会临时替换 `net.get_session`，跑完必须
`_restore_net()` 还原——pytest 把 7 个套件导进同一个进程，不还原的话后面的联网用例
会拿到 `FakeSession`（这个坑实测踩过，症状是 `test_native_search` 收集期 AttributeError）。
`test_cli_flow` 同理：它换掉的是 `cli` 与 `jx_resolve` 上的真函数，脚本末尾统一
`restore()` 还原，免得污染后面的套件。

---

## 核心流程

```
接管播放器容器 takeover()
  → 等片名稳定 waitStableTitle()      精确选择器/og:title 命中即返回；
                                      否则等 1.5s 稳定且清洗通过，或 5s 兜底
  → 按片名搜库 searchWithRetry()      有官网走 suggest、没官网走 `ac=detail&wd=`（各家二选一）
                                      首搜失败则用 wsyzyCleanTitle 清洗后重搜
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

### 终端折行：`toast` 用 `soft_wrap`

`rich` 默认会按终端宽度硬折行，中英混排 + emoji 时折行位置很难看。所有 `toast`
输出走 `soft_wrap=True`，由文案自己控制换行（`\n`），不依赖终端宽度。

### 输出配色与分块：颜色看符号，分块看来源

一条命令跑下来，终端里同时躺着五种东西：搜索进度、命中结果、命令速查表、选集网格、
后台线程插播（浏览器打开回执 / 页面回退提醒 / 预取里程碑）。全挤在一起就分不出哪行属于
哪件事，所以 `ui.py` 定了两条约定，**调用点一律不传样式**：

| 维度 | 依据 | 取值 |
|---|---|---|
| 颜色 | 文案**首符号**（`_style_of`） | `✓` 绿 · `✗` 红 · `⚠` 黄 · `ℹ`/`↻`/`⬇` 与进度行蓝灰 |
| 分块 | 消息**来源**（`toast(..., block=)`） | `flow` 主链路 · `bg` 后台线程插播 · `panel`/`sec:标题` 面板 |

分块刻意**不按颜色断**：同一条工作流（搜索 → 命中 → 解析 → 起播 → 选集网格）里 ✓
和进度行交替出现，按类别断块会把一件事切成五六行。所以判据是「这几行是不是在说同
一件事」：

* `flow` 内部连续打印**不空行**，起播状态和紧随其后的选集网格也同属这一块
  （`render_list(..., block="flow")` → 只给 `section()` 传 `block` 就贴在当前块里，
  不另开一块）；
* 只有**换来源**才空一行：后台线程插播 `bg`（浏览器打开回执 / 页面回退提醒 / 预取与下载
  进度）、独立面板 `section()` / `start_block()`、banner `boot`；
* 面板用 `─` 细横线框住，标题本身就是块名 —— 换标题必然留白；
* **用户敲下的那行命令回显自带一条分隔线**，所以 `_input()` 读完一行就把块标记清零，
  不在 `video_vip> 20` 下面再补一个空行。

```
  ██╗   ██╗██╗██████╗                 ← banner（boot）

  正在 优酷 站内搜索「嫁金钗」...       ← 下面四行同属 flow，紧挨着不空行
  ✓ 按片名找到优酷视频页：             ← ✓ 绿
    https://v.youku.com/…
  ✓ LX解析：浏览器（内嵌播放页…）

  命令速查                             ← 换来源 → 空一行 + 细横线框
  ────────────────────────────
  …
  ────────────────────────────

  ✓ 播放页已在浏览器打开：…            ← 后台插播（bg）→ 再空一行

video_vip> 20                          ← 命令回显本身就是分隔线
  ✓ 正在播放 第20集                    ← 起播 + 选集网格是一组内容，不空行
  《嫁金钗》选集（共 20 集，敲数字或 n/p 换集）:
  ────────────────────────────
     1   2   3 …  19 [20]
  ────────────────────────────
```

`--quiet` / 测试用的 `SilentUi`、以及用例里的 `ScriptUi` 都保留了 `block` 关键字参数
（收下即丢），所以给后台线程加块标记不会把静默模式跑崩。

---

## 已验证

* **纯函数单测**：`wsyzy_clean_title` / `is_clean_title` / `parse_eps` /
  `rank_candidates` / `cur_ep_num` / `parse_pixel_value` / `build_player_frame_layout`
  在同一批用例上逐条比对 —— **198 项全部一致**。
* **真实链路**：bilibili / 腾讯视频 / 爱奇艺页面抓取 → 片名识别 → 无损云搜索 →
  详情 → 取 m3u8 → ffplay 实际解码播放（`ffmpeg -i <m3u8> -f null -` 验证流可用）。
* **单元/集成**：GM 存储与索引迁移、菜单分组、弹窗播放、自动解析、`_running`
  守卫、`abort` 中止、广告声抑制器幂等、多候选面板排序、交互换集 —— 合计 332 项通过。
* **播放页与手动换集（embed）**：全部用**真实 ffmpeg 造的 HLS + 真实 playwright chromium +
  真实 `EmbedBackend`/`EmbedServer`/`cli.interactive()`** 跑，不打桩。
  验证：手动换 3 集只开 1 个标签页、页面标题随集号变成 `片名 · 集名`、
  选集栏高亮跟随、移动端「☰ 选集」按钮可展开/收起而桌面端没有、真实 `interactive()`
  里退出时收到 `halted` 回执（页面真的停播）。
  另验证：长轮询对 token 变化的响应延迟 0.6s（远小于 20s 超时，即换集不被后台标签页
  节流拖住）、`pagehide` 信标能让页面被判为已关闭且不被仍在跑的长轮询抹掉、
  浏览器打不开时明确告警并给出 `--player mpv/ffplay` 替代方案、不调 `stop()` 也能
  0.04s 干净退出（长轮询 handler 是 daemon，不会拖住解释器）。
* **并发安全**：`play()` 会被两个线程同时调用（CLI 主循环、页面选集轮询线程），
  所以「起本地服务」这一段必须串行化。实测 8 线程并发
  `play()` 只产生 1 个服务；`play()` 与 `stop()` 交错 4s 后没有孤儿服务。
  这一条做了**反向验证** —— 用子类复现修复前不持锁的逻辑，同样场景下它产生了
  **8 个服务**，且 `stop()` 只关掉 1 个、留下 **7 个永远关不掉的孤儿**（各占一个端口），
  证明竞态真实存在、测试确实测得出来、修复是必要的。
* **真实片源**：从无损云 API 搜到真实 m3u8（庆余年，时长 7265s），在 chromium 里
  6.6s 内起播成功（`currentTime` 持续推进），
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
  *（2026-10 补充：该天花板已随改用原生接口打破——凡人修仙传现由**优酷原生**命中并枚举整季
  30 集；狂飙/水龙吟由**爱奇艺原生**命中。上面的 Bing 实测记录保留作历史。）*

* **改走原生搜索（`_site_urls` → `_NATIVE_SEARCHERS`）**：爱奇艺 / 优酷 / 腾讯 / 芒果 四站
  片名→官方页全部改由各家原生 JSON 接口完成，Bing 仅留作芒果兜底。端到端实测
  （`tests/test_native_search.py` + `tests/test_native_accuracy.py`，117 项断言全绿）：
  狂飙→爱奇艺 39 集连续、凡人修仙传→优酷 30 集连续、庆余年第二季 ep3→腾讯精确单集页且
  整季 36 集、以家人之名 ep2→芒果 `pinned=True`、声生不息→腾讯《声生不息·港乐季》。
  过程中修掉两个**只按域名过滤挡不住**的错配：优酷把另一部剧《以子之名》当站内结果给出
  （`isYouku=1 hasYouku=1`）、腾讯 PGC 自频道给出 year=0 的粉丝搬运号（《凡人修仙传》127 集
  解说号）——分别由 `_rank_by_title` 标题分级与 `year>0` 判据解决。
  另修既有健壮性 bug：`_tencent_episode_url` 对电影/单片类裸 `json.loads(params.tabs)`
  抛 `JSONDecodeError` 打断整条查找链，收敛到 `_tx_tabs()` 统一吞异常。
  *（2026-10 再补充：其中「Bing 仅留作芒果兜底」已被下一条推翻并删除。）*

* **芒果改走片库目录 + 删除 Bing 兜底**：推翻「芒果做不到精准匹配」的旧结论——错的不是判据，
  是数据源。从零实测（14 轮探针）确认：`so.mgtv.com/so` 是纯 CSR（HTML 里剧名 0 次、`/b/`
  链接 0 条）；`mobileso/pc/search/v1` 免签的 `src=intelmgtv` 是**视频粒度模糊推荐源**，
  严格判据下 14 部只有 3 部给出可播正片，正片 cid 常常根本不在结果里（《与凤行》626407）；
  而 `pianku.api.mgtv.com/rider/list/pcweb/v3` 是**专辑粒度权威目录**，分页参数是 `pn`
  （前端自己发的 `pagedNo` 是假参数，据此曾误判「片库不可枚举」）、`pageSize` 硬锁 36、
  `hitDocs[].playPartId` 即可播 vid（漏了它曾误判「cid 补不出可播页」）。故 `_mg_native_urls`
  改为按 `pn` 枚举三频道（约 6100 条 / 170 页 / 8 并发 3~5s，进程内建一次）建
  `{归一化片名: (cid, vid)}` 索引做全等匹配；`_BING_FALLBACK_SITES` 与
  `_bing_search`/`_extract_urls`/`_decode_bing_href`/`_site_domain`、只喂 Bing 用的
  `_SITE_PATTERNS` 正则整条删除，`_site_urls(query, site)` 签名去掉 `pattern` 形参。
  端到端复测（`tests/test_native_search.py` 69 项 + `tests/test_native_accuracy.py` 33 项全绿）：
  与凤行→`/b/626407/20772683.html`、以家人之名→333900（旧搜索给的是衍生篇 343554）、
  玉簟秋→802659、我的人间烟火→509691 全部命中且播放页反查剧名一致；以家人之名 ep2 仍
  `pinned=True`、整季 25 集 1..25 连续；乱查询四家全空 → `find_page_url` 优雅返回 None，
  不再由 Bing 捞回花絮页充当结果。
  遗留边界：① 目录只枚举了 `kind=a1`，「查不到 = 无版权」目前只对该档成立；② 综艺的
  `episode/list` 用日期而非「第N集」标注，页面可播但整季列不出来（见上文已知缺口）。

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

实现过程中修掉的真实缺陷：`parseFloat("211px")` 语义（取前缀数字，Python 的
`float()` 会抛异常）、`.5` 的舍入方向（`Math.round` 向上、Python `round` 是银行家舍入）、
页面编码判定（requests 对无 charset 头的 `text/html` 退回 ISO-8859-1，导致爱奇艺
标题变乱码）、`find_target_ele` 误把 `Window` 当 `Document` 查询、B 站偶发限流需重试。
* **第三方源抽流（`jx_resolve`）**：用同一条真实腾讯单集页逐源实测，789 / CK / M3U8 /
  PlayM3U8 / 麒麟 / 冰豆 六源抽出真流（四条 789 家族源回同一条 `xhyyxx.oxapi.cn`
  的 tx1080 m3u8，符合它们共用同一下钻逻辑的设计）；LX 本次返回 None（该源自身
  接口抖动，`_resolve_lx` 未改动）；未登记的云解析 `is_supported()` 返回 False、
  `resolve()` 不发请求直接 None（如实报「不支持」）。

* **冰豆取流链（`src/bdjs/bd_run.js`）**：端到端实测四种形态——
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

* **`--save` 打通第三方源 / URL 优先 / 换源保集数（2026-10）**：四条行为改动。  
  ① `--save` 不再只走无损云：显式 `-s` 第三方源时「视频页 → 解析页 → `jx_resolve` 抽
  明文 m3u8 → 同一条下载通路」，`_run_save` 里 URL 与片名两条入口都支持，`--episode`
  靠 `list_episode_pages` 枚举整季定集；未接入下载接口的源直接报错并列出可用编号。
  真实链路验证：`--save --title 以家人之名 -e 2 -s 4`（麒麟）拿到
  `png.ruyijx.com/m3u8/….m3u8` 并带正确 Referer；`--save <芒果入口页> -e 3 -s 4`
  枚举整季后抽到**另一条**直链（确实是第3集）；不给 `-s` 的无损云路径回归正常
  （46 集、第01集直链）。同一天 LX / 789 对同一条芒果页抽流返回 None —— 属第三方
  接口自身波动，代码如实报「本次抽流失败，可重试或 -s 0」，不静默换源。
  ② 模式二只认 URL：原先「URL + `--title`」会拿 title 走无损云片名搜索，导致 `-s`
  指定的第三方源永远用不上，且页面里那一季被搜索结果替换（同名不同版本会播错片）；
  现在统一落到 `_play_page_url`，`--title` 只在页面识别不出剧名时当显示名兜底
  （`drama` 取值顺序：`fetch_show_name` → 页面标题 → `--title`）。
  ③ 交互 `m` 换片名时会追问官方站（`_pick_site(ui, keep=…)`，回车沿用上次选择），
  答案写进 `state["site"]` 供 `find_page_url(prefer=…)`；无损云会话不问（不分官方站）。
  ④ 交互 `s` 换源**保持在播的那一集**：改用「当前集的官方页」（选集栏那条 url 去掉
  `source.url` 前缀）重解析，并把 `season_url` 一路传进 `interactive`；`relaunch` 增加
  `episode` 参数，换源/弹窗路径都带上当前集号，换片名仍传 0（回第 1 集）。定不到那一集
  时如实播报「现在在第 N 集」，不谎报。四条改动已固化为离线用例
  `tests/test_cli_flow.py`（`ui._input` 脚本喂入 + 网络函数打桩，当时 23 项断言全过），
  真机浏览器会话未逐步回放。

* **一行式交互命令（2026-10）**：`m` / `s` 允许把后续追问折叠进同一行
  （`m 嫁金钗`、`m 嫁金钗 site 爱奇艺`、`s 4`、`s 麒麟`、`s p4`），只敲单个字母时
  老写法（逐项追问 / 源菜单）完全不变；紧凑写法 `m嫁金钗` / `s4` 也认。
  新增 `_SITES_CN`（四家标准写法，`--site` 的 choices、`_pick_site` 的追问、
  `m … site X` 三处共用，简称先展开再比全等）、`_KNOWN_CMDS`（整词命令表，保证
  `save` 不被拆成 `s` + `ave`）与五个纯函数 `_match_site` / `_split_site_arg` /
  `_match_source` / `_resolve_source_arg` / `_apply_source`。命令切分改成
  「首 token 是命令、其余原样是参数」，**参数不再参与 `lower()`**（否则英文片名被
  削小写、站内搜索字面一变召回就变）；非 `m`/`s` 的命令带参数一律拒绝并提示。
  `site X` 在无损云会话下会被忽略并如实告知。菜单与一行式共用 `_apply_source`，
  两条写法不会再各走各的边界条件。ui 的帮助表补了常驻的一行式语法提示。
  已在 `tests/test_cli_flow.py` 第 [5] 段固化 25 项（纯函数 12 + 端到端流程 13，
  套件合计 48 项全过）；真机终端未逐条回放。

* **`site` 的「优先 vs 限定」（2026-10）**：回答「`m 片名 site 爱奇艺` 会生效吗」时
  实测出的语义落差。`find_page_url(prefer=…)` 做的是 `sites.insert(0, prefer)` +
  搜不到 `continue`，即**优先站**而非硬限定；庆余年 `prefer=爱奇艺` 真实返回
  `v.qq.com/x/cover/…`（爱奇艺站内那次没出结果），落到别家却**毫无提示**，只有成功行
  的站名泄露真相 —— 而 `--site` 的 help 当时写的是「限定」。改法：搜索实现改名
  `_scan_page_url`（内部一行没动），`find_page_url` 变薄壳，命中站≠指定站时补一条
  note（CLI 三处调用点都已把 note 接到 `ui.toast`，无需各自改）；`--site` help 与
  README 措辞改成「优先…未命中会退回其它三家并提示」。无损云会话下 `site` 仍按原样
  **忽略并提示**（它按片名直连、不分官方站），不自动改解析源。
  第 [6] 段固化 5 项（prefer 透传 / 落到别家提示 / 命中不多嘴 / 没指定不提 /
  全空不谎报），套件 53 项全过；端到端复跑 `find_page_url(庆余年, prefer=爱奇艺)`
  经真实 `Ui.toast` 通道确认提示确实显示且不折行错乱。
  踩坑一条：拿裸 `print` 当 on_note 探针会在 GBK 控制台炸 `⚠`（`UnicodeEncodeError`），
  走 `ui.toast`（rich + soft_wrap）才与真实链路一致 —— 探针也得用生产通道。

* **终端输出的配色与分块（2026-10）**：真机跑 `--title 嫁金钗` 时，banner、三家搜索进度、
  命中行、解析回执、命令速查、选集网格、浏览器打开回执全挤在一坨，分不出块。约定定为
  **颜色看首符号、分块看来源**：`_style_of` 按 `✓/✗/⚠/ℹ↻⬇` 选色（cli.py 近百处 `ui.toast`
  一处不改就各归各的色），`toast(..., block=)` 只让**换来源**时留白 —— 主链路 `flow` 内部
  连续不空行，后台线程插播（浏览器回执 / 页面回退提醒 / 预取与下载进度）标 `bg`，
  独立面板走 `section()` / `start_block()` 并上下各加一条 `─`。
  中途推翻过两版：① 「按符号类别断块」—— 离线重放真渲染后发现 ✓ 与进度行交替时每条
  之间都插了空行，一条工作流被切成六行，比原来的平铺更难读；② 把选集网格当面板 ——
  真机敲 `20` 换集后出来的是「命令回显 / 空行 / 正在播放 / 空行 / 选集网格」，一组内容
  被劈成三截。于是 `section()` 加了 `block` 参数（传了就贴在当前块里只打标题），
  `render_list` 默认挂 `flow`；`_input()` 读完一行把块标记清零，让命令回显自己当分隔线。
  第 [7] 段固化 15 项（三色分配 / flow 不空行 / 起播+网格同块 / bg 留白 / `section`
  换标题留白 / 面板带细横线 / 首条输出前不空行 / `_input` 后不补空行 / `SilentUi`
  签名兼容），套件 68 项全过；
  假 console 直接接住 `print(文本, 样式)`，所以断言不依赖 rich 装没装。
  另一个坑：拿 `Console(file=StringIO(), force_terminal=True)` 探 ANSI 时色码数恒为 0，
  必须显式给 `color_system="truecolor"` —— rich 对着内存流不做终端色彩探测。

* **`s` 菜单标出当前在用的解析源（2026-10）**：`s` 的用途就是「同一部影片在几家接口之间跳」，
  可菜单里那九行长得分毫不差，敲一次 `s` 得重新回忆刚才用的是哪家。做法：`_source_menu()`
  读 `state["src"]` —— `switch_source` / `relaunch` 每次成功都会更新它、失败时保持原值，所以
  这个基准恒等于「现在真正在播的那条通路」；命中行打 `▶` + 粗绿并注「（当前）」，其余行前缀
  补两个空格，编号列仍与 `▶` 行对齐。`p+编号` 的弹窗播放另记一笔 `state["popup_src"]` 并注
  「（弹窗中）」：弹窗只是浏览器里另开的窗口，**不改**终端会话在用的源，所以两个标记可能落在
  不同的行上。当初想过把弹窗也写进 `state["src"]`，那会让下一次 `m` 换片名悄悄改用弹窗源 ——
  那是行为改变而不是显示改变，不做。第 [8] 段固化 4 项（`▶` 只在当前行 / 其余行不带 / 弹窗后
  注「弹窗中」/ 弹窗不改 `▶` 的位置），套件 72 项全过；这段不碰网络，`FakeConsumer.pick_popup`
  是为此新增的唯一假成员。

* **资源型 / 采集型：`kind` 成为所有源的分流判据（2026-10）**：原先只有「无损云」一个特例 ——
  某条源要不要视频页散落在 `index == 0`、`type` 里有没有 "3"、硬编码的 `-s 0` 等写法里，
  加一家同类站点就得回头把分支一个个改一遍。现在把类别提成 `ParseSource.kind`，**11 条源逐条
  标注**（资源型：无损云 / 暴风 / 红牛；采集型：8 家 jx 解析页），并规定所有分流**只问**
  `cli._source_needs_page_url(index)` 一个函数：`--title` 要不要先「片名 → 官方页」、`p编号`
  能不能弹窗、`--save` 走片库直连还是解析页抽流、换源/换片失败回退到哪条通路，全都从它出发。
  随之清掉的是散在若干处的 `-s 0` / `VIDEO_PARSE_LIST[0]` / `return (0, False)`，改读
  `DEFAULT_RESOURCE_INDEX`（由 `kind` 现算，列表增删时编号不漂移）。站点参数也从 `wsyzy.py`
  的模块常量变成源上的字段：`api`（`provide/vod`）与 `suggest`（官网域名）由 `ParseSource`
  注入，`search_list` 按「有官网走联想、没官网走库内搜索」二选一，并把 `vod_id/vod_name`
  归一成 `id/name`，下游的排序/详情/选集完全不用知道连的是哪家。
  **踩过的坑**：暴风/红牛没有官网，最初写成 `source.suggest or SITE` 兜底 —— 于是「在暴风搜
  片名」会悄悄变成「在无损云搜片名」，不报错、只是源不对。改成 `suggest` 允许为空（给了
  `source` 就照它说的算，只有完全没传 source 才回退无损云）。另一个是探针自己的错：把
  `quote()` 过的片名再交给 `requests` 的 `params`，等于二次编码，站方收到 `%E8%B5%83` 字面串，
  当时误判成「暴风搜不到《赘婿》」。第 [9] 段固化 21 项（`kind` 与 `api`/`url`/`type` 对得上 /
  判据与 `kind` 等价 / 站点参数注入与斜杠归一 / 两条搜索通路的键名与单次编码 / 采集型接
  `--title` 直接报错 / 资源型 `p编号` 被拒），套件 93 项全过，全程假 `net.req_json` 不打站点。
