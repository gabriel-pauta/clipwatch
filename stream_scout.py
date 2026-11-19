"""Stream VOD mode: find the hype in a 4-10 h VOD without downloading the video.

1. Pull only the chat replay (TwitchDownloaderCLI for Twitch, yt-dlp
   ``live_chat`` for YouTube; Kick has none, audio carries it) and a
   low-bitrate audio track.
2. Score every second: chat velocity (hype words weigh more) + loudness +
   pattern interrupts (a sudden jump over the previous 15 s). A sliding 15 s
   window ranks them; the top 12 non-overlapping peaks become candidate
   windows that start 20 s before the peak, so the setup is in.
3. faster-whisper transcribes those 12 windows only.
4. The job's LLM (claude CLI, Gemini key or any OpenAI-compatible key) picks the
   best 5 standalone 30-55 s clips and writes their titles and hooks.
5. yt-dlp downloads only those slices (<=1080p) and they are joined into one
   short source. ``main.py`` then transcribes and renders that file as usual,
   with the picks as its clip list (no second selection pass).

Pure helpers (``per_second_*``, ``hype_scores``, ``top_windows``,
``clamp_pick``) are numpy/stdlib only and unit-tested.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from typing import List, Optional
from urllib.parse import urlparse

import numpy as np
from pydantic import BaseModel

WINDOW = 15            # sliding window (s)
PRE_ROLL = 20          # seconds kept before a peak: the setup before the reaction
CANDIDATE_LEN = 60     # each candidate window is PRE_ROLL before + 40 s after the peak
N_CANDIDATES = 12
N_CLIPS = 5
CLIP_MIN, CLIP_MAX = 30.0, 55.0
AUDIO_RATE = 4000      # loudness only needs an envelope; keeps a 10 h decode small

HYPE = re.compile(
    r"^(w+|l+|clip ?it|clip|jaja+|ja ja|haha+|lol+|lmao+|kekw|omegalul|lul+|pog\w*|"
    r"xd+|ez|gg+|no way|nooo+|what+|wtf|💀|😂|🤣|🔥)$", re.I)
ALLOWED_HOSTS = ("twitch.tv", "youtube.com", "youtu.be", "kick.com")


def platform(url: str) -> Optional[str]:
    host = (urlparse(url).hostname or "").lower()
    for h in ALLOWED_HOSTS:
        if host == h or host.endswith("." + h):
            return {"youtu.be": "youtube", "youtube.com": "youtube"}.get(h, h.split(".")[0])
    return None


def is_vod_url(url: str) -> bool:
    return platform(url or "") is not None


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def per_second_chat(messages: List[tuple], total: int) -> np.ndarray:
    """Chat weight per second; a hype message (W, CLIP IT, JAJAJA...) counts 3."""
    c = np.zeros(max(total, 1), dtype=float)
    for t, text in messages:
        i = int(t)
        if 0 <= i < len(c):
            c[i] += 3.0 if HYPE.match((text or "").strip()) else 1.0
    return c


def per_second_rms_db(samples: np.ndarray, rate: int) -> np.ndarray:
    n = len(samples) // rate
    if n == 0:
        return np.zeros(1)
    blocks = samples[: n * rate].astype(np.float32).reshape(n, rate)
    rms = np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768.0)


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def hype_scores(chat: np.ndarray, loud_db: np.ndarray, window: int = WINDOW) -> np.ndarray:
    """Score for a window starting at each second.

    Chat is shifted ~8 s earlier (viewers type after the moment). Interrupt =
    loudness of this window minus the window before it: a scream after quiet
    play scores, a constantly loud segment does not."""
    n = max(len(chat), len(loud_db))
    chat = np.pad(chat, (0, n - len(chat)))
    loud = np.pad(loud_db, (0, n - len(loud_db)), constant_values=loud_db.min() if len(loud_db) else -90)
    chat = np.roll(chat, -8)
    chat[-8:] = 0
    k = np.ones(window) / window
    # Forward window [t, t+window); the tail is padded with its own edge value
    # (zero-padding dB would read as the loudest moment of the VOD).
    chat_w = np.convolve(np.pad(chat, (0, window - 1)), k, mode="valid")
    loud_w = np.convolve(np.pad(loud, (0, window - 1), mode="edge"), k, mode="valid")
    prev = np.concatenate([np.full(window, loud_w[0]), loud_w[:-window]])[:n]
    interrupt = np.clip(loud_w - prev, 0, None)
    has_chat = chat.sum() > 0
    return (1.5 * _z(chat_w) if has_chat else 0) + _z(loud_w) + 0.8 * _z(interrupt)


def top_windows(scores: np.ndarray, n: int = N_CANDIDATES, total: Optional[int] = None) -> List[dict]:
    """Top ``n`` non-overlapping candidate windows [peak-20, peak+40]."""
    total = total or len(scores)
    order = np.argsort(scores)[::-1]
    taken: List[dict] = []
    for peak in order:
        start = max(0, int(peak) - PRE_ROLL)
        end = min(total, start + CANDIDATE_LEN)
        if any(start < w["end"] and end > w["start"] for w in taken):
            continue
        taken.append({"id": f"w{len(taken) + 1}", "start": float(start), "end": float(end),
                      "peak": float(peak), "score": float(scores[peak])})
        if len(taken) >= n:
            break
    return sorted(taken, key=lambda w: w["start"])


def clamp_pick(pick: dict, window: dict, total: float) -> Optional[dict]:
    """Keep a model pick inside its window and inside 30-55 s."""
    s = max(float(pick["start"]), window["start"] - 10)
    e = min(float(pick["end"]), window["end"] + 15, total)
    if e - s > CLIP_MAX:
        e = s + CLIP_MAX
    if e - s < CLIP_MIN:
        e = min(total, s + CLIP_MIN)
        s = max(0.0, e - CLIP_MIN)
    if e - s < 5:
        return None
    return {**pick, "start": round(s, 2), "end": round(e, 2)}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _js_runtime() -> list:
    """yt-dlp only auto-enables deno; without a JS runtime YouTube downloads
    stall on the n-challenge, so hand it node when deno is missing."""
    import shutil
    if shutil.which("deno") or not shutil.which("node"):
        return []
    return ["--js-runtimes", "node"]


def _ytdlp(*args, cwd=None):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", *_js_runtime(), *args]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _twitch_vod_id(url: str) -> Optional[str]:
    m = re.search(r"/videos?/(\d+)", url)
    return m.group(1) if m else None


def resolve_vod(url: str) -> str:
    """A Twitch channel link becomes its newest archive VOD, which Twitch keeps
    growing while the stream is live, so a mid-stream run covers everything so far."""
    if platform(url) != "twitch" or _twitch_vod_id(url):
        return url
    name = (urlparse(url).path.strip("/").split("/") or [""])[0]
    if not re.fullmatch(r"\w{2,25}", name or ""):
        return url
    r = _ytdlp("--flat-playlist", "--playlist-end", "1", "--print", "%(id)s",
               f"https://www.twitch.tv/{name}/videos?filter=archives&sort=time")
    vid = (r.stdout.strip().splitlines() or [""])[0].lstrip("v")
    if not vid.isdigit():
        raise RuntimeError(f"No archived VOD found for twitch.tv/{name} (archives off?).")
    print(f"   ↪ Channel link: using its newest archive VOD twitch.tv/videos/{vid}")
    return f"https://www.twitch.tv/videos/{vid}"


def fetch_chat(url: str, workdir: str, begin: Optional[float] = None,
               end: Optional[float] = None) -> List[tuple]:
    """[(seconds, text)] from the chat replay, or [] when there is none.

    Twitch takes a [begin, end] range (TwitchDownloaderCLI -b/-e), so a live
    archive is read one chunk at a time. YouTube's replay only exists once the
    broadcast is over; it is fetched whole once and cached."""
    plat = platform(url)
    try:
        if plat == "twitch":
            vod = _twitch_vod_id(url)
            exe = os.environ.get("TWITCH_DOWNLOADER_CLI") or "TwitchDownloaderCLI"
            tag = f"_{int(begin)}_{int(end)}" if begin is not None else ""
            out = os.path.join(workdir, f"chat{tag}.json")
            if vod and not os.path.exists(out):
                cmd = [exe, "chatdownload", "--id", vod, "-o", out, "--banner", "false"]
                if begin is not None:
                    cmd += ["-b", f"{int(begin)}s", "-e", f"{int(end)}s"]
                subprocess.run(cmd, capture_output=True, timeout=1800,
                               env={**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"})
            if os.path.exists(out):
                with open(out, encoding="utf-8") as f:
                    data = json.load(f)
                return [(float(c.get("content_offset_seconds") or 0),
                         (c.get("message") or {}).get("body") or "")
                        for c in data.get("comments", [])]
        elif plat == "youtube":
            if not glob.glob(os.path.join(workdir, "chat*.live_chat.json")):
                _ytdlp("--skip-download", "--write-subs", "--sub-langs", "live_chat",
                       "-o", os.path.join(workdir, "chat"), url)
            msgs = []
            for path in glob.glob(os.path.join(workdir, "chat*.live_chat.json")):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            ev = json.loads(line)["replayChatItemAction"]
                            t = int(ev.get("videoOffsetTimeMsec") or 0) / 1000
                            for a in ev.get("actions", []):
                                r = (a.get("addChatItemAction") or {}).get("item", {}) \
                                    .get("liveChatTextMessageRenderer")
                                if r:
                                    text = "".join(x.get("text", "") for x in r["message"]["runs"])
                                    msgs.append((t, text))
                        except (KeyError, ValueError, TypeError):
                            continue
            if begin is not None:
                msgs = [m for m in msgs if begin <= m[0] < end]
            return msgs
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"   ℹ️ Chat replay unavailable ({type(e).__name__}); scoring on audio only.")
    return []


def loudness(audio_path: str) -> np.ndarray:
    """Per-second dB, streamed so a 10 h track never sits in memory at once."""
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", audio_path, "-ac", "1",
                             "-ar", str(AUDIO_RATE), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out = []
    chunk = AUDIO_RATE * 2 * 600  # ten minutes of s16
    while True:
        buf = proc.stdout.read(chunk)
        if not buf:
            break
        out.append(per_second_rms_db(np.frombuffer(buf, dtype=np.int16), AUDIO_RATE))
    proc.wait()
    return np.concatenate(out) if out else np.zeros(1)


_MODEL = None


def whisper():
    """One faster-whisper model per process (a live watch transcribes for hours)."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        try:
            import ctranslate2
            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        # base, all cores: watches take turns (asr_turn), so one transcription owns the CPU.
        _MODEL = WhisperModel(os.environ.get("SCOUT_WHISPER_MODEL", "base"),
                              device="cuda" if gpu else "cpu",
                              compute_type="float16" if gpu else "int8",
                              cpu_threads=os.cpu_count() or 4)
        print(f"   🎙️ Whisper on {'CUDA' if gpu else 'CPU'}")
    return _MODEL


