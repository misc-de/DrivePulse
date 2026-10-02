"""Import tours from common geodata files (GPX, KML, KMZ, GeoJSON, CSV).

Every parser boils the file down to an ordered list of (lat, lon) points plus
an optional name. Route-like data (GPX ``rtept``, waypoint lists) is kept
as-is; dense tracks (GPX ``trkpt``, KML/GeoJSON lines) are thinned to their
turn points via :func:`extract_turn_waypoints` so the tour stays editable in
the waypoint entry rows and within the router's waypoint limit.

The resulting waypoints are stored as ``"lat, lon"`` text, which
:func:`drivepulse_app.map._geocoding.geocode` resolves without a network
round-trip.
"""
from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from drivepulse_app.map._tour_pipeline import extract_turn_waypoints

MAX_WAYPOINTS = 25

SUPPORTED_PATTERNS = (
    "*.gpx", "*.kml", "*.kmz", "*.geojson", "*.json", "*.csv",
)
SUPPORTED_MIME_TYPES = (
    "application/gpx+xml",
    "application/vnd.google-earth.kml+xml",
    "application/vnd.google-earth.kmz",
    "application/geo+json",
    "text/csv",
)


class GeoImportError(ValueError):
    """The file could not be parsed or contained no usable coordinates."""


@dataclass
class ImportedTour:
    name: str
    waypoints: list[str]


def format_point(lat: float, lon: float) -> str:
    return f"{lat:.6f}, {lon:.6f}"


def parse_point(text: str) -> tuple[float, float] | None:
    """Parse ``"lat, lon"`` (as written by :func:`format_point`)."""
    parts = text.replace(";", ",").split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def import_file(path: Path) -> ImportedTour:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".kmz":
        data = _unzip_kml(data)
        suffix = ".kml"

    if suffix == ".gpx":
        name, points, dense = _parse_gpx(data)
    elif suffix == ".kml":
        name, points, dense = _parse_kml(data)
    elif suffix in {".geojson", ".json"}:
        name, points, dense = _parse_geojson(data)
    elif suffix == ".csv":
        name, points, dense = _parse_csv(data)
    else:
        raise GeoImportError(f"unsupported file type: {suffix}")

    points = _valid_points(points)
    if len(points) < 2:
        raise GeoImportError("need at least two coordinates")
    if dense or len(points) > MAX_WAYPOINTS:
        points = extract_turn_waypoints(
            [[lon, lat] for lat, lon in points], max_waypoints=MAX_WAYPOINTS
        )
    return ImportedTour(
        name=(name or path.stem).strip(),
        waypoints=[format_point(lat, lon) for lat, lon in points],
    )


def _valid_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lat, lon in points:
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        if out and out[-1] == (lat, lon):
            continue
        out.append((lat, lon))
    return out


# ---------------------------------------------------------------- XML helpers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_root(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise GeoImportError(f"invalid XML: {exc}") from exc


def _child_text(elem: ET.Element, name: str) -> str | None:
    for child in elem:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


# ---------------------------------------------------------------- GPX


def _parse_gpx(data: bytes) -> tuple[str | None, list[tuple[float, float]], bool]:
    root = _xml_root(data)
    groups: dict[str, list[ET.Element]] = {"rte": [], "trk": [], "wpt": []}
    for elem in root:
        tag = _local(elem.tag)
        if tag in groups:
            groups[tag].append(elem)

    def _pts(elems: list[ET.Element], pt_tag: str) -> list[tuple[float, float]]:
        out = []
        for e in elems:
            for node in e.iter():
                if _local(node.tag) != pt_tag:
                    continue
                try:
                    out.append((float(node.attrib["lat"]), float(node.attrib["lon"])))
                except (KeyError, ValueError):
                    continue
        return out

    meta_name = None
    for elem in root:
        if _local(elem.tag) == "metadata":
            meta_name = _child_text(elem, "name")

    # Preference: planned route > waypoint list > recorded track.
    rte = _pts(groups["rte"], "rtept")
    if len(rte) >= 2:
        return _child_text(groups["rte"][0], "name") or meta_name, rte, False
    wpt = _pts(groups["wpt"], "wpt")
    trk = _pts(groups["trk"], "trkpt")
    if len(trk) >= 2:
        return _child_text(groups["trk"][0], "name") or meta_name, trk, True
    return meta_name, wpt, False


# ---------------------------------------------------------------- KML / KMZ


