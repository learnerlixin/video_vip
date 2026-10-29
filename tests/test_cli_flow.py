# -*- coding: utf-8 -*-
"""test_cli_flow.py —— cli 的九条行为契约的离线流程回归（不打网络、不真下载）。

覆盖的都是「容易在后续改动里悄悄退化」的控制流，而不是单元取值：

    [1] --save 不再只走无损云：显式 -s 第三方源时「视频页 → 解析页 → 抽明文
        m3u8 → 同一条下载通路」；没接入下载接口的源直接报错；不给 -s 仍走无损云。
    [2] 同时给了 URL 和 --title 时只认 URL（--title 被忽略），第三方源编号生效。
    [3] 交互循环里 `m` 换片名可以改「到哪家官方站找页面」，第三方源才问。
    [4] 交互循环里 `s` 换解析源时解析的是**当前这一集**的官方页，集数保持。
    [5] 一行式命令：`m 片名 [site 官方站]` 与 `s 编号/源名/p弹窗` 折叠掉追问，
        只敲单个字母仍走老写法；紧凑写法要能拆、`save` 不能被拆、参数不能被
        lower()、认不出的参数一律拒绝执行。
    [6] `site` 是**优先站**不是硬限定：那家没命中会兜底到其它三家，此时必须提示
        「改用 X」；命中指定站 / 没指定站 / 四家全空都不得瞎报。
    [7] 终端输出的两条视觉约定：**颜色看首符号**（✓ 绿 / ✗ 红 / ⚠ 黄 / 进度蓝灰，
        调用点不传样式），**分块看来源**（起播 + 选集网格同属 flow 不空行，后台插播
        bg、独立面板才留白，面板带细横线；用户敲的命令回显本身就是一条分隔线）。
    [8] `s` 菜单里 **▶ 标出当前在用的解析源**：换源是在几家接口之间跳，没有基准就
        等于盲选；弹窗播放另开浏览器窗口，只注「弹窗中」，不改终端会话在用的源。
    [9] 每条源都自带 **kind 分类**（资源型=有片库、按片名搜；采集型=必须喂一个具体
        视频页），字段与类别得对得上；CLI 的每条分流（要不要 URL、能不能弹窗、走不走
        --title）只问 `_source_needs_page_url`；站点参数由源注入 `WsyzyDirect`，两条
        搜索通路（官网联想 / 库内搜索）都归一成同一种候选形状。

手法：`ui._input` 变成脚本喂入（ScriptUi），终端输出收进 LOG，网络函数打桩。
`FakeConsumer/FakeResult` 只满足交互循环真正读到的那几个属性，`fake_enrich`
复刻真 `_enrich_thirdparty_episodes` 的关键机制——**按「实际解析的是哪一页」对齐
集号**，[4] 的「保持集数」正是搭在这句话上的。[6] 打桩的是搜索实现
`_scan_page_url`，被测的是 `find_page_url` 那层壳。[7] 不看 LOG 文本，而是拿假
console（RecConsole）接住 rich 的 `print(文本, 样式)`，断言「哪行上了什么色、
哪两块之间该留白」—— 所以 rich 装没装都照样跑。

跑法与其它用例一致：
    python tests/test_cli_flow.py     退出码即结果
    pytest tests/test_cli_flow.py     走末尾 test_no_failures()
"""
import argparse
import inspect
import sys
import types
from pathlib import Path
from typing import List
from urllib.parse import quote

# 仓库根（tests -> video_vip -> AI_coding -> 仓库根）：用 __file__ 反推，不写死路径
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from AI_coding.video_vip.src import cli
from AI_coding.video_vip.src import config as cfg_mod
from AI_coding.video_vip.src import net as net_mod
from AI_coding.video_vip.src import site_search
from AI_coding.video_vip.src import ui as ui_mod

# 断言细节里会出现剧名、URL 与中文提示；GBK 控制台下遇到编不出的字符按 ? 代替，
# 不让「打印」本身把用例跑崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"\n         {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         {detail}")


# ───────────────────────────────── 打桩脚手架 ─────────────────────────────────
_ORIG = []


def stub(obj, name, value):
    """替换模块属性（`cli.xxx` / `jx.resolve`）并记住原值，末尾统一还原。

    这些用例改的是**别的模块也没有**的全局名，不还原会污染同目录其它用例。
    """
    _ORIG.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)


def restore():
    for obj, name, value in reversed(_ORIG):
        setattr(obj, name, value)
    _ORIG.clear()


LOG = []          # 所有 ui 输出都落到这里，断言靠它检查「有没有如实播报」


