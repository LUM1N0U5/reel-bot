#!/usr/bin/env python3
"""
Reel Bot
  find popular YouTube video -> pick the most viral-worthy 30-60s moment (scored BEFORE rendering)
  -> blurred-background 9:16 reel + karaoke captions -> post to Facebook Page as a Reel.

Usage:
  python reel_bot.py              normal run (posts to Facebook)
  python reel_bot.py --dry-run    does everything except posting / recording (saves work/reel.mp4)
  python reel_bot.py --selftest   checks that YouTube downloads work from this machine
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path
import requests

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
STATE_FILE = ROOT / "used_segments.json"

YT_API_KEY = os.environ.get("YT_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH = "https://graph.facebook.com/v21.0"
BUFFER = 3.0   # seconds of safety margin between used segments
PAUSE = 0.8    # gap between word start times that counts as a phrase boundary

COOKIES = None
if os.environ.get("YT_COOKIES"):
    COOKIES = WORK / "cookies.txt"
    COOKIES.write_text(os.environ["YT_COOKIES"], encoding="utf-8")


def log(*a):
    print(*a, flush=True)


# ============================ record keeping ============================
def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def overlaps(s, e, used):
    return any(s < u[1] + BUFFER and e > u[0] - BUFFER for u in used)


# ============================ 1. find videos ============================
def find_videos():
    import datetime as dt
    if not YT_API_KEY:
        raise SystemExit("YT_API_KEY is not set")
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CFG["days_back"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=30, params=dict(
        part="snippet", q=CFG["topic"], type="video", order="viewCount", publishedAfter=after,
        maxResults=15, key=YT_API_KEY, videoDuration=CFG["video_duration"],
        relevanceLanguage=CFG["language"]))
    r.raise_for_status()
    ids = [i["id"]["videoId"] for i in r.json().get("items", [])]
    if not ids:
        return []
    d = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30,
                     params=dict(part="snippet", id=",".join(ids), key=YT_API_KEY)).json()
    meta = {v["id"]: v["snippet"] for v in d.get("items", [])}
    return [dict(id=i, title=meta[i]["title"], channel=meta[i]["channelTitle"],
                 cat=str(meta[i].get("categoryId", "")), desc=(meta[i].get("description") or "")[:300])
            for i in ids if i in meta]


# ============================ 2. analyse video (before any rendering) ============================
def ydl_opts(**extra):
    o = {"quiet": True, "no_warnings": True,
         "js_runtimes": {"node": {}, "deno": {}},
         "remote_components": ["ejs:github"]}
    if COOKIES:
        o["cookiefile"] = str(COOKIES)
    o.update(extra)
    return o


def parse_words(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    words = []
    for ev in data.get("events", []):
        base = ev.get("tStartMs", 0)
        for seg in ev.get("segs", []) or []:
            t = seg.get("utf8", "").strip()
            if t:
                words.append({"w": t, "t": (base + seg.get("tOffsetMs", 0)) / 1000})
    words.sort(key=lambda x: x["t"])
    return words


YT_NOTES = []        # warnings/errors yt-dlp printed for the current video
BLOCK_HITS = 0       # consecutive videos that looked blocked by YouTube
DIAG_SHOWN = 0


class _YDLLog:
    """Collects yt-dlp warnings so we can SHOW why captions are missing."""
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): YT_NOTES.append(str(m))
    def error(self, m): YT_NOTES.append(str(m))


def fetch_info(vid):
    """Returns (duration, heatmap, words) using YouTube captions + 'most replayed' data."""
    global BLOCK_HITS, DIAG_SHOWN
    from yt_dlp import YoutubeDL
    for f in WORK.glob(f"{vid}.*.json3"):
        f.unlink()
    YT_NOTES.clear()
    opts = ydl_opts(skip_download=True, ignore_no_formats_error=True, writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=[CFG["language"]], subtitlesformat="json3",
                    outtmpl={"default": str(WORK / "%(id)s.%(ext)s")}, logger=_YDLLog())
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
    lang = (info.get("language") or "").lower()
    if lang and not lang.startswith(CFG["language"].lower()):
        log(f"  [lang] video language is '{lang}', not '{CFG['language']}' - skipping")
        BLOCK_HITS = 0
        return info.get("duration") or 0, [], []
    files = list(WORK.glob(f"{vid}.*.json3"))
    words = parse_words(files[0]) if files else []
    if words:
        BLOCK_HITS = 0
    else:
        notes = " | ".join(" ".join(n.split())[:200] for n in YT_NOTES[:3]) or "no message at all"
        blocked = any(x in n.lower() for n in YT_NOTES
                      for x in ("not a bot", "sign in", "login required", "cookies", "confirm you"))
        if blocked:
            BLOCK_HITS += 1
        if DIAG_SHOWN < 3:
            DIAG_SHOWN += 1
            tracks = len(info.get("subtitles") or {}) + len(info.get("automatic_captions") or {})
            log(f"  [diag] formats={len(info.get('formats') or [])}, caption tracks={tracks}; yt-dlp said: {notes}")
        if BLOCK_HITS >= 3:
            raise SystemExit(
                "STOPPED: YouTube is refusing the saved login (YT_COOKIES), so nothing can be read. "
                "Export fresh cookies from the throwaway account and update the YT_COOKIES secret.")
    return info.get("duration") or 0, info.get("heatmap") or [], words


def pick_peaks(heat, n):
    centers = []
    for h in sorted(heat, key=lambda h: h["value"], reverse=True):
        c = (h["start_time"] + h["end_time"]) / 2
        if all(abs(c - x) >= 40 for x in centers):
            centers.append(c)
        if len(centers) >= n:
            break
    return centers


def heat_avg(heat, s, e):
    tot = w = 0.0
    for h in heat:
        ov = min(e, h["end_time"]) - max(s, h["start_time"])
        if ov > 0:
            tot += h["value"] * ov
            w += ov
    return tot / w if w else 0.0


def pace_score(ws, s, e):
    """0-100: speech density across the clip, penalising long dead stretches."""
    n = len(ws)
    wps = n / max(e - s, 1)
    if wps < 2.0:
        sc = max(0, (wps - 1.0)) * 100
    elif wps > 3.8:
        sc = max(0, (5.5 - wps) / 1.7) * 100
    else:
        sc = 100
    gaps = [ws[i + 1]["t"] - ws[i]["t"] for i in range(n - 1)]
    if gaps and max(gaps) > 2.5:
        sc -= 30
    return max(0, min(100, sc))


def make_candidates(words, heat, duration):
    """30-60s windows that start/end on natural pauses, centred on replay peaks."""
    lo, hi = CFG["min_clip"], CFG["max_clip"]
    ends = [i for i in range(len(words) - 1) if words[i + 1]["t"] - words[i]["t"] >= PAUSE]
    starts = [0] + [i + 1 for i in ends]
    if heat:
        centers = pick_peaks(heat, 4)
    else:
        centers = list(range(int(hi), int(max(duration - hi, hi + 1)), 90))[:4]
    targets = [lo + 5, (lo + hi) // 2, hi - 5]
    seen, out = set(), []
    for c in centers:
        near = [si for si in starts if c - hi + 10 <= words[si]["t"] <= c - 3]
        if len(near) > 5:
            near = near[::max(1, len(near) // 5)][:5]
        for si in near:
            s = words[si]["t"]
            for tgt in targets:
                cand = None
                for ei in ends:
                    e = min(words[ei]["t"] + 0.6, words[ei + 1]["t"])
                    if e - s > tgt:
                        break
                    if e - s >= lo:
                        cand = ei
                if cand is None or (si, cand) in seen:
                    continue
                seen.add((si, cand))
                e = min(words[cand]["t"] + 0.6, words[cand + 1]["t"])
                ws = words[si:cand + 1]
                out.append(dict(s=max(0, s - 0.2), e=e, text=" ".join(w["w"] for w in ws),
                                heat=heat_avg(heat, s, e) if heat else 0.0,
                                pace=pace_score(ws, s, e)))
    out.sort(key=lambda c: c["heat"] * 50 + c["pace"] * 0.5, reverse=True)
    return out[:6]


MODEL_FALLBACKS = ["gemini-flash-latest"]
_GOOD_MODEL = None
_DISCOVERED = None
_DEAD = {"gemini-2.5-flash", "gemini-2.5-flash-lite"}   # known retired for this key: never try them
GEM_DEADLINE = None   # time.time() after which we stop asking Gemini this run
_FAILS = 0         # set to 99 only when the key itself is rejected
GEM_BASE = "https://generativelanguage.googleapis.com/v1beta"


def discover_models():
    """Ask Google which Flash models this key can really use (cached)."""
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED
    _DISCOVERED = []
    try:
        r = requests.get(f"{GEM_BASE}/models", params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
        r.raise_for_status()
        names = []
        for m in r.json().get("models", []):
            n = m.get("name", "").replace("models/", "")
            if ("flash" in n and "generateContent" in m.get("supportedGenerationMethods", [])
                    and not any(x in n for x in ("image", "tts", "live", "audio", "embedding", "robotics"))):
                names.append(n)
        names.sort(key=lambda n: ("omni" in n or "latest" in n, "lite" in n, "preview" in n or "exp" in n, n))
        _DISCOVERED = names
        log("  [llm] models your key can use:", ", ".join(names[:8]) or "(none)")
    except Exception as ex:
        log("  [llm] could not list models:", str(ex)[:150])
    return _DISCOVERED


def _retry_delay(text, default=40):
    """Google sometimes says how long to wait ("retryDelay": "34s"). Cap it so runs stay short."""
    m = re.search(r'retryDelay"?\s*:\s*"?(\d+(?:\.\d+)?)s', text)
    secs = float(m.group(1)) + 2 if m else default
    return min(max(secs, 15), 70)


def _budget_left():
    return GEM_DEADLINE is None or time.time() < GEM_DEADLINE


def _try_model(model, body):
    """Try one model up to 3 times. Returns (status, data): ok | dead | fatal | busy."""
    url = f"{GEM_BASE}/models/{model}:generateContent"
    cur = json.loads(json.dumps(body))
    cur["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}   # faster answers
    waits = [10, 25]
    for attempt in range(3):
        if not _budget_left():
            return "busy", None
        try:
            r = requests.post(url, params={"key": GEMINI_KEY}, json=cur, timeout=60)
            if r.status_code == 400 and "thinkingConfig" in cur["generationConfig"]:
                cur["generationConfig"].pop("thinkingConfig")   # this model dislikes the setting
                r = requests.post(url, params={"key": GEMINI_KEY}, json=cur, timeout=60)
            sc = r.status_code
            if sc in (404, 400):
                log(f"  [llm] model '{model}' rejected ({sc}): {r.text[:150].strip()}")
                return "dead", None
            if sc in (401, 403):
                log(f"  [llm] key problem ({sc}): {r.text[:200].strip()}")
                return "fatal", None
            if sc == 429:
                log(f"  [llm] '{model}' rate-limited (429): {' '.join(r.text.split())[:200]}")
                if re.search(r"per ?day|PerDay", r.text, re.I):
                    log(f"  [llm] daily free quota used up for '{model}' - moving on")
                    return "dead", None
                if attempt < 2:
                    wait = _retry_delay(r.text)
                    log(f"  [llm] waiting {wait:.0f}s for the per-minute limit to reset")
                    time.sleep(wait)
                    continue
                return "busy", None
            if sc in (500, 502, 503, 504):
                log(f"  [llm] '{model}' busy ({sc}), attempt {attempt + 1}/3")
                if attempt < 2:
                    time.sleep(waits[attempt])
                    continue
                return "busy", None
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            try:
                data = json.loads(txt)
            except ValueError:
                log(f"  [llm] '{model}' sent unreadable JSON, attempt {attempt + 1}/3")
                continue
            if isinstance(data, dict):
                data = [data]
            return "ok", [d for d in data if isinstance(d, dict)]
        except requests.exceptions.Timeout:
            log(f"  [llm] '{model}' timed out, attempt {attempt + 1}/3")
        except Exception as ex:
            log(f"  [llm] '{model}' error: {str(ex)[:150]}")
            return "busy", None
    return "busy", None


def llm_score(video, cands):
    """Gemini call scoring up to 3 candidates. Persistent: many models, retries, one cool-down round."""
    global _GOOD_MODEL, _FAILS
    if not GEMINI_KEY or _FAILS >= 99 or not _budget_left():
        return None
    items = [{"id": i, "text": c["text"][:1200]} for i, c in enumerate(cands)]
    prompt = (
        "You are a short-form video editor judging which excerpt of a YouTube video would perform "
        "best as a Facebook/Instagram Reel. Each candidate is a 30-60 second transcript (auto-captions, "
        "no punctuation). Video title: " + video["title"] + ". Channel: " + video.get("channel", "") +
        ". Description start: " + video.get("desc", "").replace("\n", " ") + "\n"
        "Also give 'gameplay' (0-10): how sure you are that this excerpt shows ACTUAL in-game video-game "
        "footage being played (player actions, missions, driving, shooting inside the game). Give 0-3 if it "
        "is real-world footage, news, a reaction, a trailer breakdown, an interview, or just talking.\n"
        "For each candidate give integer scores 0-10: hook (do the first 3 seconds grab attention), "
        "self_contained (makes sense without the rest of the video), payoff (clear punchline/insight/reveal), "
        "retention (keeps viewers watching to the end). Also write 'title' (max 80 chars, curiosity-driven, "
        "honest, no hashtags) and 'hashtags' (5 relevant hashtags starting with #).\n"
        'Return ONLY a JSON array like [{"id":0,"hook":0,"self_contained":0,"payoff":0,"retention":0,"gameplay":0,'
        '"title":"","hashtags":["#a"]}].\nCandidates: ' + json.dumps(items))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}

    for round_no in range(2):                      # round 2 happens after a cool-down
        models = []
        first = CFG.get("gemini_model")
        for m in [_GOOD_MODEL, first] + discover_models() + MODEL_FALLBACKS:
            if m and m not in models and m not in _DEAD:
                models.append(m)
        models = models[:6]
        if not models:
            break
        for model in models:
            if not _budget_left():
                break
            status, data = _try_model(model, body)
            if status == "ok":
                _GOOD_MODEL = model
                log(f"  [llm] using model '{model}'")
                return data
            if status == "fatal":
                _FAILS = 99
                return None
            if status == "dead":
                _DEAD.add(model)
        if round_no == 0 and _budget_left():
            log("  [llm] every model was busy - cooling down 60s, then trying them all again")
            time.sleep(60)
    log("  [llm] no Gemini model worked for this video")
    return None


def final_score(c, llm, has_heat):
    heat, pace = c["heat"] * 100, c["pace"]
    if llm:
        if CFG.get("require_gameplay") and llm.get("gameplay", 0) < 6:
            return 30.0      # not real gameplay: never let it pass the threshold
        l = (0.35 * llm.get("hook", 0) + 0.25 * llm.get("self_contained", 0) +
             0.20 * llm.get("payoff", 0) + 0.20 * llm.get("retention", 0)) * 10
        return 0.45 * l + 0.35 * heat + 0.20 * pace if has_heat else 0.70 * l + 0.30 * pace
    # No AI rating available: be stricter, so unscored clips rarely pass the threshold
    return (0.6 * heat + 0.4 * pace) * 0.8 if has_heat else 0.5 * pace


def choose_clip(state):
    # Phase 1: read captions + build candidate clips for several videos. No Gemini yet (cheap).
    pool, evaluated, tried = [], 0, 0
    for v in find_videos():
        if evaluated >= CFG["videos_to_evaluate"]:
            break
        want_cat = str(CFG.get("only_category", "") or "")
        if want_cat and v.get("cat") != want_cat:
            log(f"[skip] {v['title'][:50]}: not in YouTube category {want_cat} (Gaming is 20)")
            continue
        if tried >= 10:
            break
        if tried:
            time.sleep(3)        # be gentle with YouTube
        tried += 1
        try:
            duration, heat, words = fetch_info(v["id"])
        except Exception as ex:
            log(f"[skip] {v['title'][:50]}: info failed ({str(ex)[:120]})")
            continue
        if len(words) < 50:
            log(f"[skip] {v['title'][:50]}: no usable captions")
            continue
        evaluated += 1
        used = state.get(v["id"], [])
        cands = [c for c in make_candidates(words, heat, duration)
                 if not overlaps(c["s"], c["e"], used)]
        if not cands:
            log(f"[skip] {v['title'][:50]}: no fresh candidate windows")
            continue
        for c in cands:
            c["llm"] = None
            c["video"] = v
            c["score"] = final_score(c, None, bool(heat))
        pool.append((v, cands, bool(heat)))
        log(f"[prepared] {v['title'][:50]} -> {len(cands)} candidate clips")
    if not pool:
        log("No usable videos this cycle. Skipping.")
        return None

    # Phase 2: ask Gemini about only the best few videos, with long pauses between calls.
    pool.sort(key=lambda p: max(c["score"] for c in p[1]), reverse=True)
    global GEM_DEADLINE
    GEM_DEADLINE = time.time() + CFG.get("gemini_budget_minutes", 10) * 60
    n_rate = CFG.get("videos_to_rate", 2)
    pause = CFG.get("gemini_pause_seconds", 25)
    best = None
    for n, (v, cands, has_heat) in enumerate(pool[:n_rate]):
        if n:
            time.sleep(pause)
        top3 = cands[:3]
        llm = llm_score(v, top3) or []
        for i, c in enumerate(top3):
            c["llm"] = next((x for x in llm if x.get("id") == i), None)
            c["score"] = final_score(c, c["llm"], has_heat)
        top = max(top3, key=lambda c: c["score"])
        log(f"[scored] {v['title'][:50]} -> best {top['score']:.0f} "
            f"({top['s']:.0f}-{top['e']:.0f}s, heat={top['heat']:.2f}, pace={top['pace']:.0f})")
        if best is None or top["score"] > best["score"]:
            best = top
    if best and best["score"] >= CFG["min_score"]:
        return best
    log(f"No clip reached the minimum score of {CFG['min_score']}. Skipping this cycle.")
    return None


# ============================ 3. build the reel ============================
def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def download_section(vid, s, e, out):
    out = Path(out)
    out.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--js-runtimes", "node", "--remote-components", "ejs:github",
           "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "--merge-output-format", "mp4", "--download-sections", f"*{s:.2f}-{e:.2f}",
           "--force-keyframes-at-cuts", "-o", str(out), f"https://www.youtube.com/watch?v={vid}"]
    if COOKIES:
        cmd += ["--cookies", str(COOKIES)]
    run(cmd)
    return out


def transcribe(path):
    """Returns (words, detected_language). Audio is decoded with ffmpeg, not PyAV."""
    import numpy as np
    from faster_whisper import WhisperModel
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    model = WhisperModel("base", compute_type="int8")
    segs, info = model.transcribe(audio, word_timestamps=True)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append({"w": w.word.strip(), "t": w.start, "e": w.end})
    return [w for w in words if w["w"]], info.language


def ts(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def make_ass(words, path, per_line=3):
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
        "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        # Primary = highlighted word (yellow), Secondary = not yet spoken (white). Colours are BGR.
        f"Style: K,{CFG['font']},92,&H0000F0FF,&H00FFFFFF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,7,3,2,50,50,520,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = []
    for i in range(0, len(words), per_line):
        g = words[i:i + per_line]
        text = ""
        for j, w in enumerate(g):
            nxt = g[j + 1]["t"] if j + 1 < len(g) else w["e"]
            text += f"{{\\kf{max(1, int(round((nxt - w['t']) * 100)))}}}{w['w'].upper()} "
        lines.append(f"Dialogue: 0,{ts(g[0]['t'])},{ts(g[-1]['e'])},K,,0,0,0,,{text.strip()}")
    Path(path).write_text(head + "\n".join(lines), encoding="utf-8")


def render_reel(src_name, with_subs):
    chain = ("[bg][fg]overlay=(W-w)/2:(H-h)/2" + (",ass=subs.ass" if with_subs else "") +
             ",eq=saturation=1.25:contrast=1.05[v]")
    fc = ("[0:v]split[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
          "[b]scale=1080:-2[fg];" + chain)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src_name, "-t", "60", "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-r", "30",
         "-movflags", "+faststart", "reel.mp4"], cwd=WORK)
    return WORK / "reel.mp4"


def make_caption(clip):
    v, llm = clip["video"], clip.get("llm") or {}
    title = (llm.get("title") or v["title"])[:100]
    topic_tag = "#" + re.sub(r"\W+", "", CFG["topic"].title())
    tags = [t for t in llm.get("hashtags", []) if isinstance(t, str) and t.startswith("#")]
    tags = list(dict.fromkeys(tags + [topic_tag] + CFG["extra_hashtags"]))[:9]
    desc = (f"{title} 🔥\n\nFollow for more {CFG['topic']} content!\n"
            f"🎥 Credit: {v['channel']} - https://youtu.be/{v['id']}\n\n{' '.join(tags)}")
    return title, desc


# ============================ 4. post to Facebook ============================
def post_reel(path, title, desc):
    for name, val in (("FB_PAGE_ID", FB_PAGE_ID), ("FB_PAGE_TOKEN", FB_TOKEN)):
        if not val:
            raise SystemExit(f"{name} is not set")
    r = requests.post(f"{GRAPH}/{FB_PAGE_ID}/video_reels", timeout=60,
                      data=dict(upload_phase="start", access_token=FB_TOKEN))
    r.raise_for_status()
    video_id, upload_url = r.json()["video_id"], r.json()["upload_url"]
    size = path.stat().st_size
    with open(path, "rb") as f:
        u = requests.post(upload_url, data=f, timeout=900, headers={
            "Authorization": f"OAuth {FB_TOKEN}", "offset": "0", "file_size": str(size)})
    u.raise_for_status()
    fin = requests.post(f"{GRAPH}/{FB_PAGE_ID}/video_reels", timeout=120, data=dict(
        upload_phase="finish", video_id=video_id, video_state="PUBLISHED",
        title=title, description=desc, access_token=FB_TOKEN))
    fin.raise_for_status()
    return video_id, fin.json()


# ============================ entry points ============================
def run_once(dry):
    state = load_state()
    clip = choose_clip(state)
    if not clip:
        return
    v = clip["video"]
    log(f"[chosen] {v['title']} | {clip['s']:.1f}-{clip['e']:.1f}s | score {clip['score']:.0f}")
    src = download_section(v["id"], clip["s"], clip["e"], WORK / "source.mp4")
    words, spoken = transcribe(src)
    if spoken != CFG["language"]:
        log(f"[skip] clip is spoken in '{spoken}', not '{CFG['language']}'. Skipping this cycle.")
        return
    if words:
        make_ass(words, WORK / "subs.ass")
    reel = render_reel("source.mp4", bool(words))
    title, desc = make_caption(clip)
    log("[caption]\n" + desc)
    if dry:
        log(f"[dry-run] reel saved at {reel}; nothing posted or recorded.")
        return
    vid_fb, res = post_reel(reel, title, desc)
    log("[posted]", vid_fb, res)
    state.setdefault(v["id"], []).append([round(clip["s"], 2), round(clip["e"], 2)])
    save_state(state)


def selftest():
    log("cookies:", "provided" if COOKIES else "none")
    d, heat, words = fetch_info("dQw4w9WgXcQ")
    log(f"info OK: duration={d}s heatmap_points={len(heat)} caption_words={len(words)}")
    out = download_section("dQw4w9WgXcQ", 0, 8, WORK / "selftest.mp4")
    log("download OK:", out, out.stat().st_size, "bytes")
    log("SELFTEST PASSED - YouTube downloads work from this machine.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        run_once(a.dry_run)
