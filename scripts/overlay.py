"""Deterministic photo overlay renderer. Vision and copy are supplied by the Skill host."""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import math
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_photo(path):
    with Image.open(path) as raw:
        # Coordinates always refer to the visually upright image.
        return ImageOps.exif_transpose(raw).convert("RGB")


def font_path(explicit=None):
    candidates = [explicit, os.environ.get("XHS_FONT"),
                  "C:/Windows/Fonts/msyh.ttc",
                  "/System/Library/Fonts/PingFang.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise ValueError("找不到中文字体。请用 --font 或 XHS_FONT 指定中文 TTF/OTF/TTC 字体。")


def rectangle(value, label):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label}: 需要 [x0,y0,x1,y1]")
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
           for v in value):
        raise ValueError(f"{label}: 坐标必须是有限数字")
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError(f"{label}: 坐标必须归一化到 0..1 且右下角大于左上角")
    return value


def intersect(a, b, gap=0):
    return (a[0] < b[2] + gap and a[2] > b[0] - gap and
            a[1] < b[3] + gap and a[3] > b[1] - gap)


def validate(config):
    if config.get("version") != 1:
        raise ValueError("仅支持 version: 1")
    if config.get("preset", "compact-notes") not in ("compact-notes", "cards"):
        raise ValueError("preset 必须是 compact-notes / cards")
    if config.get("distribution", "balanced") not in ("balanced", "grouped"):
        raise ValueError("distribution 必须是 balanced / grouped")
    if not isinstance(config.get("uniform_font", True), bool):
        raise ValueError("uniform_font 必须是布尔值")
    if not isinstance(config.get("sentence_lines", True), bool):
        raise ValueError("sentence_lines 必须是布尔值")
    if not isinstance(config.get("post_type"), str) or not config["post_type"].strip():
        raise ValueError("缺少 post_type")
    analysis = config.get("analysis", {})
    if not isinstance(analysis.get("people"), list) or not analysis["people"]:
        raise ValueError("analysis.people 不能为空；必须先完成视觉分析")
    protected = []
    for i, person in enumerate(analysis["people"]):
        protected.append(rectangle(person["bbox"], f"people[{i}].bbox"))
        for face in person.get("faces", []):
            protected.append(rectangle(face, "face"))
    for box in analysis.get("protected_regions", []):
        protected.append(rectangle(box, "protected_regions"))
    for box in analysis.get("whitespace", []):
        rectangle(box, "whitespace")
    boxes = config.get("boxes", [])
    if sum(b.get("role") == "title" for b in boxes) != 1:
        raise ValueError("需要且仅需一个 title")
    if not 1 <= sum(b.get("role") == "note" for b in boxes) <= 3:
        raise ValueError("需要 1~3 个 note")
    if sum(b.get("role") == "extra" for b in boxes) > 1:
        raise ValueError("最多一个 extra")
    ids = set()
    for box in boxes:
        if not box.get("id") or box["id"] in ids:
            raise ValueError("每个文字框需要唯一 id")
        ids.add(box["id"])
        if box.get("role") not in ("title", "note", "extra"):
            raise ValueError("role 必须是 title / note / extra")
        if not isinstance(box.get("text"), str) or not box["text"].strip():
            raise ValueError("文字不能为空")
        if len(box["text"]) > 200:
            raise ValueError("单框文案超过 200 字，请精简文案")
        if box.get("style", "auto") not in STYLES:
            raise ValueError("不支持的文字框样式")
        if "rect" in box:
            rectangle(box["rect"], "box.rect")
        if "anchor" in box:
            anchor = box["anchor"]
            if (not isinstance(anchor, list) or len(anchor) != 2 or
                    any(isinstance(v, bool) or not isinstance(v, (float, int)) or
                        not math.isfinite(v) or not 0 <= v <= 1 for v in anchor)):
                raise ValueError("anchor 必须是 0..1 范围内的 [x,y]")
    return protected


