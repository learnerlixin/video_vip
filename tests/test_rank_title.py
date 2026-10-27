"""test_rank_title.py —— _rank_by_title 纯函数单测（无网络）。"""
import sys
from pathlib import Path

# 仓库根（tests -> video_vip -> AI_coding -> 仓库根）：用 __file__ 反推，
# 不写死绝对路径，换机器/换盘符也能直接 `python tests/xxx.py` 跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from AI_coding.video_vip.src.site_search import _rank_by_title, _norm_title, _is_noise_title

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
print("归一化")
print("=" * 70)
case("标点/空格归一", _norm_title("庆余年 第二季"), _norm_title("庆余年第二季"))
case("书名号归一", _norm_title("《庆余年第二季》深度解读"), "庆余年第二季深度解读")
case("间隔号归一", _norm_title("声生不息·港乐季"), "声生不息港乐季")
case("高亮标签剥离", _norm_title("<em>狂</em><em>飙</em>"), "狂飙")

print()
print("=" * 70)
print("二创识别")
print("=" * 70)
case("深度解读=噪声", _is_noise_title("庆余年第二季深度解读"), True)
case("正片=非噪声", _is_noise_title("庆余年第二季"), False)
case("花絮=噪声", _is_noise_title("以家人之名花絮合集"), True)

print()
print("=" * 70)
print("分级排序 / 剔除不相关")
print("=" * 70)
Q = "以家人之名"
# 关键回归：优酷把另一部剧《以子之名》当站内结果给出，必须剔除
r = _rank_by_title(Q, [("以子之名", "u_bad"), (Q, "u_exact")])
case("剔除相近但不同的剧", r, ["u_exact"])
r = _rank_by_title(Q, [("以子之名", "u_bad"), ("以家人之名 TV版", "u_loose")])
case("无精确时保留包含项", r, ["u_loose"])
r = _rank_by_title(Q, [("以家人之名", "u1"), ("以家人之名 深度解读", "u2")])
case("正片排在二创前", r, ["u1", "u2"])
r = _rank_by_title(Q, [("觅食迹 第二季", "x"), ("微体兔美食", "y")])
case("全不相关→判空", r, [])
r = _rank_by_title("庆余年第二季",
                   [("庆余年 第二季", "u1"), ("庆余年第二季深度解读", "u2")])
case("空格差异视为相等且优先", r, ["u1", "u2"])
r = _rank_by_title("声生不息", [("声生不息·港乐季", "u1")])
case("间隔号差异算包含", r, ["u1"])
r = _rank_by_title("狂飙", [("", "u_empty"), (None, "u_none"), ("狂飙", "ok")])
case("空标题剔除", r, ["ok"])

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
