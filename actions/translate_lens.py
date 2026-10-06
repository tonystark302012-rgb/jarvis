"""
translate_lens — Google-Lens-style screen translator (Report I).

Capture a screen region → read its text (region_ocr: tesseract offline →
Gemini vision) → translate every line (translate's own engine chain:
Argos offline → Gemini, privacy-gated) → floating HUD overlay showing
original → translated pairs.

Pieces are REUSED, not rebuilt:
    region_ocr._capture / ._read_text   — the live-screen reader
    translate.translate                 — the engine ladder + privacy gate
    ui._LensOverlay (new, visual only)  — where the pairs appear

`repeat` re-captures like region_ocr does (live ticker translate), each
frame replacing the overlay.
"""
from __future__ import annotations

import time

_MAX_LINES = 8
_MAX_CHARS = 700


def _translate_line(text: str, params: dict) -> str | None:
    """One line through translate's engine chain. Returns None on the
    honest failure strings so one bad line doesn't fake a translation."""
    try:
        from actions import translate
        p = {"text": text,
             "source": params.get("source") or params.get("from") or "",
             "target": params.get("target") or params.get("to") or "",
             "auto": params.get("auto", "")}
        out = translate.translate(p)
    except Exception:
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    if out.startswith(("No translation engine", "Privacy mode is ON",
                       "What should I translate")):
        return None
    return out.strip()


def _read_lines(region: str) -> list[str]:
    from actions import region_ocr
    img = region_ocr._capture(region)
    if img is None:
        return []
    try:
        from actions.region_ocr import _read_text
        text = _read_text(img, mode="text")
    except Exception:
        return []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[:_MAX_LINES]


def translate_lens(parameters: dict | None = None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    region = str(params.get("region", "center") or "center")
    try:
        repeat = max(1, min(30, int(params.get("repeat") or 1)))
    except (TypeError, ValueError):
        repeat = 1
    pause = 2.0
    try:
        pause = max(0.5, min(10.0, float(params.get("pause") or 2.0)))
    except (TypeError, ValueError):
        pass

    src_pair = (str(params.get("source") or params.get("from") or ""),
                str(params.get("target") or params.get("to") or ""))
    results: list[str] = []

    for i in range(repeat):
        lines = _read_lines(region)
        if not lines:
            results.append(f"frame {i + 1}: nothing readable in {region!r} "
                           "region (no capture backend or no text).")
        else:
            pairs: list[tuple[str, str]] = []
            batch = "\n".join(lines)[:_MAX_CHARS]
            for ln in lines:
                if len("\n".join(p[0] for p in pairs)) > _MAX_CHARS:
                    break
                tr = _translate_line(ln, params)
                pairs.append((ln, tr if tr else ""))
            # batch sanity: if EVERY line failed, surface translate's own
            # honest reason once instead of N empty pairs
            if pairs and not any(p[1] for p in pairs):
                probe = _translate_line(batch.splitlines()[0], params)
                if probe is None:
                    from actions import translate as _t
                    reason = _t.translate(dict(params, text="hello"))
                    results.append(f"frame {i + 1}: translation unavailable "
                                   f"({reason})")
                    if player is not None:
                        try:
                            player.show_content(
                                "LENS", results[-1][:2000])
                        except Exception:
                            pass
                    return "\n".join(results)
            translated = sum(1 for _o, t in pairs if t)
            head = (f"frame {i + 1}: {len(pairs)} line(s), "
                    f"{translated} translated "
                    f"({src_pair[0] or 'auto'} → {src_pair[1] or 'default'})")
            results.append(head)
            if player is not None:
                try:
                    player.show_lens(pairs, src_pair[0], src_pair[1])
                except Exception:
                    pass
            for orig, tr in pairs:
                results.append(f"{orig}\n  → {tr or '(untranslated)'}")
        if i + 1 < repeat:
            time.sleep(pause)

    out = "\n".join(results)
    if player is not None:
        try:
            player.show_content("TRANSLATE LENS", out[:4000])
        except Exception:
            pass
    return out


TOOL = {
    "name": "translate_lens",
    "description": (
        "Screen translate lens (Google-Lens style): capture a screen "
        "`region` (presets: full/center/top/bottom/left/right/cursor or "
        "'x,y,w,h'), OCR it (region_ocr: tesseract offline → Gemini), "
        "translate each line (Argos offline → Gemini, privacy-gated) and "
        "show original → translation pairs in a floating HUD overlay. "
        "Params: region, from/to, repeat + pause for a live ticker. Use "
        "for 'translate what's on screen', 'iska translation dikhao', "
        "'read and translate this region'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "region": {"type": "STRING",
                       "description": "preset name or 'x,y,w,h'."},
            "from": {"type": "STRING", "description": "source lang code."},
            "to": {"type": "STRING", "description": "target lang code."},
            "repeat": {"type": "STRING",
                       "description": "frames for live mode (1-30)."},
            "pause": {"type": "STRING",
                      "description": "seconds between frames."},
        },
        "required": [],
    },
    "handler": translate_lens,
}
