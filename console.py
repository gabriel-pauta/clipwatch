"""Clipwatch console: the local web page at /console.

Pick the streamers to watch (first run asks), start or stop the lineup runner,
and browse the finished clips in clips-es / clips-en / clips-other.
Local self-host only: it reads and writes files next to this one.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psutil
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
LINEUP = HERE / "lineup.json"
STATE = HERE / "lineup_state.json"
LOG = HERE / "lineup.log"
PAGE = HERE / "console" / "index.html"
CLIP_DIRS = ("clips-es", "clips-en", "clips-other")
DEFAULT_GAME = "Grand Theft Auto VI (GTA 6)"
NAME_OK = re.compile(r"^[\w.-]{1,60}$")

router = APIRouter()


class Channel(BaseModel):
    url: str
    lang: str = "en"
    preset: str | None = None


class Lineup(BaseModel):
    game: str = DEFAULT_GAME
    channels: list[Channel] = []


class Key(BaseModel):
    key: str


class Start(BaseModel):
    i_have_rights: bool = False


def normalize(raw: str) -> str:
    """'xqc', 'twitch.tv/xqc', '@RDjavi' or a full link -> a full channel link."""
    raw = raw.strip()
    if not raw:
        raise HTTPException(400, "Empty channel.")
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}/live"
    if "://" not in raw:
        raw = ("https://" + raw) if "." in raw.split("/")[0] else f"https://www.twitch.tv/{raw}"
    host = urlparse(raw).hostname or ""
    if not any(h in host for h in ("twitch.tv", "youtube.com", "youtu.be", "kick.com")):
        raise HTTPException(400, f"Not a Twitch, YouTube or Kick link: {raw}")
    if "youtube.com/@" in raw and not raw.rstrip("/").endswith("/live"):
        raw = raw.rstrip("/") + "/live"
    return raw


def name_of(url: str) -> str:
    return urlparse(url).path.strip("/").split("/")[0].lstrip("@").lower() or "stream"


def read_lineup() -> dict:
    if not LINEUP.exists():
        return {"configured": False, "game": DEFAULT_GAME, "channels": []}
    cfg = json.loads(LINEUP.read_text(encoding="utf-8"))
    out = []
    for lang in ("es", "en"):
        for e in cfg.get(lang, []):
            url = e["url"] if isinstance(e, dict) else e
            preset = e.get("preset") if isinstance(e, dict) else None
            out.append({"url": url, "lang": lang, "preset": preset, "name": preset or name_of(url)})
    return {"configured": bool(out), "game": cfg.get("game", DEFAULT_GAME), "channels": out}


def runner_pid() -> int | None:
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info["cmdline"] or []
            if any(c.endswith("lineup.py") for c in cmd) and "--i-have-rights" in cmd:
                return p.info["pid"]
        except (psutil.Error, TypeError):
            continue
    return None


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/console")


@router.get("/console", include_in_schema=False)
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.get("/api/console/lineup")
def get_lineup():
    return read_lineup()


@router.put("/api/console/lineup")
def put_lineup(body: Lineup):
    cfg = {"_comment": "Written by the Clipwatch console. lang picks clips-es / clips-en.",
           "game": body.game.strip(), "es": [], "en": []}
    seen = set()
    for c in body.channels:
        url = normalize(c.url)
        if url in seen:
            continue
        seen.add(url)
        lang = c.lang if c.lang in ("es", "en") else "en"
        cfg[lang].append({"url": url, "preset": c.preset} if c.preset else url)
    LINEUP.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return read_lineup()


@router.get("/api/console/status")
def status():
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:] if LOG.exists() else []
    env = (HERE / ".env").read_text(encoding="utf-8", errors="replace") if (HERE / ".env").exists() else ""
    has_key = bool(re.search(r"^GEMINI_API_KEY=\s*\S+", env, re.M))
    return {"running": runner_pid() is not None, "jobs": state, "log": tail, "gemini_key": has_key}


@router.post("/api/console/key")
def save_key(body: Key):
    """Store the user's own Google (Gemini) API key in .env; never sent back to the page."""
    key = body.key.strip()
    if not re.fullmatch(r"[\w-]{20,200}", key):
        raise HTTPException(400, "That doesn't look like a Google API key. Copy it again from aistudio.google.com/apikey.")
    path = HERE / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith("GEMINI_API_KEY=")] + [f"GEMINI_API_KEY={key}"]
    if not any(l.startswith("LLM_PROVIDER=") for l in lines):
        lines.append("LLM_PROVIDER=gemini")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = key  # new jobs see it without a backend restart
    return {"gemini_key": True}


@router.post("/api/console/runner/start")
def start(body: Start):
    if not body.i_have_rights:
        raise HTTPException(400, "Confirm you may clip these channels first.")
    if not read_lineup()["configured"]:
        raise HTTPException(400, "Add at least one streamer first.")
    if runner_pid():
        return {"running": True}
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log = open(LOG, "a", encoding="utf-8")  # the runner's own stdout duplicates its log lines; keep crashes
    subprocess.Popen([sys.executable, str(HERE / "lineup.py"), "--i-have-rights"], cwd=HERE,
                     stdout=subprocess.DEVNULL, stderr=log, creationflags=flags, env=env)
    return {"running": True}


@router.post("/api/console/runner/stop")
def stop():
    pid = runner_pid()
    if pid:
        psutil.Process(pid).terminate()
    return {"running": False}


@router.get("/api/console/clips")
def clips(limit: int = 300):
    out = []
    for d in CLIP_DIRS:
        root = HERE / d
        if not root.is_dir():
            continue
        for mp4 in root.glob("*/*.mp4"):
            txt = mp4.with_suffix(".txt")
            st = mp4.stat()
            out.append({
                "lang": d.removeprefix("clips-"), "streamer": mp4.parent.name, "file": mp4.name,
                "src": f"/console/clip/{d}/{mp4.parent.name}/{mp4.name}",
                "info": txt.read_text(encoding="utf-8", errors="replace") if txt.exists() else "",
                "mtime": st.st_mtime, "size": st.st_size,
            })
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out[:max(1, min(limit, 2000))]


@router.get("/console/clip/{folder}/{streamer}/{file}", include_in_schema=False)
def clip_file(folder: str, streamer: str, file: str):
    # Path parts are checked one by one: no "..", no separators, only the clip folders.
    if folder not in CLIP_DIRS or not NAME_OK.match(streamer) or not re.fullmatch(r"[\w.-]{1,200}\.(mp4|txt)", file):
        raise HTTPException(404)
    path = HERE / folder / streamer / file
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
