#!/usr/bin/env python3
"""AeroSpeak ATC — AI ATC for console flight simmers.
Hold-to-talk web radio: Gemini hears you, replies as a controller,
Charlie (ElevenLabs) speaks with VHF radio degradation."""

import io, json, os, re, uuid, wave
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
from scipy.signal import butter, sosfiltfilt, lfilter
from starlette.applications import Starlette
from starlette.responses import JSONResponse, FileResponse, Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

BASE = Path(__file__).parent
(BASE / "audio").mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = BASE / "settings.json"

def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

SETTINGS = load_settings()
_ATIS_ROLL = {}  # airports that have had ATIS generated this session (letter roll)

SIMBRIEF_ID  = SETTINGS.get("simbrief_id") or os.environ.get("SIMBRIEF_ID", "")
ATC_VOICE    = SETTINGS.get("voice") or os.environ.get("ATC_VOICE", "IKne3meq5aSn9XLyUdCD")

GEMINI_KEY   = os.environ.get("GEMINI_KEY", "")
ELEVEN_KEY   = os.environ.get("ELEVEN_KEY", "")
PLAN = simbrief_summary() if SIMBRIEF_ID else "(no SimBrief ID configured)"

# ---------------------------------------------------------------- airport data
# Packed OurAirports dataset (US/CA/global medium+large airports, runways)
import json as _json
AIRPORT_FILE = BASE / "data" / "airports.json"
APTS = {}
if AIRPORT_FILE.exists():
    try:
        APTS = _json.loads(AIRPORT_FILE.read_text())
    except Exception:
        APTS = {}

METAR_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=json&taf=false&hours=1"

import urllib.parse as _up


def fetch_metar(icao):
    """Fetch a METAR for icao; returns dict with raw + parsed bits."""
    try:
        url = METAR_URL.format(icao=_up.quote(icao))
        req = Request(url, headers={"User-Agent": "AeroSpeakATC/0.1"})
        with urlopen(req, timeout=10) as r:
            arr = _json.loads(r.read().decode())
        if not arr:
            return None
        raw = arr[0].get("rawOb") or arr[0].get("raw_text") or ""
        wind = arr[0].get("wdir"), arr[0].get("wspd"), arr[0].get("wgst")
        alt = arr[0].get("altim")
        vis = arr[0].get("visib")
        ceil = arr[0].get("cloudbase_feet_agl")
        return {"raw": raw, "wind_dir": wind[0], "wind_spd": wind[1], "wind_gust": wind[2],
                "altim": alt, "visib": vis, "ceil": ceil}
    except Exception:
        return None


def active_runways(arpt, wind_dir):
    """Pick best runway(s) for a wind direction: headwind at the threshold."""
    rws = arpt.get("r", [])
    if not rws:
        return []
    if not wind_dir:
        # no wind data: prefer the longest
        best = sorted(rws, key=lambda r: -r[2])[:2]
        return [r[0] for r in best]
    scored = []
    for ident, hdg, ln, surf in rws:
        diff = abs((hdg - wind_dir + 180) % 360 - 180)  # headwind angle
        scored.append((diff, ln, ident))
    scored.sort(key=lambda x: (x[0], -x[1]))
    return [x[2] for x in scored[:2]]


def airport_context(icao):
    """Build a compact ATC context string for a given airport."""
    if not icao:
        return "", None
    arpt = APTS.get(icao.upper())
    if not arpt:
        return f"(airport {icao.upper()} not in local dataset)", {"icao": icao.upper()}
    metar = fetch_metar(icao.upper())
    parts = [
        f"AIRPORT: {arpt.get('n')} ({icao.upper()}), {arpt.get('m')}, elev {arpt.get('e')} ft",
    ]
    if arpt.get("r"):
        lines = ", ".join(f"{i}/{l}" for i, h, l, s in arpt["r"][:12])
        parts.append(f"RUNWAYS: {lines}")
    if metar and metar.get("raw"):
        parts.append(f"METAR: {metar['raw']}")
        wdir = metar.get("wind_dir")
        if wdir:
            act = active_runways(arpt, wdir)
            if act:
                parts.append(f"ACTIVE RUNWAYS (wind {wdir}/{metar.get('wind_spd')}kt): {', '.join(act)}")
    ctx = "\n".join(parts)
    return ctx, {"icao": icao.upper(), "metar": metar}