class ScriptUi(cli.Ui):
    """`_input` 变成脚本喂入，其余输出收进 LOG（真 Ui 会走 rich + 阻塞读键盘）。"""

    def __init__(self, keys=(), quiet=True):
        super().__init__(quiet=quiet)
        self.keys = list(keys)
        self.prompts = []

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        if not self.keys:
            raise EOFError
        return self.keys.pop(0)

    def _print(self, text, style=None, soft_wrap=False, **kw):
        LOG.append(str(text))

    def toast(self, text, status=True, **kw):
        LOG.append("TOAST:" + str(text))

    def ep_bar(self, eps, idx, cb, title=""):
        LOG.append(f"EPBAR n={len(eps)} idx={idx}")

    def render_help(self, tips=None):
        pass

    def set_status(self, s):
        pass

    def progress(self, *a, **k):
        class _Noop:
            def update(self, *x, **y):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *x):
                return False
        return _Noop()


class FakeBackend:
    def __init__(self):
        self.played = []

    def play(self, url, title=""):
        self.played.append(url)

    def describe(self):
        return "fake"

    def stop(self):
        pass


class FakeResult:
    def __init__(self, episodes, index=0, name="测试剧"):
        self.episodes = episodes
        self.index = index
        self.hit = {"name": name}
        self.title = name
        self.backend = FakeBackend()      # 换集/换源要复用同一个后端对象
        self.embed_page = None

    def episode(self):
        return self.episodes[self.index]


class FakeConsumer:
    """只满足交互循环真正读到的成员；假后端不提供 take_picked /
    take_notices 等可选协议，因此页面选集轮询会直接跳过，可以放心驱动整个循环。"""

    def __init__(self, page_url, sel=1, result=None):
        self.page = types.SimpleNamespace(url=page_url)   # 会话最初那一页
        self.selected_index = sel
        self.last_result = result
        self.cfg = cli.Config(url=page_url, is_mobile=False)

    def shutdown(self, stop_player=True):
        pass

    def toggle_auto(self):
        return False

    def pick_popup(self, index):
        """弹窗播放：假打开成功（[8] 用它验证「弹窗中」标记）。"""
        calls.update(popup=index)
        return True

    def reload_current_player(self):
        pass

    def menu(self):
        return None


jx = cli.jx_resolve
PREFIXES = {i: (src.url or "") for i, src in enumerate(cli.VIDEO_PARSE_LIST)}
OFFICIAL = ["https://v.qq.com/x/cover/cid/e1.html",     # 整季入口页 = 第 1 集页
            "https://v.qq.com/x/cover/cid/e2.html",
            "https://v.qq.com/x/cover/cid/e3.html"]


def eps_of(src):
    """第三方源解析出来的选集栏：`源前缀 + 官方页`（HTML 解析页，不是流）。"""
    return [{"name": f"第{k + 1}集", "url": PREFIXES[src] + u}
            for k, u in enumerate(OFFICIAL)]


def fake_enrich(result, consumer, url, season_url, ui, drama=""):
    """复刻真函数的关键一步：补成整季后，按「解析的是哪一页」对齐 result.index。"""
    result.episodes = eps_of(consumer.selected_index)
    cur = next((i for i, u in enumerate(OFFICIAL) if u == url), 0)
    result.index = min(cur, len(result.episodes) - 1)


def ok_resolve(parse_page):
    """假抽流：任何解析页都能抽出这条 m3u8（真接口失败另有专门用例覆盖）。"""
    return types.SimpleNamespace(m3u8="https://cdn/x.m3u8",
                                 headers=lambda: {"User-Agent": "UA"})


calls = {}

# ─────────────────────────────── [1] --save 抽流下载 ─────────────────────────
print("=" * 70)
print("[1] --save 走第三方源抽流下载")
print("=" * 70)

stub(cli, "_save_one", lambda url, title, ep_name, ui, opts, headers=None: (
    calls.update(save_one=(url, title, ep_name, headers)), True)[1])
stub(cli, "find_page_url", lambda title, episode=0, prefer=None, on_note=None: (
    calls.update(find=(title, episode, prefer)),
    types.SimpleNamespace(site="腾讯视频", url=OFFICIAL[max(episode - 1, 0)],
                          episode=episode, episode_pinned=True,
                          season_url=OFFICIAL[0], label=lambda: "腾讯 "))[1])
stub(jx, "resolve", lambda p: (calls.update(resolve=p), ok_resolve(p))[1])
stub(cli, "fetch_show_name", lambda u: "页面识别出的剧名")
stub(cli, "PageSession",
     lambda u: types.SimpleNamespace(url=u, load=lambda: None))

ns = argparse.Namespace(url=None, title="测试剧", episode=2, site=None, mobile=False,
                        out=None, variant="highest", prefetch_workers=8, quiet=True)

rc = cli._run_save(ns, ScriptUi(), source=1)
check("--save -s 1 抽流后转下载",
      rc == 0 and calls.get("save_one", (None,))[0] == "https://cdn/x.m3u8",
      f"rc={rc} calls={calls.get('save_one')}")
check("解析页 = 源前缀 + 第2集官方页",
      calls.get("resolve") == PREFIXES[1] + OFFICIAL[1], str(calls.get("resolve")))
check("集号写进文件名（第2集）", calls["save_one"][2] == "第2集", calls["save_one"][2])
check("抽到的请求头一路传到下载", calls["save_one"][3] == {"User-Agent": "UA"},
      str(calls["save_one"][3]))

