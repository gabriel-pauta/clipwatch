"""Watch a stream, live or finished, and cut every postable clip from all of it.

Volume over a top-N: the stream is scanned in chunks (15 min by default) from
0:00. While it is live the watcher catches up on the hours already streamed,
then follows the live edge, polling until the broadcast ends. Each chunk:

    chat + audio of the chunk -> hype windows (stream_scout) -> whisper on
    those windows -> the LLM returns EVERY standalone clip in them ->
    1080p slice of each pick -> preset render + retention pass -> announced
    to the dashboard (CLIP_READY) the moment it is done.

Range access per platform:
  Twitch   live or not: the archive VOD grows while live; yt-dlp sections + chat -b/-e.
  YouTube  finished: yt-dlp sections + chat replay. Live: the HLS segments are
           numbered (sq, 5 s each) and old numbers stay fetchable, so any range of
           the broadcast is read straight from the CDN. No chat until it ends.
  Kick     finished VODs only (its live playlist keeps no history).

Stop early: create a file named STOP in the job's output folder.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from urllib.parse import urlparse

import httpx

import stream_render
import stream_scout as scout

CHUNK = int(os.environ.get("LIVE_CHUNK_SECONDS", "900"))
WINDOWS_PER_CHUNK = int(os.environ.get("LIVE_WINDOWS_PER_CHUNK", "6"))
CLIPS_PER_CHUNK = int(os.environ.get("LIVE_CLIPS_PER_CHUNK", "4"))
RENDER_WORKERS = int(os.environ.get("LIVE_RENDER_WORKERS", "1"))  # 7 watches already render side by side
POLL = int(os.environ.get("LIVE_POLL_SECONDS", "90"))
REQUIRE_CAM = os.environ.get("LIVE_REQUIRE_CAM", "1") == "1"   # people share clips with a face
LAG = 120            # stay this far behind the live edge: the newest segments are still settling
MAX_HOURS = float(os.environ.get("LIVE_MAX_HOURS", "24"))


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def next_chunk(done: float, head: float, live: bool, chunk: int = CHUNK) -> Optional[tuple]:
    """The next [start, end) to scan, or None to wait (live) / stop (finished)."""
    if head - done >= chunk:
        return done, done + chunk
    if not live and head - done > 30:
        return done, head
    return None


def overlaps(clip: dict, existing: List[dict]) -> bool:
    return any(clip["start"] < c["vod_end"] and clip["end"] > c["vod_start"] for c in existing)


def segment_range(a: float, b: float, seg: float) -> range:
    return range(int(a // seg), int(math.ceil(b / seg)) + 1)


def safe_title(text: str) -> str:
    # yt-dlp stamps live titles with the current time; a resumed watch must find its own files.
    text = re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:?\d{2}$", "", text or "")
    return re.sub(r"[^\w\- ]+", "", text or "stream")[:60].strip() or "stream"


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class Source:
    """Any yt-dlp URL that supports --download-sections (VODs, Twitch live archives)."""

    def __init__(self, url: str, work: str, meta: dict):
        self.url, self.work = url, work
        self.plat = scout.platform(url)
        self._apply(meta)

    def _apply(self, meta: dict):
        self.title = safe_title(meta.get("title"))
        self.lang = meta.get("language")
        self.live = meta.get("live_status") == "is_live"
        self.duration = float(meta.get("duration") or 0)

    def refresh(self):
        meta = _meta(self.url)
        if meta:
            self._apply(meta)

    def audio(self, a: float, b: float, out: str) -> str:
        scout._ytdlp("-f", "ba[format_note*=original]/ba/b", "--download-sections", f"*{a}-{b}",
                     "-o", out + ".%(ext)s", self.url)
        found = glob.glob(glob.escape(out) + ".*")
        if not found:
            raise RuntimeError(f"audio {a:.0f}-{b:.0f}s did not download")
        return found[0]

    def video(self, a: float, b: float, out: str) -> str:
        r = scout._ytdlp("-f", scout.SLICE_FORMAT, "--merge-output-format", "mp4",
                         "--download-sections", f"*{a}-{b}", "--force-keyframes-at-cuts",
                         "-o", out, self.url)
        if not os.path.exists(out):
            raise RuntimeError(f"slice {a:.0f}-{b:.0f}s failed: {r.stderr[-300:]}")
        return out

    def chat(self, a: float, b: float) -> List[tuple]:
        if self.plat == "youtube" and self.live:
            return []  # the replay only exists once the broadcast ends
        return scout.fetch_chat(self.url, self.work, a, b)


class SegmentSource(Source):
    """Reads a time range straight from the HLS segments, many at once.

    yt-dlp/ffmpeg cannot do this on a playlist that is still growing (no
    #EXT-X-ENDLIST): ffmpeg treats it as live and downloads in real time, so a
    15-minute chunk took 15 minutes and a watch never caught up."""

    def _urls(self, kind: str, a: float, b: float) -> tuple:
        raise NotImplementedError

    def _fetch(self, kind: str, a: float, b: float, out: str) -> float:
        """Write segments covering [a, b] to ``out`` (MPEG-TS); returns where it starts."""
        urls, start = self._urls(kind, a, b)
        if not urls:
            raise RuntimeError(f"no {kind} segments for {a:.0f}-{b:.0f}s")

        def get(url):
            for _ in range(3):
                try:
                    r = httpx.get(url, timeout=30)
                    if r.status_code == 200:
                        return r.content
                except httpx.HTTPError:
                    pass
            return b""
        with ThreadPoolExecutor(8) as pool:
            parts = list(pool.map(get, urls))
        with open(out, "wb") as f:
            for p in parts:
                f.write(p)
        return start

    def audio(self, a, b, out):
        ts = out + ".ts"
        start = self._fetch("audio", a, b, ts)
        path = out + ".m4a"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a - start), "-t", str(b - a),
                        "-i", ts, "-vn", "-c:a", "aac", path], check=True)
        os.remove(ts)
        return path

    def video(self, a, b, out):
        vts, ats = out + ".v.ts", out + ".a.ts"
        vs, as_ = self._fetch("video", a, b, vts), self._fetch("audio", a, b, ats)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a - vs), "-t", str(b - a), "-i", vts,
                        "-ss", str(a - as_), "-t", str(b - a), "-i", ats, "-map", "0:v:0", "-map", "1:a:0",
                        "-c", "copy", out],  # no re-encode: preset_render encodes once anyway
                       check=True)
        for p in (vts, ats):
            os.remove(p)
        return out


class YouTubeLive(SegmentSource):
    """A live YouTube broadcast read by HLS segment number (sq): every sq since
    the start stays fetchable, so hours 0..now are reachable mid-stream."""

    AUDIO = "234-1/233-1/234/233/ba[protocol^=m3u8]"
    VIDEO = "bv*[height<=1080][protocol^=m3u8][vcodec^=avc1]/bv*[height<=1080][protocol^=m3u8]"

    def _apply(self, meta: dict):
        super()._apply(meta)
        if not self.live:
            return  # ended: keep the last head and manifests we had
        self.manifests = {k: _manifest_url(self.url, f) for k, f in
                          (("audio", self.AUDIO), ("video", self.VIDEO))}
        head_sq, self.seg = _head(self.manifests["audio"])
        self.duration = (head_sq + 1) * self.seg

    def _urls(self, kind, a, b):
        text = httpx.get(self.manifests[kind], timeout=30).text
        template = [l for l in text.splitlines() if l.startswith("http")][-1]
        seqs = list(segment_range(a, b, self.seg))
        return [re.sub(r"/sq/\d+/", f"/sq/{n}/", template) for n in seqs], seqs[0] * self.seg


class TwitchArchive(SegmentSource):
    """A Twitch VOD (growing while live) read from its own playlist: every
    segment is listed with its duration, so a range maps to segments directly."""

    AUDIO = "ba/b"
    VIDEO = "b[height<=1080]/b"   # Twitch renditions carry their own audio

    def _apply(self, meta: dict):
        super()._apply(meta)
        self.manifests = {k: _manifest_url(self.url, f) for k, f in
                          (("audio", self.AUDIO), ("video", self.VIDEO))}
        self._scan()

    def _scan(self):
        text = httpx.get(self.manifests["audio"], timeout=30).text
        segs = parse_hls(text, self.manifests["audio"])
        if segs:
            self.duration = segs[-1][1]
        self.live = "#EXT-X-ENDLIST" not in text

    def refresh(self):
        self._scan()  # the playlist alone says how far the stream got and whether it ended

    def _urls(self, kind, a, b):
        text = httpx.get(self.manifests[kind], timeout=30).text
        segs = parse_hls(text, self.manifests[kind])
        picked = [s for s in segs if s[1] > a and s[0] < b]
        urls = [s[2] for s in picked]
        init = hls_init(text, self.manifests[kind])
        return ([init] + urls if init and urls else urls), (picked[0][0] if picked else a)

    def video(self, a, b, out):
        ts = out + ".ts"
        start = self._fetch("video", a, b, ts)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a - start), "-t", str(b - a), "-i", ts,
                        "-c", "copy", out],  # no re-encode: preset_render encodes once anyway
                       check=True)
        os.remove(ts)
        return out


class LiveRecorder(SegmentSource):
    """A live stream with no history to read back (Twitch channel whose current
    broadcast has no archive yet, Kick): record it from now into a rolling local
    buffer and clip as it goes. Twitch chat comes from the anonymous read-only
    IRC login (justinfan), stamped on the recording's own timeline."""

    FMT = "b[height<=1080]/b"   # one muxed rendition: video + audio together

    def __init__(self, url: str, work: str, meta: dict):
        self.url, self.work = url, work
        self.plat = scout.platform(url)
        self.channel = (urlparse(url).path.strip("/").split("/") or ["stream"])[0]
        self.title = safe_title(meta.get("title") or f"{self.channel} live")
        self.lang = meta.get("language")
        self.live, self.duration = True, 0.0
        self.rec = os.path.join(work, "rec")
        os.makedirs(self.rec, exist_ok=True)
        self.segs: List[tuple] = []     # (start, end, path) on the recording timeline
        self.chat_log: List[tuple] = []
        self.init: Optional[bytes] = None
        self.lock = threading.Lock()
        self.manifest = _manifest_url(url, self.FMT)
        threading.Thread(target=self._record, daemon=True).start()
        if self.plat == "twitch":
            threading.Thread(target=self._irc, daemon=True).start()
        print(f"   ⏺️ No archive to read back: recording {self.channel} from now (rolling buffer)")

    def refresh(self):
        pass  # the recorder thread keeps duration and live up to date

    def _record(self):
        seen, misses = set(), 0
        while self.live:
            try:
                r = httpx.get(self.manifest, timeout=15)
                if r.status_code in (403, 404, 410):   # token expired or stream gone
                    self.manifest = _manifest_url(self.url, self.FMT)
                    continue
                text = r.text
                if self.init is None:
                    init = hls_init(text, self.manifest)
                    self.init = httpx.get(init, timeout=20).content if init else b""
                m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
                seq = int(m.group(1)) if m else 0
                for dur, title, uri in _hls_entries(text):
                    n, seq = seq, seq + 1
                    if n in seen:
                        continue
                    seen.add(n)
                    if title and title != "live":     # Twitch marks stitched ads with another title
                        continue
                    path = os.path.join(self.rec, f"{n}.seg")
                    with open(path, "wb") as f:
                        f.write(httpx.get(_join(self.manifest, uri), timeout=20).content)
                    with self.lock:
                        self.segs.append((self.duration, self.duration + dur, path))
                        self.duration += dur
                if "#EXT-X-ENDLIST" in text:
                    self.live = False
                misses = 0
            except Exception:
                misses += 1
                if misses > 30:                      # ~2 min without the playlist: offline
                    self.live = False
            time.sleep(4)

    def _irc(self):
        import random
        import socket
        while self.live:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=330)
                s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.channel}\r\n".encode())
                buf = ""
                while self.live:
                    data = s.recv(65536).decode("utf-8", "ignore")
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        m = re.search(r" PRIVMSG #\w+ :(.*)$", line)
                        if m:
                            self.chat_log.append((self.duration, m.group(1)))
            except OSError:
                time.sleep(10)

    def chat(self, a, b):
        return [(t, m) for t, m in list(self.chat_log) if a <= t < b]

    def prune(self, before: float):
        """Drop recorded segments that end before ``before`` (already scanned and sliced)."""
        with self.lock:
            keep = []
            for s in self.segs:
                if s[1] < before:
                    try:
                        os.remove(s[2])
                    except OSError:
                        pass
                else:
                    keep.append(s)
            self.segs = keep

    def _fetch(self, kind, a, b, out):
        with self.lock:
            picked = [s for s in self.segs if s[1] > a and s[0] < b]
        if not picked:
            raise RuntimeError(f"recording has no {a:.0f}-{b:.0f}s (pruned or not reached)")
        with open(out, "wb") as f:
            f.write(self.init or b"")
            for _, _, path in picked:
                with open(path, "rb") as seg:
                    f.write(seg.read())
        return picked[0][0]

    def audio(self, a, b, out):
        raw = out + ".rec"
        start = self._fetch("video", a, b, raw)
        path = out + ".m4a"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a - start), "-t", str(b - a),
                        "-i", raw, "-vn", "-c:a", "aac", path], check=True)
        os.remove(raw)
        return path

    def video(self, a, b, out):
        raw = out + ".rec"
        start = self._fetch("video", a, b, raw)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a - start), "-t", str(b - a), "-i", raw,
                        "-c", "copy", out],  # no re-encode: preset_render encodes once anyway
                       check=True)
        os.remove(raw)
        return out


