"""Reachable and the two one-to-many paths (§4.12, §4.3, §4.13).

``one`` is the origin of a one-to-many search and the destination of a
many-to-one search. That single fact drives three things here:

* the naive-clock frame is the **``one`` endpoint's zone** in both directions
  (§4.12 table), resolved through §4.3 with its one-request budget;
* ``one_role`` labels which of those two roles the caller asked for;
* the request spelling follows each pinned endpoint: ``one`` is a comma pair for
  ``/api/v6/one-to-all`` and a semicolon pair for both one-to-many endpoints,
  whose ``many`` value is the comma-joined semicolon pairs.

Output contracts are index contracts. ``transit_one_to_many`` and
``transit_one_to_many_transit`` preserve every requested destination index and
require each destination-indexed array to have exactly the requested length; a
mismatch is ``malformed_upstream`` / ``unexpected_shape``, never a shorter,
padded or re-ordered list. A missing required Pareto member is malformed, never
a fabricated zero. ``transit_one_to_many`` has no clock at all, so it neither
accepts nor reports a time frame.

The public functions never raise a classified failure: they return the constant
error shape from :mod:`transit_core.errors`.
"""

import math

from . import durations, errors, http, places, timeparse, timezones

MAX_DESTINATIONS = 50
DEFAULT_MAX_SECONDS = 3600
DEFAULT_MAX_TIME_MIN = 60
DEFAULT_MAX_TRANSFERS = 5
DEFAULT_MAX_TRAVEL_MINUTES = 60
DEFAULT_WALK_MINUTES = 15
STREET_PROFILES = ("WALK", "BIKE", "CAR")
DEFAULT_TRANSIT_MODES = ("TRANSIT",)

#: The pinned ``Mode`` members that denote transit service (§4.4.1), including the
#: deprecated aliases the provider still understands. This is the **request**
#: vocabulary; :data:`transit_core.durations.TRANSIT_MODES` is the response table.
ACCEPTED_TRANSIT_MODES = frozenset(
    {
        "TRANSIT", "TRAM", "SUBWAY", "METRO", "BUS", "COACH", "RAIL",
        "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL",
        "REGIONAL_RAIL", "SUBURBAN", "FUNICULAR", "AERIAL_LIFT", "AREAL_LIFT",
        "CABLE_CAR", "FERRY", "AIRPLANE", "OTHER", "ODM", "RIDE_SHARING", "FLEX",
    }
)

#: Street modes the transit-mode argument must reject with ``street_mode_not_transit``.
STREET_REQUEST_MODES = frozenset(
    {"WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF"}
)


# ── local helpers ───────────────────────────────────────────────────


def _run(builder):
    try:
        return builder()
    except errors.ToolError as exc:
        return exc.to_result()


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)[:120]


def _fail_value(name, message, value=None):
    details = {"argument": name}
    if value is not None:
        details["value"] = _json_safe(value)
    errors.fail(errors.VALUE_OUT_OF_RANGE, message, details)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:                      # an int outside the double range
        return False


def _number(value, name, minimum, maximum):
    if not _is_finite_number(value):
        _fail_value(name, f"{name} must be a finite number", value)
    if value < minimum or value > maximum:
        _fail_value(name, f"{name} must be between {minimum} and {maximum}", value)
    return value


def _coordinate(value, name, minimum, maximum):
    return _number(value, name, minimum, maximum)


def _int_in_range(value, name, minimum, maximum, default=None):
    if value is None:
        return default
    if not _is_int(value):
        _fail_value(name, f"{name} must be an integer", value)
    if value < minimum or value > maximum:
        _fail_value(name, f"{name} must be between {minimum} and {maximum}", value)
    return value


