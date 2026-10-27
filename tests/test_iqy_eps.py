"""test_iqy_eps.py —— 爱奇艺选集枚举回归（stub 网络，无外部请求）。

覆盖：avlistinfo 优先 / 按 total 翻页（hasMore 不可靠）/ contentType 过滤 /
/v_ 单集页反推 albumId 补全整季 / 错 albumId 的 vid 归属兜底 / HTML 兜底不退化。
"""
import json
import sys
from pathlib import Path

# 仓库根（tests -> video_vip -> AI_coding -> 仓库根）：用 __file__ 反推，
# 不写死绝对路径，换机器/换盘符也能直接 `python tests/xxx.py` 跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from AI_coding.video_vip.src import site_search as ss

PASS, FAIL = 0, 0


def case(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         got={got}\n        want={want}")


AID = "1063371101969801"          # 沧元图 动态漫画
V1 = "v_1qfr4asm7bw"              # 真实第 1 集 vid
ALBUM = "https://www.iqiyi.com/a_a8tkp0z2ed.html"


def vid_at(n):
    """第 n 集 vid：n=1 用真实 vid，其余用占位（保证 /v_ 归属校验能过）。"""
    return V1 if n == 1 else f"v_ep{n:03d}"


class Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.encoding = "utf-8"

    def json(self):
        return self._payload


class FakeSession:
    """routes: [(url 子串, fn(url, params) -> Resp)]"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for pat, fn in self.routes:
            if pat in url:
                return fn(url, params or {})
        raise AssertionError(f"未预置的 URL: {url}")


# stub 会临时替换 net.get_session。pytest 把 6 个套件导进**同一个进程**，不还原的话
# 后面的真实网络用例（test_native_*）会拿到 FakeSession；直接跑脚本时每进程独立，
# 碰不到这个问题，但还原本身无害且让两种跑法都成立。
_ORIG_GET_SESSION = ss.net.get_session


def _restore_net():
    ss.net.get_session = _ORIG_GET_SESSION


def install(routes):
    sess = FakeSession(routes)
    ss.net.get_session = lambda: sess
    return sess


def api_page(url, params, total=52, per=20):
    """每页 per 条、total=52：验证按 total 翻页而非信 hasMore（实测恒 False）。"""
    page = int(params.get("page") or 1)
    lo = (page - 1) * per + 1
    hi = min(lo + per - 1, total)
    lst = [{"contentType": 1, "order": n,
            "playUrl": f"http://www.iqiyi.com/{vid_at(n)}.html"}
           for n in range(lo, hi + 1)]
    return Resp({"data": {"epsodelist": lst, "total": total, "hasMore": False}})


ALBUM_HTML = ('<html><script>var pageParams = { albumId: "%s" };</script>'
              '<a href="/%s.html">第1集</a></html>' % (AID, V1))
MOBILE_HTML = '{"data":{"albumId":%s,"name":"沧元图 动态漫画"}}' % AID

print("=" * 70)
print("avlistinfo 优先 + 按 total 翻页 + contentType 过滤")
print("=" * 70)
sess = install([("www.iqiyi.com/a_", lambda u, p: Resp(ALBUM_HTML)),
                ("avlistinfo", api_page)])
eps = ss._iqy_list_eps(ALBUM)
case("专辑页翻满 52 集（不信 hasMore）", len(eps or []), 52)
case("集号连续从第1集起", eps[0][0] if eps else None, "第1集")
case("末集 URL", eps[-1][1] if eps else None,
     f"https://www.iqiyi.com/{vid_at(52)}.html")
pages = sorted(int(p.get("page") or 0) for u, p in sess.calls if "avlistinfo" in u)
case("请求页码 1..3", pages, [1, 2, 3])
aid_used = {p.get("aid") for u, p in sess.calls if "avlistinfo" in u}
case("aid 取自桌面页内嵌 albumId", aid_used, {AID})

sess = install([("avlistinfo", lambda u, p: Resp(
    {"data": {"epsodelist": [
        {"contentType": 1, "order": 1, "playUrl": "http://www.iqiyi.com/v_a.html"},
        {"contentType": 1001, "order": 2, "playUrl": "http://www.iqiyi.com/v_trailer.html"},
        {"contentType": 1, "order": 3, "playUrl": "http://www.iqiyi.com/v_b.html"}],
        "total": 3, "hasMore": False}}))])
case("contentType!=1 的预告被丢弃",
     [u for _, u in (ss._iqy_list_eps_api(AID) or [])],
     ["https://www.iqiyi.com/v_a.html", "https://www.iqiyi.com/v_b.html"])
case("order 不连续时按 order 编号",
     [lab for lab, _ in (ss._iqy_list_eps_api(AID) or [])],
     ["第1集", "第3集"])

sess = install([("avlistinfo", lambda u, p: Resp({"code": "A00001", "msg": "参数错误"}))])
case("错误 aid 返回 None 而非抛异常", ss._iqy_list_eps_api("bad"), None)
case("空 albumId 直接 None", ss._iqy_list_eps_api(""), None)

print()
print("=" * 70)
print("单集页 /v_ 反推 albumId 补全整季")
print("=" * 70)
sess = install([("m.iqiyi.com/v_", lambda u, p: Resp(MOBILE_HTML)),
                ("avlistinfo", api_page)])
eps = ss._iqy_list_eps(f"https://www.iqiyi.com/{V1}.html")
case("粘贴单集页也拿到 52 集", len(eps or []), 52)
case("走的是移动版反推", any("m.iqiyi.com" in u for u, _ in sess.calls), True)
case("换集可定位第 3 集",
     ss._iqiyi_episode_url(f"https://www.iqiyi.com/{V1}.html", 3),
     f"https://www.iqiyi.com/{vid_at(3)}.html")

# 移动版反推到「不含当前集」的 albumId：必须退回只给这一集，不能交别剧
sess = install([("m.iqiyi.com/v_", lambda u, p: Resp('{"albumId":999}')),
                ("avlistinfo", lambda u, p: Resp(
                    {"data": {"epsodelist": [
                        {"contentType": 1, "order": 1,
                         "playUrl": "http://www.iqiyi.com/v_other_show.html"}],
                        "total": 1, "hasMore": False}}))])
eps = ss._iqy_list_eps(f"https://www.iqiyi.com/{V1}.html")
case("当前集不在接口结果里 → 退回单集", len(eps or []), 1)
case("退回的仍是当前集本身", eps[0][1] if eps else None,
     f"https://www.iqiyi.com/{V1}.html")

# 移动版拿不到 albumId（空壳）：重试后仍失败 → 退回只给这一集
sess = install([("m.iqiyi.com/v_", lambda u, p: Resp("<html>空壳</html>"))])
eps = ss._iqy_list_eps(f"https://www.iqiyi.com/{V1}.html")
case("反推失败退回单集", len(eps or []), 1)
case("反推失败前重试 3 次",
     len([1 for u, _ in sess.calls if "m.iqiyi.com" in u]), 3)

print()
print("=" * 70)
print("HTML 兜底（avlistinfo 空/失败时不退化）")
print("=" * 70)
HTML3 = ('<html><a href="/v_x1.html">第1集</a><a href="/v_x2.html">第2集</a>'
         '<a href="/v_x3.html">第3集</a></html>')
sess = install([("www.iqiyi.com/a_", lambda u, p: Resp(HTML3)),
                ("avlistinfo", lambda u, p: Resp({"code": "A00001"}))])
case("接口报错时走 HTML 锚文本",
     len(ss._iqy_list_eps("https://www.iqiyi.com/a_cxg4f2204g.html") or []), 3)

sess = install([("www.iqiyi.com/a_", lambda u, p: Resp(HTML3)),
                ("avlistinfo", lambda u, p: Resp(
                    {"data": {"epsodelist": [], "total": 0}}))])
case("接口空列表时走 HTML 锚文本",
     len(ss._iqy_list_eps("https://www.iqiyi.com/a_zzz.html") or []), 3)

# 桌面页既无 albumId 也无「第N集」锚文本 → 顺序枚举全部 /v_ 链接（老兜底保留）。
# 注意：该兜底走 _IQIYI_EPS_RE，只认绝对 www.iqiyi.com/v_x.html（原行为，未改）。
sess = install([("www.iqiyi.com/a_", lambda u, p: Resp(
    '<html><a href="https://www.iqiyi.com/v_y1.html">看第1集</a>'
    '<a href="https://www.iqiyi.com/v_y2.html">看第2集</a></html>')),
    ("avlistinfo", lambda u, p: Resp({"code": "A00001"}))])
eps = ss._iqy_list_eps("https://www.iqiyi.com/a_yyy.html") or []
case("无锚文本时顺序枚举", len(eps), 2)
case("顺序枚举的集号从 1 起", [lab for lab, _ in eps], ["第1集", "第2集"])

sess = install([("www.iqiyi.com/a_", lambda u, p: Resp("<html>啥也没有</html>")),
                ("avlistinfo", lambda u, p: Resp({"code": "A00001"}))])
case("全空返回 None", ss._iqy_list_eps("https://www.iqiyi.com/a_none.html"), None)

print()
print("=" * 70)
print("albumId 提取（两种形态）")
print("=" * 70)
case("桌面 JS 形态 albumId: \"...\"", ss._iqy_extract_album_id(
    'var x = { albumId: "7410473159536632" };'), "7410473159536632")
case("移动 JSON 形态 \"albumId\":...", ss._iqy_extract_album_id(
    '{"inner":"683","albumId":1063371101969801,"x":1}'), AID)
case("没有 albumId", ss._iqy_extract_album_id("<html>none</html>"), "")
case("非数字 albumId 不误抓", ss._iqy_extract_album_id('{"albumId":"abc"}'), "")

_restore_net()

print()
print("=" * 70)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 70)
if __name__ == '__main__':
    sys.exit(1 if FAIL else 0)


def test_no_failures():
    """pytest 入口。脚本体在模块 import 时已把全部用例跑完，这里只断言汇总结果。

    两种跑法等价：
      python tests/test_x.py     走文件末尾的 sys.exit（退出码即结果）
      pytest tests/              走这个函数（FAIL>0 则该条用例失败）
    """
    _restore_net()
    assert FAIL == 0, f"{FAIL} 项断言失败（详见输出里的 [FAIL] 行）"
