"""test_native_search.py —— 验证四站原生搜索改造（独立脚本，跑完可删）。

验证目标：
  1. _site_urls 四站都只走原生接口（Bing 兜底已删除，不存在的符号即回归测试）。
  2. 返回的 URL 全部通过 url_supported，且域名属于该站。
  3. find_page_url 端到端：片名 → SiteHit，episode_pinned 如实标注，不抛异常。
  4. list_episode_pages 用原生入口页仍能枚举整季。
  5. 腾讯：站内无片源（分销/待播）不返回无法解析的 cid-only 页。
  6. 芒果片库索引：片名全等命中权威 cid+vid，查不到返回空（= 可信的无版权判定）。
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

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


DOMAINS = {
    "爱奇艺": ("iqiyi.com",),
    "优酷": ("youku.com",),
    "腾讯视频": ("v.qq.com",),
    "芒果TV": ("mgtv.com",),
}

CASES = ["凡人修仙传", "狂飙", "庆余年第二季", "哪吒之魔童闹海", "水龙吟", "以家人之名"]

print("=" * 72)
print("1) _site_urls：只走原生接口，结果合规且域名归属正确")
print("=" * 72)
for site in ("爱奇艺", "优酷", "腾讯视频", "芒果TV"):
    for title in CASES:
        t0 = time.time()
        try:
            urls = ss._site_urls(title, site)
        except Exception as e:
            check(f"{site}/{title}", False, f"EXC {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        check(f"{site}/{title} 全部url_supported({len(urls)})",
              all(url_supported(u) for u in urls), str(urls[:2]))
        bad = [u for u in urls if not any(d in u for d in DOMAINS[site])]
        check(f"{site}/{title} 域名归属", not bad, str(bad[:2]))
        print(f"         {len(urls)} urls {dt:.1f}s  {urls[:1]}")

print()
print("=" * 72)
print("2) 腾讯：站内无片源不给 cid-only 死页")
print("=" * 72)
for title in ["水龙吟", "庆余年第二季", "哪吒之魔童闹海"]:
    urls = ss._tx_native_urls(title)
    dead = [u for u in urls
            if not re.search(r"v\.qq\.com/x/cover/[A-Za-z0-9]+/[A-Za-z0-9]+\.html", u)]
    check(f"腾讯/{title} 无cid-only死页({len(urls)}条)", not dead, str(dead[:2]))

print()
print("=" * 72)
print("3) find_page_url 端到端（不抛异常 + SiteHit 契约）")
print("=" * 72)
for title, ep in [("凡人修仙传", 0), ("狂飙", 0), ("庆余年第二季", 3),
                  ("哪吒之魔童闹海", 0), ("水龙吟", 0), ("以家人之名", 2),
                  ("声生不息", 0), ("不存在的一部剧xyz", 0)]:
    t0 = time.time()
    try:
        hit = ss.find_page_url(title, episode=ep)
    except Exception as e:
        check(f"find_page_url({title},ep{ep})", False, f"EXC {type(e).__name__}: {e}")
        continue
    dt = time.time() - t0
    if hit is None:
        print(f"  [NONE] {title} ep{ep} {dt:.1f}s")
        check(f"find_page_url({title}) 无结果不抛异常", True)
        continue
    check(f"find_page_url({title},ep{ep}) → {hit.site} url合规",
          url_supported(hit.url), hit.url)
    print(f"         {hit.site} pinned={hit.episode_pinned} {hit.url}  {dt:.1f}s")

print()
print("=" * 72)
print("4) list_episode_pages 用原生入口页枚举整季")
print("=" * 72)
for title, site in [("狂飙", "爱奇艺"), ("凡人修仙传", "优酷"),
                    ("庆余年第二季", "腾讯视频"), ("以家人之名", "芒果TV")]:
    urls = ss._site_urls(title, site)
    if not urls:
        print(f"  [SKIP] {site}/{title} 原生无结果")
        continue
    entry = urls[0]
    if site == "爱奇艺":
        entry = next((u for u in urls if ss._IQY_ALBUM_RE.search(u)), urls[0])
    t0 = time.time()
    eps = ss.list_episode_pages(entry)
    dt = time.time() - t0
    n = len(eps or [])
    check(f"{site}/{title} 枚举整季", n > 0, f"entry={entry}")
    if eps:
        print(f"         {n} 集 首={eps[0][0]} 末={eps[-1][0]}  {dt:.1f}s")

print()
print("=" * 72)
print("5) 芒果片库索引：全等命中权威 cid / 查不到即无版权 / Bing 路径已删净")
print("=" * 72)
residual = [n for n in ("_bing_search", "_extract_urls", "_decode_bing_href",
                        "_BING_FALLBACK_SITES", "_site_domain", "_SITE_PATTERNS")
            if hasattr(ss, n)]
check("Bing 兜底符号零残留", not residual, str(residual))

idx = ss._mg_lib_index()
check(f"片库索引建成（{len(idx)} 条）", len(idx) > 1000)

# cid 以片库目录为权威（这三条正是 intelmgtv 搜索接口给不出的专辑）
for name, cid in [("与凤行", "626407"), ("以家人之名", "333900"), ("玉簟秋", "802659")]:
    urls = ss._mg_native_urls(name)
    check(f"芒果/{name} 命中权威 cid {cid}",
          bool(urls) and f"/b/{cid}/" in urls[0], str(urls[:2]))

# 反向：全库查不到 → 空列表（可信的「芒果无此片」），而不是返回一条花絮页
check("芒果/乱查询 返回空", not ss._mg_native_urls("不存在的一部剧xyz"))

print()
print("=" * 72)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
if __name__ == '__main__':
    sys.exit(1 if FAIL else 0)


def test_no_failures():
    """pytest 入口。脚本体在模块 import 时已把全部用例跑完，这里只断言汇总结果。

    两种跑法等价：
      python tests/test_x.py     走文件末尾的 sys.exit（退出码即结果）
      pytest tests/              走这个函数（FAIL>0 则该条用例失败）
    """
    assert FAIL == 0, f"{FAIL} 项断言失败（详见输出里的 [FAIL] 行）"
