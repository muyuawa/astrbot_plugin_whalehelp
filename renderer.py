"""Offline menu rendering with uploaded backgrounds and a CJK font dependency."""

import hashlib
import json
import logging
import struct
import time
import uuid
from dataclasses import asdict
from functools import lru_cache
from itertools import groupby
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pylopdf_fonts_zh_cn import sans_path

ASSETS = Path(__file__).parent / "assets"
INK = "#443947"
MUTED = "#766775"
PINK = "#99536F"
BLUE = "#496F96"
WIDTH = 1080
MARGIN = 56
CARD_WIDTH = 472
logger = logging.getLogger("astrbot")


@lru_cache(maxsize=32)
def font(size):
    return ImageFont.truetype(str(sans_path()), size)


def _length(text, size):
    return font(size).getlength(str(text))


def _wrap(text, size, width):
    lines, line = [], ""
    for char in str(text):
        if char == "\n":
            lines.append(line)
            line = ""
        elif line and _length(line + char, size) > width:
            lines.append(line.rstrip())
            line = char.lstrip()
        else:
            line += char
    if line:
        lines.append(line.rstrip())
    return lines or [""]


def _elide(text, size, width):
    text = str(text)
    if _length(text, size) <= width:
        return text
    while text and _length(text + "…", size) > width:
        text = text[:-1]
    return text + "…"


def _text(draw, xy, text, size, fill=INK, bold=False):
    draw.text(
        xy,
        str(text),
        font=font(size),
        fill=fill,
        anchor="lt",
        stroke_width=0.4 if bold else 0,
    )


