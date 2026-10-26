"""net.py —— HTTP 请求层（`req(url)`）。

    req(url, timeout=...)  →  {status, responseText, finalUrl}

用 requests 实现，每线程一个 Session 复用连接。错误名固定为
resp_err / net_err / timeout，上层 `except WsyzyError as e: ... e.args[0]`
据此分支。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, Optional

import requests

from .config import DEFAULT_UA

DEFAULT_TIMEOUT = 8.0          # 8000ms
_local = threading.local()


class WsyzyError(Exception):
    """错误名固定为 resp_err / net_err / timeout，附带中文提示。"""


def get_session() -> requests.Session:
    """每线程一个 Session（连接复用）。"""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        _local.session = sess
    return sess


def gm_xmlhttp_request(
    url: str,
    method: str = "GET",
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
    data: Any = None,
) -> Dict[str, Any]:
    """发一次 HTTP 请求，返回 `{status, responseText, finalUrl}`。

    失败时抛 WsyzyError，错误名为 resp_err / net_err / timeout。
    """
    try:
        resp = get_session().request(
            method, url, timeout=timeout, headers=headers, data=data
        )
    except requests.Timeout as exc:
        raise WsyzyError("timeout") from exc
    except requests.RequestException as exc:
        raise WsyzyError("net_err") from exc

    # 接口返回的是 JSON（RFC 8259 规定 utf-8），但服务端偶尔不带 charset 头，
    # requests 会退回 ISO-8859-1 把中文变成乱码，所以这里显式按声明/utf-8 解码。
    from .page import detect_encoding

    resp.encoding = detect_encoding(resp)
    text = resp.text
    if "\ufffd" in text:
        try:
            candidate = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            candidate = None
        if candidate is not None and "\ufffd" not in candidate:
            text = candidate

    return {
        "status": resp.status_code,
        "responseText": text,
        "finalUrl": resp.url,
    }


def req(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """`req(url)`：GET 并要求 200 + 非空响应体，否则 resp_err。"""
    res = gm_xmlhttp_request(url, timeout=timeout)
    if res["status"] == 200 and res["responseText"]:
        return res["responseText"]
    raise WsyzyError("resp_err")


def req_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """`JSON.parse(await req(url))`"""
    text = req(url, timeout=timeout)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise WsyzyError("resp_err") from exc
