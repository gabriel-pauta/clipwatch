"""Keep one stream watch running per lineup channel, unattended.

Every few minutes: for each channel in lineup.json that is live and has no
watch in flight, submit a stream-mode job to the local backend. A watch clips
only the GTA 6 stretches (live_watch.chunk_shows_game) and drops every finished
clip into clips-es / clips-en. When a stream ends its watch ends; the next time
the channel goes live a new one starts.

    .venv\\Scripts\\python lineup.py --i-have-rights

The flag is the rights confirmation the dashboard asks for, given once for the
whole lineup by the person who starts the runner.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("LINEUP_API", "http://localhost:8000")
STATE = os.path.join(HERE, "lineup_state.json")
EVERY = int(os.environ.get("LINEUP_EVERY_SECONDS", "300"))


def channels() -> list:
    path = os.path.join(HERE, "lineup.json")
    if not os.path.exists(path):
        return []  # first run: streamers are picked in the console (/console) or by the agent
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    for lang in ("es", "en"):
        for entry in cfg.get(lang, []):
            # "https://..." or {"url": "...", "preset": "..."} when the link is not the preset name
            url = entry["url"] if isinstance(entry, dict) else entry
            preset = entry.get("preset") if isinstance(entry, dict) else None
            out.append((url, lang, preset or name_of(url)))
    return out


def name_of(url: str) -> str:
    return urlparse(url).path.strip("/").split("/")[0].lstrip("@").lower()


def live_link(url: str):
    """The live broadcast's own link, or None when the channel is offline.
    YouTube @channel/live becomes watch?v=... (the backend accepts single videos only)."""
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download",
                        "--print", "%(live_status)s %(webpage_url)s", url],
                       capture_output=True, text=True, timeout=90)
    status, _, link = (r.stdout.strip().splitlines() or [""])[-1].partition(" ")
    if status != "is_live":
        return None
    return link if "youtube.com" in url and "watch?v=" in link else url


def job_active(job_id: str) -> bool:
    try:
        r = httpx.get(f"{API}/api/status/{job_id}", timeout=15)
        return r.status_code == 200 and r.json().get("status") in ("queued", "processing")
    except httpx.HTTPError:
        return True  # backend unreachable: do not stack a duplicate


def known_presets() -> set:
    try:
        with open(os.path.join(HERE, "streamer_presets.json"), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def submit(url: str, name: str, lang: str, gemini_key: str) -> str:
    headers = {"X-Gemini-Key": gemini_key} if gemini_key else {}
    # A streamer without a facecam preset uses the stock face/screen detector ("auto").
    preset = name if name in known_presets() else "auto"
    r = httpx.post(f"{API}/api/process", headers=headers, timeout=60, json={
        "url": url, "acknowledged": True, "stream_mode": "1", "streamer_preset": preset,
        "streamer_name": name, "streamer_lang": lang})
    r.raise_for_status()
    return r.json()["job_id"]


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "lineup.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--i-have-rights", action="store_true",
                    help="confirm you may process these channels' streams")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    args = ap.parse_args()
    if not args.i_have_rights:
        sys.exit("Refusing to start: pass --i-have-rights to confirm you may clip these streams.")
    key = dotenv_values(os.path.join(HERE, ".env")).get("GEMINI_API_KEY") or ""
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    log(f"lineup runner up: {', '.join(p for _, _, p in channels()) or 'no streamers yet'}")
    while True:
        key = dotenv_values(os.path.join(HERE, ".env")).get("GEMINI_API_KEY") or ""
        for url, lang, name in channels():  # re-read every pass: console edits apply live
            job = state.get(name)
            if job and job_active(job):
                continue
            try:
                link = live_link(url)
                if not link:
                    continue
                state[name] = submit(link, name, lang, key)
                log(f"{name} ({lang}) is live: watch started, job {state[name]}")
                with open(STATE, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
            except Exception as e:
                log(f"{name}: {type(e).__name__}: {str(e)[:200]}")
        if args.once:
            return
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
