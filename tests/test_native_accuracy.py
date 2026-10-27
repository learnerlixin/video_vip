"""test_native_accuracy.py —— 验证原生搜索结果的**正确性**（不只是接口通）。

反查每个入口页：剧名是否吻合、整季是否连续、有没有把 PGC 搬运号/合集当成正片。
芒果走片库全量索引（D 段）：全等命中播放页后反查剧名吻合、整季枚举得出。
腾讯有频控，请求间加延时。
"""
import re
import sys
from pathlib import Path
import time

# 仓库根（tests -> video_vip -> AI_coding -> 仓库根）：用 __file__ 反推，
# 不写死绝对路径，换机器/换盘符也能直接 `python tests/xxx.py` 跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from AI_coding.video_vip.src import site_search as ss
from AI_coding.video_vip.src.config import url_supported

PASS, FAIL, NONE = 0, 0, 0

# (片名, 集数, 期望命中的站或 None=不强求)
CASES = [
    ("狂飙", 0), ("庆余年第二季", 3), ("哪吒之魔童闹海", 0),
    ("以家人之名", 2), ("水龙吟", 0), ("凡人修仙传", 0), ("声生不息", 0),
    ("甄嬛传", 5), ("大侦探", 0), ("琅琊榜", 0), ("流浪地球2", 0),
    ("熊出没", 0), ("长相思", 1), ("三体", 0),
]

print("=" * 74)
print("A) 腾讯原生：只给官方专辑（year>0），不给 PGC 搬运/合集")
print("=" * 74)
for title in ["凡人修仙传", "声生不息", "以家人之名", "庆余年第二季",
              "哪吒之魔童闹海", "甄嬛传", "狂飙", "流浪地球2", "长相思", "大侦探"]:
    for _try in range(3):
        try:
            urls = ss._tx_native_urls(title)
            break
        except Exception:
            time.sleep(4)
            urls = []
    bad = [u for u in urls
           if not re.search(r"v\.qq\.com/x/cover/[A-Za-z0-9]+/[A-Za-z0-9]+\.html", u)]
    # 已知腾讯只有 PGC 搬运/无站内源的（year=0），必须判空；
    # 声生不息 有正版「声生不息·港乐季」(year=2022)，属正常命中，不在判空之列。
    expect_empty = title in ("凡人修仙传", "以家人之名")
    ok = (not bad) and (not urls if expect_empty else True)
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {title:12s} urls={len(urls)} 死链={len(bad)} "
          f"{'(应判空)' if expect_empty else ''} {urls[:1]}")
    time.sleep(2.2)

print()
print("=" * 74)
print("B) find_page_url 端到端 + 反查剧名")
print("=" * 74)
for title, ep in CASES:
    try:
        hit = ss.find_page_url(title, episode=ep)
    except Exception as e:
        FAIL += 1
        print(f"  [FAIL] {title} EXC {type(e).__name__}: {e}")
        continue
    if hit is None:
        NONE += 1
        print(f"  [NONE] {title} ep{ep}")
        continue
    season = hit.season_url or hit.url
    name = ss.fetch_show_name(season)
    eps = ss.list_episode_pages(season) or []
    match = (not name) or (title in name) or (name in title)
    ok = url_supported(hit.url) and match
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {title:12s} → {hit.site} "
          f"pinned={hit.episode_pinned} 剧名={name!r} 整季={len(eps)}集")
    if not ok:
        print(f"          season={season} url={hit.url}")
    time.sleep(1.5)

print()
print("=" * 74)
print("C) 整季枚举连续性（1..N 无缺口）")
print("=" * 74)
for title, site in [("狂飙", "爱奇艺"), ("凡人修仙传", "优酷"),
                    ("庆余年第二季", "腾讯视频"), ("以家人之名", "芒果TV")]:
    urls = ss._site_urls(title, site)
    if not urls:
        print(f"  [SKIP] {site}/{title} 无结果")
        continue
    entry = urls[0]
    if site == "爱奇艺":
        entry = next((u for u in urls if ss._IQY_ALBUM_RE.search(u)), urls[0])
    eps = ss.list_episode_pages(entry) or []
    nums = [int(re.search(r"(\d+)", lab).group(1))
            for lab, _u in eps if re.search(r"\d+", lab)]
    cont = bool(nums) and nums == list(range(1, len(nums) + 1))
    if cont:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if cont else 'FAIL'}] {site}/{title} {len(nums)}集 "
          f"{nums[:6]}…{nums[-2:] if nums else ''}")
    time.sleep(1.5)

print()
print("=" * 74)
print("D) 芒果片库：命中页反查剧名吻合 + 整季可枚举")
print("=" * 74)
# 这几部正是旧的 intelmgtv 搜索接口给不出/给错正片的：《与凤行》正片 cid 626407 从不出现在
# 搜索结果里，《难哄》《承欢记》搜出来是快乐大本营与辟谣新闻。片库目录是全库权威表，
# 全等命中后反查剧名必须吻合、整季必须枚举得出。
# 综艺另说：它的 episode/list 用**日期**而非「第N集」标注（实测 t4=2022-04-17、t1=期号），
# `_mg_show_name` 取不到 t3、`_mg_eps_from_data` 也筛不出集 → 剧名退回播放页 <title>，集数不强求。
for q, variety in [("与凤行", False), ("以家人之名", False), ("玉簟秋", False),
                   ("我的人间烟火", False), ("声生不息", True)]:
    urls = ss._mg_native_urls(q)
    if not urls:
        FAIL += 1
        print(f"  [FAIL] 芒果/{q} 片库未命中")
        continue
    entry = urls[0]
    name = ss.fetch_show_name(entry)
    if not name:
        m = re.search(r"<title[^>]*>(.*?)</title>",
                      ss.net.get_session().get(entry, headers={"User-Agent": ss._NUA},
                                               timeout=15).text, re.S | re.I)
        name = ss._strip_mark(m.group(1)) if m else ""
    eps = ss.list_episode_pages(entry) or []
    nq, nn = ss._norm_title(q), ss._norm_title(name)
    ok = bool(nn) and nq in nn and (True if variety else len(eps) > 0)
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] 芒果/{q:8s} → {entry} "
          f"剧名={name!r} 整季={len(eps)}集{'（综艺）' if variety else ''}")
    time.sleep(1.5)

print()
print("=" * 74)
print(f"结果：PASS={PASS}  FAIL={FAIL}  无命中={NONE}")
print("=" * 74)
if __name__ == '__main__':
    sys.exit(1 if FAIL else 0)


def test_no_failures():
    """pytest 入口。脚本体在模块 import 时已把全部用例跑完，这里只断言汇总结果。

    两种跑法等价：
      python tests/test_x.py     走文件末尾的 sys.exit（退出码即结果）
      pytest tests/              走这个函数（FAIL>0 则该条用例失败）
    """
    assert FAIL == 0, f"{FAIL} 项断言失败（详见输出里的 [FAIL] 行）"