VOICE_ID     = SETTINGS.get("voice") or os.environ.get("ATC_VOICE", "IKne3meq5aSn9XLyUdCD")  # Charlie
CALLSIGN     = SETTINGS.get("callsign", "")
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_turbo_v2_5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Lightweight model for audio one-call (separate free-tier quota pool, ~1s).
GEMINI_LITE = os.environ.get("GEMINI_LITE", "gemini-flash-lite-latest")

def simbrief_summary():
    try:
        req = Request(f"https://www.simbrief.com/api/xml.fetcher.php?userid={SIMBRIEF_ID}",
                      headers={"User-Agent": "AeroSpeakATC/0.1"})
        with urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", "ignore")
        def tog(name):
            m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
            return m.group(1).strip() if m else ""
        return (f"PILOT'S FILED FLIGHT PLAN: callsign {tog('callsign')}, "
                f"aircraft {tog('icaocode')}, {tog('orig_icao')} -> {tog('dest_icao')}, "
                f"route {tog('route')}, cruise {tog('cruise_altitude')} ft.")
    except Exception as e:
        return f"(no live SimBrief plan: {e})"

PLAN = simbrief_summary() if SIMBRIEF_ID else "(no SimBrief ID configured)"

def current_airport():
    """Return ICAO for the active airport: settings airport, else SimBrief origin."""
    a = (SETTINGS.get("airport") or "").strip().upper()
    if a:
        return a
    m = re.search(r"([A-Z]{4})", PLAN)
    if m:
        return m.group(1)
    return ""


def atc_context_line():
    """Build the airport+ATIS context string to inject into Gemini prompts."""
    icao = current_airport()
    if not icao:
        return ""
    ctx, _ = airport_context(icao)
    return f"\nCURRENT AIRPORT CONTEXT:\n{ctx}"


SYSTEM_PROMPT = f"""You are an AI air traffic controller at a US towered airport, speaking on VHF radio.
You communicate in standard, concise ATC phraseology: callsign first, then the instruction, then frequency.
You ONLY handle pre-departure ops: ATIS, clearance delivery, ground (pushback and taxi), and tower (takeoff).
After takeoff clearance, end with a quick departure handoff like 'Contact departure on one two six point one five, good day.'
Never give radar vectors, approaches, or beyond-departure instruction. Keep replies under 45 words.
Read numbers as spoken ATC (e.g. 'two five zero', 'flight level two zero zero', 'squawk one two three four').
If the pilot says something non-aviation, respond in character with a sharp radio-style quip and steer back to procedure.
CONTEXT: {PLAN}"""

HISTORY: list[dict] = []

# ---------------------------------------------------------------- audio
CLICK_OPEN = None
CLICK_CLOSE = None

def _load_click(name):
    p = BASE / "static" / name
    return load_wav_bytes(p.read_bytes()) if p.exists() else None

def load_wav_bytes(b: bytes, sr=44100):
    with wave.open(io.BytesIO(b)) as w:
        got = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
    if got != sr and len(a) > 1:
        x = np.linspace(0, 1, len(a))
        a = np.interp(np.linspace(0, 1, int(len(a) * sr / got)), x, a)
    return a

def encode_wav(a, sr=44100):
    amax = np.max(np.abs(a)) + 1e-12
    if amax > 0.96:
        a = a / amax * 0.96
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((a * 32767).astype(np.int16).tobytes())
    return buf.getvalue()

def bandpass(x, sr, lo, hi, order=4):
    return sosfiltfilt(butter(order, [max(lo,1)/(sr/2), min(hi, (sr/2)-1)/(sr/2)], "band", output="sos"), x)

def compress(x, sr, threshold=0.22, ratio=10., rel=0.15, makeup=3.2):
    env = np.abs(x)
    env = np.maximum(env, np.convolve(env, np.ones(int(sr*0.005))/int(sr*0.005), mode="same"))
    aa = np.exp(-1/(sr*rel))
    env = lfilter([1-aa], [1, -aa], env)
    over = env - threshold
    g = np.ones_like(env)
    idx = over > 0
    g[idx] = threshold/over[idx]*(1-1/10.0)+(1/10.0)
    g = np.clip(g, 0, 1)
    return x * g * makeup