def _boolean(value, name, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        _fail_value(name, f"{name} must be a boolean", value)
    return value


def _timezone_arg(name, value):
    if value is None:
        return None
    return timezones.resolve_zone_name(value)


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _number_text(value):
    return str(value)


def _pair(lat, lon, separator):
    return f"{_number_text(lat)}{separator}{_number_text(lon)}"


def _destinations(value):
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        _fail_value("destinations", "destinations must be an array of [lat, lon] pairs")
    if not value:
        _fail_value("destinations", "destinations must not be empty")
    if len(value) > MAX_DESTINATIONS:
        _fail_value(
            "destinations",
            f"destinations accepts at most {MAX_DESTINATIONS} entries",
            len(value),
        )
    checked = []
    for index, entry in enumerate(value):
        if (
            not isinstance(entry, (list, tuple))
            or isinstance(entry, (str, bytes))
            or len(entry) != 2
        ):
            _fail_value(
                "destinations",
                "each destination must be a [lat, lon] pair",
                {"index": index, "value": _json_safe(entry)},
            )
        lat = _coordinate(entry[0], f"destinations[{index}].lat", -90, 90)
        lon = _coordinate(entry[1], f"destinations[{index}].lon", -180, 180)
        checked.append((lat, lon))
    return checked


def _request_modes(value):
    if value is None:
        return list(DEFAULT_TRANSIT_MODES)
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        _fail_value("transit_modes", "transit_modes must be an array of mode names")
    if not value:
        # http.encode_params drops an empty list, so an empty request would silently
        # become the default TRANSIT query. Reject instead of changing the meaning.
        _fail_value("transit_modes", "transit_modes must not be empty")
    modes = list(value)
    for mode in modes:
        if not isinstance(mode, str):
            _fail_value("transit_modes", "every transit mode must be a string", mode)
        if mode.startswith("DEBUG_") or mode in STREET_REQUEST_MODES:
            errors.fail(
                errors.STREET_MODE_NOT_TRANSIT,
                f"{mode!r} is a street mode, not a transit mode",
                {"argument": "transit_modes", "value": mode},
            )
        if mode not in ACCEPTED_TRANSIT_MODES:
            errors.fail(
                errors.UNKNOWN_MODE,
                f"unknown transit mode {mode!r}",
                {"argument": "transit_modes", "value": mode},
            )
    return modes


def _one_role(arrive_by):
    return "destination" if arrive_by else "origin"


def _frame_for(parsed, *, control, timezone, endpoint_query, transport, now):
    """The ``TimeFrame`` for a naive clock, plus its resolution when discovered."""
    if parsed.needs_frame:
        resolution = None
        if timezone is not None:
            choice = timeparse.FrameChoice(timezone, timezones.CALLER_TIMEZONE)
        else:
            resolution = timezones.resolve_endpoint_zone(endpoint_query, transport=transport)
            choice = timeparse.FrameChoice(resolution.zone, resolution.source)
        frame = timeparse.interpret(
            parsed, control=control, frame=choice, display_zone=choice.zone, now=now
        )
        return frame, None if timezone is not None else resolution

    display_zone = timezone
    source = timezones.CALLER_TIMEZONE if timezone is not None else timezones.NOT_APPLICABLE
    if display_zone is None and endpoint_query is not None:
        display_zone = endpoint_query.declared_zone
        source = (
            timezones.CALLER_ENDPOINT
            if display_zone is not None
            else timezones.NOT_APPLICABLE
        )
    choice = timeparse.FrameChoice(display_zone, source) if display_zone is not None else None
    return (
        timeparse.interpret(
            parsed, control=control, frame=choice, display_zone=display_zone, now=now
        ),
        None,
    )


def _optional_duration(value, member, index):
    if value is None:
        return None
    if not _is_finite_number(value) or value < 0:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} is not a non-negative finite number",
            {"index": index, "member": member},
        )
    return value


def _optional_int(value, member, index):
    if value is None:
        return None
    if not _is_int(value) or value < 0:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} is not a non-negative integer",
            {"index": index, "member": member},
        )
    return value


# ── §4.12.1 reachable ───────────────────────────────────────────────


def reachable(
    lat,
    lon,
    *,
    max_time_min=DEFAULT_MAX_TIME_MIN,
    time=None,
    arrive_by=False,
    timezone=None,
    endpoint_timezone=None,
    transport=None,
    now=None,
):
    """All places reachable from one coordinate within a time budget."""
    return _run(
        lambda: _reachable(
            lat,
            lon,
            max_time_min=max_time_min,
            time=time,
            arrive_by=arrive_by,
            timezone=timezone,
            endpoint_timezone=endpoint_timezone,
            transport=transport,
            now=now,
        )
    )


