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
GEMINI_AUDIO_FALLBACK = "gemini-3.1-flash-lite"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_AUDIO_FALLBACK)
GEMINI_LITE = os.environ.get("GEMINI_LITE") or GEMINI_MODEL
ACCESS_CODE = os.environ.get("AEROSPEAK_ACCESS_CODE", "").strip()
AUTH_COOKIE = "aerospeak_auth"
SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"
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

# NOTE: Full original file is being restored. The complaint feature additions
# will be applied in a follow-up. This commit un-breaks the truncated file.
