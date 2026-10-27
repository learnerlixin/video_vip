"""ui.py —— 终端 UI 层（toast / placeholder / askChoice / epBar）。

    toast(msg, autoHide)        →  Ui.toast(msg)
    set_status(t)               →  Ui.set_status(t)
    hide_status()               →  Ui.hide_status()
    ask_choice(candidates, detailMap)  →  Ui.ask_choice(...)
    mountEpBar(...)             →  Ui.ep_bar(...)

有 rich 时用 rich 渲染（彩色/面板/进度条），否则退回纯 print。

输出的两条视觉约定（调用点不必传样式，也不该各传各的）：

    颜色看首符号   ✓ 绿 / ✗ 红 / ⚠ 黄 / ℹ↻⬇ 与进度行蓝灰 —— 由 _style_of 自动判
    分块看来源     同一条链路的连续状态行（搜索 → 命中 → 解析 → 起播 → 选集网格）
                   都是 `flow`，彼此不空行；只有换来源才空一行：后台线程插播 `bg`、
                   独立面板 `section()` / `start_block()`、banner `boot`。
                   面板自带细横线框（_rule）；用户敲下的那行命令回显本身就是分隔线，
                   所以 `_input` 读完一行就把块标记清零，不在它下面再补空行。
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict, List, Optional

try:                                  # rich 是可选依赖，缺失时退化为纯文本
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _HAS_RICH = True
except ImportError:                   # pragma: no cover
    _HAS_RICH = False


def _disp_width(s: str) -> int:
    """字符串在终端里的显示列宽：CJK / 全角 / emoji 计 2，其余计 1。

    优先用 rich 的 cell_len（与 rich 渲染口径一致，避免网格错位），
    没有 rich 时退化到 east_asian_width 手算。
    """
    if _HAS_RICH:
        try:
            from rich.cell_len import cell_len
            return cell_len(s)
        except Exception:              # pragma: no cover
            pass
    import unicodedata
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _ep_short(ep: Dict[str, str], idx: int) -> str:
    """把选集条目的标题压成网格里的短标签。

    「庆余年 第17集」/「第17集」这类除集号外只剩片名、无额外信息的 → 返回空
    （网格里纯编号显示，最紧凑）。综艺等有真实小标题的（「03 嘉宾XXX」）原样保留。
    """
    import re as _re
    name = (ep.get("name") or "").strip()
    if not name:
        return ""
    m = _re.search(r"第\s*\d+\s*集", name)
    if m:
        # 剥掉集号与片名前缀，只看集号之后是否还挂着真正的小标题
        rest = name[m.end():].strip(" -、：:　")
        return rest          # 「庆余年 第17集」→ ""；「第17集 花絮」→ "花絮"
    return name


def order_choice(
    candidates: List[Dict[str, Any]],
    detail_map: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """候选选择面板的排序：**名称匹配度优先**，其次压掉带编号的续集，年份只作
    同分内的兜底（最新在前）。

    年份原本排在最前，实测搜「年会不能停」时 2025 的续集「年会不能停2：水到渠成」
    会凭年份抢走 ⭐推荐，把用户要的 2023 本片挤到第 2 项——用户没点名续集就不该抢位。
    """
    detail_map = detail_map or {}

    def year_of(c: Dict[str, Any]) -> int:
        d = detail_map.get(c["id"])
        if not d:
            return 0
        try:
            return int(d.get("vod_year") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(
        candidates,
        key=lambda c: (-(c.get("_score") or 0),
                       1 if c.get("_sequel") else 0,
                       -year_of(c)),
    )


# 消息首字符 → (类别, rich 颜色)。颜色只用 256 色里的柔和档，避免默认高饱和色
# 在一屏里互相抢注意力。类别只是色名的注释性分组，**分块不看类别**（看 block）。
_MARK_KIND: tuple = (
    ("✓", "ok", "color(114)"),        # 绿：这一步做成了
    ("✗", "err", "color(203)"),       # 红：这一步没成
    ("⚠", "warn", "color(214)"),      # 黄：做成了，但有个坑得知道
    ("ℹ", "note", "color(117)"),      # 蓝：中性说明（例如某参数被忽略）
    ("↻", "note", "color(117)"),      # 重载
    ("⬇", "note", "color(117)"),      # 抽流 / 下载
)
_STYLE_INFO = "color(117)"            # 「正在 X 搜索…」这类进度行


def _style_of(msg: str) -> str:
    """按文案首字符挑颜色。

    全项目的 toast 文案本来就以符号开头（✓ 成功 / ✗ 失败 / ⚠ 警告 / ℹ 说明），
    所以分类**不需要调用方传参** —— cli.py 里近百处 `ui.toast` 一行不改就能各归各的色。
    """
    head = (msg or "").lstrip()
    for mark, _kind, style in _MARK_KIND:
        if head.startswith(mark):
            return style
    return _STYLE_INFO


class Ui:
    """UI 后端接口。默认实现走终端（rich 可用时上色）。"""

    def __init__(self, console: Optional[Any] = None, quiet: bool = False):
        self.quiet = quiet
        if console is not None:
            self.console = console
        elif _HAS_RICH:
            self.console = Console(stderr=False, highlight=False)
        else:                          # pragma: no cover
            self.console = None
        self._status: str = ""
        # 后台线程（embed_open_watch / embed_poller / auto-next）会在主线程
        # 阻塞于 input() 时打印 toast。rich 的 Console.input 先把提示符打到行尾
        # （不换行）再等输入，后台 print 会接在提示符后面并自带换行，导致提示符
        # 被盖掉、换行后一片空白，看着像「下一行不显示」。用一个锁 + 当前提示符
        # 记录，让后台打印在自己那行结束后补换行并重画提示符。
        self._out_lock = threading.RLock()
        self._prompt: str = ""
        self._input_active: bool = False
        # 上一行属于哪个来源块（flow/bg/sec:xxx/panel/boot）。来源一变就空一行，
        # 让「流水状态 / 后台插播 / 命令表 / 选集网格」各自成块。
        self._last_block: str = ""

    # ── 输出原语 ──────────────────────────────────────────────────────────
    def _print(self, text: str, style: str = "", soft_wrap: bool = False) -> None:
        if self.quiet:
            return
        with self._out_lock:
            if self.console is not None:
                kw = {"soft_wrap": True} if soft_wrap else {}
                self.console.print(text, style=style, **kw) if style else self.console.print(text, **kw)
            else:                          # pragma: no cover
                print(text, file=sys.stdout, flush=True)
            # 正处于 input 阻塞期（多为后台线程 toast）：重画一个干净提示符，
            # 免得用户只看到被 toast 挤掉的那行、以为卡住了。
            if self._input_active and self._prompt:
                self._redraw_prompt()

    def _redraw_prompt(self) -> None:
        """input 阻塞期被后台打印打断后，补一个换行 + 重画当前提示符。"""
        if self.console is not None:
            self.console.print(self._prompt, end="", highlight=False, markup=False)
        else:                              # pragma: no cover
            sys.stdout.write(self._prompt)
            sys.stdout.flush()

    def _input(self, prompt: str) -> str:
        with self._out_lock:
            self._prompt = prompt
            self._input_active = True
        try:
            raw = (self.console.input(prompt) if self.console is not None
                   else input(prompt))            # pragma: no cover
        finally:
            with self._out_lock:
                self._input_active = False
                self._prompt = ""
        # 用户敲下的那行命令本身就是一条分隔线：命令回显下面别再补空行，
        # 否则「敲 20 → 空行 → 正在播放 → 空行 → 选集」会把一件事拉成三截。
        with self._out_lock:
            self._last_block = ""
        return raw

    # ── 分块与分类输出 ────────────────────────────────────────────────────
    def _rule(self) -> str:
        """一条与内容等宽收的细横线，用来框住面板类块。"""
        return "─" * min(max(self._term_width() - 4, 20), 56)

    def _gap(self, block: str) -> None:
        """来源块与上一行不同就空一行（程序开头第一条不空，避免莫名一个空白行）。"""
        if self._last_block and self._last_block != block:
            self._print("")
        self._last_block = block

    def _print_styled(self, text: str, style: str, block: str) -> None:
        """分块 + 上色 + 落一行，`toast` 与 `set_status` 共用这一条路径。"""
        with self._out_lock:
            self._gap(block)
            self._print(text, style=style, soft_wrap=True)

    def start_block(self, block: str = "panel") -> None:
        """面板/表格这类输出自己声明开一块：与上一块之间空一行。

        调用方随后不必再关心分块 —— 下一条 `toast` 换来源时同样会自动空行。
        """
        with self._out_lock:
            self._gap(block)

    def section(self, title: str, style: str = "bold color(117)",
                *, block: Optional[str] = None) -> None:
        """带标题的块起点：默认标题就是块名，所以换标题必然空一行。

        `--list` 的「[内嵌播放] / [弹窗播放不带选集] / [播放后端] / [缓存目录]」用的
        就是默认 —— 四块本来平级，紧挨着打就分不清哪行属于哪块。

        传 `block`（如 `flow`）则是**贴在当前块里**只打个标题、不空行：选集网格走这条，
        因为「✓ 正在播放 第20集」和下面那张网格说的是同一件事，中间插空行反倒像两件
        不相干的东西（标题 + 细横线已经足够把它俩跟上面的流水分开）。
        """
        with self._out_lock:
            self._gap(f"sec:{title.strip()}" if block is None else block)
            self._print(title, style=style)

    def toast(self, msg: str, auto_hide: bool = True, *, block: str = "flow") -> None:
        """终端没有「2.5 秒后自动消失」的概念，auto_hide 仅保留签名一致性。

        颜色按 `_style_of(msg)` 自动选（✓ 绿 / ✗ 红 / ⚠ 黄 / 其余蓝灰）：文案里的符号
        本来就是分类依据，没必要让近百处调用点各传一遍参数。

        `block` 才是**分块**依据，与颜色无关：同一条链路上的进度与结果（搜索 → 命中
        → 解析 → 起播）都是默认的 `flow`，连续打印时彼此不空行 —— 它们说的是同一件事，
        行间隔开会把工作流切成碎片。只有换来源才空行：`bg` 是后台线程插播（浏览器打开
        回执、自动连播、预取里程碑），面板走 `section()` / `start_block()`。

        soft_wrap=True：toast 是「一行状态」，不该被 rich 按词折行。
        rich 默认会在 `✓`/`⚠` 后面的空格处断行，长文案时把 emoji 甩成
        单独一行（宽度 79 的终端上 81 列的 toast 就会这样）。交给终端
        自己按字符硬折行，emoji 才不会落单。
        """
        del auto_hide
        self._print_styled(f"  {msg}", _style_of(msg), block)

    # ── ui.setStatus / hidePlaceholder ────────────────────────────────────
    def set_status(self, text: str) -> None:
        """占位层文案：终端里作为一行状态输出（同一状态重复设置只打一次）。"""
        if text == self._status:
            return
        self._status = text
        self._print_styled(f"· {text}", "dim", "flow")

    def hide_status(self) -> None:
        self._status = ""

    # ── progress(total, label) —— 进度条工厂 ──────────────────────────────
    def progress(self, total: float, label: str = "") -> Any:
        """返回原地刷新的进度条 handle（协议见 download.py：update/finish/close）。

        quiet 或 rich 缺失时退化为空实现 —— 此时高频推进不落任何行，
        调用方（player/download/prefetch）自己会改走 on_progress 逐行报。
        """
        if self.quiet or self.console is None or not _HAS_RICH:
            return _NullBar()
        return _RichBar(self.console, max(float(total), 1.0), label)

    # ── askChoice(candidates, detailMap) ──────────────────────────────────
    def ask_choice(
        self,
        candidates: List[Dict[str, Any]],
        detail_map: Optional[Dict[Any, Dict[str, Any]]] = None,
        default_first: bool = True,
    ) -> Dict[str, Any]:
        """多候选选择面板。返回用户选中的候选（dict，含 id/name/_score）。

        排序见 order_choice：**名称匹配度优先**，其次压掉带编号的续集，年份只作
        同分内的兜底（最新在前）。
        回车选第 1 项（⭐推荐）；输入序号选指定项；q / Ctrl-C 抛 KeyboardInterrupt。
        """
        detail_map = detail_map or {}
        ordered = order_choice(candidates, detail_map)

        rows: List[str] = []
        for i, c in enumerate(ordered):
            d = detail_map.get(c["id"]) or {}
            meta: List[str] = []
            if d:
                for key in ("type_name", "vod_year", "vod_remarks"):
                    if d.get(key):
                        meta.append(str(d[key]))
                if d.get("vod_time"):
                    meta.append("更新 " + str(d["vod_time"]).split(" ")[0])
            prefix = "⭐推荐 · " if i == 0 else ""
            rows.append(f"[{i + 1}] {c['name']}    {prefix}{(' · '.join(meta)) or '暂无详情'}")

        body = "\n".join(rows)
        head = f"搜到多个相关结果，请选择要播放的（共 {len(ordered)} 个）"
        self.start_block()               # 面板自成一块，别贴在上一条进度下面
        if self.console is not None and _HAS_RICH:
            self.console.print(
                Panel(Text(body), title=head, border_style="color(117)", expand=False)
            )
        else:                          # pragma: no cover
            self._print(head)
            self._print(body)

        while True:
            tip = "输入序号选择，回车选第 1 项，q 取消 > " if default_first else "输入序号 > "
            try:
                raw = self._input(tip).strip()
            except EOFError:
                raise KeyboardInterrupt
            if raw.lower() in ("q", "quit", "exit"):
                raise KeyboardInterrupt
            if not raw and default_first:
                return ordered[0]
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(ordered):
                    return ordered[idx]
            self._print("无效输入，请重新选择", style="color(203)")

    # ── mountEpBar(epBar, wrapper, load, eps, curIdx) ─────────────────────
    def _term_width(self) -> int:
        """终端可用列数：优先 rich 探测值，退化到 shutil，兜底 79。"""
        w = getattr(self.console, "width", 0) if self.console is not None else 0
        if not w or w <= 0:
            try:
                import shutil
                w = shutil.get_terminal_size((79, 24)).columns
            except Exception:              # pragma: no cover
                w = 79
        return max(40, w)

    def ep_bar(
        self,
        eps: List[Dict[str, str]],
        cur_idx: int,
        load: Callable[[Dict[str, str]], None],
        max_rows: Optional[int] = None,
        title: str = "",
    ) -> None:
        """选集栏：**一次性列出全部集数**，自适应终端宽度排成多列网格，高亮当前集。

        编号即可直接跳转（交互循环里敲数字 / n / p 换集）。每格去掉片名前缀，
        只留「第N集」之外的短标题；若某集只有「第N集」或无标题，则纯编号显示，
        网格更紧凑（庆余年 46 集几行即可看全）。
        """
        if not eps:
            return
        del max_rows  # 全量显示，不再按窗口截断
        self.render_list(eps, cur_idx, indent=4, title=title)
        del load  # 换集动作由交互循环负责，这里只做展示

    def render_list(
        self,
        eps: List[Dict[str, str]],
        cur_idx: int,
        indent: int = 4,
        title: str = "",
        block: Optional[str] = "flow",
    ) -> None:
        """把全部集数排成自适应终端宽度的多列网格，用 `[N]` 高亮当前集。

        每格补齐到固定槽宽（cw+2），ASCII 括号宽度恒为 1，避免因 ▶◀ 这类
        模糊宽度字符或分隔空格让最后一格溢出换行。CJK 宽度用 _disp_width 对齐。

        网格默认**贴在播放状态那一块里**（`block="flow"`，见 `section`）：换集后
        「正在播第几集」和「整季在哪」是一组信息，被空行劈开就成了一堆碎行。
        """
        if not eps:
            return
        shorts = [_ep_short(ep, i) for i, ep in enumerate(eps)]
        cell_txt = [f"{i + 1} {s}".rstrip() if s else f"{i + 1}"
                    for i, s in enumerate(shorts)]
        cw = max(_disp_width(t) for t in cell_txt)
        slot = cw + 2                       # 每格内容 + 前后各 1 标记位
        pad_left = " " * (indent - 2)       # 缩进 2 是行首，剩余额外在格子间补
        cols = max(1, (self._term_width() - indent) // slot)
        rows = (len(cell_txt) + cols - 1) // cols
        pref = f"《{title}》" if title else ""
        rule = self._rule()
        self.section(f"  {pref}选集（共 {len(eps)} 集，敲数字或 n/p 换集）:",
                     style="dim", block=block)
        self._print("  " + rule, style="dim")
        for r in range(rows):
            cells: List[str] = []
            for c in range(cols):
                i = r * cols + c
                if i >= len(cell_txt):
                    continue
                t = cell_txt[i]
                wrapped = f"[{t}]" if i == cur_idx else f" {t} "
                cells.append(wrapped + " " * (slot - _disp_width(wrapped)))
            self._print(pad_left + "  " + "".join(cells).rstrip(),
                        style="color(110)", soft_wrap=True)
        self._print("  " + rule, style="dim")

    # ── 命令速查面板 ──────────────────────────────────────────────────────
    def render_help(self, tips: Optional[List[str]] = None) -> None:
        """把交互命令排成两栏对齐的命令速查表（CJK 宽度对齐），下方附提示。

        键列内部先对齐（键补到统一宽再接说明），两栏之间留固定间隔；栏宽按
        终端宽度收敛，窄终端不至于溢出。全部走 soft_wrap，绝不让 rich 在 emoji
        或中英交界把某条命令折到下一行。
        """
        cmds = [
            ("n / p", "下一集 / 上一集"),
            ("<数字>", "跳到第 N 集"),
            ("l", "列出全部集数"),
            ("m [片名]", "重选影片（可指定官方站）"),
            ("s [编号]", "换解析源（保持当前集）"),
            ("r", "重新加载"),
            ("a", "开关自动解析"),
            ("x", "开关自动连播"),
            ("d", "保存当前集"),
            ("u", "打印直链"),
            ("h", "查看帮助"),
            ("q", "退出"),
        ]
        kw = max(_disp_width(k) for k, _ in cmds)
        cells = [f"{k + ' ' * (kw - _disp_width(k))}  {d}" for k, d in cmds]
        nrows = (len(cells) + 1) // 2
        left, right = cells[:nrows], cells[nrows:]
        lw = max(_disp_width(c) for c in left)
        gap = 4
        rule = self._rule()
        self.section("  命令速查")            # 与上一块（搜索/解析进度）之间空一行
        self._print("  " + rule, style="dim")
        for i in range(nrows):
            r = right[i] if i < len(right) else ""
            line = "  " + left[i] + " " * (lw - _disp_width(left[i]) + gap) + r
            self._print(line.rstrip(), style="color(110)", soft_wrap=True)
        self._print("  " + rule, style="dim")
        # 一行式写法光看命令表猜不出来，所以常驻提示；调用方给的 tips 追加在后面。
        default_tips = [
            "一行式：m 片名 [site 官方站] 直接换片、s 编号/源名 直接换源"
            "（如 m 嫁金钗 site 爱奇艺、s 4）",
            "只写 m / s 则逐项追问；其余命令不带参数",
        ]
        for t in [*default_tips, *(tips or [])]:
            self._print("  " + t, style="dim", soft_wrap=True)



class _NullBar:
    """不画任何东西的进度条（--quiet / 无 rich 时的回退）。"""

    def update(self, current, note="") -> None:
        pass

    def finish(self, note="") -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "_NullBar":
        return self

    def __exit__(self, *a) -> None:
        pass


class _RichBar:
    """rich Progress 的薄封装：适配 download.py 的 handle 协议。

    update(done, note) 里 note 只暂存，finish 时作为一行输出 —— 进度条
    走 start/stop 而非 Live 上下文，toast 等普通 print 才能安全穿插。
    """

    def __init__(self, console: Any, total: float, label: str = ""):
        from rich.progress import (
            BarColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
        )

        self._label = label or "进度"
        self._total = total
        self._note = ""
        self._started = False
        self._progress = Progress(
            TextColumn("[bold cyan]{task.fields[label]}[/]"),
            BarColumn(bar_width=None),
            TextColumn("[green]{task.percentage:>5.1f}%[/]"),
            TextColumn("[dim]{task.completed}/{task.fields[max]}[/]"),
            TimeRemainingColumn(compact=True),
            console=console,
            transient=True,          # 结束后擦掉进度条，不留残行
        )
        self._tid = self._progress.add_task(
            self._label, total=total, label=self._label, max=int(total)
        )

    def update(self, current, note="") -> None:
        if not self._started:
            self._progress.start()
            self._started = True
        self._progress.update(self._tid, completed=float(current))
        if note:
            self._note = note

    def finish(self, note="") -> None:
        if note:
            self._note = note
        self.update(self._total)

    def close(self) -> None:
        if self._started:
            self._progress.stop()
            self._started = False
        if self._note:
            print(f"  {self._label} 完成 · {self._note}", file=sys.stdout, flush=True)

    def __enter__(self) -> "_RichBar":
        return self

    def __exit__(self, *a) -> None:
        self.close()


class SilentUi(Ui):
    """测试/`--quiet` 用：吞掉所有输出，ask_choice 直接返回第一项。"""

    def __init__(self) -> None:
        super().__init__(console=None, quiet=True)
        self.logs: List[str] = []

    def _print(self, text: str, style: str = "", soft_wrap: bool = False) -> None:
        del soft_wrap
        self.logs.append(text)

    def toast(self, msg: str, auto_hide: bool = True, *, block: str = "flow") -> None:
        del block            # 静默模式下不画块，签名与基类保持一致即可
        self.logs.append(msg)

    def set_status(self, text: str) -> None:
        self._status = text
        self.logs.append(text)

    def ask_choice(self, candidates, detail_map=None, default_first=True):  # type: ignore[override]
        return candidates[0]
