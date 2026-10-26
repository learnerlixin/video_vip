"""prefetch.py —— HLS 并发预取引擎。

为什么需要它：无损云的分片服务器对**单连接**限速，实测单连接只有 ~1.2 Mbps，
而 720p 片源需要 ~1.4 Mbps —— 顺序取流必然追不上实时播放（模拟 40s 视频卡 4 次）。
但同一台服务器**并发**能拿到高得多的聚合带宽：

    并发  1 路:  1203 kbps   ✗ 不够
    并发  4 路:  1733 kbps   ✓
    并发  8 路:  4777 kbps   ✓
    并发 16 路:  8975 kbps   ✓

播放器（ffplay/mpv/浏览器 hls.js）都是顺序取流，改不了这个瓶颈。所以这里改成：
用线程池并发把分片拉到本地，同时写一份指向本地文件的 playlist，缓存够几片就起播，
播放器往后播时文件已经就位。PoC 实测：3.9s 起播，60s 视频 0 次缓冲抽干。

关键实现点：
  * 逐行改写 playlist，**保留所有标签**（EXT-X-DISCONTINUITY / EXT-X-KEY / MAP 等），
    只把裸 URI 行换成本地文件名 —— 重建 playlist 会丢掉 DISCONTINUITY 导致时间轴错乱。
  * 分片按顺序提交给线程池（FIFO 队列 → 大致顺序下载，8 片在飞），
    保证播放头前面的分片优先就位。
  * 本地文件播放器不认 HTTP 选项（-user_agent / -reconnect 会报
    "Option user_agent not found"），所以起播用的是 LOCAL_* 参数集。
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://wsyzy.cc/",
    "Accept": "*/*",
}

# 分片文件名后缀：按 Content-Type 猜不到时统一用 .ts（HLS 分片的常见格式）
_SEG_SUFFIX = ".ts"


class PrefetchError(Exception):
    """预取失败（playlist 取不到、不是 HLS、分片全部失败等）。"""


@dataclass
class Segment:
    """一个 HLS 分片。"""

    index: int
    url: str                      # 绝对 URL
    local_name: str               # 写到本地 playlist 里的文件名
    duration: float = 0.0
    downloaded: bool = False
    size: int = 0
    error: Optional[str] = None
    # BYTERANGE 分片：(起始偏移, 长度)。None=整片下载；给了就按 Range 只取这一段字节，
    # 落地成本地独立 .ts（LX 等源的 byterange 分片 URL 以 .image/ts.php 结尾，
    # ffmpeg 会因扩展名白名单拒绝远程直连，只能我们逐片 Range 下到本地再封装）。
    byterange: Optional[Tuple[int, int]] = None


@dataclass
class HlsStream:
    """解析后的 HLS 媒体播放列表。"""

    base_url: str                 # 媒体 playlist 的 URL（用于解析相对 URI）
    lines: List[str]              # 原始行，URI 行已替换为占位符
    segments: List[Segment] = field(default_factory=list)
    target_duration: float = 2.0
    version: int = 3
    keys: Dict[str, str] = field(default_factory=dict)   # 原始 key URI → 本地文件名
    maps: Dict[str, str] = field(default_factory=dict)   # EXT-X-MAP 的 URI → 本地文件名
    has_byterange: bool = False
    total_duration: float = 0.0
    bandwidth: float = 0.0        # master playlist 里该 variant 的 BANDWIDTH（bps）

    @property
    def is_encrypted(self) -> bool:
        return bool(self.keys)

    @property
    def estimated_bytes(self) -> int:
        """按 BANDWIDTH × 时长估算整集体积；没有 BANDWIDTH 时返回 0（不猜）。"""
        if self.bandwidth > 0 and self.total_duration > 0:
            return int(self.bandwidth / 8 * self.total_duration)
        return 0


_URI_PLACEHOLDER = "\x00SEG{}\x00"
_KEY_URI_RE = re.compile(r'URI="([^"]+)"')
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE", re.IGNORECASE)


def _looks_like_hls(text: str) -> bool:
    return "#EXTM3U" in text and ("#EXTINF" in text or "#EXT-X-STREAM-INF" in text)


def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _pick_variant(text: str, base_url: str, prefer: str = "highest") -> Tuple[Optional[str], float]:
    """从 master playlist 里挑一个 variant，返回 (绝对 URL, BANDWIDTH)。

    `prefer`：highest（默认，码率最高）/ lowest（最省带宽，弱网更稳）。
    BANDWIDTH 用于估算整集体积，拿不到时为 0。
    """
    best_url: Optional[str] = None
    best_bw = -1.0 if prefer == "highest" else float("inf")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        m = re.search(r"BANDWIDTH=(\d+)", line)
        bw = float(m.group(1)) if m else 0.0
        # 下一行非注释的就是 URI
        uri = None
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt.startswith("#"):
                uri = nxt.strip()
                break
        if uri is None:
            continue
        if prefer == "highest" and bw > best_bw:
            best_bw, best_url = bw, urljoin(base_url, uri)
        elif prefer == "lowest" and bw < best_bw:
            best_bw, best_url = bw, urljoin(base_url, uri)
    return best_url, (best_bw if best_url else 0.0)


def parse_hls(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 30.0,
    variant: str = "highest",
    max_redirects: int = 3,
    headers: Optional[dict] = None,
) -> HlsStream:
    """抓取并解析 HLS playlist；若是 master 则自动下钻到媒体 playlist。

    返回的 HlsStream.lines 里，分片 URI 行已替换成占位符，segments 按出现顺序排列。
    `headers` 给了就用它（第三方抽出的 m3u8 常绑自己的 UA/Referer），否则用默认。
    """
    sess = session or requests.Session()
    sess.headers.update(headers or DEFAULT_HEADERS)

    current = url
    text = ""
    bandwidth = 0.0
    for _ in range(max_redirects + 1):
        try:
            resp = sess.get(current, timeout=timeout)
        except requests.RequestException as exc:
            raise PrefetchError(f"playlist 抓取失败: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise PrefetchError(f"playlist HTTP {resp.status_code}")
        text = resp.text
        if not _looks_like_hls(text):
            raise PrefetchError("响应不是 HLS playlist")
        if not _is_master(text):
            break
        picked, bandwidth = _pick_variant(text, resp.url or current, prefer=variant)
        if not picked:
            raise PrefetchError("master playlist 里找不到 variant")
        current = picked
    else:
        raise PrefetchError("master playlist 下钻层数过多")

    base_url = current
    raw_lines = text.splitlines()

    has_byterange = any(_BYTERANGE_RE.search(l) for l in raw_lines)

    segments: List[Segment] = []
    keys: Dict[str, str] = {}
    maps: Dict[str, str] = {}
    out_lines: List[str] = []
    target_duration = 2.0
    version = 3
    pending_dur = 0.0
    pending_byterange: Optional[Tuple[int, int]] = None
    br_end = 0                       # 上一条 byterange 的结束偏移（无 @o 时接续用）
    seg_idx = 0

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#"):
            m = re.match(r"#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)", stripped)
            if m:
                target_duration = float(m.group(1))
                out_lines.append(line)
                continue
            m = re.match(r"#EXT-X-VERSION:(\d+)", stripped)
            if m:
                version = int(m.group(1))
                out_lines.append(line)
                continue
            m = re.match(r"#EXTINF:(\d+(?:\.\d+)?)", stripped)
            if m:
                pending_dur = float(m.group(1))
                out_lines.append(line)
                continue

            # BYTERANGE：<len>[@<offset>] —— 记到 pending，附加给紧随其后的分片。
            # 本地化时丢弃该标签：每片会按 Range 下成独立文件，字节已精确到位，
            # 本地清单不需要（也不该保留）区间标注。
            m = re.match(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?", stripped, re.IGNORECASE)
            if m:
                length = int(m.group(1))
                offset = int(m.group(2)) if m.group(2) is not None else br_end
                pending_byterange = (offset, length)
                br_end = offset + length
                continue

            # EXT-X-KEY / EXT-X-MAP 里带 URI，需要一并本地化
            if stripped.startswith("#EXT-X-KEY") or stripped.startswith("#EXT-X-MAP"):
                km = _KEY_URI_RE.search(stripped)
                if km:
                    kuri = km.group(1)
                    abs_k = urljoin(base_url, kuri)
                    if abs_k not in keys and stripped.startswith("#EXT-X-KEY"):
                        keys[abs_k] = f"key{len(keys)}.bin"
                    if abs_k not in maps and stripped.startswith("#EXT-X-MAP"):
                        maps[abs_k] = f"map{len(maps)}.ts"
                    local_name = keys.get(abs_k) or maps.get(abs_k)
                    if local_name:
                        line = _KEY_URI_RE.sub(f'URI="{local_name}"', line, count=1)
                out_lines.append(line)
                continue

            out_lines.append(line)
            continue

        # 裸 URI 行 = 分片地址
        abs_url = urljoin(base_url, stripped)
        seg = Segment(
            index=seg_idx,
            url=abs_url,
            local_name=f"seg{seg_idx:05d}{_SEG_SUFFIX}",
            duration=pending_dur,
            byterange=pending_byterange,
        )
        segments.append(seg)
        out_lines.append(_URI_PLACEHOLDER.format(seg_idx))
        seg_idx += 1
        pending_dur = 0.0
        pending_byterange = None

    if not segments:
        raise PrefetchError("playlist 里没有分片")

    # 用本地文件名替换占位符
    final_lines = []
    for line in out_lines:
        if line.startswith("\x00SEG") and line.endswith("\x00"):
            idx = int(line[len("\x00SEG"):-1])
            final_lines.append(segments[idx].local_name)
        else:
            final_lines.append(line)

    return HlsStream(
        base_url=base_url,
        lines=final_lines,
        segments=segments,
        target_duration=target_duration,
        version=version,
        keys=keys,
        maps=maps,
        has_byterange=has_byterange,
        total_duration=sum(s.duration for s in segments),
        bandwidth=bandwidth,
    )


def render_local_playlist(stream: HlsStream) -> str:
    """把解析结果写成本地 playlist 文本（标签原样保留，URI 已是本地文件名）。"""
    return "\n".join(stream.lines) + "\n"


ProgressFn = Callable[[int, int, int], None]      # (已完成片数, 总片数, 累计字节)


class Prefetcher:
    """并发下载 HLS 分片到本地目录，边下边可起播。

    用法：
        pf = Prefetcher(stream, cache_dir, workers=8)
        pf.start()
        pf.wait_for_segments(8, timeout=60)     # 缓存够 8 片即可起播
        ...
        pf.stop()                               # 取消下载（不清理文件）
    """

    def __init__(
        self,
        stream: HlsStream,
        cache_dir: Path,
        workers: int = 8,
        retries: int = 3,
        timeout: float = 60.0,
        session: Optional[requests.Session] = None,
        on_progress: Optional[ProgressFn] = None,
        headers: Optional[dict] = None,
    ):
        self.stream = stream
        self.cache_dir = Path(cache_dir)
        self.workers = max(1, workers)
        self.retries = max(0, retries)
        self.timeout = timeout
        self.on_progress = on_progress
        self._sess = session or requests.Session()
        self._sess.headers.update(headers or DEFAULT_HEADERS)
        self._pool: Optional[ThreadPoolExecutor] = None
        self._futures: List[Future] = []
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._done = 0
        self._bytes = 0
        self._failed: List[int] = []
        # 密钥 / init segment 下载失败清单（URL → 失败原因）。分片下全了但密钥没拿到
        # 时，加密流解不了密；调用方（download.save_as_mp4）靠它给出可读报错，
        # 否则 ffmpeg 只会甩一句 "Invalid data found"，看不出是密钥的问题。
        self._key_errors: List[str] = []
        self._ready = threading.Event()      # 首片就位
        self._all_done = threading.Event()
        self._started_at: Optional[float] = None

    # ── 状态查询 ──────────────────────────────────────────────────────────
    @property
    def total(self) -> int:
        return len(self.stream.segments)

    @property
    def done(self) -> int:
        with self._lock:
            return self._done

    @property
    def ready_prefix(self) -> int:
        """开头**连续**就位的分片数 —— 播放器能不间断播到这里。

        并发下载是分片乱序完成的，`done` 只说明"总共下了几片"，不代表开头连续。
        起播必须看连续前缀：否则 seg00001 还在飞就开播，播放头立刻撞上文件空洞。
        重试耗尽的死链算作"可越过"，免得一片坏分片把前缀卡到超时。
        """
        with self._lock:
            n = 0
            for seg in self.stream.segments:
                if seg.downloaded or seg.error:
                    n += 1
                else:
                    break
            return n

    @property
    def bytes_done(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def failed_indices(self) -> List[int]:
        with self._lock:
            return list(self._failed)

    @property
    def key_errors(self) -> List[str]:
        """密钥 / init segment 下载失败的 URL 清单（空列表 = 全部就位）。"""
        with self._lock:
            return list(self._key_errors)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def elapsed(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    def progress_text(self) -> str:
        done, total = self.done, self.total
        mb = self.bytes_done / 1024 / 1024
        rate = self.bytes_done * 8 / self.elapsed / 1000 if self.elapsed > 0 else 0
        return f"预取 {done}/{total} 片（{mb:.1f}MB, {rate:.0f} kbps）"

    # ── 启动 / 取消 ───────────────────────────────────────────────────────
    def start(self) -> "Prefetcher":
        """开始下载：先落地 playlist 与密钥，再按顺序并发拉分片。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 1) 密钥与 init segment（EXT-X-KEY / EXT-X-MAP）必须先就位，否则解不了密
        #    失败不能静默：_download_to 返回 False 时记下来，让调用方给出可读报错
        #    （否则 ffmpeg 只会甩一句 "Invalid data found"，看不出是密钥的问题）。
        for abs_url, name in list(self.stream.keys.items()) + list(self.stream.maps.items()):
            if not self._download_to(abs_url, self.cache_dir / name):
                with self._lock:
                    self._key_errors.append(abs_url)

        # 2) 写本地 playlist（此时分片文件还不存在，播放器按需读取）
        (self.cache_dir / "index.m3u8").write_text(
            render_local_playlist(self.stream), encoding="utf-8"
        )

        # 3) 按顺序提交分片任务：线程池队列是 FIFO，所以大致顺序下载、多片在飞，
        #    播放头前面的分片优先就位
        self._started_at = time.time()
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="prefetch"
        )
        for seg in self.stream.segments:
            if self._cancel.is_set():
                break
            self._futures.append(self._pool.submit(self._fetch_segment, seg))
        return self

    def stop(self, grace: float = 3.0) -> None:
        """取消尚未开始的下载，并等在飞的分片收尾。不删除已下载文件。

        `grace` 是等待在飞任务的宽限秒数。必须等：`shutdown(wait=False)` 只取消
        排队中的任务，已经在 `sess.get()` 里的那几个会继续跑完并 `tmp.replace(dest)`。
        若调用方紧接着清理缓存目录，这些迟到写入会把文件"复活"，留下删不掉的残留
        （实测：stop() 后缓存目录里剩一个完整分片）。
        """
        self._cancel.set()
        with self._lock:
            pool, self._pool = self._pool, None
            futures = list(self._futures)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if grace <= 0:
            return
        deadline = time.time() + grace
        for fut in futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if fut.done():
                continue
            try:
                fut.result(timeout=remaining)
            except Exception:
                pass

    def wait_all(self, timeout: Optional[float] = None) -> bool:
        """等所有分片下完；返回是否全部成功。"""
        return self._all_done.wait(timeout) if timeout else (self._all_done.wait() or True)

    def wait_for_segments(self, count: int, timeout: float = 60.0) -> bool:
        """阻塞直到开头**连续** `count` 片就位（或超时/取消）。

        用 ready_prefix 而不是 done：并发下分片乱序完成，`done >= count` 时
        开头可能还缺片，播放器一起来就撞空洞卡顿。
        """
        count = max(1, min(count, self.total))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready_prefix >= count:
                return True
            if self._cancel.is_set():
                return self.ready_prefix >= count
            if self._all_done.is_set():
                return self.ready_prefix >= count
            time.sleep(0.05)
        return self.ready_prefix >= count

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _download_to(self, url: str, dest: Path,
                     byterange: Optional[Tuple[int, int]] = None) -> bool:
        """下载单个 URL 到 dest（带重试），成功返回 True。

        `byterange` 给了 (偏移, 长度) 就发 Range 请求只取这段字节：
        正常服务器回 206 且 body 恰是该区间；个别服务器忽略 Range 回 200 全文件，
        则本地按 offset 切片取够 len 字节，保证落地文件仍是那一段。
        """
        rng_headers = {}
        if byterange:
            off, ln = byterange
            rng_headers["Range"] = f"bytes={off}-{off + ln - 1}"
        for attempt in range(self.retries + 1):
            if self._cancel.is_set():
                return False
            try:
                resp = self._sess.get(url, timeout=self.timeout, headers=rng_headers)
                if resp.status_code in (200, 206) and resp.content:
                    data = resp.content
                    if byterange and resp.status_code == 200:
                        off, ln = byterange
                        data = data[off:off + ln]        # 服务器没理 Range，自己切
                    if data:
                        dest.write_bytes(data)
                        return True
            except requests.RequestException:
                pass
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        return False

    def _fetch_segment(self, seg: Segment) -> None:
        if self._cancel.is_set():
            return
        dest = self.cache_dir / seg.local_name
        if dest.exists() and dest.stat().st_size > 0:
            ok = True
            size = dest.stat().st_size
        else:
            tmp = dest.with_suffix(dest.suffix + ".part")
            ok = self._download_to(seg.url, tmp, byterange=seg.byterange)
            size = tmp.stat().st_size if ok and tmp.exists() else 0
            if ok:
                try:
                    tmp.replace(dest)          # 原子改名，避免播放器读到半个文件
                except OSError:
                    ok = False
            elif tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        with self._lock:
            if ok:
                seg.downloaded = True
                seg.size = size
                self._done += 1
                self._bytes += size
            else:
                seg.error = "download_failed"
                self._failed.append(seg.index)
            done, total, nbytes = self._done, self.total, self._bytes
            finished = done + len(self._failed) >= total

        if self.on_progress:
            try:
                self.on_progress(done, total, nbytes)
            except Exception:
                pass
        if done == 1:
            self._ready.set()
        if finished:
            self._all_done.set()


def cleanup_dir(path: Path, retries: int = 3, delay: float = 0.2) -> None:
    """删除缓存目录，并确认真的删干净了。

    要重试是因为下载线程可能还在收尾：rmtree 成功返回后，迟到的
    `tmp.replace(dest)` 会把文件"复活"，留下一个删不掉的残留分片。
    所以每轮删完都检查目录是否还在，不在才算成功。
    失败最终静默（缓存目录本来就在 temp 下，系统会清）。
    """
    for attempt in range(max(1, retries)):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
        if not path.exists():
            return
        if attempt < retries - 1:
            time.sleep(delay * (attempt + 1))


def human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f}{unit}" if unit == "B" else f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}GB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
