"""download.py —— 把视频保存为 mp4（`d` 指令 / `--save`）。

复用 prefetch.py 的能力，分两步：

  1. **并发下载**：`parse_hls()` 解析清单 → `Prefetcher` 多线程拉分片到本地。
     单连接被限速在 ~1.2 Mbps，8 路并发能到 ~4.8 Mbps，整集 45 分钟约 400MB，
     顺序下载要等很久，所以必须并发。
  2. **封装 mp4**：ffmpeg 读本地 index.m3u8，`-c copy` 直接拷贝流、不重编码。
     分片本来就是 H.264/AAC，重编码只会白白损失画质又慢上几十倍。

几种特殊情况：
  * **加密流**（EXT-X-KEY）：`parse_hls()` 已把 key URI 本地化，`Prefetcher.start()`
    会先下密钥再下分片，ffmpeg 读本地清单时能正常解密。
  * **BYTERANGE 分片**：分片是同一（或各自）文件的不同字节区间。不交给 ffmpeg
    远程直连——这类源的 byterange URL 常以 `.image`/`ts.php` 结尾，ffmpeg 的
    hls demuxer 会按分片扩展名白名单拒掉（且无开关可绕）。改由 `Prefetcher`
    按每片 `offset@len` 发 Range 请求下成独立本地 `.ts`，再走本地封装。
  * **非 HLS 的直链单文件**（heycan-sign.byteimg.com 这类 `~tplv-*-image.image`
    签名链、裸 mp4/flv）：**没得分片可下，但也不是不能并发**——它是**一个完整文件**，
    CDN 认 `Range`。先探一次头（`bytes=0-65535`）拿到总长与 Accept-Ranges，再按
    `workers` 路 Range 分段并发下到本地、顺序拼成整文件，最后只做一次**本地**封装
    （甚至容器对得上就直接改名，不碰 ffmpeg）。签名参数 `x-expires`/`x-signature`
    对整条 URL 生效，**原样透传、绝不改写**（改任何一段都会 403）。
    服务器不理 Range（只回 200 全文件）时退回 ffmpeg 单连接远程 remux。
  * **没有 ffmpeg**：抛 DownloadError，提示安装（不静默失败）。

输出先写 `<名字>.part.mp4`，成功后再改名 —— 中途失败不会留下一个
看起来完整、实际损坏的 mp4。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.parse import urlsplit

import requests

from . import net
from .prefetch import (
    PrefetchError,
    Prefetcher,
    cleanup_dir,
    human_duration,
    human_size,
    parse_hls,
)
from .player import CACHE_ROOT, _cache_key

# 下载用的分片临时目录也收在统一缓存根下（%TEMP%\video_vip\download\），
# 不往用户的「下载」文件夹里塞隐藏目录 —— 那里只该出现最终的 mp4。
DOWNLOAD_ROOT = CACHE_ROOT / "download"


class DownloadError(Exception):
    """保存失败（没有 ffmpeg / 下载不到分片 / 封装出错）。"""


# 进度条工厂：(total, label) -> ProgressHandle。由 CLI 注入 ui.progress，
# 这样 download.py 不依赖 ui.py（也不依赖 rich），测试可以塞假实现。
ProgressFactory = Callable[[float, str], Any]


def _null_progress(total: float, label: str = "") -> Any:
    """没注入工厂时的兜底：什么都不画的进度条。"""
    class _Null:
        def update(self, current, note=""): pass
        def finish(self, note=""): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return _Null()


@dataclass
class SaveResult:
    """保存成功后的结果，CLI 用它打印摘要。"""

    path: Path
    size: int
    duration: float
    segments: int
    elapsed: float
    via_local_segments: bool      # True=预取分片后封装；False=ffmpeg 直接远程 remux


# Windows 文件名非法字符 + 控制字符；另外收尾的空格和点会让 Windows 出错
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# ffmpeg -stats 的进度行里取 time=00:01:23.45
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def sanitize_filename(name: str, fallback: str = "video", max_len: int = 120) -> str:
    """把片名/集名清成安全的文件名片段。"""
    cleaned = _INVALID_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return fallback
    return cleaned[:max_len].strip(" .") or fallback


def find_ffmpeg() -> Optional[str]:
    """找 ffmpeg 可执行文件。"""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def default_out_dir() -> Path:
    """默认输出目录：优先「下载」文件夹，退回当前目录。"""
    home = Path.home()
    for cand in (home / "Downloads", home / "下载"):
        if cand.is_dir():
            return cand / "video_vip"
    return Path.cwd()


def name_from_url(url: str) -> str:
    """从 URL 猜一个文件名（`--save <m3u8>` 没给片名时用）。

    取路径最后一段去掉扩展名；像 index / playlist / master 这种通用名没信息量，
    就再往上退一级目录名。都拿不到就返回空串，交给 sanitize_filename 兜底。
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", parts[-1])
    # 这些末段名没有信息量，退一级用目录名。
    # 注意 "hls" 不算通用名：`/2000k_1080/hls/index.m3u8` 里它至少区分了
    # 同级的 dash/，比 "index" 有用。
    generic = {"index", "playlist", "master", "video", "play", "main"}
    if stem.lower() in generic and len(parts) >= 2:
        parent = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", parts[-2])
        if parent and parent.lower() not in generic:
            return parent
    return stem


