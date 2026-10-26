"""gm.py —— 持久化存储 / 打开链接 / 样式收集。

    get_value(key, default)   读一个持久化值（JSON 文件）
    set_value(key, value)     写一个持久化值；value 为 None 即删除该键
    open_in_tab(url, opts)    调用系统默认浏览器打开 URL
    add_style(css)            收集 CSS，供内嵌 HTML 后端输出

存储落在用户目录：
    Windows:  %USERPROFILE%\\video_vip_data\\gm_store.json
    其它:      ~/.video_vip_data/gm_store.json
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List

STORE_DIR = Path(os.environ.get("VIDEO_VIP_HOME") or (Path.home() / ".video_vip"))
STORE_FILE = STORE_DIR / "gm_store.json"

_lock = threading.RLock()
_mem: Dict[str, Any] = {}
_styles: List[str] = []          # add_style 收集的样式表
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if STORE_FILE.exists():
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                _mem.update(data)
        except (OSError, ValueError):
            pass


def _flush() -> None:
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STORE_FILE, "w", encoding="utf-8") as fh:
            json.dump(_mem, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_value(key: str, default: Any = None) -> Any:
    """读一个持久化值；键不存在时返回 default。"""
    with _lock:
        _load()
        return _mem.get(key, default)


def set_value(key: str, value: Any) -> None:
    """写一个持久化值；value 为 None 即删除该键。"""
    with _lock:
        _load()
        if value is None:
            _mem.pop(key, None)
        else:
            _mem[key] = value
        _flush()


def delete_value(key: str) -> None:
    with _lock:
        _load()
        _mem.pop(key, None)
        _flush()


def list_keys() -> Dict[str, Any]:
    with _lock:
        _load()
        return dict(_mem)


def open_in_tab(url: str, active: bool = True, insert: bool = True, set_parent: bool = True) -> bool:
    """用系统默认浏览器打开 URL。

    active / insert / set_parent 三个选项在命令行下只有一个语义：打开这个 URL，
    保留参数是为了让调用点写法统一。
    """
    del active, insert, set_parent  # 语义在桌面浏览器下无差别
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def add_style(css: str) -> None:
    """收集一段 CSS，由 HTML 内嵌后端写进 <style>。"""
    with _lock:
        _styles.append(css)


def get_styles() -> List[str]:
    with _lock:
        return list(_styles)


def reset_store() -> None:
    """测试/调试用：清空持久化存储。"""
    global _loaded
    with _lock:
        _mem.clear()
        _styles.clear()
        _loaded = True
        _flush()
