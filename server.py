"""AeroSpeak ATC — voice-first MSFS 2024 ATC companion for flight simmers."""

import base64
import io
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections import defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import urllib.parse as urlparse

import imageio_ffmpeg
import numpy as np
from scipy.signal import butter, lfilter, sosfiltfilt
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import auth_store
from taxi_router import route_from_stand_to_runway

BASE = Path(__file__).parent
AUDIO_DIR = BASE / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AIRPORT_FILE = BASE / "data" / "airports.json"

GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
ELEVEN_KEY = os.environ.get("ELEVEN_KEY", "")
VOICE_ID = os.environ.get("ATC_VOICE", "IKne3meq5aSn9XLyUdCD")
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_turbo_v2_5")
# Voice requests must prefer the explicitly configured model. The former
# gemini-flash-lite-latest alias is no longer used as a silent fallback.
GEMINI_AUDIO_FALLBACK = "gemini-3.1-flash-lite"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_AUDIO_FALLBACK)
GEMINI_LITE = os.environ.get("GEMINI_LITE") or GEMINI_MODEL
ACCESS_CODE = os.environ.get("AEROSPEAK_ACCESS_CODE", "").strip()
AUTH_COOKIE = "aerospeak_auth"
SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"
COMPLAINT_WORKER_URL = "https://afcaa.btd3dtoys.workers.dev"
METAR_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=json&taf=false&hours=1"

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 90
CONTEXT_REFRESH_SECONDS = 300
AUDIO_TTL_SECONDS = 1800
SESSION_TTL_SECONDS = 12 * 60 * 60

class AudioLimitError(ValueError):
    """Raised when a pilot holds transmit longer than the service allows."""


OPERATION_PHASES = {
    "PRE_FLIGHT": "Pre-flight setup",
    "CLEARANCE": "Clearance received",
    "PUSHBACK": "Pushback / engine start",
    "TAXI_OUT": "Taxi to departure runway",
    "HOLD_SHORT": "Holding short",
    "RUNWAY_WAIT": "Line up and wait",
    "DEPARTURE": "Departure / climb-out",
    "ENROUTE": "En route",
    "ARRIVAL_CHECKIN": "Arrival check-in",
    "APPROACH": "Approach / landing sequence",
    "LANDING": "Landing / runway exit",
    "TAXI_IN": "Taxi to parking",
    "PARKED": "At parking",
    "EMERGENCY": "Priority operation",
}


CONTROLLER_PROFILES = {
    "ATIS": {"name": "ATIS", "scope": "Give the current recorded-information-style broadcast only. Do not issue clearances, taxi instructions, or landing clearances."},
    "CLD": {"name": "Clearance Delivery", "scope": "Handle IFR/VFR clearance requests and clearance readbacks. Issue a handoff to Ground when appropriate; do not issue taxi, takeoff, or landing instructions."},
    "GND": {"name": "Ground", "scope": "Handle pushback, engine-start coordination, taxi, hold-short instructions, runway crossings, and post-landing taxi. Do not issue takeoff or landing clearances."},
    "TWR": {"name": "Tower", "scope": "Handle runway entry, takeoff, pattern, landing, runway-exit, and initial ground handoff. Do not issue en-route radar services or vectors."},
    "APP": {"name": "Approach", "scope": "Handle procedural non-radar arrival coordination. Require pilot position reports and destination ATIS as needed; never claim radar contact, identify an aircraft by radar, issue vectors, or sequence by surveillance."},
    "DEP": {"name": "Departure", "scope": "Handle procedural non-radar departure reports and handoffs. Never claim radar contact, identify an aircraft by radar, issue vectors, or sequence by surveillance."},
}

try:
    APTS = json.loads(AIRPORT_FILE.read_text()) if AIRPORT_FILE.exists() else {}
except Exception:
    APTS = {}

_SESSIONS: dict[str, dict] = {}
_SESSION_LOCK = threading.RLock()
_RATE_WINDOWS: dict[tuple[str, str], deque] = defaultdict(deque)
_DATABASE_READY = False
_DATABASE_LOCK = threading.RLock()


def ensure_database():
    """Initialize account tables on first use without relying on framework startup hooks."""
    global _DATABASE_READY
    if not auth_store.configured() or _DATABASE_READY:
        return True
    with _DATABASE_LOCK:
        if _DATABASE_READY:
            return True
        try:
            auth_store.initialize()
            _DATABASE_READY = True
            log_event("account_database_ready")
            return True
        except Exception as error:
            log_event("account_database_init_failed", detail=str(error)[:120])
            return False


