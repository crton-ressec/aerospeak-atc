"""OpenStreetMap-backed airport surface routing for AeroSpeak Ground operations.

The router only returns a named taxiway sequence when a mapped, connected graph and the
requested stand/runway are available. It deliberately returns an unavailable result rather
than creating a plausible-looking route from incomplete geometry.
"""

from __future__ import annotations

import heapq
import json
import math
import re
import time
from collections import defaultdict
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_TTL_SECONDS = 6 * 60 * 60
_LAYOUT_CACHE: dict[str, dict] = {}


def _key(lat: float, lon: float) -> tuple[int, int]:
    return round(lat * 100000), round(lon * 100000)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Approximate metres for short airport-surface segments."""
    lat_scale = 111_320.0
    lon_scale = lat_scale * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * lat_scale, (a[1] - b[1]) * lon_scale)


def _clean_ref(value: str) -> str:
    return "".join(char for char in (value or "").upper() if char.isalnum())


def _runway_ref(value: str) -> str:
    cleaned = _clean_ref(value)
    match = re.fullmatch(r"0?(\d{1,2})([LRC]?)", cleaned)
    if not match:
        return cleaned
    return f"{int(match.group(1)):02d}{match.group(2)}"


def _query_surface(lat: float, lon: float, radius: int = 5000) -> list[dict]:
    query = f"""[out:json][timeout:25];
(
  way(around:{radius},{lat},{lon})[aeroway~\"taxiway|taxilane|runway|parking_position\"];
  node(around:{radius},{lat},{lon})[aeroway~\"parking_position|gate\"];
);
out tags geom;"""
    request = Request(
        OVERPASS_URL,
        data=urlencode({"data": query}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AeroSpeakATC/0.7"},
    )
    with urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8")).get("elements", [])


def _way_points(element: dict) -> list[tuple[float, float]]:
    return [(float(point["lat"]), float(point["lon"])) for point in element.get("geometry", []) if "lat" in point and "lon" in point]


def _layout_from_elements(icao: str, elements: list[dict]) -> dict:
    graph: dict[tuple[int, int], list[tuple[tuple[int, int], float, str]]] = defaultdict(list)
    points: dict[tuple[int, int], tuple[float, float]] = {}
    stands: dict[str, tuple[float, float]] = {}
    runways: dict[str, list[tuple[float, float]]] = {}

    for element in elements:
        tags = element.get("tags") or {}
        aeroway = tags.get("aeroway")
        geometry = _way_points(element)
        if aeroway in ("taxiway", "taxilane") and len(geometry) > 1:
            name = (tags.get("ref") or tags.get("name") or "").strip().upper()
            for left, right in zip(geometry, geometry[1:]):
                left_key, right_key = _key(*left), _key(*right)
                points[left_key], points[right_key] = left, right
                length = _distance(left, right)
                graph[left_key].append((right_key, length, name))
                graph[right_key].append((left_key, length, name))
        elif aeroway == "runway" and len(geometry) > 1:
            raw_ref = str(tags.get("ref") or tags.get("name") or "")
            if raw_ref:
                for identifier in raw_ref.replace(";", "/").split("/"):
                    if identifier.strip():
                        runways[_runway_ref(identifier)] = geometry
        elif aeroway in ("parking_position", "gate"):
            ref = _clean_ref(tags.get("ref") or tags.get("name") or "")
            if geometry:
                position = geometry[-1]
            elif "lat" in element and "lon" in element:
                position = (float(element["lat"]), float(element["lon"]))
            else:
                continue
            if ref:
                stands[ref] = position

    return {"icao": icao, "graph": graph, "points": points, "stands": stands, "runways": runways, "loaded_at": time.time()}


def load_layout(icao: str, lat: float, lon: float) -> dict:
    icao = (icao or "").upper()
    cached = _LAYOUT_CACHE.get(icao)
    if cached and time.time() - cached.get("loaded_at", 0) < CACHE_TTL_SECONDS:
        return cached
    layout = _layout_from_elements(icao, _query_surface(lat, lon))
    _LAYOUT_CACHE[icao] = layout
    return layout


def _nearest_graph_node(layout: dict, point: tuple[float, float], limit_metres: float = 150) -> tuple[int, int] | None:
    candidates = (( _distance(point, coordinates), key) for key, coordinates in layout["points"].items())
    nearest = min(candidates, default=(float("inf"), None))
    return nearest[1] if nearest[0] <= limit_metres else None


def _shortest_path(layout: dict, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[str, float]] | None:
    queue = [(0.0, start)]
    distances = {start: 0.0}
    previous: dict[tuple[int, int], tuple[tuple[int, int], str, float]] = {}
    while queue:
        cost, node = heapq.heappop(queue)
        if node == end:
            break
        if cost != distances.get(node):
            continue
        for neighbor, length, name in layout["graph"].get(node, []):
            candidate = cost + length
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                previous[neighbor] = (node, name, length)
                heapq.heappush(queue, (candidate, neighbor))
    if end not in previous and end != start:
        return None
    segments: list[tuple[str, float]] = []
    node = end
    while node != start:
        parent, name, length = previous[node]
        segments.append((name, length))
        node = parent
    return list(reversed(segments))


def route_from_stand_to_runway(icao: str, lat: float, lon: float, stand: str, runway: str) -> dict:
    """Return a named taxi route or an explicit reason why one cannot be safely issued."""
    normalized_stand, normalized_runway = _clean_ref(stand), _runway_ref(runway)
    if not normalized_stand or not normalized_runway:
        return {"ok": False, "reason": "A parking stand and runway are required for taxi routing."}
    if lat is None or lon is None:
        return {"ok": False, "reason": f"Airport coordinates for {icao.upper()} are unavailable."}
    try:
        layout = load_layout(icao, lat, lon)
    except Exception as error:
        return {"ok": False, "reason": f"Airport surface data could not be retrieved ({error})."}
    start_point = layout["stands"].get(normalized_stand)
    runway_points = layout["runways"].get(normalized_runway)
    if not start_point:
        return {"ok": False, "reason": f"Parking stand {stand.upper()} is not mapped in the available airport surface data."}
    if not runway_points:
        return {"ok": False, "reason": f"Runway {runway.upper()} is not mapped in the available airport surface data."}
    start = _nearest_graph_node(layout, start_point)
    runway_nodes = [_nearest_graph_node(layout, point) for point in runway_points]
    runway_nodes = [node for node in runway_nodes if node]
    if not start or not runway_nodes:
        return {"ok": False, "reason": "The mapped stand or runway is not connected to a taxiway graph."}
    candidate_routes = []
    for end in dict.fromkeys(runway_nodes):
        path = _shortest_path(layout, start, end)
        if path:
            candidate_routes.append((sum(length for _, length in path), path))
    if not candidate_routes:
        return {"ok": False, "reason": "No connected taxiway path was found between that stand and runway."}
    _, segments = min(candidate_routes, key=lambda candidate: candidate[0])
    names: list[str] = []
    for name, _ in segments:
        if not name:
            return {"ok": False, "reason": "The available taxiway path is missing taxiway names, so a safe named clearance cannot be issued."}
        if not names or names[-1] != name:
            names.append(name)
    if not names:
        return {"ok": False, "reason": "No named taxiway sequence was available for that route."}
    return {
        "ok": True,
        "airport": icao.upper(),
        "stand": normalized_stand,
        "runway": normalized_runway,
        "taxiways": names,
        "distance_metres": round(sum(length for _, length in segments)),
        "source": "OpenStreetMap airport-surface geometry",
    }
