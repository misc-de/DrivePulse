"""Traffic source parsers + aggregation (map/_traffic.py)."""
from __future__ import annotations

from drivepulse_app.map import _traffic as tr


def test_normalize_bab_items_parses_point_and_flags():
    items = [{
        "_kind": "closure", "_road": "A565", "point": "50.75,7.08",
        "title": "A565 | Bonn-Nord", "description": ["a", "", "b"],
        "isBlocked": "true", "delayTimeValue": 12,
    }]

    [event] = tr.normalize_bab_items(items)

    assert (event["lat"], event["lon"]) == (50.75, 7.08)
    assert event["kind"] == "closure"
    assert event["blocked"] is True
    assert event["delay"] == "12"
    assert event["description"] == ["a", "b"]
    assert event["source"] == tr.SOURCE_AUTOBAHN


def test_normalize_bab_items_skips_bad_points():
    items = [{"point": ""}, {"point": "0,0"}, {"point": "x,y"}]
    assert tr.normalize_bab_items(items) == []


def test_normalize_bab_items_cap_drops_roadworks_first(monkeypatch):
    monkeypatch.setattr(tr, "MAX_BAB_EVENTS", 2)
    items = [
        {"_kind": "roadworks", "point": "50,7"},
        {"_kind": "closure", "point": "50,7"},
        {"_kind": "incidents", "point": "50,7"},
    ]

    kinds = [e["kind"] for e in tr.normalize_bab_items(items)]

    assert kinds == ["incidents", "closure"]


def test_koeln_flow_maps_levels_and_skips_unknown():
    data = {"features": [
        {"attributes": {"name": "Ring", "auslastung": 2},
         "geometry": {"paths": [[[6.9, 50.9], [6.91, 50.91]], [[6.92, 50.92]]]}},
        {"attributes": {"name": "Defekt", "auslastung": 16},
         "geometry": {"paths": [[[6.9, 50.9], [6.91, 50.91]]]}},
    ]}

    segs = tr.koeln_fetch_flow(lambda _url: data)

    # Second path has a single point and is dropped; auslastung 16 = no data.
    assert segs == [{
        "coords": [[6.9, 50.9], [6.91, 50.91]], "level": "jam",
        "name": "Ring", "speed": None, "source": tr.SOURCE_KOELN,
    }]


def test_koeln_events_keeps_only_active_and_maps_kind():
    now = 1_000_000.0  # seconds
    ms = now * 1000

    def feat(typ, start, end, anzeige=1):
        return {"geometry": {"coordinates": [6.95, 50.93]},
                "properties": {"typ": typ, "anzeige": anzeige, "name": f"T{typ}",
                               "beschreibung": "gesperrt",
                               "datum_von": start, "datum_bis": end}}

    data = {"features": [
        feat(2, ms - 1, ms + 1),          # active closure
        feat(3, ms + 10_000, ms + 20_000),  # starts in the future
        feat(3, ms - 20_000, ms - 10_000),  # already over
        feat(11, ms - 1, ms + 1),         # event
        feat(3, ms - 1, ms + 1, anzeige=0),  # hidden
    ]}

    events = tr.koeln_fetch_events(lambda _url: data, now=now)

    assert [(e["title"], e["kind"], e["blocked"]) for e in events] == [
        ("T2", "closure", True),
        ("T11", "event", False),
    ]
    assert events[0]["lat"] == 50.93 and events[0]["lon"] == 6.95
    assert events[0]["start"].startswith("1970-01-12")


def test_bonn_flow_maps_status_text_and_speed():
    data = {"features": [
        {"geometry": {"type": "MultiLineString",
                      "coordinates": [[[7.1, 50.7], [7.11, 50.71]]]},
         "properties": {"verkehrsstatus": "Staugefahr", "geschwindigkeit": 14}},
        {"geometry": {"type": "LineString", "coordinates": [[7.1, 50.7], [7.2, 50.8]]},
         "properties": {"verkehrsstatus": "erhöhte Verkehrsbelastung", "geschwindigkeit": 0}},
        {"geometry": {"type": "LineString", "coordinates": [[7.1, 50.7], [7.2, 50.8]]},
         "properties": {"verkehrsstatus": "aktuell nicht ermittelbar"}},
    ]}

    segs = tr.bonn_fetch_flow(lambda _url: data)

    assert [(s["level"], s["speed"]) for s in segs] == [("jam", 14), ("slow", None)]
    assert all(s["source"] == tr.SOURCE_BONN for s in segs)


def test_fetch_traffic_merges_sources_and_respects_city_flag():
    def fake_get(url: str):
        if url == tr.KOELN_FLOW_URL:
            return {"features": [{"attributes": {"auslastung": 1},
                                  "geometry": {"paths": [[[6.9, 50.9], [6.91, 50.91]]]}}]}
        if url.startswith(tr.KOELN_CALENDAR_URL[:40]):
            return {"features": [{"geometry": {"coordinates": [6.95, 50.93]},
                                  "properties": {"typ": 2, "anzeige": 1}}]}
        if url == tr.BONN_FLOW_URL:
            return None  # source down — must not break the others
        if url.endswith("/services/warning"):
            return {"warning": [{"point": "51,7", "title": "Stau"}]}
        if url.endswith("/autobahn/"):
            return {"roads": ["A1"]}
        return {}

    data = tr.fetch_traffic(bundesweit=True, nrw=False, city=True, http_get_fn=fake_get)

    assert sorted(e["source"] for e in data["events"]) == [tr.SOURCE_AUTOBAHN, tr.SOURCE_KOELN]
    assert [s["level"] for s in data["flow"]] == ["slow"]

    no_city = tr.fetch_traffic(bundesweit=True, nrw=False, city=False, http_get_fn=fake_get)
    assert [e["source"] for e in no_city["events"]] == [tr.SOURCE_AUTOBAHN]
    assert no_city["flow"] == []
