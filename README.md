# AeroSpeak ATC

AeroSpeak ATC is a **voice-first** AI air-traffic-control practice application for console flight simmers. A pilot configures a session in Settings, holds the transmit control to speak, and receives an ATC response with VHF-style audio.

> AeroSpeak is for flight-simulation practice only. It is not for real-world aviation navigation or operational use.

## Current Capabilities

| Capability | Behavior |
|---|---|
| Voice-only radio | The application accepts browser-recorded microphone transmissions only. There is no text transmission interface. |
| Session isolation | Settings, SimBrief context, scenario, and conversation history are scoped to a browser session and expire automatically. They are not saved to a shared file. |
| SimBrief synchronization | A pilot can save a SimBrief Pilot ID in Settings and synchronize the most recent briefing. The application extracts callsign, aircraft, departure/destination/alternate, planned runways, route, cruise altitude, rules, equipment, and any available departure or arrival gate/stand value. |
| Live airport context | Sync and active radio use combine the briefing with local departure **and destination** airport/runway/frequency data plus current METAR-derived ATIS. Live context refreshes at most once every five minutes during radio use, and can be refreshed manually in Settings. |
| ATC scenarios | IFR clearance, ground/taxi, tower departure, VFR pattern, procedural non-radar arrival, and arrival taxi-in scenarios shape the controller's operational context. |
| Audio treatment | ElevenLabs speech is converted through the bundled ImageIO FFmpeg executable and processed with radio band-pass, compression, clicks, and hiss. |
| Service safeguards | The service limits request size, transmission duration, per-session request rate, generated-audio retention, and can require an optional access code before it invokes live services. |

## Setup

Create a Python environment, install the dependencies, and start the service.

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

| Environment variable | Required | Purpose |
|---|---:|---|
| `GEMINI_KEY` | Yes | Gemini API key for transcription and ATC responses. |
| `ELEVEN_KEY` | Yes | ElevenLabs API key for ATC and ATIS speech. |
| `ATC_VOICE` | No | ElevenLabs voice identifier. A Charlie-compatible default is supplied. |
| `GEMINI_MODEL` | No | Main Gemini model for future text-oriented operations. |
| `GEMINI_LITE` | No | Gemini model used for the combined voice transcription and ATC response. |
| `AEROSPEAK_ACCESS_CODE` | No | When set, users must enter this code in Settings before the app can make live SimBrief, ATIS, or radio requests. |

## Pilot Flow

The pilot opens **Settings**, saves a SimBrief Pilot ID, callsign, optional departure/arrival stand, and scenario, then selects **Sync Flight Plan**. AeroSpeak retrieves the pilot’s latest briefing, validates the key fields, and stores both departure and destination operational context in that browser session. For departure scenarios, the active airport is the origin. For arrival scenarios, the active airport is the destination and the controller uses a procedural **non-radar** flow: pilot position reports, destination ATIS, runway assignment, landing clearance, and taxi-in without radar contact or vectors. The main screen remains focused on the hold-to-talk radio control.

The access code is a deployment-level shared gate, not a replacement for an identity system. For a multi-account public product, add an authenticated user database and persist session data in a managed datastore.

## Safety and Operations

The deployment exposes a `GET /healthz` endpoint. Logs emit compact JSON events for flight-plan synchronizations and completed radio turns; they intentionally do not include the full flight plan, transcript, or generated audio. Generated audio is removed after 30 minutes. The in-memory rate limits are suitable for a single Render instance but should be moved to a shared store for horizontally scaled production deployments.

## Tests

Run the repository’s focused regression suite with:

```bash
python3 test_flight_plan_sync.py
```

The tests use mocked weather and briefing data to validate session isolation, voice-only request rejection, rate limiting, departure and destination prompt injection, key plan fields, and the non-radar response guard. A real SimBrief Pilot ID should still be used in a live staging deployment before release to verify the shape of an individual pilot’s latest briefing.