def _unzip_kml(data: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise GeoImportError("KMZ contains no KML file")
            preferred = "doc.kml" if "doc.kml" in kml_names else kml_names[0]
            return zf.read(preferred)
    except zipfile.BadZipFile as exc:
        raise GeoImportError("invalid KMZ archive") from exc


def _kml_coords(text: str) -> list[tuple[float, float]]:
    out = []
    for tup in text.split():
        parts = tup.split(",")
        if len(parts) < 2:
            continue
        try:
            out.append((float(parts[1]), float(parts[0])))
        except ValueError:
            continue
    return out


def _parse_kml(data: bytes) -> tuple[str | None, list[tuple[float, float]], bool]:
    root = _xml_root(data)
    doc_name = None
    for elem in root.iter():
        if _local(elem.tag) == "Document":
            doc_name = _child_text(elem, "name")
            break

    line: list[tuple[float, float]] = []
    points: list[tuple[float, float]] = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "LineString":
            coords = _child_text(elem, "coordinates")
            if coords:
                line.extend(_kml_coords(coords))
        elif tag == "Point":
            coords = _child_text(elem, "coordinates")
            if coords:
                points.extend(_kml_coords(coords)[:1])
        elif tag == "Track":  # gx:Track
            for node in elem:
                if _local(node.tag) == "coord" and node.text:
                    parts = node.text.split()
                    try:
                        line.append((float(parts[1]), float(parts[0])))
                    except (IndexError, ValueError):
                        continue

    if len(line) >= 2:
        return doc_name, line, True
    return doc_name, points, False


# ---------------------------------------------------------------- GeoJSON


def _parse_geojson(data: bytes) -> tuple[str | None, list[tuple[float, float]], bool]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeoImportError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise GeoImportError("not a GeoJSON object")

    name = None
    props = obj.get("properties")
    if isinstance(props, dict) and isinstance(props.get("name"), str):
        name = props["name"]
    if name is None and isinstance(obj.get("name"), str):
        name = obj["name"]

    line: list[tuple[float, float]] = []
    points: list[tuple[float, float]] = []

    def _pt(c: object) -> tuple[float, float] | None:
        if isinstance(c, list) and len(c) >= 2:
            try:
                return float(c[1]), float(c[0])
            except (TypeError, ValueError):
                return None
        return None

    def _walk_geometry(geom: object) -> None:
        if not isinstance(geom, dict):
            return
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Point":
            p = _pt(coords)
            if p:
                points.append(p)
        elif gtype == "MultiPoint" and isinstance(coords, list):
            points.extend(p for p in map(_pt, coords) if p)
        elif gtype == "LineString" and isinstance(coords, list):
            line.extend(p for p in map(_pt, coords) if p)
        elif gtype == "MultiLineString" and isinstance(coords, list):
            for part in coords:
                if isinstance(part, list):
                    line.extend(p for p in map(_pt, part) if p)
        elif gtype == "GeometryCollection":
            for g in geom.get("geometries") or []:
                _walk_geometry(g)

    def _walk(o: dict) -> None:
        otype = o.get("type")
        if otype == "FeatureCollection":
            for f in o.get("features") or []:
                if isinstance(f, dict):
                    _walk(f)
        elif otype == "Feature":
            _walk_geometry(o.get("geometry"))
        else:
            _walk_geometry(o)

    _walk(obj)
    if len(line) >= 2:
        return name, line, True
    return name, points, False


# ---------------------------------------------------------------- CSV


_LAT_KEYS = {"lat", "latitude", "breite", "breitengrad", "y"}
_LON_KEYS = {"lon", "lng", "long", "longitude", "länge", "laenge", "längengrad", "x"}


def _parse_csv(data: bytes) -> tuple[str | None, list[tuple[float, float]], bool]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise GeoImportError("empty CSV")

    header = [c.strip().lower() for c in rows[0]]
    lat_i = next((i for i, h in enumerate(header) if h in _LAT_KEYS), None)
    lon_i = next((i for i, h in enumerate(header) if h in _LON_KEYS), None)
    if lat_i is not None and lon_i is not None:
        body = rows[1:]
    else:
        # No recognisable header: assume "lat, lon" in the first two columns.
        lat_i, lon_i = 0, 1
        body = rows

    points = []
    for r in body:
        try:
            points.append((float(r[lat_i].strip()), float(r[lon_i].strip())))
        except (IndexError, ValueError):
            continue
    return None, points, len(points) > MAX_WAYPOINTS
