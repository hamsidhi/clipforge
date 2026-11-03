"""Stage 7 — animated karaoke captions + hook title as an .ass subtitle file.

One ASS event per "active word": the whole line (2-4 words) stays on screen while the spoken word
is recoloured and does a small pop (\\t + \\fscx/\\fscy). One keyword per line gets a second colour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clipforge.config import CaptionsConfig
from clipforge.models import CaptionStyle, ClipCandidate, Word
from clipforge.utils.assets import DEVANAGARI_FONT, FONTS
from clipforge.utils.text import is_devanagari, keyword_score
from clipforge.utils.timecode import ass_ts, words_in_range

# Presets shown in the web app. Values override config.captions.
PRESETS: dict[str, dict] = {
    "bold-yellow-pop": {"label": "Bold Yellow Pop", "font": "Montserrat-ExtraBold", "font_size": 82,
                        "text_color": "#FFFFFF", "highlight_color": "#FFE600", "keyword_color": "#00FF88",
                        "uppercase": True, "pop": 1.18, "outline": 6, "shadow": 3},
    "clean-white": {"label": "Clean White", "font": "Poppins-Bold", "font_size": 74,
                    "text_color": "#FFFFFF", "highlight_color": "#FFFFFF", "keyword_color": "#FFFFFF",
                    "uppercase": False, "pop": 1.08, "outline": 4, "shadow": 2, "dim_inactive": True},
    "green-highlight": {"label": "Green Highlight", "font": "Montserrat-ExtraBold", "font_size": 80,
                        "text_color": "#FFFFFF", "highlight_color": "#00FF66", "keyword_color": "#FFE600",
                        "uppercase": True, "pop": 1.15, "outline": 6, "shadow": 3},
    "hindi-devanagari": {"label": "Hindi Devanagari", "font": "NotoSansDevanagari-Bold", "font_size": 80,
                         "text_color": "#FFFFFF", "highlight_color": "#FFE600", "keyword_color": "#00FF88",
                         "uppercase": False, "pop": 1.15, "outline": 6, "shadow": 3, "script": "devanagari"},
}

SAFE_RIGHT = 140   # px kept clear on the right (Reels/Shorts action buttons)
SAFE_LEFT = 50


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS is BGR with inverted alpha)."""
    h = hex_color.lstrip("#")
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


@dataclass
class Style:
    font_key: str
    family: str
    bold: bool
    size: int
    text: str
    highlight: str
    keyword: str
    outline: int
    shadow: int
    uppercase: bool
    pop: float
    dim_inactive: bool
    position_pct: float
    script: str


def resolve_style(cfg: CaptionsConfig, override: CaptionStyle | None = None) -> Style:
    override = override or CaptionStyle()
    preset = PRESETS.get(override.preset or cfg.preset, {})
    font_key = override.font or preset.get("font") or cfg.font
    if font_key not in FONTS:
        font_key = "Montserrat-ExtraBold"
    script = preset.get("script", cfg.script)
    if script == "devanagari" and FONTS[font_key].script != "devanagari":
        font_key = DEVANAGARI_FONT
    spec = FONTS[font_key]
    return Style(
        font_key=font_key, family=spec.family, bold=spec.bold,
        size=override.font_size or preset.get("font_size") or cfg.font_size,
        text=preset.get("text_color", cfg.text_color),
        highlight=override.highlight_color or preset.get("highlight_color") or cfg.highlight_color,
        keyword=override.keyword_color or preset.get("keyword_color") or cfg.keyword_color,
        outline=preset.get("outline", cfg.outline), shadow=preset.get("shadow", cfg.shadow),
        uppercase=preset.get("uppercase", True), pop=preset.get("pop", 1.15),
        dim_inactive=preset.get("dim_inactive", False),
        position_pct=override.position_pct or cfg.position_pct, script=script,
    )


@dataclass
class CapWord:
    start: float
    end: float
    text: str


def clip_words(words: list[Word], clip: ClipCandidate, script: str) -> list[CapWord]:
    """Words inside the clip, re-based to clip time, with the user's caption edits applied."""
    inside = words_in_range(words, clip.start, clip.end)
    out: list[CapWord] = []
    for i, w in enumerate(inside):
        text = clip.word_edits.get(str(i), w.display(script))
        if not text.strip():
            continue  # an edit to "" deletes the word
        out.append(CapWord(round(max(0.0, w.start - clip.start), 3), round(min(clip.duration, w.end - clip.start), 3), text.strip()))
    return out


def chunk_words(words: list[CapWord], per_line: int, max_chars: int, gap: float = 0.6) -> list[list[CapWord]]:
    """Group words into short caption lines (≤ per_line words, ≤ max_chars), breaking at pauses/sentences."""
    lines: list[list[CapWord]] = []
    cur: list[CapWord] = []
    for w in words:
        if cur:
            length = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            pause = w.start - cur[-1].end
            ended = cur[-1].text.rstrip().endswith((".", "?", "!", "।"))
            if len(cur) >= per_line or length > max_chars or pause >= gap or ended:
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _word_text(w: CapWord, st: Style) -> str:
    t = _escape(w.text)
    if st.uppercase and not is_devanagari(t):
        t = t.upper()
    if is_devanagari(t) and FONTS[st.font_key].script != "devanagari":
        t = f"{{\\fn{FONTS[DEVANAGARI_FONT].family}\\b1}}{t}{{\\fn{st.family}\\b{1 if st.bold else 0}}}"
    return t