def _hls_entries(text: str) -> List[tuple]:
    """[(duration, title, uri)] in playlist order."""
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            d, _, title = line[8:].partition(",")
            cur = (float(d), title.strip())
        elif line and not line.startswith("#") and cur:
            out.append((cur[0], cur[1], line))
            cur = None
    return out


def _join(base: str, uri: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, uri)


def parse_hls(text: str, base: str) -> List[tuple]:
    """[(start, end, absolute url)] from a media playlist's #EXTINF entries."""
    from urllib.parse import urljoin
    out, t, dur = [], 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            out.append((t, t + dur, urljoin(base, line)))
            t += dur
            dur = None
    return out


def hls_init(text: str, base: str) -> Optional[str]:
    """fMP4 playlists keep codec headers in one #EXT-X-MAP segment that must lead the bytes."""
    from urllib.parse import urljoin
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    return urljoin(base, m.group(1)) if m else None


def _meta(url: str) -> dict:
    r = scout._ytdlp("--dump-single-json", "--skip-download", url)
    try:
        return json.loads(r.stdout) or {}   # yt-dlp can print "null"
    except ValueError:
        return {}


def _manifest_url(url: str, fmt: str) -> str:
    r = scout._ytdlp("-f", fmt, "-g", url)
    lines = [l for l in r.stdout.splitlines() if l.startswith("http")]
    if not lines:
        raise RuntimeError(f"no HLS manifest ({r.stderr[-200:]})")
    return lines[0]


