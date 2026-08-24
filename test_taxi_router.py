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


def test_ground_startup_and_pushback_are_distinct():
    state = server._blank_state()
    state["settings"].update({"controller_type": "GND", "callsign": "EUROPEAIR447"})
    assert server.ground_taxi_reply("EuropeAir 447 request engine start", state) == "EUROPEAIR447, engine start approved. Advise ready for pushback."
    assert server.ground_taxi_reply("EuropeAir 447 request pushback", state) == "EUROPEAIR447, pushback approved. Advise ready to taxi."


def test_synced_ground_call_uses_planned_runway_and_verified_route():
    state = server._blank_state()
    state["settings"].update({"controller_type": "GND", "airport": "TEST", "callsign": "EUROPEAIR447"})
    state["station_context"] = {"icao": "TEST"}
    state["flight_plan"] = {"origin": "TEST", "origin_runway": "08L", "gate": "G1", "callsign": "EUROPEAIR447"}
    server.APTS["TEST"] = {"lat": 0.0, "lon": 0.0}
    route = {"ok": True, "runway": "08L", "taxiways": ["ALPHA", "BRAVO"]}
    with patch.object(server, "route_from_stand_to_runway", return_value=route):
        reply = server.ground_taxi_reply("EuropeAir 447 ready to taxi", state)
    assert reply == "EUROPEAIR447, taxi to runway 08L via ALPHA, BRAVO, hold short runway 08L."


def test_unsynced_ground_call_uses_verified_route():
    state = server._blank_state()
    state["settings"].update({"controller_type": "GND", "airport": "TEST", "callsign": "EUROPEAIR447"})
    state["station_context"] = {"icao": "TEST"}
    server.APTS["TEST"] = {"lat": 0.0, "lon": 0.0}
    route = {"ok": True, "runway": "08L", "taxiways": ["ALPHA", "BRAVO"]}
    with patch.object(server, "route_from_stand_to_runway", return_value=route):
        reply = server.ground_taxi_reply("EuropeAir 447 ready to taxi to runway 8L at parking stand G1", state)
    assert reply == "EUROPEAIR447, taxi to runway 08L via ALPHA, BRAVO, hold short runway 08L."


if __name__ == "__main__":
    test_named_stand_to_runway_route()
    test_ground_startup_and_pushback_are_distinct()
    test_synced_ground_call_uses_planned_runway_and_verified_route()
    test_unsynced_ground_call_uses_verified_route()
    print("AeroSpeak taxi-routing tests passed")