calls.clear()
rc = cli._run_save(ns, ScriptUi(), source=5)          # 5 云解析：没接入下载接口
check("未接入下载接口的源直接报错（rc=2）", rc == 2, f"rc={rc}")
check("未接入时不抽流、不下载",
      not (calls.get("resolve") or calls.get("save_one")), str(calls))
usable = cli._downloadable_source_names()
check("可用源被列成可直接抄的编号", "1 LX解析" in usable and "云解析" not in usable,
      usable)
check("报错文案给出可用编号与 -s 0 退路",
      any("未接入下载接口" in line and "1 LX解析" in line for line in LOG), "")

LOG.clear()
calls.clear()
rc = cli._run_save(argparse.Namespace(**{**vars(ns), "title": None,
                                         "url": OFFICIAL[0]}),
                   ScriptUi(), source=1)
check("--save <视频页URL> 同样走抽流",
      rc == 0 and calls.get("resolve") == PREFIXES[1] + OFFICIAL[0],
      str(calls.get("resolve")))

# 交互循环里的 `d`：第三方源会话下当前集是解析页，也要先抽流再下
calls.clear()
third_res = FakeResult(eps_of(1), index=1)
cli._do_save(third_res, ScriptUi(), {})
check("`d` 存第三方源当前集时也先抽流",
      calls.get("resolve") == PREFIXES[1] + OFFICIAL[1]
      and calls.get("save_one", (None,))[0] == "https://cdn/x.m3u8",
      f"calls={calls}")

# 没给 -s：仍走无损云（刻意不跟播放路径共用默认源），全程不碰 jx_resolve
calls.clear()
stub(cli, "play_by_title", lambda name, episode=0, ui=None, backend=None,
         is_mobile=False, no_play=False, **kw: (
             calls.update(title_search=(name, episode, no_play)),
             FakeResult([{"name": f"第{k + 1}集",
                          "url": f"https://wsyzy.example/{k}.m3u8"}
                         for k in range(3)], index=max(episode - 1, 0)))[1])
rc = cli._run_save(ns, ScriptUi(), source=None)
check("--save 不给 -s 时仍按片名走无损云",
      rc == 0 and calls.get("title_search") == ("测试剧", 2, True), str(calls))
check("无损云路径不抽流、且不带 Referer",
      calls.get("resolve") is None and calls.get("save_one", (None, None, None, None))[3] is None,
      str(calls.get("save_one")))

# ────────────────────────── [2] URL 优先、--title 忽略 ───────────────────────
print()
print("=" * 70)
print("[2] --url + --title：只认 URL，--title 忽略")
print("=" * 70)

# 剧名取值顺序是代码里的行序，打桩前取源码来断言
fn_src = inspect.getsource(cli._play_page_url)
check("页面识别剧名优先于 --title 兜底",
      "fetch_show_name(url)" in fn_src
      and fn_src.index("fetch_show_name(url)") < fn_src.index('drama = args.title or ""'),
      "")

seen = {}
stub(cli, "Ui", ScriptUi)                 # 让 main 内部 new 的 ui 也落进 LOG


def spy_play_page_url(args, ui_, url, source, season_url=None):
    seen.update(play_page=(url, source, args.title))
    return 0


stub(cli, "_play_page_url", spy_play_page_url)
LOG.clear()
rc = cli.main(["https://v.qq.com/x/cover/cid/e1.html", "--title", "随便一个片名",
               "-s", "1", "--no-interact", "-q"])
check("URL 交给统一的页面解析流程",
      rc == 0 and seen.get("play_page", (None,))[0] == "https://v.qq.com/x/cover/cid/e1.html",
      str(seen))
check("-s 指定的第三方源生效（不再被 --title 顶掉）",
      seen.get("play_page", (None, None))[1] == 1, str(seen.get("play_page")))
check("已提示 --title 被忽略",
      any("忽略" in line and "随便一个片名" in line for line in LOG), "")

# ──────────────────────── [3] m 换片名时可指定官方站 ─────────────────────────
print()
print("=" * 70)
print("[3] 交互 m 换片名时可指定官方站")
print("=" * 70)

stub(cli, "start", lambda url, ui=None, backend=None, page=None, auto=True,
         source=None, **kw: (
             calls.setdefault("start", []).append((url, source)),
             FakeConsumer(url, sel=source,
                          result=FakeResult([{"name": "第1集",
                                              "url": PREFIXES[source] + url}],
                                            name="测试剧")))[1])
stub(cli, "_enrich_thirdparty_episodes", fake_enrich)

calls.clear()
LOG.clear()
res = FakeResult(eps_of(1), index=0)
con = FakeConsumer(OFFICIAL[0], sel=1, result=res)
u = ScriptUi(keys=["m", "新片名", "爱奇艺", "q"])
cli.interactive(con, res, u, con.cfg, save_opts={}, site=None)
check("m 之后追问到哪家官方站", any("官方站" in p for p in u.prompts), str(u.prompts))
check("find_page_url 收到 prefer=爱奇艺", calls.get("find") == ("新片名", 0, "爱奇艺"),
      str(calls.get("find")))
