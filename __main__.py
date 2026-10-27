"""`python -m video_vip ...` 入口。

源码在 `src/` 下，这里只留一个薄壳（与源码无关，故不进 src）。
"""
from __future__ import annotations
import sys
from .src.cli import main
if __name__ == "__main__":
    sys.exit(main())
