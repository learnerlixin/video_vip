"""autonext.py —— 播完当前集自动切下一集（`x` 指令 / `--auto-next`）。

## 两条判定路径

**权威信号**（embed 后端的自有播放页 + hls.js）：`<video>` 在我们自己的页面里，
浏览器派发的 `ended` 是真事件，页面通过本地 HTTP 服务回传给 Python。
后端实现 `end_confirmed()` 暴露这个信号，拿到就**直接算播完**，不看耗时。

**启发式**（本地播放器进程 mpv / ffplay / prefetch）：没有事件可收，
只能自己判断「这一集播完了没」，用**双信号**，两个都满足才算播完：

    1. `backend.finished_naturally()` —— 进程自己退出了，且不是被 stop() 打断
    2. `elapsed >= 阈值`             —— 耗时接近预期时长，排除秒退

## 为什么启发式不能只看「播放器进程退出了」

实测（ffplay 9.0.2，见项目记录）：

    正常播完 2s 视频   rc=0   用时 2.6s
    文件损坏           rc=0   用时 0.4s     ← 退出码和「播完」一模一样
    文件不存在         rc=0   用时 0.4s     ← 同上
    terminate()/kill() rc=1

退出码根本区分不了「播完」和「秒退失败」。所以判定必须加上耗时这一条。
反过来，权威信号那条路**不该**套耗时启发式：真事件没有这个歧义，
套了反而会因为「时长探测还没回来」把正常播完误判成秒退。

## 为什么不能看 is_alive()

`PrefetchBackend.is_alive()` 的实现是「播放器进程活着 **or** 后台分片还没下完」。
播放器已经退出、后台仍在下分片时它照样返回 True。若拿它当「还在播」的判据，
prefetch 后端下这一集**永远不会**自动切下一集（实测复现过）。
所以判定只看 `finished_naturally()` —— 它的契约明确：进程自己退出且未被
`stop()` 打断才为 True，播放中 / 被打断 / 未起播一律 False。

## 为什么要防连跳

如果某个解析源整批地址都失效，每集都会秒退 → 双信号里的第 2 条挡住大部分，
但万一时长拿不到（退化成绝对下限判定），就可能一路跳到最后一集。
所以连续多次「极短播放」后直接停用自动连播并告警，而不是默默跳完整部剧。

## 线程模型

交互循环阻塞在 `ui._input()` 上，所以监视器必须在后台线程跑。
换集动作通过 `advance()` 回调交回 CLI，两边共用一把锁，避免和手动换集打架。

`advance()` 只是「请求切集」，真正改集号的是 CLI 主循环，两者之间有延迟。
这段延迟里必须挂起判定（`_pending_switch` 门闩），否则会对上一个已退出的进程
重复判定 → 重复发切集请求。实测旧逻辑在 1.5s 切集延迟下会产生
`[1,1,1,1,2,2,2,2,3]` 这样的重复请求。

监视线程**无条件启动**（哪怕当前后端不支持判定）：embed 后端在 `play()`
之前还没起本地服务，那一刻 `supported` 是 False，起播之后才变 True。
若因为「此刻不支持」就不启动线程，之后没人盯着「播完了没」，自动连播会静默失效。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional

# 判定「真的播完」的耗时阈值：
#   拿得到预期时长 → 至少要播到它的这个比例（起播有 ~0.5s 开销，短片段会超过 1.0）
#   拿不到预期时长 → 退化成绝对下限，避免把秒退当成播完
MIN_RATIO = 0.6
MIN_ABS_SECONDS = 20.0

# 连续这么多次「秒退」就停用自动连播（多半是解析源整批失效）。
# 「秒退」的判定不另设阈值：耗时没达到 _looks_finished() 的门槛就算秒退。
MAX_RAPID_FAILURES = 3

POLL_INTERVAL = 1.0          # 轮询间隔（秒）

# 发出切集请求后，等主循环真的把集号切过去的最长时间。
# 超时就认为这次切集没生效，按当前集重新计时，而不是一直干等。
SWITCH_GRACE = 30.0

# _looks_finished() 的三态结果
_STILL_PLAYING = "playing"    # 还在播，本轮不判定
_FINISHED = "finished"        # 真的播完了
_FAILED_SHORT = "short"       # 进程退了但耗时太短 → 这一集是坏的


class AutoNext:
    """监视当前集是否播完，播完就切下一集。

    用法：
        an = AutoNext(
            backend=result.backend,
            get_state=lambda: (result.episodes, result.index),
            advance=lambda target: switch_to(target),   # 由 CLI 提供，内部要加锁
            notify=lambda text: ui.toast(text, False),
        )
        an.start()
        ...
        an.stop()
    """

    def __init__(
        self,
        backend: Any,
        get_state: Callable[[], Any],
        advance: Callable[[int], bool],
        notify: Optional[Callable[[str], None]] = None,
        poll_interval: float = POLL_INTERVAL,
        min_ratio: float = MIN_RATIO,
        min_abs_seconds: float = MIN_ABS_SECONDS,
        max_rapid_failures: int = MAX_RAPID_FAILURES,
        enabled: bool = True,
    ):
        self.backend = backend
        self._get_state = get_state
        self._advance = advance
        self._notify_fn = notify
        self._poll = max(0.1, poll_interval)
        self._min_ratio = min_ratio
        self._min_abs = min_abs_seconds
        self._max_rapid = max(1, max_rapid_failures)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

        self.enabled = enabled
        self.rapid_failures = 0
        self.advanced_count = 0        # 成功自动切了几集
        self.last_reason = ""          # 最近一次判定结果（调试/`x` 指令展示用）
        # 本轮播放的起点，用于算「这一集播了多久」
        self._watch_index: Optional[int] = None
        self._watch_started: float = 0.0
        # 当前这一集还没做过判定。一集只判一次，否则轮询会反复累加
        # rapid_failures —— 一个坏地址 3 秒就能刷满「连续 3 集失败」的额度。
        self._verdict_pending: bool = False
        # 已发出切集请求、正在等主循环把集号真的切过去。
        # 等待期间不做任何判定，否则会对上一个已退出的进程重复判定 → 重复切集。
        self._pending_switch: Optional[int] = None
        self._pending_since: float = 0.0
        # 「当前后端不支持」只提示一次，避免每秒刷一行
        self._unsupported_notified: bool = False

    # ── 对外 ────────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    @property
    def supported(self) -> bool:
        """当前后端能不能判断「播完」。

        本地播放器（mpv/ffplay/prefetch）靠进程退出 + 耗时；embed 靠自有播放页
        回传的真 `ended` 事件。不行的是 browser 后端，以及 embed 回退到第三方
        解析页（跨域 iframe）或本地服务起不来（file:// 兜底页）的时候。
        """
        return bool(getattr(self.backend, "can_detect_end", False))

    def start(self) -> "AutoNext":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._resync(force=True)
        # 即使当前后端「不支持」也要把线程跑起来：
        # embed 后端在 play() 之前还没起本地服务，此刻 supported 是 False，
        # 起播之后才变 True。不启动线程的话，之后没人盯着「播完了没」，
        # 自动连播会静默失效（_tick 里会自己跳过不支持的后端并提示一次）。
        self._thread = threading.Thread(target=self._loop, name="autonext", daemon=True)
        self._thread.start()
        if not self.supported:
            # 只有用户确实想要自动连播时才提示；默认关着就别拿这句话吵人
            # （之后用 x 打开时，toggle() 会给出同样的提示）。
            self._note_unsupported()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        # 等线程真的退出，否则紧接着 start() 会短暂存在两个监视线程。
        # 从监视线程自己内部调用时不能 join（会死锁），故加一道判断。
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._poll + 1.0)

    def toggle(self) -> bool:
        """开/关自动连播，返回切换后的状态（True=开）。`x` 指令用。

        重新开启时要清掉攒下的失败计数与门闩：否则「连续 3 集秒退」停用后，
        用户一开就立刻又被判停用，等于开不了。

        但**不能**重置 `_watch_started`：关掉期间监视线程仍在跟踪计时，
        这一集已经看了多久是真实信息。重置了反而会把「已经看了 40 分钟」
        变成「刚开始看」，白白多等一集；更糟的是若此刻进程恰好已退出，
        会被判成秒退。
        """
        with self._lock:
            self.enabled = not self.enabled
            on = self.enabled
            need_resync = False
            if on:
                self.rapid_failures = 0
                self._pending_switch = None
                self._unsupported_notified = False
                # 只在「压根还没开始跟踪」时补一个起点，其余情况沿用现有计时
                need_resync = self._watch_index is None
        # _resync 会回调使用方给的 _get_state()，放到锁外做，
        # 免得使用方的回调里再想拿锁时死锁。
        if on and need_resync:
            self._resync(force=True)
        if on and not self.supported:
            # 走同一条提示路径：_tick 里也用它，避免同一件事说两遍
            self._unsupported_notified = False
            self._note_unsupported()
        return on

    def set_backend(self, backend: Any) -> None:
        """换后端后调用（`s` 切解析源、`m` 重选影片都可能换掉 backend）。

        新后端可能从「不支持」变成「支持」（例如 browser → ffplay，或 embed
        从 file:// 兜底切到本地服务），所以这里要顺带把监视线程补起来；
        反之则让它空转（_tick 会自己跳过并提示一次）。
        """
        with self._lock:
            self.backend = backend
            # 后端换了，旧的计时、门闩与「不支持」提示全部作废
            self._pending_switch = None
            self._watch_index = None
            self._watch_started = 0.0
            self._verdict_pending = True
            self._unsupported_notified = False
        # 线程一律保持运行（同 start()）：supported 可能此刻为 False、稍后转 True
        if self._thread is None:
            self.start()
        else:
            self._resync(force=True)

    def note_manual_switch(self) -> None:
        """手动换集后调用：重置计时，避免把上一集的时长算到新一集头上。"""
        self._resync(force=True)

    def status_text(self) -> str:
        with self._lock:
            state = "开" if self.enabled else "关"
            if not self.supported:
                return "自动连播：不可用（当前后端无法判断播完）"
            extra = ""
            if self.rapid_failures:
                extra = f"，极短播放 {self.rapid_failures} 次"
            if self.advanced_count:
                extra += f"，已自动切 {self.advanced_count} 集"
            return f"自动连播：{state}{extra}"

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _notify(self, text: str) -> None:
        if self._notify_fn:
            try:
                self._notify_fn(text)
            except Exception:
                pass

    def _note_unsupported(self) -> None:
        """后端不支持判「播完」时提示一次（不每秒刷屏）。"""
        with self._lock:
            if self._unsupported_notified or not self.enabled:
                return
            self._unsupported_notified = True
        self._notify("⚠ 当前播放后端无法判断「播完」，自动连播不会生效"
                     "（第三方解析页是跨域 iframe，收不到 ended 事件）")

    def _page_gone(self) -> bool:
        """embed：播放页被用户关掉了 → 换集请求发出去也没人执行。

        判定完全交给后端的 `page_closed()`：主信号是页面 `pagehide` 时发的
        sendBeacon 信标（关标签页/关浏览器都能送达），兜底才是「心跳长时间静默」
        —— 兜底阈值取得很宽松，因为浏览器会节流后台标签页的定时器，太紧会把
        「正常播放但标签页在后台」误判成已关闭。细节见 EmbedServer.page_closed。
        本地播放器后端没有这个方法，返回 False（它们靠进程退出表达「播完」）。
        """
        checker = getattr(self.backend, "page_closed", None)
        if not callable(checker):
            return False
        try:
            return bool(checker()) and self.enabled
        except Exception:
            return False

    def _resync(self, force: bool = False) -> None:
        """记录当前正在看的是哪一集、从什么时候开始看的。

        必须同时把 `_verdict_pending` 置回 True：手动换集后这一集是全新的，
        要重新参与判定，否则会被上一集的「已判过」标记挡住，再也不自动切了。
        """
        try:
            eps, idx = self._get_state()
        except Exception:
            return
        with self._lock:
            if force:
                # 手动切集：把「等自动切集完成」的门闩一并清掉，
                # 否则用户手动切到某集后，监视器还以为在等上一次自动切集。
                self._pending_switch = None
            if force or idx != self._watch_index:
                self._watch_index = idx
                self._watch_started = time.time()
                self._verdict_pending = True

    def _expected(self) -> Optional[float]:
        try:
            dur = self.backend.expected_duration()
        except Exception:
            return None
        try:
            dur = float(dur) if dur else None
        except (TypeError, ValueError):
            return None
        return dur if dur and dur > 0 else None

    def _looks_finished(self) -> str:
        """三态判定：还在播 / 真播完 / 秒退失败。

        ## 两条路：权威信号优先，启发式兜底

        **权威信号** `end_confirmed()`：后端能拿到浏览器的真 `ended` 事件
        （embed 后端的自有页面 + hls.js）。这时**直接算播完**，不看耗时 ——
        下面那套「耗时 ≥ 时长 60%」的启发式是为「ffplay 退出码区分不了播完与
        秒退」设计的，真事件没有这个歧义，套启发式反而会因为「时长探测还没回来」
        而误判成秒退。

        **启发式**（本地播放器进程）：双信号
            1. `finished_naturally()` —— 进程自己退出了，且不是被 stop() 打断
            2. `elapsed >= 阈值`       —— 耗时接近预期时长，排除秒退

        注意启发式这条路**只看 finished_naturally()，不看 is_alive()**：
        PrefetchBackend.is_alive() 的实现是「播放器进程活着 **or** 后台分片还没下完」，
        播放器进程已经退出、后台仍在下分片时它照样返回 True（player.py 里
        专门写了「自动连播不能看它」）。若拿 is_alive() 当「还在播」的判据，
        prefetch 后端下这一集永远不会自动切下一集。
        而 finished_naturally() 的契约是明确的：进程自己退出且未被 stop()
        打断才为 True，播放中 / 被打断 / 未起播一律 False，足够单独作判据。
        """
        # 权威信号：后端拿到了真 ended 事件（embed 自有页面）
        try:
            confirm = getattr(self.backend, "end_confirmed", None)
            if callable(confirm) and confirm():
                self.last_reason = "收到播放页的真实 ended 事件"
                return _FINISHED
        except Exception:
            pass

        try:
            if not self.backend.finished_naturally():
                # 还在播、被 stop() 打断（用户切集/退出）、或压根没起播
                self.last_reason = "未自然退出（仍在播放/被打断/未起播）"
                return _STILL_PLAYING
        except Exception as exc:
            self.last_reason = f"查询后端状态失败：{type(exc).__name__}"
            return _STILL_PLAYING

        with self._lock:
            started = self._watch_started
        elapsed = (time.time() - started) if started else 0.0
        expected = self._expected()

        if expected:
            need = expected * self._min_ratio
            verdict = elapsed >= need
            self.last_reason = (f"耗时 {elapsed:.1f}s / 预期 {expected:.0f}s"
                                f"（阈值 {need:.0f}s）→ {'算播完' if verdict else '太短，判为失败'}")
        else:
            # 拿不到时长：只能用绝对下限，宁可漏判也不要疯狂跳集
            verdict = elapsed >= self._min_abs
            self.last_reason = (f"耗时 {elapsed:.1f}s，无预期时长"
                                f"（下限 {self._min_abs:.0f}s）→ {'算播完' if verdict else '太短，判为失败'}")

        return _FINISHED if verdict else _FAILED_SHORT

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self._tick()
            except Exception:
                # 监视线程绝不能因为一次异常就死掉，否则用户以为还开着
                pass

    def _tick(self) -> None:
        if not self.supported:
            self._note_unsupported()
            return

        # embed 后端：播放页被用户关掉了。继续判定毫无意义（换集请求发出去
        # 也没人执行），而且会一路跳到最后一集。明确停用并告知。
        if self._page_gone():
            self.enabled = False
            self._notify("⚠ 播放页已关闭，自动连播停用（重新起播后输入 x 可再开）")
            return

        try:
            eps, idx = self._get_state()
        except Exception:
            return
        if not eps:
            return

        with self._lock:
            enabled = self.enabled
            pending = self._pending_switch
            pending_since = self._pending_since

        # 关掉自动连播期间也要继续跟踪「在看哪一集、从何时开始看」。
        # 否则用户关着连播手动换了几集、再打开时，计时起点还停在老集上，
        # 会拿上一集的耗时去判这一集 —— 要么立刻误判播完，要么误判秒退。
        if not enabled:
            with self._lock:
                self._pending_switch = None
                if idx != self._watch_index:
                    self._watch_index = idx
                    self._watch_started = time.time()
                    self._verdict_pending = True
            return

        # 正在等主循环把上一次切集请求落地：这期间一律不判定。
        # 否则会对上一个已经退出的进程重复判定 → 重复发切集请求。
        if pending is not None:
            if idx == pending:
                # 切集生效了，从这一集重新计时
                with self._lock:
                    self._pending_switch = None
                    self._watch_index = idx
                    self._watch_started = time.time()
                    self._verdict_pending = True
                return
            if idx != self._watch_index:
                # 用户手动切到了别的集，以用户为准
                with self._lock:
                    self._pending_switch = None
                    self._watch_index = idx
                    self._watch_started = time.time()
                    self._verdict_pending = True
                return
            if time.time() - pending_since > SWITCH_GRACE:
                # 等了太久还没切过去：放弃这次请求，按当前集重新计时
                with self._lock:
                    self._pending_switch = None
                    self._watch_started = time.time()
                    self._verdict_pending = True
                self._notify("⚠ 自动切集迟迟未生效，已按当前集重新计时")
            return

        # 集数变了（用户手动切集）→ 重新计时，本轮不判定
        with self._lock:
            if idx != self._watch_index:
                self._watch_index = idx
                self._watch_started = time.time()
                self._verdict_pending = True
                return
            # 本集已经判过了（正在等切集或已停用），不要重复计数
            if not self._verdict_pending:
                return

        verdict = self._looks_finished()
        if verdict == _STILL_PLAYING:
            return

        with self._lock:
            # 一集只判一次：否则轮询会反复累加 rapid_failures，
            # 一个坏地址 3 秒内就能把「连续 3 集失败」的额度刷满
            self._verdict_pending = False

        if verdict == _FAILED_SHORT:
            self._handle_short_failure(eps, idx)
            return
        self._handle_finished(eps, idx)

    def _handle_short_failure(self, eps: List[Any], idx: int) -> None:
        """这一集秒退（地址失效等）：计数，跳过它，连续太多次就停用。"""
        with self._lock:
            self.rapid_failures += 1
            rapid = self.rapid_failures
        name = self._ep_name(eps, idx)
        if rapid >= self._max_rapid:
            self.enabled = False
            self._notify(
                f"✗ 连续 {rapid} 集都秒退（如「{name}」），多半是解析源失效，"
                f"已停用自动连播（输入 x 可重开）"
            )
            return
        self._notify(f"⚠ 「{name}」秒退（{self.last_reason}），跳过")
        self._go_next(eps, idx, verb="跳过")

    def _handle_finished(self, eps: List[Any], idx: int) -> None:
        """真的播完了：清零失败计数，切下一集。"""
        with self._lock:
            self.rapid_failures = 0
        self._go_next(eps, idx, verb="自动切到")

    def _ep_name(self, eps: List[Any], idx: int) -> str:
        try:
            return eps[idx].get("name") or f"第{idx + 1}集"
        except Exception:
            return f"第{idx + 1}集"

    def _go_next(self, eps: List[Any], idx: int, verb: str) -> None:
        target = idx + 1
        if target >= len(eps):
            self._notify(f"✓ 已是最后一集（共 {len(eps)} 集），自动连播结束")
            self.enabled = False
            return

        name = self._ep_name(eps, target)
        ok = False
        try:
            ok = bool(self._advance(target))
        except Exception as exc:
            self._notify(f"✗ 自动切集失败（{type(exc).__name__}）：{exc}")
        if not ok:
            # 切集失败就别再重试了，否则会每秒刷一次
            self.enabled = False
            self._notify("✗ 自动切集未成功，已停用自动连播（输入 x 可重开）")
            return

        with self._lock:
            if verb == "自动切到":
                self.advanced_count += 1
            # 挂起门闩：等主循环把集号切到 target 之后再恢复判定。
            # 这里不能直接改 _watch_index —— 主循环可能还没切，
            # 那样下一轮 _tick 会看到旧集号、重置状态，对同一个已退出的
            # 进程再判一次，于是重复切集（坏地址时会疯狂跳集）。
            self._pending_switch = target
            self._pending_since = time.time()
            self._verdict_pending = False
        self._notify(f"⏭ {verb} {name}")
