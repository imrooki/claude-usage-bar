"""Render-path cost guards: native premultiply, cached bar compositor, cached rendered frame.

Everything here is mock-only: no real window, no Win32 call. The checks pin behaviour (identical
pixels, no repeated work), not wall-clock numbers, except one relative speed check: the native
premultiply must beat a per-pixel Python loop by a factor of 10 (measured: about 90).
"""

import pathlib
import random
import sys
import time
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# The sibling import below also has to work when this module is run by its dotted name.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import usage_widget as widget
from test_refresh_feedback import make_app

NOW = 1790900000.0


def random_rgba(rng, width, height):
    count = width * height * 4
    data = rng.getrandbits(8 * count).to_bytes(count, "little")
    return Image.frombytes("RGBA", (width, height), data)


def reference_premultiply(rgba):
    """Pure-Python premultiply of straight RGBA bytes into BGRA bytes, floor(c * a / 255)."""
    out = bytearray(len(rgba))
    for index in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[index:index + 4]
        out[index:index + 4] = (blue * alpha // 255, green * alpha // 255, red * alpha // 255, alpha)
    return bytes(out)


def assert_premultiplied_like_reference(case, image, data):
    """BGRA layout, alpha untouched, every channel within alpha and within 1 of the floor result."""
    raw = image.tobytes()
    case.assertEqual(len(data), len(raw))
    case.assertEqual(data[3::4], raw[3::4])
    expected = reference_premultiply(raw)
    case.assertLessEqual(max(abs(a - b) for a, b in zip(data, expected)), 1)
    for channel in range(3):
        case.assertTrue(all(c <= a for c, a in zip(data[channel::4], data[3::4])),
                        "channel %d exceeds alpha" % channel)


class PremultiplyTests(unittest.TestCase):
    def test_random_images_match_the_reference_within_one_lsb(self):
        rng = random.Random(20261008)
        for size in ((1, 1), (2, 3), (17, 5), (64, 8), (432, 40)):
            with self.subTest(size=size):
                image = random_rgba(rng, *size)
                assert_premultiplied_like_reference(self, image, widget.premultiply_bgra(image))

    def test_every_channel_and_alpha_value_stays_within_alpha(self):
        # Row y is alpha y, column x is channel value x; the three channels take different values
        # so a swapped or shared channel would show up.
        image = Image.new("RGBA", (256, 256))
        image.putdata([(x, 255 - x, (x * 37) % 256, y) for y in range(256) for x in range(256)])
        data = widget.premultiply_bgra(image)
        assert_premultiplied_like_reference(self, image, data)
        # Alpha 0 clears the colour, alpha 255 leaves it untouched.
        self.assertEqual(data[:256 * 4], bytes(256 * 4))
        self.assertEqual(data[255 * 256 * 4:], bytes(
            value for x in range(256) for value in ((x * 37) % 256, 255 - x, x, 255)))

    def test_output_is_blue_green_red_alpha(self):
        image = Image.new("RGBA", (2, 1))
        image.putdata([(10, 20, 30, 255), (200, 100, 50, 128)])
        data = widget.premultiply_bgra(image)
        self.assertEqual(data[:4], bytes((30, 20, 10, 255)))
        blue, green, red, alpha = data[4:8]
        self.assertEqual(alpha, 128)
        # 50, 100 and 200 at alpha 128/255, in blue, green, red order.
        for got, want in ((blue, 25), (green, 50), (red, 100)):
            self.assertLessEqual(abs(got - want), 1)

    def test_input_is_left_alone_and_other_modes_are_rejected(self):
        image = random_rgba(random.Random(3), 9, 4)
        before = image.tobytes()
        widget.premultiply_bgra(image)
        self.assertEqual(image.tobytes(), before)
        for mode in ("RGB", "L", "RGBa"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                widget.premultiply_bgra(Image.new(mode, (2, 2)))

    def test_rendered_frames_keep_the_invariants(self):
        for name, theme, scale in (("dual_mixed", "dark", 1.0), ("dual_green", "light", 1.5),
                                   ("amber", "dark", 2.0), ("caption_new", "light", 1.25),
                                   ("nodata", "dark", 1.0)):
            with self.subTest(name=name, theme=theme, scale=scale):
                image = widget.render_display(widget.sample_display(name, theme, scale))
                assert_premultiplied_like_reference(self, image, widget.premultiply_bgra(image))
                # The dimmed copy sent during the flash obeys the same rule.
                dimmed = widget.dim_image(image, widget.FLASH_ALPHA_SCALE)
                assert_premultiplied_like_reference(self, dimmed, widget.premultiply_bgra(dimmed))

    def test_the_native_path_is_far_faster_than_a_per_pixel_loop(self):
        image = random_rgba(random.Random(5), 432, 40)
        raw = image.tobytes()

        def best_of(function, runs):
            best = float("inf")
            for _ in range(runs):
                started = time.perf_counter()
                function()
                best = min(best, time.perf_counter() - started)
            return best

        native = best_of(lambda: widget.premultiply_bgra(image), 15)
        per_pixel = best_of(lambda: reference_premultiply(raw), 3)
        # Measured: about 0.07 ms against about 7 ms (the table-driven generator this replaced
        # took about 3 ms, so it would fail here too). A factor of 10 leaves room for noise.
        self.assertLess(native * 10, per_pixel)


class ComposeBarCacheTests(unittest.TestCase):
    TRACK = (0, 0, 0, 48)
    FILL = (46, 160, 67, 255)

    def setUp(self):
        widget._compose_bar.cache_clear()

    def test_cache_holds_256_bars(self):
        self.assertEqual(widget._compose_bar.cache_info().maxsize, 256)

    def test_second_call_is_a_hit_and_returns_an_equal_image_with_exact_interior_pixels(self):
        first = widget._compose_bar(100, 7, 3, 60, self.TRACK, self.FILL)
        second = widget._compose_bar(100, 7, 3, 60, self.TRACK, self.FILL)
        info = widget._compose_bar.cache_info()
        self.assertEqual((info.hits, info.misses), (1, 1))
        self.assertEqual((second.mode, second.size), ("RGBA", (100, 7)))
        self.assertEqual(second.tobytes(), first.tobytes())
        self.assertEqual(second.getpixel((30, 3)), self.FILL)
        self.assertEqual(second.getpixel((80, 3)), self.TRACK)
        # A fresh composition (bypassing the cache) gives the same pixels.
        self.assertEqual(widget._compose_bar.__wrapped__(
            100, 7, 3, 60, self.TRACK, self.FILL).tobytes(), second.tobytes())

    def test_different_arguments_are_different_entries(self):
        green = (0x2E, 0xA0, 0x43, 255)
        red = (0xDA, 0x36, 0x33, 255)
        variants = [
            (100, 7, 3, 60, self.TRACK, green),
            (100, 7, 3, 61, self.TRACK, green),
            (100, 7, 3, 60, self.TRACK, red),
            (100, 7, 3, 60, (255, 255, 255, 56), green),
            (100, 7, 3, 0, self.TRACK, None),
            (100, 7, 3, 100, self.TRACK, green),
            (150, 9, 4, 90, self.TRACK, green),
        ]
        images = [widget._compose_bar(*args) for args in variants]
        self.assertEqual(widget._compose_bar.cache_info().misses, len(variants))
        self.assertEqual(len({image.tobytes() for image in images}), len(variants))
        for args, image in zip(variants, images):
            with self.subTest(args=args):
                self.assertEqual(
                    widget._compose_bar.__wrapped__(*args).tobytes(), image.tobytes())

    def test_rendering_reuses_bars_and_never_modifies_a_cached_one(self):
        display = widget.sample_display("dual_mixed", "dark", 1.5)
        real = widget._compose_bar
        returned = []

        def recording(*args):
            image = real(*args)
            returned.append((image, image.tobytes()))
            return image

        with patch.object(widget, "_compose_bar", recording):
            first = widget.render_display(display)
            misses = real.cache_info().misses
            second = widget.render_display(display)
        self.assertEqual(len(returned), 8)
        self.assertEqual(real.cache_info().misses, misses)
        self.assertGreaterEqual(real.cache_info().hits, 4)
        self.assertEqual(second.tobytes(), first.tobytes())
        for image, snapshot in returned:
            self.assertEqual(image.tobytes(), snapshot)


class RenderedFrameCacheTests(unittest.TestCase):
    def live_app(self):
        app = make_app()
        app._apply = widget.WidgetApp._apply.__get__(app, widget.WidgetApp)
        app.geom = None
        return app

    def display(self, theme="dark"):
        return widget.build_display(None, 0.0, theme, 1.0, 36)

    def payloads(self, app):
        return [entry.args[5] for entry in app.w32.update_layered.call_args_list]

    def test_flash_on_and_off_render_the_same_display_once(self):
        app = self.live_app()
        display = self.display()
        geom = (1, 2, display.width, display.height)
        with patch.object(widget, "render_display", wraps=widget.render_display) as render:
            app._flashing = True
            app._apply(display, geom, True)
            # What _end_flash does before it redraws.
            app._flashing = False
            app.shown_display = None
            app._apply(display, geom, True)
        self.assertEqual(render.call_count, 1)
        dimmed, plain = self.payloads(app)
        rendered = widget.render_display(display)
        self.assertEqual(plain, widget.premultiply_bgra(rendered))
        self.assertEqual(dimmed, widget.premultiply_bgra(
            widget.dim_image(rendered, widget.FLASH_ALPHA_SCALE)))
        self.assertNotEqual(dimmed[3::4], plain[3::4])
        self.assertEqual(app.shown_display, display)

    def test_a_changed_display_is_rendered_again_and_an_equal_one_is_not(self):
        app = self.live_app()
        dark, light = self.display("dark"), self.display("light")
        self.assertNotEqual(dark, light)
        with patch.object(widget, "render_display", wraps=widget.render_display) as render:
            app._apply(dark, (1, 2, dark.width, dark.height), False)
            self.assertEqual(render.call_count, 1)
            app._apply(light, (1, 2, light.width, light.height), False)
            self.assertEqual(render.call_count, 2)
            app._apply(light, (1, 2, light.width, light.height), True)
            self.assertEqual(render.call_count, 2)
        self.assertEqual(self.payloads(app)[1], widget.premultiply_bgra(widget.render_display(light)))
        self.assertEqual(self.payloads(app)[2], self.payloads(app)[1])

    def test_an_equal_but_separately_built_display_hits_the_cache(self):
        # The invariant the cache relies on: equal tuples draw the same picture.
        app = self.live_app()
        snapshot = {"five_hour": widget.Win(30.0, NOW + 3600.0, NOW - 60.0),
                    "seven_day": widget.Win(10.0, NOW + 86400.0, NOW - 60.0)}
        first = widget.build_display(snapshot, NOW, "dark", 1.0, 36)
        second = widget.build_display(dict(snapshot), NOW, "dark", 1.0, 36)
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(widget.render_display(first).tobytes(), widget.render_display(second).tobytes())
        with patch.object(widget, "render_display", wraps=widget.render_display) as render:
            app._apply(first, (1, 2, first.width, first.height), True)
            app._apply(second, (1, 2, second.width, second.height), True)
        self.assertEqual(render.call_count, 1)

    def test_dimming_returns_a_new_image_and_leaves_the_cached_one_alone(self):
        app = self.live_app()
        display = self.display()
        geom = (1, 2, display.width, display.height)
        real_dim = widget.dim_image
        seen = {}

        def spy(image, scale):
            seen["source"] = image
            seen["source_bytes"] = image.tobytes()
            seen["result"] = real_dim(image, scale)
            return seen["result"]

        app._flashing = True
        with patch.object(widget, "dim_image", spy):
            app._apply(display, geom, True)
        self.assertIsNot(seen["result"], seen["source"])
        self.assertEqual(seen["source"].tobytes(), seen["source_bytes"])
        self.assertNotEqual(seen["result"].tobytes(), seen["source_bytes"])
        # The undimmed frame that follows is the pristine picture, not a dimmed leftover.
        app._flashing = False
        app._apply(display, geom, True)
        self.assertEqual(self.payloads(app)[1], widget.premultiply_bgra(widget.render_display(display)))

    def test_failed_submit_keeps_the_rendered_frame_for_the_retry(self):
        app = self.live_app()
        display = self.display()
        geom = (1, 2, display.width, display.height)
        app.w32.update_layered.side_effect = [OSError("layered"), None]
        with patch.object(widget, "render_display", wraps=widget.render_display) as render:
            with self.assertRaises(OSError):
                app._apply(display, geom, True)
            self.assertIsNone(app.shown_display)
            app._apply(display, geom, True)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(app.shown_display, display)

    def test_a_whole_refresh_click_renders_once(self):
        app = self.live_app()
        app.poll_snapshot = lambda force=False: None
        app.snapshot_override = {"five_hour": widget.Win(30.0, NOW + 3600.0, NOW - 60.0),
                                 "seven_day": widget.Win(60.0, NOW + 86400.0, NOW - 60.0)}
        app.w32.track_menu.return_value = widget.MENU_REFRESH
        with patch.object(widget.time, "time", return_value=NOW), \
                patch.object(widget, "render_display", wraps=widget.render_display) as render:
            app.on_right_up()
            self.assertTrue(app._flashing)
            app.on_timer(widget.TIMER_FLASH)
        self.assertFalse(app._flashing)
        self.assertEqual(render.call_count, 1)
        dimmed, plain = self.payloads(app)
        self.assertEqual(plain, widget.premultiply_bgra(widget.render_display(app.shown_display)))
        self.assertNotEqual(dimmed[3::4], plain[3::4])


if __name__ == "__main__":
    unittest.main()
