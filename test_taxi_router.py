from unittest.mock import patch

import server
import taxi_router


def fake_layout():
    start = taxi_router._key(0.0, 0.0)
    middle = taxi_router._key(0.0, 0.001)
    runway = taxi_router._key(0.0, 0.002)
    return {
        "icao": "TEST",
        "loaded_at": 0,
        "stands": {"G1": (0.0, 0.0)},
        "runways": {"08L": [(0.0, 0.002)]},
        "points": {start: (0.0, 0.0), middle: (0.0, 0.001), runway: (0.0, 0.002)},
        "graph": {
            start: [(middle, 111, "ALPHA")],
            middle: [(start, 111, "ALPHA"), (runway, 111, "BRAVO")],
            runway: [(middle, 111, "BRAVO")],
        },
    }


def test_named_stand_to_runway_route():
    with patch.object(taxi_router, "load_layout", return_value=fake_layout()):
        route = taxi_router.route_from_stand_to_runway("TEST", 0.0, 0.0, "G1", "8L")
    assert route["ok"]
    assert route["runway"] == "08L"
    assert route["taxiways"] == ["ALPHA", "BRAVO"]


def test_gemini_is_the_only_controller_response_engine():
    state = server._blank_state()
    state["settings"].update({"controller_type": "GND", "callsign": "EUROPEAIR447"})
    gemini_reply = "EUROPEAIR447, engine start approved. Advise ready for pushback."
    with patch.object(server, "gemini_call", return_value=gemini_reply) as gemini_call:
        reply = server.gemini_controller_reply("EuropeAir 447 request startup clearance", state)
    assert reply == gemini_reply
    gemini_call.assert_called_once()


if __name__ == "__main__":
    test_named_stand_to_runway_route()
    test_gemini_is_the_only_controller_response_engine()
    print("AeroSpeak taxi-routing and Gemini-response tests passed")
