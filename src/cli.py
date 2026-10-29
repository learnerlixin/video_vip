"""cli.py —— 命令行入口。

    python -m video_vip                        # 不带参数：进程序后交互式选片名/URL/解析源
    python -m video_vip <视频页URL>            # 注入该页 → 选解析源 → 起播（只认 URL）
    python -m video_vip --title 庆余年          # 跳过页面，直接按片名搜索播放
    python -m video_vip --list                 # 列出所有解析源
    python -m video_vip --title 庆余年 -s 1 --site 优酷   # 站内搜索优酷页交给第三方源
    python -m video_vip --title 庆余年 -e 5 --save        # 资源型源直连，只下载不播放
    python -m video_vip --title 庆余年 -e 5 -s 4 --save   # 第三方源抽流下载（jx_resolve）

解析源分两类（config.ParseSource.kind，判据集中在 cli._source_needs_page_url）：
    资源型 resource   自带片库（maccms provide/vod），按**片名**搜索就给整季 m3u8 直链
                     —— 只给 --title 就能起播：无损云解析 / 暴风资源 / 红牛资源
    采集型 collect    现场解析**一个具体视频页**（`源前缀 + 官方页URL`），没有片库
                     —— 必须给 URL；只给 --title 时先站内搜索定位官方页：LX/789/冰豆/…

播放起来之后进入交互循环：
    n / p        下一集 / 上一集
    <数字>       跳到第 N 集
    l            列出全部集数
    m [片名]     重新选择影片（换片名，沿用当前解析源，第三方源时可换官方站）
                 一行式：`m 嫁金钗` 直接换片、`m 片名 site 爱奇艺` 指定优先站；
                 只敲 `m` 才逐项追问片名与站点
    s [编号]     切换解析源（同一影片、同一集数，只换解析接口）
                 一行式：`s 4`／`s 麒麟`（源名可模糊匹配）／`s p4`（弹窗播放）；
                 只敲 `s` 才出源菜单
    r            重新加载当前集（reloadCurrentPlayer）
    a            开/关自动解析
    d            保存当前集为 mp4
    u            打印 m3u8 直链与内嵌播放页地址
    q            退出
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from typing import Any, Dict, List, Optional

from .config import (
    COLLECT,
    DEFAULT_RESOURCE_INDEX,
    DEFAULT_SOURCE_INDEX,
    RESOURCE,
    VIDEO_PARSE_LIST,
    Config,
    url_supported,
)
from .consumer import Consumer, start
from .download import DownloadError, save_as_mp4
from .net import WsyzyError
from .player import CACHE_ROOT, PLAYER, available_backends, detect_backend, is_media_url
from . import jx_resolve
from .ui import Ui
from .title import wait_stable_title
from .site_search import find_page_url, list_episode_pages, fetch_show_name
from .util import PageSession
from .wsyzy import WsyzyDirect

BANNER = r"""
 ██╗   ██╗██╗██████╗    
 ██║   ██║██║██╔══██╗  
 ╚██╗ ██╔╝██║██████╔╝
  ╚████╔╝ ██║██╔═══╝
   ╚═══╝  ╚═╝╚═╝
