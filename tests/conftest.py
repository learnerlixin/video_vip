"""tests/conftest.py —— pytest 跑这套用例时的路径准备。

这里的用例是**脚本式**的：模块顶层直接跑断言、末尾汇总 `PASS/FAIL`。
所以两种跑法都要能成立：

    python tests/test_iqy_eps.py    直接跑脚本，退出码即结果
    pytest tests/                   走各文件里的 test_no_failures()

pytest 只把 `tests/` 目录本身放进 sys.path，而本项目是
`仓库根/AI_coding/video_vip/`（`AI_coding` 是命名空间包，没有 __init__.py），
`from AI_coding.video_vip.src import ...` 需要**仓库根**在路径上。本文件负责补上，
这样新增用例不必再各自写一遍引导代码。

各测试文件里仍保留 `sys.path.insert(0, str(Path(__file__).resolve().parents[3]))`：
直接 `python tests/xxx.py` 时不会加载 conftest，那句是那种跑法唯一的引导来源。

注意：`test_native_search.py` / `test_native_accuracy.py` 打真实站点，
`pytest tests/` 会把它们一起跑（离线环境会失败），只想跑离线用例时用
`pytest tests/test_iqy_eps.py tests/test_yk_eps.py tests/test_sequel_rank.py
tests/test_rank_title.py tests/test_cli_flow.py`。
"""

import sys
from pathlib import Path

# tests/ -> video_vip/ -> AI_coding/ -> 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