check("换片名从第 1 集开始（不沿用旧集号）",
      calls.get("start", [(-1, -1)])[-1][0] == OFFICIAL[0], str(calls.get("start")))

# ────────────────────────── [4] s 换源保持当前集数 ──────────────────────────
print()
print("=" * 70)
print("[4] 切换解析源保持当前集数")
print("=" * 70)

calls.clear()
LOG.clear()
res = FakeResult(eps_of(1), index=0)
con = FakeConsumer(OFFICIAL[0], sel=1, result=res)
cli.interactive(con, res, ScriptUi(keys=["2", "s", "2", "q"]), con.cfg,
                save_opts={}, site=None, season_url=OFFICIAL[0])
start_calls = calls.get("start", [])
check("s 换源解析的是当前集页（不是会话首页）",
      bool(start_calls) and start_calls[-1][0] == OFFICIAL[1], str(start_calls))
check("新源编号已应用", bool(start_calls) and start_calls[-1][1] == 2, str(start_calls))
check("换源后仍停在第 2 集", res.index == 1, f"index={res.index}")
check("如实播报保持了第几集", any("保持第 2 集" in line for line in LOG), "")

# ──────────────────── [5] 一行式命令（把追问折叠进同一条） ───────────────────
print()
print("=" * 70)
print("[5] 一行式 m / s")
print("=" * 70)

# 纯函数层：折叠语法的解析规则
_OOR = str(len(cli.VIDEO_PARSE_LIST))     # 越界编号：列表增删时不用回头改硬编码
check("m 片名 site 站 能同时切出片名与站点",
      cli._split_site_arg("嫁金钗 site 爱奇艺") == ("嫁金钗", "爱奇艺", ""),
      str(cli._split_site_arg("嫁金钗 site 爱奇艺")))
check("简称展开成标准站名（芒果→芒果TV）",
      cli._split_site_arg("片名 site 芒果")[1] == "芒果TV", "")
check("没写 site 时站名为空（沿用上次），不是错误",
      cli._split_site_arg("嫁金钗") == ("嫁金钗", None, ""),
      str(cli._split_site_arg("嫁金钗")))
check("认不出的官方站报错（不许当成片名硬搜）",
      cli._split_site_arg("片名 site 哔哩哔哩")[2] != "", "")
check("site 只跟一家", cli._split_site_arg("片名 site 爱奇艺 优酷")[2] != "", "")
check("编号直取", cli._resolve_source_arg("4") == (False, 4, ""),
      str(cli._resolve_source_arg("4")))
check("源名模糊匹配（麒麟→4）", cli._resolve_source_arg("麒麟") == (False, 4, ""),
      str(cli._resolve_source_arg("麒麟")))
check("M3U8 前缀唯一命中 7 而非 8", cli._resolve_source_arg("M3U8") == (False, 7, ""),
      str(cli._resolve_source_arg("M3U8")))
check("编号越界报错", cli._resolve_source_arg(_OOR)[1] is None,
      f"{_OOR} 号不存在 → {cli._resolve_source_arg(_OOR)}")
check("歧义源名报错并列候选", len(cli._resolve_source_arg("解析")[2]) > 0
      and cli._resolve_source_arg("解析")[1] is None, "")
check("p 前缀=弹窗", cli._resolve_source_arg("p4") == (True, 4, "")
      and cli._resolve_source_arg("p 4") == (True, 4, ""), "")
check("源名里的 p 不被当弹窗前缀", cli._resolve_source_arg("playm3u8") == (False, 8, ""),
      str(cli._resolve_source_arg("playm3u8")))

# 流程层：一行式真的不再追问
def run_keys(keys, src=1, index=0):
    """起一个第三方源会话，喂一行式命令，返回 (ui, 会话 result)。"""
    calls.clear()
    LOG.clear()
    res = FakeResult(eps_of(src), index=index)
    con = FakeConsumer(OFFICIAL[0], sel=src, result=res)
    u = ScriptUi(keys=[*keys, "q"])
    cli.interactive(con, res, u, con.cfg, save_opts={}, site=None,
                    season_url=OFFICIAL[0])
    return u, res


u, _res = run_keys(["m 嫁金钗"])
check("m 片名 直接换片：不再追问片名/站点",
      not any(p.startswith("片名") or "官方站" in p for p in u.prompts),
      str(u.prompts))
check("m 片名 走当前源、第 1 集", calls.get("find") == ("嫁金钗", 0, None),
      str(calls.get("find")))

u, _res = run_keys(["m 嫁金钗 site 爱奇艺"])
check("m 片名 site 站：一站搞定", calls.get("find") == ("嫁金钗", 0, "爱奇艺"),
      str(calls.get("find")))
check("m 片名 site 站：不追问站点", not any("官方站" in p for p in u.prompts),
      str(u.prompts))

u, _res = run_keys(["m嫁金钗"])
check("紧凑写法 m嫁金钗 等价于 m 嫁金钗", calls.get("find") == ("嫁金钗", 0, None),
      str(calls.get("find")))

