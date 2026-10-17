# -*- coding: utf-8 -*-
"""窗口与周期任务（WidgetApp）与命令行入口（Options、parse_args）。

从 usage_widget.py 拆出的模块之一，行为与原单文件实现一致。测试对 usage_widget
打的补丁经 _entry() 取回入口模块，所以这里不直接引用被测试替换的名字。
"""

import collections
import math
import os
import sys
import threading
import time

from usage_bar.data import (
    APP_REFRESH_POLL_MS,
    APP_REFRESH_WAIT_SECONDS,
    CAPTION_ASKING,
    CAPTION_NO_FILE,
    CODEX_PING_MODEL,
    CODEX_PING_POLL_MS,
    AppRefresher,
    CodexPinger,
    CodexReader,
    MODE_AUTO,
    MODE_MANUAL,
    POS_FILE_NAME,
    SNAPSHOT_NAME,
    SnapshotReader,
    Win,
    _snapshot_observed_at,
    app_refresh_caption,
    codex_ping_caption,
    valid_ping_model,
)
from usage_bar.render import (
    INITIAL_WINDOW_PX,
    _font_state,
    build_display,
    caption_text,
    compute_layout,
    offset_from_right_edge,
    premultiply_bgra,
    window_height,
)
from usage_bar.win32 import (
    CHILDID_SELF,
    EVENT_OBJECT_LOCATIONCHANGE,
    EVENT_OBJECT_REORDER,
    EVENT_SYSTEM_FOREGROUND,
    HIDE_NOTIFICATION_STATES,
    MA_NOACTIVATE,
    MAX_TIMER_MS,
    MENU_QUIT,
    MENU_REFRESH,
    MENU_SNAP,
    MK_LBUTTON,
    OBJID_CLIENT,
    OBJID_WINDOW,
    SUPPORTED_EDGES,
    SW_HIDE,
    SW_SHOWNOACTIVATE,
    SYSTEM_CHANGE_MESSAGES,
    WM_APP_RECREATE,
    WM_APP_Z_ORDER,
    WM_CAPTURECHANGED,
    WM_CLOSE,
    WM_DESTROY,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEACTIVATE,
    WM_MOUSEMOVE,
    WM_RBUTTONUP,
    WM_TIMER,
    WNDPROC,
)


