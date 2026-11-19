"""Streamer presets and the retention pass for Stream VOD clips.

``preset_render``: a fixed facecam crop (from ``streamer_presets.json``) over
the gameplay, 1080x1920:

    0-220     black hook band, ALL-CAPS curiosity hook (covers nothing)
    220-860   facecam
    860-1920  gameplay
    seam 860  kinetic word captions, the active word popped and yellow

``retention_pass``: runs on any finished clip (preset or ``auto``): strips
silences longer than 0.25 s, then prepends the loudest 2.5 s of the clip as a
cold open, joined with a white flash and a short noise whoosh.

The pure helpers (``keep_segments``, ``peak_start``, ``kinetic_events``) are
unit-tested.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import List, Optional

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_W, OUT_H, BAND, SEAM = 1080, 1920, 220, 860
TEASER = 2.5
FADE = 0.25
SILENCE_DB, SILENCE_MIN, PAD = -35, 0.25, 0.08


def presets() -> dict:
    with open(os.path.join(HERE, "streamer_presets.json"), encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def active_preset() -> Optional[dict]:
    """The job's fixed-crop preset, or None (unset, unknown or 'auto')."""
    p = presets().get((os.environ.get("STREAMER_PRESET") or "").strip().lower())
    return p if p and p.get("cam") else None


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def keep_segments(silences: List[tuple], duration: float, pad: float = PAD) -> List[tuple]:
    """Complement of the silences, each silence shrunk to ``2*pad``."""
    keep, cur = [], 0.0
    for s, e in sorted(silences):
        cut_s, cut_e = s + pad, e - pad
        if cut_e - cut_s <= 0:
            continue
        if cut_s - cur > 0.05:
            keep.append((round(cur, 3), round(cut_s, 3)))
        cur = max(cur, cut_e)
    if duration - cur > 0.05:
        keep.append((round(cur, 3), round(duration, 3)))
    return keep or [(0.0, duration)]


def peak_start(rms: np.ndarray, step: float, length: float = TEASER) -> float:
    """Start (s) of the loudest ``length`` stretch, from per-``step`` RMS."""
    n = max(1, int(round(length / step)))
    if len(rms) <= n:
        return 0.0
    sums = np.convolve(rms, np.ones(n), mode="valid")
    return float(np.argmax(sums) * step)


def _ass_text(s: str) -> str:
    return re.sub(r"[{}\\]", "", s).replace("\n", " ").strip()


def kinetic_events(words: List[dict], chunk: int = 3) -> List[tuple]:
    """[(start, end, ass_text)]: chunks of ``chunk`` words, one event per word
    with that word yellow and popping. ``words`` times are clip-relative."""
    events = []
    for i in range(0, len(words), chunk):
        group = words[i:i + chunk]
        for j, w in enumerate(group):
            end = group[j + 1]["start"] if j + 1 < len(group) else w["end"]
            parts = []
            for k, o in enumerate(group):
                t = _ass_text(o["word"]).upper()
                if k == j:
                    t = (r"{\c&H00E5FF&\fscx118\fscy118\t(0,110,\fscx100\fscy100)}"
                         + t + r"{\c&HFFFFFF&}")
                parts.append(t)
            events.append((w["start"], max(end, w["start"] + 0.05), " ".join(parts)))
    return events


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(hook: str, words: List[dict], duration: float, seam: int = SEAM) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Anton,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,0,0,5,40,40,0,1
Style: Cap,Montserrat ExtraBold,68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook.strip():
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Hook,,0,0,0,,"
                     f"{{\\pos({OUT_W // 2},{BAND // 2})}}{_ass_text(hook).upper()}")
    for s, e, text in kinetic_events(words):
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\pos({OUT_W // 2},{seam})}}{text}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #
def _probe(path: str) -> tuple:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "json", path],
                       capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    s = (d.get("streams") or [{}])[0]
    return int(s.get("width") or 1920), int(s.get("height") or 1080), \
        float((d.get("format") or {}).get("duration") or 0)


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def _encode_args() -> list:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]


def clip_words(transcript: dict, start: float, end: float) -> List[dict]:
    out = []
    for seg in (transcript or {}).get("segments", []):
        for w in seg.get("words", []) or []:
            try:
                ws, we = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if ws >= start and we <= end and str(w.get("word", "")).strip():
                out.append({"start": ws - start, "end": we - start, "word": str(w["word"])})
    return out