u, _res = run_keys(["m Osiris 之泪"])
check("参数不参与 lower()（英文片名大小写保留）",
      calls.get("find", ("",))[0] == "Osiris 之泪", str(calls.get("find")))

u, _res = run_keys(["m 嫁金钗 site 哔哩哔哩"])
check("站点认不出就拒绝，不发起搜索",
      "find" not in calls and "start" not in calls, str(calls))
check("拒绝时列出可用官方站",
      any("爱奇艺" in line and "认不出" in line for line in LOG), "")

u, res = run_keys(["2", "s 4"], index=0)      # 先跳第2集，再一行式换到 4 号源
# 换源后外层 result 会被换成新对象，所以「保持第几集」看的是 start 拿到哪一页
check("s 编号 一行式换源且解析的是第2集页",
      calls.get("start", [(-1, -1)])[-1] == (OFFICIAL[1], 4), str(calls.get("start")))

u, res = run_keys(["2", "s 麒麟"])
check("s 源名 一行式换源（麒麟→4）",
      calls.get("start", [(-1, -1)])[-1] == (OFFICIAL[1], 4), str(calls.get("start")))

u, res = run_keys(["2", f"s {_OOR}"])
check("s 越界编号不执行", not calls.get("start") and res.index == 1, str(calls))

u, res = run_keys(["n 3"])
check("其余命令不带参数：n 3 被拒绝、没换集",
      res.index == 0 and any("不带参数" in line for line in LOG), f"index={res.index}")

stub(cli, "_do_save", lambda result, ui_, opts: (calls.update(do_save=True), True)[1])
u, res = run_keys(["save"])
check("save 仍以整词命中（没被拆成 s + ave）",
      calls.get("do_save") is True and not calls.get("start"), str(calls))

# ─────────────────── [6] site 是优先站：落到别家必须说出来 ───────────────────
print()
print("=" * 70)
print("[6] find_page_url：prefer 未命中、退回别家时提示")
print("=" * 70)

# `m 片名 site 爱奇艺` 最终把站名交到 find_page_url(prefer=…)，而 prefer 只是**排序
# 优先**：那家没版权就照样兜底到其它三家。用户特意指定的站换成了别家，必须播报 ——
# 否则「我要爱奇艺」和「拿到腾讯页」之间的落差只能从成功行的站名里猜。
# 这里打桩的是搜索实现 `_scan_page_url`，壳的行为才测得准（不打网络）。
stub(site_search, "_scan_page_url",
     lambda title, episode=0, prefer=None, on_note=None: (
         calls.update(scan=(title, episode, prefer)),
         site_search.SiteHit(site="腾讯视频", url=OFFICIAL[0]))[1])

NOTES: List[str] = []
site_search.find_page_url("庆余年", prefer="爱奇艺", on_note=NOTES.append)
check("prefer 原样交到搜索实现", calls.get("scan") == ("庆余年", 0, "爱奇艺"),
      str(calls.get("scan")))
check("指定站没命中 → 提示「改用 腾讯视频」",
      any("爱奇艺" in t and "腾讯视频" in t for t in NOTES), str(NOTES))

stub(site_search, "_scan_page_url",
     lambda title, episode=0, prefer=None, on_note=None: site_search.SiteHit(
         site="爱奇艺", url="https://www.iqiyi.com/v_a.html"))
NOTES.clear()
site_search.find_page_url("嫁金钗", prefer="爱奇艺", on_note=NOTES.append)
check("命中指定的站 → 不多嘴", not NOTES, str(NOTES))

NOTES.clear()
site_search.find_page_url("嫁金钗", on_note=NOTES.append)      # 压根没写 site
check("没指定站时不提「改用」", not any("改用" in t for t in NOTES), str(NOTES))

stub(site_search, "_scan_page_url", lambda *a, **kw: None)     # 四家全空
NOTES.clear()
site_search.find_page_url("查无此片", prefer="爱奇艺", on_note=NOTES.append)
check("全都没搜到（None）不谎报落到别家",
      not any("改用" in t for t in NOTES), str(NOTES))

restore()

# ─────────────── [7] 输出分块与上色：流水不碎、换源留白、面板带线 ───────────────
print()
print("=" * 70)
print("[7] Ui：颜色按首符号、分块按来源（flow/bg/panel）")
print("=" * 70)

# 用户敲一条命令，终端里同时躺着「搜索进度 / 命中结果 / 命令速查 / 选集网格 /
# 后台插播」五种东西。全挤在一起就分不出哪行属于哪件事；但按符号类别断块又太碎
# （一条工作流被切成六行）。所以约定：**颜色看首符号，分块看来源**。
# 这里用一个假 console 接住 rich 调用，既能读到「哪行上了什么色」，又不依赖 rich
# 真的装没装。
class RecConsole:
    """记录每次 print 的 (文本, 样式)。rich 的 Console 就是这两个入参在用。"""

    width = 79

    def __init__(self):
        self.rows: List[tuple] = []

    def print(self, *args, **kw):
        self.rows.append((str(args[0]) if args else "", kw.get("style") or ""))

    # 只有 ask_choice / 交互循环会用到；[7] 用它验证「命令回显即分隔线」
    def input(self, prompt=""):
        return getattr(self, "answer", "20")