def _entry():
    """入口模块（usage_widget 或 __main__）：测试对入口模块打的补丁必须在这里仍然可见。"""
    return sys.modules.get("usage_widget") or sys.modules["__main__"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SNAPSHOT_POLL_MS = 15000
WINDOW_CHECK_MS = 2000
OWNER_RECREATE_SECONDS = 30.0
THEME_POLL_MS = 60000
GDI_TICK_MS = 20
GDI_SETTLE_MS = 1000
WATCHDOG_SECONDS = 10.0
WATCHDOG_EXIT_CODE = 3

DRAG_THRESHOLD_PX = 3
MOVE_THRESHOLD_PX = 1
MAX_REFRESH_PASSES = 3

TIMER_CHECK = 1
TIMER_POLL = 2
TIMER_THEME = 3
TIMER_EXIT = 4
TIMER_GDI_BEGIN = 5
TIMER_GDI_TICK = 6
TIMER_GDI_END = 7
TIMER_FLASH = 8
TIMER_CAPTION = 9
TIMER_PING = 10
TIMER_APP = 11
FLASH_MS = 300
FLASH_ALPHA_SCALE = 0.35
# Refresh now 之后反馈文字显示的毫秒数。
CAPTION_MS = 2500


# ---------------------------------------------------------------------------
# 窗口与周期任务
# ---------------------------------------------------------------------------

def _wnd_proc(hwnd, msg, wparam, lparam):
    """WNDPROC 的唯一入口：任何异常都不能逃出回调。"""
    try:
        app = _entry()._APP
        if app is None:
            return 0
        return app.window_proc(hwnd, msg, wparam, lparam)
    except BaseException:
        return 0


# 回调对象必须放在模块级变量里：窗口类注册后系统还会持有函数指针，对象一旦被回收
# 就会在下一条消息上崩溃
_WNDPROC_REF = WNDPROC(_wnd_proc)


# Refresh now 里一个提供方这一路（发出请求、定时轮询、收口）的固定参数。Codex 的 ping 与
# 桌面会话的请求各是一路，共用 WidgetApp 的 _start_slot / _poll_slot / _stop_slot /
# _drop_slot；驱动对象（CodexPinger、AppRefresher）各管各的内部。两路不同的地方只有这张
# 表、各自的 _finish_*，以及 Codex 只在数据源可用时才发起。驱动对象和“点击时的观测时间”
# 记的是属性名而不是对象：--no-* 会让驱动对象是 None，测试也会整个换掉它，所以每次现取。
RefreshSlot = collections.namedtuple("RefreshSlot", [
    "index",         # 反馈文字里的下标：0 是 Claude，1 是 Codex
    "driver",        # WidgetApp 上驱动对象的属性名
    "busy",          # 驱动对象上“已发出、尚未收口”的属性名
    "before",        # WidgetApp 上存点击那一刻观测时间的属性名，收口时拿它判断 new 还是 same
    "timer_id", "poll_ms",             # 轮询计时器的 id 与间隔
    "caption",       # 反馈文字函数 (读数文字, 状态) -> 文字
    "log_kind",      # 错误日志里的类别
    "error_prefix",  # start() 的原因以它开头：请求没能发出
    "error_state",   #   这时反馈文字的状态
    "drop_state",    # 轮询计时器建不起来、只好放弃这一路时反馈文字的状态
    "drop_note",     #   这时记进错误日志的话
    "skip_notes",    # {start() 的原因: 日志里的话}：只记日志，文字不动
])

CODEX_SLOT = RefreshSlot(
    index=1, driver="pinger", busy="running", before="_ping_before",
    timer_id=TIMER_PING, poll_ms=CODEX_PING_POLL_MS, caption=codex_ping_caption,
    log_kind="CodexPing", error_prefix="spawn_error:", error_state="failed",
    drop_state="failed", drop_note="poll timer unavailable; ping abandoned",
    skip_notes={"no_exe": "codex.exe not found; ping skipped"})
APP_SLOT = RefreshSlot(
    index=0, driver="app_refresher", busy="waiting", before="_app_before",
    timer_id=TIMER_APP, poll_ms=APP_REFRESH_POLL_MS, caption=app_refresh_caption,
    log_kind="AppRefresh", error_prefix="write_error:", error_state="not_sent",
    drop_state="no_session", drop_note="poll timer unavailable; request abandoned",
    skip_notes={})
REFRESH_SLOTS = (CODEX_SLOT, APP_SLOT)  # 窗口重建后按这个顺序补计时器


class WidgetApp:
    """窗口、定位与周期任务，全部在主线程。

    每个消息处理都整体 try/except，异常记日志后照常继续；计时器由系统重复投递，
    一次失败不影响下一次。
    """

    def __init__(self, opts, w32, log):
        self.opts = opts
        self.w32 = w32
        self.log = log
        self.data_dir = opts.data_dir
        self.reader = SnapshotReader(os.path.join(self.data_dir, SNAPSHOT_NAME))
        if opts.no_codex:
            self.codex = None
        else:
            self.codex = CodexReader(opts.codex_home)
        # pinger 只在 Refresh now 时才会启动子进程；没有 Codex 数据源或被 --no-codex-ping 关掉时为 None。
        self.pinger = None
        if self.codex is not None and not opts.no_codex_ping:
            self.pinger = CodexPinger(
                self.data_dir,
                model=opts.codex_ping_model or CODEX_PING_MODEL,
                exe_override=opts.codex_bin,
                codex_home=opts.codex_home)
        # 只在 Refresh now 时才写请求文件；被 --no-app-refresh 关掉时为 None。
        # 与 Codex 是否启用无关：--no-codex 时 Claude 块照样能用。
        self.app_refresher = None if opts.no_app_refresh else AppRefresher(self.data_dir)
        # 同一种 Codex 原因只记一次；原因变回空串后，下次再出现才重新记。
        self._codex_log_reason = ""
        self.pos_path = os.path.join(self.data_dir, POS_FILE_NAME)
        self.mode, self.offset = _entry().load_position(self.pos_path)
        self.light = _entry().read_light_theme()
        self.hwnd = 0
        self.owner = 0
        self._recreating = False
        self._owner_recreated_at = None
        self._owner_recreate_suppressed = False
        # 有所有者创建失败过的任务栏句柄；0 表示没有这类失败。
        self._owner_failed_tray = 0
        self._exit_deadline = None
        self.scale = 1.0
        self.geom = None
        self.shown_display = None
        self.taskbar = None
        self.visible = False
        self.exiting = False
        self.timers = set()
        self.snapshot_override = None
        self.drag = None
        self.menu_open = False
        self.msg_taskbar_created = 0
        self._busy = False
        self._again = False
        self._reassert_pending = False
        self._z_order_pending = False
        self._gdi = {}
        self._watchdog = None
        self._flashing = False
        self._caption = None          # (截止时间, 各提供方反馈文字)，None 表示没有
        self._caption_before = None   # 只在 Refresh now 读数据源期间非 None：刷新前各提供方最新的 observed_at
        # 点 Refresh now 那一刻 Codex 最新的 observed_at，ping 结束后拿它判断 new 还是 same。
        self._ping_before = None
        # 点 Refresh now 那一刻 Claude 最新的 observed_at，确认回来后拿它判断 new 还是 same。
        self._app_before = None
        # 最近一次渲染的未变暗画面：(显示元组, RGBA 图)，None 表示还没渲染过；见 _frame_image。
        self._frame = None

    # 基础设施 ----------------------------------------------------------------

    def _safe(self, func, *args):
        """执行一次回调，异常记日志后吞掉，返回是否成功。"""
        try:
            func(*args)
            return True
        except Exception as exc:
            self._log_exception(exc)
            return False

    def _log_exception(self, exc):
        try:
            self.log.log_exception(exc)
        except Exception:
            pass

    def set_timer(self, timer_id, interval_ms):
        """退出开始之后不再建计时器，否则会在销毁窗口后又冒出消息。"""
        if self.exiting or not self.hwnd:
            return False
        if not self.w32.set_timer(self.hwnd, timer_id, interval_ms):
            self.log.log("SetTimer", "SetTimer failed for timer %d" % timer_id)
            return False
        self.timers.add(timer_id)
        if timer_id == TIMER_EXIT and self._exit_deadline is None:
            self._exit_deadline = time.monotonic() + max(10, min(int(interval_ms), MAX_TIMER_MS)) / 1000.0
        return True

    def kill_timer(self, timer_id):
        if timer_id in self.timers:
            self.timers.discard(timer_id)
            if self.hwnd:
                self.w32.kill_timer(self.hwnd, timer_id)

    def _kill_all_timers(self):
        for timer_id in list(self.timers):
            self.kill_timer(timer_id)

    # 窗口过程 ----------------------------------------------------------------

    def window_proc(self, hwnd, msg, wparam, lparam):
        """返回消息结果；没有处理的消息交给系统默认处理。绝不抛异常。"""
        try:
            handled, result = self._dispatch(msg, wparam)
            if handled:
                return result
        except Exception as exc:
            self._log_exception(exc)
        try:
            return self.w32.def_window_proc(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    def _dispatch(self, msg, wparam):
        if msg == WM_MOUSEACTIVATE:
            # 点击小窗不激活它，也就不抢焦点
            return True, MA_NOACTIVATE
        if msg == WM_TIMER:
            self.on_timer(wparam)
            return True, 0
        if msg == WM_APP_Z_ORDER:
            self.on_z_order_check()
            return True, 0
        if msg == WM_LBUTTONDOWN:
            self.on_left_down()
            return True, 0
        if msg == WM_MOUSEMOVE:
            self.on_mouse_move(wparam)
            return True, 0
        if msg == WM_LBUTTONUP:
            self.on_left_up()
            return True, 0
        if msg == WM_CAPTURECHANGED:
            self.on_capture_lost()
            return True, 0
        if msg == WM_RBUTTONUP:
            self.on_right_up()
            return True, 0
        if msg in SYSTEM_CHANGE_MESSAGES or (self.msg_taskbar_created and msg == self.msg_taskbar_created):
            # 只有任务栏重建才忘掉上次拒绝有所有者创建的句柄；DPI/显示/设置变化不清。
            if self.msg_taskbar_created and msg == self.msg_taskbar_created:
                self._owner_failed_tray = 0
            self.on_system_change()
            return True, 0
        if msg == WM_CLOSE:
            self.request_exit()
            return True, 0
        if msg == WM_DESTROY:
            self.on_destroy()
            return True, 0
        return False, 0

    def on_timer(self, timer_id):
        if self.exiting:
            return
        if timer_id == TIMER_CHECK:
            self._owner_check()
            self.refresh_view(reassert_topmost=True)
        elif timer_id == TIMER_POLL:
            self.poll_snapshot()
        elif timer_id == TIMER_THEME:
            self.poll_theme()
        elif timer_id == TIMER_EXIT:
            self.kill_timer(TIMER_EXIT)
            self.request_exit()
        elif timer_id == TIMER_GDI_BEGIN:
            self.kill_timer(TIMER_GDI_BEGIN)
            self._gdi["before"] = self._gdi_counts()
            self._gdi["index"] = 0
            self.set_timer(TIMER_GDI_TICK, GDI_TICK_MS)
        elif timer_id == TIMER_GDI_TICK:
            self._gdi_tick()
        elif timer_id == TIMER_GDI_END:
            self.kill_timer(TIMER_GDI_END)
            self._gdi_report()
            self.request_exit()
        elif timer_id == TIMER_FLASH:
            self.kill_timer(TIMER_FLASH)
            self._end_flash()
        elif timer_id == TIMER_CAPTION:
            self.kill_timer(TIMER_CAPTION)
            # 计时器到点就直接擦。系统计时器可能比墙钟略早触发，不能再拿截止时间去比。
            self._caption = None
            self.refresh_view()
        elif timer_id == TIMER_PING:
            self._on_ping_timer()
        elif timer_id == TIMER_APP:
            self._on_app_timer()

    def on_destroy(self):
        if self._recreating:
            return
        if self.exiting:
            self.w32.unwatch_window_events()
            self._kill_all_timers()
            self.hwnd = 0
            self.w32.post_quit()
            return
        # 所有者销毁时系统也会销毁小窗；等销毁消息返回后再通过线程消息重建。
        self.log.log("WindowDestroyed", "window destroyed by the system; recovering via thread message")
        self.timers.clear()
        self.hwnd = 0
        self.visible = False
        if not self.w32.post_thread_message(WM_APP_RECREATE):
            self.log.log("RecreateWindow", "PostThreadMessageW failed")
            self.w32.post_quit()

    def _create_widget_window(self):
        tray = self.w32.taskbar_hwnd()
        try:
            hwnd = self.w32.create_window(0, 0, INITIAL_WINDOW_PX, INITIAL_WINDOW_PX, tray or None)
        except OSError as exc:
            if not tray:
                raise
            self._log_exception(exc)
            # 先记成无所有者，避免创建过程中重入时按旧句柄再重建。
            self.owner = 0
            hwnd = self.w32.create_window(0, 0, INITIAL_WINDOW_PX, INITIAL_WINDOW_PX, None)
            self._owner_failed_tray = tray
            self.owner = 0
            return hwnd
        if tray:
            # 有所有者创建成功后，这个任务栏可以再试。
            self._owner_failed_tray = 0
        self.owner = tray
        return hwnd

    def _owner_check(self):
        """按创建时所有者与任务栏句柄有效性判断重建。

        开始菜单打开时，Windows 调整任务栏 band 会临时改变 GW_OWNER，只记诊断日志。
        """
        if (self.exiting or self._recreating or self._busy or self.menu_open
                or self.drag is not None or not self.hwnd):
            return
        tray = self.w32.taskbar_hwnd()
        if not tray:
            return
        # 这个任务栏已经拒绝过有所有者创建，同一句柄不再反复重建。
        if self.owner == 0 and tray == self._owner_failed_tray:
            return
        if not self.owner or tray != self.owner or not self.w32.is_window(self.owner):
            if self.owner == 0:
                cause = "unowned window, taskbar 0x%X" % tray
            elif tray != self.owner:
                cause = "taskbar replaced 0x%X -> 0x%X" % (self.owner, tray)
            else:
                cause = "owner 0x%X no longer valid, taskbar 0x%X" % (self.owner, tray)
            now = time.monotonic()
            if self._owner_recreated_at is not None and now - self._owner_recreated_at < OWNER_RECREATE_SECONDS:
                if not self._owner_recreate_suppressed:
                    self._owner_recreate_suppressed = True
                    self.log.log("OwnerRecreateSuppressed", "recreate deferred for 30 s: " + cause)
                return
            self._owner_recreated_at = now
            self._owner_recreate_suppressed = False
            self._recreate_window(cause)
        else:
            actual_owner = self.w32.window_owner(self.hwnd)
            if actual_owner != tray:
                self.log.log("OwnerMismatch", "GW_OWNER=0x%X expected 0x%X; no recreate" % (actual_owner, tray))

    def _recreate_window(self, reason):
        self.log.log("OwnerChanged", reason)
        self._recreating = True
        try:
            self._kill_all_timers()
            old = self.hwnd
            if self.w32.is_window(old):
                self.w32.destroy_window(old)
            self.hwnd = 0
        finally:
            self._recreating = False
        try:
            self.hwnd = self._create_widget_window()
        except OSError as exc:
            self._log_exception(exc)
            self.request_exit()
            return
        self._after_window_created()

    def _after_window_created(self):
        """新窗口建好之后的共用收尾：清掉上一扇窗口的显示状态，补上计时器，马上画一次。

        _recreate_window 与 on_thread_message 都走这里。
        """
        self.visible = False
        self.geom = None
        self.shown_display = None
        self._z_order_pending = False
        self._flashing = False
        self._caption = None
        self._start_timers(recreated=True)
        self.refresh_view(force=True, reassert_topmost=True)

    def on_thread_message(self, message):
        if message != WM_APP_RECREATE or self.exiting or self.hwnd:
            return
        try:
            self.hwnd = self._create_widget_window()
        except OSError as exc:
            self._log_exception(exc)
            self.w32.post_quit()
            return
        self._after_window_created()

    # 显示与定位 --------------------------------------------------------------

    def on_window_event(self, event, hwnd, object_id, child_id):
        """合并窗口事件，每批最多投递一次检查；对象内部的列表重排不相关。

        尺寸/位置变化只看顶层窗口自身（全屏进出）；光标与子控件的变化忽略。
        """
        if self.exiting or not self.hwnd or not hwnd or self._z_order_pending:
            return
        if event == EVENT_OBJECT_REORDER:
            if child_id != CHILDID_SELF:
                return
            if object_id != OBJID_WINDOW and not (
                    object_id == OBJID_CLIENT and hwnd == self.w32.desktop_hwnd()):
                return
        elif event == EVENT_OBJECT_LOCATIONCHANGE:
            if object_id != OBJID_WINDOW or child_id != CHILDID_SELF:
                return
        elif event != EVENT_SYSTEM_FOREGROUND:
            return
        self._z_order_pending = True
        if not self.w32.post_z_order_check(self.hwnd):
            self._z_order_pending = False

    def on_z_order_check(self):
        self._z_order_pending = False
        if self.exiting or not self.hwnd:
            return
        # 事件既驱动层级恢复也驱动可见性复核（全屏进出即时隐藏/恢复）。
        # 层级恢复的代价守卫在 _refresh_once 内部（新显示看 topmost_allowed，
        # 已显示才问 taskbar_above）；自身事件被 SKIPOWNPROCESS 滤掉，不成环。
        self.refresh_view(reassert_topmost=True)

    def _current_snapshot(self):
        if self.snapshot_override is not None:
            return self.snapshot_override
        return self.reader.snapshot

    def refresh_view(self, force=False, reassert_topmost=False):
        """重新判断可见性、重算尺寸与位置，必要时重绘；所有触发源共用这一条路径。

        重入保护：UpdateLayeredWindow、SetWindowPos 这类调用会在内部处理别的线程
        发来的消息（例如系统广播的 WM_SETTINGCHANGE），那条消息又会走到这里；嵌套的
        请求只记个标记，等当前这一轮做完再补一轮。
        """
        if self.exiting or not self.hwnd:
            return
        self._reassert_pending |= reassert_topmost
        if self._busy:
            self._again = True
            return
        self._busy = True
        try:
            passes = 0
            self._again = True
            while self._again and passes < MAX_REFRESH_PASSES:
                self._again = False
                passes += 1
                reassert = self._reassert_pending
                self._reassert_pending = False
                self._refresh_once(force and passes == 1, reassert)
        finally:
            self._busy = False

    def _refresh_once(self, force, reassert_topmost):
        if self.menu_open or self.drag is not None:
            # 菜单或拖动进行中不挪窗口、不隐藏窗口
            return
        info = self.w32.taskbar_pos()
        if info is None:
            self.log.log("TaskbarPos", "taskbar position query failed")
            info = self.taskbar
            if info is None:
                self._hide()
                return
        self.taskbar = info
        rect, edge = info
        if edge not in SUPPORTED_EDGES:
            self.log.log("TaskbarEdge", "unsupported taskbar edge %d; widget hidden" % edge)
            self._hide()
            return
        if self.w32.taskbar_autohide():
            self._hide()
            return
        notification_state = self.w32.notification_state()
        if (notification_state in HIDE_NOTIFICATION_STATES
                or self.w32.fullscreen_window_present()):
            self._hide()
            return
        scale = self.w32.scale_for(self.hwnd)
        height = window_height(rect, scale)
        theme = "light" if self.light else "dark"
        # 句柄自测改写 snapshot_override 时仍走单提供方，避免把 Codex 画进自测画面。
        if self.snapshot_override is not None:
            codex_snapshot = None
            codex_available = False
        else:
            codex_snapshot = None if self.codex is None else self.codex.snapshot
            codex_available = bool(self.codex and self.codex.available)
        now = time.time()
        captions = None
        if self._caption is not None:
            # 截止时间是兜底：定时器没建成，或到点那次重绘被菜单、拖动、隐藏挡住时，
            # 之后两秒一次的 TIMER_CHECK 会因为截止时间已过而重绘擦掉反馈文字。
            deadline, texts = self._caption
            if now < deadline:
                captions = texts
            else:
                self._caption = None
        # asking 在 ping 或等待桌面会话期间持续显示，不受 2.5 秒反馈到期影响。
        # 每一路只因自己有已发出、尚未收口的请求（_slot_busy）而把自己那一项（slot.index）
        # 变成 asking，各路互不影响，可以同时成立。
        for slot in _entry().REFRESH_SLOTS:
            if self._slot_busy(slot):
                base = list(captions) if captions else []
                base = (base + ["", ""])[:2]
                base[slot.index] = CAPTION_ASKING
                captions = tuple(base)
        display = build_display(
            self._current_snapshot(), now, theme, scale, height,
            codex=codex_snapshot, codex_available=codex_available, captions=captions)
        tray = self.w32.tray_notify_rect() if self.mode == MODE_AUTO else None
        geom = compute_layout(rect, edge, scale, display.width, self.mode, self.offset, tray)
        self.scale = scale
        self._apply(display, geom, force)
        shown_now = not self.visible
        if shown_now:
            self.w32.show_window(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True
        # 只在需要时恢复置顶层级：任务栏确实盖在小窗上方，判据与 on_z_order_check 相同
        # （taskbar_above：任务栏在上方，且没有重叠的系统面板）。以前只要允许就每 2 秒无条件
        # 置顶一次，会和屏幕键盘、录屏浮层这类置顶窗口来回抢层级，还让系统里每个
        # EVENT_OBJECT_REORDER 钩子都被通知一遍。例外：小窗刚从隐藏变为显示（例如全屏应用
        # 退出之后）时它可能落在置顶带的任何位置，所以只要允许就无条件置顶一次；这一轮
        # 是不是 TIMER_CHECK 带着 reassert_topmost 来的都一样，否则恰好由 15 秒轮询重新
        # 显示的小窗就要等到任务栏盖住它才会被抬起来。
        if shown_now:
            raise_window = self.w32.topmost_allowed(self.hwnd)
        elif reassert_topmost:
            raise_window = self.w32.taskbar_above(self.hwnd)
        else:
            raise_window = False
        if raise_window:
            self.w32.keep_topmost(self.hwnd)

    def _frame_image(self, display):
        """显示元组对应的未变暗画面，与上一次相同的元组直接复用。

        元组相等画面就相同（见 build_display），所以点 Refresh now 时变暗和恢复用的是同一个
        元组，只渲染第一次，之后只重新预乘并提交；没有请求改变画面时，前面那次带反馈文字的
        重绘也是这个元组。缓存里是未变暗的原图：变暗由调用方用 dim_image 另取新图，绝不
        改它。渲染抛错时缓存保持原样。
        """
        frame = self._frame
        if frame is not None and frame[0] == display:
            return frame[1]
        image = _entry().render_display(display)
        self._frame = (display, image)
        return image

    def _apply(self, display, geom, force):
        """内容或尺寸变了就重绘并提交位图（同时设定位置与尺寸），只是位置变了才移动。

        重绘时画面按显示元组缓存（_frame_image），变暗只作用在缓存图的副本上。
        """
        x, y, width, height = geom
        old = self.geom
        if force or old is None or old[2:] != (width, height) or display != self.shown_display:
            image = self._frame_image(display)
            if self._flashing:
                image = _entry().dim_image(image, FLASH_ALPHA_SCALE)
            data = premultiply_bgra(image)
            self.w32.update_layered(self.hwnd, x, y, width, height, data)
            self.shown_display = display
            self.geom = geom
            if _font_state["fallback"]:
                self.log.log("FontFallback", "segoeui.ttf not loaded; using the default font")
        elif abs(x - old[0]) > MOVE_THRESHOLD_PX or abs(y - old[1]) > MOVE_THRESHOLD_PX:
            self.move_to(x, y)

    def move_to(self, x, y):
        """只移动窗口，SetWindowPos 成功才记下新位置，返回是否成功。

        失败时 geom 保持原值，下一轮刷新仍会看到位置差异并重试；记下没移成的位置会让
        重试一直被压住，直到位置再次变化。
        """
        moved = bool(self.w32.move_window(self.hwnd, x, y))
        if moved:
            self.geom = (x, y, self.geom[2], self.geom[3])
        return moved

    def _hide(self):
        if self.visible:
            self.w32.show_window(self.hwnd, SW_HIDE)
            self.visible = False

    # 周期任务 ----------------------------------------------------------------

    def poll_snapshot(self, force=False):
        """读两个数据源，有变化或有反馈文字要挂上时再重绘。TIMER_POLL 与 Refresh now 共用。

        Refresh now 会先把刷新前的最新观测时间放进 self._caption_before。读完数据源后
        立刻取走并清掉这个标记，再算出反馈文字；第一次重绘就带着它。
        """
        changed = self.reader.refresh(force=force)
        reason = self.reader.last_reason
        if reason and reason != "missing":
            self.log.log("SnapshotInvalid", reason)
        codex_changed = self._poll_codex(force)
        # refresh_view 内部的 UpdateLayeredWindow / SetWindowPos 会处理别的线程发来的消息，
        # 不能让那些路径看到仍然"刷新中"的标记，所以先取到局部变量并立刻置 None。
        before = self._caption_before
        self._caption_before = None
        if before is not None:
            self._arm_caption(before)
        # 数据没变、也没有反馈文字要挂上，且 2 秒一次的 TIMER_CHECK 还在跑时，这一轮不再做几何查询。
        # TIMER_CHECK 每次都会 refresh_view，用当前时间重建显示元组，陈旧变暗、年龄与重置文字都在那里更新。
        # 计时器没建成时保留这里的重绘，否则这些随时间变化的文字就只会在数据变化时才更新。
        idle = not (changed or codex_changed or self._caption is not None)
        if idle and TIMER_CHECK in self.timers:
            return
        self.refresh_view()

    def _latest_observed(self):
        """各提供方当前快照里最新的 observed_at：(Claude, Codex)；没有窗口或没有该数据源则 None。"""
        codex = None if self.codex is None else self.codex.snapshot
        return (_snapshot_observed_at(self.reader.snapshot), _snapshot_observed_at(codex))

    def _arm_caption(self, before):
        """读完数据源后算出各提供方的反馈文字，记下截止时间并起定时器。

        before 是刷新前 _latest_observed() 的结果。定时器建不起来也不用处理：
        set_timer 自己记日志，截止时间仍由之后的重绘兜底擦掉。
        """
        texts = [caption_text(
            before[0], _snapshot_observed_at(self.reader.snapshot), self.reader.last_reason)]
        if self.codex is None:
            texts.append("")
        else:
            texts.append(caption_text(
                before[1], _snapshot_observed_at(self.codex.snapshot), self.codex.last_reason))
        self._caption = (time.time() + CAPTION_MS / 1000.0, tuple(texts))
        self.set_timer(TIMER_CAPTION, CAPTION_MS)

    def _poll_codex(self, force):
        """刷新 Codex。异常只记日志，不打断已经完成的 Claude 刷新，也不挡住后面的重绘。

        返回是否需要重绘：快照变了，或刷新抛了异常（宁可多画一次）。没有 Codex 数据源时为假。
        """
        reader = self.codex
        if reader is None:
            return False
        try:
            changed = reader.refresh(force=force)
        except Exception as exc:
            self._log_exception(exc)
            return True
        reason = reader.last_reason
        if reason == "":
            self._codex_log_reason = ""
            return changed
        if reason == "no_codex" or reason == self._codex_log_reason:
            return changed
        self._codex_log_reason = reason
        self.log.log("CodexRead", reason)
        return changed

    def _rewrite_slot_caption(self, index, rewrite):
        """改掉已有反馈文字里下标 index（0 是 Claude，1 是 Codex）那一项，另一项不动。
        没有反馈文字时什么也不做，截止时间不变。"""
        if self._caption is None:
            return
        deadline, texts = self._caption
        pair = list((tuple(texts) + ("", ""))[:2])
        pair[index] = rewrite(pair[index])
        self._caption = (deadline, tuple(pair))

    def _set_slot_caption(self, index, text):
        """把反馈文字里下标 index（0 是 Claude，1 是 Codex）那一项换成 text。

        只保留未过期的另一项，没有或已过期则另一项为空串。截止时间重新从现在算起，
        起 TIMER_CAPTION 并重绘一次。
        """
        now = time.time()
        pair = ["", ""]
        if self._caption is not None:
            deadline, texts = self._caption
            if now < deadline:
                pair = list((tuple(texts) + ("", ""))[:2])
        pair[index] = text
        self._caption = (now + CAPTION_MS / 1000.0, (pair[0], pair[1]))
        self.set_timer(TIMER_CAPTION, CAPTION_MS)
        self.refresh_view()

    # Refresh now 的两路（Codex ping、桌面会话请求）共用的流程。slot 是 RefreshSlot，
    # 这些方法不认识 Codex 与 Claude。

    def _slot_busy(self, slot):
        """这一路有没有已发出、尚未收口的请求；驱动对象被关掉时没有。"""
        driver = getattr(self, slot.driver)
        return driver is not None and bool(getattr(driver, slot.busy))

    def _start_slot(self, slot, before, redraw=True):
        """发出这一路的请求并起轮询计时器。只在 Refresh now 里调用，TIMER_POLL 绝不会走到。

        before 是点击那一刻这个提供方最新的 observed_at。冷却里只改这一路已有的文字
        （改不改成 cooldown 由 slot.caption 定）；请求没能发出（原因以 slot.error_prefix
        开头）就记日志并标成失败；正在等、没有可用目录这类原因什么也不改。内部绝不抛异常。

        屏幕上的内容有变（asking、cooldown、失败文字）时默认马上重绘一次。redraw 为假时不
        重绘：_refresh_now 之后紧跟着 _begin_flash 的强制重绘，那一帧已经带着这些文字，
        不必每一路各画一帧。
        """
        try:
            driver = getattr(self, slot.driver)
            if driver is None or self.exiting or not self.hwnd:
                return
            reason = driver.start()
            if reason == "started":
                setattr(self, slot.before, before)
                if not self.set_timer(slot.timer_id, slot.poll_ms):
                    self._drop_slot(slot)
                    self._rewrite_slot_caption(
                        slot.index, lambda _text: slot.caption("", slot.drop_state))
                if redraw:
                    self.refresh_view()
            elif reason == "cooldown":
                self._rewrite_slot_caption(
                    slot.index, lambda text: slot.caption(text, "cooldown"))
                if redraw:
                    self.refresh_view()
            elif reason in slot.skip_notes:
                self.log.log(slot.log_kind, slot.skip_notes[reason])
            elif reason.startswith(slot.error_prefix):
                self.log.log(slot.log_kind, reason)
                self._rewrite_slot_caption(
                    slot.index, lambda _text: slot.caption("", slot.error_state))
                if redraw:
                    self.refresh_view()
        except Exception as exc:
            self._log_exception(exc)

    def _poll_slot(self, slot, finish):
        """轮询计时器到点：驱动对象还没有结果就等下一次，有结果就在这里统一收口。

        只换这一路的那一项，另一路若仍未过期就保留。finish(outcome, detail, before)
        做各提供方自己的事（重读数据源、记日志），返回这一项的反馈文字。
        """
        try:
            driver = getattr(self, slot.driver)
            result = None if driver is None else driver.poll()
            if result is None:
                if not self._slot_busy(slot):
                    self.kill_timer(slot.timer_id)
                return
            self.kill_timer(slot.timer_id)
            outcome, detail = result
            text = finish(outcome, detail, getattr(self, slot.before))
            setattr(self, slot.before, None)
            self._set_slot_caption(slot.index, text)
        except Exception as exc:
            self._log_exception(exc)

    def _stop_slot(self, slot):
        """停掉这一路正在进行的请求。可重复调用，绝不抛异常。"""
        try:
            driver = getattr(self, slot.driver)
            if driver is not None:
                driver.stop()
        except Exception as exc:
            self._log_exception(exc)

    def _drop_slot(self, slot):
        """轮询计时器建不起来：没有计时器就没人收尾，这一路会一直停在 asking，所以放弃它。"""
        self.log.log(slot.log_kind, slot.drop_note)
        self._stop_slot(slot)
        setattr(self, slot.before, None)

    # Codex ping --------------------------------------------------------------

    def _start_codex_ping(self, before_codex, redraw=True):
        """只在 Refresh now 里调用，TIMER_POLL 不会走到。

        Codex 没装或还没用过（没有 sessions 目录）时，不替用户去启动它。
        redraw 原样交给 _start_slot。
        """
        if self.codex is None or not self.codex.available:
            return
        self._start_slot(CODEX_SLOT, before_codex, redraw)

    def _on_ping_timer(self):
        """ping 的结果只在这里统一收口。只换 Codex 那一项（下标 1），
        Claude 那一项若仍未过期就保留。"""
        self._poll_slot(CODEX_SLOT, self._finish_ping)

    def _finish_ping(self, outcome, detail, before):
        """ping 结束：重读 Codex 日志，返回 Codex 那一项的反馈文字。"""
        self._poll_codex(True)
        if outcome == "failed" or outcome == "timeout":
            self.log.log("CodexPing", "ping %s (%s)" % (outcome, detail))
            return codex_ping_caption("", "failed")
        return caption_text(
            before, _snapshot_observed_at(self.codex.snapshot), self.codex.last_reason)

    # 桌面会话请求 ------------------------------------------------------------

    def _start_app_refresh(self, before_claude, redraw=True):
        """只在 Refresh now 里调用，TIMER_POLL 绝不会走到。内部绝不抛异常。
        redraw 原样交给 _start_slot。"""
        self._start_slot(APP_SLOT, before_claude, redraw)

    def _on_app_timer(self):
        """TIMER_APP：读确认文件，收口后只换 Claude 那一项。"""
        self._poll_slot(APP_SLOT, self._finish_app_refresh)

    def _finish_app_refresh(self, outcome, _detail, before):
        """请求收口：返回 Claude 那一项的反馈文字。只有 ok 才重读 usage.json。

        超时而 usage.json 又不在（上一次读取报 missing）时写 no file，不写 no session：
        usage.json 不在，多半是小窗与插件指着不同的文件夹，no session 会把这个原因盖住。
        """
        if outcome == "ok":
            self.reader.refresh(force=True)
            reason = self.reader.last_reason
            if reason and reason != "missing":
                self.log.log("SnapshotInvalid", reason)
            return caption_text(
                before, _snapshot_observed_at(self.reader.snapshot), self.reader.last_reason)
        if outcome == "unavailable":
            return app_refresh_caption("", "no_limits")
        self.log.log("AppRefresh", "no answer within %d s" % APP_REFRESH_WAIT_SECONDS)
        if self.reader.last_reason == "missing":
            return CAPTION_NO_FILE
        return app_refresh_caption("", "no_session")

    def poll_theme(self):
        light = _entry().read_light_theme()
        if light != self.light:
            self.light = light
            self.refresh_view()

    def on_system_change(self):
        """DPI、显示器、系统设置、任务栏重建：立即重读主题并重算位置。"""
        self._owner_check()
        self.light = _entry().read_light_theme()
        self.refresh_view()

    # 鼠标与菜单 ----------------------------------------------------------------

    def on_left_down(self):
        if self.geom is None or self.menu_open:
            return
        cursor_x, _cursor_y = self.w32.cursor_pos()
        # 先取得鼠标捕获，再建拖动状态：本窗口若已持有捕获，SetCapture 会同步发来
        # WM_CAPTURECHANGED，那时拖动状态还没建，不会把新拖动当成"拖动中失去捕获"而收尾
        self.w32.set_capture(self.hwnd)
        self.drag = {"cursor_x": cursor_x, "window_x": self.geom[0], "moved": False}

    def on_mouse_move(self, wparam):
        drag = self.drag
        if drag is None or self.taskbar is None or self.geom is None:
            return
        if not wparam & MK_LBUTTON:
            # 按键已经松开但没收到松开消息（例如在别处松开）
            self._finish_drag()
            return
        cursor_x, _cursor_y = self.w32.cursor_pos()
        delta = cursor_x - drag["cursor_x"]
        if not drag["moved"] and abs(delta) <= DRAG_THRESHOLD_PX:
            return
        drag["moved"] = True
        left, _top, right, _bottom = self.taskbar[0]
        x, y, width, _height = self.geom
        new_x = max(left, min(drag["window_x"] + delta, right - width))
        if new_x != x:
            self.move_to(new_x, y)

    def on_left_up(self):
        self._finish_drag()

    def on_capture_lost(self):
        if self.drag is not None:
            self._finish_drag(release=False)

    def _finish_drag(self, release=True):
        """结束拖动；移动超过阈值才切到手动模式并保存。

        先清掉 self.drag 再释放鼠标捕获：ReleaseCapture 会同步发出 WM_CAPTURECHANGED，
        那条消息进来时看到 drag 已为空，就不会重复收尾。
        """
        drag = self.drag
        self.drag = None
        if release:
            self.w32.release_capture()
        if drag is None or not drag["moved"] or self.geom is None or self.taskbar is None:
            return
        x, _y, width, _height = self.geom
        self.offset = offset_from_right_edge(x, width, self.taskbar[0][2], self.scale)
        self.mode = MODE_MANUAL
        self._save_position()
        self.refresh_view()

    def _end_flash(self):
        """结束变暗。先丢掉已显示的元组，下一轮即使被挡住也会重画未变暗的画面。

        丢掉的只是"窗口里现在是什么"的记录，渲染缓存（_frame_image）不受影响：重画只重新
        预乘并提交，不再渲染。
        """
        self._flashing = False
        self.shown_display = None
        self.refresh_view(force=True)

    def _refresh_now(self, before=None):
        """Refresh now：记下刷新前的观测时间，重读数据源并重绘（第一次重绘就带反馈文字），再变暗。

        before 是菜单打开那一刻的 _latest_observed()。菜单开着时 TIMER_POLL 仍会派发，
        新数据可能已经读进读取器；等菜单关了再取，before 会等于新数据，反馈就被写成 same。
        before 为 None 时才退回当场取（无参调用仍然成立）。
        poll_snapshot 仍是对外入口，不给它加参数：TIMER_POLL 与现有测试都按 force 单参数
        调用或替换它。反馈文字靠 _caption_before 接进去：只在读数据源期间非 None，
        poll_snapshot 读完就清掉；这里的 finally 保住异常路径也清掉。
        之后（读完数据源、反馈文字已经挂上）才请求 Codex 与桌面会话，这样
        asking / cooldown 是在已有反馈文字上改。
        两路发出请求后各自不重绘（redraw 为假）：紧接着 _begin_flash 的强制重绘那一帧已经
        带着 asking / cooldown，每路各画一帧只是白画。例外是已经在闪的时候：_begin_flash
        那时不再重绘，两路就仍然各自重绘，文字才不会晚到闪烁结束。
        """
        if before is None:
            before = self._latest_observed()
        self._caption_before = before
        try:
            self.poll_snapshot(force=True)
        finally:
            self._caption_before = None
        redraw = self._flashing  # 已经在闪时 _begin_flash 不画，两路自己画
        self._safe(self._start_codex_ping, before[1], redraw)
        self._safe(self._start_app_refresh, before[0], redraw)
        self._begin_flash()

    def _begin_flash(self):
        """刷新后把画面短暂变暗再恢复，数据没变时点击也有可见反馈。

        闪烁不进显示元组，所以开始和结束都用 force=True 重绘。开始变暗的这一帧带着刚发出的
        请求的 asking / cooldown（两路请求不再各自重绘），是这次点击的最后一个新画面，渲染
        一次；结束时显示元组不变，命中渲染缓存，只重新预乘并提交，不再渲染。已经在闪时
        再点刷新只重设定时器，不再多画一次。重绘抛错也要建上定时器；定时器建不起来就
        立刻取消变暗，避免小窗一直停在暗的画面上。
        """
        if self.exiting or not self.hwnd:
            return
        already = self._flashing
        self._flashing = True
        try:
            if not already:
                self.refresh_view(force=True)
        finally:
            if not self.set_timer(TIMER_FLASH, FLASH_MS):
                self._end_flash()

    def _save_position(self):
        try:
            _entry().save_position(self.data_dir, self.mode, self.offset)
        except OSError as exc:
            self._log_exception(exc)

    def on_right_up(self):
        if self.menu_open or self.drag is not None:
            return
        x, y = self.w32.cursor_pos()
        # 菜单开着时 TIMER_POLL 仍会派发，新数据可能在这期间被读进读取器；
        # 刷新前的观测时间必须在菜单打开这一刻取，不能等菜单关了再取。
        before = self._latest_observed()
        self.menu_open = True
        try:
            command = self.w32.track_menu(self.hwnd, x, y)
        finally:
            self.menu_open = False
        if self.exiting:
            return
        if not self.hwnd and not self.w32.post_thread_message(WM_APP_RECREATE):
            self.log.log("RecreateWindow", "PostThreadMessageW failed")
            self.w32.post_quit()
        if command == MENU_REFRESH:
            self._refresh_now(before)
        elif command == MENU_SNAP:
            self.mode = MODE_AUTO
            self._save_position()
            self.refresh_view()
        elif command == MENU_QUIT:
            self.request_exit()

    # 退出 ----------------------------------------------------------------------

    def _arm_watchdog(self):
        """退出请求发出 10 秒后进程还在就强制结束。

        这是整个程序里唯一的线程：只在退出时启动一次，只睡眠和 os._exit，不碰窗口，
        因此主线程卡死（例如销毁窗口或菜单循环不返回）时它仍能生效。
        """
        def force_exit():
            try:
                self.log.log("WatchdogExit", "exit still pending after %d s; forcing exit" % WATCHDOG_SECONDS)
            finally:
                os._exit(WATCHDOG_EXIT_CODE)
        self._watchdog = threading.Timer(WATCHDOG_SECONDS, force_exit)
        self._watchdog.daemon = True
        self._watchdog.start()

    def request_exit(self):
        """Quit、WM_CLOSE 与 --exit-after 共用：先置标志，再停全部计时器，最后销毁窗口。

        窗口销毁后 WM_DESTROY 里 PostQuitMessage，消息循环结束；之后的收尾（注销窗口类、
        关互斥量）在循环外做。
        """
        if self.exiting:
            return
        self.exiting = True
        for slot in _entry().REFRESH_SLOTS:
            self._stop_slot(slot)
        self._flashing = False
        self._caption = None
        self.w32.unwatch_window_events()
        try:
            self._arm_watchdog()
        except Exception as exc:
            # 起不了看门狗（例如系统线程资源耗尽）也不能耽误正常退出
            self._log_exception(exc)
        self._kill_all_timers()
        hwnd = self.hwnd
        if not hwnd or not self.w32.destroy_window(hwnd):
            self.w32.post_quit()

    # 句柄自测（--selftest-gdi）----------------------------------------------------

    def _gdi_counts(self):
        return self.w32.gui_resources(0), self.w32.gui_resources(1)

    def _gdi_tick(self):
        """一轮自测；索引先加再执行，即使本轮出错也保证在固定轮数后结束。"""
        index = self._gdi.get("index", 0)
        if index >= self.opts.selftest_gdi:
            self.kill_timer(TIMER_GDI_TICK)
            self.set_timer(TIMER_GDI_END, GDI_SETTLE_MS)
            return
        self._gdi["index"] = index + 1
        self._gdi_body(index)

    def _gdi_body(self, index):
        """用不同的百分比重绘一次，再把窗口左右抖动 2 像素（走 SetWindowPos 那条路）。"""
        now = time.time()
        five = Win(float((index * 7) % 101), now + 3600.0, now)
        seven = Win(float((index * 13 + 5) % 101), now + 86400.0, now)
        self.snapshot_override = {"five_hour": five, "seven_day": seven}
        self.refresh_view(force=True)
        if self.geom is not None:
            x, y, _width, _height = self.geom
            self.move_to(x + (2 if index % 2 else -2), y)

    def _gdi_report(self):
        before = self._gdi.get("before", (0, 0))
        after = self._gdi_counts()
        _entry().say("GDI before=%d after=%d USER before=%d after=%d" % (before[0], after[0], before[1], after[1]))

    # 主流程 ----------------------------------------------------------------------

    def _start_timers(self, recreated=False):
        self.set_timer(TIMER_CHECK, WINDOW_CHECK_MS)
        self.set_timer(TIMER_POLL, SNAPSHOT_POLL_MS)
        self.set_timer(TIMER_THEME, THEME_POLL_MS)
        if recreated:
            if self._exit_deadline is not None:
                self.set_timer(TIMER_EXIT, max(10, int((self._exit_deadline - time.monotonic()) * 1000)))
            for slot in _entry().REFRESH_SLOTS:
                # 窗口重建清掉了全部计时器；补不上就放弃，否则没人再收尾这一路。
                if self._slot_busy(slot) and not self.set_timer(slot.timer_id, slot.poll_ms):
                    self._drop_slot(slot)
        elif self.opts.exit_after is not None:
            self.set_timer(TIMER_EXIT, int(self.opts.exit_after * 1000))
        if self.opts.selftest_gdi and not recreated:
            self.set_timer(TIMER_GDI_BEGIN, GDI_SETTLE_MS)

    def run(self):
        _entry()._APP = self
        try:
            self.w32.register_class(_WNDPROC_REF)
            try:
                self.msg_taskbar_created = self.w32.register_message("TaskbarCreated")
                self.hwnd = self._create_widget_window()
                self._safe(self.poll_snapshot)
                self._safe(self.refresh_view, False, True)
                # 注册失败仍能用两秒轮询兜底，不影响小窗启动。
                self._safe(self.w32.watch_window_events, self.on_window_event)
                self._start_timers()
                result = self.w32.run_message_loop(self.on_thread_message, self._log_exception)
                if result < 0:
                    self.log.log("MessageLoop", "GetMessageW failed")
            finally:
                self.w32.unwatch_window_events()
                self.w32.unregister_class()
        finally:
            for slot in _entry().REFRESH_SLOTS:
                self._stop_slot(slot)
            _entry()._APP = None
        return 0



# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

Options = collections.namedtuple(
    "Options",
    ["data_dir", "exit_after", "selftest_render", "selftest_gdi", "no_codex", "codex_home",
     "codex_bin", "no_codex_ping", "codex_ping_model", "no_app_refresh", "warnings"],
    defaults=(False, None, None, False, None, False, ()))


def _arg_text(text):
    """写进警告的参数文字：ascii() 保证纯 ASCII，超过 40 个字符截断。"""
    shown = ascii(text)
    return shown if len(shown) <= 40 else shown[:37] + "..."


def parse_args(argv):
    """宽松解析：未知参数、缺值或值不合法的参数一律忽略，每一处各记一条到 warnings。不做任何 I/O。"""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(_entry().__file__))
    data_dir = os.path.join(base_dir, "data")
    exit_after = None
    render_dir = None
    gdi_count = None
    no_codex = False
    codex_home_arg = None
    codex_bin_arg = None
    no_codex_ping = False
    ping_model = None
    no_app_refresh = False
    warnings = []
    names = ("--data-dir", "--exit-after", "--selftest-render", "--selftest-gdi",
             "--codex-home", "--codex-bin", "--codex-ping-model")
    index = 0
    while index < len(argv):
        name, equals, inline = argv[index].partition("=")
        if name == "--no-codex":
            no_codex = True
            index += 1
            continue
        if name == "--no-codex-ping":
            no_codex_ping = True
            index += 1
            continue
        if name == "--no-app-refresh":
            no_app_refresh = True
            index += 1
            continue
        if name not in names:
            warnings.append("ignored unknown argument " + _arg_text(argv[index]))
            index += 1
            continue
        if equals:
            value, step = inline, 1
        elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value, step = argv[index + 1], 2
        else:
            warnings.append("ignored " + name + ", no value given")
            index += 1
            continue
        index += step
        if not value:
            warnings.append("ignored " + name + ", no value given")
        elif name == "--data-dir":
            data_dir = os.path.abspath(value)
        elif name == "--selftest-render":
            render_dir = os.path.abspath(value)
        elif name == "--codex-home":
            codex_home_arg = os.path.abspath(value)
        elif name == "--codex-bin":
            codex_bin_arg = os.path.abspath(value)
        elif name == "--codex-ping-model":
            if valid_ping_model(value):
                ping_model = value
            else:
                warnings.append("ignored invalid --codex-ping-model " + _arg_text(value))
        elif name == "--exit-after":
            try:
                seconds = float(value)
            except ValueError:
                seconds = None
            if seconds is not None and math.isfinite(seconds) and seconds >= 0:
                exit_after = seconds
            else:
                warnings.append("ignored invalid number for --exit-after: " + _arg_text(value))
        elif name == "--selftest-gdi":
            try:
                count = int(value)
            except ValueError:
                count = 0
            if count >= 1:
                gdi_count = count
            else:
                warnings.append("ignored invalid number for --selftest-gdi: " + _arg_text(value))
    return Options(data_dir, exit_after, render_dir, gdi_count, no_codex, codex_home_arg,
                   codex_bin_arg, no_codex_ping, ping_model, no_app_refresh, warnings)