def resolve_out_path(
    out: Optional[Any],
    title: str = "",
    ep_name: str = "",
    url: str = "",
) -> Path:
    """把用户给的 `--out` 解析成最终 mp4 路径。

    * 没给 → 默认目录下 `<片名> <集名>.mp4`；片名也没有时退回 URL 猜的名字
    * 给的是已存在目录（或以分隔符结尾）→ 该目录下自动起名
    * 其余 → 当成完整文件路径；没写 .mp4 后缀就补上
    """
    stem = sanitize_filename(" ".join(x for x in (title, ep_name) if x).strip())
    if stem == "video" and url:
        # 片名/集名都没给（例如 --save 直接甩一个 m3u8），用 URL 猜个像样的名字
        guessed = sanitize_filename(name_from_url(url), fallback="")
        if guessed:
            stem = guessed
    if out is None or str(out).strip() == "":
        return default_out_dir() / f"{stem}.mp4"

    p = Path(str(out).strip()).expanduser()
    if p.is_dir() or str(out).strip().endswith(("\\", "/")):
        return p / f"{stem}.mp4"
    if p.suffix.lower() not in (".mp4", ".mkv", ".mov", ".ts"):
        p = p.with_suffix(".mp4")
    return p


def probe_duration(target: Any, ffmpeg: Optional[str] = None,
                   timeout: float = 60.0) -> float:
    """用 ffprobe 读时长；读不到返回 0（不影响主流程）。

    `target` 可以是本地路径，也可以是 m3u8/mp4 的网络地址 ——
    ffprobe 对 HLS 会直接按 playlist 里的 EXTINF 累加出总时长，
    不必把分片下完（实测本地 m3u8 约 0.3s）。
    自动连播靠它拿到「这一集应该播多久」，否则只能退化成一个粗糙的绝对下限。
    """
    exe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not exe:
        return 0.0
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(target)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return float((r.stdout or "").strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 直链单文件（非 HLS）：Range 分段并发下载
# ─────────────────────────────────────────────────────────────────────────────
# 无损云给的地址不一定都是 m3u8：heycan-sign.byteimg.com 这类
# `…~tplv-q356p4ooq1-image.image?x-expires=…&x-signature=…` 看着像图片模板，
# 实测 Content-Type 是 video/mp4、Content-Length 就是整集体积（609MB 级）——
# 它是**一个完整文件**。单连接拉要按小时算，CDN 又认 Range，所以拆成多段并发取。
_PROBE_BYTES = 65536           # 探总长用的首段长度（够读出 ftyp 魔数即可）
_MIN_PART = 2 * 1024 * 1024    # 段太小反而增加请求数与首字节开销


def _probe_single_file(url: str, headers: Optional[dict] = None,
                       timeout: float = 30.0) -> Optional[dict]:
    """探这个地址是不是「单个完整文件 + 支持 Range」，是则返回元信息，否则 None。

    只发一个 `Range: bytes=0-<probe-1>` 的小请求：支持 Range 的 CDN 回 206 并在
    Content-Range 里给出总长；不认 Range 的回 200（Content-Length 也是总长）。
    两种情况都顺带拿到首段字节，用来确认真的是媒体（MP4 的 `ftyp` / MPEG-TS 同步字节），
    免得把播放器 HTML 壳页当成视频下到一半才发现。
    **签名参数对整条 URL 生效，这里原样请求、一个字符都不改。**
    """
    hd = dict(headers or net.get_session().headers)
    hd["Range"] = f"bytes=0-{_PROBE_BYTES - 1}"
    try:
        r = net.get_session().get(url, headers=hd, timeout=timeout, stream=True)
    except Exception:
        return None
    try:
        if r.status_code not in (200, 206):
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" in ctype or "text/" in ctype:
            return None
        total = 0
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            tail = cr.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                total = int(tail)
        if not total:
            cl = r.headers.get("Content-Length") or ""
            if cl.isdigit():
                total = int(cl)
        if total <= 0:
            return None
        head = next(r.iter_content(16), b"") or b""
    finally:
        r.close()
    # MP4/MOV：偏移 4 处是 ftyp；MPEG-TS：首字节 0x47。其余（含 HTML 壳）不认。
    is_mp4 = head[4:8] == b"ftyp"
    if not (is_mp4 or head[:1] == b"\x47"):
        return None
    ranged = r.status_code == 206 and len(head) < total
    return {"total": total, "range": ranged, "is_mp4": is_mp4,
            "suffix": ".mp4" if is_mp4 else ".ts"}


def _fetch_part(url: str, start: int, end: int, dest: Path, retries: int,
                timeout: float, headers: Optional[dict],
                ranged: bool = True) -> Optional[int]:
    """取 `bytes=start-end` 写到 dest，返回落盘字节数；失败返回 None。

    `ranged=False` 表示探测阶段已确认这台服务器不认 Range，那就不该走到这里
    （调用方会直接退回 ffmpeg 单连接），留一个明确的失败出口好过把整文件当段拼坏。
    """
    hd = dict(headers or net.get_session().headers)
    hd["Range"] = f"bytes={start}-{end}"
    expect = end - start + 1
    for attempt in range(retries + 1):
        try:
            with net.get_session().get(url, headers=hd, timeout=timeout,
                                       stream=True) as r:
                if r.status_code == 206:
                    with dest.open("wb") as f:
                        for chunk in r.iter_content(1024 * 256):
                            if chunk:
                                f.write(chunk)
                    got = dest.stat().st_size
                    if got == expect:
                        return got
                elif r.status_code == 200 and ranged:
                    # 少数 CDN 认了区间却回 200：body 长度恰等于请求区间就直接收，
                    # 否则它是全文件，掐头取 expect 字节（只探测阶段判断过的才会走到）
                    data = r.content
                    if len(data) == expect:
                        dest.write_bytes(data)
                        return expect
                    slice_ = data[start:start + expect]
                    if len(slice_) == expect:
                        dest.write_bytes(slice_)
                        return expect
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    return None


def _download_segments(
    url: str,
    total: int,
    parts_dir: Path,
    workers: int = 8,
    retries: int = 3,
    timeout: float = 120.0,
    headers: Optional[dict] = None,
    ranged: bool = True,
    suffix: str = ".mp4",
    stop_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> Optional[Path]:
    """按 `bytes=off-end` 把整文件切成 N 段并发下到 parts_dir，顺序拼成一个本地文件。

    段数取 `workers*4`（再按 `_MIN_PART` 兜住下限），拼完即删段文件；任何一段重试
    耗尽就返回 None（调用方退回 ffmpeg 单连接远程 remux）——绝不留一个「看起来完整、
    实际少了一截」的成品，拼接后还会核对总字节数与服务器声明的长度一致。
    """
    if not ranged:
        return None
    workers = max(1, workers)
    seg = max(_MIN_PART, total // (workers * 4) + 1)
    ranges = [(off, min(off + seg, total) - 1) for off in range(0, total, seg)]
    parts_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    state = {"done": 0, "bytes": 0}
    want = {i: (e - s + 1) for i, (s, e) in enumerate(ranges)}

    def fetch(i: int) -> bool:
        if stop_event is not None and stop_event.is_set():
            return False
        s, e = ranges[i]
        dest = parts_dir / f"part{i:05d}.bin"
        if dest.exists() and dest.stat().st_size == want[i]:
            size = want[i]                      # 上次重试留下的好段，不重复下载
        else:
            tmp = parts_dir / f"part{i:05d}.bin.part"
            size = _fetch_part(url, s, e, tmp, retries, timeout, headers)
            if size is None or size != want[i]:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                return False
            try:
                tmp.replace(dest)               # 原子改名，坏段不会留下半截文件
            except OSError:
                return False
        with lock:
            state["done"] += 1
            state["bytes"] += size
            snapshot = (state["done"], len(ranges), state["bytes"])
        if on_progress:
            try:
                on_progress(*snapshot)
            except Exception:
                pass
        return True

    ok_flags: List[bool] = []
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="segdl") as pool:
        for ok in pool.map(fetch, range(len(ranges))):
            ok_flags.append(bool(ok))
            if stop_event is not None and stop_event.is_set():
                break
    if len(ok_flags) < len(ranges) or not all(ok_flags):
        return None

    # 段文件 → 整文件：按序号拼接，边拼边删，最后核对字节数
    merged = parts_dir / f"merged{suffix}"
    try:
        with merged.open("wb") as out:
            for i in range(len(ranges)):
                p = parts_dir / f"part{i:05d}.bin"
                with p.open("rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        return None
    if merged.stat().st_size != total:
        try:
            merged.unlink()
        except OSError:
            pass
        return None
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 单文件直链（非 HLS）：探测 + Range 分段并发下载 + 本地改名/封装
# ─────────────────────────────────────────────────────────────────────────────
def _download_single_file(
    url: str,
    dest: Path,
    part: Path,
    notify: Callable[[str], None],
    stop_event: Optional[threading.Event],
    t0: float,
    workers: int = 8,
    new_bar: Optional[Callable[[float, str], Any]] = None,
    headers: Optional[dict] = None,
) -> Optional[SaveResult]:
    """非 HLS 的单个完整文件（裸 mp4/flv 签名直链，如冰豆/无损云）：Range 分段并发下载。

    探测确认「支持 Range 的单文件」才走这条，否则返回 None（调用方退回 ffmpeg 单连接
    远程 remux）。探测已读过 ftyp/0x47 确认容器，mp4 直接改名收工不碰 ffmpeg；
    非 mp4(ts) 再本地 `-c copy` 封一次。签名参数原样透传、绝不改写。
    """
    meta = _probe_single_file(url, headers)
    if not meta or not meta.get("range"):
        return None
    total = meta["total"]
    parts_dir = DOWNLOAD_ROOT / _cache_key(url)
    # 段数与 _download_segments 内部算法保持一致，进度条 total 才对得上
    seg = max(_MIN_PART, total // (max(1, workers) * 4) + 1)
    nseg = (total + seg - 1) // seg
    notify(f"检测到单文件直链（{human_size(total)}），{nseg} 段 Range 并发下载…")
    bar = (new_bar or _null_progress)(nseg, "下载")
    try:
        merged = _download_segments(
            url, total, parts_dir, workers=workers, headers=headers,
            suffix=meta.get("suffix", ".mp4"), stop_event=stop_event,
            on_progress=lambda done, tot, nbytes: bar.update(done, human_size(nbytes)),
        )
        bar.finish()
    finally:
        try:
            bar.close()
        except Exception:
            pass
    if merged is None:
        cleanup_dir(parts_dir)
        return None                                   # 有段失败：退回 ffmpeg 单连接兜底
    if meta.get("is_mp4"):
        try:
            merged.replace(part)                      # 同一完整 mp4，直接改名，不重封装
        except OSError:
            shutil.move(str(merged), str(part))       # 跨盘改名失败时退回复制移动
    else:
        exe = find_ffmpeg()
        if not exe:
            cleanup_dir(parts_dir)
            return None
        _run_ffmpeg(_mux_cmd(exe, str(merged), part, local=True), 0.0, stop_event, None)
    cleanup_dir(parts_dir)
    if not part.exists() or part.stat().st_size == 0:
        raise DownloadError("单文件下载没有产出成品")
    try:
        part.replace(dest)
    except OSError as exc:
        raise DownloadError(f"写入 {dest} 失败：{exc}") from exc
    size = dest.stat().st_size
    real_dur = probe_duration(dest)
    elapsed = time.time() - t0
    return SaveResult(path=dest, size=size, duration=real_dur, segments=0,
                      elapsed=elapsed, via_local_segments=True)


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg 调用
# ─────────────────────────────────────────────────────────────────────────────
def _run_ffmpeg(
    cmd: List[str],
    total_duration: float,
    stop_event: Optional[threading.Event],
    bar: Any = None,
) -> None:
    """跑 ffmpeg，把 `-stats` 的 time= 推进到进度条；失败抛 DownloadError。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )
    except OSError as exc:
        raise DownloadError(f"无法启动 ffmpeg：{exc}") from exc

    tail: List[str] = []
    last_report = 0.0
    assert proc.stderr is not None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                raise DownloadError("已被用户取消")
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            for line in re.split(r"[\r\n]+", chunk):
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                m = _TIME_RE.search(line)
                if m and bar is not None and total_duration > 0:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    now = time.time()
                    # 轻限流：ffmpeg 的 stats 刷得很快，进度条自己也会限流，
                    # 这里再挡一层避免无谓的字符串格式化
                    if now - last_report >= 0.1:
                        last_report = now
                        bar.update(secs, human_duration(secs))
    finally:
        proc.stderr.close()

    rc = proc.wait()
    if stop_event is not None and stop_event.is_set():
        raise DownloadError("已被用户取消")
    if rc != 0:
        detail = " | ".join(tail[-6:]) or f"exit {rc}"
        raise DownloadError(f"ffmpeg 封装失败（rc={rc}）：{detail}")


def _mux_cmd(ffmpeg: str, src: str, dest: Path, local: bool,
             ua: Optional[str] = None, referer: Optional[str] = None) -> List[str]:
    """拼 ffmpeg 命令：流拷贝、不重编码、faststart 便于边下边播。"""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    if local:
        # 本地清单里的分片/密钥后缀是 .ts/.bin，hls demuxer 默认会拦，放开限制
        cmd += ["-allowed_extensions", "ALL",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    else:
        # 远程地址：-allowed_extensions 只有 HLS demuxer 认（分片常以 .image/ts.php
        # 结尾，默认扩展名白名单会拒）。裸 mp4/flv 单文件传它会报 "Option
        # allowed_extensions not found"（mp4 demuxer 无此选项），故仅在 src 是 m3u8
        # 清单时才加，并补 protocol_whitelist（清单会引用子分片/密钥）。
        if ".m3u8" in src.lower():
            cmd += ["-allowed_extensions", "ALL",
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
        cmd += ["-user_agent", ua or
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"]
        if referer:
            cmd += ["-headers", f"Referer: {referer}\r\n"]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"]
    cmd += ["-i", src, "-c", "copy", "-movflags", "+faststart", str(dest)]
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────
def save_as_mp4(
    url: str,
    out: Optional[Any] = None,
    title: str = "",
    ep_name: str = "",
    workers: int = 8,
    variant: str = "highest",
    keep_temp: bool = False,
    stop_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    make_progress: Optional[ProgressFactory] = None,
    ffmpeg: Optional[str] = None,
    headers: Optional[dict] = None,
) -> SaveResult:
    """把 `url` 保存成 mp4，返回 SaveResult。

    `on_progress(text)` 报**里程碑**（解析完成、已保存…），一次一行；
    `make_progress(total, label)` 造**进度条**，承接下载/封装的高频推进
    （1395 片不再刷 1395 行）。`stop_event` 置位即中断。
    `headers` 给第三方抽出的 m3u8 带自定义 UA/Referer（默认无损云头）。
    失败抛 DownloadError（调用方负责提示，不要静默吞掉）。
    """
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        raise DownloadError(
            "本机找不到 ffmpeg，无法封装 mp4。"
            "安装：winget install Gyan.FFmpeg（或 scoop install ffmpeg）"
        )

    def notify(text: str) -> None:
        if on_progress:
            try:
                on_progress(text)
            except Exception:
                pass

    def new_bar(total: float, label: str) -> Any:
        factory = make_progress or _null_progress
        try:
            return factory(total, label)
        except Exception:
            return _null_progress(total, label)

    dest = resolve_out_path(out, title, ep_name, url=url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part.mp4")

    t0 = time.time()
    stream = None
    cache_dir: Optional[Path] = None
    via_local = False
    try:
        # 先探「支持 Range 的单个完整文件」（一次 bytes=0-15 小请求即可判定）：命中就
        # 直接分段并发下载，绕开 parse_hls —— 否则它会对 508MB 裸 mp4 全量 GET 进内存
        # 才发现「不是 HLS playlist」，白等掉一整集的时间。
        # 仅对 URL 里没有 .m3u8 的地址预探测：真 HLS 源（789/LX/公益…）一律直接走
        # parse_hls，行为与改动前完全一致，避免对 ticket 型 m3u8 多发一次请求消耗票据。
        if ".m3u8" not in url.lower():
            res = _download_single_file(url, dest, part, notify, stop_event, t0,
                                        workers=workers, new_bar=new_bar, headers=headers)
            if res is not None:
                return res

        try:
            stream = parse_hls(url, variant=variant, headers=headers)
        except PrefetchError as exc:
            # 既不是 Range 单文件、又不是 HLS → 让 ffmpeg 单连接远程 remux 兜底
            notify(f"⚠ 无法解析 HLS 清单（{exc}），改为直接下载封装")
            return _remux_remote(exe, url, dest, part, notify, stop_event, t0,
                                 new_bar=new_bar, headers=headers)

        if stop_event is not None and stop_event.is_set():
            raise DownloadError("已被用户取消")

        total = len(stream.segments)
        dur = stream.total_duration
        est = stream.estimated_bytes
        est_txt = f"，预计 {human_size(est)}" if est else ""
        notify(f"解析完成：{total} 片 / {human_duration(dur)}{est_txt}")

        # BYTERANGE 分片也走预取：Prefetcher 会按每片的 offset@len 发 Range 请求，
        # 下成独立本地 .ts（这些源的 byterange URL 常以 .image/ts.php 结尾，
        # ffmpeg 会因分片扩展名白名单拒绝远程直连，逐片 Range 到本地再封装才稳）。
        # ── 并发下载分片 ────────────────────────────────────────────────────
        cache_dir = DOWNLOAD_ROOT / _cache_key(url)
        dl_bar = new_bar(total, "下载")
        pf = Prefetcher(
            stream, cache_dir, workers=workers,
            # 每片回调一次（1395 片就是 1395 次）→ 推进进度条，不再逐行打印
            on_progress=lambda done, tot, nbytes: dl_bar.update(done, human_size(nbytes)),
            headers=headers,
        )
        try:
            pf.start()
            # 保存要的是完整文件，必须等全部分片下完
            while not pf.wait_all(timeout=1.0):
                if stop_event is not None and stop_event.is_set():
                    raise DownloadError("已被用户取消")
                if pf.done + len(pf.failed_indices) >= pf.total:
                    break
        finally:
            pf.stop()
            try:
                dl_bar.finish()
                dl_bar.close()
            except Exception:
                pass

        failed = pf.failed_indices
        if failed:
            raise DownloadError(
                f"有 {len(failed)} 个分片下载失败（如 #{failed[:5]}），"
                f"已中止以免存出残缺文件"
            )
        if pf.done == 0:
            raise DownloadError("一个分片都没下到")
        if pf.key_errors:
            # 分片下全了但密钥没拿到：加密流解不了密。必须在这里拦住，
            # 否则 ffmpeg 只会报一句 "Invalid data found"，看不出是密钥的问题。
            raise DownloadError(
                f"加密流密钥下载失败（{', '.join(pf.key_errors)}），无法解密分片"
            )

        notify(f"✓ {pf.done} 片下载完成（{human_size(pf.bytes_done)}），开始封装 mp4…")

        # ── 封装 ────────────────────────────────────────────────────────────
        playlist = str(cache_dir / "index.m3u8")
        mux_bar = new_bar(dur, "封装")
        try:
            _run_ffmpeg(_mux_cmd(exe, playlist, part, local=True),
                        dur, stop_event, mux_bar)
            mux_bar.finish()
        finally:
            try:
                mux_bar.close()
            except Exception:
                pass
        via_local = True

    finally:
        # 分片临时目录：默认删掉（成品 mp4 已经不需要它了）
        if cache_dir is not None and not keep_temp and cache_dir.exists():
            cleanup_dir(cache_dir)

    if not part.exists() or part.stat().st_size == 0:
        raise DownloadError("封装没有产出文件")

    try:
        part.replace(dest)          # 原子改名，避免留下半个成品
    except OSError as exc:
        raise DownloadError(f"写入 {dest} 失败：{exc}") from exc

    size = dest.stat().st_size
    real_dur = probe_duration(dest) or (stream.total_duration if stream else 0.0)
    elapsed = time.time() - t0
    return SaveResult(path=dest, size=size, duration=real_dur,
                      segments=len(stream.segments) if stream else 0,
                      elapsed=elapsed, via_local_segments=via_local)


def _remux_remote(
    exe: str,
    url: str,
    dest: Path,
    part: Path,
    notify: Callable[[str], None],
    stop_event: Optional[threading.Event],
    t0: float,
    total_duration: float = 0.0,
    new_bar: Optional[Callable[[float, str], Any]] = None,
    headers: Optional[dict] = None,
) -> SaveResult:
    """不预取，直接让 ffmpeg 从远程地址下载并封装（裸 mp4 / BYTERANGE 用）。"""
    notify("ffmpeg 直接下载封装中…")
    bar = (new_bar or _null_progress)(total_duration, "下载")
    ua = (headers or {}).get("User-Agent")
    referer = (headers or {}).get("Referer")
    try:
        _run_ffmpeg(_mux_cmd(exe, url, part, local=False, ua=ua, referer=referer),
                    total_duration, stop_event, bar)
        bar.finish()
    finally:
        try:
            bar.close()
        except Exception:
            pass
    if not part.exists() or part.stat().st_size == 0:
        raise DownloadError("ffmpeg 没有产出文件")
    try:
        part.replace(dest)
    except OSError as exc:
        raise DownloadError(f"写入 {dest} 失败：{exc}") from exc
    size = dest.stat().st_size
    real_dur = probe_duration(dest)
    elapsed = time.time() - t0
    return SaveResult(path=dest, size=size, duration=real_dur, segments=0,
                      elapsed=elapsed, via_local_segments=False)