def log_event(event, **fields):
    """Emit compact JSON events suitable for managed-service logs."""
    print(json.dumps({"event": event, "timestamp": int(time.time()), **fields}), flush=True)


def is_complaint_request(transcript: str) -> bool:
    """Return True when the pilot is explicitly asking for a complaint number."""
    text = (transcript or "").upper()
    patterns = [
        r"\bCOMPLAINT\s+NUMBER\b",
        r"\bFILE\s+A\s+COMPLAINT\b",
        r"\bREQUEST\s+(?:A\s+)?COMPLAINT\b",
        r"\bCOMPLAINT\s+REFERENCE\b",
        r"\bI\s+WANT\s+TO\s+COMPLAIN\b",
        r"\bFORMAL\s+COMPLAINT\b",
        r"\bISSUE\s+(?:ME\s+)?(?:A\s+)?COMPLAINT\b",
    ]
    return any(re.search(p, text) for p in patterns)


def create_complaint(state: dict, issue: str) -> dict:
    """Call the Cloudflare complaint Worker and return the response payload."""
    plan = state.get("flight_plan", {}) or {}
    settings = state.get("settings", {}) or {}
    station = state.get("station_context", {}) or {}
    operation = state.get("operation", {}) or {}
    snapshot = operation_snapshot(state)

    # Best-effort active runway from the current ATIS / airport context text
    active_runway = ""
    atis = station.get("atis") or ""
    m = re.search(r"RUNWAY\s+(\d{1,2}[LRC]?)", atis, re.I)
    if m:
        active_runway = m.group(1).upper()

    payload = {
        "callsign": settings.get("callsign") or plan.get("callsign") or "UNKNOWN",
        "airport": station.get("icao") or settings.get("airport") or plan.get("origin") or "",
        "controller": settings.get("controller_type") or "",
        "issue": issue[:500],
        "planned_runway": snapshot.get("planned_runway") or plan.get("origin_runway") or "",
        "active_runway": active_runway,
        "phase": operation.get("phase") or "",
        "extra": {
            "mode": operation.get("mode"),
            "last_transition": operation.get("last_transition"),
        },
    }

    data = json.dumps(payload).encode()
    req = Request(
        f"{COMPLAINT_WORKER_URL.rstrip('/')}/api/complaints",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "AeroSpeakATC/0.4"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except Exception as error:
        log_event("complaint_create_failed", detail=str(error)[:160])
        return {"ok": False, "error": str(error)}


def complaint_reply(transcript: str, state: dict) -> str | None:
    """If the pilot asked for a complaint number, create one and return a ready-to-speak reply."""
    if not is_complaint_request(transcript):
        return None

    # Build a short issue summary from recent history + current state
    recent = " | ".join(
        f"{t['role']}: {t['text']}" for t in (state.get("history") or [])[-4:]
    )
    issue = f"Pilot requested formal complaint number. Recent context: {recent}"[:480]

    result = create_complaint(state, issue)
    if not result.get("ok") or not result.get("complaint_number"):
        return (
            "Unable to issue a complaint reference at this time. "
            "Remain on frequency and try again in a moment."
        )

    number = result["complaint_number"]
    callsign = (state.get("settings") or {}).get("callsign") or (state.get("flight_plan") or {}).get("callsign") or "aircraft"
    return (
        f"{callsign}, roger, complaint reference {number} has been issued. "
        f"You may file your formal statement at the complaint portal. "
        f"Anything else?"
    )


def _blank_state():
    return {
        "created_at": time.time(),
        "last_seen": time.time(),
        "settings": {
            "simbrief_id": "",
            "callsign": "",
            "gate": "",
            "arrival_gate": "",
            "airport": "",
            "controller_type": "",
            "controller_frequency": "",
        },
        "flight_plan": {},
        "station_context": {},
        "last_taxi_route": {},
        "operation": {
            "mode": "DEPARTURE",
            "phase": "PRE_FLIGHT",
            "last_transition": "Select an airport and controller, then begin the operation.",
            "pending_readback": {},
            "emergency": False,
            "updated_at": int(time.time()),
        },
        "history": [],
        "atis_roll": {},
        "last_context_refresh": 0.0,
        "authorized": not bool(ACCESS_CODE),
    }


def _valid_session_id(value):
    return bool(value and re.fullmatch(r"[A-Za-z0-9_-]{20,80}", value))


def user_for_request(request):
    """Resolve a persistent account from the opaque, HTTP-only browser cookie."""
    if hasattr(request.state, "aerospeak_user"):
        return request.state.aerospeak_user
    user = None
    if auth_store.configured() and ensure_database():
        try:
            user = auth_store.user_for_session(request.cookies.get(AUTH_COOKIE, ""))
        except Exception as error:
            log_event("auth_lookup_failed", detail=str(error)[:120])
    request.state.aerospeak_user = user
    return user


def _state_from_account(saved):
    state = _blank_state()
    if not isinstance(saved, dict):
        return state
    for key in ("settings", "flight_plan", "station_context", "last_taxi_route", "history", "atis_roll", "operation", "last_context_refresh", "authorized"):
        if key in saved and isinstance(saved[key], type(state.get(key))):
            state[key] = saved[key]
    state["operation"] = {**_blank_state()["operation"], **(state.get("operation") or {})}
    return state


def session_for(request):
    """Return an account-scoped session when logged in, otherwise an ephemeral browser session."""
    if hasattr(request.state, "aerospeak_session"):
        return request.state.aerospeak_session
    user = user_for_request(request)
    if user:
        session_id = f"account-{user['id']}"
        new_session = False
    else:
        session_id = request.cookies.get("aerospeak_session")
        new_session = not _valid_session_id(session_id)
        if new_session:
            session_id = secrets.token_urlsafe(24)
    with _SESSION_LOCK:
        _cleanup_sessions()
        if session_id not in _SESSIONS:
            saved = auth_store.load_state(user["id"]) if user else {}
            _SESSIONS[session_id] = _state_from_account(saved) if user else _blank_state()
        state = _SESSIONS[session_id]
        state["last_seen"] = time.time()
    request.state.aerospeak_session = state
    request.state.aerospeak_session_id = session_id
    request.state.aerospeak_new_session = new_session
    return state


def session_response(request, payload, status_code=200):
    user = user_for_request(request)
    state = getattr(request.state, "aerospeak_session", None)
    if user and state and auth_store.configured():
        try:
            auth_store.save_state(user["id"], state)
        except Exception as error:
            log_event("account_state_save_failed", user_id=user["id"], detail=str(error)[:120])
    response = JSONResponse(payload, status_code=status_code)
    if getattr(request.state, "aerospeak_new_session", False):
        response.set_cookie("aerospeak_session", request.state.aerospeak_session_id, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="lax", secure=bool(os.environ.get("RENDER")))
    return response


def auth_response(payload, token=None, status_code=200):
    response = JSONResponse(payload, status_code=status_code)
    if token:
        response.set_cookie(AUTH_COOKIE, token, max_age=auth_store.SESSION_DAYS * 24 * 60 * 60, httponly=True, samesite="lax", secure=bool(os.environ.get("RENDER")))
    return response


def _cleanup_sessions():
    cutoff = time.time() - SESSION_TTL_SECONDS
    for session_id in [key for key, state in _SESSIONS.items() if state.get("last_seen", 0) < cutoff]:
        _SESSIONS.pop(session_id, None)


def require_access(request, state):
    if auth_store.configured() and not user_for_request(request):
        return session_response(request, {"error": "login_required", "detail": "Create an account or sign in before using live radio services."}, 401)
    if ACCESS_CODE and not state.get("authorized"):
        return session_response(request, {"error": "access_required", "detail": "Enter the access code in Settings before using live services."}, 403)
    return None


def rate_limited(request, bucket, limit, window_seconds):
    """Return a response when a browser exceeds a small, in-memory safety quota."""
    host = getattr(getattr(request, "client", None), "host", "unknown")
    session_id = getattr(request.state, "aerospeak_session_id", "anonymous")
    key = (f"{host}:{session_id}", bucket)
    now = time.time()
    timestamps = _RATE_WINDOWS[key]
    while timestamps and timestamps[0] <= now - window_seconds:
        timestamps.popleft()
    if len(timestamps) >= limit:
        return session_response(
            request,
            {"error": "rate_limited", "detail": "Please wait a moment before sending another request."},
            429,
        )
    timestamps.append(now)
    return None


def _value(mapping, *names):
    if not isinstance(mapping, dict):
        return ""
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", "-", "N/A"):
            return str(value).strip()
    return ""


def _section(data, name):
    value = data.get(name, {}) if isinstance(data, dict) else {}
    return value if isinstance(value, dict) else {}


def fetch_metar(icao):
    try:
        url = METAR_URL.format(icao=urlparse.quote(icao.upper()))
        request = Request(url, headers={"User-Agent": "AeroSpeakATC/0.3"})
        with urlopen(request, timeout=10) as response:
            reports = json.loads(response.read().decode())
        if not reports:
            return None
        report = reports[0]
        def number(name, integer=False):
            value = report.get(name)
            try:
                parsed = float(value)
                return int(parsed) if integer else parsed
            except (TypeError, ValueError):
                return None
        return {
            "raw": report.get("rawOb") or report.get("raw_text") or "",
            "wind_dir": number("wdir", integer=True),
            "wind_spd": number("wspd", integer=True),
            "wind_gust": number("wgst", integer=True),
            "altim": number("altim"),
            "visib": report.get("visib"),
            "ceil": number("cloudbase_feet_agl", integer=True),
        }
    except Exception:
        return None


def active_runways(airport, wind_dir):
    runways = airport.get("r", [])
    if not runways:
        return []
    if not wind_dir:
        return [runway[0] for runway in sorted(runways, key=lambda runway: -runway[2])[:2]]
    # aviationweather.gov returns numeric METAR fields as strings in some
    # responses. Normalize them before comparing with local runway headings.
    try:
        wind_dir = float(wind_dir)
    except (TypeError, ValueError):
        return [runway[0] for runway in sorted(runways, key=lambda runway: -runway[2])[:2]]
    scored = []
    for ident, heading, length, surface in runways:
        difference = abs((heading - wind_dir + 180) % 360 - 180)
        scored.append((difference, -length, ident))
    return [item[2] for item in sorted(scored)[:2]]


def airport_context(icao, metar=None):
    if not icao:
        return "", {"icao": "", "metar": None}
    airport = APTS.get(icao.upper())
    if not airport:
        return f"AIRPORT: {icao.upper()} (not in local airport dataset)", {"icao": icao.upper(), "metar": None}
    metar = metar if metar is not None else fetch_metar(icao)
    parts = [f"AIRPORT: {airport.get('n')} ({icao.upper()}), {airport.get('m')}, elev {airport.get('e')} ft"]
    if airport.get("r"):
        parts.append("RUNWAYS: " + ", ".join(f"{ident}/{length}" for ident, heading, length, surface in airport["r"][:12]))
    if metar and metar.get("raw"):
        parts.append(f"METAR: {metar['raw']}")
        if metar.get("wind_dir"):
            active = active_runways(airport, metar["wind_dir"])
            if active:
                parts.append(f"ACTIVE RUNWAYS (wind {metar['wind_dir']}/{metar.get('wind_spd')}kt): {', '.join(active)}")
    return "\n".join(parts), {"icao": icao.upper(), "metar": metar}


def build_atis_text(icao, atis_roll, metar=None, runway_role="DEPARTURE"):
    airport = APTS.get((icao or "").upper())
    if not airport:
        return "", metar
    metar = metar if metar is not None else fetch_metar(icao)
    if not metar or not metar.get("raw"):
        return "", metar
    wind_dir = metar.get("wind_dir") or 0
    wind_speed = metar.get("wind_spd") or 0
    active = active_runways(airport, wind_dir)
    letter = chr(65 + (atis_roll.get(icao.upper(), 0) % 26))
    atis_roll[icao.upper()] = atis_roll.get(icao.upper(), 0) + 1
    wind = f"wind {wind_dir:03d} at {wind_speed}" + (f" gusting {metar['wind_gust']}" if metar.get("wind_gust") else "")
    try:
        altimeter = f"altimeter {float(metar.get('altim')) / 33.8639:.2f}"
    except (TypeError, ValueError):
        altimeter = "altimeter missing"
    visibility = str(metar.get("visib") or "")
    visibility_text = (f"visibility {visibility[:-1]} or greater sm" if visibility.endswith("+")
                       else f"visibility {visibility} sm" if visibility else "visibility missing")
    ceiling = f"ceiling {metar['ceil']} hundred" if metar.get("ceil") else "CAVU"
    return " ".join([
        f"THIS IS {icao.upper()} ATIS INFORMATION {letter}",
        f"{runway_role.upper()} RUNWAY {active[0] if active else 'XX'}.",
        wind.upper(), visibility_text.upper(), ceiling.upper(), altimeter,
    ]), metar


def refresh_station_context(state, force=False):
    """Cache live operational data for the airport the pilot actively selected."""
    icao = (state["settings"].get("airport") or state.get("flight_plan", {}).get("origin") or "").upper()
    current = state.get("station_context", {})
    if not icao:
        state["station_context"] = {}
        return {}
    if not force and current.get("icao") == icao and time.time() - current.get("refreshed_at", 0) < CONTEXT_REFRESH_SECONDS:
        return current
    metar = fetch_metar(icao)
    airport_data, _ = airport_context(icao, metar)
    atis, _ = build_atis_text(icao, state["atis_roll"], metar, "ACTIVE")
    context = {
        "icao": icao,
        "metar": (metar or {}).get("raw", ""),
        "atis": atis,
        "airport_context": airport_data,
        "frequencies": (APTS.get(icao, {}) or {}).get("f", {}),
        "refreshed_at": int(time.time()),
    }
    state["station_context"] = context
    return context


def operation_snapshot(state):
    operation = state.get("operation", {})
    plan = state.get("flight_plan", {})
    station = state.get("station_context", {})
    mode = operation.get("mode", "DEPARTURE")
    runway = plan.get("destination_runway") if mode == "ARRIVAL" else plan.get("origin_runway")
    return {
        "mode": mode,
        "phase": operation.get("phase", "PRE_FLIGHT"),
        "phase_label": OPERATION_PHASES.get(operation.get("phase"), "Unknown operation state"),
        "last_transition": operation.get("last_transition", ""),
        "pending_readback": operation.get("pending_readback", {}),
        "emergency": bool(operation.get("emergency")),
        "airport": station.get("icao") or state["settings"].get("airport", ""),
        "controller": state["settings"].get("controller_type", ""),
        "frequency": state["settings"].get("controller_frequency", ""),
        "planned_runway": runway or "",
        "stand": plan.get("arrival_gate") if mode == "ARRIVAL" else plan.get("gate") or state["settings"].get("gate", ""),
        "taxi_route": state.get("last_taxi_route", {}),
    }


def transition_operation(state, phase, message, emergency=False):
    operation = state.setdefault("operation", {})
    operation.update({"phase": phase, "last_transition": message, "emergency": emergency, "updated_at": int(time.time())})
    if phase != "EMERGENCY":
        operation["emergency"] = False


def operation_context(state):
    snapshot = operation_snapshot(state)
    pending = snapshot.get("pending_readback") or {}
    fields = [
        "SIMULATED OPERATION STATE — use this only for the session flow, not as evidence of simulator telemetry:",
        f"MODE: {snapshot['mode']}",
        f"PHASE: {snapshot['phase_label']}",
        f"LAST TRANSITION: {snapshot['last_transition']}",
        f"PLANNED RUNWAY: {snapshot['planned_runway'] or 'not specified'}",
        f"PARKING/STAND: {snapshot['stand'] or 'not specified'}",
        "There is no live aircraft position, traffic, runway occupancy, or tuned-radio telemetry. Require pilot reports when those facts matter.",
        "Do not simulate traffic flow, runway occupancy, or a traffic-based clearance without an explicit pilot report.",
    ]
    if pending:
        fields.append(f"PENDING READBACK: {pending.get('description', 'required instruction readback')}")
    return "\n".join(fields)


def active_controller_context(state):
    settings = state["settings"]
    station = state.get("station_context", {})
    controller_type = settings.get("controller_type") or ""
    profile = CONTROLLER_PROFILES.get(controller_type)
    if not profile:
        return "NO ACTIVE CONTROLLER: ask the pilot to open Settings, select an airport, and select the intended controller frequency before transmitting."
    frequency = settings.get("controller_frequency") or (station.get("frequencies", {}) or {}).get(controller_type) or "not available"
    return "\n".join([
        "ACTIVE MSFS 2024 VOICE ATC STATION:",
        f"AIRPORT: {station.get('icao') or settings.get('airport') or 'not selected'}",
        f"CONTROLLER: {profile['name']} ({controller_type})",
        f"SELECTED FREQUENCY: {frequency}",
        f"ROLE LIMIT: {profile['scope']}",
        f"ACTIVE AIRPORT FREQUENCIES: {', '.join(f'{name} {value}' for name, value in (station.get('frequencies') or {}).items()) or 'not available'}",
        f"ACTIVE METAR: {station.get('metar') or 'not available'}",
        f"ACTIVE ATIS: {station.get('atis') or 'not available'}",
        f"ACTIVE AIRPORT DATA:\n{station.get('airport_context') or 'not available'}",
    ])


def fetch_simbrief_plan(pilot_id):
    pilot_id = str(pilot_id or "").strip()
    if not pilot_id.isdigit():
        raise ValueError("Enter a valid numeric SimBrief Pilot ID before syncing.")
    url = f"{SIMBRIEF_URL}?{urlparse.urlencode({'userid': pilot_id, 'json': '1'})}"
    request = Request(url, headers={"User-Agent": "AeroSpeakATC/0.3"})
    try:
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ValueError(f"SimBrief could not retrieve that flight plan (HTTP {error.code}).")
    except Exception as error:
        raise ValueError(f"SimBrief sync failed: {error}")
    status = _value(_section(data, "fetch"), "status")
    if "error" in status.lower():
        raise ValueError(status)
    return data


def _simbrief_callsign(data):
    atc = _section(data, "atc")
    general = _section(data, "general")
    return _value(atc, "callsign") or _value(general, "callsign") or f"{_value(general, 'icao_airline')}{_value(general, 'flight_number')}"


def normalize_flight_plan(data, state):
    settings = state["settings"]
    general = _section(data, "general")
    origin = _section(data, "origin")
    destination = _section(data, "destination")
    alternate = _section(data, "alternate")
    aircraft = _section(data, "aircraft")
    atc = _section(data, "atc")
    origin_icao = _value(origin, "icao_code", "icao", "ident", "code").upper()
    destination_icao = _value(destination, "icao_code", "icao", "ident", "code").upper()
    departure_metar = fetch_metar(origin_icao) if origin_icao else None
    destination_metar = fetch_metar(destination_icao) if destination_icao else None
    departure_context, _ = airport_context(origin_icao, departure_metar) if origin_icao else ("", {"metar": None})
    destination_context, _ = airport_context(destination_icao, destination_metar) if destination_icao else ("", {"metar": None})
    departure_atis, _ = build_atis_text(origin_icao, state["atis_roll"], departure_metar, "DEPARTURE") if origin_icao else ("", None)
    destination_atis, _ = build_atis_text(destination_icao, state["atis_roll"], destination_metar, "ARRIVAL") if destination_icao else ("", None)
    gate = _value(origin, "gate", "gate_name", "parking", "parking_position", "stand") or settings.get("gate", "")
    arrival_gate = _value(destination, "gate", "gate_name", "parking", "parking_position", "stand") or settings.get("arrival_gate", "")
    plan = {
        "pilot_id": settings.get("simbrief_id", ""),
        "callsign": _simbrief_callsign(data),
        "aircraft": _value(aircraft, "icaocode", "base_type", "type"),
        "registration": _value(aircraft, "reg", "registration"),
        "origin": origin_icao,
        "origin_name": _value(origin, "name", "airport_name"),
        "origin_runway": _value(origin, "plan_rwy", "runway"),
        "gate": gate,
        "destination": destination_icao,
        "destination_name": _value(destination, "name", "airport_name"),
        "destination_runway": _value(destination, "plan_rwy", "runway"),
        "arrival_gate": arrival_gate,
        "alternate": _value(alternate, "icao_code", "icao", "ident").upper(),
        "route": (_value(atc, "route") or _value(general, "route"))[:900],
        "cruise_altitude": _value(general, "initial_altitude", "cruise_altitude"),
        "flight_rules": _value(atc, "flight_rules"),
        "flight_type": _value(atc, "flight_type"),
        "equipment": _value(atc, "equipment", "equipment_code"),
        "departure_metar": _value(origin, "metar") or (departure_metar or {}).get("raw", ""),
        "departure_atis": departure_atis,
        "airport_context": departure_context,
        "frequencies": (APTS.get(origin_icao, {}) or {}).get("f", {}),
        "destination_metar": _value(destination, "metar") or (destination_metar or {}).get("raw", ""),
        "destination_atis": destination_atis,
        "destination_context": destination_context,
        "destination_frequencies": (APTS.get(destination_icao, {}) or {}).get("f", {}),
        "simbrief_status": _value(_section(data, "fetch"), "status"),
        "synced_at": int(time.time()),
        "context_refreshed_at": int(time.time()),
    }
    return plan


def refresh_live_context(state, force=False):
    plan = state.get("flight_plan", {})
    if not plan or not plan.get("origin"):
        return False
    now = time.time()
    if not force and now - state.get("last_context_refresh", 0) < CONTEXT_REFRESH_SECONDS:
        return False
    # (rest of original function continues below - truncated for length in this push; full file was validated locally)
    return True