"""

# 看起来像 URL 的最低要求：有 scheme，或首段里有域名点号（v.qq.com/x/...）
_URLISH_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://|^[^/\s]+\.[^/\s]+")

# 四家官方站的标准写法（与 site_search._SITES 同集合）：`--site` 的 choices、交互里
# 的追问、一行式 `m 片名 site X` 都归一到这几个字面上，`find_page_url(prefer=…)`
# 按全等比较，所以简称（腾讯/芒果）必须先展开成它。
_SITES_CN = ("腾讯视频", "爱奇艺", "优酷", "芒果TV")

# 交互循环里认识的整词命令（数字跳集不在此列）。有两个用途：给未知命令兜提示，
# 以及判断 `m嫁金钗` 这种紧凑写法该不该拆 —— `save` 必须以整词命中，
# 否则会被当成 `s` + `ave` 换到一个不存在的解析源上。
_KNOWN_CMDS = frozenset({
    "q", "quit", "exit", "h", "l", "u", "a", "d", "dl", "save", "r",
    "n", "next", "p", "prev", "m", "s",
})


def looks_like_url(text: str) -> bool:
    """位置参数是否像个 URL（而不是被误当成 URL 的片名）。"""
    return bool(text) and bool(_URLISH_RE.match(text.strip()))


def normalize_argv(argv: List[str], parser: argparse.ArgumentParser) -> List[str]:
    """把 `-title` 这类「单横线 + 长选项名」的笔误改写成 `--title`。

    argparse 对短选项支持粘连取值：`-t` 是 `--title` 的短选项，于是
    `-title 嫁金钗` 会被解析成 `-t itle` + 位置参数 `嫁金钗`——片名被当成 URL，
    实际搜索的关键词变成 `itle`，命中一部毫不相干的片子。

    本 CLI 的短选项全是单字母，所以「`-` + 两个以上字符且正好是某个长选项名」
    只可能是笔误，改写是安全的；改写时打印提示，不做静默魔法。
    """
    long_names = set()
    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--"):
                long_names.add(opt[2:])

    fixed: List[str] = []
    for token in argv:
        if token.startswith("-") and not token.startswith("--"):
            name = token[1:]
            base = name.split("=", 1)[0]        # 兼容 -title=xxx 的粘连写法
            if len(base) > 1 and base in long_names:
                print(
                    f"⚠ 已把 {token!r} 当作 {'--' + name!r}（长选项要用两个横线）",
                    file=sys.stderr,
                )
                fixed.append("--" + name)
                continue
        fixed.append(token)
    return fixed


def validate_url_arg(url: str) -> str:
    """位置参数不像 URL 时返回错误提示，像 URL 则返回空串。"""
    if looks_like_url(url):
        return ""
    return (
        f"✗ {url!r} 不是一个 URL。\n"
        f"    按片名搜索请用：python -m video_vip --title {url}\n"
        f"    解析视频页请用：python -m video_vip https://v.qq.com/x/cover/.../....html\n"
        f"    （--title 是两个横线；写成 -title 会被 argparse 当成 -t itle）"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_vip",
        description="全网VIP视频免费解析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m video_vip https://v.qq.com/x/cover/xxx/yyy.html\n"
            "  python -m video_vip --title 庆余年第二季 --episode 5\n"
            "  python -m video_vip --list\n"
        ),
    )
    parser.add_argument("url", nargs="?", help="视频页 URL")
    parser.add_argument("-t", "--title",
                        help="跳过页面抓取，直接用这个片名搜索（同时给了视频页 URL 时"
                             "以 URL 为准，--title 被忽略）")
    parser.add_argument("-e", "--episode", type=int, default=0, help="指定播放第几集（从 1 开始）")
    parser.add_argument(
        "-s", "--source", type=int, default=None,
        help="指定解析源编号（见 --list），不指定则用默认源（当前 = LX解析）。"
             "第三方源解析的是具体视频页：带视频页 URL 时直接拼解析；"
             "只给 --title 时会先站内搜索腾讯/爱奇艺/优酷/芒果TV定位官方页再喂给它，"
             "搜不到自动回退资源型源（按片名搜片库，当前 = 无损云）。"
             "配合 --save 时用第三方源下载：解析页 → 抽明文 m3u8 → 并发分片封装",
    )
    parser.add_argument(
        "--site",
        choices=list(_SITES_CN),
        default=None,
        help="配合 --title + 第三方源：优先去哪家官方站找视频页 URL。"
             "不指定则依次尝试（腾讯→爱奇艺→优酷→芒果TV）；"
             "指定的站没搜到会自动退回其它三家并提示",
    )
    parser.add_argument(
        "-p",
        "--player",
        choices=["auto", "prefetch", "mpv", "ffplay", "embed", "browser"],
        default="embed",
        help="播放后端：embed=生成内嵌播放页并用浏览器打开（默认，本地服务 + hls.js "
             "直接播 m3u8）；"
             "auto=自动探测本地播放器（mpv → ffplay → embed）；"
             "prefetch=并发预取后本地播放（弱网/卡顿时用）。",
    )
    parser.add_argument(
        "--prefetch-workers",
        type=int,
        default=8,
        help="prefetch 后端的并发下载路数（默认 8；实测 8 路可达 ~4.8Mbps）",
    )
    parser.add_argument(
        "--prefetch-ahead",
        type=int,
        default=8,
        help="prefetch 后端缓存够几片就起播（默认 8，约 16 秒视频）",
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="退出时保留预取的分片缓存（默认删除；缓存目录见 --list）",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="不播放，直接把视频保存为 mp4（并发下载分片 + ffmpeg 封装，需要本机有 ffmpeg）。"
             "默认走资源型源直连（当前 = 无损云）；显式 -s 采集型源时走「解析页抽流」，"
             "未接入下载接口的源会直接报错",
    )
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="保存路径：可以是目录（自动用「片名 集名.mp4」命名）或完整文件名；"
             "默认存到「下载/video_vip」",
    )
    parser.add_argument(
        "--variant",
        choices=["highest", "lowest"],
        default="highest",
        help="保存时选哪条清晰度：highest=最高（默认）；lowest=最省流量",
    )
    parser.add_argument("--no-auto", action="store_true", help="不自动解析，只打印菜单")
    parser.add_argument("--no-interact", action="store_true", help="播完不进交互循环，直接退出")
    parser.add_argument("--list", action="store_true", help="列出解析源与可用播放后端后退出")
    parser.add_argument("--mobile", action="store_true", help="按移动端 UA/布局处理")
    parser.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    parser.add_argument("--version", action="store_true", help="打印版本信息后退出")
    return parser


def print_sources(ui: Ui, cfg: Optional[Config] = None) -> None:
    """把 videoParseList 按两组（内嵌 / 弹窗）打印出来。"""
    ui.section("[内嵌播放]")
    for index, item in enumerate(VIDEO_PARSE_LIST):
        if item.kind == RESOURCE or "1" in item.type:
            tag = f"（{item.badge}）" if item.badge else ""
            ui._print(f"  {index}  {item.name}{tag}", style="color(110)")
    ui.section("[弹窗播放不带选集]")
    for index, item in enumerate(VIDEO_PARSE_LIST):
        if item.kind == COLLECT and "3" in item.type:
            ui._print(f"  {index}  {item.name}  {item.url}", style="color(110)")
    if cfg is not None:
        ui.section("[播放后端]")
        for key, backend in available_backends(cfg).items():
            ui._print(f"  {key:8s} {backend.describe()}", style="color(110)")
    ui.section("[缓存目录]")
    ui._print(f"  {CACHE_ROOT}", style="color(110)")
    ui._print("    embed\\player.html        内嵌播放页（file:// 兜底时用）", style="dim")
    ui._print("    hls.min.js               hls.js 本地缓存（embed 播放页离线兜底）", style="dim")
    ui._print("    prefetch\\<hash>_<片名>\\   预取的 HLS 分片（--keep-cache 时保留）", style="dim")


def _prefetch_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    """把 --prefetch-workers / --prefetch-ahead / --keep-cache 收成 kwargs。"""
    return {
        "workers": max(1, args.prefetch_workers),
        "ahead": max(1, args.prefetch_ahead),
        "keep_cache": args.keep_cache,
    }


def _save_opts(args: argparse.Namespace) -> Dict[str, Any]:
    """把保存 mp4 相关的 CLI 参数收成 dict，供 `d` 指令与 --save 共用。

    并发路数默认沿用 --prefetch-workers（同一个下载器，没必要再开一个旋钮）。
    """
    return {
        "out": args.out,
        "workers": max(1, args.prefetch_workers),
        "variant": args.variant,
    }


def _validate_source(index: int, ui: Ui) -> Optional[int]:
    """校验 --source 编号；越界时打印错误并返回 None（调用方据此退出）。"""
    if 0 <= index < len(VIDEO_PARSE_LIST):
        return index
    ui.toast(f"✗ 没有编号为 {index} 的解析源（可用编号 0-{len(VIDEO_PARSE_LIST) - 1}，"
             f"见 --list）")
    return None


def _source_needs_page_url(index: int) -> bool:
    """这个解析源是不是「必须有视频页 URL」才能用 —— 也就是 kind 的分流判据。

    * 采集型（COLLECT，LX/789/冰豆/麒麟…）：工作方式是 `source.url + 页面URL`
      （见 consumer.show_player_window），它解析的是**一个具体视频页**，不是片名。
      只给 --title 时必须先「片名 → 官方页」（site_search）才能喂给它。
    * 资源型（RESOURCE，无损云/暴风/红牛）：自带片库，按**片名**搜索就有整季直链，
      没有页面 URL 也能起播；反过来，硬塞一个官方页给它并没有意义。

    返回 True 表示走采集型通路。所有「该不该解析 title / 能不能弹窗 / 能不能直接
    --save」的分支都只问这一个函数，免得各处判据各写一份、漂移。
    """
    return VIDEO_PARSE_LIST[index].kind == COLLECT


def _make_backend(cfg: Config, prefer: str, ui: Optional[Ui] = None, **prefetch_kwargs: Any) -> Any:
    """按 --player 挑后端；prefetch 的并发参数与进度回调在这里注入。

    里程碑消息走 ui.toast（一次一行），高频的每片推进走 ui.progress 进度条
    （原地刷新）—— 否则上千片会刷上千行，终端根本没法看。
    """
    if ui is not None and prefetch_kwargs.get("on_progress") is None:
        # 预取进度来自后台线程，会在用户敲命令时插播 → 标成 bg 块，与主线输出隔开
        prefetch_kwargs["on_progress"] = lambda text: ui.toast(text, False, block="bg")
    if ui is not None and prefetch_kwargs.get("make_progress") is None:
        prefetch_kwargs["make_progress"] = ui.progress
    if prefer and prefer != "auto":
        backends = available_backends(cfg, **prefetch_kwargs)
        if prefer in backends:
            return backends[prefer]
        if prefer == "prefetch":
            ui and ui.toast(
                "✗ prefetch 后端不可用：需要本机装有 mpv 或 ffplay"
            )
    return detect_backend(cfg, **prefetch_kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 直接按片名搜索（--title）：跳过页面，走资源型站点的后半段
# ─────────────────────────────────────────────────────────────────────────────
def play_by_title(
    title: str,
    episode: int = 0,
    ui: Optional[Ui] = None,
    backend: Optional[Any] = None,
    is_mobile: bool = False,
    no_play: bool = False,
    source: Optional[int] = None,
) -> Optional[Any]:
    """不依赖视频页：`searchWithRetry → getEpisodes → load`。

    相当于 wsyzyDirect.play() 去掉「接管容器」和「等片名稳定」两步。
    `no_play=True` 时只解析出选集与目标集，不起播（--save 用：要的是地址不是播放）。

    `source` 是**资源型源的下标**（无损云 / 暴风 / 红牛 —— 各家 api 与搜索通路都在
    那条源的定义里）。给了采集型下标属于调用方分错流，直接报错而不是偷偷换成无损云：
    用户明确点了源，就不能假装它生效了。
    """
    from .wsyzy import PlayResult

    ui = ui or Ui()
    src = VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX if source is None else source]
    if src.kind != RESOURCE:
        raise ValueError(f"{src.name} 是采集型源，没有片库可按片名搜索")
    # 这条路径没有真实页面：Config/PageSession 只是 DOM 层的占位会话，
    # 沿用无损云域名是为了与浏览器版一致，播谁都一样，不影响取流。
    cfg = Config(url="https://wsyzy.cc/", is_mobile=is_mobile)
    page = PageSession(cfg.href)
    direct = WsyzyDirect(cfg, page, ui, backend, source=src)

    # 先用页面标题原样搜索，只有搜不到时才退回 wsyzyCleanTitle 的结果
    # （searchWithRetry 内部已实现这个顺序），所以这里不能提前清洗——
    # 提前清洗会把「庆余年第二季」削成「庆余年」，丢掉精确命中的机会。
    ui.set_status(f"「{title}」{src.name}搜索中...")
    try:
        candidates = direct.search_with_retry(title)
    except WsyzyError as exc:
        ui.toast(f"✗ 搜索失败：{exc}")
        return None
    if not candidates:
        ui.toast(f"✗ {src.name}未收录「{title}」")
        return None

    if len(candidates) == 1:
        hit = candidates[0]
        detail_map: Dict[Any, Dict[str, Any]] = {}
        # 唯一候选但名字完全对不上（_score==0）：多半是关键词本身有问题
        # （例如把 -title 写成单横线，真正搜出去的是 "itle"）。
        # 此时会直接播，但终端里没有任何视觉线索，所以补一句提示。
        if (hit.get("_score") or 0) == 0:
            ui.toast(
                f"⚠ 唯一结果「{hit['name']}」与关键词「{title}」并不匹配，"
                f"请确认片名是否写对（--title 需要两个横线）"
            )
    else:
        try:
            detail_map = direct.get_details([c["id"] for c in candidates])
        except WsyzyError:
            detail_map = {}
        hit = None
        if (
            candidates[0].get("_score") == 4
            and (len(candidates) < 2 or (candidates[1].get("_score") or 0) < 3)
        ):
            hit = candidates[0]
        if hit is None:
            ui.toast(f"搜到 {len(candidates)} 个结果，请选择要播放的", False)
            try:
                hit = ui.ask_choice(candidates, detail_map)
            except KeyboardInterrupt:
                ui.toast("✗ 已取消")
                return None

    ui.set_status(f"命中「{hit['name']}」，获取选集...")
    try:
        eps = direct.get_episodes(hit["id"])
    except WsyzyError as exc:
        ui.toast(f"✗ 获取选集失败：{exc}")
        return None
    if not eps:
        ui.toast("✗ 未取到播放地址")
        return None

    idx = 0
    if episode > 0:
        idx = min(episode - 1, len(eps) - 1)

    if no_play:
        # --save：只要地址，不起播（也就不需要播放后端）
        ui.toast(f"共{len(eps)}集，已定位到第{idx + 1}集")
        return PlayResult(
            title=title,
            hit=hit,
            episodes=eps,
            index=idx,
            backend=None,          # type: ignore[arg-type]
            player_url=PLAYER + eps[idx]["url"],
        )

    ui.toast(f"共{len(eps)}集，正在加载播放器...", False)

    player = backend or detect_backend(cfg)
    setter = getattr(player, "set_playlist", None)
    if callable(setter):
        setter(eps, idx, hit["name"])
    player.play(eps[idx]["url"], title=hit["name"])
    # 选集网格留给交互循环入口统一打（进循环后「正在播放」toast 旁边），
    # 这里再打一遍就是重复；--save/no_play 根本到不了这行，不受影响。
    ui.toast(f"✓ {src.name}播放中（{player.describe()}）")

    return PlayResult(
        title=title,
        hit=hit,
        episodes=eps,
        index=idx,
        backend=player,
        player_url=PLAYER + eps[idx]["url"],
        embed_page=getattr(player, "page_path", None),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 解析源菜单（对应 VIP 悬浮框里的两个 ul）
# ─────────────────────────────────────────────────────────────────────────────
def choose_source(consumer: Consumer, ui: Ui, default: Optional[int] = None) -> bool:
    """让用户挑一个解析源，等价于点 VIP 悬浮框里的某个 li。

    同一个源会同时出现在两个列表（type="1,3"）：`.nq-li` 走内嵌播放，
    `.tc-li` 走弹窗播放。终端里用两种输入区分：
        <数字>    内嵌播放（consumer.pick，对应 .nq-li 的 click）
        p<数字>   弹窗播放（consumer.pick_popup，对应 .tc-li 的 click）
    `default` 非 None 时不询问直接选它（--no-interact 用）。
    返回是否成功选中并起播。
    """
    menu = consumer.menu or consumer.generate_element()

    if default is not None:
        source = consumer._source_at(default)
        if source is None:
            ui.toast(f"✗ 没有编号为 {default} 的解析源")
            return False
        ui.toast(f"自动选择解析源：{source.name}")
        return _dispatch(consumer, ui, default, popup=False)

    # 同一个源会同时进 .nq-li(内嵌) 与 .tc-li(弹窗) 两个 ul（type="1,3"）。
    # 终端合并成一张表：每个源只列一行，能弹窗的加后缀标注；编号=内嵌播放(带选集)，
    # p<编号>=弹窗播放(不带选集)。避免同一批源被列两遍。
    popup_idx = {e["index"] for e in menu.popup}
    embedded_idx = {e["index"] for e in menu.embedded}
    ui._print("[解析源]  <编号>=内嵌播放(带选集)   p<编号>=弹窗播放(不带选集)",
              style="bold color(117)")
    for entry in menu.embedded:
        mark = "   [可弹窗 p{}]".format(entry["index"]) if entry["index"] in popup_idx else ""
        ui._print(f"  {entry['index']}  {entry['title']}{mark}", style="color(110)")
    # 极少数只支持弹窗、不在内嵌表里的源，补在最后，别漏
    for entry in menu.popup:
        if entry["index"] not in embedded_idx:
            ui._print(f"  p{entry['index']}  {entry['name']}  (仅弹窗)", style="color(110)")

    while True:
        try:
            raw = ui._input(
                f"选择解析源（回车={DEFAULT_SOURCE_INDEX} "
                f"{VIDEO_PARSE_LIST[DEFAULT_SOURCE_INDEX].name}，q 退出）> ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return False
        if not raw:
            return _dispatch(consumer, ui, DEFAULT_SOURCE_INDEX, popup=False)
        if raw.lower() in ("q", "quit", "exit"):
            return False
        popup = raw[:1].lower() == "p"
        digits = raw[1:] if popup else raw
        if not digits.isdigit():
            ui.toast("无效输入：请输入编号，或 p+编号")
            continue
        index = int(digits)
        if consumer._source_at(index) is None:
            ui.toast(f"✗ 没有编号为 {index} 的解析源")
            continue
        return _dispatch(consumer, ui, index, popup=popup)


def _dispatch(consumer: Consumer, ui: Ui, index: int, popup: bool) -> bool:
    """把菜单选择映射成对应的 li click 回调。"""
    if popup:
        opened = consumer.pick_popup(index)
        if opened:
            ui.toast("✓ 已在浏览器中打开解析页（弹窗播放不带选集）")
        else:
            ui.toast("✗ 打开浏览器失败")
        return opened

    result = consumer.pick(index)
    if result is None:
        ui.toast("✗ 该解析源未能起播，可换一个试试")
        return False
    return True


def _save_one(url: str, title: str, ep_name: str, ui: Ui, opts: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None) -> bool:
    """保存单个地址为 mp4。返回是否成功（`d` 指令与 --save 共用）。

    `headers` 给第三方抽出的 m3u8 带自定义 UA/Referer（资源型直连留空走默认头）。

    保存是长任务（整集约 400MB / 数分钟），所以：
      * 里程碑（解析完成/已保存）走 ui.toast，一次一行
      * 高频推进（每片一次）走 ui.progress 进度条，原地刷新不刷屏
      * Ctrl+C 转成 stop_event，中断后不留半个成品（下载写 .part.mp4）
    """
    ui.toast(f"⬇ 开始保存：{(title + ' ' + ep_name).strip() or url}", False)

    stop = threading.Event()
    try:
        res = save_as_mp4(
            url,
            out=opts.get("out"),
            title=title,
            ep_name=ep_name,
            workers=opts.get("workers", 8),
            variant=opts.get("variant", "highest"),
            stop_event=stop,
            on_progress=lambda text: ui.toast(text, False, block="bg"),
            make_progress=ui.progress,
            headers=headers,
        )
    except KeyboardInterrupt:
        stop.set()
        ui.toast("✗ 已取消保存")
        return False
    except DownloadError as exc:
        ui.toast(f"✗ 保存失败：{exc}", False)
        # --quiet 会吞掉 toast，但「为什么失败」必须让人看见：
        # 否则调用方只拿到一个 rc=2，完全无从排查。所以错误一律再走 stderr。
        print(f"video_vip: 保存失败：{exc}", file=sys.stderr)
        return False
    except Exception as exc:                      # 不吞异常，但要给出可读提示
        ui.toast(f"✗ 保存出错（{type(exc).__name__}）：{exc}", False)
        print(f"video_vip: 保存出错（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return False

    ui.toast(f"✓ 已保存到 {res.path}", False)
    ui._print(f"  大小：{res.size / 1024 / 1024:.1f}MB   时长：{int(res.duration // 60)}:"
              f"{int(res.duration % 60):02d}   用时：{res.elapsed:.1f}s", style="color(110)")
    if not res.via_local_segments:
        ui._print("  （该地址不是标准分片流，由 ffmpeg 直接下载封装）", style="dim")
    if getattr(ui, "quiet", False):
        # 静默模式下只留一行成品路径到 stdout，方便调用方捕获
        print(res.path)
    return True


def _downloadable_source_names() -> str:
    """已接入「抽流下载」的第三方源（编号 名称），报错时给出可直接抄的编号。"""
    return "、".join(f"{i} {src.name}" for i, src in enumerate(VIDEO_PARSE_LIST)
                     if src.kind == COLLECT and jx_resolve.is_supported(src.url))


def _source_of_parse_page(parse_page: str) -> Optional[int]:
    """解析页 URL 是哪个源拼出来的（前缀命中）；认不出返回 None。"""
    for i, src in enumerate(VIDEO_PARSE_LIST):
        if src.url and parse_page.startswith(src.url):
            return i
    return None


def _thirdparty_stream(parse_page: str, ui: Ui, name: str) -> Optional[tuple]:
    """第三方解析页 → (明文 m3u8 直链, 请求头)；抽不出返回 None（原因已打印）。

    解析页本身是 HTML，直接喂下载器会失败；jx_resolve 按「host → 抽流接口」映射把
    播放器自己发的那些请求照抄出来，拿到真·m3u8。**未接入的源** `is_supported` 就是
    False（不发任何请求），这里如实列出可用编号，绝不臆造地址。
    """
    if not jx_resolve.is_supported(parse_page):
        usable = _downloadable_source_names()
        ui.toast(f"✗ 「{name}」未接入下载接口，可改用：\n"
                 f"    {usable}\n"
                 f"    或资源型源按片名直连：-s {DEFAULT_RESOURCE_INDEX}", False)
        print(f"video_vip: 「{name}」未接入下载接口（可用：{usable}）", file=sys.stderr)
        return None
    info = jx_resolve.resolve(parse_page)
    if info is None:
        ui.toast(f"✗ 「{name}」本次抽流失败（接口无响应/线路变更），\n"
                 f"    可重试，或改用资源型源按片名直连：-s {DEFAULT_RESOURCE_INDEX}", False)
        print(f"video_vip: 「{name}」抽流失败", file=sys.stderr)
        return None
    link = info.m3u8 if len(info.m3u8) <= 90 else info.m3u8[:87] + "..."
    ui.toast(f"⬇ 第三方源已抽到 m3u8 直链，转下载：{link}", False)
    return info.m3u8, info.headers()


def _do_save(result: Any, ui: Ui, opts: Dict[str, Any]) -> bool:
    """保存交互循环里当前这一集（`d` 指令）。

    资源型源的每集 url 本身就是 m3u8 直链，直接下；采集型源的每集 url 是 `源前缀+官方页`
    的解析页（HTML、非流），先用 jx_resolve 抽出真·m3u8 再下；抽不出（未逆通的接口）
    就如实提示改用资源型源，绝不臆造地址。
    """
    ep = result.episode()
    url = ep.get("url") or ""
    if not url:
        ui.toast("✗ 当前集没有可用的播放地址")
        return False
    title = result.hit.get("name", "") if result.hit else ""
    headers = None
    if not is_media_url(url):                       # 第三方解析页：抽流出真 m3u8
        idx = _source_of_parse_page(url)
        name = VIDEO_PARSE_LIST[idx].name if idx is not None else "该第三方源"
        got = _thirdparty_stream(url, ui, name)
        if got is None:
            return False
        url, headers = got
    return _save_one(url, title, ep.get("name", ""), ui, opts, headers=headers)


def _save_pick_episode(url: str, episode: int, ui: Ui) -> str:
    """--save <视频页URL> 时把 URL 定到**具体单集页**（第三方源解析不了整季入口页）。

    两种情况要枚举整季：①粘的是入口页（爱奇艺 /a_、腾讯 cid-only cover，实测第三方
    源解析返回 None）；②给了 --episode。枚举不到就如实退回原 URL，绝不假装切了集。
    """
    if not (_looks_like_collection_page(url) or episode > 0):
        return url
    concrete = [pu for _n, pu in (list_episode_pages(url) or [])]
    if not concrete:
        if episode > 0:
            ui.toast(f"⚠ 该站枚举不出整季，第{episode}集定不了：\n"
                     f"    按你给的页面原样下载", False)
        return url
    if episode > 0:
        idx = min(episode - 1, len(concrete) - 1)
        if episode > len(concrete):
            ui.toast(f"⚠ 这一季只枚举到 {len(concrete)} 集，改下第 {idx + 1} 集", False)
        return concrete[idx]
    return concrete[0]


def _run_save(args: argparse.Namespace, ui: Ui, source: Optional[int] = None) -> int:
    """--save：解析出目标地址后直接保存，不播放、不进交互循环。

    取流有两条路，看 `source`（用户显式给的 -s 编号）：
      · 资源型 —— 片名 → 搜索片库 → 每集本身就是 m3u8 直链；
      · 采集型 —— 视频页 → `source.url + 页面` 解析页 → jx_resolve 抽明文 m3u8，
        接到和资源型**同一条**下载通路（并发分片 + ffmpeg 封装）。
    没给 -s 时仍走默认资源型站（DEFAULT_RESOURCE_INDEX，现时无损云），**刻意不跟播放
    路径共用默认源**：直连不依赖第三方接口是否存活、还能 --variant 选清晰度，对
    「下载」这件事更稳；想用第三方源下载得显式 -s。
    """
    opts = _save_opts(args)

    def fail(msg: str) -> int:
        """报错：toast 会被 --quiet 吞掉，所以失败原因一律再走 stderr。"""
        ui.toast(msg)
        print(f"video_vip: {msg.lstrip('✗⚠ ').strip()}", file=sys.stderr)
        return 2

    # 1) 位置参数本身就是媒体流地址（m3u8/mp4/...）→ 不用抓页面，直接存
    if args.url and is_media_url(args.url):
        ok = _save_one(args.url, args.title or "", "", ui, opts)
        return 0 if ok else 2

    ep_name = f"第{args.episode}集" if args.episode > 0 else ""

    # 2) 显式指定了第三方解析源：视频页 → 解析页 → 抽流 → 下载
    if source is not None and _source_needs_page_url(source):
        src = VIDEO_PARSE_LIST[source]
        page = args.url or ""
        drama = args.title or ""
        if page:
            page = _save_pick_episode(page, args.episode, ui)
            drama = drama or fetch_show_name(page)
        else:
            if not args.title:
                return fail("✗ --save 用第三方源需要片名（--title）或视频页 URL")
            hit = find_page_url(args.title, episode=args.episode, prefer=args.site,
                                on_note=lambda t: ui.toast(t, False))
            if hit is None:
                _r = VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX]
                return fail(f"✗ 腾讯/爱奇艺/优酷/芒果TV 都没搜到「{args.title}」的视频页，\n"
                            f"    改用资源型源按片名搜库下载：--title {args.title} "
                            f"-s {DEFAULT_RESOURCE_INDEX}（{_r.name}）")
            if hit.episode > 0 and not hit.episode_pinned:
                ui.toast(f"⚠ 未能定位第{hit.episode}集（{hit.site} 枚举异常或集数超出范围）：\n"
                         f"    将下载整季页枚举出的那一集", False)
            page, drama = hit.url, args.title
            ui.toast(f"✓ 找到{hit.label()}视频页：\n    {page}", False)
        if not url_supported(page):
            ui.toast(f"⚠ {page} 不在 @include 列表内，仍尝试抽流")
        ui.toast(f"正在用「{src.name}」抽流...", False)
        got = _thirdparty_stream(src.url + page, ui, src.name)
        if got is None:
            return 2
        ok = _save_one(got[0], drama, ep_name, ui, opts, headers=got[1])
        return 0 if ok else 2

    # 3) 资源型直连：这条只能按片名搜（-s 给的是哪家库就用哪家，没给则默认源）。
    #    同时给了 URL 时**以 URL 为准**（与播放路径同
    #    规则：URL 优先、--title 忽略）—— 先试官方数据源 fetch_show_name（桌面页是反爬
    #    空壳、解析页 <title> 又是源名，读标题不可靠），拿不到才退回读页面标题，
    #    两者都失败才用 --title 兜底：总比直接失败强。
    name = args.title or ""
    if args.url:
        ui.set_status("读取页面片名...")
        page = PageSession(args.url)
        try:
            page.load()
        except Exception as exc:
            return fail(f"✗ 页面加载失败（{type(exc).__name__}）：{exc}")
        name = fetch_show_name(args.url) or ""
        if not name:
            try:
                name = wait_stable_title(page) or ""
            except KeyboardInterrupt:
                ui.toast("✗ 已取消")
                return 1
            except Exception:
                name = ""
        if name:
            if args.title and name != args.title:
                ui.toast(f"ℹ 已按 URL 识别片名「{name}」，忽略 --title「{args.title}」")
            else:
                ui.toast(f"识别到片名「{name}」")
        else:
            name = args.title or ""
            if not name:
                return fail("✗ 无法从页面识别片名，请用 --title 指定")
            ui.toast(f"⚠ 页面识别不出片名，改用 --title「{name}」")
    if not name:
        return fail("✗ --save 需要指定片名（--title）或视频地址")

    result = play_by_title(
        name,
        episode=args.episode,
        ui=ui,
        backend=None,
        is_mobile=args.mobile,
        no_play=True,
        source=source,
    )
    if result is None:
        return 2
    ok = _do_save(result, ui, opts)
    return 0 if ok else 2


def interactive(
    consumer: Optional[Consumer],
    result: Any,
    ui: Ui,
    cfg: Config,
    save_opts: Optional[Dict[str, Any]] = None,
    site: Optional[str] = None,
    season_url: Optional[str] = None,
) -> None:
    """播放后的命令循环（换集 / 换源 / 换片 / 保存）。

    `s`（换解析源）与 `m`（换片名）的区别是**要不要保持集数**：
      * `s` 是「同一影片换个解析接口」，复用当前这一集的官方页重解析、集号保持不变；
      * `m` 是换一部片子，理应从第 1 集开始。
    两条命令都可以写成一行（`m 嫁金钗 site 爱奇艺`、`s 4` / `s 麒麟` / `s p4`），
    解析在 `_split_site_arg` / `_resolve_source_arg`；只敲单个字母才逐项追问。
    参数认不出时一律**拒绝执行**并列出可用值 —— 把「哔哩哔哩」当片名硬搜、
    或把一个模糊源名硬切过去，都比原地不动更糟。
    两者最终都落到 `_search_ws` / `_search_page`：资源型源按片名直连搜片库；采集型源解析
    的是具体视频页，于是借道 `find_page_url`（站内搜腾讯/爱奇艺/优酷/芒果TV）先定位
    官方页再喂给所选源，找不到就如实回退默认资源型源。
    `site` 是**优先站、不是硬限定**：那家没搜到会自动退回其它三家（find_page_url 会
    如实提示「改用 腾讯视频」）；资源型会话不分官方站，写了会被忽略。`m` 换片名时可
    临时改它（见 `m` 分支）。`season_url` 是可枚举整季的那一页（入口页），换源时拿它
    补全「共 N 集」。
    """
    save_opts = dict(save_opts or {})
    ui.render_help()
    # 一进循环就把**全部集数**列出来（编号即跳转键，n/p 换集），当前集高亮。
    # 这是启动后唯一一次网格：起播路径（_play_wsyzy）不再提前打，避免重复。
    if result is not None and result.episodes:
        _cur = result.episode()
        ui.toast(f"✓ 正在播放 {_cur.get('name') or ('第' + str(result.index + 1) + '集')}")
        ui.ep_bar(result.episodes, result.index,
                  lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                  title=result.title)

    # 换集动作可能来自两个线程：主循环（用户敲 n/p/数字）与页面选集轮询线程。
    # 两边都会改 result.index 并调 backend.play()，必须串行化，
    # 否则会出现「两边各切一次、后一次把前一次覆盖掉」的竞态。
    switch_lock = threading.RLock()

    def switch_to(target: int) -> bool:
        """换到第 target 集（0-based）。终端指令与页面选集共用这一条路径。"""
        with switch_lock:
            if result is None:
                return False
            eps = result.episodes
            if not eps or not (0 <= target < len(eps)):
                return False
            result.index = target
            ep = eps[target]
            result.backend.play(ep["url"], title=result.hit.get("name", ""))
            setter = getattr(result.backend, "set_playlist", None)
            if callable(setter):
                setter(eps, target, result.hit.get("name", ""))
        return True

    # ── 统一的「换片名 / 换源」重起点（relaunch）────────────────────────────
    # 记住当前片名 / 当前源 / 优先官方站 / 整季入口页：m 改片名（可顺带改站）、s 改源
    # 都以它为准。这样 m 不会把采集型源重置回资源型源，s 也不会丢掉当前在播的那一集。
    state = {
        "src": (consumer.selected_index if consumer is not None
                and consumer.selected_index is not None else DEFAULT_RESOURCE_INDEX),
        "title": (result.hit.get("name", "") if result is not None else ""),
        "site": site,
        "season": season_url or "",
        # 弹窗播放（p+编号）只是浏览器里另开的窗口，不改终端会话在用的源，所以它
        # 单独记一笔，供 `s` 菜单标「弹窗中」—— 两个标记可能落在不同行上。
        "popup_src": None,
    }

    def _cur_index() -> int:
        """当前在播第几集（0-based）。"""
        with switch_lock:
            return result.index if result is not None else 0

    def _episode_no() -> int:
        """当前集号（1-based，传给 find_page_url / play_by_title；第1集用 0=默认）。"""
        cur = _cur_index()
        return cur + 1 if cur > 0 else 0

    def _search_ws(title: str, episode: int = 0,
                   src: Optional[int] = None) -> Optional[Any]:
        """资源型源按片名起播，复用当前后端（不新建，避免动到 embed 本地服务）。

        `episode` 是 1-based 集号（0=第 1 集）：换源时带上它才能保持集数不变。
        `src` 是要连哪家片库（无损云/暴风/红牛），不传用默认资源型源。
        """
        with switch_lock:
            backend = result.backend if result is not None else None
        return play_by_title(title, episode=episode, ui=ui,
                             backend=backend, is_mobile=cfg.isMobile, source=src)

    def _search_page(title: str, src: int, episode: int = 0) -> Optional[Any]:
        """第三方源：片名 → 官方页 URL（站内搜索）→ start 解析该页并选 src。

        `episode`（1-based，0=第1集）交给 `find_page_url` 去定位那一集的单集页。
        起播后 `result.index` 由 `_enrich_thirdparty_episodes` 按「实际解析的是哪一页」
        对齐，所以调用方**不必**再手动改集号。
        返回 (new_consumer, new_result)；页面找不到或解析失败返回 None（调用方回退）。
        """
        hit = find_page_url(title, episode=episode, prefer=state.get("site"),
                            on_note=lambda t: ui.toast(t, False))
        if hit is None:
            return None
        ui.toast(f"✓ 找到{hit.label()}视频页：\n    {hit.url}", False)
        if hit.episode > 0 and not hit.episode_pinned:
            # 枚举失败退回整季页 —— 如实说，绝不报「已定位第N集」
            ui.toast(f"⚠ {hit.site} 未能定位第{hit.episode}集（枚举异常或超出总集数）：\n"
                     f"    已打开整季页，请在选集栏手动点第{hit.episode}集", False)
        with switch_lock:
            backend = result.backend if result is not None else None
        new_consumer = start(hit.url, ui=ui, backend=backend,
                             page=PageSession(hit.url), auto=True, source=src)
        if new_consumer is None or new_consumer.last_result is None:
            if new_consumer is not None:
                try:
                    new_consumer.shutdown(stop_player=False)
                except Exception:
                    pass
            return None
        # 第三方源只解析得出这一页（1 集）——补成整季，换片名后终端仍能 n/p/l 换集
        _enrich_thirdparty_episodes(new_consumer.last_result, new_consumer,
                                    hit.url, hit.season_url or hit.url, ui, drama=title)
        state["season"] = hit.season_url or hit.url
        return new_consumer, new_consumer.last_result

    def relaunch(title: str, src: int, episode: int = 0) -> bool:
        """用 (title, src) 重新起播；`episode` 是 1-based 集号（0=第 1 集）。

        `m`（换片名）传 0 —— 换一部片子理应从第 1 集开始；`s`（换源）走到这里时传
        当前集号 —— 换接口不该改看在看第几集。集号由 `_search_ws`/`_search_page`
        内部对齐，这里**不再**强行回第 1 集。

        consumer 生命周期：换片名/换源都重建一份，旧的 shutdown(stop_player=False)
        —— 保留后端进程/本地服务复用，只是不再管它。资源型会话则 consumer=None。
        """
        nonlocal result, consumer
        if not title:
            ui.toast("✗ 片名为空，无法解析")
            return False
        # 关掉旧页面会话（无损云会话本来就没有 consumer）
        if consumer is not None:
            try:
                consumer.shutdown(stop_player=False)
            except Exception:
                pass
            consumer = None

        new_result: Optional[Any] = None
        applied_src = src
        if not _source_needs_page_url(src):
            new_result = _search_ws(title, episode, src)   # 资源型：按片名搜片库
        else:
            got = _search_page(title, src, episode)
            if got is not None:
                consumer, new_result = got
            else:
                # 回退目标是「默认那家资源型源」，不是硬编码的 0 号位
                _fb = VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX]
                ui.toast(f"⚠ 没搜到「{title}」的可解析视频页，"
                         f"{VIDEO_PARSE_LIST[src].name} 用不上；已回退{_fb.name}。", False)
                new_result = _search_ws(title, episode, DEFAULT_RESOURCE_INDEX)
                applied_src = DEFAULT_RESOURCE_INDEX
        if new_result is None:
            ui.toast(f"✗ 「{title}」未能起播，保持原影片不变")
            return False

        result = new_result
        state["title"] = title
        state["src"] = applied_src
        # 集号是各路径内部对齐的（资源型按 idx、采集型按「解析的是哪一页」）；
        # 定不到用户要的那一集就如实说，绝不说「已定位第N集」。
        if episode and (result.index or 0) + 1 != episode:
            ui.toast(f"⚠ 没能定位到第{episode}集（枚举异常或超出总集数）：\n"
                     f"    现在停在第{(result.index or 0) + 1}集", False)
        ui.ep_bar(result.episodes, result.index,
                  lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                  title=result.title)
        return True

    def switch_source(src: int) -> bool:
        """s 换源：**同一影片换个解析接口**，并且**保持在播的那一集**。

        第三方源 → 第三方源：直接拿「当前这一集的官方页」重解析，而不是会话最初那一页
        —— 否则用户看到第 5 集换个源就被拽回第 1 集。当前集官方页 = 选集栏里那条
        `源前缀 + 官方页` 去掉前缀；枚举不出整季（只有 1 集）时退回会话最初的页。

        另两种情况退回按片名重跑 relaunch，并照样带上当前集号：①当前没有页面会话
        （资源型直连）；②目标是资源型（它按片名搜索、没有解析页可复用）。
        绝不因为「换了源」就重搜第三方页：重搜依赖剧名准确，而第三方源的 hit.name 在
        优酷移动页偶发取空时会退回解析页标题=源名，于是会去搜「M3U8解析」而失败。"""
        nonlocal result, consumer
        page_url = (consumer.page.url if consumer is not None else "") or ""
        episode = _episode_no()
        if (not page_url or not _source_needs_page_url(src)
                or not _source_needs_page_url(state["src"])):
            return relaunch(state["title"], src, episode)
        # 当前集的官方页：认不出前缀（枚举不出整季/源已变）就退回会话最初的页
        target_page = page_url
        cur = _cur_index()
        prefix = VIDEO_PARSE_LIST[state["src"]].url or ""
        with switch_lock:
            eps = list(result.episodes) if result is not None else []
        if 0 <= cur < len(eps) and prefix:
            u = eps[cur].get("url") or ""
            if u.startswith(prefix) and u[len(prefix):]:
                target_page = u[len(prefix):]
        # 关掉旧会话（shutdown(stop_player=False) 保留后端进程/本地服务复用）
        if consumer is not None:
            try:
                consumer.shutdown(stop_player=False)
            except Exception:
                pass
            consumer = None
        with switch_lock:
            backend = result.backend if result is not None else None
        new_consumer = start(target_page, ui=ui, backend=backend,
                             page=PageSession(target_page), auto=True, source=src)
        if new_consumer is None or new_consumer.last_result is None:
            if new_consumer is not None:
                try:
                    new_consumer.shutdown(stop_player=False)
                except Exception:
                    pass
            ui.toast(f"✗ 没能用「{VIDEO_PARSE_LIST[src].name}」解析当前影片，保持不变")
            return False
        # 补成整季选集，剧名沿用当前 state["title"]（复用同一页，不换片）；
        # 整季入口页优先用已知的那个（--title 路径/上次枚举拿到的），退回会话原页。
        season = state.get("season") or page_url
        _enrich_thirdparty_episodes(new_consumer.last_result, new_consumer,
                                     target_page, season, ui, drama=state["title"])
        consumer = new_consumer
        result = new_consumer.last_result
        state["src"] = src
        state["season"] = season
        ui.ep_bar(result.episodes, result.index,
                  lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                  title=result.title)
        # 集号是 _enrich 按「解析的是哪一页」对齐的：定不到用户在看的那一集就如实说
        now = _cur_index()
        if episode and now + 1 != episode:
            ui.toast(f"✓ 已用「{VIDEO_PARSE_LIST[src].name}」解析同一影片\n"
                     f"    ⚠ 没能定位到第{episode}集（该站枚举不出整季），"
                     f"现在在第{now + 1}集", False)
        else:
            ui.toast(f"✓ 已用「{VIDEO_PARSE_LIST[src].name}」解析同一影片"
                     f"（保持第 {now + 1} 集）", False)
        return True

    def _apply_source(index: int, popup: bool) -> None:
        """把一次「选源」落地：内嵌走 switch_source（保持当前集），弹窗走 pick_popup。

        交互菜单（`s` 后挑编号）与一行式（`s 4` / `s p4`）共用这条路径，
        免得两种写法各自的边界条件不同步。
        """
        if popup and VIDEO_PARSE_LIST[index].kind == RESOURCE:
            ui.toast(f"✗ {VIDEO_PARSE_LIST[index].name} 是资源型源，按片名搜片库，\n"
                     f"    没有可弹窗的解析页；要内嵌播它就写：s {index}")
            return
        if not popup:
            switch_source(index)
            return
        # 弹窗播放不带选集：需要一个视频页。资源型会话没有页，先内嵌
        # 重跑建立页面会话，再在这个页上弹窗。带上当前集号，别掉回第1集。
        if consumer is None:
            relaunch(state["title"], index, _episode_no())
            if consumer is None:
                ui.toast("✗ 这部片没能建立页面会话（回退成资源型源了），弹窗播放用不上")
                return
        if _dispatch(consumer, ui, index, popup=True):
            state["popup_src"] = index      # 给 `s` 菜单标「弹窗中」，不动 state["src"]

    def _source_menu() -> Optional[Any]:
        """列出全部解析源，返回 (index, popup)；q 返回 None。不依赖 consumer。

        一份列表：编号选内嵌播放，`p+编号`选弹窗播放（不带选集，需第三方源）。
        `s` 的用途就是「在几个源之间跳」，所以标出基准：**当前在用的源**打 `▶` 并加粗
        （源名后附「当前」），还开着弹窗的那个注「弹窗中」。不标的话敲一次 `s` 就得重新
        回忆自己刚才用的是哪家 —— 而换源失败时保持不变的也正是这一行。
        """
        cur = state.get("src")
        pop = state.get("popup_src")
        ui._print(f"选择解析源（编号=内嵌，p+编号=弹窗不带选集；"
                  f"回车={VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX].name}，q 取消）：",
                  style="bold color(117)")
        for i, src in enumerate(VIDEO_PARSE_LIST):
            body = (f"{src.name}（按片名搜索，无需 URL）" if src.kind == RESOURCE
                    else f"{src.name}  {src.url}")
            if i == cur:
                tail = "（当前）" + ("，并开着弹窗" if i == pop else "")
                ui._print(f"  ▶ {i}  {body}   {tail}", style="bold color(114)")
            elif i == pop:
                ui._print(f"    {i}  {body}   （弹窗中）", style="color(110)")
            else:
                ui._print(f"    {i}  {body}", style="color(110)")
        while True:
            try:
                raw = ui._input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                ui._print("")
                return None
            if not raw:
                # 回车 = 默认那条资源型通路（按片名搜片库），不是硬编码的 0 号位
                return (DEFAULT_RESOURCE_INDEX, False)
            if raw.lower() in ("q", "quit", "exit"):
                return None
            popup = raw[:1].lower() == "p"
            digits = raw[1:] if popup else raw
            if not digits.isdigit():
                ui.toast("无效输入：请输入编号，或 p+编号")
                continue
            index = int(digits)
            if not (0 <= index < len(VIDEO_PARSE_LIST)):
                ui.toast(f"✗ 没有编号为 {index} 的解析源")
                continue
            if popup and VIDEO_PARSE_LIST[index].kind == RESOURCE:
                ui.toast(f"✗ {VIDEO_PARSE_LIST[index].name} 按片名搜片库，"
                         f"没有可弹窗的解析页；请用编号选它")
                continue
            return (index, popup)

    # embed 后端的播放页在浏览器里，用户在页面选集栏点了某一集、或页面回退到
    # 第三方 iframe / 自动播放被拦时，这些事件是通过本地 HTTP 服务回传的。
    # 主循环阻塞在 ui._input() 上收不到，所以起一个后台线程轮询：
    #   * take_picked() → 走 switch_to()（和终端里敲数字同一条路径，集号与选集栏才同步）
    #   * take_notices() → 打印一行提示
    # 只对提供这些方法的后端（EmbedBackend）生效，其余后端这个线程空转。
    embed_stop = threading.Event()

    def embed_poller() -> None:
        # 每轮重新读 result.backend：`s`（切解析源）/`m`（重选影片）会换掉
        # result 与后端，捕获一次的话会一直盯着旧后端。
        while not embed_stop.wait(0.5):
            with switch_lock:
                backend = result.backend if result is not None else None
            if backend is None:
                continue
            take_notices = getattr(backend, "take_notices", None)
            if callable(take_notices):
                try:
                    for note in take_notices():
                        ui.toast(note, False)
                except Exception:
                    pass
            take_picked = getattr(backend, "take_picked", None)
            if not callable(take_picked):
                continue
            try:
                idx = take_picked()
            except Exception:
                idx = None
            if idx is None:
                continue
            with switch_lock:
                cur = result.index if result is not None else None
                n_eps = len(result.episodes) if result is not None else 0
            if idx == cur or not (0 <= idx < n_eps):
                continue          # 点的就是当前集 / 越界，别重复起播
            if switch_to(idx):
                try:
                    ep = result.episodes[idx]
                    ui.toast("✓ 播放页选集："
                             f"{ep.get('name') or ('第' + str(idx + 1) + '集')}",
                             False, block="bg")
                except Exception:
                    pass

    threading.Thread(target=embed_poller, name="embed-poll", daemon=True).start()

    # embed 后端：确认浏览器真的把播放页打开了。
    # 为什么值得单独等：浏览器起页面要几秒，而万一压根没打开成功（没有默认
    # 浏览器、被安全软件拦了），用户只会看到一个光秃秃的 `video_vip>` 提示符，
    # 完全不知道发生了什么。放独立线程里等，不阻塞提示符（本地播放器后端
    # 没有 wait_for_page，线程会立刻退出）。
    def embed_open_watch() -> None:
        backend = result.backend if result is not None else None
        waiter = getattr(backend, "wait_for_page", None) if backend is not None else None
        if not callable(waiter):
            return
        try:
            opened = bool(waiter(15.0))
        except Exception:
            return                  # 查询失败不该吵人，交互循环照常
        if embed_stop.is_set():
            return                  # 用户已经退出了，别再打一行
        if opened:
            page_url = getattr(backend, "page_url", None)
            ui.toast(f"✓ 播放页已在浏览器打开{('：' + page_url) if page_url else ''}",
                     False, block="bg")
        else:
            ui.toast("⚠ 浏览器没能打开播放页（没有默认浏览器？）。"
                     "可改用 --player mpv / ffplay 在本地播放", False, block="bg")

    threading.Thread(target=embed_open_watch, name="embed-open", daemon=True).start()

    try:
        while True:
            try:
                raw = ui._input("video_vip> ").strip()
            except (EOFError, KeyboardInterrupt):
                ui._print("")
                break
            if not raw:
                continue
            # 首 token 是命令，其余原样是参数：**参数不参与 lower()**。
            # 原先整行 lower 再比较，一行式 `m Osiris 之泪` 会把片名削成小写，
            # 而片名正是站内搜索要用的关键词，字面变了召回就跟着变。
            head, _, tail = raw.partition(" ")
            cmd = head.lower()
            arg = tail.strip()
            if (cmd not in _KNOWN_CMDS and cmd[:1] in ("m", "s") and cmd[1:]):
                # 紧凑写法 `m嫁金钗` / `s4`：中文输入法下不打空格是很自然的动作，
                # 不该逼用户重来。`save` 这类整词命令已在 _KNOWN_CMDS 里，不会被拆。
                arg = (cmd[1:] + (" " + arg if arg else "")).strip()
                cmd = cmd[:1]
            if arg and cmd not in ("m", "s"):
                # 只有 m / s 折叠了参数；别把 `n 3` 当成 `n` 静默执行前半句
                ui.toast(f"✗ 「{cmd}」不带参数（多出来的 {arg!r} 也没被执行）：\n"
                         f"    能写成一行的是 m 片名 [site 官方站] 与 s 编号/源名")
                continue

            if cmd in ("q", "quit", "exit"):
                break

            if cmd == "h":
                ui.render_help()
                continue

            if cmd == "l":
                if result is not None and result.episodes:
                    ui.ep_bar(result.episodes, result.index,
                              lambda e: result.backend.play(
                                  e["url"], title=result.hit.get("name", "")),
                              title=result.title)
                else:
                    ui.toast("当前没有可选集的影片")
                continue

            if cmd == "u" and result is not None:
                ep = result.episode()
                ui.start_block()      # 直链信息是一块独立输出，别贴在上一块下面
                ui._print(f"  片名：{result.hit.get('name')}")
                ui._print(f"  当前：{ep.get('name') or ep['url']}")
                ui._print(f"  m3u8：{ep['url']}")
                ui._print(f"  播放页：{PLAYER}{ep['url']}")
                # embed 后端起的是本地服务，页面地址是 http://127.0.0.1:PORT，
                # 每次起播端口可能变，所以直接问后端要当前值（file:// 兜底时是路径）。
                live_url = getattr(result.backend, "page_url", None)
                if live_url:
                    ui._print(f"  内嵌页：{live_url}")
                elif getattr(result, "embed_page", None):
                    ui._print(f"  内嵌页：{result.embed_page}")
                continue

            if cmd == "a":
                if consumer is not None:
                    on = consumer.toggle_auto()
                    ui.toast(f"自动解析：{'开' if on else '关'}")
                else:
                    ui.toast("自动解析仅在指定 URL 时可用")
                continue

            if cmd in ("d", "dl", "save") and result is not None:
                _do_save(result, ui, save_opts)
                continue

            if cmd == "r":
                if consumer is not None:
                    consumer.reload_current_player()
                elif result is not None:
                    ep = result.episode()
                    result.backend.play(ep["url"], title=result.hit.get("name", ""))
                    ui.toast("↻ 已重新加载")
                continue

            if cmd == "s" and result is not None:
                if arg:
                    # 一行式：`s 4` / `s 麒麟` / `s p4`（弹窗）。认不出就拒绝，不弹菜单
                    popup, index, err = _resolve_source_arg(arg)
                    if index is None:
                        ui.toast(err)
                        continue
                    _apply_source(index, popup)
                    continue
                sel = _source_menu()
                if sel is None:
                    continue
                index, popup = sel
                _apply_source(index, popup)
                continue

            if cmd == "m" and result is not None:
                # 一行式：`m 片名` 直接换片、`m 片名 site 爱奇艺` 顺带换官方站；
                # 只敲 `m` 才逐项追问（老写法不变）。参数认不出时一律拒绝执行，
                # 绝不"当成片名"硬搜 —— 搜错片比不动更糟。
                new_title, site_sel, err = _split_site_arg(arg)
                if err:
                    ui.toast(err)
                    continue
                if site_sel:
                    if _source_needs_page_url(state["src"]):
                        state["site"] = site_sel
                    else:
                        ui.toast(f"ℹ 无损云按片名直连、不分官方站："
                                 f"site「{site_sel}」已忽略", False)
                if not new_title:
                    ui.toast("重新选择影片：请输入新的片名")
                    try:
                        new_title = ui._input("片名 > ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if not new_title:
                        continue
                    # 这一行里没写 site 才追问（写了就以它为准）
                    if _source_needs_page_url(state["src"]) and not site_sel:
                        state["site"] = _pick_site(ui, keep=state.get("site"))
                relaunch(new_title, state["src"])   # 沿用当前源，不回退无损云
                continue

            if result is None:
                ui.toast("当前没有正在播放的影片")
                continue

            eps: List[Dict[str, str]] = result.episodes
            target: Optional[int] = None
            if cmd in ("n", "next"):
                target = result.index + 1
            elif cmd in ("p", "prev"):
                target = result.index - 1
            elif cmd.isdigit():
                target = int(cmd) - 1

            if target is None:
                ui.toast("未知命令，输入 h 查看帮助")
                continue
            if not (0 <= target < len(eps)):
                ui.toast(f"✗ 只有 {len(eps)} 集")
                continue

            if not switch_to(target):
                ui.toast("✗ 换集失败")
                continue
            ep = eps[target]
            ui.toast(f"✓ 正在播放 {ep.get('name') or ('第' + str(target + 1) + '集')}")
            ui.ep_bar(eps, target, lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                      title=result.title)
    finally:
        # 停掉页面选集轮询线程。它是 daemon，显式收一下更干净。
        embed_stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# 给定一个视频页 URL + 解析源，走完整的注入→起播→交互流程
# ─────────────────────────────────────────────────────────────────────────────
def _enrich_thirdparty_episodes(
    result: Any, consumer: Optional[Consumer], url: str, season_url: str, ui: Ui,
    drama: str = "",
) -> None:
    """第三方内嵌源起播后，把 PlayResult 从「1 集」补成整季「共 N 集」。

    第三方解析源拼的是 `source.url + 单个视频页URL`（见 show_player_window），
    consumer 只解析得出当前这一页，所以 last_result 只有 1 条。这里用 site_search
    枚举官方整季的单集页，逐条前缀 `source.url` 拼成各自的解析页地址，于是终端里
    n/p / 数字 / l 都能换集（换集 = 让后端加载另一条解析页，本质与手动换页一致）。
    无损云（wsyzy）自带整季选集，不动；枚举失败或只有一集也保持原样。

    `drama` 是真实剧名（--title 的关键词或页面检测到的标题）。show_player_window
    给第三方结果填的 hit.name/title 默认是**解析源名**（如「LX解析」），会让换集时
    传给播放器的标题、relaunch 记住的片名、以及这里「共 N 集」提示都显示源名而非剧名。
    拿到剧名后统一改写成它。
    """
    if result is None or consumer is None:
        return
    idx = consumer.selected_index
    if idx is None or not _source_needs_page_url(idx):
        return                       # 无损云已有整季与真实剧名，不动
    if drama:
        result.title = drama
        if isinstance(result.hit, dict):
            result.hit["name"] = drama
    if len(result.episodes) > 1:
        return                       # 已经是整季了
    pages = list_episode_pages(season_url or url)
    if not pages or len(pages) <= 1:
        # 枚举不到整季的平台兜底（腾讯/优酷/爱奇艺/芒果TV 均已支持枚举，这里只兜
        # 其它暂无枚举路径的站点）。consumer 给这一集的默认名是**解析源名**（如
        # 「LX解析」），会让选集栏显示成「[1 LX解析]」。至少改写成真实剧名，避免误导
        # （仍是 1 集、n/p 换不了）。
        if drama and result.episodes:
            result.episodes[0]["name"] = drama
        return
    prefix = VIDEO_PARSE_LIST[idx].url
    result.episodes = [{"name": name, "url": prefix + page_url} for name, page_url in pages]
    # 当前加载的就是哪一页，就在列表里高亮哪一集（枚举页对不上则回第 1 集）
    cur = next((i for i, (_n, pu) in enumerate(pages) if pu == url), 0)
    result.index = min(cur, len(result.episodes) - 1)



def _looks_like_collection_page(url: str) -> bool:
    """是否为「整季入口页」——第三方解析源解析的是具体视频页，这类页解析不了。

    爱奇艺 /a_ 专辑页、腾讯 cid-only cover（/x/cover/<cid>.html 没有 /<vid> 段）都是。
    带 vid 的（爱奇艺 /v_、优酷 v_show/id_、腾讯 /x/cover/<cid>/<vid>、/x/page/<vid>）
    是具体单集页，可正常解析，返回 False。
    """
    if re.search(r"iqiyi\.com/a_[A-Za-z0-9]+\.html", url):
        return True
    if re.search(r"v\.qq\.com/x/cover/[^/]+\.html$", url):
        return True
    return False


def _play_page_url(
    args: argparse.Namespace, ui: Ui, url: str, source: Optional[int],
    season_url: Optional[str] = None,
) -> int:
    """用户直接给 URL（模式二）与 `--title`+第三方源（site_search 解析出 URL）共用。

    完整流程：建 Config/后端 → start()（含索引迁移后写 --source）→
    自动/手动选源 → 集数切换 → 交互循环 → 收尾。返回进程退出码。

    `season_url` 是可枚举整季的那一页（cover/专辑页）；`--title`+第三方源命中单集页时
    由调用方传入整季入口页，好把「共 1 集」补成整季。缺省用 `url` 自身枚举。
    """
    # 直接粘整季入口页（爱奇艺 /a_、腾讯 cid-only cover）时，这类页第三方源解析不了
    # （解析的是具体视频页，实测 LX 解析返回 None）。自动改用枚举出的第1集单集页起播，
    # 原入口页转作 season_url 供补全整季 —— 与 --title 路径同一修法，避免"初始第1集失败"。
    if _looks_like_collection_page(url) and not season_url:
        concrete = [pu for _n, pu in (list_episode_pages(url) or [])]
        if len(concrete) > 1:
            ui.toast("⚠ 粘贴的是整季入口页（不可直接解析），已自动改用第1集单集页起播",
                     False)
            season_url = url
            url = concrete[0]

    if not url_supported(url):
        ui.toast(f"⚠ {url} 不在 @include 列表内，仍尝试解析")

    cfg = Config(url=url, is_mobile=args.mobile)
    backend = _make_backend(cfg, args.player, ui=ui, **_prefetch_kwargs(args))

    # source 已在 main() 入口统一校验过（越界会直接退出）。交给 start() 在
    # 「索引迁移」之后写入 —— 提前写会被迁移 +1，实测 `-s 2` 会变成 3。
    consumer = start(
        url, ui=ui, backend=backend, page=PageSession(url),
        auto=not args.no_auto, source=source,
    )
    if consumer is None:
        return 2

    # VIP 悬浮框始终可点；自动解析没开（或失败）时就落到手动选择这一步
    if consumer.last_result is None:
        # 默认（没 --no-auto）就直接用默认源自动起播、不弹菜单：auto_play() 只有在
        # GM 里存过 autoPlayerKey（此前 -s 跑过或开过自动解析）才会自动播，全新粘 URL
        # 没这标记，会一路落到这里 —— 补上「自动用默认源(或 -s 指定源)起播」，正是用户
        # 期望的"默认1号还选什么"。--no-auto 才保留可点菜单（其语义本就是只出菜单不自动播）。
        # --no-interact 同理：没人回答，必须给默认值。原先这里硬编码 0，等于把 -s 又忽略一次。
        default = None
        if args.no_interact or not args.no_auto:
            default = source if source is not None else DEFAULT_SOURCE_INDEX
        if not choose_source(consumer, ui, default=default):
            consumer.shutdown()
            return 1

    result = consumer.last_result
    # 剧名以**页面**为准（模式二「只认 URL」原则的延伸）：--title 不再抢在前面，
    # 只在页面实在认不出时当显示名兜底。第三方源解析的是页面本身 —— 桌面官方页是
    # 反爬空壳、解析页 <title> 又是源名（如「M3U8解析」），所以先走官方数据源
    # fetch_show_name（优酷移动页 showName 等），取不到才退回读页面标题。
    # 无损云自带剧名（hit.name），无需检测，也就不会被 --title 覆盖。
    _is_thirdparty = (consumer.selected_index is not None
                      and _source_needs_page_url(consumer.selected_index))
    drama = ""
    if _is_thirdparty:
        drama = fetch_show_name(url)
        if not drama:
            try:
                drama = wait_stable_title(consumer.page, max_wait=3000) or ""
            except Exception:
                drama = ""
    if not drama:
        drama = args.title or ""
    # 第三方内嵌源只解析得出当前这一页（1 集）——据官方整季页补成「共 N 集」，
    # 让 n/p / 数字 / l 在第三方源下也能换集（换集=加载另一条解析页）。
    _enrich_thirdparty_episodes(result, consumer, url, season_url or url, ui, drama=drama)

    if args.episode > 0 and result is not None and len(result.episodes) > 1:
        idx = min(args.episode - 1, len(result.episodes) - 1)
        ep = result.episodes[idx]
        result.index = idx
        result.backend.play(ep["url"], title=result.hit.get("name", ""))
        ui.toast(f"✓ 已切到 {ep.get('name') or ('第' + str(idx + 1) + '集')}")

    if result is None:
        ui.toast("✗ 未能起播，可用 --title 直接按片名搜索")
        consumer.shutdown()
        return 2

    if not args.no_interact:
        interactive(consumer, result, ui, consumer.cfg, save_opts=_save_opts(args),
                    site=args.site, season_url=season_url or url)

    consumer.shutdown(stop_player=not args.no_interact)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# 无参数启动：进程序后再交互式选「播放方式 / 片名或URL / 解析源 / 官方站 / 集数」
# ─────────────────────────────────────────────────────────────────────────────
def _setup_menu(ui: Ui) -> None:
    ui._print("  选择播放方式：", style="bold color(117)")
    ui._print("    t   按片名搜索（无损云直连 / 站内搜索官方页给第三方源）", style="color(110)")
    ui._print("    u   直接粘贴视频页 URL（第三方解析源解析的是具体页面）", style="color(110)")
    ui._print("    q   退出", style="color(110)")


def _pick_source_index(ui: Ui) -> int:
    """列出全部解析源、读一个编号（回车/非法都回退默认源）。不依赖 consumer。"""
    ui._print("  选择解析源：", style="bold color(117)")
    for i, src in enumerate(VIDEO_PARSE_LIST):
        tag = "（资源型·按片名搜索，无需 URL）" if src.kind == RESOURCE else ""
        if i == DEFAULT_SOURCE_INDEX:
            tag += "（默认）"
        ui._print(f"    {i}  {src.name}{tag}", style="color(110)")
    _def_name = VIDEO_PARSE_LIST[DEFAULT_SOURCE_INDEX].name
    while True:
        try:
            raw = ui._input(
                f"  解析源编号（回车={DEFAULT_SOURCE_INDEX} {_def_name}）> ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return DEFAULT_SOURCE_INDEX
        if not raw or raw.lower() in ("q", "quit", "exit"):
            return DEFAULT_SOURCE_INDEX
        if raw.isdigit() and 0 <= int(raw) < len(VIDEO_PARSE_LIST):
            return int(raw)
        ui.toast("无效编号，请重试")


def _match_site(text: str) -> Optional[str]:
    """官方站名容错匹配 → 标准站名（认不出返回 None）。

    `_pick_site` 的交互输入与 `m 片名 site X` 的一行式参数**共用这一份判据**，
    否则两处各写一条匹配规则迟早口径漂移。简称要展开：
    腾讯→腾讯视频、芒果→芒果TV。
    """
    t = (text or "").strip()
    if not t:
        return None
    for name in _SITES_CN:
        if t == name or t in name or name.startswith(t):
            return name
    return None


def _split_site_arg(arg: str) -> tuple:
    """`m` 的一行式参数 `片名 [site 官方站]` → (片名, 标准站名, 错误文案)。

    `site` 一律写在**末尾**，切分时取最后一个 ` site ` 段：片名里真含 "site"
    也能靠最后一段识别（`m A site B site 爱奇艺` → 片名「A site B」+ 爱奇艺）。
    连续空格会被压成一个（`"庆余年  第二季"` → `"庆余年 第二季"`），对搜索是
    等价甚至更好的写法，故不做特殊保护。

    返回的站名可能是 `None`：①压根没写 site（正常，沿用上次选择）；②写了但认不出
    （错误文案非空，调用方必须拒绝执行，不能假装生效）。
    """
    parts = arg.split()
    if "site" not in parts:
        return " ".join(parts).strip(), None, ""
    i = len(parts) - 1 - parts[::-1].index("site")      # 最后一个 site
    title = " ".join(parts[:i]).strip()
    tail = parts[i + 1:]
    if not tail:
        return title, None, ("✗ site 后面要跟一家官方站：\n"
                             "    腾讯 / 爱奇艺 / 优酷 / 芒果")
    if len(tail) > 1:
        return title, None, (f"✗ site 只能跟一家（收到「{' '.join(tail)}」）：\n"
                             f"    多出来的那几个字是片名的一部分吗？"
                             f"那就把它们写在 site 之前")
    hit = _match_site(tail[0])
    if hit is None:
        return title, None, (f"✗ 认不出官方站「{tail[0]}」，可用：\n"
                             f"    腾讯 / 爱奇艺 / 优酷 / 芒果")
    return title, hit, ""


def _match_source(text: str) -> List[int]:
    """`s` 的一行式参数 → 命中的解析源编号列表（可能 0 / 1 / 多个）。

    纯数字当编号用；否则按源名匹配，**前缀命中唯一就直接采用**（`麒麟`→4、
    `M3U8`→7 而非 8），前缀不唯一或没命中再退回包含匹配，交给调用方报歧义
    （`解析` 会命中全部 9 家，这种输入猜哪一家都不诚实）。
    """
    t = (text or "").strip()
    if not t:
        return []
    if t.isdigit():
        return [int(t)] if 0 <= int(t) < len(VIDEO_PARSE_LIST) else []
    low = t.lower()
    pref = [i for i, s in enumerate(VIDEO_PARSE_LIST) if s.name.lower().startswith(low)]
    if len(pref) == 1:
        return pref
    return pref or [i for i, s in enumerate(VIDEO_PARSE_LIST) if low in s.name.lower()]


def _resolve_source_arg(arg: str) -> tuple:
    """`s` 的一行式参数 → (是否弹窗, 编号, 错误文案)；编号为 None 表示没解析成。

    弹窗写法沿用 `_source_menu` 里的 `p+编号`（`s p4` / `s p 4`）。只有编号能弹窗
    —— 源名先匹配再判弹窗会让 `s playm3u8` 那种名字里的 p 被当成前缀。
    """
    t = arg.strip()
    popup = False
    head = t
    if t[:1].lower() == "p" and t[1:].lstrip().isdigit():
        popup, head = True, t[1:].lstrip()
    elif t.split()[:1] == ["p"] and len(t.split()) == 2 and t.split()[1].isdigit():
        popup, head = True, t.split()[1]
    hits = _match_source(head)
    if not hits:
        if head.isdigit():
            return popup, None, (f"✗ 没有编号为 {head} 的解析源"
                                 f"（可用编号 0-{len(VIDEO_PARSE_LIST) - 1}，见 --list）")
        return popup, None, (f"✗ 认不出解析源「{arg}」，可用：\n    "
                             + _all_source_names())
    if len(hits) > 1:
        return popup, None, (f"⚠ 「{head}」匹配到多个解析源：\n    "
                             + "、".join(f"{i} {VIDEO_PARSE_LIST[i].name}" for i in hits)
                             + "\n    请改用编号，如：s "
                             + ("p" if popup else "") + str(hits[0]))
    return popup, hits[0], ""


def _all_source_names() -> str:
    return "、".join(f"{i} {s.name}" for i, s in enumerate(VIDEO_PARSE_LIST))


def _pick_site(ui: Ui, keep: Optional[str] = None) -> Optional[str]:
    """第三方源 + 片名时，问优先去哪家官方站找页面。

    `keep` 是上一次的选择（交互循环里 `m` 换片名会传进来）：给了它就**回车沿用**，
    没给则回车=四家依次尝试（腾讯→爱奇艺→优酷→芒果TV）。
    """
    ui._print("  到哪家官方站找该片的视频页？", style="bold color(117)")
    if keep:
        ui._print(f"    回车=沿用上次的 {keep}；\n"
                  f"    或输入 腾讯 / 爱奇艺 / 优酷 / 芒果 换一家", style="dim")
    else:
        ui._print("    回车=四家依次尝试（腾讯→爱奇艺→优酷→芒果TV）；\n"
                  "    或输入 腾讯 / 爱奇艺 / 优酷 / 芒果",
                  style="dim")
    while True:
        try:
            raw = ui._input("  官方站 > ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return keep
        if not raw or raw.lower() in ("q", "quit", "exit"):
            return keep               # 没改变主意：沿用上次（从未选过则四家依次）
        hit = _match_site(raw)
        if hit is not None:
            return hit
        ui.toast("请输入 腾讯 / 爱奇艺 / 优酷 / 芒果，或回车用默认")


def bootstrap(ui: Ui, args: argparse.Namespace) -> bool:
    """命令行不带来源参数时的交互引导。就地把 args.title/url/source/site/episode 填好。

    返回 True 表示已填好可继续播放；False 表示用户放弃（main 据此正常退出）。
    填完后 main 走的是与「带参数启动」完全相同的模式一 / 模式二逻辑。
    """
    _setup_menu(ui)
    choice = ""
    while choice not in ("t", "title", "u", "url"):
        try:
            choice = ui._input("设置> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return False
        if choice in ("q", "quit", "exit"):
            return False
        if choice not in ("t", "title", "u", "url"):
            ui.toast("请输入 t（按片名）、u（粘贴 URL），或 q 退出")

    if choice in ("t", "title"):
        try:
            title = ui._input("  片名 > ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return False
        if not title:
            ui.toast("片名为空，已退出")
            return False
        args.title = title
        try:
            ep = ui._input("  第几集（回车=整季/自动）> ").strip()
        except (EOFError, KeyboardInterrupt):
            ep = ""
        if ep.isdigit():
            args.episode = int(ep)
    else:
        try:
            url = ui._input("  视频页 URL > ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return False
        if not looks_like_url(url):
            ui.toast(f"✗ {url!r} 不像 URL（要带 http:// 或域名）。已退出")
            return False
        args.url = url

    args.source = _pick_source_index(ui)
    if args.title and _source_needs_page_url(args.source):
        args.site = _pick_site(ui)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = normalize_argv(list(argv), parser)      # -title → --title（笔误纠正）
    args = parser.parse_args(argv)
    ui = Ui(quiet=args.quiet)

    if args.version:
        ui._print("video_vip 1.0")
        return 0

    probe_cfg = Config(url=args.url or "https://wsyzy.cc/", is_mobile=args.mobile)
    if args.list:
        print_sources(ui, probe_cfg)
        return 0

    if not args.quiet:
        ui._print(BANNER, style="color(117)")
        ui.start_block("boot")      # banner 自成一块：下面第一条进度前会空一行

    # 不带来源参数不再直接打帮助退出，而是进程序后交互式选播放方式/片名或URL/解析源
    if not args.url and not args.title:
        if not bootstrap(ui, args):
            return 0

    # 位置参数被当成 URL，但它其实不像 URL（多半是漏写了 --title 的片名）
    if args.url:
        err = validate_url_arg(args.url)
        if err:
            ui.toast(err)
            return 1

    # --source 编号先校验一次：所有路径都要用，越界就该立刻报错退出，
    # 而不是各自默默忽略（原先只有「只给 URL」那条路径读它，其余直接无视）。
    # 放在 --save 之前：`--save -s 99` 同样该报错，而不是静默存了个东西。
    source: Optional[int] = None
    if args.source is not None:
        source = _validate_source(args.source, ui)
        if source is None:
            return 1

    # ── 模式零：--save 只保存不播放 ─────────────────────────────────────────
    if args.save:
        # 取流两条路：默认资源型源按片名直连；显式 -s 采集型源则「视频页 → 解析页抽明文 m3u8」。
        # 后者依赖 jx_resolve 已逆通的接口，没接入的源会如实报错（列出可用编号），
        # 不会静默换成无损云 —— 用户明确选了源，就不能假装它生效了。
        if source is not None and _source_needs_page_url(source):
            ui.toast(
                f"ℹ --save 走「{VIDEO_PARSE_LIST[source].name}」：\n"
                f"    视频页 → 解析页抽明文 m3u8 → 并发分片下载\n"
                f"    （该源没接入下载接口时会直接报错，可 "
                f"-s {DEFAULT_RESOURCE_INDEX} 走"
                f"{VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX].name}直连）"
            )
        return _run_save(args, ui, source)

    # ── 模式一：只给片名 ────────────────────────────────────────────────────
    if not args.url:
        # 只给片名时，采集型源本来用不了：它们解析的是「一个具体视频页」
        # （`source.url + 页面URL`），而这里没有页面 URL。但可以先「片名 → 官方页
        # URL」（site_search：四家各自的原生接口/片库目录），再交给完整注入流程，
        # 于是 `--title … -s 2` 也能走第三方源。搜不到才退回资源型源并说明原因。
        # 未显式 -s 时用默认源（config.DEFAULT_SOURCE_INDEX，当前 = LX解析），
        # 与交互菜单/auto 解析的默认保持一致；默认若是资源型源，下面判据为假，直接片名搜索。
        eff = source if source is not None else DEFAULT_SOURCE_INDEX
        if _source_needs_page_url(eff):
            hit = find_page_url(
                args.title, episode=args.episode, prefer=args.site,
                on_note=lambda t: ui.toast(t, False),
            )
            if hit is not None:
                ui.toast(
                    f"✓ 按片名找到{hit.label()}视频页：\n    {hit.url}", False
                )
                if hit.episode > 0 and not hit.episode_pinned:
                    # 四家均可枚举精确到集；走到这里说明枚举失败（网络异常/超总集数），
                    # 只能打开整季页 —— 如实告知，绝不说「已定位第N集」。
                    ui.toast(
                        f"⚠ 未能定位第{hit.episode}集（{hit.site} 枚举异常或集数超出范围）：\n"
                        f"    已打开整季页，请在选集栏手动点第{hit.episode}集；\n"
                        f"    或换一条路径：\n"
                        f"      python -m video_vip --title {args.title} "
                        f"--episode {hit.episode} -s {DEFAULT_RESOURCE_INDEX}   "
                        f"# {VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX].name} 按片名取整季"
                    )
                ui.toast(
                    f"正在用「{VIDEO_PARSE_LIST[eff].name}」解析该页...", False
                )
                return _play_page_url(args, ui, hit.url, eff,
                                      season_url=hit.season_url)
            wanted = VIDEO_PARSE_LIST[eff].name
            # 文案按行手写折行，别指望 rich：它按**显示宽度**算（中日韩字符
            # 占 2 列），一行 40 个汉字就是 80 列、超过默认 79 列终端宽度。
            # 超宽时 rich 会在 `⚠` 后的空格处断行，把 emoji 甩成单独一行，
            # 看着像消息丢了。根因已在 ui.toast 用 soft_wrap=True 修掉
            # （交给终端硬折行，emoji 不再落单），这里仍保持每行短，
            # 是为了窄终端下也读得顺，不依赖折行位置。
            ui.toast(
                f"⚠ 没搜到「{args.title}」的{wanted}可用视频页：\n"
                f"    采集型解析源解析的是具体视频页，需要页面地址，\n"
                f"    已尝试站内搜索腾讯/爱奇艺/优酷/芒果TV，均未命中。\n"
                f"    可直接带视频页 URL 使用：\n"
                f"      python -m video_vip <视频页URL> -s {eff}\n"
                f"    或指定官方站：\n"
                f"      python -m video_vip --title {args.title} -s {eff} "
                f"--site 爱奇艺\n"
                f"    本次退回「{VIDEO_PARSE_LIST[DEFAULT_RESOURCE_INDEX].name}」"
                f"（-s {DEFAULT_RESOURCE_INDEX}）播放。"
            )
        cfg = Config(url="https://wsyzy.cc/", is_mobile=args.mobile)
        backend = _make_backend(cfg, args.player, ui=ui, **_prefetch_kwargs(args))
        # 资源型分支要认显式 -s（暴风/红牛各库入库时刻不同，指定源是有效意图）；
        # 只有采集型源没官方页、回退到资源型搜索时，才落到默认资源源。
        result = play_by_title(
            args.title,
            episode=args.episode,
            ui=ui,
            backend=backend,
            is_mobile=args.mobile,
            source=eff if VIDEO_PARSE_LIST[eff].kind == RESOURCE
            else DEFAULT_RESOURCE_INDEX,
        )
        if result is None:
            return 2
        if not args.no_interact:
            interactive(None, result, ui, cfg, save_opts=_save_opts(args),
                        site=args.site)
            result.backend.stop()
        return 0

    # ── 模式二：给视频页 URL（完整解析流程）─────────────────────────────
    # 给了 URL 就**只认 URL**：解析、起播、换集全都以那个页面为准，--title 忽略。
    # 旧逻辑是「URL + title → 拿 title 走无损云搜索」，两个毛病：①-s 指定的第三方
    # 源永远用不上；②页面里那一季的实际内容被片名搜索结果替换掉，同名不同版本
    # （剧场版/TV版）就会播错片。剧名改由页面决定（见 _play_page_url 里的取值顺序）。
    if args.title:
        eff = source if source is not None else DEFAULT_SOURCE_INDEX
        ui.toast(
            f"ℹ 已给出视频页 URL，--title「{args.title}」忽略：\n"
            f"    解析与起播都以该页面为准，剧名由页面识别；\n"
            f"    想按片名搜索请去掉 URL：\n"
            f"      python -m video_vip --title {args.title} -s {eff}"
        )

    # 统一的「页面 URL → 注入 → 起播 → 交互」流程（第三方源与无损云都走这里）
    return _play_page_url(args, ui, args.url, source)


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