def _pill(draw, x, y, text, fill="#F9E5EE", ink=PINK, size=19, height=36):
    width = round(_length(text, size)) + 28
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=fill)
    _text(draw, (x + 14, y + (height - size) // 2 - 1), text, size, ink)
    return width


def _sparkle(draw, x, y, radius, color):
    draw.polygon(
        [
            (x, y - radius),
            (x + 3, y - 3),
            (x + radius, y),
            (x + 3, y + 3),
            (x, y + radius),
            (x - 3, y + 3),
            (x - radius, y),
            (x - 3, y - 3),
        ],
        fill=color,
    )


def _row(entry, prefix):
    entry_prefix = prefix if entry.use_prefix else ""
    label = entry_prefix + entry.command if entry.kind == "command" else entry.command
    command = _wrap(label, 27, CARD_WIDTH - 52)
    usage = _wrap(entry.usage, 18, CARD_WIDTH - 52) if entry.usage else []
    description = _wrap(entry.description, 22, CARD_WIDTH - 52)
    alias_lines, line = [], "别名  "
    for name in entry.aliases:
        label = entry_prefix + name
        joined = line + (" · " if line != "别名  " else "") + label
        if _length(joined, 18) > CARD_WIDTH - 52 and line != "别名  ":
            alias_lines.append(line)
            line = label
        else:
            line = joined
    if entry.aliases:
        alias_lines.append(_elide(line, 18, CARD_WIDTH - 52))
    height = len(command) * 36 + len(usage) * 27 + len(description) * 31
    height += len(alias_lines) * 29
    if entry.conditional:
        height += 28
    return dict(
        command=command,
        usage=usage,
        description=description,
        alias=alias_lines,
        conditional=entry.conditional,
        height=height + 38,
    )


class MenuRenderer:
    def __init__(self, root, title="", subtitle="", bot_name="AstrBot"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.title = str(title)
        self.subtitle = str(subtitle)
        self.extra_text = ""
        self.bot_name = str(bot_name).strip() or "AstrBot"
        self.background = ""
        self.background_fade = 60
        self._last_background_warning = None

    @property
    def display_title(self):
        return self.title or f"{self.bot_name}的帮助菜单"

    def render(self, page, prefix="/", trigger="菜单"):
        """Render a page to a content-addressed PNG. Call via asyncio.to_thread."""
        background, background_stamp = self._background_source()
        extra_text = str(self.extra_text or "").strip()
        try:
            fade = max(0, min(100, int(self.background_fade)))
        except (TypeError, ValueError, OverflowError):
            fade = 60
        payload = json.dumps(
            [
                "layout-v8",
                asdict(page),
                self.bot_name,
                self.title,
                self.subtitle,
                extra_text,
                prefix,
                trigger,
                background_stamp,
                fade,
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
        output = self.root / f"whale-{digest}.png"
        self._cleanup()
        if output.is_file() and output.stat().st_size:
            output.touch()
            return output

        cards, columns = [], [512, 512]
        for _, grouped in groupby(page.entries, key=lambda e: e.plugin):
            group = list(grouped)
            for start in range(0, len(group), 4):
                entries = group[start : start + 4]
                rows = [_row(e, prefix) for e in entries]
                height = 96 + sum(r["height"] for r in rows) - 10
                column = min(range(2), key=lambda n: columns[n])
                cards.append(
                    dict(
                        x=MARGIN + column * (CARD_WIDTH + 24),
                        y=columns[column],
                        height=height,
                        label=entries[0].plugin_label,
                        rows=rows,
                        continued=start > 0,
                    )
                )
                columns[column] += height + 24
        body_bottom = max(columns) if cards else 790
        footer_y = body_bottom + 12
        extra_lines = _wrap(extra_text, 18, WIDTH - 128) if extra_text else []
        extra_height = len(extra_lines) * 29 + 16 if extra_lines else 0
        height = max(1080, footer_y + 214 + extra_height)
        canvas = self._background(height, background, fade)
        self._header(canvas, page)

        shadow = Image.new("RGBA", canvas.size)
        shadow_draw = ImageDraw.Draw(shadow)
        for card in cards:
            x, y, h = card["x"], card["y"], card["height"]
            shadow_draw.rounded_rectangle(
                (x + 2, y + 8, x + CARD_WIDTH - 2, y + h + 8),
                radius=28,
                fill=(113, 73, 102, 16),
            )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
        draw = ImageDraw.Draw(canvas)
        for index, card in enumerate(cards):
            self._card(draw, card, index)
        if not cards:
            self._empty(draw, page)
        self._footer(draw, page, footer_y, prefix, trigger, extra_lines)
        temporary = self.root / f"{uuid.uuid4().hex}.tmp"
        try:
            canvas.convert("RGB").save(temporary, format="PNG", optimize=True)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    def _cleanup(self):
        cached = sorted(
            self.root.glob("whale-*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        cutoff = time.time() - 86400
        for index, path in enumerate(cached):
            if index >= 64 or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def _background_source(self):
        """AstrBot's file configuration stores paths relative to plugin_data."""
        if not isinstance(self.background, str) or not self.background.strip():
            return None, None
        try:
            relative = Path(self.background)
            storage = self.root.parent.resolve()
            path = (storage / relative).resolve()
            path.relative_to(storage)
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != "files"
            ):
                return None, None
            stamp = path.stat()
            return path, (
                str(path),
                stamp.st_mtime_ns,
                stamp.st_ctime_ns,
                stamp.st_size,
                stamp.st_ino,
            )
        except (OSError, ValueError):
            return None, None

    def _background(self, height, background=None, fade=60):
        if background is not None:
            try:
                if background.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("image exceeds 32 MiB")
                with Image.open(background) as source:
                    if source.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError("unsupported image format")
                    if source.width * source.height > 24_000_000:
                        raise ValueError("image exceeds 24 megapixels")
                    fitted = ImageOps.fit(
                        ImageOps.exif_transpose(source).convert("RGBA"),
                        (WIDTH, height),
                        method=Image.Resampling.LANCZOS,
                    )
                canvas = Image.new("RGBA", (WIDTH, height), "white")
                canvas.alpha_composite(fitted)
                canvas.alpha_composite(
                    Image.new(
                        "RGBA", canvas.size, (255, 255, 255, round(255 * fade / 100))
                    )
                )
                return canvas
            except (
                OSError,
                ValueError,
                SyntaxError,
                struct.error,
                Image.DecompressionBombError,
            ) as exc:
                warning = (str(background), str(exc))
                if warning != self._last_background_warning:
                    logger.warning(
                        "WhaleHelp: 自定义背景无法读取，使用默认背景 (%s)",
                        type(exc).__name__,
                    )
                    self._last_background_warning = warning
        canvas = Image.new("RGBA", (WIDTH, height))
        draw = ImageDraw.Draw(canvas)
        top, bottom = (255, 244, 249), (242, 248, 255)
        for y in range(height):
            t = y / (height - 1)
            color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
            draw.line((0, y, WIDTH, y), fill=(*color, 255))
        decor = Image.new("RGBA", canvas.size)
        d = ImageDraw.Draw(decor)
        d.ellipse((-150, -230, 630, 470), fill=(255, 222, 237, 65))
        d.ellipse((700, 90, 1050, 440), fill=(220, 237, 255, 170))
        d.arc((650, 42, 1100, 490), 180, 315, fill=(255, 255, 255, 200), width=2)
        d.ellipse((38, 405, 50, 417), fill=(219, 192, 209, 170))
        _sparkle(d, 1015, 470, 11, (153, 189, 220, 160))
        canvas.alpha_composite(decor)
        # The same original artwork softly decorates the lower page background.
        with Image.open(ASSETS / "whale-girl.png") as source:
            watermark = source.convert("RGBA")
            watermark.thumbnail((570, 680), Image.Resampling.LANCZOS)
            watermark.putalpha(
                watermark.getchannel("A").point(lambda a: round(a * 0.055))
            )
            canvas.alpha_composite(
                watermark,
                (
                    WIDTH - watermark.width + 66,
                    max(450, height - watermark.height - 80),
                ),
            )
        return canvas

    def _header(self, canvas, page):
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((56, 42, 98, 84), radius=14, fill="#EEDDE9")
        draw.rounded_rectangle((67, 53, 86, 72), radius=4, outline=PINK, width=2)
        draw.line((72, 59, 81, 59), fill=PINK, width=2)
        draw.line((72, 65, 78, 65), fill=PINK, width=2)
        brand = _elide(self.bot_name, 25, 480)
        _text(draw, (111, 49), brand, 25, bold=True)
        _text(draw, (134 + _length(brand, 25), 55), "/  HELP MENU", 16, MUTED)
        _text(draw, (64, 139), "你好，欢迎来到", 23, MUTED)
        title_size = 49
        while title_size > 30 and _length(self.display_title, title_size) > 615:
            title_size -= 1
        _text(
            draw,
            (60, 185),
            _elide(self.display_title, title_size, 615),
            title_size,
            bold=True,
        )
        draw.rounded_rectangle((64, 254, 110, 259), radius=3, fill="#D98EB0")
        subtitle = (
            self.subtitle
            or f"让{self.bot_name}帮你找到需要的{'功能' if page.has_features else '指令'}。"
        )
        for i, line in enumerate(_wrap(subtitle, 23, 570)[:2]):
            _text(draw, (64, 280 + i * 32), line, 23, MUTED)
        x = 64
        for label, fill, ink in [
            (f"{page.plugin_count} 个插件", "#F6DFEA", PINK),
            (
                f"{page.total_entries} {'个入口' if page.has_features else '条指令'}",
                "#E1EEFB",
                BLUE,
            ),
            (f"第 {page.number} / {page.total_pages} 页", "#FFFFFF", MUTED),
        ]:
            x += _pill(draw, x, 361, label, fill=fill, ink=ink, height=38) + 10
        with Image.open(ASSETS / "whale-girl.png") as source:
            mascot = source.convert("RGBA")
            mascot.thumbnail((332, 376), Image.Resampling.LANCZOS)
            canvas.alpha_composite(mascot, (714, 92))
        draw = ImageDraw.Draw(canvas)
        _sparkle(draw, 684, 191, 12, "#D8A7C0")
        _sparkle(draw, 997, 131, 8, "#ABC9E8")
        _text(
            draw,
            (58, 455),
            ("发现功能" if page.has_features else "发现指令")
            if not page.keyword
            else "搜索结果",
            28,
            bold=True,
        )
        hint = f"“{page.keyword}”" if page.keyword else "按插件分组，找到你想用的功能"
        hint = _elide(hint, 19, 675)
        _text(draw, (208, 463), hint, 19, MUTED)
        _text(draw, (929, 462), f"{len(page.entries):02d} ITEMS", 16, MUTED)

    def _card(self, draw, card, index):
        x, y, height = card["x"], card["y"], card["height"]
        draw.rounded_rectangle(
            (x, y, x + CARD_WIDTH, y + height),
            radius=28,
            fill="#FFFFFF",
            outline="#F3EAF0",
            width=1,
        )
        blue = index % 3 == 1
        fill, ink = ("#E8F2FC", BLUE) if blue else ("#FBE9F1", PINK)
        draw.rounded_rectangle((x + 24, y + 24, x + 65, y + 65), radius=14, fill=fill)
        # A quiet four-square icon, consistent with Material's rounded geometry.
        for ix in (0, 1):
            for iy in (0, 1):
                left, top = x + 34 + ix * 11, y + 34 + iy * 11
                draw.rounded_rectangle(
                    (left, top, left + 7, top + 7), radius=2, fill=ink
                )
        label = card["label"] + (" · 续" if card["continued"] else "")
        _text(draw, (x + 79, y + 32), _elide(label, 25, 282), 25, bold=True)
        _pill(
            draw,
            x + CARD_WIDTH - 83,
            y + 31,
            str(len(card["rows"])) + " 条",
            fill=fill,
            ink=ink,
            size=16,
            height=30,
        )
        row_y = y + 96
        for i, row in enumerate(card["rows"]):
            cx = x + 26
            if i:
                draw.line(
                    (cx, row_y - 19, x + CARD_WIDTH - 26, row_y - 19),
                    fill="#F1EAF0",
                    width=1,
                )
            for line in row["command"]:
                _text(draw, (cx, row_y), line, 27, bold=True)
                row_y += 36
            for line in row["usage"]:
                _text(draw, (cx, row_y), line, 18, BLUE)
                row_y += 27
            for line in row["description"]:
                _text(draw, (cx, row_y + 1), line, 22, MUTED)
                row_y += 31
            for alias_line in row["alias"]:
                _text(draw, (cx, row_y + 3), alias_line, 18, MUTED)
                row_y += 29
            if row["conditional"]:
                _text(draw, (cx, row_y + 4), "条件限制 · 以插件提示为准", 17, PINK)
                row_y += 28
            row_y += 38

    def _empty(self, draw, page):
        draw.rounded_rectangle((56, 512, 1024, 768), radius=28, fill="#FFFFFF")
        _text(draw, (94, 557), "暂时没有找到指令", 32, bold=True)
        message = (
            "换个关键词试试，或发送菜单查看全部功能。"
            if page.keyword
            else "当前会话还没有可展示的插件指令。"
        )
        _text(draw, (94, 620), message, 24, MUTED)
        _text(draw, (94, 668), "菜单会在插件启用后自动更新。", 21, BLUE)

    def _footer(self, draw, page, y, prefix, trigger, extra_lines):
        draw.rounded_rectangle((56, y, 1024, y + 134), radius=26, fill="#FFFFFF")
        footer = _elide(f"和{self.bot_name}一起，探索更多功能", 22, 910)
        _text(draw, (80, y + 22), footer, 22, bold=True)
        base = prefix + trigger
        x = 80
        next_page = page.number + 1 if page.number < page.total_pages else 1
        for text, fill, ink in [
            (f"{base} {next_page}  翻页", "#FBE7F0", PINK),
            (f"{base} 关键词  搜索", "#E7F1FC", BLUE),
            (f"{base} 文字  复制", "#F1ECF6", MUTED),
        ]:
            x += (
                _pill(
                    draw,
                    x,
                    y + 71,
                    _elide(text, 19, 270),
                    fill=fill,
                    ink=ink,
                    size=19,
                    height=36,
                )
                + 12
            )
        _text(
            draw,
            (64, y + 157),
            "<参数> 必填    [参数] 可选    ·    额外条件以各插件实际提示为准",
            18,
            MUTED,
        )
        page_text = f"{page.number:02d} / {page.total_pages:02d}"
        _text(draw, (1024 - _length(page_text, 16), y + 184), page_text, 16, MUTED)
        for index, line in enumerate(extra_lines):
            _text(draw, (64, y + 218 + index * 29), line, 18, BLUE)