def fresh(**kw):
    return cli.Ui(console=RecConsole(), quiet=False, **kw)


def texts(ui):
    return [t for t, _s in ui.console.rows]


ui = fresh()
ui.toast("✓ 按片名找到优酷视频页", False)
ui.toast("正在用「LX解析」解析该页...", False)
ui.toast("✗ 解析失败：该源当前不可用", False)
ui.toast("⚠ 爱奇艺 没搜到，改用腾讯视频", False)
ui.toast("↻ 重新加载", False)
styles = dict(ui.console.rows)
check("✓ 绿 / ✗ 红 / ⚠ 黄 各归各的色（调用点没传样式）",
      styles.get("  ✓ 按片名找到优酷视频页") == "color(114)"
      and styles.get("  ✗ 解析失败：该源当前不可用") == "color(203)"
      and styles.get("  ⚠ 爱奇艺 没搜到，改用腾讯视频") == "color(214)",
      str({k: v for k, v in styles.items() if k.strip()}))
check("进度行（无符号前缀）与 ↻ 都走蓝灰",
      styles.get("  正在用「LX解析」解析该页...") == "color(117)"
      and styles.get("  ↻ 重新加载") == "color(117)", "")
check("同一条链路（flow）连续打印不空行 —— 别把工作流切成碎片",
      "" not in texts(ui), str(texts(ui)))
check("程序第一条输出之前不空行", texts(ui)[0].startswith("  ✓"), texts(ui)[0])

ui = fresh()
ui.toast("✓ 正在播放 第1集", False)
ui.toast("✓ 播放页已在浏览器打开：http://127.0.0.1:53633", False, block="bg")
check("后台线程插播（bg）与流水之间空一行",
      texts(ui) == ["  ✓ 正在播放 第1集", "",
                    "  ✓ 播放页已在浏览器打开：http://127.0.0.1:53633"],
      str(texts(ui)))

ui = fresh()
ui.section("[内嵌播放]")
ui._print("  0  LX解析")
ui.section("[播放后端]")
ui._print("  embed    浏览器")
check("section() 换标题必空一行",
      texts(ui) == ["[内嵌播放]", "  0  LX解析", "", "[播放后端]", "  embed    浏览器"],
      str(texts(ui)))
check("section 标题自成一个块（后续 toast 换来源时会空行）",
      dict(ui.console.rows)["[内嵌播放]"] == "bold color(117)", "")

ui = fresh()
ui.render_help()
rows = texts(ui)
rules = [i for i, t in enumerate(rows) if t.strip().startswith("─")]
check("命令速查上下各一条细横线", len(rules) >= 2
      and "命令速查" in rows[0]
      and any("n / p" in t for t in rows[rules[0]:rules[1]]),
      f"rules={rules} 首行={rows[0]!r}")
tip_row = next((i for i, t in enumerate(rows) if "一行式" in t), -1)
check("一行式提示常驻在框外（不必按 h 也能看到）",
      tip_row > rules[-1], str(rows[-3:]))

ui = fresh()
ui.render_list([{"name": f"嫁金钗 第{i}集"} for i in range(1, 21)], 0, title="嫁金钗")
rows = texts(ui)
check("选集网格带标题与上下细横线",
      sum(1 for t in rows if t.strip().startswith("─")) == 2
      and any("共 20 集" in t for t in rows), str(rows[:3]))

# 换集后的三行（正在播放 / 选集标题 / 网格）说的是同一件事，被空行劈开就像三件事
ui = fresh()
ui.toast("✓ 正在播放 第20集", False)
ui.render_list([{"name": f"嫁金钗 第{i}集"} for i in range(1, 21)], 19, title="嫁金钗")
rows = texts(ui)
check("选集网格贴在播放状态下面（一组内容，中间不空行）",
      "" not in rows and "正在播放" in rows[0] and "选集" in rows[1], str(rows[:3]))
ui.toast("✓ 播放页已在浏览器打开", False, block="bg")
check("网格之后换来源（bg）照旧留白", texts(ui)[-2] == "", str(texts(ui)[-3:]))

# 交互循环里用户敲的那行命令自带一次换行 → 它本身就是分隔线，不必再空一行
ui = fresh()
ui.toast("✓ 播放页已在浏览器打开", False, block="bg")
ui._input("video_vip> ")
ui.toast("✓ 正在播放 第20集", False)
check("命令回显即分隔线：_input 之后不补空行",
      texts(ui) == ["  ✓ 播放页已在浏览器打开", "  ✓ 正在播放 第20集"], str(texts(ui)))

ui = fresh()
ui.toast("✓ 正在播放 第1集", False)
ui.render_help()
check("面板开块：紧跟着的速查表与上一行之间留白",
      "" in texts(ui), str(texts(ui)))

