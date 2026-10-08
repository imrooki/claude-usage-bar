"""What the Codex ping and the desktop-app request share on Refresh now.

The "nothing new" caption set, the cooldown arithmetic, the RefreshSlot table, and the generic
start / poll / stop / drop / re-arm path that both providers run through, plus the window code
that walks the whole table (the asking overlay, request_exit, run). The generic path is
exercised with a made-up third provider, so nothing here depends on Codex or Claude internals.
What each provider does on its own stays in test_codex_ping.py and test_app_refresh.py.
"""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# The sibling import below also has to work when this module is run by its dotted name.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import usage_widget as widget
from test_refresh_feedback import make_app as make_window_app


NOW = 1790000000.0
DEADLINE = NOW + widget.CAPTION_MS / 1000.0

# Read-caption texts that mean "nothing new to report" (caption_text outputs, plus the empty
# text of a block that has no caption yet), and the ones worth keeping through a cooldown.
NOTHING_NEW = ("", "no data", "no file", "same 14:29", "same --:--")
NEWS = ("new 14:59", "read error")
# Texts the ping and the request put on screen themselves; a cooldown never rewrites these.
OWN_STATES = ("asking", "cooldown", "ping failed", "not sent", "no session", "no limits")


class CooldownCaptionTests(unittest.TestCase):
    def test_nothing_new_becomes_cooldown_and_news_stays(self):
        for text in NOTHING_NEW:
            with self.subTest(text=text):
                self.assertEqual(widget.cooldown_caption(text), "cooldown")
        for text in NEWS + OWN_STATES:
            with self.subTest(text=text):
                self.assertEqual(widget.cooldown_caption(text), text)

    def test_every_text_caption_text_can_produce_is_on_one_side(self):
        # Fails when caption_text learns a new kind of text, until someone decides whether a
        # click that finds it has news (kept during a cooldown) or not (becomes "cooldown").
        reasons = ("", "missing", "no_codex", "no_rate_limits", "bad_json", "read_error:OSError")
        kinds = set()
        for reason in reasons:
            for before in (None, NOW):
                for after in (None, NOW, NOW + 60.0):
                    text = widget.caption_text(before, after, reason)
                    is_news = text == "read error" or text.startswith("new ")
                    fixed = text in ("read error", "no file", "no data")
                    kinds.add(text if fixed else text.split()[0])
                    with self.subTest(reason=reason, before=before, after=after):
                        expected = text if is_news else "cooldown"
                        self.assertEqual(widget.cooldown_caption(text), expected)
        self.assertEqual(kinds, {"read error", "no file", "no data", "new", "same"})

    def test_both_providers_use_the_one_definition(self):
        for text in NOTHING_NEW + NEWS + OWN_STATES:
            expected = "cooldown" if text in NOTHING_NEW else text
            with self.subTest(text=text):
                self.assertEqual(widget.cooldown_caption(text), expected)
                self.assertEqual(widget.codex_ping_caption(text, "cooldown"), expected)
                self.assertEqual(widget.app_refresh_caption(text, "cooldown"), expected)

    def test_no_file_becomes_cooldown_for_both_providers(self):
        # Regression: the app refresh used to keep "no file" on screen during a cooldown,
        # although its docstring said every "nothing new" text turns into "cooldown".
        self.assertEqual(widget.app_refresh_caption("no file", "cooldown"), "cooldown")
        self.assertEqual(widget.codex_ping_caption("no file", "cooldown"), "cooldown")
        self.assertEqual(
            widget.app_refresh_caption(
                widget.caption_text(NOW - 60.0, NOW - 60.0, "missing"), "cooldown"),
            "cooldown")


