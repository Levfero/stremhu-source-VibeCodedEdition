import re

_STYLE_TAG_REGEX = re.compile(r"\{.*?\}")
_DRAWING_TAG_REGEX = re.compile(r"\{\\p[1-9]")


def ass_to_vtt(content: str) -> str:
    """Konvertálja a SubStation Alpha (.ass / .ssa) szöveges feliratot WebVTT (.vtt) formátumra."""
    lines = ["WEBVTT\n"]
    in_events = False
    for line in content.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("[") and clean_line.endswith("]"):
            in_events = clean_line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if clean_line.startswith(("Dialogue:", "dialogue:")):
            parts = clean_line.split(",", 9)
            if len(parts) >= 10:
                text = parts[9]
                # Rajzolt alakzatok (vektor rajzok, pl. {\p1}m 0 0...{\p0}) kizárása
                if _DRAWING_TAG_REGEX.search(text):
                    continue

                start = parts[1].strip()
                end = parts[2].strip()

                # Stílus címkék törlése (pl. {\pos(100,200)}, {\c&H0000FF&})
                text = _STYLE_TAG_REGEX.sub("", text)
                # Sortörések és nem törő szóközök (\h) normalizálása
                text = (
                    text.replace(r"\h", " ")
                    .replace(r"\N", "\n")
                    .replace(r"\n", "\n")
                    .strip()
                )

                def format_ts(ts: str) -> str:
                    p = ts.split(":")
                    if len(p) < 3:
                        return ts
                    h = p[0].zfill(2)
                    m = p[1].zfill(2)
                    s = p[2]
                    if "." in s:
                        parts_s = s.split(".", 1)
                        sec = parts_s[0].zfill(2)
                        ms = parts_s[1].ljust(3, "0")[:3]
                        return f"{h}:{m}:{sec}.{ms}"
                    return f"{h}:{m}:{s.zfill(2)}.000"

                if text:
                    lines.append(f"{format_ts(start)} --> {format_ts(end)}\n{text}\n")
    return "\n".join(lines)
