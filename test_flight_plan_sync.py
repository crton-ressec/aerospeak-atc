import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import server


SAMPLE_PLAN = {
    "fetch": {"status": "Success"},
    "general": {"icao_airline": "AAL", "flight_number": "100", "initial_altitude": "35000"},
    "origin": {"icao_code": "KJFK", "plan_rwy": "04L", "metar": "KJFK 231651Z 24012KT 10SM FEW050 25/16 A2992"},
    "destination": {"icao_code": "KLAX", "plan_rwy": "25R"},
    "alternate": {"icao_code": "KSAN"},
    "aircraft": {"icaocode": "A320", "reg": "N123AA"},
    "atc": {"callsign": "AAL100", "route": "MERIT3 GREKI Q436 J70 RIKAL HLYWD2", "flight_rules": "I", "flight_type": "S", "equipment": "SDE2E3FGHIJ1RWXY"},
}
METAR = {"raw": "KJFK 231651Z 24012KT 10SM FEW050 25/16 A2992", "wind_dir": 240, "wind_spd": 12, "wind_gust": None, "altim": 1013.0, "visib": "10+", "ceil": None}


def fake_request(cookie=None):
    return SimpleNamespace(cookies={"aerospeak_session": cookie} if cookie else {}, state=SimpleNamespace(), client=SimpleNamespace(host="127.0.0.1"))


def test_context_injection():
    state = server._blank_state()
    state["settings"].update({"simbrief_id": "12345", "gate": "B12", "arrival_gate": "C18", "airport": "KLAX", "controller_type": "APP", "controller_frequency": "124.500"})
    with patch.object(server, "fetch_metar", return_value=METAR):
        plan = server.normalize_flight_plan(SAMPLE_PLAN, state)
    state["flight_plan"] = plan
    with patch.object(server, "fetch_metar", return_value=METAR):
        server.refresh_station_context(state, force=True)
    context = server.flight_plan_context(state)
    prompt = server.system_prompt(state)
    assert plan["callsign"] == "AAL100"
    assert plan["gate"] == "B12"
    assert plan["arrival_gate"] == "C18"
    assert "CURRENT DEPARTURE ATIS" in context
    assert "CURRENT DESTINATION ATIS" in context
    assert "DESTINATION FREQUENCIES" in context
    assert "AAL100" in prompt and "KJFK" in prompt and "KLAX" in prompt and "C18" in prompt
    assert "ACTIVE MSFS 2024 VOICE ATC STATION" in prompt
    assert "CONTROLLER: Approach (APP)" in prompt
    assert "never claim radar contact" in prompt.lower()


def test_selected_controller_frequency_context():
    state = server._blank_state()
    state["settings"].update({"airport": "KJFK", "controller_type": "GND", "controller_frequency": "121.650"})
    state["station_context"] = {
        "icao": "KJFK",
        "metar": "KJFK 231651Z 24012KT 10SM FEW050 25/16 A2992",
        "atis": "THIS IS KJFK ATIS INFORMATION A",
        "airport_context": "AIRPORT: John F. Kennedy International Airport (KJFK)",
        "frequencies": {"CLD": 135.05, "GND": 121.65, "TWR": 119.1},
    }
    context = server.active_controller_context(state)
    assert "CONTROLLER: Ground (GND)" in context
    assert "SELECTED FREQUENCY: 121.650" in context
    assert "taxi" in context.lower()


def test_nonradar_guard_rewrites_radar_language():
    state = server._blank_state()
    state["settings"]["controller_type"] = "APP"
    reply = server.enforce_nonradar_reply("Radar contact. Fly heading zero niner zero.", state)
    assert "Negative radar service" in reply
    assert "radar contact" not in reply.lower()


def test_browser_sessions_are_isolated():
    first = fake_request()
    first_state = server.session_for(first)
    first_state["settings"]["callsign"] = "FIRST1"
    second = fake_request()
    second_state = server.session_for(second)
    assert first_state is not second_state
    assert second_state["settings"]["callsign"] == ""


def test_rate_limit_blocks_excess_requests():
    request = fake_request()
    server.session_for(request)
    for _ in range(2):
        assert server.rate_limited(request, "unit", 2, 60) is None
    response = server.rate_limited(request, "unit", 2, 60)
    assert response.status_code == 429


def test_optional_access_gate():
    request = fake_request()
    old_code = server.ACCESS_CODE
    try:
        server.ACCESS_CODE = "practice-code"
        state = server.session_for(request)
        state["authorized"] = False
        response = server.require_access(request, state)
        assert response.status_code == 403
        state["authorized"] = True
        assert server.require_access(request, state) is None
    finally:
        server.ACCESS_CODE = old_code


def test_voice_only_boundary_rejects_json():
    class Request:
        headers = {"content-type": "application/json"}
        cookies = {}
        state = SimpleNamespace()
        client = SimpleNamespace(host="127.0.0.2")
    response = asyncio.run(server.api_chat(Request()))
    assert response.status_code == 415


if __name__ == "__main__":
    test_context_injection()
    test_selected_controller_frequency_context()
    test_nonradar_guard_rewrites_radar_language()
    test_browser_sessions_are_isolated()
    test_rate_limit_blocks_excess_requests()
    test_optional_access_gate()
    test_voice_only_boundary_rejects_json()
    print("AeroSpeak session, flight-plan, safety, and voice-only tests passed")