def _est_width(text: str, size: int, uppercase: bool) -> float:
    return len(text) * size * (0.68 if uppercase else 0.58)


def build_ass(words: list[CapWord], clip: ClipCandidate, cfg: CaptionsConfig, width: int = 1080, height: int = 1920) -> str:
    st = resolve_style(cfg, clip.caption_style)
    base = ass_color(st.text)
    dim = ass_color(st.text, 0x50) if st.dim_inactive else base
    hl = ass_color(st.highlight)
    kw = ass_color(st.keyword)
    cx = (SAFE_LEFT + width - SAFE_RIGHT) // 2
    cy = int(height * st.position_pct)
    max_w = width - SAFE_LEFT - SAFE_RIGHT - 40
    pop = int(round(st.pop * 100))
    settle = int(round((1 + (st.pop - 1) * 0.5) * 100))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st.family},{st.size},{base},{hl},&H00000000,&H80000000,{-1 if st.bold else 0},0,0,0,100,100,0,0,1,{st.outline},{st.shadow},5,{SAFE_LEFT},{SAFE_RIGHT},0,1
Style: Hook,{FONTS['Montserrat-ExtraBold'].family},62,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,5,60,60,0,1
Style: HookBox,{st.family},10,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    lines = chunk_words(words, cfg.words_per_line, cfg.max_chars_per_line)
    for li, line in enumerate(lines):
        line_start = line[0].start
        nxt = lines[li + 1][0].start if li + 1 < len(lines) else clip.duration
        line_end = max(line_start + 0.15, min(line[-1].end + 0.35, nxt))
        texts = [_word_text(w, st) for w in line]
        plain = " ".join(w.text for w in line)
        fs = st.size
        est = _est_width(plain, fs, st.uppercase)
        if est > max_w:
            fs = max(40, int(fs * max_w / est))
        scores = [keyword_score(w.text) for w in line]
        key_idx = max(range(len(line)), key=lambda i: scores[i]) if max(scores) > 0 else -1
        for i, w in enumerate(line):
            a = line_start if i == 0 else w.start
            b = line_end if i == len(line) - 1 else line[i + 1].start
            if b - a < 0.02:
                continue
            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(f"{{\\1c{hl}\\fscx100\\fscy100\\t(0,80,\\fscx{pop}\\fscy{pop})\\t(80,160,\\fscx{settle}\\fscy{settle})}}{t}"
                                 f"{{\\1c{dim}\\fscx100\\fscy100}}")
                elif j == key_idx:
                    parts.append(f"{{\\1c{kw}}}{t}{{\\1c{dim}}}")
                else:
                    parts.append(t)
            text = f"{{\\an5\\pos({cx},{cy})\\fs{fs}\\1c{dim}}}" + " ".join(parts)
            events.append(f"Dialogue: 2,{ass_ts(a)},{ass_ts(b)},Caption,,0,0,0,,{text}")

    hook = (clip.hook or clip.title).strip()
    if cfg.hook_title and hook:
        events += _hook_events(hook, min(cfg.hook_seconds, clip.duration), width)
    return header + "\n".join(events) + "\n"


def _wrap(text: str, max_chars: int, max_lines: int = 3) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1 :])]
    return lines


def _rounded_rect(w: int, h: int, r: int) -> str:
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _hook_events(hook: str, seconds: float, width: int) -> list[str]:
    size = 62
    lines = _wrap(hook, 22)
    longest = max(len(line) for line in lines)
    pad_x, pad_y = 38, 26
    box_w = min(width - 80, int(longest * size * 0.66) + 2 * pad_x)
    box_h = int(len(lines) * size * 1.22) + 2 * pad_y
    x0 = (width - box_w) // 2
    y0 = 250
    end = ass_ts(seconds)
    fade = "\\fad(150,350)"
    box = f"Dialogue: 0,{ass_ts(0)},{end},HookBox,,0,0,0,,{{\\an7\\pos({x0},{y0}){fade}\\p1}}{_rounded_rect(box_w, box_h, 28)}{{\\p0}}"
    fs = size if longest * size * 0.66 + 2 * pad_x <= width - 80 else int((width - 80 - 2 * pad_x) / (longest * 0.66))
    text = "\\N".join(_escape(line) for line in lines)
    txt = f"Dialogue: 1,{ass_ts(0)},{end},Hook,,0,0,0,,{{\\an5\\pos({width // 2},{y0 + box_h // 2}){fade}\\fs{fs}}}{text}"
    return [box, txt]