def _head(manifest: str) -> tuple:
    text = httpx.get(manifest, timeout=30).text
    seqs = [int(m) for m in re.findall(r"/sq/(\d+)/", text)]
    durs = [float(m) for m in re.findall(r"#EXTINF:([\d.]+)", text)]
    if not seqs:
        raise RuntimeError("live manifest lists no segments")
    return max(seqs), (durs[0] if durs else 5.0)


def open_source(url: str, work: str) -> Source:
    plat = scout.platform(url)
    channel_link = plat == "twitch" and not scout._twitch_vod_id(url)
    if channel_link:
        try:
            vod = scout.resolve_vod(url)
        except RuntimeError:
            vod = None                       # no archives at all
        if vod:
            arch = TwitchArchive(vod, work, _meta(vod))
            if arch.live:                    # the archive IS the current broadcast: full history
                return arch
        chan = _meta(url)
        if chan.get("live_status") == "is_live":
            return LiveRecorder(url, work, chan)   # live, but nothing to read back: record from now
        if vod:
            return arch
        raise RuntimeError("Channel is offline and has no archive.")
    meta = _meta(url)
    if not meta:
        raise RuntimeError("Could not read that stream link.")
    if plat == "youtube" and meta.get("webpage_url"):
        url = meta["webpage_url"]   # @channel/live -> this broadcast's own link (resume matches on it)
    live = meta.get("live_status") == "is_live"
    if plat == "twitch":
        return TwitchArchive(url, work, meta)
    if live and plat == "youtube":
        return YouTubeLive(url, work, meta)
    if live:
        return LiveRecorder(url, work, meta)       # Kick and others: record from now
    return Source(url, work, meta)


