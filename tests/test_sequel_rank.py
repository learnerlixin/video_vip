"""test_sequel_rank.py —— 续集编号降级 + 候选排序回归（纯函数，无网络）。

覆盖 title.is_numbered_sequel / wsyzy.rank_candidates / ui.order_choice：
搜「年会不能停」时 2025 的续集不得凭年份或前缀抢走 ⭐推荐。
"""
import sys
from pathlib import Path

# 仓库根（tests -> video_vip -> AI_coding -> 仓库根）：用 __file__ 反推，
# 不写死绝对路径，换机器/换盘符也能直接 `python tests/xxx.py` 跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from AI_coding.video_vip.src.title import (
    is_numbered_sequel, norm_title, strip_mark)
from AI_coding.video_vip.src.wsyzy import WsyzyDirect
from AI_coding.video_vip.src.ui import order_choice

PASS, FAIL = 0, 0


def case(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         got={got}\n        want={want}")


print("=" * 70)
print("归一化（title.py 单一实现，site_search 复用）")
print("=" * 70)
from AI_coding.video_vip.src import site_search as ss

case("site_search 复用同一实现", ss._norm_title is norm_title, True)
case("strip_mark 剥高亮标签", strip_mark("<em>狂</em><em>飙</em>"), "狂飙")
case("标点/空格归一", norm_title("庆余年 第二季"), norm_title("庆余年第二季"))
case("全角叹号归一", norm_title("年会不能停！"), "年会不能停")

print()
print("=" * 70)
print("is_numbered_sequel")
print("=" * 70)
case("裸数字续集", is_numbered_sequel("年会不能停", "年会不能停2：水到渠成"), True)
case("叹号也算本片起头",
     is_numbered_sequel("年会不能停！", "年会不能停2：水到渠成"), True)
case("中文数字续集", is_numbered_sequel("庆余年", "庆余年第二季"), True)
case("第N部", is_numbered_sequel("流浪地球", "流浪地球 第三部"), True)
case("同一片名不算续集", is_numbered_sequel("年会不能停", "年会不能停！"), False)
case("无编号后缀不算续集", is_numbered_sequel("庆余年", "庆余年  TV版"), False)
case("query 自身带编号不误伤",
     is_numbered_sequel("年会不能停2", "年会不能停2：水到渠成"), False)
case("不相关片名", is_numbered_sequel("狂飙", "狂飙 解说"), False)

print()
print("=" * 70)
print("rank_candidates 续集降级")
print("=" * 70)
d = WsyzyDirect.__new__(WsyzyDirect)          # 只测纯打分，不碰实例状态

items = [{"id": 1, "name": "年会不能停！"},
         {"id": 2, "name": "年会不能停2：水到渠成"}]
r = d.rank_candidates(items, "年会不能停")     # 用户搜的没带叹号
case("本片仍为精确/最高档", r[0]["name"], "年会不能停！")
case("续集被压到 2 分（前缀档降为包含档）", r[1]["_score"], 2)
case("续集打上 _sequel 标记", r[1]["_sequel"], True)
case("本片不带 _sequel", r[0]["_sequel"], False)

# 用户点名续集：不该被当成「别人没要的续集」压到包含档（3 分前缀档要保住）
r = d.rank_candidates([{"id": 1, "name": "年会不能停2：水到渠成"},
                       {"id": 2, "name": "年会不能停！"}], "年会不能停2")
case("用户点名续集时不降级", r[0]["_score"], 3)
case("点名的续集不带 _sequel", r[0]["_sequel"], False)
case("点名的续集排在前面", r[0]["name"], "年会不能停2：水到渠成")

r = d.rank_candidates([{"id": 1, "name": "年会不能停！"}], "年会不能停！")
case("唯一候选精确命中仍是 4 分", r[0]["_score"], 4)

print()
print("=" * 70)
print("order_choice：匹配度压过年份")
print("=" * 70)
detail = {1: {"vod_year": "2023"}, 2: {"vod_year": "2025"}}
cands = [{"id": 1, "name": "年会不能停！", "_score": 4, "_sequel": False},
         {"id": 2, "name": "年会不能停2：水到渠成", "_score": 2, "_sequel": True}]
case("⭐推荐落在本片而非更新的续集", order_choice(cands, detail)[0]["id"], 1)
case("续集仍在列表里（想播可选到）", len(order_choice(cands, detail)), 2)

# 匹配度相同时年份仍作兜底（最新在前）
cands = [{"id": 1, "name": "A", "_score": 3, "_sequel": False},
         {"id": 2, "name": "B", "_score": 3, "_sequel": False}]
case("同分仍按年份最新在前", order_choice(cands, detail)[0]["id"], 2)

# 无详情（年份全 0）也不能崩
case("无 detail_map 也可排序",
     [c["id"] for c in order_choice(cands, None)], [1, 2])

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
    assert FAIL == 0, f"{FAIL} 项断言失败（详见输出里的 [FAIL] 行）"
