"""test_yk_eps.py —— 优酷选集枚举回归（stub 网络，无外部请求）。

覆盖：只按 videoType==正片 过滤 / 不按 stage 去重（综艺一期多条）/
集号按顺序重编 / 换集与枚举同一口径 / stage=1..N 的剧集行为不变。
"""
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


class Resp:
    def __init__(self, text):
        self.text = text
        self.encoding = "utf-8"


# stub 会临时替换 net.get_session。pytest 把 6 个套件导进**同一个进程**，不还原的话
# 后面的真实网络用例（test_native_*）会拿到 FakeSession；直接跑脚本时每进程独立，
# 碰不到这个问题，但还原本身无害且让两种跑法都成立。
_ORIG_GET_SESSION = ss.net.get_session


def _restore_net():
    ss.net.get_session = _ORIG_GET_SESSION


def install(page_text):
    ss.net.get_session = lambda: FakeSession(page_text)


class FakeSession:
    def __init__(self, text):
        self.text = text
        self.urls = []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        return Resp(self.text)


def entry(stage, title, vid, vt="正片"):
    """拼一条与优酷移动页同形态的选集 JSON。

    vid 必须带 X 前缀：_YK_STAGE_VID_RE 只认 `(X[A-Za-z0-9=]+)`，真实优酷 vid
    都是 X 开头的 base64（XNjU1NzcyMDQxNg==）。这里传短名，统一补 X。
    """
    return ('{"data":{"videoType":"%s","title":"%s","stage":%d,'
            '"action":{"type":"JUMP_TO_VIDEO","value":"%s"}}}'
            % (vt, title, stage, "X" + vid))


# 综艺：stage 是日期，同一日期挂多条正片（偏爱之恋 第二季实测形态）
VARIETY = "{" + ",".join([
    entry(20261017, "超前速递：线索大开箱", "vA"),
    entry(20261020, "第1期 上：偏爱岛回归", "vB"),
    entry(20261021, "第1期 下：岛内约会", "vC"),
    entry(20261021, "偏爱日记：甜蜜摸头", "vD"),
    entry(20261022, "高糖纯享：齐聚偏爱岛", "vE"),
    entry(20261022, "岛民可见：初见面直击", "vF"),
    entry(20261023, "加更：竞技场荷尔蒙", "vG"),
    entry(20261024, "加载中：合宿生活开启", "vH"),
    entry(20261023, "竞技场雄竞开局", "vP1", "预告片"),
    entry(20261027, "本周抢先看", "vP2", "预告片"),
]) + "}"

# 剧集：stage 本就 1..N 且唯一
SERIES = "{" + ",".join(
    [entry(i, f"第{i}集：剧情", f"s{i}") for i in range(1, 6)]
    + [entry(3, "第3集预告", "trailer", "预告片")]) + "}"

print("=" * 70)
print("综艺（stage=播出日期，一期多条）")
print("=" * 70)
install(VARIETY)
rows = ss._yk_positive_rows("vz")
case("正片全保留，不按 stage 去重（8 条）", len(rows), 8)
case("预告片被丢弃", [v for _s, v in rows],
     ["XvA", "XvB", "XvC", "XvD", "XvE", "XvF", "XvG", "XvH"])
case("同 stage 保持页面出现顺序", [v for s, v in rows if s == 20261021], ["XvC", "XvD"])

eps = ss.list_episode_pages(
    "https://v.youku.com/v_show/id_vz.html")
case("枚举 8 集", len(eps or []), 8)
case("集号连续从第1集起", [lab for lab, _ in eps][:3], ["第1集", "第2集", "第3集"])
case("不再印日期式集号", any("20261017" in lab for lab, _ in eps), False)
case("末集是第8集", eps[-1][0], "第8集")

case("换集与枚举同口径：ep3 = 枚举第3条",
     ss._youku_episode_url("https://v.youku.com/v_show/id_vz.html", 3),
     eps[2][1])
case("原来恒 None 的 ep3 现在能定位",
     ss._youku_episode_url("https://v.youku.com/v_show/id_vz.html", 3),
     "https://v.youku.com/v_show/id_XvC.html")
case("ep8 末条可定位",
     ss._youku_episode_url("https://v.youku.com/v_show/id_vz.html", 8),
     "https://v.youku.com/v_show/id_XvH.html")
case("越界 ep9 返回 None",
     ss._youku_episode_url("https://v.youku.com/v_show/id_vz.html", 9), None)
case("episode<=0 返回 None",
     ss._youku_episode_url("https://v.youku.com/v_show/id_vz.html", 0), None)

print()
print("=" * 70)
print("剧集（stage=1..N 唯一）—— 与旧行为一致，无退化")
print("=" * 70)
install(SERIES)
rows = ss._yk_positive_rows("sz")
case("5 集全在", [v for _s, v in rows], ["Xs1", "Xs2", "Xs3", "Xs4", "Xs5"])
case("预告片仍被丢弃", len(rows), 5)
eps = ss.list_episode_pages("https://v.youku.com/v_show/id_sz.html")
case("标签仍是第1..5集", [lab for lab, _ in eps],
     ["第1集", "第2集", "第3集", "第4集", "第5集"])
case("stage 即序号，ep4 定位正确",
     ss._youku_episode_url("https://v.youku.com/v_show/id_sz.html", 4),
     "https://v.youku.com/v_show/id_Xs4.html")

print()
print("=" * 70)
print("去重边界：vid 重复要压掉，stage 重复不能压")
print("=" * 70)
install("{" + ",".join([entry(1, "第1集", "d1"), entry(1, "第1集(重复组件)", "d1"),
                        entry(2, "第2集", "d2")]) + "}")
case("同一 vid 被列两次只算一集",
     [v for _s, v in ss._yk_positive_rows("dup")], ["Xd1", "Xd2"])

install("{" + ",".join([entry(20261022, "岛民可见", "q2"),
                        entry(20261021, "第1期 下", "q1")]) + "}")
case("按 stage 升序（页面乱序也排回来）",
     [v for _s, v in ss._yk_positive_rows("ord")], ["Xq1", "Xq2"])

print()
print("=" * 70)
print("异常与空数据")
print("=" * 70)
install("{}`")
case("抓不到选集 → 空列表", ss._yk_positive_rows("bad"), [])
case("空枚举 → None", ss.list_episode_pages("https://v.youku.com/v_show/id_bad.html"), None)
case("空时换集 None",
     ss._youku_episode_url("https://v.youku.com/v_show/id_bad.html", 1), None)


class Boom:
    def get(self, *a, **k):
        raise RuntimeError("网络炸了")


ss.net.get_session = lambda: Boom()
case("请求异常不抛，返回空", ss._yk_positive_rows("x"), [])
case("请求异常时枚举 None",
     ss.list_episode_pages("https://v.youku.com/v_show/id_x.html"), None)

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