# --------------------------------------------------------------------------- #
# Watch loop
# --------------------------------------------------------------------------- #
def run(url: str, output_dir: str, llm) -> None:
    work = os.path.join(output_dir, "scout")
    os.makedirs(work, exist_ok=True)
    src = open_source(url, work)
    lang = os.environ.get("STREAMER_LANG") or src.lang or "auto-detect from transcript"
    print(f"📡 Watching: {src.title} ({src.duration / 3600:.1f} h so far, "
          f"{'LIVE' if src.live else 'finished'}). Chunks of {CHUNK // 60} min, every viable clip.")
    meta_path = os.path.join(output_dir, f"{src.title}_metadata.json")
    shorts: List[dict] = []
    lock = threading.Lock()
    whisper_lock = threading.Lock()
    preset = {"value": stream_render.active_preset()}
    done = float(os.environ.get("LIVE_START_SECONDS") or 0)
    started = time.time()
    pool = ThreadPoolExecutor(RENDER_WORKERS)
    pending: dict = {}   # clip index -> start, for clips queued but not rendered yet

    def save():
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"shorts": shorts, "transcript": {"language": lang, "segments": []},
                       "source_video": "", "output_format": "vertical",
                       "live_watch": {"url": src.url, "scanned_until": done}},
                      f, indent=2, ensure_ascii=False)
        os.replace(tmp, meta_path)

    def render(pick: dict, idx: int):
        name = f"{src.title}_clip_{idx + 1}.mp4"
        sl = os.path.join(work, f"slice_{idx + 1}.mp4")
        try:
            src.video(pick["start"], pick["end"], sl)
            with whisper_lock:
                words = scout.transcribe_words(sl)
            # Auto: a found facecam is reused for every later clip; a miss is never
            # cached, so the next clip tries again (people share clips with a face).
            base = preset["value"] or _auto_preset(sl)
            if base.get("cam") and preset["value"] is None:
                preset["value"] = base
            # Fixed per stream on purpose: GTA 6's photoreal characters fool face
            # detection, so switching layouts per clip framed game faces, not the streamer.
            layout = base
            length = pick["end"] - pick["start"]
            if not clip_shows_game(sl, length):
                print(f"   🎮 Clip {idx + 1} dropped: {game_filter()} not on screen")
                return
            if REQUIRE_CAM and not layout.get("cam"):
                print(f"   🙈 Clip {idx + 1} dropped: no facecam found")
                return
            # Gemini looks at the clip's frames and rewrites hook + title to match
            # what is on screen, before the hook is burned in (no key: kept as is).
            import hook_grounding
            hook_grounding.reground(sl, pick, words, 0, length)
            # Rendered under a temp name, renamed only when complete: a clip opened
            # mid-render failed in Windows' player ("Server execution failed").
            tmp = os.path.join(output_dir, f".rendering_{idx + 1}.mp4")
            ok = stream_render.preset_render(sl, tmp, layout, pick, words, 0, length + 1)
            if not ok:
                return
            stream_render.retention_pass(tmp)
            os.replace(tmp, os.path.join(output_dir, name))
            print(f"   ✅ Clip {idx + 1} [{scout._ts(pick['start'])}] {pick['viral_hook_text']}")
            print(f"CLIP_READY {idx} {name}")
            deliver(os.path.join(output_dir, name), pick, words.get("language"), src)
            with lock:
                save()
        except Exception as e:
            print(f"   ❌ Clip {idx + 1} failed: {type(e).__name__}: {e}")
        finally:
            pending.pop(idx, None)
            if os.path.exists(sl):
                os.remove(sl)

    if not os.path.exists(meta_path):
        _adopt_previous_watch(output_dir, src, meta_path)
    # Resume (server restart, crash): keep the clips and progress already saved,
    # re-render any queued clip whose file never got written, carry on from there.
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            saved = json.load(f)
        shorts.extend(saved.get("shorts") or [])
        done = max(done, float((saved.get("live_watch") or {}).get("scanned_until") or 0))
        missing = [i for i in range(len(shorts))
                   if not os.path.exists(os.path.join(output_dir, f"{src.title}_clip_{i + 1}.mp4"))]
        if isinstance(src, LiveRecorder):
            # A fresh recording starts its own timeline at 0: old times mean nothing on it.
            done, missing = 0.0, []
            for c in shorts:
                c["vod_start"] = c["vod_end"] = -1.0
        print(f"↩️ Resuming at {scout._ts(done)} with {len(shorts)} clips ({len(missing)} to re-render)")
        for i in range(len(shorts)):
            if i in missing:
                pool.submit(render, shorts[i], i)
            else:
                print(f"CLIP_READY {i} {src.title}_clip_{i + 1}.mp4")

    while True:
        if os.path.exists(os.path.join(output_dir, "STOP")):
            print("⏹️ STOP file found: finishing the clips in flight.")
            break
        if time.time() - started > MAX_HOURS * 3600:
            print(f"⏹️ {MAX_HOURS:.0f} h limit reached.")
            break
        head = src.duration - (LAG if src.live else 0)
        span = next_chunk(done, head, src.live)
        if span is None:
            if not src.live:
                break
            print(f"   ⏳ Caught up at {scout._ts(done)}; live edge {scout._ts(src.duration)}. "
                  f"Next look in {POLL}s.")
            time.sleep(POLL)
            src.refresh()
            continue
        a, b = span
        try:
            if not chunk_shows_game(src, a, b):
                print(f"   🎮 {scout._ts(a)}-{scout._ts(b)}: not {game_filter()} on screen, skipped")
                picks = []
            else:
                picks = _scan_chunk(src, a, b, lang, llm, whisper_lock)
        except Exception as e:
            print(f"   ⚠️ Chunk {scout._ts(a)}-{scout._ts(b)} skipped: {type(e).__name__}: {e}")
            picks = []
        with lock:
            for p in picks:
                if overlaps(p, shorts):
                    continue
                clip = {**p, "vod_start": p["start"], "vod_end": p["end"]}
                shorts.append(clip)
                pending[len(shorts) - 1] = clip["start"]
                pool.submit(render, clip, len(shorts) - 1)
            done = b
            save()
        print(f"   📈 Scanned to {scout._ts(done)} | {len(shorts)} clips queued so far")
        if hasattr(src, "prune"):   # recorder: keep only what pending clips still need
            src.prune(min([done - 120] + [t - 30 for t in list(pending.values())]))
        if src.live:
            src.refresh()
    pool.shutdown(wait=True)
    save()
    print(f"🏁 Watch over: {len(shorts)} clips from {scout._ts(done)} of stream.")


