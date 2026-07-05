"""Stage 4 — Instagram image (template path).

Renders a 1080x1080 news card: a short headline over a gradient background,
with a badge and accent bar. Chosen over generative art because for news
topics a legible headline beats a pretty-but-vague AI image.

Two steps, kept separate so render() needs no network:
  design_spec()  Claude turns the IG draft into a small design brief.
  render()       pure Pillow — draws the card from that brief.
generate()       does both and writes the PNG.
"""

from __future__ import annotations

import base64
import json
import os

from PIL import Image, ImageDraw, ImageFont

from . import llm

SIZE = 1080
MARGIN = 90

# template = Pillow gradient card (free, default). openai = generative poster
# (dramatic composite style, costs ~$0.04-0.17/image).
_BACKEND = os.environ.get("MEDIA_ENGINE_IMAGE_BACKEND", "template").strip().lower()
_IMAGE_QUALITY = os.environ.get("MEDIA_ENGINE_IMAGE_QUALITY", "medium").strip().lower()


def backend() -> str:
    return _BACKEND

_SPEC_SYSTEM = (
    "You design a single square Instagram news card. Given the post draft, "
    "return a compact design brief. headline: <= 10 words, punchy, the one "
    "thing to read. subhead: <= 14 words of supporting context. badge: 1-2 "
    "uppercase words for a corner tag (e.g. BREAKING, EXPLAINER, TIMELINE). "
    "Colors are hex like #1A2B3C: pick a background gradient (top/bottom) and "
    "an accent that fit the topic's mood; ensure text will be readable on it. "
    "Respect any sensitivity in the topic — restrained palette, no lurid red "
    "for tragedy."
)

_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "subhead": {"type": "string"},
        "badge": {"type": "string"},
        "bg_top_hex": {"type": "string"},
        "bg_bottom_hex": {"type": "string"},
        "accent_hex": {"type": "string"},
    },
    "required": [
        "headline",
        "subhead",
        "badge",
        "bg_top_hex",
        "bg_bottom_hex",
        "accent_hex",
    ],
    "additionalProperties": False,
}


def design_spec(ig_draft: dict, angle: dict) -> dict:
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nINSTAGRAM DRAFT:\n" + json.dumps(ig_draft, indent=2)
        + "\n\nDesign the card."
    )
    return llm.structured(_SPEC_SYSTEM, user, _SPEC_SCHEMA, max_tokens=1000)


# --- pure rendering (no network) ---------------------------------------------

def _hex(value: str) -> tuple[int, int, int]:
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (26, 26, 32)


def _font(size: int, bold: bool = False):
    names = (
        ["arialbd.ttf", "Arialbd.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]
    )
    dirs = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", ""]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(os.path.join(d, n) if d else n, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _gradient(top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), top)
    draw = ImageDraw.Draw(img)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (SIZE, y)], fill=row)
    return img


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text: str, max_w: int, max_h: int, start: int, bold: bool):
    """Largest font size whose wrapped block fits the box."""
    size = start
    while size > 24:
        font = _font(size, bold=bold)
        lines = _wrap(draw, text, font, max_w)
        line_h = round(size * 1.15)
        if len(lines) * line_h <= max_h:
            return font, lines, line_h
        size -= 4
    font = _font(24, bold=bold)
    return font, _wrap(draw, text, font, max_w), round(24 * 1.15)


def render(spec: dict, out_path: str) -> str:
    top, bottom = _hex(spec["bg_top_hex"]), _hex(spec["bg_bottom_hex"])
    accent = _hex(spec["accent_hex"])
    img = _gradient(top, bottom)
    draw = ImageDraw.Draw(img)

    mid = tuple((top[i] + bottom[i]) // 2 for i in range(3))
    luminance = 0.299 * mid[0] + 0.587 * mid[1] + 0.114 * mid[2]
    text_color = (245, 245, 245) if luminance < 140 else (17, 17, 17)

    inner = SIZE - 2 * MARGIN

    # badge pill (top-left)
    badge = spec["badge"].strip().upper()[:20]
    bfont = _font(30, bold=True)
    bw = draw.textlength(badge, font=bfont)
    pad = 22
    draw.rounded_rectangle(
        [MARGIN, MARGIN, MARGIN + bw + 2 * pad, MARGIN + 58], radius=29, fill=accent
    )
    draw.text((MARGIN + pad, MARGIN + 12), badge, font=bfont, fill=(255, 255, 255))

    # headline — big, auto-fitted, lower-middle of the card
    hfont, hlines, hlh = _fit(
        draw, spec["headline"], inner, max_h=430, start=110, bold=True
    )
    sfont, slines, slh = _fit(
        draw, spec["subhead"], inner, max_h=170, start=46, bold=False
    )
    block_h = len(hlines) * hlh + 30 + len(slines) * slh
    y = SIZE - MARGIN - 70 - block_h  # sit above the footer bar

    for line in hlines:
        draw.text((MARGIN, y), line, font=hfont, fill=text_color)
        y += hlh
    y += 30
    for line in slines:
        draw.text((MARGIN, y), line, font=sfont, fill=text_color)
        y += slh

    # accent footer bar
    draw.rectangle([MARGIN, SIZE - MARGIN - 30, MARGIN + 160, SIZE - MARGIN - 18], fill=accent)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# --- generative backend (OpenAI images) --------------------------------------

def _poster_prompt(spec: dict, angle: dict) -> str:
    return (
        "Dramatic, high-impact editorial exposé poster, square 1:1 composition. "
        f'Bold distressed condensed UPPERCASE headline reading exactly "{spec["headline"]}" '
        "in white and blood-red grunge lettering, very high contrast, cinematic. "
        f'A smaller supporting line: "{spec["subhead"]}". '
        "Dark moody layered background suited to the topic — for finance/markets use a "
        "crashing red stock chart, a faint city skyline or stock-exchange building, torn "
        "newspaper clippings and a red warning stamp; adapt the imagery to the subject "
        f"({angle.get('angle', '')}). Serious, attention-grabbing news tone. "
        "Spell all text exactly as given — no gibberish text, no watermark, no logos."
    )


def _openai_image(spec: dict, angle: dict, out_path: str) -> str:
    from openai import OpenAI

    client = OpenAI(max_retries=2, timeout=180.0)
    result = client.images.generate(
        model="gpt-image-1",
        prompt=_poster_prompt(spec, angle),
        size="1024x1024",
        quality=_IMAGE_QUALITY,
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(result.data[0].b64_json))
    return out_path


def generate(draft: dict, angle: dict, out_path: str, use_backend: str | None = None) -> dict:
    """Design the card spec, then render via the chosen backend.

    use_backend: None -> env default; "template" (free Pillow card) or "openai"
    (generative poster) overrides it. Returns {path, spec, backend}.
    """
    b = (use_backend or _BACKEND).strip().lower()
    spec = design_spec(draft, angle)
    if b == "openai":
        _openai_image(spec, angle, out_path)
    else:
        render(spec, out_path)
    return {"path": out_path, "spec": spec, "backend": b}
