"""Display-formatters for route durations, distances, POI categories
and maneuver icon/text lookups.

Pure mapping functions — no I/O, no state. Splitting them out keeps
services.py focused on routing logic and gives the UI mixins a tighter
import target for the few helpers they actually need.
"""
from __future__ import annotations


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"


def format_distance(meters: float, units: str = "metric") -> str:
    meters = max(0.0, meters)
    if units == "imperial":
        miles = meters / 1609.344
        if miles < 0.2:
            # Below ~320 m, switch to feet rounded to a friendly 10 ft step.
            feet = meters * 3.28084
            return f"{int(round(feet / 10) * 10)} ft"
        if miles >= 10 and abs(miles - round(miles)) < 0.05:
            return f"{miles:.0f} mi"
        return f"{miles:.1f} mi"
    # metric
    if meters < 1000:
        # Show metres directly (rounded to 10 m) instead of "0.x km".
        return f"{int(round(meters / 10) * 10)} m"
    km = meters / 1000.0
    if km >= 10 and abs(km - round(km)) < 0.05:
        return f"{km:.0f} km"
    return f"{km:.1f} km"


def poi_category(tags: dict) -> str:
    amenity = tags.get("amenity", "")
    if amenity == "fuel":
        return "fuel"
    if amenity == "parking":
        return "parking"
    if amenity in {"restaurant", "fast_food", "cafe"}:
        return "food"
    if amenity in {"supermarket"} or tags.get("shop"):
        return "shop"
    if amenity in {"hospital", "pharmacy"}:
        return "medical"
    if tags.get("tourism"):
        return "tourism"
    return "other"


def maneuver_icon(maneuver_type: str, modifier: str) -> str:
    """Map an OSRM maneuver type+modifier to a bundled dp-nav-* icon name."""
    if maneuver_type == "depart":
        return "dp-nav-depart-symbolic"
    if maneuver_type == "arrive":
        return "dp-nav-arrive-symbolic"
    if maneuver_type in {"roundabout", "rotary", "roundabout turn",
                          "exit roundabout", "exit rotary"}:
        return "dp-nav-roundabout-symbolic"
    if maneuver_type == "merge":
        return "dp-nav-merge-symbolic"
    if maneuver_type in {"on ramp", "off ramp"}:
        return "dp-nav-ramp-symbolic"
    if maneuver_type == "fork":
        if modifier in {"left", "slight left", "sharp left"}:
            return "dp-nav-fork-left-symbolic"
        return "dp-nav-fork-right-symbolic"
    if modifier == "uturn":
        return "dp-nav-uturn-symbolic"
    if modifier == "sharp left":
        return "dp-nav-sharp-left-symbolic"
    if modifier == "sharp right":
        return "dp-nav-sharp-right-symbolic"
    if modifier == "slight left":
        return "dp-nav-slight-left-symbolic"
    if modifier == "slight right":
        return "dp-nav-slight-right-symbolic"
    if modifier == "left":
        return "dp-nav-left-symbolic"
    if modifier == "right":
        return "dp-nav-right-symbolic"
    return "dp-nav-straight-symbolic"


def maneuver_text_key(maneuver_type: str, modifier: str) -> str:
    """Map an OSRM maneuver to a translation key."""
    if maneuver_type == "depart":
        return "map.maneuver.depart"
    if maneuver_type == "arrive":
        return "map.maneuver.arrive"
    if maneuver_type in {"roundabout", "rotary", "roundabout turn"}:
        return "map.maneuver.roundabout"
    if maneuver_type in {"exit roundabout", "exit rotary"}:
        return "map.maneuver.exit_roundabout"
    if maneuver_type == "merge":
        return "map.maneuver.merge"
    if maneuver_type == "fork":
        if modifier in {"left", "slight left", "sharp left"}:
            return "map.maneuver.fork.left"
        return "map.maneuver.fork.right"
    if maneuver_type == "on ramp":
        return "map.maneuver.on_ramp"
    if maneuver_type == "off ramp":
        return "map.maneuver.off_ramp"
    if modifier == "uturn":
        return "map.maneuver.uturn"
    if modifier == "sharp left":
        return "map.maneuver.turn.sharp_left"
    if modifier == "sharp right":
        return "map.maneuver.turn.sharp_right"
    if modifier == "slight left":
        return "map.maneuver.turn.slight_left"
    if modifier == "slight right":
        return "map.maneuver.turn.slight_right"
    if modifier == "left":
        return "map.maneuver.turn.left"
    if modifier == "right":
        return "map.maneuver.turn.right"
    return "map.maneuver.straight"