CLIPS_ROOT = os.environ.get("CLIPS_ROOT") or os.path.dirname(os.path.abspath(__file__))
LANG_DIRS = {"es": "clips-es", "spanish": "clips-es", "en": "clips-en", "english": "clips-en"}


def lang_folder(detected: Optional[str]) -> str:
    """clips-es / clips-en: the streamer's preset language wins, else what Whisper heard."""
    for v in (os.environ.get("STREAMER_LANG"), detected):
        key = (v or "").strip().lower()
        if key in LANG_DIRS:
            return LANG_DIRS[key]
    return "clips-other"


def deliver_name(pick: dict) -> str:
    slug = re.sub(r"[^\w]+", "-", (pick.get("viral_hook_text") or "clip").lower()).strip("-")[:48]
    t = int(pick.get("vod_start", pick.get("start", 0)))
    return f"{time.strftime('%Y-%m-%d')}_{t // 3600:d}h{t % 3600 // 60:02d}m{t % 60:02d}s_{slug}"


def deliver(path: str, pick: dict, detected_lang: Optional[str], src) -> Optional[str]:
    """Hard-link the finished clip into clips-<lang>/<streamer>/ with a .txt of
    everything needed to post it (title, hook, descriptions, source)."""
    streamer = (os.environ.get("STREAMER_NAME") or os.environ.get("STREAMER_PRESET") or "").strip() or         (urlparse(src.url).path.strip("/").split("/") or ["stream"])[0] or "stream"
    folder = os.path.join(CLIPS_ROOT, lang_folder(detected_lang), streamer)
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, deliver_name(pick))
    if os.path.exists(base + ".mp4"):
        os.remove(base + ".mp4")
    try:
        os.link(path, base + ".mp4")
    except OSError:
        import shutil
        shutil.copy2(path, base + ".mp4")
    lines = [f"TITLE: {pick.get('video_title_for_youtube_short', '')}",
             f"HOOK: {pick.get('viral_hook_text', '')}",
             f"SCORE: {pick.get('predicted_score', '')}",
             f"TIKTOK: {pick.get('video_description_for_tiktok', '')}",
             f"INSTAGRAM: {pick.get('video_description_for_instagram', '')}",
             f"WHY: {pick.get('why', '')}",
             f"SOURCE: {src.url} at {scout._ts(pick.get('vod_start', pick.get('start', 0)))}"]
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(chr(10).join(lines) + chr(10))
    return base + ".mp4"


