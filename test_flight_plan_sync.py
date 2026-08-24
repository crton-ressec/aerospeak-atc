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
    state["settings"].update({"simbrief_id": "12345", "gate": "B12"})
    with patch.object(server, "fetch_metar", return_value=METAR):
        plan = server.normalize_flight_plan(SAMPLE_PLAN, state)
    state["flight_plan"] = plan
    context = server.flight_plan_context(state)
    prompt = server.system_prompt(state)
    assert plan["callsign"] == "AAL100"
    assert plan["gate"] == "B12"
    assert "CURRENT DEPARTURE ATIS" in context
    assert "DEPARTURE FREQUENCIES" in context
    assert "AAL100" in prompt and "KJFK" in prompt and "B12" in prompt


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
    test_browser_sessions_are_isolated()
    test_rate_limit_blocks_excess_requests()
    test_optional_access_gate()
    test_voice_only_boundary_rejects_json()
    print("AeroSpeak session, flight-plan, safety, and voice-only tests passed")
