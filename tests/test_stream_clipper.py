"""Pure helpers of the stream upgrades: scout scoring, picks, retention, captions, CLI provider."""
import json

import numpy as np
import pytest
from pydantic import BaseModel

import claude_cli_provider
import llm_backend
import stream_render
import stream_scout


def test_platform_allowlist():
    assert stream_scout.platform("https://www.twitch.tv/videos/123") == "twitch"
    assert stream_scout.platform("https://youtu.be/abc") == "youtube"
    assert stream_scout.platform("https://kick.com/x/videos/1") == "kick"
    assert stream_scout.platform("https://evil.com/twitch.tv") is None


def test_chat_spike_and_scream_become_windows():
    total = 3600
    msgs = [(t, "hello") for t in range(0, total, 5)]
    msgs += [(1000 + i * 0.05, "W") for i in range(200)]        # chat spike at ~1000 s
    loud = np.full(total, -40.0)
    loud[2500:2510] = -5.0                                        # scream after quiet
    scores = stream_scout.hype_scores(stream_scout.per_second_chat(msgs, total), loud)
    wins = stream_scout.top_windows(scores, n=2, total=total)
    peaks = sorted(w["peak"] for w in wins)
    assert 970 <= peaks[0] <= 1000 and 2480 <= peaks[1] <= 2510
    for w in wins:   # setup kept before the peak, no overlap
        assert w["peak"] - w["start"] == stream_scout.PRE_ROLL
    assert wins[0]["end"] <= wins[1]["start"]


def test_audio_only_when_no_chat():
    loud = np.full(600, -40.0)
    loud[300:305] = -3.0
    s = stream_scout.hype_scores(np.zeros(600), loud)
    assert 280 <= int(np.argmax(s)) <= 305


def test_clamp_pick_enforces_30_55():
    w = {"start": 100.0, "end": 160.0}
    assert stream_scout.clamp_pick({"start": 100, "end": 200}, w, 1000)["end"] == 155.0
    short = stream_scout.clamp_pick({"start": 120, "end": 130}, w, 1000)
    assert short["end"] - short["start"] == 30.0


def test_keep_segments_strips_long_pauses():
    keep = stream_render.keep_segments([(2.0, 3.0), (5.0, 5.1)], 10.0)
    assert keep == [(0.0, 2.08), (2.92, 10.0)]   # 0.1 s pause shorter than 2*pad stays


def test_peak_start_finds_loudest_stretch():
    rms = np.ones(200)
    rms[120:145] = 50
    assert stream_render.peak_start(rms, 0.1) == pytest.approx(12.0)


def test_kinetic_events_highlight_each_word():
    words = [{"start": 0.0, "end": 0.3, "word": "no"}, {"start": 0.3, "end": 0.6, "word": "way{"},
             {"start": 0.6, "end": 1.0, "word": "bro"}, {"start": 1.2, "end": 1.5, "word": "W"}]
    ev = stream_render.kinetic_events(words)
    assert len(ev) == 4 and ev[0][1] == 0.3
    assert "WAY" in ev[1][2] and "{" not in ev[1][2].replace("{\\", "")
    assert ev[3][2].count("\\c&H00E5FF&") == 1


def test_presets_file():
    p = stream_render.presets()
    assert {"auto", "kaicenat", "thegrefg", "rdjavi"} <= set(p)
    assert p["auto"]["cam"] is None and p["rdjavi"]["cam"]["x"] < 0.1


class _S(BaseModel):
    ok: bool


