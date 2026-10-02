import json
import zipfile

import pytest

from drivepulse_app.map._geo_import import (
    MAX_WAYPOINTS,
    GeoImportError,
    import_file,
    parse_point,
)
from drivepulse_app.map._geocoding import geocode

GPX_ROUTE = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <rte><name>Alpenrunde</name>
    <rtept lat="48.1" lon="11.5"/><rtept lat="47.9" lon="11.2"/><rtept lat="47.5" lon="11.0"/>
  </rte>
</gpx>"""

KML_LINE = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Seetour</name>
<Placemark><LineString><coordinates>11.5,48.1,0 11.4,48.0,0</coordinates></LineString></Placemark>
</Document></kml>"""


def test_gpx_route_keeps_points_and_name(tmp_path):
    f = tmp_path / "x.gpx"
    f.write_text(GPX_ROUTE)
    tour = import_file(f)
    assert tour.name == "Alpenrunde"
    assert tour.waypoints == ["48.100000, 11.500000", "47.900000, 11.200000", "47.500000, 11.000000"]


def test_gpx_track_is_thinned(tmp_path):
    pts = "".join(f'<trkpt lat="{48 + i * 0.001}" lon="{11 + (i % 2) * 0.0001}"/>' for i in range(500))
    f = tmp_path / "track.gpx"
    f.write_text(f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{pts}</trkseg></trk></gpx>')
    tour = import_file(f)
    assert tour.name == "track"
    assert 2 <= len(tour.waypoints) <= MAX_WAYPOINTS
    assert tour.waypoints[0] == "48.000000, 11.000000"


def test_kml_and_kmz(tmp_path):
    f = tmp_path / "a.kml"
    f.write_text(KML_LINE)
    assert import_file(f).waypoints[0] == "48.100000, 11.500000"
    z = tmp_path / "a.kmz"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("doc.kml", KML_LINE)
    tour = import_file(z)
    assert tour.name == "Seetour"
    assert tour.waypoints[-1] == "48.000000, 11.400000"


def test_geojson_points(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.5, 48.1]}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.4, 48.0]}},
    ]}
    f = tmp_path / "p.geojson"
    f.write_text(json.dumps(fc))
    assert import_file(f).waypoints == ["48.100000, 11.500000", "48.000000, 11.400000"]


def test_csv_with_header_and_semicolon(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("name;lat;lon\nA;48.1;11.5\nB;48.0;11.4\n")
    assert import_file(f).waypoints == ["48.100000, 11.500000", "48.000000, 11.400000"]


def test_too_few_points_and_garbage_raise(tmp_path):
    f = tmp_path / "one.csv"
    f.write_text("48.1,11.5\n")
    with pytest.raises(GeoImportError):
        import_file(f)
    g = tmp_path / "bad.gpx"
    g.write_text("not xml")
    with pytest.raises(GeoImportError):
        import_file(g)


def test_geocode_short_circuits_coordinates():
    def _no_network(_url):
        raise AssertionError("must not hit Nominatim")

    assert geocode("48.100000, 11.500000", _no_network) == (48.1, 11.5)
    assert parse_point("Marienplatz, München") is None
