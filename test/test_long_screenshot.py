import unittest

from PIL import Image, ImageChops, ImageDraw, ImageStat

import screenshot_app as app


def make_content(width: int = 240, height: int = 1200) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 18):
        color = ((y * 7) % 220 + 20, (y * 11) % 210 + 25, (y * 13) % 200 + 30)
        draw.rectangle((8, y + 2, width - 9, y + 11), fill=color)
        draw.line((0, y, width, y), fill="#cbd5e1")
    return image


def difference(first: Image.Image, second: Image.Image) -> float:
    return max(ImageStat.Stat(ImageChops.difference(first, second)).mean)


class ScheduledRoot:
    def __init__(self) -> None:
        self.jobs = []
        self.cancelled = set()
        self.next_id = 1

    def after(self, delay, callback):
        del delay
        identifier = f"job-{self.next_id}"
        self.next_id += 1
        self.jobs.append((identifier, callback))
        return identifier

    def after_cancel(self, identifier) -> None:
        self.cancelled.add(identifier)

    def run(self, limit: int = 100) -> None:
        steps = 0
        while self.jobs and steps < limit:
            identifier, callback = self.jobs.pop(0)
            if identifier not in self.cancelled:
                callback()
            steps += 1


class FakeCapturePanel:
    def __init__(self, *args) -> None:
        self.updates = []
        self.closed = False

    def update_result(self, *args) -> None:
        self.updates.append(args)

    def set_paused(self, paused: bool) -> None:
        del paused

    def capture_without_panel(self, callback) -> None:
        callback()

    def show_after_capture(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class LongScreenshotAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = make_content()
        self.settings = app.LongScreenshotSettings(min_overlap=50)

    def test_stitches_regular_scrolling_content_exactly(self) -> None:
        assembler = app.LongScreenshotAssembler()
        for offset in (0, 120, 240, 360):
            result = assembler.append(
                self.content.crop((0, offset, 240, offset + 300)),
                self.settings,
            )

        expected = self.content.crop((0, 0, 240, 660))
        self.assertEqual(result.state, "added")
        self.assertEqual(assembler.image().size, expected.size)
        self.assertEqual(difference(assembler.image(), expected), 0.0)

    def test_keeps_fixed_header_and_footer_only_once(self) -> None:
        header = Image.new("RGB", (240, 30), "#123456")
        footer = Image.new("RGB", (240, 25), "#222222")
        ImageDraw.Draw(header).text((8, 8), "FIXED", fill="white")
        ImageDraw.Draw(footer).text((8, 4), "FOOTER", fill="white")
        assembler = app.LongScreenshotAssembler()

        for offset in (0, 90, 180, 270):
            frame = Image.new("RGB", (240, 300), "white")
            frame.paste(header, (0, 0))
            frame.paste(self.content.crop((0, offset, 240, offset + 245)), (0, 30))
            frame.paste(footer, (0, 275))
            result = assembler.append(frame, self.settings)

        expected = Image.new("RGB", (240, 570), "white")
        expected.paste(header, (0, 0))
        expected.paste(self.content.crop((0, 0, 240, 515)), (0, 30))
        expected.paste(footer, (0, 545))
        self.assertEqual(result.match.static_bottom, 25)
        self.assertEqual(assembler.image().size, expected.size)
        self.assertEqual(difference(assembler.image(), expected), 0.0)

    def test_duplicate_frame_does_not_extend_result(self) -> None:
        assembler = app.LongScreenshotAssembler()
        frame = self.content.crop((0, 0, 240, 300))

        assembler.append(frame, self.settings)
        result = assembler.append(frame.copy(), self.settings)

        self.assertEqual(result.state, "duplicate")
        self.assertEqual(result.image.size, (240, 300))
        self.assertEqual(result.accepted_frames, 1)

    def test_unrelated_frame_returns_partial_content_without_duplication(self) -> None:
        assembler = app.LongScreenshotAssembler()
        first = self.content.crop((0, 0, 240, 300))
        second = self.content.crop((0, 120, 240, 420))
        unrelated = Image.new("RGB", (240, 300), "#ff00ff")

        assembler.append(first, self.settings)
        assembler.append(second, self.settings)
        before = assembler.image()
        result = assembler.append(unrelated, self.settings)

        self.assertEqual(result.state, "unmatched")
        self.assertEqual(result.image.size, before.size)
        self.assertEqual(difference(result.image, before), 0.0)

    def test_small_dynamic_header_area_does_not_shift_the_seam(self) -> None:
        content = make_content(width=500, height=1600)
        assembler = app.LongScreenshotAssembler()

        for index, offset in enumerate((0, 170, 340, 510)):
            frame = Image.new("RGB", (500, 500), "white")
            frame.paste(content.crop((0, offset, 500, offset + 450)), (0, 50))
            draw = ImageDraw.Draw(frame)
            draw.rectangle((0, 0, 499, 49), fill="#1f2937")
            draw.text((15, 15), "Toolbar", fill="white")
            draw.rectangle((430, 10, 485, 35), fill=(index * 50, 80, 120))
            result = assembler.append(frame, self.settings)

        self.assertEqual(result.state, "added")
        self.assertEqual(result.match.overlap, 330)
        self.assertEqual(result.added_height, 170)
        self.assertEqual(assembler.image().size, (500, 1010))


class LongScreenshotControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = make_content(width=220, height=900)
        self.frames = [
            self.content.crop((0, offset, 220, offset + 260))
            for offset in (0, 100, 200, 300)
        ]

    def test_auto_mode_scrolls_and_finishes_incrementally(self) -> None:
        root = ScheduledRoot()
        outputs = []
        errors = []
        scrolls = []
        frames = iter(self.frames)
        controller = app.LongScreenshotController(
            root,
            (0, 0, 220, 260),
            app.LongScreenshotSettings(
                max_frames=4,
                min_overlap=50,
                capture_mode="auto",
            ),
            lambda message: None,
            outputs.append,
            errors.append,
            capture_func=lambda: next(frames),
            scroll_func=lambda box, amount: scrolls.append((box, amount)),
            panel_factory=FakeCapturePanel,
        )

        controller.start()
        root.run()

        self.assertFalse(errors)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].image.size, (220, 560))
        self.assertEqual(outputs[0].reason, "limit")
        self.assertEqual(len(scrolls), 3)

    def test_manual_mode_accepts_only_stable_frames(self) -> None:
        root = ScheduledRoot()
        outputs = []
        errors = []
        sequence = iter(
            [
                self.frames[0],
                self.frames[0],
                self.frames[1],
                self.frames[1],
                self.frames[2],
                self.frames[2],
            ]
        )
        controller = app.LongScreenshotController(
            root,
            (0, 0, 220, 260),
            app.LongScreenshotSettings(
                max_frames=3,
                min_overlap=50,
                capture_mode="manual",
            ),
            lambda message: None,
            outputs.append,
            errors.append,
            capture_func=lambda: next(sequence),
            scroll_func=lambda box, amount: None,
            panel_factory=FakeCapturePanel,
        )

        controller.start()
        root.run()

        self.assertFalse(errors)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].accepted_frames, 3)
        self.assertEqual(outputs[0].image.size, (220, 460))


if __name__ == "__main__":
    unittest.main()