sui = ui_mod.SilentUi()
sui.toast("✓ 好了", False, block="bg")
check("--quiet / 测试用的 SilentUi 也吃 block 关键字（签名一致，不会 TypeError）",
      sui.logs == ["✓ 好了"], str(sui.logs))

print()
print("=" * 70)
print("[8] s 菜单：▶ 标出当前在用的解析源")
print("=" * 70)

# 换源就是把同一部影片在几家接口之间跳。菜单里不标基准，敲一次 `s` 就得重新回忆
# 刚才用的是哪家；而弹窗播放只是浏览器里另开的窗口，**不改**终端会话在用的源，
# 所以「当前」与「弹窗中」可以落在不同行上。
# 这一段不碰网络：`s` 菜单与弹窗分支只走 consumer.pick_popup。
u, _res = run_keys(["s"])
menu = [line for line in LOG if line.strip() and not line.startswith("TOAST:")]
arrows = [l for l in menu if "▶" in l]
check("当前在用的源带 ▶（会话起在 1 号源）",
      len(arrows) == 1 and arrows[0].startswith("  ▶ 1  "), str(arrows))
check("其余源不带 ▶、编号列仍对齐",
      any(l.startswith("    4  麒麟解析") for l in menu)
      and not any("▶" in l for l in menu if not l.startswith("  ▶")), str(menu[:3]))

u, _res = run_keys(["s", "p2", "s"])
marks = [l for l in LOG if "▶" in l or "弹窗中" in l]
check("弹窗播放后那一行注「弹窗中」",
      any("弹窗中" in l and l.lstrip().startswith("2 ") for l in marks), str(marks))
check("弹窗不改终端会话的源：第二次开菜单时 ▶ 仍在 1 号",
      [l for l in marks if "▶" in l][-1].startswith("  ▶ 1  "), str(marks))

# ───────────── [9] kind 分类：所有源都标注，分流只看它 ─────────────
print()
print("=" * 70)
print("[9] 资源型 / 采集型：kind 字段的自洽与分流")
print("=" * 70)

# 两类源的因果链完全不同，混用不会报错、只会走错通路：
#   资源型 RESOURCE —— 自带片库（maccms `provide/vod`），按**片名**搜就有整季 m3u8
#     直链，所以必须有 api，也没有「可弹窗的解析页」这种东西；
#   采集型 COLLECT  —— 现场解析**一个具体视频页**（`url + 页面URL`），必须有 url，
#     给片名它无从下手。
# kind 标错或字段漏填，得在这一层就被抓住，而不是等到运行时分流失配。
L = cli.VIDEO_PARSE_LIST
check("每条源都显式标注 kind（只有这两类，不允许漏标/第三种）",
      all(s.kind in (cli.RESOURCE, cli.COLLECT) for s in L),
      str([(s.name, s.kind) for s in L if s.kind not in (cli.RESOURCE, cli.COLLECT)]))
check("资源型都给 api、不给 url",
      all(s.api and not s.url for s in L if s.kind == cli.RESOURCE),
      str([(s.name, bool(s.api), s.url) for s in L if s.kind == cli.RESOURCE]))
check("采集型都给 url、不给 api",
      all(s.url and not s.api for s in L if s.kind == cli.COLLECT),
      str([(s.name, bool(s.url), s.api) for s in L if s.kind == cli.COLLECT]))
check("资源型不带 type=3（弹窗要解析页，它们没有）",
      all("3" not in s.type for s in L if s.kind == cli.RESOURCE),
      str([(s.name, s.type) for s in L if s.kind == cli.RESOURCE]))
check("默认回退目标指向一条资源型源（列表增删不用回头改硬编码 0）",
      bool(cfg_mod.RESOURCE_INDEXES)
      and cli.DEFAULT_RESOURCE_INDEX == cfg_mod.RESOURCE_INDEXES[0],
      f"resource={cfg_mod.RESOURCE_INDEXES} default={cli.DEFAULT_RESOURCE_INDEX}")
check("分流判据与 kind 等价：所有分支只问 _source_needs_page_url",
      all(cli._source_needs_page_url(i) == (s.kind == cli.COLLECT)
          for i, s in enumerate(L)),
      str([(s.name, cli._source_needs_page_url(i)) for i, s in enumerate(L)]))

# 实测两家的搜索通路互斥：无损云关了库内搜索（`ac=detail&wd=` 回「暂不支持搜索」）、
# 只有官网 suggest；暴风/红牛没有官网（suggest 404）、只能库内搜索。所以资源型必须
# 容得下两种形态，`search_list` 也不能只写一条路。
SUG = [s for s in L if s.kind == cli.RESOURCE and s.suggest]
LIB = [s for s in L if s.kind == cli.RESOURCE and not s.suggest]
check("两种形态都在（有官网联想的 / 只能库内搜索的）",
      bool(SUG) and bool(LIB),
      f"suggest={[s.name for s in SUG]} api_only={[s.name for s in LIB]}")