def _reachable(
    lat, lon, *, max_time_min, time, arrive_by, timezone, endpoint_timezone, transport, now
):
    lat = _coordinate(lat, "lat", -90, 90)
    lon = _coordinate(lon, "lon", -180, 180)
    max_time_min = _int_in_range(max_time_min, "max_time_min", 1, 1440, DEFAULT_MAX_TIME_MIN)
    arrive_by = _boolean(arrive_by, "arrive_by", False)
    control = timeparse.CONTROL_ARRIVE_BY if arrive_by else timeparse.CONTROL_DEPART_AFTER
    parsed = timeparse.classify_syntax(time)
    caller_timezone = _timezone_arg("timezone", timezone)
    declared = _timezone_arg("endpoint_timezone", endpoint_timezone)

    endpoint = timezones.EndpointQuery.coordinate("one", lat, lon, declared_zone=declared)
    frame, resolution = _frame_for(
        parsed,
        control=control,
        timezone=caller_timezone,
        endpoint_query=endpoint,
        transport=transport,
        now=now,
    )

    data = http.api_get(
        "/api/v6/one-to-all",
        {
            "one": _pair(lat, lon, ","),
            "maxTravelTime": max_time_min,
            "time": frame.wire_value,
            "arriveBy": arrive_by,
        },
        transport=transport,
    )
    if not isinstance(data, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "one-to-all response is not an object", {})
    raw_one = data.get("one")
    if not isinstance(raw_one, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "one-to-all response has no one place object",
            {"member": "one"},
        )
    _valid_place(raw_one, "one")
    places_list = data.get("all")
    if not isinstance(places_list, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "one-to-all response has no all array",
            {"member": "all"},
        )
    if resolution is not None:
        timezones.check_zone_agreement(
            resolution,
            raw_one.get("tz") if isinstance(raw_one.get("tz"), str) else None,
            endpoint_label="one",
        )

    entries = []
    for index, item in enumerate(places_list):
        if not isinstance(item, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "reachable entry is not an object",
                {"index": index},
            )
        raw_place = item.get("place")
        if not isinstance(raw_place, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "reachable entry has no place object",
                {"index": index, "member": "place"},
            )
        _valid_place(raw_place, "place", index=index)
        entries.append(
            {
                "name": raw_place.get("name"),
                "lat": raw_place.get("lat"),
                "lon": raw_place.get("lon"),
                "travel_time_seconds": _optional_int(item.get("duration"), "duration", index),
                "transfers_k": _optional_int(item.get("k"), "k", index),
            }
        )

    outcome = errors.NO_ROUTES if not entries else errors.OK
    return errors.success_result(
        outcome,
        time_frame=frame.to_object(),
        query={
            "one": _pair(lat, lon, ","),
            "max_time_min": max_time_min,
            "control": control,
            "applied_frame_zone": frame.frame_zone,
            "frame_source": frame.frame_source,
            "one_role": _one_role(arrive_by),
        },
        one=places.map_place(raw_one),
        entries=entries,
    )


def _valid_place(raw, member, index=None):
    if _text(raw.get("name")) is None:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} has no non-empty name",
            {"member": member, "index": index},
        )
    lat, lon = raw.get("lat"), raw.get("lon")
    if not _is_finite_number(lat) or not _is_finite_number(lon):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} has no numeric coordinate",
            {"member": member, "index": index},
        )
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} coordinate is out of range",
            {"member": member, "index": index},
        )


# ── §4.12.2 one-to-many (street) ────────────────────────────────────


def one_to_many(
    from_lat,
    from_lon,
    destinations,
    *,
    mode="WALK",
    max_sec=DEFAULT_MAX_SECONDS,
    arrive_by=False,
    transport=None,
):
    """Street durations from one coordinate to every requested destination.

    Takes no clock: street durations depend on none, so no frame is accepted or
    reported (§4.12).
    """
    return _run(
        lambda: _one_to_many(
            from_lat,
            from_lon,
            destinations,
            mode=mode,
            max_sec=max_sec,
            arrive_by=arrive_by,
            transport=transport,
        )
    )


