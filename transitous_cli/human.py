"""Human rendering for the native CLI.

One lossy projection per operation, built from the same result object the JSON
mode serialises.  Nothing here re-parses times in the host zone, rounds a number
to zero, drops a returned row or adds a second display cap: every returned
itinerary, leg, row and alternative is printed in backend order, and an unknown
value is printed as ``unknown`` rather than false or zero.  Canonical timestamp
objects are printed with their own ``display`` string, which already names the
zone/offset provenance.
"""

import json

UNKNOWN = "unknown"


def _ts(value):
    if isinstance(value, dict):
        display = value.get("display")
        if isinstance(display, str) and display:
            return display
        iso = value.get("iso")
        if isinstance(iso, str) and iso:
            return iso
    return UNKNOWN


def _scalar(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _duration(seconds):
    if seconds is None:
        return UNKNOWN
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return _scalar(seconds)
    if isinstance(seconds, float) and seconds.is_integer():
        seconds = int(seconds)
    return f"{seconds} s"


def _place(place):
    if not isinstance(place, dict):
        return UNKNOWN
    name = place.get("name")
    name = name if isinstance(name, str) and name else UNKNOWN
    bits = [name]
    if place.get("stop_id") is not None:
        bits.append(f"stop {place['stop_id']}")
    lat, lon = place.get("lat"), place.get("lon")
    if lat is not None or lon is not None:
        bits.append(f"({_scalar(lat)}, {_scalar(lon)})")
    if place.get("track"):
        bits.append(f"track {place['track']}")
    if place.get("platform"):
        bits.append(f"platform {place['platform']}")
    if place.get("zone"):
        bits.append(f"zone {place['zone']}")
    return " ".join(bits)


def _tri(value):
    return _scalar(value)


def _flags(row, keys):
    out = []
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        out.append(f"{key}={_scalar(value)}")
    return ", ".join(out)


def _line(prefix, *parts):
    return prefix + " " + " | ".join(str(p) for p in parts if p not in (None, ""))


# ── plan ────────────────────────────────────────────────────────────


def render_plan(result):
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    lines.append(f"From: {_place(result.get('from'))}")
    lines.append(f"To: {_place(result.get('to'))}")

    frame = result.get("time_frame")
    if isinstance(frame, dict):
        interpreted = frame.get("interpreted_instant")
        lines.append(
            _line("Search:", f"control {_scalar(frame.get('control'))}",
                  f"time {_ts(interpreted)}",
                  f"frame zone {_scalar(frame.get('applied_frame_zone') or frame.get('frame_zone'))}",
                  f"source {_scalar(frame.get('frame_source'))}")
        )

    query = result.get("query")
    if isinstance(query, dict):
        lines.append(
            _line("Query:", f"window {_duration(query.get('search_window_seconds'))}",
                  f"requested {_scalar(query.get('minimum_requested'))}",
                  f"returned {_scalar(query.get('returned_count'))} of {_scalar(query.get('upstream_count'))}",
                  f"max_results {_scalar(query.get('max_results'))}")
        )
        if query.get("truncated"):
            lines.append(
                f"Truncated: {_scalar(query.get('dropped_count'))} dropped "
                f"({_scalar(query.get('truncation_reason'))})"
            )
        else:
            lines.append("Truncated: no")
        modes = query.get("requested_modes")
        if isinstance(modes, list) and modes:
            lines.append(f"Requested modes: {', '.join(str(m) for m in modes)}")

    constraints = result.get("constraints_active")
    if isinstance(constraints, list):
        lines.append("Constraints: " + (", ".join(str(c) for c in constraints) or "none"))
    if result.get("no_routes_reason"):
        lines.append(f"No routes reason: {result['no_routes_reason']}")

    itineraries = result.get("itineraries") or []
    lines.append(f"Itineraries: {len(itineraries)}")
    for position, itinerary in enumerate(itineraries, 1):
        lines.extend(_render_itinerary(itinerary, f"Itinerary {position}"))

    direct = result.get("direct") or []
    if direct:
        lines.append(f"Direct street-only alternatives: {len(direct)}")
        for position, alternative in enumerate(direct, 1):
            lines.extend(
                _render_itinerary(alternative, f"Direct alternative {position}")
            )

    continuation = result.get("continuation")
    if isinstance(continuation, dict):
        previous = continuation.get("previous")
        following = continuation.get("next")
        lines.append(
            "Continuation: previous "
            + ("available" if isinstance(previous, dict) else "none")
            + " | next "
            + ("available" if isinstance(following, dict) else "none")
        )
        lines.append(
            "  To continue, save the desired previous/next object (for example "
            "`.continuation.next`) and pass that object to `plan --continuation FILE`. "
            "A max-results cut does not guarantee a provider next cursor."
        )

    lines.append(
        "Note: this is a bounded search context in upstream order, not a whole-day optimum."
    )
    return "\n".join(lines)


def _render_itinerary(itinerary, title):
    if not isinstance(itinerary, dict):
        return [f"{title}: {_scalar(itinerary)}"]
    lines = [
        _line(
            f"{title}:",
            f"id {_scalar(itinerary.get('itinerary_id'))}",
            f"{_ts(itinerary.get('departure'))} -> {_ts(itinerary.get('arrival'))}",
            f"elapsed {_duration(itinerary.get('elapsed_seconds'))}",
            f"changes {_tri(itinerary.get('changes'))}",
            f"legs {_scalar(itinerary.get('transit_leg_count'))} transit",
        )
    ]
    if itinerary.get("arrival_date_offset_days"):
        lines.append(f"  arrives +{itinerary['arrival_date_offset_days']} day(s)")
    legs = itinerary.get("legs") or []
    for leg in legs:
        lines.extend(_render_leg(leg))
    return lines


def _render_leg(leg):
    if not isinstance(leg, dict):
        return [f"  leg: {_scalar(leg)}"]
    head = _line(
        f"  leg {_scalar(leg.get('index'))}:",
        _scalar(leg.get("mode")),
        f"{_place(leg.get('from'))} -> {_place(leg.get('to'))}",
        f"depart {_ts(leg.get('departure'))}",
        f"arrive {_ts(leg.get('arrival'))}",
        f"duration {_duration(leg.get('duration_seconds'))}",
    )
    extras = []
    if leg.get("route"):
        extras.append(f"route {leg['route']}")
    if leg.get("headsign"):
        extras.append(f"headsign {leg['headsign']}")
    if leg.get("trip_id"):
        extras.append(f"trip {leg['trip_id']}")
    if leg.get("agency_name"):
        extras.append(f"agency {leg['agency_name']}")
    extras.extend(
        part
        for part in (
            None if leg.get("realtime") is None else f"realtime={_tri(leg.get('realtime'))}",
            None if leg.get("scheduled") is None else f"scheduled={_tri(leg.get('scheduled'))}",
            None if leg.get("cancelled") is None else f"cancelled={_tri(leg.get('cancelled'))}",
        )
        if part
    )
    if leg.get("walking_route_unavailable"):
        extras.append("walking_route_unavailable=true (duration is an estimate)")
    if leg.get("intermediate_stop_count") is not None:
        extras.append(f"intermediate stops {leg['intermediate_stop_count']}")
    if leg.get("scheduled_departure") is not None:
        extras.append(f"scheduled depart {_ts(leg['scheduled_departure'])}")
    if leg.get("scheduled_arrival") is not None:
        extras.append(f"scheduled arrive {_ts(leg['scheduled_arrival'])}")
    lines = [head]
    if extras:
        lines.append("    " + " | ".join(extras))
    if leg.get("note"):
        lines.append(f"    note: {leg['note']}")
    return lines


# ── geocode / reverse / nearby ──────────────────────────────────────


def _render_match(index, match):
    if not isinstance(match, dict):
        return f"{index}. {_scalar(match)}"
    return _line(
        f"{index}.",
        _scalar(match.get("name")),
        f"[{_scalar(match.get('match_type'))}]",
        f"id {_scalar(match.get('match_id'))}",
        f"stop_id {_scalar(match.get('stop_id'))}",
        f"({_scalar(match.get('lat'))}, {_scalar(match.get('lon'))})",
    )


def render_geocode(result):
    rows = result.get("results") or []
    lines = [
        f"Outcome: {_scalar(result.get('outcome'))}",
        f"Count: {_scalar(result.get('count'))}",
    ]
    for index, match in enumerate(rows, 1):
        lines.append(_render_match(index, match))
    return "\n".join(lines)


def render_reverse_geocode(result):
    center = result.get("center")
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    if isinstance(center, list) and len(center) == 2:
        lines.append(f"Center: ({_scalar(center[0])}, {_scalar(center[1])})")
    lines.append(f"Count: {_scalar(result.get('count'))}")
    for index, match in enumerate(result.get("results") or [], 1):
        lines.append(_render_match(index, match))
    return "\n".join(lines)


def render_nearby_stops(result):
    center = result.get("center")
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    if isinstance(center, list) and len(center) == 2:
        lines.append(f"Center: ({_scalar(center[0])}, {_scalar(center[1])})")
    lines.append(f"Radius: {_scalar(result.get('radius_m'))} m")
    lines.append(f"Count: {_scalar(result.get('count'))} (truncated: {_scalar(result.get('truncated'))})")
    for index, stop in enumerate(result.get("stops") or [], 1):
        if not isinstance(stop, dict):
            lines.append(f"{index}. {_scalar(stop)}")
            continue
        modes = stop.get("modes")
        lines.append(
            _line(
                f"{index}.",
                f"{_scalar(stop.get('distance_m'))} m",
                f"stop_id {_scalar(stop.get('stop_id'))}",
                _scalar(stop.get("name")),
                f"({_scalar(stop.get('lat'))}, {_scalar(stop.get('lon'))})",
                "modes " + (", ".join(str(m) for m in modes) if isinstance(modes, list) and modes else UNKNOWN),
                f"zone {_scalar(stop.get('timezone'))}",
            )
        )
    return "\n".join(lines)


# ── departures ──────────────────────────────────────────────────────


def render_departures(result):
    frame = result.get("time_frame")
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    if isinstance(frame, dict):
        lines.append(
            _line("Search:", f"control {_scalar(frame.get('control'))}",
                  f"time {_ts(frame.get('interpreted_instant'))}",
                  f"source {_scalar(frame.get('frame_source'))}")
        )
    query = result.get("query")
    if isinstance(query, dict):
        lines.append(
            _line("Query:", f"stop {_scalar(query.get('stop_id'))}",
                  f"count {_scalar(query.get('count'))}",
                  f"window {_duration(query.get('window_seconds'))}")
        )
    lines.append(f"Returned: {_scalar(result.get('returned_count'))}")
    for index, row in enumerate(result.get("departures") or [], 1):
        if not isinstance(row, dict):
            lines.append(f"{index}. {_scalar(row)}")
            continue
        lines.append(
            _line(
                f"{index}.",
                _scalar(row.get("route")),
                f"to {_scalar(row.get('headsign'))}",
                f"mode {_scalar(row.get('mode'))}",
                f"scheduled {_ts(row.get('scheduled_departure'))}",
                f"expected {_ts(row.get('expected_departure'))}",
                f"delay {_duration(row.get('delay_seconds'))}",
            )
        )
        lines.append(
            "    "
            + _flags(row, ("realtime", "scheduled", "trip_cancelled", "stop_cancelled",
                           "trip_id", "agency_name", "route_type"))
        )
    return "\n".join(lines)


# ── trip ────────────────────────────────────────────────────────────


def render_trip(result):
    lines = [
        f"Outcome: {_scalar(result.get('outcome'))}",
        f"Trip: {_scalar(result.get('trip_id'))}",
    ]
    for leg in result.get("legs") or []:
        lines.extend(_render_leg(leg))
        if isinstance(leg, dict):
            for stop in leg.get("intermediate_stops") or []:
                if not isinstance(stop, dict):
                    lines.append(f"    stop: {_scalar(stop)}")
                    continue
                lines.append(
                    _line(
                        "    stop:",
                        _scalar(stop.get("stop_id")),
                        _scalar(stop.get("name")),
                        f"arrive {_ts(stop.get('arrival'))}",
                        f"depart {_ts(stop.get('departure'))}",
                        _flags(stop, ("track", "cancelled", "stop_cancelled")),
                    )
                )
    return "\n".join(lines)


# ── reachable ───────────────────────────────────────────────────────


def render_reachable(result):
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    query = result.get("query")
    if isinstance(query, dict):
        lines.append(
            _line("Query:", f"one {_scalar(query.get('one'))}",
                  f"role {_scalar(query.get('one_role'))}",
                  f"max_time {_scalar(query.get('max_time_min'))} min",
                  f"control {_scalar(query.get('control'))}")
        )
    lines.append(f"One: {_place(result.get('one'))}")
    for index, entry in enumerate(result.get("entries") or [], 1):
        if not isinstance(entry, dict):
            lines.append(f"{index}. {_scalar(entry)}")
            continue
        lines.append(
            _line(
                f"{index}.",
                _scalar(entry.get("name")),
                f"travel {_duration(entry.get('travel_time_seconds'))}",
                f"transfers_k {_scalar(entry.get('transfers_k'))}",
                f"({_scalar(entry.get('lat'))}, {_scalar(entry.get('lon'))})",
            )
        )
    return "\n".join(lines)


# ── one-to-many ─────────────────────────────────────────────────────


def render_one_to_many(result):
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    query = result.get("query")
    if isinstance(query, dict):
        lines.append(
            _line("Query:", f"one {_scalar(query.get('one'))}",
                  f"role {_scalar(query.get('one_role'))}",
                  f"destinations {_scalar(query.get('destination_count'))}",
                  f"mode {_scalar(query.get('mode'))}",
                  f"max_sec {_duration(query.get('max_sec'))}")
        )
    for index, row in enumerate(result.get("results") or [], 1):
        if not isinstance(row, dict):
            lines.append(f"{index}. {_scalar(row)}")
            continue
        lines.append(
            _line(
                f"{index}.",
                f"destination ({_scalar(row.get('lat'))}, {_scalar(row.get('lon'))})",
                f"duration {_duration(row.get('duration_seconds'))}",
                f"distance {_scalar(row.get('distance_meters'))} m",
            )
        )
    return "\n".join(lines)


def render_one_to_many_transit(result):
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    query = result.get("query")
    if isinstance(query, dict):
        modes = query.get("applied_modes")
        lines.append(
            _line("Query:", f"one {_scalar(query.get('one'))}",
                  f"role {_scalar(query.get('one_role'))}",
                  f"destinations {_scalar(query.get('destination_count'))}",
                  "modes " + (", ".join(str(m) for m in modes) if isinstance(modes, list) and modes else UNKNOWN),
                  f"control {_scalar(query.get('control'))}")
        )
    for index, row in enumerate(result.get("results") or [], 1):
        if not isinstance(row, dict):
            lines.append(f"{index}. {_scalar(row)}")
            continue
        lines.append(
            _line(
                f"{index}.",
                f"destination ({_scalar(row.get('lat'))}, {_scalar(row.get('lon'))})",
                f"street {_duration(row.get('street_duration_seconds'))}",
                f"{_scalar(row.get('street_distance_meters'))} m",
            )
        )
        transit = row.get("transit") or []
        if not transit:
            lines.append("    transit: none for this destination")
        for alternative in transit:
            if not isinstance(alternative, dict):
                lines.append(f"    transit: {_scalar(alternative)}")
                continue
            lines.append(
                _line("    transit:", _duration(alternative.get("duration_seconds")),
                      f"transfers {_scalar(alternative.get('transfers'))}")
            )
    return "\n".join(lines)


# ── map-trips ───────────────────────────────────────────────────────


def render_map_trips(result):
    lines = [f"Outcome: {_scalar(result.get('outcome'))}"]
    query = result.get("query")
    if isinstance(query, dict):
        bbox = query.get("bbox") if isinstance(query.get("bbox"), dict) else {}
        lines.append(
            _line("Query:", f"bbox min {_scalar(bbox.get('min'))}",
                  f"max {_scalar(bbox.get('max'))}",
                  f"zoom {_scalar(query.get('zoom'))}")
        )
    window = result.get("time_window")
    if isinstance(window, dict):
        lines.append(
            _line("Window:", f"start {_ts(window.get('start_time'))}",
                  f"end {_ts(window.get('end_time'))}")
        )
    lines.append(
        f"Returned: {_scalar(result.get('returned_count'))} of "
        f"{_scalar(result.get('upstream_count'))} "
        f"(truncated: {_scalar(result.get('truncated'))})"
    )
    for index, segment in enumerate(result.get("trips") or [], 1):
        if not isinstance(segment, dict):
            lines.append(f"{index}. {_scalar(segment)}")
            continue
        lines.append(
            _line(
                f"{index}.",
                f"trip {_scalar(segment.get('trip_id'))}",
                f"route {_scalar(segment.get('route'))}",
                f"mode {_scalar(segment.get('mode'))}",
                f"{_place(segment.get('from'))} -> {_place(segment.get('to'))}",
                f"depart {_ts(segment.get('departure'))}",
                f"arrive {_ts(segment.get('arrival'))}",
                f"distance {_scalar(segment.get('distance_meters'))} m",
                f"realtime={_scalar(segment.get('realtime'))}",
                f"polyline={_scalar(segment.get('polyline_present'))}",
            )
        )
    return "\n".join(lines)


RENDERERS = {
    "plan": render_plan,
    "geocode": render_geocode,
    "departures": render_departures,
    "nearby_stops": render_nearby_stops,
    "nearby": render_nearby_stops,
    "reverse_geocode": render_reverse_geocode,
    "trip": render_trip,
    "reachable": render_reachable,
    "one_to_many": render_one_to_many,
    "one_to_many_transit": render_one_to_many_transit,
    "map_trips": render_map_trips,
}

RAW_OPERATIONS = frozenset({"health", "refresh", "rentals"})


def render(renderer, result):
    """Human body text for one successful or no-routes result."""
    if isinstance(renderer, str) and renderer in RAW_OPERATIONS:
        return json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False)
    function = RENDERERS.get(renderer)
    if function is None:
        return json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False)
    return function(result)


__all__ = ["render", "RENDERERS", "RAW_OPERATIONS"]