def _adopt_previous_watch(output_dir: str, src: Source, meta_path: str) -> None:
    """A new job on a stream an earlier job already watched continues it: the
    furthest-scanned earlier watch's clips (files hard-linked) and progress move in."""
    best, best_done = None, 0.0
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(output_dir)), "*",
                                       "*_metadata.json")):
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(output_dir):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        lw = data.get("live_watch") or {}
        if lw.get("url") == src.url and float(lw.get("scanned_until") or 0) > best_done:
            best, best_done = path, float(lw["scanned_until"])
    if not best:
        return
    old_dir = os.path.dirname(best)
    old_title = os.path.basename(best)[:-len("_metadata.json")]  # may carry an old live timestamp
    import shutil
    with open(best, encoding="utf-8") as f:
        n = len(json.load(f).get("shorts") or [])
    for i in range(n):
        old_clip = os.path.join(old_dir, f"{old_title}_clip_{i + 1}.mp4")
        new_clip = os.path.join(output_dir, f"{src.title}_clip_{i + 1}.mp4")
        if os.path.exists(old_clip):
            try:
                os.link(old_clip, new_clip)
            except OSError:
                shutil.copy2(old_clip, new_clip)
    shutil.copy2(best, meta_path)
    print(f"🔗 Continuing the earlier watch of this stream ({os.path.basename(old_dir)[:8]}).")