def _one_to_many(from_lat, from_lon, destinations, *, mode, max_sec, arrive_by, transport):
    from_lat = _coordinate(from_lat, "from_lat", -90, 90)
    from_lon = _coordinate(from_lon, "from_lon", -180, 180)
    destinations = _destinations(destinations)
    if not isinstance(mode, str) or mode not in STREET_PROFILES:
        errors.fail(
            errors.UNKNOWN_MODE,
            f"unknown street mode {mode!r}; expected WALK, BIKE or CAR",
            {"argument": "mode", "value": _json_safe(mode)},
        )
    if max_sec is None:
        max_sec = DEFAULT_MAX_SECONDS
    if not _is_finite_number(max_sec) or max_sec < 0:
        _fail_value("max_sec", "max_sec must be a finite non-negative number", max_sec)
    arrive_by = _boolean(arrive_by, "arrive_by", False)

    one = _pair(from_lat, from_lon, ";")
    data = http.api_get(
        "/api/v1/one-to-many",
        {
            "one": one,
            "many": ",".join(_pair(lat, lon, ";") for lat, lon in destinations),
            "mode": mode,
            "max": max_sec,
            "maxMatchingDistance": 25,
            "arriveBy": arrive_by,
        },
        transport=transport,
    )

    if not isinstance(data, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "one-to-many response is not an array",
            {"member": "durations"},
        )
    if len(data) != len(destinations):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "one-to-many response length does not match the requested destinations",
            {"expected": len(destinations), "actual": len(data)},
        )

    results = []
    for index, (lat, lon) in enumerate(destinations):
        entry = data[index]
        if not isinstance(entry, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "one-to-many entry is not an object",
                {"index": index},
            )
        results.append(
            {
                "lat": lat,
                "lon": lon,
                "duration_seconds": _optional_duration(entry.get("duration"), "duration", index),
                "distance_meters": _optional_duration(entry.get("distance"), "distance", index),
            }
        )

    outcome = (
        errors.NO_ROUTES
        if all(row["duration_seconds"] is None for row in results)
        else errors.OK
    )
    return errors.success_result(
        outcome,
        query={
            "one": one,
            "destination_count": len(destinations),
            "mode": mode,
            "max_sec": max_sec,
            "arrive_by": arrive_by,
            "one_role": _one_role(arrive_by),
        },
        results=results,
    )


# ── §4.12.3 one-to-many transit (intermodal) ────────────────────────


def one_to_many_transit(
    from_lat,
    from_lon,
    destinations,
    *,
    time=None,
    arrive_by=False,
    max_transfers=DEFAULT_MAX_TRANSFERS,
    transit_modes=None,
    max_access_walk_minutes=DEFAULT_WALK_MINUTES,
    max_egress_walk_minutes=DEFAULT_WALK_MINUTES,
    max_travel_minutes=DEFAULT_MAX_TRAVEL_MINUTES,
    timezone=None,
    endpoint_timezone=None,
    transport=None,
    now=None,
):
    """Street duration plus the transit Pareto set for every destination."""
    return _run(
        lambda: _one_to_many_transit(
            from_lat,
            from_lon,
            destinations,
            time=time,
            arrive_by=arrive_by,
            max_transfers=max_transfers,
            transit_modes=transit_modes,
            max_access_walk_minutes=max_access_walk_minutes,
            max_egress_walk_minutes=max_egress_walk_minutes,
            max_travel_minutes=max_travel_minutes,
            timezone=timezone,
            endpoint_timezone=endpoint_timezone,
            transport=transport,
            now=now,
        )
    )


