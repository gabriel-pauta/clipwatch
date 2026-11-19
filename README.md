# clipwatch

Watches your favorite streamers while they're live and cuts vertical Shorts for you, with the streamer's face in frame. Pick the channels, pick the game, press start. Clips land in a folder and in a local web page, ready to post.

Built on [OpenShorts](https://github.com/mutonby/openshorts) (MIT). Runs on your own Windows PC and uses a **free** Gemini API key, so it costs $0 to run.

![The clip gallery](docs/clipwatch/clips.jpg)

## Paste this into your agent to start

Works with Claude Code, Codex, Cursor or any coding agent that can run commands on your PC. Copy the whole block:

```text
Set up Clipwatch for me on this Windows PC: https://github.com/gabriel-pauta/clipwatch

1. Clone it into my home folder (git clone https://github.com/gabriel-pauta/clipwatch).
2. Check I have Python 3.11 (py -3.11), ffmpeg and git. If one is missing, give me the
   winget command to install it and wait for me.
3. Ask me which streamers I want clips from. For each one, ask for the Twitch name,
   YouTube @handle or Kick link, and whether its clips are English (en) or Spanish (es).
   Then ask which game to clip (default: Grand Theft Auto VI (GTA 6); empty = any game).
4. Write my answers to lineup.json in the repo folder, in this shape:
   {"game": "Grand Theft Auto VI (GTA 6)",
    "en": ["https://www.twitch.tv/somename"],
    "es": ["https://www.youtube.com/@somehandle/live"]}
5. Copy .env.example to .env. Ask me for a free Google (Gemini) API key from
   https://aistudio.google.com/apikey and put it in .env as GEMINI_API_KEY=...
   Never print the key back to me. If I'd rather not paste it here, skip this:
   the console has an "add google api key" box.
6. Run start.bat from the repo folder. The first run installs about 2 GB of packages and
   takes a while. When it's done the console opens at http://localhost:8000/console
7. Tell me to press "start watching" there myself, because that button is where I
   confirm I'm allowed to clip these channels. Don't press it for me.
```

## How it works

1. **Pick streamers.** The first time you open the console it asks who to watch. Type a Twitch name, a YouTube `@handle` or a link, choose EN or ES, and add as many as you like. There are quick-add buttons for a few big GTA streamers.
2. **Pick the game.** Clips are only cut while that game is on screen (Gemini looks at frames to check). Leave it empty to clip anything.
3. **Start watching.** Every 5 minutes Clipwatch checks who is live and starts a watch for each one. It works through the stream in 15-minute chunks and keeps up with the live edge.
4. **Get clips.** Finished Shorts appear in the console and in `clips-en/<streamer>/` or `clips-es/<streamer>/`. Each `.mp4` has a `.txt` next to it with a title, hook, ready-to-paste TikTok and Instagram captions, and the source timestamp.

![First-run setup](docs/clipwatch/setup.jpg)

## Manual install

You need Windows, [Python 3.11](https://www.python.org/downloads/release/python-3119/), [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) (`winget install Gyan.FFmpeg`) and git.

```powershell
git clone https://github.com/gabriel-pauta/clipwatch
cd clipwatch
copy .env.example .env
notepad .env
```

Put your free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) after `GEMINI_API_KEY=` and save. (Or skip this: the console shows an **add google api key** box until a key is set.) Then:

```powershell
.\start.bat
```

The first run builds a Python environment (~2 GB). After that, `start.bat` starts the backend and opens **http://localhost:8000/console**.

## Using the console

| What | Where |
|---|---|
| Add or remove streamers, change the game | **edit** next to "lineup" |
| Start / stop watching | button top right |
| Only one streamer's clips | click the name in the lineup |
| Only English or Spanish | EN / ES switch above the clips |
| Copy the caption for a clip | copy icon under the clip |
| Download a clip | arrow icon under the clip |
| See what the runner is doing | **runner log** at the bottom of the lineup |

Stopping only stops new watches. A stream that's already being watched finishes on its own when it ends.

## Good to know

- **Only clip streams you're allowed to.** Your own channel, or a streamer who said yes. The start button asks you to confirm.
- A stream needs a visible facecam. No face, no clip.
- If Gemini can't tell whether the game is on screen, that stretch is skipped.
- Big streamers have tuned facecam positions in `streamer_presets.json`. Anyone else uses automatic face detection.
- Free Gemini keys have rate limits. Five to seven streamers at once is fine; dozens will hit the limit.
- Prefer your Claude subscription for picking clips? Set `LLM_PROVIDER=claude_cli` in `.env` (the Gemini key is still used for the game check).

## License

MIT, same as OpenShorts (see `LICENSE` and `NOTICE`). The `cloud/` folder is under OpenShorts' own commercial license and isn't used by Clipwatch. The original OpenShorts README is in [docs/OPENSHORTS.md](docs/OPENSHORTS.md).