def game_filter() -> str:
    """The game to clip, read per check so a console change applies to running watches.
    lineup.json "game" wins; "" means any game."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lineup.json"), encoding="utf-8") as f:
            game = json.load(f).get("game")
        if game is not None:
            return game.strip()
    except (OSError, ValueError):
        pass
    return os.environ.get("LIVE_GAME_FILTER", "Grand Theft Auto VI (GTA 6)")


def game_question(game: str) -> str:
    if re.search(r"gta|grand theft", game, re.I):
        return GAME_QUESTION.format(game=game)
    return (f"Does ANY of these livestream frames show the video game {game} on screen: gameplay, "
            "its HUD or map, menus, a loading screen or a cutscene? A webcam, chat overlay or "
            "alerts may also be on screen. Answer only yes or no.")


# The model cannot recognise GTA 6 by name (it launched after its training), so it is asked
# about the series' look; asked for "GTA 6" exactly it said no to plain GTA 6 gameplay.
GAME_QUESTION = ("Does ANY of these livestream frames show a Grand Theft Auto game on screen: "
                 "gameplay, driving, the minimap/HUD, a mission prompt, the map, a loading screen, "
                 "settings or menus, or a cutscene? The target is {game}, a 2026 release you may not "
                 "have seen; any GTA-style open-world crime game counts. A webcam, chat overlay or "
                 "alerts may also be on screen. Answer only yes or no.")


def _jpg(video: str, at: float) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", video, "-frames:v", "1",
                           "-vf", "scale=768:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                          capture_output=True).stdout


def frames_show_game(frames: List[bytes], tries: int = 5) -> bool:
    """Gemini yes/no on the frames. Unsure is NO: when Gemini cannot answer after
    retries (~5 min), the stretch is skipped. Failing open once let a non-GTA
    clip (a real police bodycam video) through. No filter or no key: allowed."""
    key = os.getenv("GEMINI_API_KEY")
    if not game_filter() or not key:
        return True
    if not frames:
        return False
    from google import genai
    from google.genai import types
    parts = [types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames]
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60_000))  # keep a reference: a dropped client closes mid-request
    for attempt in range(tries):
        try:
            r = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite",
                contents=parts + [game_question(game_filter())])
            return "yes" in (r.text or "").lower()
        except Exception as e:
            print(f"   ℹ️ Game check retry {attempt + 1}/{tries} ({type(e).__name__})")
            time.sleep(20 * (attempt + 1))
    print("   ⛔ Game check unavailable: stretch skipped (unsure counts as no)")
    return False


def chunk_shows_game(src: Source, a: float, b: float) -> bool:
    """Two frames of the chunk: streams switch games and titles rarely say which."""
    if not game_filter() or not os.getenv("GEMINI_API_KEY"):
        return True
    frames = []
    for i, t in enumerate((a + (b - a) * 0.3, a + (b - a) * 0.7)):
        clip = os.path.join(src.work, f"probe_{int(a)}_{i}.mp4")
        try:
            src.video(t, t + 2, clip)
            jpg = _jpg(clip, 1)
            if jpg:
                frames.append(jpg)
        except Exception:
            pass
        finally:
            if os.path.exists(clip):
                os.remove(clip)
    return frames_show_game(frames)


def clip_shows_game(slice_path: str, length: float) -> bool:
    """The clip's own frames: a chunk can mix GTA with something else."""
    if not game_filter() or not os.getenv("GEMINI_API_KEY"):
        return True
    return frames_show_game([f for f in (_jpg(slice_path, length * k) for k in (0.25, 0.5, 0.75)) if f])