def radio_fx(pcm, sr):
    v = bandpass(pcm, sr, 250, 3000, 3)
    v = compress(v, sr, 0.16, 10, makeup=3.2)
    v = compress(v, sr, 0.05, 4, makeup=2.4)
    v = np.tanh(v*1.8)/np.tanh(1.8)
    rng = np.random.default_rng(7)
    hiss = rng.normal(0, 1, len(v))
    hiss = bandpass(hiss, sr, 1500, 5000, 2)
    hiss *= 0.10
    env = np.abs(v)
    env = np.convolve(env, np.ones(int(sr*0.05))/int(sr*0.05), mode="same")
    env = env/(np.max(env)+1e-12)
    duck = 1.0 - np.clip(env*0.85, 0, 0.88)
    eff = hiss * duck
    pop  = _load_click("click-open.wav")[:int(0.07*sr)] if _load_click("click-open.wav") is not None else np.zeros(int(0.07*sr))
    popc = _load_click("click-close.wav")[:int(0.12*sr)] if _load_click("click-close.wav") is not None else np.zeros(int(0.12*sr))
    off = int(0.12*sr)
    mix = np.zeros(off + len(v) + len(popc) + int(0.6*sr))
    mix[off-len(pop):off] += pop*1.5
    mix[off:off+len(v)] += v
    mix[off+len(v):off+len(v)+len(popc)] += popc*1.4
    mix[:len(eff)] += eff
    mix = np.tanh(mix*1.4)/np.tanh(1.4)
    return mix

# ---------------------------------------------------------------- gemini
def gemini_call(contents, model=None, retries=3):
    """Send a full contents array (list of {role, parts}) to Gemini and return text.
    Uses GEMINI_MODEL by default, or an explicit model. Retries transient 429/503
    errors (free tier is flaky) and returns a safe fallback instead of crashing
    on filtered/empty responses."""
    import time
    model = model or GEMINI_MODEL
    last_err = None
    for attempt in range(retries):
        payload = json.dumps({"contents": contents}).encode()
        req = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=payload, headers={"Content-Type": "application/json",
                                   "x-goog-api-key": GEMINI_KEY})
        try:
            with urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode())
            try:
                txt = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt:
                    return txt
            except (KeyError, IndexError, TypeError):
                pass  # empty/filtered response -> fall through to retry
            last_err = "empty response"
        except HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503):
                raise
        except Exception as e:
            last_err = repr(e)
        time.sleep(1.5 * (attempt + 1))
    return "Say again?"  # safe fallback

def gemini_transcribe(audio_bytes, mime):
    """Transcribe pilot's transmission. Convert any input to 16k mono WAV first
    (Safari sends m4a/mp4 that Gemini's free API sometimes rejects), then send."""
    import base64, subprocess, tempfile
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as tf:
        tf.write(audio_bytes); tin = tf.name
    tout = tin + ".wav"
    try:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", tin,
                        "-ar", "16000", "-ac", "1", tout], check=True)
        wav = open(tout, "rb").read()
    finally:
        for p in (tin, tout):
            try: os.remove(p)
            except OSError: pass
    parts = [{"text": "Transcribe the pilot's radio transmission verbatim. Output only the spoken words."},
             {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}]
    return gemini_call([{"role": "user", "parts": parts}])

def gemini_reply(user_text):
    parts = []
    for h in HISTORY[-8:]:
        parts.append({"role": h["role"], "parts": [{"text": h["parts"][0]["text"]}]})
    parts.append({"role": "user", "parts": [{"text": user_text + atc_context_line()}]})
    parts.insert(0, {"role": "user", "parts": [{"text": SYSTEM_PROMPT}]})
    parts.insert(1, {"role": "model", "parts": [{"text": "Understood. Standing by on frequency."}]})
    reply = gemini_call(parts)
    HISTORY.append({"role": "user", "parts": [{"text": user_text}]})
    HISTORY.append({"role": "model", "parts": [{"text": reply}]})
    return reply