class asr_turn:
    """Cross-process lock: one watch transcribes at a time. Seven Whisper models
    sharing 8 cores at once thrashed (CPU 100%, a chunk took 15+ min instead of ~3)."""

    def __enter__(self):
        import fcntl
        import tempfile
        self.f = open(os.path.join(tempfile.gettempdir(), "stream-clipper-asr.lock"), "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _pcm(path: str, start: float = 0.0, length: Optional[float] = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", str(max(0.0, start))]
    if length:
        cmd += ["-t", str(length)]
    cmd += ["-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32)


class _Line(BaseModel):
    t: float
    text: str


class _Win(BaseModel):
    id: str
    lines: List[_Line]


class _Wins(BaseModel):
    windows: List[_Win]


def _gemini_windows(audio_path: str, windows: List[dict], offset: float) -> bool:
    """All windows in one free Gemini request: local Whisper on a CPU shared by seven
    watches ran slower than real time (30 s of audio took 45 s)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key or not windows:
        return False
    try:
        from google import genai
        from google.genai import types
        parts = []
        for w in windows:
            mp3 = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(max(0.0, w["start"] - offset)),
                                  "-t", str(w["end"] - w["start"]), "-i", audio_path, "-ac", "1",
                                  "-b:a", "48k", "-f", "mp3", "-"], capture_output=True).stdout
            parts += [f"WINDOW {w['id']}:", types.Part.from_bytes(data=mp3, mime_type="audio/mp3")]
        parts.append("Transcribe the speech in each WINDOW verbatim, in its original language. "
                     "For each line give t = seconds from the start of THAT window. Game dialogue "
                     "counts as speech too. A window with no speech gets an empty list.")
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60_000))
        cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=_Wins)
        for attempt in range(3):
            try:
                r = client.models.generate_content(
                    model=os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite",
                    contents=parts, config=cfg)
                break
            except Exception as e:
                if attempt == 2 or "503" not in str(e) and "429" not in str(e):
                    raise
                import time
                time.sleep(8 * (attempt + 1))
        got = {w.id: w for w in _Wins.model_validate_json(r.text).windows}
        for w in windows:
            lines = got[w["id"]].lines if w["id"] in got else []
            w["text"] = " ".join(f"[{_ts(w['start'] + l.t)}] {l.text.strip()}" for l in lines)
        return True
    except Exception as e:
        print(f"   ℹ️ Gemini transcription unavailable ({type(e).__name__}): local Whisper")
        return False


def transcribe_windows(audio_path: str, windows: List[dict], offset: float = 0.0) -> None:
    """Adds ``text`` (absolute [h:mm:ss] stamps) to each window, in place.
    ``offset``: where ``audio_path`` starts on the stream's timeline."""
    if _gemini_windows(audio_path, windows, offset):
        return
    model = whisper()
    for w in windows:
        pcm = _pcm(audio_path, w["start"] - offset, w["end"] - w["start"])
        with asr_turn():
            segs, _ = model.transcribe(pcm, vad_filter=False)
            segs = list(segs)  # the generator decodes lazily: finish inside the turn
        w["text"] = " ".join(f"[{_ts(w['start'] + s.start)}] {s.text.strip()}" for s in segs)


def transcribe_words(path: str) -> dict:
    """Word-timed transcript of a clip file, in main.transcribe_media's shape."""
    pcm = _pcm(path)
    with asr_turn():
        segs, info = whisper().transcribe(pcm, word_timestamps=True, vad_filter=False)
        segs = list(segs)
    out = [{"start": s.start, "end": s.end, "text": s.text,
            "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]}
           for s in segs]
    return {"language": info.language, "segments": out}


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------- #
# Picking
# --------------------------------------------------------------------------- #
class ScoutPick(BaseModel):
    window_id: str
    start: float
    end: float
    predicted_score: int
    video_title_for_youtube_short: str
    viral_hook_text: str
    video_description_for_tiktok: str
    video_description_for_instagram: str
    why: str = ""


class ScoutResponse(BaseModel):
    shorts: List[ScoutPick]


# Volume, autoshorts-style: every viable clip, not a fixed top N.
PROMPT = """You cut YouTube Shorts from a livestream. Below are {n} candidate windows where chat
and audio spiked. Times are absolute seconds in the stream; transcript lines carry [h:mm:ss]
stamps. Language of the stream: {lang}.

Return EVERY window that holds a postable STANDALONE clip (up to {k}). Be generous: the goal is
volume, so do not stop at 2 if there are 5 good ones. Skip only filler, dead air, or moments
that need earlier context to make sense. Returning none is fine when nothing hits.

Each clip: {lo:.0f}-{hi:.0f} seconds, at most one per window. It must survive without the stream
around it: open on a complete thought, contain a turn (setup -> payoff/reaction), and end ON
the payoff line, not a trail-off. Cut dead-air openers ("uh", "wait hold on", "ok so").
Never pick stream setup: mic/audio checks, "can you hear me", settings and menus, "is chat
good", schedule talk. Game audio (cutscene dialogue) is not the streamer: the hook and title
must describe what the STREAMER says or does in the clip, never a scene they did not react to.
predicted_score 0-100 for how hard it hits.
viral_hook_text: max 8 words, ALL CAPS, curiosity gap, in the stream's language, no spoilers.
video_title_for_youtube_short: max 90 chars, in the stream's language.
Descriptions: one line each with 3 hashtags.

{windows}"""


def pick_clips(windows: List[dict], chat: List[tuple], lang: str, total: float,
               llm, k: int = N_CLIPS) -> List[dict]:
    """``llm(prompt, schema) -> dict``: whichever provider the job runs on."""
    if not windows:
        return []
    blocks = []
    for w in windows:
        top = [t for s, t in chat if w["start"] <= s <= w["end"] + 10][:25]
        blocks.append(f"### {w['id']} {w['start']:.0f}-{w['end']:.0f}s (hype {w['score']:.2f})\n"
                      f"TRANSCRIPT: {w.get('text') or '(no speech)'}\n"
                      f"CHAT: {' | '.join(top) or '(none)'}")
    prompt = PROMPT.format(n=len(windows), k=k, lo=CLIP_MIN, hi=CLIP_MAX, lang=lang,
                           windows="\n\n".join(blocks))
    data = llm(prompt, ScoutResponse)
    by_id = {w["id"]: w for w in windows}
    picks = []
    for p in sorted(data["shorts"], key=lambda p: -p["predicted_score"]):
        w = by_id.get(p["window_id"])
        c = clamp_pick(p, w, total) if w else None
        if c and not any(c["start"] < q["end"] and c["end"] > q["start"] for q in picks):
            picks.append(c)
    return picks[:k]


SLICE_FORMAT = ("bv*[height<=1080][protocol^=m3u8][vcodec^=avc1]+ba[protocol^=m3u8][format_note*=original]"
                "/bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]"
                "/b[height<=1080][protocol^=m3u8]/bv*[height<=1080]+ba/b")