def preset_render(src: str, out: str, preset: dict, clip: dict, transcript: dict,
                  start: float, end: float) -> bool:
    W, H, dur = _probe(src)
    cam = preset.get("cam")
    # No facecam: gameplay fills everything under the hook band, captions in the lower third.
    seam = SEAM if cam else 1400
    game_h = OUT_H - SEAM if cam else OUT_H - BAND
    g = preset.get("game")
    if g:
        gx, gy, gw, gh = _even(g["x"] * W), _even(g["y"] * H), _even(g["w"] * W), _even(g["h"] * H)
    else:
        gh = H
        gw = min(W, _even(H * OUT_W / game_h))
        gx, gy = _even((W - gw) / 2), 0
    cam_h = SEAM - BAND
    work = tempfile.mkdtemp(prefix="preset-")
    with open(os.path.join(work, "subs.ass"), "w", encoding="utf-8") as f:
        f.write(build_ass(clip.get("viral_hook_text") or "", clip_words(transcript, start, end), dur, seam))
    game = (f"crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{game_h}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{game_h}")
    tail = f"pad={OUT_W}:{OUT_H}:0:{BAND}:black,setsar=1,ass=subs.ass:fontsdir=fonts[v]"
    if cam:
        cx, cy, cw, ch = (_even(cam["x"] * W), _even(cam["y"] * H),
                          _even(cam["w"] * W), _even(cam["h"] * H))
        graph = (f"[0:v]split=2[a][b];"
                 f"[a]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{cam_h}:force_original_aspect_ratio=increase,"
                 f"crop={OUT_W}:{cam_h}[cam];[b]{game}[game];[cam][game]vstack,{tail}")
    else:
        graph = f"[0:v]{game},{tail}"
    import shutil
    shutil.copytree(os.path.join(HERE, "fonts"), os.path.join(work, "fonts"), dirs_exist_ok=True)
    # cwd=work: relative paths keep Windows drive colons out of the filter graph.
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src),
                        "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                        *_encode_args(), os.path.abspath(out)],
                       cwd=work, capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        print(f"   ❌ Preset render failed: {r.stderr[-400:]}")
        return False
    return True


def _silences(path: str) -> List[tuple]:
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                        f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends))


def _rms(path: str, step: float = 0.1, rate: int = 8000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    n = int(rate * step)
    if len(a) < n:
        return np.zeros(1)
    a = a[: len(a) // n * n].reshape(-1, n)
    return np.sqrt((a ** 2).mean(axis=1))


def retention_pass(path: str) -> bool:
    """Rewrite ``path`` in place: dead pauses out, 2.5 s cold open in."""
    if not os.path.exists(path):
        return False
    _, _, dur = _probe(path)
    keep = keep_segments(_silences(path), dur)
    teaser = dur > TEASER * 3
    p = peak_start(_rms(path), 0.1) if teaser else 0.0
    n = len(keep)
    k = n + 1 if teaser else n
    graph = f"[0:v]fps=30,format=yuv420p,split={k}" + "".join(f"[v{i}]" for i in range(k)) + ";"
    graph += f"[0:a]aresample=48000,asplit={k}" + "".join(f"[a{i}]" for i in range(k)) + ";"
    for i, (s, e) in enumerate(keep):
        graph += (f"[v{i}]trim={s}:{e},setpts=PTS-STARTPTS[mv{i}];"
                  f"[a{i}]atrim={s}:{e},asetpts=PTS-STARTPTS[ma{i}];")
    graph += "".join(f"[mv{i}][ma{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[mv][ma];"
    if teaser:
        t = n
        graph += (f"[v{t}]trim={p}:{p + TEASER},setpts=PTS-STARTPTS[tv];"
                  f"[a{t}]atrim={p}:{p + TEASER},asetpts=PTS-STARTPTS[ta];"
                  "[tv]settb=1/30[tv2];[mv]fps=30,settb=1/30[mv2];"
                  f"[tv2][mv2]xfade=transition=fadewhite:duration={FADE}:offset={TEASER - FADE}[v];"
                  f"[ta][ma]acrossfade=d={FADE}[xa];"
                  "anoisesrc=d=0.6:c=pink:a=0.35:r=48000,highpass=f=500,lowpass=f=5000,"
                  "afade=t=in:d=0.4,afade=t=out:st=0.4:d=0.2,"
                  f"adelay={int((TEASER - 0.45) * 1000)}:all=1[wh];"
                  "[xa][wh]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        graph += "[mv]null[v];[ma]anull[a]"
    tmp = path + ".ret.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-filter_complex", graph,
                        "-map", "[v]", "-map", "[a]", *_encode_args(), tmp],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        print(f"   Retention pass skipped: {r.stderr[-300:]}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, path)
    cut = dur - sum(e - s for s, e in keep)
    print(f"   ⚡ Retention: -{max(0.0, cut):.1f}s dead air" + (f", cold open from {p:.1f}s" if teaser else ""))
    return True