def gemini_respond_audio(audio_bytes, mime="audio/wav"):
    """One-call path: transcribe the audio AND reply as ATC in a single
    generateContent request. Halves free-tier quota usage (2 calls -> 1) and
    removes the fragile 'Say again?' intermediate."""
    import base64, subprocess, tempfile
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as tf:
        tf.write(audio_bytes); tin = tf.name
    tout = tin + ".wav"
    try:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", tin,
                        "-ar", "16000", "-ac", "1", tout], check=True)
        wav = open(tout, "rb").read()
    finally:
        for p in (tin, tout):
            try: os.remove(p)
            except OSError: pass
    b64 = base64.b64encode(wav).decode()
    parts = [
        {"text": "You are the ATC controller. Transcribe the pilot's radio transmission, then reply as a realistic ATC controller with proper phraseology. Use this format:\nTRANSCRIPT: <exact words>\nREPLY: <your ATC reply>" + atc_context_line()},
        {"inline_data": {"mime_type": "audio/wav", "data": b64}}
    ]
    return gemini_call([{"role": "user", "parts": parts}], model=GEMINI_LITE)

# ---------------------------------------------------------------- eleven
def eleven_speak(text):
    payload = json.dumps({
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.8,
                           "style": 0.0, "use_speaker_boost": True}
    }).encode()
    req = Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
                  data=payload, headers={"xi-api-key": ELEVEN_KEY,
                                         "Content-Type": "application/json"})
    with urlopen(req, timeout=60) as r:
        mp3 = r.read()
    tmp = BASE / "audio" / f"{uuid.uuid4().hex}.mp3"
    tmp.write_bytes(mp3)
    return tmp

# ---------------------------------------------------------------- routes
async def index(request):
    return FileResponse(BASE / "static" / "index.html")

async def api_chat(request):
    import base64
    ctype = request.headers.get("content-type", "")
    raw_text = None
    audio_bytes = None
    mime = None
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("audio")
        txt_field = (form.get("text") or "").strip()
        if upload is not None:
            audio_bytes = await upload.read()
            mime = upload.content_type or "audio/webm"
        elif txt_field:
            raw_text = txt_field
        if audio_bytes is None and raw_text is None:
            return JSONResponse({"error": "no audio field"}, status_code=400)
    else:
        body = await request.json()
        raw_text = (body.get("text") or "").strip()

    if audio_bytes is not None:
        # One-call path: transcribe + reply in a single Gemini request.
        try:
            combined = gemini_respond_audio(audio_bytes, mime)
        except Exception as e:
            return JSONResponse({"error": "transcribe_failed", "detail": str(e)}, status_code=502)
        if not combined or "say again" in combined.lower()[:80]:
            return JSONResponse({"error": "transcribe_failed", "detail": "empty capture"}, status_code=502)
        # Parse the TRANSCRIPT / REPLY block
        text = combined
        reply = combined
        m = re.search(r"REPLY:\s*(.+)", combined, re.S | re.I)
        if m:
            reply = m.group(1).strip()
        m = re.search(r"TRANSCRIPT:\s*(.+?)(?:\nREPLY:|$)", combined, re.S | re.I)
        if m:
            text = m.group(1).strip()
        if not reply or reply.lower() == "say again?":
            return JSONResponse({"error": "brain_unavailable", "detail": "empty reply"}, status_code=502)
    else:
        raw_text = raw_text or ""
        if not raw_text:
            return JSONResponse({"error": "empty"}, status_code=400)
        try:
            reply = gemini_reply(raw_text)
        except Exception as e:
            return JSONResponse({"error": "brain_unavailable", "detail": str(e)}, status_code=502)
        if not reply or reply.lower() == "say again?":
            return JSONResponse({"error": "brain_unavailable", "detail": "empty reply"}, status_code=502)
        text = raw_text

    try:
        mp3 = eleven_speak(reply)
        wav = mp3.with_suffix(".wav")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", "44100", "-ac", "1", str(wav)], check=True)
        pcm = load_wav_bytes(wav.read_bytes())
        fx = encode_wav(radio_fx(pcm, 44100))
        out = BASE / "audio" / f"{uuid.uuid4().hex}.wav"
        out.write_bytes(fx)
    except Exception as e:
        # TTS failed (quota/network): still show the reply text, no audio.
        return JSONResponse({"text": reply, "audio": None})

    return JSONResponse({"text": reply, "audio": f"/audio/{out.name}"})

async def audio(request):
    name = request.path_params["name"]
    path = BASE / "audio" / name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/wav")