def direct_for(src):
    """只喂站点参数，不看 play()：这里要验的是 api/suggest 的注入与归一。"""
    return cli.WsyzyDirect(cli.Config(url="https://example.invalid/"),
                           types.SimpleNamespace(url="https://example.invalid/",
                                                 window=None),
                           ui=ScriptUi(), source=src)


d_lib = direct_for(LIB[0])
check("没官网的资源型：api 归一带尾斜杠、suggest 保持空（不能用无损云官网兜底）",
      d_lib.api == LIB[0].api.rstrip("/") + "/" and d_lib.suggest == "",
      f"api={d_lib.api} suggest={d_lib.suggest!r}")
check("源名跟着源走（用户可见文案不再写死无损云）",
      d_lib.src_name == LIB[0].name, d_lib.src_name)
d_sug = direct_for(SUG[0])
check("有官网的资源型：suggest 去尾斜杠（后面拼路径），api 照样带",
      d_sug.suggest == SUG[0].suggest.rstrip("/") and d_sug.api.endswith("/"),
      f"suggest={d_sug.suggest!r} api={d_sug.api}")
d_old = direct_for(None)
check("不传 source 仍是无损云那对地址（历史调用点与用例不必改）",
      d_old.api == cli.WsyzyDirect.API.rstrip("/") + "/"
      and d_old.suggest == cli.WsyzyDirect.SITE,
      f"api={d_old.api} suggest={d_old.suggest}")

REQ = []


def fake_json(url, **kw):
    """假 provide/vod：按 URL 里的通路回各家的真实形状（不打真实站点）。"""
    REQ.append(url)
    if "/index.php/ajax/suggest" in url:
        return {"list": [{"id": 21, "name": "赘婿"}, {"name": "缺 id 的噪声"}, {}]}
    return {"list": [{"vod_id": 21, "vod_name": "赘婿",
                      "vod_play_url": "第1集$https://cdn/1.m3u8#第2集$https://cdn/2.m3u8"}]}


stub(net_mod, "req_json", fake_json)

REQ.clear()
hits = direct_for(LIB[0]).search_list("赘婿")
check("没官网 → 库内搜索 ac=detail&wd=，域名取自该源",
      len(REQ) == 1 and REQ[0].startswith(d_lib.api) and "?ac=detail&wd=" in REQ[0],
      str(REQ))
check("vod_id/vod_name 归一成 id/name（下游只认一种形状）",
      hits == [{"id": 21, "name": "赘婿"}], str(hits))
check("片名只编码一次（预先 quote 再交给 params 会二次编码成 %25E8…）",
      REQ[0].endswith(f"wd={quote('赘婿')}") and "%25" not in REQ[0], REQ[0])

REQ.clear()
hits = direct_for(SUG[0]).search_list("赘婿")
check("有官网 → 只发一次 suggest 请求，不碰库内搜索",
      REQ == [f"{d_sug.suggest}/index.php/ajax/suggest?mid=1&wd={quote('赘婿')}"],
      str(REQ))
check("suggest 结果里缺 id/name 的行被丢掉（后面要按 id 取详情）",
      hits == [{"id": 21, "name": "赘婿"}], str(hits))

REQ.clear()
eps = direct_for(LIB[0]).get_episodes(21)
check("详情/选集也用该源的 api，不是硬编码无损云接口",
      REQ == [f"{d_lib.api}?ac=detail&ids=21"], str(REQ))
check("选集解析两家共用同一条（都是 $$$ / # 分隔）",
      [e["name"] for e in eps] == ["第1集", "第2集"], str(eps))

# 分类字段最终要落到「执行走哪条路」：采集型接不了 --title，资源型弹窗没意义。
COL_SRC = next(i for i, s in enumerate(L) if s.kind == cli.COLLECT)
try:
    cli.play_by_title("赘婿", source=COL_SRC)
    check("采集型走 --title 路径直接报错（不拿片名硬喂需要 URL 的源）", False, "没抛异常")
except ValueError as e:
    check("采集型走 --title 路径报错并点名是哪条源",
          "采集型" in str(e) and L[COL_SRC].name in str(e), str(e))

u, _res = run_keys([f"s p{cli.DEFAULT_RESOURCE_INDEX}"])
check("一行式 `s p<资源型>` 被拒：不开弹窗、也不静默改成内嵌",
      "popup" not in calls and "start" not in calls
      and any("资源型" in line for line in LOG), str(calls))

u, _res = run_keys(["s", f"p{cli.DEFAULT_RESOURCE_INDEX}", "q"])
menu_reject = [line for line in LOG if line.startswith("TOAST:") and "弹窗" in line]
check("菜单里 `p<资源型>` 同样被拒并留在菜单（可以接着改选别家）",
      "popup" not in calls and menu_reject, str(menu_reject))

restore()

print()
print("=" * 70)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 70)
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)


def test_no_failures():
    """pytest 入口。脚本体在模块 import 时已把全部用例跑完，这里只断言汇总结果。"""
    assert FAIL == 0, f"{FAIL} 项断言失败（详见输出里的 [FAIL] 行）"