def _one_to_many_transit(
    from_lat,
    from_lon,
    destinations,
    *,
    time,
    arrive_by,
    max_transfers,
    transit_modes,
    max_access_walk_minutes,
    max_egress_walk_minutes,
    max_travel_minutes,
    timezone,
    endpoint_timezone,
    transport,
    now,
):
    from_lat = _coordinate(from_lat, "from_lat", -90, 90)
    from_lon = _coordinate(from_lon, "from_lon", -180, 180)
    destinations = _destinations(destinations)
    arrive_by = _boolean(arrive_by, "arrive_by", False)
    max_transfers = _int_in_range(
        max_transfers, "max_transfers", 0, 10, DEFAULT_MAX_TRANSFERS
    )
    max_access_walk_minutes = _int_in_range(
        max_access_walk_minutes, "max_access_walk_minutes", 0, 120, DEFAULT_WALK_MINUTES
    )
    max_egress_walk_minutes = _int_in_range(
        max_egress_walk_minutes, "max_egress_walk_minutes", 0, 120, DEFAULT_WALK_MINUTES
    )
    max_travel_minutes = _int_in_range(
        max_travel_minutes, "max_travel_minutes", 1, 1440, DEFAULT_MAX_TRAVEL_MINUTES
    )
    modes = _request_modes(transit_modes)
    control = timeparse.CONTROL_ARRIVE_BY if arrive_by else timeparse.CONTROL_DEPART_AFTER
    parsed = timeparse.classify_syntax(time)
    caller_timezone = _timezone_arg("timezone", timezone)
    declared = _timezone_arg("endpoint_timezone", endpoint_timezone)

    endpoint = timezones.EndpointQuery.coordinate(
        "one", from_lat, from_lon, declared_zone=declared
    )
    frame, _resolution = _frame_for(
        parsed,
        control=control,
        timezone=caller_timezone,
        endpoint_query=endpoint,
        transport=transport,
        now=now,
    )

    data = http.api_get(
        "/api/experimental/one-to-many-intermodal",
        {
            "one": _pair(from_lat, from_lon, ";"),
            "many": ",".join(_pair(lat, lon, ";") for lat, lon in destinations),
            "time": frame.wire_value,
            "maxTravelTime": max_travel_minutes,
            "maxMatchingDistance": 25,
            "maxTransfers": max_transfers,
            "transitModes": modes,
            "maxPreTransitTime": max_access_walk_minutes * 60,
            "maxPostTransitTime": max_egress_walk_minutes * 60,
            "arriveBy": arrive_by,
        },
        transport=transport,
    )

    street_sets = _indexed_array(data, "street_durations", len(destinations))
    transit_sets = _indexed_array(data, "transit_durations", len(destinations))

    results = []
    any_street = False
    any_transit = False
    for index, (lat, lon) in enumerate(destinations):
        street = street_sets[index]
        if not isinstance(street, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "street_durations entry is not an object",
                {"index": index, "member": "street_durations"},
            )
        street_duration = _optional_duration(street.get("duration"), "duration", index)
        street_distance = _optional_duration(street.get("distance"), "distance", index)
        if street_duration is not None:
            any_street = True

        pareto = transit_sets[index]
        if not isinstance(pareto, list):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "transit_durations entry is not an array",
                {"index": index, "member": "transit_durations"},
            )
        transit = []
        for entry_index, entry in enumerate(pareto):
            if not isinstance(entry, dict):
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    "ParetoSetEntry is not an object",
                    {"index": index, "entry": entry_index},
                )
            # Both members are required by the pinned schema: never default them.
            if "duration" not in entry:
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    "ParetoSetEntry.duration is required and was absent",
                    {"index": index, "entry": entry_index, "member": "duration"},
                )
            if "transfers" not in entry:
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    "ParetoSetEntry.transfers is required and was absent",
                    {"index": index, "entry": entry_index, "member": "transfers"},
                )
            duration = entry["duration"]
            transfers = entry["transfers"]
            if not _is_finite_number(duration) or duration < 0:
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    "ParetoSetEntry.duration is not a non-negative number",
                    {"index": index, "entry": entry_index, "member": "duration"},
                )
            if not _is_int(transfers) or transfers < 0:
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    "ParetoSetEntry.transfers is not a non-negative integer",
                    {"index": index, "entry": entry_index, "member": "transfers"},
                )
            transit.append({"duration_seconds": duration, "transfers": transfers})
        if transit:
            any_transit = True

        results.append(
            {
                "lat": lat,
                "lon": lon,
                "street_duration_seconds": street_duration,
                "street_distance_meters": street_distance,
                "transit": transit,
            }
        )

    outcome = errors.OK if (any_street or any_transit) else errors.NO_ROUTES
    return errors.success_result(
        outcome,
        time_frame=frame.to_object(),
        query={
            "one": _pair(from_lat, from_lon, ";"),
            "destination_count": len(destinations),
            "max_travel_minutes": max_travel_minutes,
            "applied_modes": modes,
            "control": control,
            "one_role": _one_role(arrive_by),
        },
        results=results,
    )


def _indexed_array(data, member, expected):
    if not isinstance(data, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "intermodal response is not an object", {})
    value = data.get(member)
    if not isinstance(value, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"intermodal response has no {member} array",
            {"member": member},
        )
    if len(value) != expected:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{member} length does not match the requested destinations",
            {"member": member, "expected": expected, "actual": len(value)},
        )
    return value


__all__ = [
    "MAX_DESTINATIONS",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MAX_TIME_MIN",
    "DEFAULT_MAX_TRANSFERS",
    "DEFAULT_MAX_TRAVEL_MINUTES",
    "DEFAULT_WALK_MINUTES",
    "STREET_PROFILES",
    "ACCEPTED_TRANSIT_MODES",
    "STREET_REQUEST_MODES",
    "reachable",
    "one_to_many",
    "one_to_many_transit",
]
