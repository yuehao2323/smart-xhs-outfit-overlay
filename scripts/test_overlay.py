"""Behavior checks for geometry, source integrity and batch isolation."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

import overlay


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        Image.new("RGB", (1000, 1400), (122, 160, 180)).save(self.root / "photo.png")
        self.config = {
            "version": 1, "image": "photo.png", "post_type": "OOTD", "sentence_lines": False,
            "source_sha256": overlay.digest(self.root / "photo.png"),
            "source_size": [1000, 1400],
            "analysis": {"people": [{"bbox": [.35, .15, .7, .93],
                                       "faces": [[.4, .15, .55, .25]]}]},
            "boxes": [{"id": "title", "role": "title", "text": "今天穿点明亮的"},
                      {"id": "note", "role": "note", "text": "同色系搭配，出门少一点纠结"}]}
        self.path = self.root / "config.json"

    def render(self, config=None, folder="out"):
        overlay.save_json(self.path, config or self.config)
        overlay.render(self.path, self.root / folder, ("png", "jpg"))
        return overlay.load_json(self.root / folder / "layout.json")

    def test_safe_geometry_and_unchanged_photo_pixels(self):
        report = self.render()
        self.assertEqual(report["mode"], "overlay")
        original = overlay.read_photo(self.root / "photo.png")
        final = overlay.read_photo(self.root / "out/final.png")
        diff = ImageChops.difference(original, final)
        mask = Image.new("L", original.size, 255)
        draw = ImageDraw.Draw(mask)
        protected = [350, 210, 700, 1302]
        boxes = report["boxes"]
        for i, box in enumerate(boxes):
            self.assertFalse(overlay.intersect(box["rect"], protected))
            for other in boxes[:i]:
                self.assertFalse(overlay.intersect(box["rect"], other["rect"]))
            draw.rectangle(box["rect"], fill=0)
            face = ImageFont.truetype(report["font"], box["font_size"])
            for line in box["lines"]:
                self.assertLessEqual(face.getlength(line),
                                     box["rect"][2] - box["rect"][0] - 2 * box["padding"])
            self.assertLessEqual(len(box["lines"]) * box["line_height"] + 2 * box["padding"],
                                 box["rect"][3] - box["rect"][1])
        self.assertIsNone(Image.composite(diff, Image.new("RGB", diff.size), mask).getbbox())
        with Image.open(self.root / "out/final.jpg") as jpg:
            self.assertEqual(jpg.size, original.size)

    def test_full_subject_uses_panel(self):
        config = copy.deepcopy(self.config)
        config["analysis"]["people"][0]["bbox"] = [0, 0, 1, 1]
        report = self.render(config)
        self.assertEqual(report["mode"], "side-panel")
        for box in report["boxes"]:
            self.assertGreater(box["rect"][0], 1000)

    def test_compact_notes_stack_and_use_role_colors(self):
        config = copy.deepcopy(self.config)
        config["distribution"] = "grouped"
        config["boxes"][1]["anchor"] = [.745, .2]
        config["boxes"].append({"id": "extra", "role": "extra", "text": "鞋子换成白色，整体更利落。"})
        report = self.render(config)
        title, note, extra = report["boxes"]
        self.assertEqual([b["resolved_style"] for b in (title, note, extra)],
                         ["black", "white", "gray-white"])
        self.assertTrue(all(b["corner_radius"] == 0 for b in report["boxes"]))
        self.assertEqual(note["rect"][0], extra["rect"][0])
        self.assertEqual(extra["rect"][1] - note["rect"][3], 8)
        self.assertLessEqual(note["padding"], note["font_size"] * .15)

    def test_uniform_font_and_evenly_spaced_centers(self):
        config = copy.deepcopy(self.config)
        config["boxes"].append({"id": "extra", "role": "extra", "text": "白色鞋子收尾。"})
        report = self.render(config)
        boxes = report["boxes"]
        self.assertEqual(len({b["font_size"] for b in boxes}), 1)
        centers = [(b["rect"][1] + b["rect"][3]) / 2 for b in boxes]
        self.assertAlmostEqual(centers[1] - centers[0], centers[2] - centers[1], delta=1)
        self.assertLess(boxes[0]["rect"][0], 350)
        self.assertGreater(boxes[1]["rect"][0], 700)
        self.assertLess(boxes[2]["rect"][0], 350)

    def test_tight_box_shrinks_all_fonts_together(self):
        config = copy.deepcopy(self.config)
        config["boxes"][1].update(text="同色搭配好看" * 5, rect=[.75, .3, .95, .36])
        report = self.render(config)
        sizes = {b["font_size"] for b in report["boxes"]}
        self.assertEqual(len(sizes), 1)
        self.assertLess(next(iter(sizes)), 23)

    def test_cjk_punctuation_does_not_start_wrapped_line(self):
        font = ImageFont.truetype(overlay.font_path(), 30)
        lines = overlay.wrap("同色搭配。白色鞋子，利落收尾。", font, 120)
        self.assertEqual("".join(lines), "同色搭配。白色鞋子，利落收尾。")
        self.assertTrue(all(line[0] not in "，。！？；：" for line in lines if line))

    def test_one_sentence_per_line_without_final_period(self):
        config = copy.deepcopy(self.config)
        config["sentence_lines"] = True
        config["boxes"][1]["text"] = "同色搭配。\n白色鞋子收尾."
        report = self.render(config)
        self.assertEqual(report["boxes"][1]["lines"], ["同色搭配", "白色鞋子收尾"])
        saved = overlay.load_json(self.root / "out/editable.json")
        self.assertEqual(saved["boxes"][1]["text"], "同色搭配\n白色鞋子收尾")
        self.assertIn("同色搭配\n白色鞋子收尾", (self.root / "out/copy.txt").read_text(encoding="utf-8"))

    def test_sentence_mode_does_not_split_long_sentence(self):
        font_file = overlay.font_path()
        with self.assertRaises(overlay.TextFitError):
            overlay.fit("这一整句不能拆开", 80, 500, 30, 24, font_file, keep_sentences=True)

    def test_legacy_cards_keep_rounded_corners(self):
        config = copy.deepcopy(self.config)
        config["preset"] = "cards"
        report = self.render(config)
        self.assertEqual(report["preset"], "cards")
        self.assertTrue(all(b["corner_radius"] > 0 for b in report["boxes"]))

    def test_fixed_face_collision_rejected(self):
        config = copy.deepcopy(self.config)
        config["boxes"][0]["rect"] = [.35, .15, .7, .4]
        with self.assertRaisesRegex(ValueError, "相交"):
            self.render(config)

    def test_long_text_rejected_without_clipping(self):
        config = copy.deepcopy(self.config)
        config["boxes"][0].update(text="长文案" * 50, rect=[.01, .01, .1, .04])
        with self.assertRaisesRegex(ValueError, "无法"):
            self.render(config)

    def test_changed_source_rejected(self):
        Image.new("RGB", (1000, 1400), "red").save(self.root / "photo.png")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.render()

    def test_exif_orientation(self):
        image = Image.new("RGB", (800, 600))
        exif = Image.Exif()
        exif[274] = 6
        path = self.root / "rotated.jpg"
        image.save(path, exif=exif)
        info = overlay.inspect_images([path], self.root / "inspect")[0]
        self.assertEqual(info["source_size"], [600, 800])

    def test_translucent_styles_contrast_on_extreme_backgrounds(self):
        for style in ("black", "white", "gray-white", "gray-black", "palette"):
            for color in ("white", "black"):
                bg, fg, _ = overlay.color_style(style, Image.new("RGB", (20, 20), color), "note")
                base = 255 if color == "white" else 0
                composite = [round(c * bg[3] / 255 + base * (1 - bg[3] / 255)) for c in bg[:3]]
                levels = sorted([overlay.luminance(composite), overlay.luminance(fg)])
                self.assertGreaterEqual((levels[1] + .05) / (levels[0] + .05), 4.5)
                bg, fg, _ = overlay.color_style(style, Image.new("RGB", (20, 20), color), "note", True)
                composite = [round(c * bg[3] / 255 + base * (1 - bg[3] / 255)) for c in bg[:3]]
                levels = sorted([overlay.luminance(composite), overlay.luminance(fg)])
                self.assertGreaterEqual((levels[1] + .05) / (levels[0] + .05), 4.5)

    def test_batch_keeps_success_and_reports_failure(self):
        overlay.save_json(self.path, self.config)
        overlay.save_json(self.root / "batch.json", ["config.json", "missing.json"])
        result = subprocess.run([sys.executable, str(Path(overlay.__file__).resolve()), "batch",
                                 str(self.root / "batch.json"), "--out", str(self.root / "batch")],
                                capture_output=True)
        self.assertEqual(result.returncode, 1)
        report = overlay.load_json(self.root / "batch/batch-report.json")
        self.assertEqual([r["status"] for r in report], ["ok", "error"])
        self.assertTrue((self.root / "batch/001/final.png").is_file())


if __name__ == "__main__":
    unittest.main()
