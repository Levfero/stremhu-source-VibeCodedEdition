import re


def ass_to_vtt(content: str) -> str:
    """Konvertálja a SubStation Alpha (.ass / .ssa) szöveges feliratot WebVTT (.vtt) formátumra."""
    lines = ["WEBVTT\n"]
    in_events = False
    for line in content.splitlines():
        clean_line = line.strip()
        if clean_line.lower() == "[events]":
            in_events = True
            continue
        if not in_events:
            continue
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) >= 10:
                start = parts[1].strip()
                end = parts[2].strip()
                text = parts[9]
                # Stílus címkék törlése (pl. {\pos(100,200)}, {\c&H0000FF&})
                text = re.sub(r"\{.*?\}", "", text)
                # Sortörések normalizálása
                text = text.replace(r"\N", "\n").replace(r"\n", "\n").strip()

                def format_ts(ts: str) -> str:
                    p = ts.split(":")
                    if len(p) < 3:
                        return ts
                    h = p[0].zfill(2)
                    m = p[1].zfill(2)
                    s = p[2]
                    if "." in s:
                        sec, ms = s.split(".")
                        return f"{h}:{m}:{sec.zfill(2)}.{ms.ljust(3, '0')[:3]}"
                    return f"{h}:{m}:{s.zfill(2)}.000"

                if text:
                    lines.append(f"{format_ts(start)} --> {format_ts(end)}\n{text}\n")
    return "\n".join(lines)