def _scan_chunk(src: Source, a: float, b: float, lang: str, llm, whisper_lock) -> List[dict]:
    a0 = max(0.0, a - scout.PRE_ROLL)  # room for the setup before a peak right at the chunk start
    print(f"🔎 Chunk {scout._ts(a)}-{scout._ts(b)}")
    audio = src.audio(a0, b, os.path.join(src.work, f"audio_{int(a)}"))
    try:
        loud = scout.loudness(audio)
        n = len(loud)
        chat = src.chat(a0, b)
        local_chat = [(t - a0, m) for t, m in chat]
        scores = scout.hype_scores(scout.per_second_chat(local_chat, n), loud)
        k = max(1, round(WINDOWS_PER_CHUNK * (b - a) / CHUNK))
        wins = [w for w in scout.top_windows(scores, k + 2, n) if w["peak"] >= a - a0][:k]
        for w in wins:
            for f in ("start", "end", "peak"):
                w[f] += a0
            w["id"] = f"c{int(a)}_{w['id']}"
        print(f"   💬 {len(chat)} chat msgs | 🔥 peaks at " + ", ".join(scout._ts(w["peak"]) for w in wins))
        with whisper_lock:
            scout.transcribe_windows(audio, wins, offset=a0)
        picks = scout.pick_clips(wins, chat, lang, b + 15, llm, k=CLIPS_PER_CHUNK)
        print(f"   ✂️ {len(picks)} clip(s) picked")
        return picks
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass


def _auto_preset(slice_path: str) -> dict:
    """'auto': find the facecam once (stock detector) and reuse its box; none -> gameplay only."""
    try:
        import camera_inset
        box = camera_inset.detect(slice_path)
        if box:
            W, H, _ = stream_render._probe(slice_path)
            x, y, w, h = box
            print(f"   📹 Facecam found at {box}")
            return {"cam": {"x": x / W, "y": y / H, "w": w / W, "h": h / H}}
    except Exception as e:
        print(f"   ℹ️ Facecam detection skipped ({type(e).__name__})")
    print("   ℹ️ No facecam: gameplay-only layout")
    return {"cam": None}