class CooldownRemainingTests(unittest.TestCase):
    def test_no_cooldown_set_does_not_read_the_clock(self):
        clock = Mock(return_value=1000.0)
        self.assertEqual(widget.cooldown_remaining(clock, None, 300.0), 0.0)
        clock.assert_not_called()

    def test_counts_down_and_ends_exactly_at_the_duration(self):
        cases = ((1000.0, 300.0), (1100.0, 200.0), (1299.5, 0.5), (1300.0, 0.0),
                 (1300.5, 0.0), (9000.0, 0.0))
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertAlmostEqual(
                    widget.cooldown_remaining(lambda: now, 1000.0, 300.0), expected)

    def test_a_clock_that_went_backwards_means_finished(self):
        self.assertEqual(widget.cooldown_remaining(lambda: 999.9, 1000.0, 300.0), 0.0)
        self.assertEqual(widget.cooldown_remaining(lambda: -5.0, 1000.0, 300.0), 0.0)

    def test_both_drivers_count_the_same_way(self):
        # Same start, same duration, same readings: the pinger and the refresher must agree.
        clock_now = [1000.0]

        def clock():
            return clock_now[0]

        pinger = widget.CodexPinger(".", clock=clock)
        pinger._last_started_at = 1000.0
        refresher = widget.AppRefresher(".", monotonic=clock)
        refresher._begin_cooldown(widget.CODEX_PING_COOLDOWN_SECONDS)
        for now in (900.0, 999.9, 1000.0, 1100.0, 1299.5, 1300.0, 1400.0):
            clock_now[0] = now
            with self.subTest(now=now):
                expected = widget.cooldown_remaining(
                    clock, 1000.0, widget.CODEX_PING_COOLDOWN_SECONDS)
                self.assertEqual(pinger.cooldown_left(), expected)
                self.assertEqual(refresher.cooldown_left(), expected)


class FakeDriver:
    """start / poll / stop stand-in. The caller names the busy flag (running, waiting, ...)."""

    def __init__(self, busy):
        self.busy_name = busy
        setattr(self, busy, False)
        self.start_result = "started"
        self.poll_results = []
        self.stops = 0

    def start(self):
        if self.start_result == "started":
            setattr(self, self.busy_name, True)
        return self.start_result

    def poll(self):
        if self.poll_results:
            result = self.poll_results.pop(0)
            if result is not None:
                setattr(self, self.busy_name, False)
            return result
        return None

    def stop(self):
        self.stops += 1
        setattr(self, self.busy_name, False)


def make_app():
    """A real WidgetApp with the window mocked out and both providers switched off."""
    options = widget.Options(".", None, None, None, no_codex=True, no_app_refresh=True)
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(options, Mock(), Mock())
    app.hwnd = 10
    app.w32.set_timer.return_value = True
    app.refresh_view = Mock()
    return app