def test_claude_cli_provider_routing(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude_cli")
    assert llm_backend.active() and llm_backend.model_name() == "sonnet"
    cmd = claude_cli_provider.command({"type": "object"})
    assert "-p" in cmd and "--json-schema" in cmd and "dontAsk" in cmd
    payload, _ = claude_cli_provider.parse_output(json.dumps(
        {"is_error": False, "structured_output": {"ok": True}, "usage": {}}))
    assert payload == {"ok": True}
    with pytest.raises(RuntimeError):
        claude_cli_provider.parse_output(json.dumps({"is_error": True, "result": "nope"}))


def test_api_key_providers_still_route(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    assert llm_backend.active() and llm_backend.provider() == "openai"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("LLM_BASE_URL")
    assert not llm_backend.active()


def test_live_chunks_backfill_then_follow():
    import live_watch as lw
    assert lw.next_chunk(0, 5 * 3600, live=True) == (0, 900)           # backfill from 0:00
    assert lw.next_chunk(17100, 17500, live=True) is None              # caught up: wait
    assert lw.next_chunk(17100, 17500, live=False) == (17100, 17500)   # ended: last bit
    assert lw.next_chunk(17480, 17500, live=False) is None             # nothing left


def test_live_overlap_and_segments():
    import live_watch as lw
    have = [{"vod_start": 100, "vod_end": 140}]
    assert lw.overlaps({"start": 130, "end": 170}, have)
    assert not lw.overlaps({"start": 140, "end": 180}, have)
    assert list(lw.segment_range(600, 630, 5.0)) == list(range(120, 127))
    assert lw.safe_title("GTA 6 🔥 LIVE!!") == "GTA 6  LIVE"


def test_parse_hls_ranges():
    import live_watch as lw
    text = "#EXTM3U\n#EXTINF:10.000,\n0.ts\n#EXTINF:10.000,\n1.ts\n#EXTINF:4.5,\nhttps://x.cdn/2.ts\n"
    segs = lw.parse_hls(text, "https://cdn.test/vod/audio_only/index-dvr.m3u8")
    assert segs[0] == (0.0, 10.0, "https://cdn.test/vod/audio_only/0.ts")
    assert segs[2][2] == "https://x.cdn/2.ts" and segs[-1][1] == 24.5
    assert [s[2][-4:] for s in segs if s[1] > 12 and s[0] < 15] == ["1.ts"]


def test_new_job_adopts_previous_watch(tmp_path):
    import json as _j
    import live_watch as lw

    class S:  # minimal source
        title, url = "GTA 6 EN VIVO", "https://www.youtube.com/watch?v=x"
    old = tmp_path / "old"; old.mkdir()
    (old / "GTA 6 EN VIVO_clip_1.mp4").write_bytes(b"clip")
    (old / "GTA 6 EN VIVO_metadata.json").write_text(_j.dumps(
        {"shorts": [{"start": 1}, {"start": 2}], "live_watch": {"url": S.url, "scanned_until": 1800}}))
    new = tmp_path / "new"; new.mkdir()
    meta = new / "GTA 6 EN VIVO_metadata.json"
    lw._adopt_previous_watch(str(new), S, str(meta))
    assert _j.loads(meta.read_text())["live_watch"]["scanned_until"] == 1800
    assert (new / "GTA 6 EN VIVO_clip_1.mp4").read_bytes() == b"clip"


def test_adopt_maps_old_timestamped_names(tmp_path):
    import json as _j
    import live_watch as lw

    class S:
        title, url = "GTA 6 EN VIVO", "https://www.youtube.com/watch?v=x"
    old = tmp_path / "old"; old.mkdir()
    (old / "GTA 6 EN VIVO  2026-11-18 1228_clip_1.mp4").write_bytes(b"c1")
    (old / "GTA 6 EN VIVO  2026-11-18 1228_metadata.json").write_text(_j.dumps(
        {"shorts": [{"start": 1}], "live_watch": {"url": S.url, "scanned_until": 900}}))
    new = tmp_path / "new"; new.mkdir()
    lw._adopt_previous_watch(str(new), S, str(new / "GTA 6 EN VIVO_metadata.json"))
    assert (new / "GTA 6 EN VIVO_clip_1.mp4").read_bytes() == b"c1"



def test_clip_delivery_naming(monkeypatch):
    import live_watch as lw
    monkeypatch.setenv("STREAMER_LANG", "Spanish")
    assert lw.lang_folder("en") == "clips-es"          # the preset's language wins
    monkeypatch.delenv("STREAMER_LANG")
    assert lw.lang_folder("en") == "clips-en" and lw.lang_folder("fr") == "clips-other"
    name = lw.deliver_name({"viral_hook_text": "¡NO TE VA A DAR TIEMPO!", "vod_start": 1528.4})
    assert name.endswith("_0h25m28s_no-te-va-a-dar-tiempo")


def test_hls_init_segment():
    import live_watch as lw
    text = '#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:2.0,\n0.mp4\n'
    assert lw.hls_init(text, "https://cdn.test/a/index.m3u8") == "https://cdn.test/a/init-0.mp4"
    assert lw.hls_init("#EXTM3U\n#EXTINF:2.0,\n0.ts\n", "https://cdn.test/a/i.m3u8") is None


def test_hls_entries_flag_twitch_ads():
    import live_watch as lw
    text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXTINF:2.000,live\na.ts\n#EXTINF:2.000,Amazon|123\nad.ts\n"
    assert lw._hls_entries(text) == [(2.0, "live", "a.ts"), (2.0, "Amazon|123", "ad.ts")]
