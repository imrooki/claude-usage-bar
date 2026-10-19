# -*- coding: utf-8 -*-
"""Claude 用量小窗：贴在 Windows 任务栏通知区域左边的两行横向进度条，没有背景色。

上一行是 5 小时窗口，下一行是 7 天窗口，每行带百分比与重置时间。数据只来自
<data-dir>\\usage.json（由别的程序写入）。读数超过 30 分钟视为陈旧：进度条变成
灰色，并显示读数的年龄（单栏写在重置时间右边，双栏写在页眉里）。右键 Refresh now
之后，页眉或附加列（没有任何数据时是第二行）会短暂（约 2.5 秒）改写成刷新结果，
如 new 14:58、same 14:29、no data、no file、read error。本程序自己不联网、不读任何凭证文件；
唯一例外是用户点 Refresh now 且 Codex 已启用时，会启动用户自己的 Codex 命令行
（原生 codex.exe，固定参数，不经 shell，隐藏窗口）发一个很小的请求，让 Codex
把最新额度写进它的会话日志，随后小窗照常只读这份日志；--no-codex-ping 可关闭。
用户点 Refresh now 时，小窗还会往数据目录写一个很小的请求文件 refresh-request.json，
并读 usage-feed 插件写的确认文件 refresh-ack.json，让装了插件的桌面会话报一次账号级
用量；本程序仍然不联网（联网的是插件所在的 Claude Code 会话，不是本程序）；
--no-app-refresh 可关闭。
窗口是无父窗口的顶层分层窗口（逐像素 alpha），以主任务栏为所有者，
随任务栏一起抬升层级。对资源管理器窗口只读查询；跨线程所有权会连接输入队列，主线程
不能阻塞。

运行：pythonw usage_widget.py [--data-dir DIR] [--no-codex] [--codex-home DIR]
        [--codex-bin FILE] [--no-codex-ping] [--codex-ping-model NAME]
        [--no-app-refresh]
测试用参数：--exit-after SEC、--selftest-render OUTDIR、--selftest-gdi N

代码分三层：
  1. 纯函数（快照读取与校验、显示状态、显示元组、布局、文字、渲染、预乘转换），可离线测试；
  2. Win32 薄封装（ctypes，只用白名单里的函数）；
  3. 窗口与周期任务（WidgetApp，全部在主线程；周期任务用 SetTimer 与 WM_TIMER，
     窗口事件（前台切换、外壳重排、顶层窗口尺寸/位置变化）经消息投递驱动同一条刷新路径）。
"""

import os
import sys
import time

from usage_bar.data import *
from usage_bar.skins import *
from usage_bar.render import *
from usage_bar.win32 import *
from usage_bar.app import *

from usage_bar.data import (
    _read_codex_tail,
    _snapshot_observed_at,
)
from usage_bar.render import (
    _clock_text,
    _compose_bar,
    _pill_mask,
    _sample_codex_snapshot,
    _sample_snapshot,
)

_APP = None


def run_app(opts, log):
    w32 = Win32()
    handle, error = w32.create_mutex(MUTEX_NAME)
    if error == ERROR_ALREADY_EXISTS:
        w32.close_handle(handle)
        say("ClaudeUsageWidget is already running; exiting")
        return 0
    if not handle:
        # 没能确认是唯一实例：不建数据目录；日志只在目录已经存在时才写进去。
        log.log("MutexError", "CreateMutexW failed, error %d" % error)
        return 1
    try:
        prepare_data_folder(opts, log)
        w32.set_dpi_awareness()
        return WidgetApp(opts, w32, log).run()
    finally:
        w32.close_handle(handle)


def prepare_data_folder(opts, log):
    """只在确认是唯一实例之后调用。第二次启动在互斥量处已经退出，不建目录，也不写日志。"""
    # 数据目录不存在就建（小窗的文件都写在这里）。建不成也照常启动，只说一声：日志也写不进去。
    try:
        os.makedirs(opts.data_dir, exist_ok=True)
    except (OSError, ValueError) as exc:
        say("data folder %s could not be created: %s: %s"
            % (opts.data_dir, type(exc).__name__, exc))
    for text in opts.warnings:
        log.log("Argument", text)
        say(text)


def main(argv=None):
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if opts.selftest_render:
        return selftest_render(opts.selftest_render)
    # ErrorLog 的构造不碰磁盘；目录和参数警告都等 run_app 确认唯一实例之后再处理。
    log = ErrorLog(opts.data_dir)
    try:
        return run_app(opts, log)
    except Exception as exc:
        log.log_exception(exc)
        say("fatal: %s: %s" % (type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
