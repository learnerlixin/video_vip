"""cli.py —— 命令行入口。

    python -m video_vip                        # 不带参数：进程序后交互式选片名/URL/解析源
    python -m video_vip <视频页URL>            # 完整流程：识别片名 → 搜索 → 播放
    python -m video_vip --title 庆余年          # 跳过页面，直接按片名搜索播放
    python -m video_vip <URL> --no-auto        # 只出解析源菜单，不自动播放
    python -m video_vip --list                 # 列出所有解析源
    python -m video_vip --title 庆余年 -s 1 --site 优酷   # 站内搜索优酷页交给第三方源

播放起来之后进入交互循环：
    n / p        下一集 / 上一集
    <数字>       跳到第 N 集
    l            列出全部集数
    m            重新选择影片（换片名，沿用当前解析源）
    s            切换解析源（沿用当前片名）
    r            重新加载当前集（reloadCurrentPlayer）
    a            开/关自动解析
    x            开/关自动连播
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

from .autonext import AutoNext
from .config import VIDEO_PARSE_LIST, Config, DEFAULT_SOURCE_INDEX, url_supported
from .consumer import Consumer, start
from .download import DownloadError, probe_duration, save_as_mp4
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
    parser.add_argument("-t", "--title", help="跳过页面抓取，直接用这个片名搜索")
    parser.add_argument("-e", "--episode", type=int, default=0, help="指定播放第几集（从 1 开始）")
    parser.add_argument(
        "-s", "--source", type=int, default=None,
        help="指定解析源编号（见 --list），不指定则用默认源（当前 = LX解析）。"
             "第三方源解析的是具体视频页：带视频页 URL 时直接拼解析；"
             "只给 --title 时会先站内搜索腾讯/爱奇艺/优酷定位官方页再喂给它，"
             "搜不到自动回退无损云（-s 0）",
    )
    parser.add_argument(
        "--site",
        choices=["腾讯视频", "爱奇艺", "优酷", "芒果TV"],
        default=None,
        help="配合 --title + 第三方源：限定去哪家官方站找视频页 URL。"
             "不指定则依次尝试（腾讯→爱奇艺→优酷→芒果TV）",
    )
    parser.add_argument(
        "-p",
        "--player",
        choices=["auto", "prefetch", "mpv", "ffplay", "embed", "browser"],
        default="embed",
        help="播放后端：embed=生成内嵌播放页并用浏览器打开（默认，本地服务 + hls.js "
             "直接播 m3u8，支持 --auto-next 自动连播）；"
             "auto=自动探测本地播放器（mpv → ffplay → embed）；"
             "prefetch=并发预取后本地播放（弱网/卡顿时用）。"
             "本地播放器（mpv/ffplay/prefetch）与 embed 均支持自动连播；"
             "browser 与 embed 回退到第三方解析页时不支持",
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
        help="不播放，直接把视频保存为 mp4（并发下载分片 + ffmpeg 封装，需要本机有 ffmpeg）",
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
    parser.add_argument(
        "--auto-next",
        action="store_true",
        help="一集播完自动切下一集（交互循环里用 x 可随时开/关）",
    )
    parser.add_argument("--list", action="store_true", help="列出解析源与可用播放后端后退出")
    parser.add_argument("--mobile", action="store_true", help="按移动端 UA/布局处理")
    parser.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    parser.add_argument("--version", action="store_true", help="打印版本信息后退出")
    return parser


def print_sources(ui: Ui, cfg: Optional[Config] = None) -> None:
    """把 videoParseList 按两组（内嵌 / 弹窗）打印出来。"""
    ui._print("[内嵌播放]", style="bold color(117)")
    for index, item in enumerate(VIDEO_PARSE_LIST):
        if item.wsyzy or "1" in item.type:
            tag = "（屏蔽欧美、欧洲线路）" if item.wsyzy else ""
            ui._print(f"  {index}  {item.name}{tag}", style="color(110)")
    ui._print("[弹窗播放不带选集]", style="bold color(117)")
    for index, item in enumerate(VIDEO_PARSE_LIST):
        if not item.wsyzy and "3" in item.type:
            ui._print(f"  {index}  {item.name}  {item.url}", style="color(110)")
    if cfg is not None:
        ui._print("[播放后端]", style="bold color(117)")
        for key, backend in available_backends(cfg).items():
            ui._print(f"  {key:8s} {backend.describe()}", style="color(110)")
    ui._print("[缓存目录]", style="bold color(117)")
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
    """这个解析源是不是「必须有视频页 URL」才能用。

    第三方源（LX/七哥/TXNQ/789/冰豆/麒麟）的工作方式是
    `source.url + 页面URL`（见 consumer.show_player_window）—— 它们解析的是
    **一个具体视频页**，不是片名。只有无损云（wsyzy=True）是按片名搜索的，
    所以只有它能在「只给 --title、没有视频页」的情况下工作。
    """
    return not VIDEO_PARSE_LIST[index].wsyzy


def _make_backend(cfg: Config, prefer: str, ui: Optional[Ui] = None, **prefetch_kwargs: Any) -> Any:
    """按 --player 挑后端；prefetch 的并发参数与进度回调在这里注入。

    里程碑消息走 ui.toast（一次一行），高频的每片推进走 ui.progress 进度条
    （原地刷新）—— 否则上千片会刷上千行，终端根本没法看。
    """
    if ui is not None and prefetch_kwargs.get("on_progress") is None:
        prefetch_kwargs["on_progress"] = lambda text: ui.toast(text, False)
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
# 直接按片名搜索（--title）：跳过页面，走 wsyzyDirect 的后半段
# ─────────────────────────────────────────────────────────────────────────────
def play_by_title(
    title: str,
    episode: int = 0,
    ui: Optional[Ui] = None,
    backend: Optional[Any] = None,
    is_mobile: bool = False,
    no_play: bool = False,
) -> Optional[Any]:
    """不依赖视频页：`searchWithRetry → getEpisodes → load`。

    相当于 wsyzyDirect.play() 去掉「接管容器」和「等片名稳定」两步。
    `no_play=True` 时只解析出选集与目标集，不起播（--save 用：要的是地址不是播放）。
    """
    from .wsyzy import PlayResult

    ui = ui or Ui()
    cfg = Config(url="https://wsyzy.cc/", is_mobile=is_mobile)
    page = PageSession(cfg.href)
    direct = WsyzyDirect(cfg, page, ui, backend)

    # 先用页面标题原样搜索，只有搜不到时才退回 wsyzyCleanTitle 的结果
    # （searchWithRetry 内部已实现这个顺序），所以这里不能提前清洗——
    # 提前清洗会把「庆余年第二季」削成「庆余年」，丢掉精确命中的机会。
    ui.set_status(f"「{title}」无损云搜索中...")
    try:
        candidates = direct.search_with_retry(title)
    except WsyzyError as exc:
        ui.toast(f"✗ 搜索失败：{exc}")
        return None
    if not candidates:
        ui.toast(f"✗ 无损云未收录「{title}」")
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
    ui.toast(f"✓ 无损云播放中（{player.describe()}）")

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

    `headers` 给第三方抽出的 m3u8 带自定义 UA/Referer（无损云直连留空走默认头）。

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
            on_progress=lambda text: ui.toast(text, False),
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


def _do_save(result: Any, ui: Ui, opts: Dict[str, Any]) -> bool:
    """保存交互循环里当前这一集（`d` 指令）。

    无损云的每集 url 就是 m3u8 直链，直接下；第三方源的每集 url 是 `源前缀+官方页`
    的解析页（HTML、非流），先用 jx_resolve 抽出真·m3u8 再下；抽不出（未逆通的接口）
    就如实提示改用无损云，绝不臆造地址。
    """
    ep = result.episode()
    url = ep.get("url") or ""
    if not url:
        ui.toast("✗ 当前集没有可用的播放地址")
        return False
    title = result.hit.get("name", "") if result.hit else ""
    headers = None
    if not is_media_url(url):                       # 第三方解析页：抽流出真 m3u8
        if not jx_resolve.is_supported(url):        # 未接入下载的源：直接报错
            ui.toast("✗ 该第三方源尚未接入下载接口（不支持），请用 `m` 换无损云（-s 0）再 d",
                     False)
            return False
        info = jx_resolve.resolve(url)              # 已支持：抽流，失败多为接口抖动
        if info is None:
            ui.toast("✗ 该源本次抽流失败（接口无响应/换源），可重试或 `m` 换无损云（-s 0）再 d",
                     False)
            return False
        link = info.m3u8 if len(info.m3u8) <= 90 else info.m3u8[:87] + "..."
        ui.toast(f"⬇ 第三方源已抽到 m3u8 直链，转下载：{link}", False)
        url = info.m3u8
        headers = info.headers()
    return _save_one(url, title, ep.get("name", ""), ui, opts, headers=headers)


def _run_save(args: argparse.Namespace, ui: Ui) -> int:
    """--save：解析出目标地址后直接保存，不播放、不进交互循环。"""
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

    # 2) 给的是片名（可能还带视频页 URL）→ 走无损云搜索拿选集
    if args.title:
        result = play_by_title(
            args.title,
            episode=args.episode,
            ui=ui,
            backend=None,
            is_mobile=args.mobile,
            no_play=True,
        )
        if result is None:
            return 2
        ok = _do_save(result, ui, opts)
        return 0 if ok else 2

    # 3) 只给了视频页 URL → 从页面读出片名，再走无损云搜索（不起播）
    if args.url:
        ui.set_status("读取页面片名...")
        page = PageSession(args.url)
        try:
            page.load()
        except Exception as exc:
            return fail(f"✗ 页面加载失败（{type(exc).__name__}）：{exc}")
        try:
            page_title = wait_stable_title(page)
        except KeyboardInterrupt:
            ui.toast("✗ 已取消")
            return 1
        except Exception:
            page_title = ""
            ui.toast("⚠ 读取片名失败，将按空片名处理")
        if not page_title or len(page_title) < 2:
            return fail("✗ 无法从页面识别片名，请用 --title 指定")

        ui.toast(f"识别到片名「{page_title}」")
        result = play_by_title(
            page_title,
            episode=args.episode,
            ui=ui,
            backend=None,
            is_mobile=args.mobile,
            no_play=True,
        )
        if result is None:
            return 2
        ok = _do_save(result, ui, opts)
        return 0 if ok else 2

    ui.toast("✗ --save 需要指定片名（--title）或视频地址")
    print("video_vip: --save 需要指定片名（--title）或视频地址", file=sys.stderr)
    return 1


def interactive(
    consumer: Optional[Consumer],
    result: Any,
    ui: Ui,
    cfg: Config,
    save_opts: Optional[Dict[str, Any]] = None,
    auto_next: bool = False,
    site: Optional[str] = None,
) -> None:
    """播放后的命令循环（换集 / 换源 / 换片 / 保存 / 连播）。

    `auto_next=True` 时一集播完自动切下一集；循环里随时可用 `x` 开/关。

    `m`（换片名）与 `s`（换解析源）走**统一的 relaunch**：记住当前片名与当前源，
    任一方改动都用「新片名 + 新源」重新起播、回到第 1 集。无损云按片名直连搜索；
    第三方源解析的是具体视频页，于是借道 `find_page_url`（站内搜腾讯/爱奇艺/优酷）
    先定位该片名的官方页再喂给所选源，找不到就如实回退无损云。`site` 是限定优先站。
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

    # 换集动作可能来自两个线程：主循环（用户敲 n/p/数字）和自动连播的监视线程。
    # 两边都会改 result.index 并调 backend.play()，必须串行化，
    # 否则会出现「两边各切一次、后一次把前一次覆盖掉」的竞态。
    switch_lock = threading.RLock()

    # 自动连播的「耗时够不够长」判定需要知道这一集应该播多久。
    # prefetch 后端能从 HLS 清单里直接拿到；普通 mpv/ffplay 拿不到，
    # 只能用 ffprobe 探一次（实测 m3u8 约 0.3s）。探测是网络请求，
    # 必须放后台线程，否则会卡住交互输入。
    probe_gen = [0]          # 代数：切集就 +1，用来丢弃迟到的旧结果

    def probe_duration_async(url: str) -> None:
        """后台探测这一集的时长并告知后端；结果过期就丢弃。"""
        with switch_lock:
            gen = probe_gen[0]
            backend = result.backend if result is not None else None
        if backend is None:
            return
        # 后端能拿到「真 ended 事件」（embed 自有页面）时，自动连播走的是权威信号，
        # 根本不看时长 —— 再花一次 ffprobe 网络请求去探时长纯属浪费，跳过。
        if callable(getattr(backend, "end_confirmed", None)):
            return
        # 后端自己已经知道时长（prefetch 从清单里读到了）就不用再探
        try:
            if backend.expected_duration():
                return
        except Exception:
            pass
        setter = getattr(backend, "set_expected_duration", None)
        if not callable(setter):
            return

        def _work() -> None:
            try:
                dur = probe_duration(url, timeout=30.0)
            except Exception:
                return
            if not dur or dur <= 0:
                return
            # 用户可能已经切到别的集了：这时写入会把新集的时长覆盖成旧集的，
            # 自动连播的判定就全错了。代数不符就丢弃。
            with switch_lock:
                if gen != probe_gen[0]:
                    return
            try:
                setter(dur)
            except Exception:
                pass

        threading.Thread(target=_work, name="dur-probe", daemon=True).start()

    def switch_to(target: int) -> bool:
        """换到第 target 集（0-based）。手动指令与自动连播共用这一条路径。"""
        with switch_lock:
            if result is None:
                return False
            eps = result.episodes
            if not eps or not (0 <= target < len(eps)):
                return False
            probe_gen[0] += 1        # 作废旧的探测结果
            result.index = target
            ep = eps[target]
            result.backend.play(ep["url"], title=result.hit.get("name", ""))
            setter = getattr(result.backend, "set_playlist", None)
            if callable(setter):
                setter(eps, target, result.hit.get("name", ""))
            url = ep["url"]
        probe_duration_async(url)
        return True

    def get_state() -> Any:
        with switch_lock:
            if result is None:
                return [], 0
            return result.episodes, result.index

    # 监视线程在后台跑，即使 auto_next=False 也要跑：它得持续跟踪
    # 「在看哪一集、从何时开始看」，这样用户中途按 x 打开时计时是准的。
    monitor = AutoNext(
        backend=result.backend if result is not None else None,
        get_state=get_state,
        advance=switch_to,
        notify=lambda text: ui.toast(text, False),
        enabled=auto_next,
    )
    monitor.start()
    if auto_next:
        ui.toast(monitor.status_text(), False)

    # ── 统一的「换片名 / 换源」重起点（relaunch）────────────────────────────
    # 记住当前片名与当前源：m 改片名、s 改源都调 relaunch，用「新片名 + 当前源」
    # 重新起播并回到第 1 集。这样 m 不会再把第三方源重置回无损云。
    state = {
        "src": (consumer.selected_index if consumer is not None
                and consumer.selected_index is not None else 0),
        "title": (result.hit.get("name", "") if result is not None else ""),
    }

    def _search_ws(title: str) -> Optional[Any]:
        """无损云按片名起播，复用当前后端（不新建，避免动到 embed 本地服务）。"""
        with switch_lock:
            backend = result.backend if result is not None else None
        return play_by_title(title, episode=0, ui=ui,
                             backend=backend, is_mobile=cfg.isMobile)

    def _search_page(title: str, src: int) -> Optional[Any]:
        """第三方源：片名 → 官方页 URL（站内搜索）→ start 解析该页并选 src。

        返回 (new_consumer, new_result)；页面找不到或解析失败返回 None（调用方回退）。
        """
        hit = find_page_url(title, episode=0, prefer=site,
                            on_note=lambda t: ui.toast(t, False))
        if hit is None:
            return None
        ui.toast(f"✓ 找到{hit.label()}视频页：\n    {hit.url}", False)
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
        return new_consumer, new_consumer.last_result

    def relaunch(title: str, src: int) -> bool:
        """用 (title, src) 重新起播并回到第 1 集；成功返回 True。

        consumer 生命周期：换片名/换源都重建一份，旧的 shutdown(stop_player=False)
        —— 保留后端进程/本地服务复用，只是不再管它。无损云会话则 consumer=None。
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
            new_result = _search_ws(title)          # 无损云：按片名直连
        else:
            got = _search_page(title, src)
            if got is not None:
                consumer, new_result = got
            else:
                ui.toast(f"⚠ 没搜到「{title}」的可解析视频页，"
                         f"{VIDEO_PARSE_LIST[src].name} 用不上；已回退无损云。", False)
                new_result = _search_ws(title)
                applied_src = 0
        if new_result is None:
            ui.toast(f"✗ 「{title}」未能起播，保持原影片不变")
            return False

        with switch_lock:
            probe_gen[0] += 1                        # 作废旧地址的时长探测
        result = new_result
        state["title"] = title
        state["src"] = applied_src
        monitor.set_backend(result.backend)
        try:
            result.index = 0                         # 回到第 1 集
        except Exception:
            pass
        ui.ep_bar(result.episodes, result.index,
                  lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                  title=result.title)
        probe_duration_async(result.episode()["url"])
        return True

    def switch_source(src: int) -> bool:
        """s 换源：**同一影片换个解析接口**——复用当前官方页 URL，直接用新源重解析，
        绝不按片名重搜（换源本就该是同一页；重搜依赖剧名准确，而第三方源的 hit.name
        在优酷移动页偶发取空时会退回解析页标题=源名，于是会去搜「M3U8解析」而失败）。

        只有两种情况退回按片名重跑 relaunch：①当前是无损云会话（没有页面 URL 可复用）；
        ②目标是**无损云**（它本就按片名搜索、没有可复用的解析页）。"""
        nonlocal result, consumer
        page_url = (consumer.page.url if consumer is not None else "") or ""
        if not page_url or not _source_needs_page_url(src):
            return relaunch(state["title"], src)
        # 关掉旧会话（shutdown(stop_player=False) 保留后端进程/本地服务复用）
        if consumer is not None:
            try:
                consumer.shutdown(stop_player=False)
            except Exception:
                pass
            consumer = None
        with switch_lock:
            backend = result.backend if result is not None else None
        new_consumer = start(page_url, ui=ui, backend=backend,
                             page=PageSession(page_url), auto=True, source=src)
        if new_consumer is None or new_consumer.last_result is None:
            if new_consumer is not None:
                try:
                    new_consumer.shutdown(stop_player=False)
                except Exception:
                    pass
            ui.toast(f"✗ 没能用「{VIDEO_PARSE_LIST[src].name}」解析当前影片，保持不变")
            return False
        # 补成整季选集，剧名沿用当前 state["title"]（复用同一页，不换片）
        _enrich_thirdparty_episodes(new_consumer.last_result, new_consumer,
                                     page_url, page_url, ui, drama=state["title"])
        consumer = new_consumer
        result = new_consumer.last_result
        state["src"] = src
        with switch_lock:
            probe_gen[0] += 1                        # 作废旧地址的时长探测
        monitor.set_backend(result.backend)
        try:
            result.index = 0                         # 回到第 1 集
        except Exception:
            pass
        ui.ep_bar(result.episodes, result.index,
                  lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                  title=result.title)
        probe_duration_async(result.episode()["url"])
        ui.toast(f"✓ 已用「{VIDEO_PARSE_LIST[src].name}」解析同一影片", False)
        return True

    def _source_menu() -> Optional[Any]:
        """列出全部解析源，返回 (index, popup)；q 返回 None。不依赖 consumer。

        一份列表：编号选内嵌播放，`p+编号`选弹窗播放（不带选集，需第三方源）。
        """
        ui._print("选择解析源（编号=内嵌，p+编号=弹窗不带选集；回车=无损云，q 取消）：",
                  style="bold color(117)")
        for i, src in enumerate(VIDEO_PARSE_LIST):
            if src.wsyzy:
                ui._print(f"  {i}  {src.name}（按片名搜索，无需 URL）", style="color(110)")
            else:
                ui._print(f"  {i}  {src.name}  {src.url}", style="color(110)")
        while True:
            try:
                raw = ui._input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                ui._print("")
                return None
            if not raw:
                return (0, False)
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
            if popup and VIDEO_PARSE_LIST[index].wsyzy:
                ui.toast("✗ 无损云按片名搜索，没有可弹窗的解析页；请用编号选它")
                continue
            return (index, popup)

    # embed 后端的播放页在浏览器里，用户在页面选集栏点了某一集、或页面回退到
    # 第三方 iframe / 自动播放被拦时，这些事件是通过本地 HTTP 服务回传的。
    # 主循环阻塞在 ui._input() 上收不到，所以起一个后台线程轮询：
    #   * take_picked() → 走 switch_to()（和终端里敲数字同一条路径，集号/计时才同步）
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
                monitor.note_manual_switch()
                try:
                    ep = result.episodes[idx]
                    ui.toast("✓ 播放页选集："
                             f"{ep.get('name') or ('第' + str(idx + 1) + '集')}", False)
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
            ui.toast(f"✓ 播放页已在浏览器打开{('：' + page_url) if page_url else ''}", False)
        else:
            ui.toast("⚠ 浏览器没能打开播放页（没有默认浏览器？）。"
                     "可改用 --player mpv / ffplay 在本地播放", False)

    threading.Thread(target=embed_open_watch, name="embed-open", daemon=True).start()

    # 进循环前当前这一集已经在播了（play_by_title 起播的），
    # 它没经过 switch_to()，所以要单独补一次时长探测。
    if result is not None:
        try:
            probe_duration_async(result.episode()["url"])
        except Exception:
            pass

    try:
        while True:
            try:
                raw = ui._input("video_vip> ").strip()
            except (EOFError, KeyboardInterrupt):
                ui._print("")
                break
            if not raw:
                continue
            cmd = raw.lower()

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

            if cmd == "x":
                monitor.toggle()
                ui.toast(monitor.status_text(), False)
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
                # 重新加载等于这一集从头再播，计时必须归零，
                # 否则监视器会拿「重载前已经看了多久」去判这一集。
                monitor.note_manual_switch()
                # play() 会清掉预期时长（它属于上一个 URL），这里补探一次
                if result is not None:
                    probe_duration_async(result.episode()["url"])
                continue

            if cmd == "s" and result is not None:
                sel = _source_menu()
                if sel is None:
                    continue
                index, popup = sel
                if popup:
                    # 弹窗播放不带选集：需要一个视频页。无损云会话没有页，先内嵌
                    # 重跑建立页面会话，再在这个页上弹窗。
                    if consumer is None:
                        relaunch(state["title"], index)
                        if consumer is None:
                            continue            # 回退成无损云了，没有可弹窗的页
                    _dispatch(consumer, ui, index, popup=True)
                    continue
                switch_source(index)
                continue

            if cmd == "m" and result is not None:
                ui.toast("重新选择影片：请输入新的片名")
                try:
                    new_title = ui._input("片名 > ").strip()
                except (EOFError, KeyboardInterrupt):
                    continue
                if new_title:
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
            # 手动换集后立刻重置计时（不等下一次轮询），
            # 并清掉可能还挂着的「等自动切集落地」门闩。
            monitor.note_manual_switch()
            ep = eps[target]
            ui.toast(f"✓ 正在播放 {ep.get('name') or ('第' + str(target + 1) + '集')}")
            ui.ep_bar(eps, target, lambda e: result.backend.play(e["url"], title=result.hit.get("name", "")),
                      title=result.title)
    finally:
        # 先停 embed 轮询线程，再停监视线程：反过来会让轮询线程在监视线程
        # 已停后还可能触发一次 switch_to。两者都是 daemon，但显式停掉更干净。
        embed_stop.set()
        # 监视线程是 daemon，但显式停掉更干净：
        # 否则退出后它还可能再发一次切集请求，把播放器又拉起来。
        monitor.stop()


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
    # 真实剧名：--title 关键词优先。第三方源解析的是页面本身——桌面官方页是反爬空壳、
    # 解析页 <title> 又是源名（如「M3U8解析」），据此读到的标题不是剧名；故先打官方
    # 数据源 fetch_show_name（优酷移动页 showName）拿真名，取不到才退回读页面标题。
    # 无损云自带剧名，无需检测。
    drama = args.title or ""
    _is_thirdparty = (consumer.selected_index is not None
                      and _source_needs_page_url(consumer.selected_index))
    if not drama and _is_thirdparty:
        drama = fetch_show_name(url)
        if not drama:
            try:
                drama = wait_stable_title(consumer.page, max_wait=3000) or ""
            except Exception:
                drama = ""
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
                    auto_next=args.auto_next, site=args.site)

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
        tag = "（无损云·按片名搜索，无需 URL）" if src.wsyzy else ""
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


def _pick_site(ui: Ui) -> Optional[str]:
    """第三方源 + 片名时，问优先去哪家官方站找页面；回车=三家依次尝试。"""
    ui._print("  到哪家官方站找该片的视频页？", style="bold color(117)")
    ui._print("    回车=三家依次尝试（腾讯→爱奇艺→优酷）；或输入 腾讯 / 爱奇艺 / 优酷",
              style="dim")
    while True:
        try:
            raw = ui._input("  官方站 > ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print("")
            return None
        if not raw or raw.lower() in ("q", "quit", "exit"):
            return None
        for name in ("腾讯视频", "爱奇艺", "优酷"):
            if raw == name or raw in name or name.startswith(raw):
                return name
        ui.toast("请输入 腾讯 / 爱奇艺 / 优酷，或回车用默认")


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

    # 自动连播的监视器活在交互循环里，不进循环就没人盯着「播完了没」。
    # 与其让 --auto-next 静默失效，不如明说一声。
    if args.auto_next and (args.no_interact or args.save):
        ui.toast("⚠ --auto-next 需要交互循环，已忽略"
                 "（去掉 --no-interact/--save 才会生效）")

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
        # --save 走的是无损云搜索取地址，第三方解析源给不出可下载的裸流，
        # 所以 -s 在这里没有意义 —— 说清楚，别让用户以为指定生效了。
        if source is not None and _source_needs_page_url(source):
            ui.toast(
                f"⚠ --save 只支持无损云直连取流，\n"
                f"    「{VIDEO_PARSE_LIST[source].name}」不会生效\n"
                f"    （第三方解析源给不出可下载的裸流）"
            )
        return _run_save(args, ui)

    # ── 模式一：只给片名 ────────────────────────────────────────────────────
    if not args.url:
        # 只给片名时，第三方解析源本来用不了：它们解析的是「一个具体视频页」
        # （`source.url + 页面URL`），而这里没有页面 URL。但可以先「片名 → 官方页
        # URL」（site_search：Bing 站内搜索腾讯/爱奇艺/优酷），再交给完整注入流程，
        # 于是 `--title … -s 2` 也能走第三方源。搜不到才退回无损云并说明原因。
        # 未显式 -s 时用默认源（config.DEFAULT_SOURCE_INDEX，当前 = LX解析），
        # 与交互菜单/auto 解析的默认保持一致；默认是无损云(0) 时下面判据为假，直接片名搜索。
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
                    # 三家均可枚举精确到集；走到这里说明枚举失败（网络异常/超总集数），
                    # 只能打开整季页 —— 如实告知，绝不说「已定位第N集」。
                    ui.toast(
                        f"⚠ 未能定位第{hit.episode}集（{hit.site} 枚举异常或集数超出范围）：\n"
                        f"    已打开整季页，请在选集栏手动点第{hit.episode}集；\n"
                        f"    或换一条路径：\n"
                        f"      python -m video_vip --title {args.title} "
                        f"--episode {hit.episode} -s 0   # 无损云按片名取整季"
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
                f"    第三方解析源解析的是具体视频页，需要页面地址，\n"
                f"    已尝试站内搜索腾讯/爱奇艺/优酷，均未命中。\n"
                f"    可直接带视频页 URL 使用：\n"
                f"      python -m video_vip <视频页URL> -s {eff}\n"
                f"    或指定官方站：\n"
                f"      python -m video_vip --title {args.title} -s {eff} "
                f"--site 爱奇艺\n"
                f"    本次退回「无损云解析」（-s 0）播放。"
            )
        cfg = Config(url="https://wsyzy.cc/", is_mobile=args.mobile)
        backend = _make_backend(cfg, args.player, ui=ui, **_prefetch_kwargs(args))
        result = play_by_title(
            args.title,
            episode=args.episode,
            ui=ui,
            backend=backend,
            is_mobile=args.mobile,
        )
        if result is None:
            return 2
        if not args.no_interact:
            interactive(None, result, ui, cfg, save_opts=_save_opts(args),
                        auto_next=args.auto_next, site=args.site)
            result.backend.stop()
        return 0

    # ── 模式二：给视频页 URL（完整解析流程）─────────────────────────────
    if args.title:
        # URL + 片名：页面只用来判定当前集数与站点容器，片名以用户输入为准。
        # 注意这条路径走的仍是无损云搜索（play_by_title），所以 -s 指定的
        # 第三方源同样用不上 —— 明确告知，别让用户以为生效了。
        if source is not None and _source_needs_page_url(source):
            ui.toast(
                f"⚠ 已指定 --title，走的是无损云按片名搜索，\n"
                f"    「{VIDEO_PARSE_LIST[source].name}」不会生效。\n"
                f"    想用该解析源请去掉 --title：\n"
                f"      python -m video_vip {args.url} -s {source}"
            )
        if not url_supported(args.url):
            ui.toast(f"⚠ {args.url} 不在 @include 列表内，仍尝试解析")
        cfg = Config(url=args.url, is_mobile=args.mobile)
        backend = _make_backend(cfg, args.player, ui=ui, **_prefetch_kwargs(args))
        page = PageSession(args.url)
        page.load()
        result = play_by_title(
            args.title, episode=args.episode, ui=ui, backend=backend, is_mobile=args.mobile
        )
        if result is None:
            return 2
        if not args.no_interact:
            interactive(None, result, ui, cfg, save_opts=_save_opts(args),
                        auto_next=args.auto_next, site=args.site)
            result.backend.stop()
        return 0

    # 只给 URL：交给统一的「页面 URL → 注入 → 起播 → 交互」流程
    return _play_page_url(args, ui, args.url, source)


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