def wrap(text, font, width):
    """Measure actual glyph advance and ink; explicit newlines remain editable."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            probe = line + char
            bounds = font.getbbox(probe)
            measured = max(font.getlength(probe), bounds[2] - bounds[0])
            if measured > width:
                if not line:
                    return None
                split = len(line)
                while split > 0 and (probe[split] in "，。！？；：、）》】」』,.!?;:%" or
                                     probe[split - 1] in "（《【「『("):
                    split -= 1
                if not split:
                    return None
                lines.append(probe[:split].rstrip())
                line = probe[split:].lstrip()
                bounds = font.getbbox(line)
                if max(font.getlength(line), bounds[2] - bounds[0]) > width:
                    return None
            else:
                line = probe
        lines.append(line.rstrip())
    return lines


class TextFitError(ValueError):
    pass


def sentence_lines(text):
    return [line.strip().rstrip("。.") for line in re.split(r"[。！？!?；;\n]+", text)
            if line.strip().rstrip("。.")]


def fit(text, width, max_height, target, minimum, font_file, max_lines=6, compact=False,
        keep_sentences=False):
    pad = max(3, round(target * .10)) if compact else max(12, round(target * .48))
    for size in range(int(target), int(minimum) - 1, -1):
        font = ImageFont.truetype(font_file, size)
        if keep_sentences:
            lines = sentence_lines(text)
            if not lines or any(max(font.getlength(s), font.getbbox(s)[2] - font.getbbox(s)[0]) >
                                width - 2 * pad for s in lines):
                continue
        else:
            lines = wrap(text, font, width - 2 * pad)
        if lines is None or len(lines) > max_lines:
            continue
        line_height = max(round(size * (1.10 if compact else 1.38)), max(font.getbbox(s or "中")[3] -
                          font.getbbox(s or "中")[1] for s in lines))
        height = len(lines) * line_height + 2 * pad
        if height <= max_height:
            return {"lines": lines, "font_size": size, "line_height": line_height,
                    "padding": pad, "height": height}
    raise TextFitError("文案无法在可读字号下放入文字框，请缩短文案或加宽文字框")


STYLES = {"auto", "black", "white", "gray-white", "gray-black", "palette"}


def luminance(rgb):
    linear = [v / 255 / 12.92 if v / 255 <= .04045 else
              ((v / 255 + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))


def color_style(style, crop, role, compact=False):
    avg = tuple(round(v) for v in ImageStat.Stat(crop).mean[:3])
    if style == "auto":
        style = ("black" if role == "title" else "gray-white" if role == "extra" else "white") if compact else (
            "black" if role == "title" else ("white" if luminance(avg) < .6 else "black"))
    if style == "black":
        return ((0, 0, 0, 255) if compact else (22, 23, 24, 244)), (255, 255, 255), style
    if style == "white":
        return ((255, 255, 255, 255) if compact else (250, 249, 246, 248)), (25, 26, 27), style
    if style == "palette":
        tiny = crop.resize((48, 48)).quantize(colors=4).convert("RGB")
        common = max(tiny.getcolors(2304), key=lambda pair: pair[0])[1]
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in common))
        bg = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, min(s, .2), max(v, .8)))
        return (*bg, 255), (22, 23, 24), style
    # Constrain opacity so even extreme underlying pixels keep text contrast.
    if style == "gray-white":
        return ((76, 76, 76, 235) if compact else (48, 50, 52, 220)), (255, 255, 255), style
    return (222, 223, 223, 232), (20, 21, 22), style


def place_boxes(photo, config, protected, font_file, common_size=None):
    width, height = photo.size
    compact = config.get("preset", "compact-notes") == "compact-notes"
    balanced = config.get("distribution", "balanced") == "balanced"
    blocked = [[round(v * (width if i % 2 == 0 else height)) for i, v in enumerate(b)]
               for b in protected]
    margin, gap = round(width * (.025 if compact else .04)), max(6, round(width * (.008 if compact else .014)))
    edges = photo.convert("L").resize((180, 240)).filter(ImageFilter.FIND_EDGES)
    placements = []
    for index, box in enumerate(config["boxes"]):
        title = box["role"] == "title"
        target = max(20, round(width * ((.034 if compact else .039) if title else .023)))
        minimum = max(16, round(width * (.026 if title else .018)))
        if common_size is not None:
            target = minimum = common_size
        candidates = []
        if "rect" in box:
            r = [round(v * (width if i % 2 == 0 else height)) for i, v in enumerate(box["rect"])]
            fitted = fit(box["text"], r[2] - r[0], r[3] - r[1], target, minimum, font_file,
                         max_lines=10 if compact else 6, compact=compact,
                         keep_sentences=config.get("sentence_lines", True))
            r[3] = r[1] + fitted["height"]
            if any(intersect(r, b, gap) for b in blocked + [p["rect"] for p in placements]):
                raise ValueError(f"固定文字框 {box['id']} 与人物或其他文字框相交")
            placements.append({**box, **fitted, "rect": r})
            continue
        fractions = ([.60, .48, .36, .28, .24] if title else [.48, .42, .34, .27, .23, .21]) if compact else (
            [.31, .28, .25] if title else [.27, .24, .21])
        for fraction in fractions:
            box_width = round(width * fraction)
            try:
                fitted = fit(box["text"], box_width, height * .21, target, minimum,
                             font_file, (3 if title else 9) if compact else (3 if title else 5), compact,
                             config.get("sentence_lines", True))
            except ValueError:
                continue
            bh = fitted["height"]
            xs = [margin, width - margin - box_width]
            xs += list(range(margin, width - margin - box_width + 1, max(1, width // 16)))
            ys = list(range(margin, height - margin - bh + 1, max(1, height // 28)))
            if balanced:
                # Equal vertical center spacing, alternating edge alignment.
                center_y = .14 + .68 * index / max(1, len(config["boxes"]) - 1)
                balance_anchor = [margin / width if index % 2 == 0 else
                                  (width - margin - box_width) / width,
                                  max(margin / height, center_y - bh / (2 * height))]
                xs.insert(0, round(balance_anchor[0] * width))
                ys.insert(0, round(balance_anchor[1] * height))
            if not balanced and compact and placements and placements[-1]["role"] != "title":
                xs.insert(0, placements[-1]["rect"][0])
                ys.insert(0, placements[-1]["rect"][3] + gap)
            if "anchor" in box:
                ax, ay = box["anchor"]
                xs.insert(0, round(ax * width))
                ys.insert(0, round(ay * height))
            for x in xs:
                for y in ys:
                    r = [x, y, x + box_width, y + bh]
                    if x < margin or y < margin or r[2] > width - margin or r[3] > height - margin:
                        continue
                    if any(intersect(r, b, gap) for b in blocked + [p["rect"] for p in placements]):
                        continue
                    e = edges.crop((int(x / width * 180), int(y / height * 240),
                                    max(int(x / width * 180) + 1, int(r[2] / width * 180)),
                                    max(int(y / height * 240) + 1, int(r[3] / height * 240))))
                    texture = ImageStat.Stat(e).mean[0] / 255
                    if balanced:
                        default_anchor = balance_anchor
                    elif compact:
                        previous = placements[-1] if placements and not title else None
                        default_anchor = ([.025, .055] if title else
                                          [previous["rect"][0] / width,
                                           (previous["rect"][3] + gap) / height]
                                          if previous and previous["role"] != "title" else [.52, .17])
                    else:
                        default_anchor = [.05 if index % 2 == 0 else .74,
                                          .055 if title else min(.85, .23 + index * .18)]
                    preferred = box.get("anchor", default_anchor)
                    distance = abs(x / width - preferred[0]) + abs(y / height - preferred[1])
                    in_whitespace = any(x / width >= b[0] and y / height >= b[1] and
                                        r[2] / width <= b[2] and r[3] / height <= b[3]
                                        for b in config["analysis"].get("whitespace", []))
                    score = distance + texture * .7 - .14 * in_whitespace + .1 * (.31 - fraction)
                    candidates.append((score, r, fitted))
        if not candidates:
            return None
        _, r, fitted = min(candidates, key=lambda candidate: candidate[0])
        placements.append({**box, **fitted, "rect": r})
    return placements


def side_panel(photo, config, font_file, common_size=None):
    width, height = photo.size
    panel_width = max(round(width * .48), 440)
    canvas = Image.new("RGB", (width + panel_width, height), (239, 237, 231))
    canvas.paste(photo, (0, 0))
    margin = round(panel_width * .08)
    y = margin
    placements = []
    budget = (height - margin * 2) // len(config["boxes"]) - margin
    for box in config["boxes"]:
        title = box["role"] == "title"
        fitted = fit(box["text"], panel_width - margin * 2, budget,
                     common_size if common_size is not None else max(20, round(width * (.039 if title else .023))),
                     common_size if common_size is not None else max(16, round(width * .018)), font_file,
                     max_lines=10, compact=config.get("preset", "compact-notes") == "compact-notes",
                     keep_sentences=config.get("sentence_lines", True))
        r = [width + margin, y, width + panel_width - margin, y + fitted["height"]]
        placements.append({**box, **fitted, "rect": r})
        y = r[3] + margin
    if config.get("distribution", "balanced") == "balanced":
        free = height - 2 * margin - sum(p["height"] for p in placements)
        step = free / max(1, len(placements) - 1)
        y = margin
        for p in placements:
            p["rect"][1], p["rect"][3] = round(y), round(y) + p["height"]
            y += p["height"] + step
    return canvas, placements


def render(config_path, out_dir, formats=("png",), font=None, long_edge=4096):
    config_path = Path(config_path).resolve()
    config = load_json(config_path)
    if config.get("sentence_lines", True) is True:
        for box in config.get("boxes", []):
            if isinstance(box.get("text"), str):
                box["text"] = "\n".join(sentence_lines(box["text"]))
    protected = validate(config)
    photo_path = (config_path.parent / config["image"]).resolve()
    if not config.get("source_sha256") or digest(photo_path) != config["source_sha256"]:
        raise ValueError("原图哈希缺失或不匹配；请重新分析当前图片，避免复用旧坐标")
    photo = read_photo(photo_path)
    if list(photo.size) != config.get("source_size"):
        raise ValueError("source_size 与 EXIF 转正后的原图尺寸不匹配")
    original_size = list(photo.size)
    if max(photo.size) > long_edge:
        photo.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    font_file = font_path(font)
    uniform = config.get("uniform_font", True)
    sizes = list(range(max(20, round(photo.width * .023)),
                       max(16, round(photo.width * .018)) - 1, -1)) if uniform else [None]
    placements = None
    fit_error = None
    for size in sizes:
        try:
            placements = place_boxes(photo, config, protected, font_file, size)
            fit_error = None
        except TextFitError as exc:
            fit_error = exc
            continue
        if placements is not None:
            break
    mode = "overlay"
    if placements is None:
        if fit_error is not None:
            raise fit_error
        if config.get("fallback", "panel") != "panel":
            raise ValueError("没有足够安全留白；请缩短文案或允许 fallback: panel")
        for size in sizes:
            try:
                panel, placements = side_panel(photo, config, font_file, size)
                photo = panel
                break
            except TextFitError as exc:
                fit_error = exc
        if placements is None:
            raise fit_error
        mode = "side-panel"
    canvas = photo.convert("RGBA")
    layer = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(layer)
    compact = config.get("preset", "compact-notes") == "compact-notes"
    for box in placements:
        r = box["rect"]
        bg, fg, selected = color_style(box.get("style", "auto"), photo.crop(tuple(r)), box["role"], compact)
        box["resolved_style"] = selected
        box["background"] = list(bg)
        box["foreground"] = list(fg)
        box["corner_radius"] = 0 if compact else max(3, round(box["font_size"] * .16))
        draw.rounded_rectangle(r, radius=box["corner_radius"], fill=bg)
        face = ImageFont.truetype(font_file, box["font_size"])
        for i, line in enumerate(box["lines"]):
            bounds = face.getbbox(line or " ")
            # Offset bearings to guarantee actual ink stays inside the padded box.
            draw.text((r[0] + box["padding"] - bounds[0],
                       r[1] + box["padding"] + i * box["line_height"] - bounds[1]),
                      line, font=face, fill=(*fg, 255))
    canvas = Image.alpha_composite(canvas, layer).convert("RGB")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for fmt in formats:
        if fmt not in ("png", "jpg"):
            raise ValueError("输出格式仅支持 png / jpg")
        canvas.save(out / f"final.{fmt}", **({"quality": 96, "subsampling": 0} if fmt == "jpg" else {}))
    # Save a self-contained editable project with a byte-identical source copy.
    source_copy = out / ("source" + photo_path.suffix.lower())
    if source_copy != photo_path:
        source_copy.write_bytes(photo_path.read_bytes())
    editable = {**config, "image": source_copy.name}
    save_json(out / "editable.json", editable)
    report = {"mode": mode, "preset": config.get("preset", "compact-notes"),
              "uniform_font": uniform, "distribution": config.get("distribution", "balanced"),
              "sentence_lines": config.get("sentence_lines", True),
              "original_size": original_size, "rendered_size": list(canvas.size),
              "font": font_file, "boxes": placements,
              "note": "人物区域来自视觉模型估计；已按给定区域检查零相交，仍需视觉验收。"}
    save_json(out / "layout.json", report)
    text = "\n\n".join(f"[{b['role']}]\n{b['text']}" for b in config["boxes"])
    if config.get("caption"):
        text += "\n\n[帖子正文]\n" + config["caption"]
    (out / "copy.txt").write_text(text + "\n", encoding="utf-8")
    return {"config": str(config_path), "output": str(out), "mode": mode,
            "size": list(canvas.size), "status": "ok"}


def inspect_images(paths, out_dir):
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = []
    for i, path in enumerate(paths):
        path = Path(path).resolve()
        photo = read_photo(path)
        size = list(photo.size)
        photo.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        preview = out / f"preview-{i + 1:03d}.jpg"
        photo.save(preview, quality=92)
        result.append({"image": str(path), "source_sha256": digest(path),
                       "source_size": size, "preview": str(preview)})
    save_json(out / "inspection.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="小红书穿搭文字框：原图保留、自动避让、可编辑配置")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="准备 EXIF 转正预览、原图尺寸和指纹，供视觉模型分析")
    inspect.add_argument("images", nargs="+")
    inspect.add_argument("--out", required=True)
    for command in ("render", "batch"):
        p = sub.add_parser(command)
        p.add_argument("config", help="render: 配置 JSON；batch: JSON 数组，每项一个配置文件路径")
        p.add_argument("--out", required=True)
        p.add_argument("--format", choices=("png", "jpg", "both"), default="both")
        p.add_argument("--font")
        p.add_argument("--long-edge", type=int, default=4096, help="原图最长边上限；不会放大小图")
    args = parser.parse_args()
    if args.command == "inspect":
        result = inspect_images(args.images, args.out)
    else:
        if args.long_edge < 640:
            parser.error("--long-edge 至少为 640")
        formats = ("png", "jpg") if args.format == "both" else (args.format,)
        if args.command == "render":
            result = render(args.config, args.out, formats, args.font, args.long_edge)
        else:
            manifest_path = Path(args.config).resolve()
            manifest = load_json(manifest_path)
            if not isinstance(manifest, list) or not manifest:
                raise ValueError("批处理清单必须是非空配置路径数组")
            result = []
            for i, path in enumerate(manifest):
                try:
                    result.append(render(manifest_path.parent / path,
                                         Path(args.out) / f"{i + 1:03d}",
                                         formats, args.font, args.long_edge))
                except Exception as exc:
                    result.append({"config": str(path), "status": "error", "error": str(exc)})
            Path(args.out).mkdir(parents=True, exist_ok=True)
            save_json(Path(args.out) / "batch-report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "batch" and any(item["status"] == "error" for item in result):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