class RefreshSlotTableTests(unittest.TestCase):
    def test_the_two_slots(self):
        self.assertEqual(widget.REFRESH_SLOTS, (widget.CODEX_SLOT, widget.APP_SLOT))
        # Caption index: 0 is the Claude block, 1 the Codex block.
        self.assertEqual(widget.APP_SLOT.index, 0)
        self.assertEqual(widget.CODEX_SLOT.index, 1)
        self.assertEqual(widget.APP_SLOT.timer_id, widget.TIMER_APP)
        self.assertEqual(widget.CODEX_SLOT.timer_id, widget.TIMER_PING)
        self.assertEqual(widget.APP_SLOT.poll_ms, widget.APP_REFRESH_POLL_MS)
        self.assertEqual(widget.CODEX_SLOT.poll_ms, widget.CODEX_PING_POLL_MS)
        self.assertIs(widget.APP_SLOT.caption, widget.app_refresh_caption)
        self.assertIs(widget.CODEX_SLOT.caption, widget.codex_ping_caption)
        self.assertEqual(len({slot.index for slot in widget.REFRESH_SLOTS}), 2)
        self.assertEqual(len({slot.timer_id for slot in widget.REFRESH_SLOTS}), 2)

    def test_the_names_in_the_table_exist_on_the_real_objects(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
                    patch.object(widget, "read_light_theme", return_value=False):
                app = widget.WidgetApp(widget.Options(root, None, None, None), Mock(), Mock())
            for slot in widget.REFRESH_SLOTS:
                with self.subTest(driver=slot.driver):
                    driver = getattr(app, slot.driver)
                    self.assertIsNotNone(driver)
                    self.assertIs(getattr(driver, slot.busy), False)
                    self.assertIsNone(getattr(app, slot.before))
                    for method in ("start", "poll", "stop"):
                        self.assertTrue(callable(getattr(driver, method)))

    def test_the_failure_reasons_in_the_table_are_the_ones_the_drivers_return(self):
        def refuse(*_args, **_kwargs):
            raise OSError("refused")

        with tempfile.TemporaryDirectory() as root:
            pinger = widget.CodexPinger(root, popen=refuse, find_exe=lambda _override: "codex.exe")
            self.assertTrue(pinger.start().startswith(widget.CODEX_SLOT.error_prefix))
            missing = widget.CodexPinger(root, find_exe=lambda _override: None)
            self.assertIn(missing.start(), widget.CODEX_SLOT.skip_notes)

            def no_token():
                raise ValueError("no token")

            refresher = widget.AppRefresher(root, token=no_token)
            self.assertTrue(refresher.start().startswith(widget.APP_SLOT.error_prefix))


class StandInSlotTests(unittest.TestCase):
    """The shared path with a made-up third provider; it names neither Codex nor Claude."""

    def setUp(self):
        self.app = make_app()
        self.driver = FakeDriver("active")
        self.app.third = self.driver
        self.app._third_before = None
        self.slot = widget.RefreshSlot(
            index=1, driver="third", busy="active", before="_third_before",
            timer_id=77, poll_ms=123, caption=self.caption, log_kind="Third",
            error_prefix="boom:", error_state="failed", drop_state="gone",
            drop_note="timer gone", skip_notes={"nope": "no luck"})

    @staticmethod
    def caption(read_caption, state):
        if state == "cooldown":
            return widget.cooldown_caption(read_caption)
        return {"failed": "F", "gone": "G"}.get(state, read_caption)

    def logged(self):
        return [item.args for item in self.app.log.log.call_args_list]

    def arm_timer(self):
        self.app.set_timer(self.slot.timer_id, self.slot.poll_ms)
        self.app.w32.set_timer.reset_mock()
        self.app.w32.kill_timer.reset_mock()

    # start

    def test_started_remembers_the_observation_and_arms_the_timer(self):
        self.app._start_slot(self.slot, 12.5)
        self.assertEqual(self.app._third_before, 12.5)
        self.app.w32.set_timer.assert_any_call(10, 77, 123)
        self.assertIn(77, self.app.timers)
        self.app.refresh_view.assert_called_once_with()
        self.assertTrue(self.driver.active)

    def test_cooldown_rewrites_only_this_slot_and_only_when_nothing_is_new(self):
        self.driver.start_result = "cooldown"
        cases = (
            (("keep", "same 14:00"), ("keep", "cooldown")),
            (("keep", "no file"), ("keep", "cooldown")),
            (("keep", "new 14:59"), ("keep", "new 14:59")),
            (("same 14:00", "read error"), ("same 14:00", "read error")),
        )
        for before, after in cases:
            with self.subTest(before=before):
                self.app.refresh_view.reset_mock()
                self.app._caption = (DEADLINE, before)
                self.app._start_slot(self.slot, None)
                self.assertEqual(self.app._caption, (DEADLINE, after))
                self.app.refresh_view.assert_called_once_with()
        self.assertEqual(self.app.timers, set())

    def test_cooldown_without_a_caption_still_redraws(self):
        self.driver.start_result = "cooldown"
        self.app._start_slot(self.slot, None)
        self.assertIsNone(self.app._caption)
        self.app.refresh_view.assert_called_once_with()

    def test_a_failed_start_is_logged_and_marked(self):
        self.driver.start_result = "boom:OSError"
        self.app._caption = (DEADLINE, ("keep", "new 14:59"))
        self.app._start_slot(self.slot, 12.5)
        self.assertEqual(self.logged(), [("Third", "boom:OSError")])
        self.assertEqual(self.app._caption, (DEADLINE, ("keep", "F")))
        self.app.refresh_view.assert_called_once_with()
        self.assertIsNone(self.app._third_before)

    def test_a_skip_note_is_only_logged(self):
        self.driver.start_result = "nope"
        caption = (DEADLINE, ("keep", "new 14:59"))
        self.app._caption = caption
        self.app._start_slot(self.slot, 12.5)
        self.assertEqual(self.logged(), [("Third", "no luck")])
        self.assertEqual(self.app._caption, caption)
        self.app.refresh_view.assert_not_called()

    def test_other_reasons_change_nothing(self):
        self.driver.start_result = "busy"
        self.app._caption = (DEADLINE, ("a", "b"))
        self.app._third_before = 1.0
        self.app._start_slot(self.slot, 12.5)
        self.assertEqual(self.logged(), [])
        self.assertEqual(self.app._caption, (DEADLINE, ("a", "b")))
        self.assertEqual(self.app._third_before, 1.0)
        self.app.refresh_view.assert_not_called()
        self.app.w32.set_timer.assert_not_called()

    def test_start_is_skipped_without_a_driver_a_window_or_while_exiting(self):
        for name in ("no driver", "no window", "exiting"):
            with self.subTest(name=name):
                app = make_app()
                app.third = None if name == "no driver" else self.driver
                app._third_before = None
                if name == "no window":
                    app.hwnd = 0
                if name == "exiting":
                    app.exiting = True
                app._start_slot(self.slot, 12.5)
                app.refresh_view.assert_not_called()
                app.w32.set_timer.assert_not_called()
                self.assertFalse(self.driver.active)

    def test_a_refused_timer_abandons_the_request(self):
        self.app.w32.set_timer.return_value = False
        self.app._caption = (DEADLINE, ("keep", "same 14:00"))
        self.app._start_slot(self.slot, 12.5)
        self.assertEqual(self.driver.stops, 1)
        self.assertIsNone(self.app._third_before)
        self.assertNotIn(77, self.app.timers)
        self.assertEqual(self.logged(), [("SetTimer", "SetTimer failed for timer 77"),
                                         ("Third", "timer gone")])
        self.assertEqual(self.app._caption, (DEADLINE, ("keep", "G")))
        self.app.refresh_view.assert_called_once_with()

    def test_a_driver_that_raises_is_logged_and_not_propagated(self):
        self.driver.start = Mock(side_effect=RuntimeError("boom"))
        self.app._start_slot(self.slot, 12.5)
        self.app.log.log_exception.assert_called_once()
        self.app.refresh_view.assert_not_called()

    def test_redraw_only_decides_whether_the_screen_is_redrawn(self):
        # Refresh now passes redraw=False because its flash redraw follows. Everything else a
        # start does (text, timer, observation, log) must be the same either way.
        def start(start_result, timer_ok, redraw):
            app = make_app()
            driver = FakeDriver("active")
            driver.start_result = start_result
            app.third = driver
            app._third_before = None
            app._caption = (DEADLINE, ("keep", "same 14:00"))
            app.w32.set_timer.return_value = timer_ok
            app._start_slot(self.slot, 12.5, redraw)
            state = (app._caption, app._third_before, sorted(app.timers), driver.active,
                     driver.stops, [item.args for item in app.log.log.call_args_list])
            return state, app.refresh_view.call_count

        cases = (
            # (name, what start() says, whether the poll timer can be made, redraws by default)
            ("started", "started", True, 1),
            ("timer refused", "started", False, 1),
            ("cooldown", "cooldown", True, 1),
            ("start failed", "boom:OSError", True, 1),
            ("skipped", "nope", True, 0),
            ("busy", "busy", True, 0),
        )
        for name, start_result, timer_ok, redraws in cases:
            with self.subTest(name=name):
                drawn_state, drawn = start(start_result, timer_ok, True)
                deferred_state, deferred = start(start_result, timer_ok, False)
                self.assertEqual(drawn, redraws)
                self.assertEqual(deferred, 0)
                self.assertEqual(deferred_state, drawn_state)
        # The default is to redraw.
        self.app._start_slot(self.slot, 12.5)
        self.app.refresh_view.assert_called_once_with()

    # poll

    def test_a_result_is_finished_once_and_replaces_only_this_slot(self):
        self.arm_timer()
        self.driver.active = True
        self.driver.poll_results = [("ok", "d")]
        self.app._third_before = 12.5
        self.app._caption = (NOW + 100.0, ("keep", "stale"))
        finish = Mock(return_value="T")
        with patch.object(widget.time, "time", return_value=NOW):
            self.app._poll_slot(self.slot, finish)
        finish.assert_called_once_with("ok", "d", 12.5)
        self.app.w32.kill_timer.assert_called_once_with(10, 77)
        self.assertIsNone(self.app._third_before)
        self.assertEqual(self.app._caption, (DEADLINE, ("keep", "T")))
        self.app.refresh_view.assert_called_once_with()

    def test_no_result_yet_keeps_the_timer_only_while_busy(self):
        self.arm_timer()
        finish = Mock()
        self.driver.active = True
        self.app._poll_slot(self.slot, finish)
        self.app.w32.kill_timer.assert_not_called()
        self.driver.active = False
        self.app._poll_slot(self.slot, finish)
        self.app.w32.kill_timer.assert_called_once_with(10, 77)
        finish.assert_not_called()

    def test_no_driver_kills_the_timer(self):
        self.arm_timer()
        self.app.third = None
        self.app._poll_slot(self.slot, Mock())
        self.app.w32.kill_timer.assert_called_once_with(10, 77)

    def test_a_finish_that_raises_is_logged_and_keeps_the_observation(self):
        self.arm_timer()
        self.driver.active = True
        self.driver.poll_results = [("ok", "d")]
        self.app._third_before = 12.5
        self.app._poll_slot(self.slot, Mock(side_effect=RuntimeError("boom")))
        self.app.log.log_exception.assert_called_once()
        self.assertEqual(self.app._third_before, 12.5)
        self.assertNotIn(77, self.app.timers)

    # stop, drop, busy

    def test_stop_is_repeatable_and_survives_a_missing_or_broken_driver(self):
        self.app._stop_slot(self.slot)
        self.app._stop_slot(self.slot)
        self.assertEqual(self.driver.stops, 2)
        self.app.third = None
        self.app._stop_slot(self.slot)
        self.app.log.log_exception.assert_not_called()
        self.app.third = Mock()
        self.app.third.stop.side_effect = RuntimeError("boom")
        self.app._stop_slot(self.slot)
        self.app.log.log_exception.assert_called_once()

    def test_drop_logs_stops_and_forgets_the_observation(self):
        self.app._third_before = 12.5
        self.app._drop_slot(self.slot)
        self.assertEqual(self.logged(), [("Third", "timer gone")])
        self.assertEqual(self.driver.stops, 1)
        self.assertIsNone(self.app._third_before)

    def test_busy_needs_a_driver_and_its_flag(self):
        self.assertFalse(self.app._slot_busy(self.slot))
        self.driver.active = True
        self.assertTrue(self.app._slot_busy(self.slot))
        self.app.third = None
        self.assertFalse(self.app._slot_busy(self.slot))

    # re-arm after the window is rebuilt

    def test_every_slot_in_the_table_is_rearmed_when_busy(self):
        slots = widget.REFRESH_SLOTS + (self.slot,)
        with patch.object(widget, "REFRESH_SLOTS", slots):
            self.driver.active = True
            self.app._start_timers(recreated=True)
            self.app.w32.set_timer.assert_any_call(10, 77, 123)
            self.assertIn(77, self.app.timers)
            self.assertEqual(self.driver.stops, 0)

            idle = make_app()
            idle.third = FakeDriver("active")
            idle._third_before = None
            idle._start_timers(recreated=True)
            self.assertNotIn(77, idle.timers)

    def test_a_slot_whose_timer_cannot_be_rearmed_is_dropped(self):
        slots = widget.REFRESH_SLOTS + (self.slot,)
        with patch.object(widget, "REFRESH_SLOTS", slots):
            self.driver.active = True
            self.app._third_before = 12.5
            self.app.w32.set_timer.side_effect = lambda hwnd, timer_id, ms: timer_id != 77
            self.app._start_timers(recreated=True)
        self.assertEqual(self.driver.stops, 1)
        self.assertIsNone(self.app._third_before)
        self.assertNotIn(77, self.app.timers)
        self.assertIn(("Third", "timer gone"), self.logged())


class WholeTableTests(unittest.TestCase):
    """The window code that is not a Refresh now step walks REFRESH_SLOTS instead of naming the
    two providers, so a slot added to the table needs no change there.

    A made-up third slot is added to the table; the real drivers are off (no Codex, no app
    refresher), so whatever these paths do for the third slot they do from the table alone.
    """

    def setUp(self):
        self.app = make_window_app()
        self.driver = FakeDriver("active")
        self.app.third = self.driver
        self.app._third_before = None
        self.slot = widget.RefreshSlot(
            index=1, driver="third", busy="active", before="_third_before",
            timer_id=77, poll_ms=123, caption=lambda read_caption, state: read_caption,
            log_kind="Third", error_prefix="boom:", error_state="failed",
            drop_state="gone", drop_note="timer gone", skip_notes={})
        patcher = patch.object(widget, "REFRESH_SLOTS", widget.REFRESH_SLOTS + (self.slot,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_dual_layout(self):
        snapshot = {"five_hour": widget.Win(30.0, NOW + 3600.0, NOW - 60.0),
                    "seven_day": widget.Win(60.0, NOW + 86400.0, NOW - 60.0)}
        self.app.reader.snapshot = snapshot
        self.app.codex = Mock(snapshot=snapshot, available=True)

    def drawn_captions(self):
        with patch.object(widget.time, "time", return_value=NOW):
            self.app._refresh_once(False, False)
        return self.app._apply.call_args_list[-1][0][0].captions

    def test_asking_goes_to_the_index_of_every_busy_slot_in_the_table(self):
        self.use_dual_layout()
        stored = (NOW + 100.0, ("new 14:58", "same 14:00"))
        cases = (
            # (index of the stand-in slot, busy, stored text, what the picture says)
            (0, True, None, ("asking", "")),
            (1, True, None, ("", "asking")),
            (0, True, stored, ("asking", "same 14:00")),
            (1, True, stored, ("new 14:58", "asking")),
            (1, False, None, ()),
            (1, False, stored, ("new 14:58", "same 14:00")),
        )
        for index, busy, caption, expected in cases:
            with self.subTest(index=index, busy=busy, stored=caption is not None):
                slot = self.slot._replace(index=index)
                with patch.object(widget, "REFRESH_SLOTS", (slot,)):
                    self.driver.active = busy
                    self.app._caption = caption
                    self.assertEqual(self.drawn_captions(), expected)

    def test_every_busy_slot_asks_at_once(self):
        self.use_dual_layout()
        claude = self.slot._replace(index=0, driver="second", busy="waiting", before="_second_before")
        self.app.second = FakeDriver("waiting")
        self.app._second_before = None
        with patch.object(widget, "REFRESH_SLOTS", (claude, self.slot)):
            for waiting, active, expected in (
                    (True, True, ("asking", "asking")),
                    (True, False, ("asking", "")),
                    (False, True, ("", "asking")),
                    (False, False, ())):
                with self.subTest(waiting=waiting, active=active):
                    self.app.second.waiting = waiting
                    self.driver.active = active
                    self.app._caption = None
                    self.assertEqual(self.drawn_captions(), expected)

    def test_request_exit_stops_every_slot_in_the_table(self):
        self.driver.active = True
        self.app._arm_watchdog = Mock()
        self.app.w32.destroy_window.return_value = True
        self.app.request_exit()
        self.assertEqual(self.driver.stops, 1)
        self.assertFalse(self.driver.active)
        self.app.request_exit()
        self.assertEqual(self.driver.stops, 1)

    def test_run_stops_every_slot_in_the_table_on_the_way_out(self):
        for loop_fails in (False, True):
            with self.subTest(loop_fails=loop_fails):
                app = make_window_app()
                driver = FakeDriver("active")
                driver.active = True
                app.third = driver
                app._third_before = None
                app.hwnd = 0
                app.w32.create_window.return_value = 11
                if loop_fails:
                    app.w32.run_message_loop.side_effect = RuntimeError("loop failed")
                else:
                    app.w32.run_message_loop.return_value = 0
                app.poll_snapshot = Mock()
                app.refresh_view = Mock()
                app._start_timers = Mock()
                if loop_fails:
                    with self.assertRaises(RuntimeError):
                        app.run()
                else:
                    self.assertEqual(app.run(), 0)
                self.assertEqual(driver.stops, 1)
                self.assertIsNone(widget._APP)


class RewriteSlotCaptionTests(unittest.TestCase):
    def test_only_the_named_item_changes_and_the_deadline_stays(self):
        for index, expected in ((0, ("A!", "b")), (1, ("a", "B!"))):
            with self.subTest(index=index):
                app = make_app()
                app._caption = (DEADLINE, ("a", "b"))
                seen = []

                def rewrite(text):
                    seen.append(text)
                    return text.upper() + "!"

                app._rewrite_slot_caption(index, rewrite)
                self.assertEqual(seen, ["a" if index == 0 else "b"])
                self.assertEqual(app._caption, (DEADLINE, expected))

    def test_nothing_to_rewrite_without_a_caption_and_short_captions_are_padded(self):
        app = make_app()
        rewrite = Mock()
        app._rewrite_slot_caption(1, rewrite)
        rewrite.assert_not_called()
        self.assertIsNone(app._caption)
        app._caption = (DEADLINE, ("only",))
        app._rewrite_slot_caption(1, lambda text: "[%s]" % text)
        self.assertEqual(app._caption, (DEADLINE, ("only", "[]")))


class RealSlotsOnTheSharedPathTests(unittest.TestCase):
    def test_no_file_during_a_cooldown_becomes_cooldown_in_each_providers_own_slot(self):
        for slot in widget.REFRESH_SLOTS:
            with self.subTest(driver=slot.driver):
                app = make_app()
                driver = FakeDriver(slot.busy)
                driver.start_result = "cooldown"
                setattr(app, slot.driver, driver)
                texts = ["new 14:59", "new 14:59"]
                texts[slot.index] = "no file"
                app._caption = (DEADLINE, tuple(texts))
                app._start_slot(slot, None)
                texts[slot.index] = "cooldown"
                self.assertEqual(app._caption, (DEADLINE, tuple(texts)))

    def test_each_provider_method_runs_its_own_slot(self):
        app = make_app()
        app.codex = Mock(available=True)
        with patch.object(app, "_start_slot") as start:
            app._start_codex_ping(1.0)
            app._start_app_refresh(2.0)
            # Refresh now passes redraw=False through the thin entry points.
            app._start_codex_ping(3.0, False)
            app._start_app_refresh(4.0, redraw=False)
        self.assertEqual(
            start.call_args_list,
            [call(widget.CODEX_SLOT, 1.0, True), call(widget.APP_SLOT, 2.0, True),
             call(widget.CODEX_SLOT, 3.0, False), call(widget.APP_SLOT, 4.0, False)])
        with patch.object(app, "_poll_slot") as poll:
            app._on_ping_timer()
            app._on_app_timer()
        self.assertEqual(
            poll.call_args_list,
            [((widget.CODEX_SLOT, app._finish_ping),),
             ((widget.APP_SLOT, app._finish_app_refresh),)])

    def test_stop_and_drop_reach_only_the_slots_own_driver_and_observation(self):
        for slot in widget.REFRESH_SLOTS:
            with self.subTest(driver=slot.driver):
                app = make_app()
                drivers = {}
                for other in widget.REFRESH_SLOTS:
                    drivers[other.driver] = FakeDriver(other.busy)
                    setattr(app, other.driver, drivers[other.driver])
                    setattr(app, other.before, 1.0)
                stops = {other.driver: 0 for other in widget.REFRESH_SLOTS}
                app._stop_slot(slot)
                stops[slot.driver] += 1
                self.assertEqual({name: item.stops for name, item in drivers.items()}, stops)
                # Dropping stops the driver once more, logs the slot's own note and forgets
                # only this slot's click-time observation.
                app._drop_slot(slot)
                stops[slot.driver] += 1
                self.assertEqual({name: item.stops for name, item in drivers.items()}, stops)
                self.assertEqual(
                    [item.args for item in app.log.log.call_args_list],
                    [(slot.log_kind, slot.drop_note)])
                for other in widget.REFRESH_SLOTS:
                    expected = None if other is slot else 1.0
                    self.assertEqual(getattr(app, other.before), expected)

    def test_codex_is_only_asked_when_its_data_source_is_there(self):
        for name, codex in (("no reader", None), ("unavailable", Mock(available=False))):
            with self.subTest(name=name):
                app = make_app()
                app.codex = codex
                with patch.object(app, "_start_slot") as start:
                    app._start_codex_ping(1.0)
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