async def api_settings(request):
    if request.method == "GET":
        return JSONResponse({"simbrief_id": SETTINGS.get("simbrief_id", ""),
                            "callsign": SETTINGS.get("callsign", ""),
                            "voice": SETTINGS.get("voice", ""),
                            "airport": SETTINGS.get("airport", "")})
    body = await request.json()
    if "simbrief_id" in body:
        SETTINGS["simbrief_id"] = str(body["simbrief_id"]).strip()
    if "callsign" in body:
        SETTINGS["callsign"] = str(body["callsign"]).strip()
    if "voice" in body:
        SETTINGS["voice"] = str(body["voice"]).strip()
    if "airport" in body:
        SETTINGS["airport"] = str(body["airport"]).strip().upper()
    save_settings(SETTINGS)
    return JSONResponse({"ok": True, "settings": SETTINGS})

async def api_airports(request):
    """Search airports by ICAO/name/city. Returns compact list."""
    q = (request.query_params.get("q") or "").strip().upper()
    if not q:
        return JSONResponse({"airports": []})
    out = []
    for icao, a in APTS.items():
        if q in icao or q in (a.get("n") or "").upper() or q in (a.get("m") or "").upper():
            out.append({"icao": icao, "name": a.get("n"), "city": a.get("m"), "country": a.get("c"),
                        "freqs": a.get("f", {})})
            if len(out) >= 12:
                break
    return JSONResponse({"airports": out})


async def api_frequencies(request):
    icao = (request.path_params["icao"] or "").upper()
    a = APTS.get(icao)
    if not a:
        return JSONResponse({"error": "airport not found"}, status_code=404)
    return JSONResponse({"icao": icao, "freqs": a.get("f", {}), "runways": a.get("r", [])})


async def api_atis(request):
    """Real ATIS from live METAR + airport data."""
    icao = (request.path_params["icao"] or "").upper()
    a = APTS.get(icao)
    if not a:
        return JSONResponse({"error": "airport not found"}, status_code=404)
    metar = fetch_metar(icao)
    if not metar or not metar.get("raw"):
        return JSONResponse({"error": "no metar", "icao": icao}, status_code=404)
    wdir = metar.get("wind_dir") or 0
    wspd = metar.get("wind_spd") or 0
    wgst = metar.get("wind_gust")
    act = active_runways(a, wdir)
    # Build realistic ATIS text (letter starts at A per session)
    letter = chr(65 + (_ATIS_ROLL.get(icao, 0) % 26))
    _ATIS_ROLL[icao] = _ATIS_ROLL.get(icao, 0) + 1
    wind_str = f"wind {wdir:03d} at {wspd}" + (f" gusting {wgst}" if wgst else "")
    alt = metar.get("altim")
    # aviationweather.gov returns altim in hPa; ATIS speaks inHg (29.92)
    try:
        alt_inhg = float(alt) / 33.8639
        alt_str = f"altimeter {alt_inhg:.2f}"
    except (TypeError, ValueError):
        alt_str = "altimeter missing"
    ceil = metar.get("ceil")
    vis = metar.get("visib")
    _v = str(vis or "")
    if _v.endswith("+"):
        vis_str = f"visibility {_v[:-1]} or greater sm"
    else:
        vis_str = f"visibility {_v} sm" if vis else "visibility missing"
    parts = [
        f"THIS IS {icao} ATIS INFORMATION {letter}",
        f"DEPARTURE RUNWAY {act[0] if act else 'XX'}.",
        wind_str.upper(),
        vis_str.upper(),
        (f"CEILING {ceil} HUNDRED" if ceil else "CAVU"),
        alt_str,
    ]
    atis_text = " ".join(parts)
    audio_url = None
    try:
        mp3 = eleven_speak(atis_text)
        wav = mp3.with_suffix(".wav")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", "44100", "-ac", "1", str(wav)], check=True)
        pcm = load_wav_bytes(wav.read_bytes())
        fx = encode_wav(radio_fx(pcm, 44100))
        out = BASE / "audio" / f"{uuid.uuid4().hex}.wav"
        out.write_bytes(fx)
        audio_url = f"/audio/{out.name}"
    except Exception:
        audio_url = None
    return JSONResponse({"icao": icao, "atis": atis_text, "metar": metar.get("raw"), "audio": audio_url})


routes = [
    Route("/", index),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/settings", api_settings, methods=["GET", "POST"]),
    Route("/api/airports", api_airports),
    Route("/api/frequencies/{icao}", api_frequencies),
    Route("/api/atis/{icao}", api_atis),
    Route("/audio/{name}", audio),
    Mount("/static", StaticFiles(directory=str(BASE / "static"))),
]
app = Starlette(routes=routes)
application = app
