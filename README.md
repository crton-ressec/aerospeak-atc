# AeroSpeak ATC

AeroSpeak ATC is a **voice-first ATC companion for Microsoft Flight Simulator 2024**. It replaces menu-driven radio selections with a chosen airport controller frequency, hold-to-talk transmission, concise ATC replies, and VHF-style audio.

> AeroSpeak is for flight-simulation use only. It is not for real-world aviation navigation or operational use.

## Current Capabilities

| Capability | Behavior |
|---|---|
| Voice-only radio | The application accepts browser-recorded microphone transmissions only. There is no text transmission interface. |
| Session isolation | Airport, controller selection, SimBrief context, settings, and conversation history are scoped to a browser session and expire automatically. They are not saved to a shared file. |
| SimBrief synchronization | A pilot can save a SimBrief Pilot ID in Settings and synchronize the most recent briefing. The application extracts callsign, aircraft, departure/destination/alternate, planned runways, route, cruise altitude, rules, equipment, and any available departure or arrival gate/stand value. |
| Live airport context | Active radio use combines local airport/runway/frequency data with current METAR-derived ATIS. SimBrief adds flight-plan context when the pilot chooses to synchronize it. |
| Ground taxi routing | With no synced flight plan, Ground can request mapped airport-surface data and produce a named stand-to-runway route only when a connected, named taxiway graph is available. With a synced plan, Ground tells the pilot to follow the flight plan. |
| Frequency-driven controller | The pilot selects an airport and its listed Clearance, Ground, Tower, Approach, Departure, or ATIS frequency in Settings. The selected station governs the controller's role and response limits on every voice turn. |
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

The pilot opens **Settings**, selects an airport, then selects the exact listed controller frequency they are calling. AeroSpeak persists that airport and station for the browser session, loads its local runway/frequency data plus live METAR-derived ATIS, and confines every reply to the selected station’s role. A SimBrief sync is optional and adds callsign, aircraft, route, departure/destination context, and available stand information. The main screen remains focused on hold-to-talk radio interaction.

Approach and Departure operate as **procedural non-radar** services. AeroSpeak relies on pilot position reports and does not claim radar contact, provide vectors, or infer aircraft position from the simulator. Ground taxi routes are derived from available open airport-surface mapping; the service refuses to invent a named route if the stand, runway, taxiway labels, or connectivity are not mapped. Airport layouts can differ from MSFS or third-party scenery, so the pilot must verify any route against the displayed simulator airport layout.

The access code is a deployment-level shared gate, not a replacement for an identity system. For a multi-account public product, add an authenticated user database and persist session data in a managed datastore.

## Safety and Operations

The deployment exposes a `GET /healthz` endpoint. Logs emit compact JSON events for flight-plan synchronizations and completed radio turns; they intentionally do not include the full flight plan, transcript, or generated audio. Generated audio is removed after 30 minutes. The in-memory rate limits are suitable for a single Render instance but should be moved to a shared store for horizontally scaled production deployments.

## Tests

Run the repository’s focused regression suite with:

```bash
python3 test_flight_plan_sync.py
```

The tests use mocked weather and briefing data to validate session isolation, voice-only request rejection, rate limiting, departure and destination prompt injection, key plan fields, and the non-radar response guard. A real SimBrief Pilot ID should still be used in a live staging deployment before release to verify the shape of an individual pilot’s latest briefing.
