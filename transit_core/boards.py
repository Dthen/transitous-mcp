"""Departure boards, trip itineraries and map trip segments (§4.9–4.11, §4.13).

Three timetable tools with three different time questions:

* ``departures`` reads one stop's clock. A naive clock is read in **that stop's**
  zone, resolved by one bounded ``/api/v6/stop`` lookup (§4.3 step 3) and
  overridable by ``timezone`` or ``endpoint_timezone``. An explicit instant and a
  caller override need no discovery request at all.
* ``trip`` takes no time input. ``/api/v6/trip`` returns the pinned ``Itinerary``
  (there is no ``TripResponse``), so the result is the ordered leg list and each
  leg's ``intermediateStops`` are pinned ``Place`` objects.
* ``map_trips`` takes a window. A bounding box has no endpoint zone, so a naive
  window requires a caller ``timezone`` and otherwise fails with
  ``bbox_window_requires_explicit_zone`` before any request.

Honesty rules shared by all three:

* ``StopTime.realTime`` is required by the pinned schema. A stop-time body that
  omits it is ``malformed_upstream`` / ``unexpected_shape`` — never a scheduled
  service. ``realTime: false`` reports ``realtime: false``,
  ``expected_departure: null`` and keeps the supplied scheduled time. The pinned
  ``StopTime`` has no ``scheduled`` property, so a board's ``scheduled`` is
  ``null`` in both cases; it is never the negation of ``realTime``.
* ``delay_seconds`` exists only when a scheduled and a realtime instant both do.
* Event display zones come from the payload's own per-place ``tz``. A missing
  zone renders ``zone: null`` with the true offset and an
  ``endpoint_zone_unavailable`` warning; the interpretation frame and the other
  endpoint are never substituted.
* Whole-itinerary cancellation attribution is T1's
  :func:`transit_core.places.map_itinerary_places`: a place that terminates a
  dummy walk anywhere in the itinerary never reports ``stop_cancelled: true``.
  Trip legs are shape- and order-validated before any T1 mapper sees them.

The public functions never raise a classified failure: they return the constant
error shape from :mod:`transit_core.errors`. That wrapper is deliberately local —
T4 owns no shared helper module, and the two owned modules must stay independent.
"""

import math

from . import durations, errors, http, places, rendering, timeparse, timezones

#: §4.9 count range and the preserved §4.11 map-trips cap.
MAX_DEPARTURES = 50
MAX_TRIP_SEGMENTS = 50
DEFAULT_DEPARTURE_COUNT = 10
DEFAULT_ZOOM = 12
MIN_ZOOM = 1
MAX_ZOOM = 19
MIN_WINDOW_SECONDS = 1
MAX_WINDOW_SECONDS = 86400


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


def _int_in_range(value, name, minimum, maximum, default=None, required=False):
    if value is None:
        if required:
            _fail_value(name, f"{name} is required")
        return default
    if not _is_int(value):
        _fail_value(name, f"{name} must be an integer", value)
    if value < minimum or value > maximum:
        _fail_value(name, f"{name} must be between {minimum} and {maximum}", value)
    return value


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        _fail_value(name, f"{name} must be a non-empty string", value)
    return value


def _text(value):
    """A non-empty string from the payload, else ``None`` (never fabricated)."""
    return value if isinstance(value, str) and value.strip() else None


def _timezone_arg(name, value):
    if value is None:
        return None
    return timezones.resolve_zone_name(value)


def _place_zone(raw):
    zone = raw.get("tz") if isinstance(raw, dict) else None
    return zone if isinstance(zone, str) and zone.strip() else None


def _add_warning(warnings, value):
    if value not in warnings:
        warnings.append(value)


def _payload_instant(text, *, member="timestamp"):
    """Parse a payload instant, classifying a bad string as an upstream shape fault."""
    if not isinstance(text, str) or not text.strip():
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"payload {member} is missing or not a string",
            {"member": member},
        )
    try:
        return timezones.parse_rfc3339(text)
    except ValueError:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"payload {member} is not an RFC 3339 timestamp",
            {"member": member, "value": http.excerpt(text)},
        )


def _render(instant, zone, warnings):
    """The §2.5 timestamp for a payload instant, warning when the zone is missing."""
    if zone is None:
        _add_warning(warnings, durations.ENDPOINT_ZONE_UNAVAILABLE)
    return rendering.timestamp(
        instant,
        zone=zone,
        zone_source=rendering.RESPONSE_PLACE if zone else rendering.UNAVAILABLE,
    )


def _payload_timestamp(text, zone, warnings, *, member="timestamp"):
    if text is None:
        return None
    return _render(_payload_instant(text, member=member), zone, warnings)


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _frame_for(parsed, *, control, timezone, endpoint_query, transport, now):
    """Build the ``TimeFrame`` and, when discovery was needed, its resolution.

    A caller ``timezone`` and an explicit/omitted time issue no request. A naive
    clock with no override resolves the direction-relevant endpoint through §4.3,
    whose budget is at most one request.
    """
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
        return frame, (None if timezone is not None else resolution)

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


# ── §4.9 departures ─────────────────────────────────────────────────


def departures(
    stop_id,
    *,
    count=DEFAULT_DEPARTURE_COUNT,
    time=None,
    window=None,
    timezone=None,
    endpoint_timezone=None,
    transport=None,
    now=None,
):
    """A stop's next departures, with scheduled and realtime times kept apart.

    Returns the canonical envelope with ``outcome``, ``time_frame``, ``query``,
    ``stop``, ``departures``, ``returned_count`` and ``warnings``.
    """
    return _run(
        lambda: _departures(
            stop_id,
            count=count,
            time=time,
            window=window,
            timezone=timezone,
            endpoint_timezone=endpoint_timezone,
            transport=transport,
            now=now,
        )
    )


def _departures(
    stop_id, *, count, time, window, timezone, endpoint_timezone, transport, now
):
    stop_id = _required_text(stop_id, "stop_id")
    count = _int_in_range(
        count, "count", 1, MAX_DEPARTURES, default=DEFAULT_DEPARTURE_COUNT
    )
    window = _int_in_range(window, "window", MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS)
    parsed = timeparse.classify_syntax(time)
    caller_timezone = _timezone_arg("timezone", timezone)
    declared = _timezone_arg("endpoint_timezone", endpoint_timezone)

    query = timezones.EndpointQuery.stop("stop", stop_id, declared_zone=declared)
    frame, resolution = _frame_for(
        parsed,
        control=timeparse.CONTROL_DEPART_AFTER,
        timezone=caller_timezone,
        endpoint_query=query,
        transport=transport,
        now=now,
    )

    params = {"stopId": stop_id, "n": count, "time": frame.wire_value}
    if window is not None:
        params["window"] = window
    data = http.api_get("/api/v6/stoptimes", params, transport=transport)

    if not isinstance(data, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "stoptimes response is not an object", {})
    stop_times = data.get("stopTimes")
    if not isinstance(stop_times, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "stoptimes response has no stopTimes array",
            {"member": "stopTimes"},
        )
    raw_stop = data.get("place")
    if not isinstance(raw_stop, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "stoptimes response has no place object",
            {"member": "place"},
        )

    if resolution is not None:
        timezones.check_zone_agreement(resolution, _place_zone(raw_stop), endpoint_label="stop")

    warnings = []
    rows = []
    for index, stop_time in enumerate(stop_times[:count]):
        if not isinstance(stop_time, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "stop time is not an object",
                {"index": index},
            )
        # StopTime.realTime is required by the pinned schema.
        if "realTime" not in stop_time:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "StopTime.realTime is required and was absent",
                {"index": index, "member": "realTime"},
            )
        realtime = stop_time["realTime"]
        if not isinstance(realtime, bool):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "StopTime.realTime is not a boolean",
                {"index": index, "member": "realTime", "value": _json_safe(realtime)},
            )
        raw_place = stop_time.get("place")
        if not isinstance(raw_place, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "stop time has no place object",
                {"index": index, "member": "place"},
            )
        zone = _place_zone(raw_place)
        scheduled = _payload_timestamp(
            raw_place.get("scheduledDeparture"), zone, warnings,
            member="scheduledDeparture",
        )
        expected = None
        if realtime:
            expected = _payload_timestamp(
                raw_place.get("departure"), zone, warnings, member="departure"
            )
        delay = None
        if realtime and scheduled is not None and expected is not None:
            delay = expected["epoch_seconds"] - scheduled["epoch_seconds"]
        cancellation = places.map_stop_time_cancellation(stop_time)
        rows.append(
            {
                "route": _text(stop_time.get("routeShortName"))
                or _text(stop_time.get("routeLongName")),
                "headsign": _text(stop_time.get("headsign")),
                "mode": _text(stop_time.get("mode")),
                "route_type": stop_time.get("routeType")
                if _is_int(stop_time.get("routeType"))
                else None,
                "agency_name": _text(stop_time.get("agencyName")),
                "trip_id": _text(stop_time.get("tripId")),
                "scheduled_departure": scheduled,
                "expected_departure": expected,
                "realtime": realtime,
                # The pinned StopTime has no `scheduled` property: never inferred.
                "scheduled": None,
                "trip_cancelled": cancellation["trip_cancelled"],
                "stop_cancelled": cancellation["stop_cancelled"],
                "delay_seconds": delay,
            }
        )

    return errors.success_result(
        errors.OK,
        time_frame=frame.to_object(),
        query={
            "stop_id": stop_id,
            "count": count,
            "window_seconds": window,
            "control": timeparse.CONTROL_DEPART_AFTER,
        },
        stop=places.map_place(raw_stop),
        departures=rows,
        returned_count=len(rows),
        warnings=warnings,
    )


# ── §4.10 trip ──────────────────────────────────────────────────────


def trip(trip_id, *, transport=None):
    """The pinned ``Itinerary`` for one trip id, mapped leg by leg (§4.10)."""
    return _run(lambda: _trip(trip_id, transport=transport))


def _validated_legs(raw_legs):
    """Shape- and order-check the itinerary's legs before any T1 mapper runs.

    T1's duration/cancellation helpers treat ordered, shape-validated legs as a
    precondition (§3.1); this normalizer is that precondition for the trip path.
    """
    if not isinstance(raw_legs, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "Itinerary.legs must be an array",
            {"member": "legs"},
        )
    checked = []
    previous_end = None
    for index, leg in enumerate(raw_legs):
        if not isinstance(leg, dict):
            errors.fail(errors.UNEXPECTED_SHAPE, "leg is not an object", {"index": index})
        if _text(leg.get("mode")) is None:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "Leg.mode is required and was absent",
                {"index": index, "member": "mode"},
            )
        raw_from, raw_to = leg.get("from"), leg.get("to")
        if not isinstance(raw_from, dict) or not isinstance(raw_to, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "leg endpoints must be place objects",
                {"index": index},
            )
        start_text = _first_text(leg.get("startTime"), raw_from.get("departure"))
        end_text = _first_text(leg.get("endTime"), raw_to.get("arrival"))
        if start_text is None or end_text is None:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "leg start/end time is missing",
                {"index": index},
            )
        start = _payload_instant(start_text, member="startTime")
        end = _payload_instant(end_text, member="endTime")
        if end < start:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "leg ends before it starts",
                {"index": index},
            )
        if previous_end is not None and start < previous_end:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "legs are not ordered in time",
                {"index": index},
            )
        previous_end = end
        checked.append((leg, start, end))
    return checked


def _intermediate_stops(raw_stops, dummy_endpoints, warnings):
    if raw_stops is None:
        return [], None
    if not isinstance(raw_stops, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "intermediateStops is not an array",
            {"member": "intermediateStops"},
        )
    mapped = []
    for index, raw in enumerate(raw_stops):
        if not isinstance(raw, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "intermediate stop is not an object",
                {"index": index},
            )
        zone = _place_zone(raw)
        place = places.map_place(
            raw, dummy_endpoint=places.place_key(raw) in dummy_endpoints
        )
        mapped.append(
            {
                "stop_id": place["stop_id"],
                "name": place["name"],
                "arrival": _payload_timestamp(
                    raw.get("arrival"), zone, warnings, member="arrival"
                ),
                "departure": _payload_timestamp(
                    raw.get("departure"), zone, warnings, member="departure"
                ),
                "scheduled_arrival": _payload_timestamp(
                    raw.get("scheduledArrival"), zone, warnings,
                    member="scheduledArrival",
                ),
                "scheduled_departure": _payload_timestamp(
                    raw.get("scheduledDeparture"), zone, warnings,
                    member="scheduledDeparture",
                ),
                "track": _text(raw.get("track")),
                # The raw Place flag and the attributed verdict are separate facts.
                "cancelled": places.tristate(raw.get("cancelled")),
                "stop_cancelled": place["stop_cancelled"],
                "source_cancelled": place["source_cancelled"],
                "source_cancelled_origin": place["source_cancelled_origin"],
            }
        )
    return mapped, len(mapped)


def _trip(trip_id, *, transport):
    trip_id = _required_text(trip_id, "trip_id")
    data = http.api_get("/api/v6/trip", {"tripId": trip_id}, transport=transport)
    if not isinstance(data, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "trip response is not an object", {})

    raw_legs = data.get("legs")
    checked = _validated_legs(raw_legs)
    # The whole-itinerary dummy-endpoint set is computed once, from every leg.
    dummy_endpoints = places.dummy_walk_endpoints(raw_legs)
    endpoint_places = places.map_itinerary_places(raw_legs)

    warnings = []
    legs = []
    for index, (leg, start, end) in enumerate(checked):
        raw_from, raw_to = leg.get("from"), leg.get("to")
        from_place, to_place = endpoint_places[index]
        from_zone, to_zone = _place_zone(raw_from), _place_zone(raw_to)
        cancellation = places.leg_cancellation(leg)
        scheduled_start = _first_text(
            leg.get("scheduledStartTime"), raw_from.get("scheduledDeparture")
        )
        scheduled_end = _first_text(
            leg.get("scheduledEndTime"), raw_to.get("scheduledArrival")
        )
        intermediate, count = _intermediate_stops(
            leg.get("intermediateStops"), dummy_endpoints, warnings
        )
        legs.append(
            {
                "index": index,
                "mode": leg.get("mode"),
                "from": from_place,
                "to": to_place,
                "departure": _render(start, from_zone, warnings),
                "arrival": _render(end, to_zone, warnings),
                "scheduled_departure": _payload_timestamp(
                    scheduled_start, from_zone, warnings, member="scheduledStartTime"
                ),
                "scheduled_arrival": _payload_timestamp(
                    scheduled_end, to_zone, warnings, member="scheduledEndTime"
                ),
                "realtime": places.tristate(leg.get("realTime")),
                "scheduled": places.tristate(leg.get("scheduled")),
                "cancelled": cancellation["cancelled"],
                "walking_route_unavailable": cancellation["walking_route_unavailable"],
                "duration_is_estimate": cancellation["duration_is_estimate"],
                "note": cancellation["note"],
                "route": _text(leg.get("routeShortName"))
                or _text(leg.get("routeLongName")),
                "headsign": _text(leg.get("headsign")),
                "agency_name": _text(leg.get("agencyName")),
                "intermediate_stop_count": count,
                "intermediate_stops": intermediate,
            }
        )

    return errors.success_result(
        errors.OK, trip_id=trip_id, legs=legs, warnings=warnings
    )


# ── §4.11 map trips ─────────────────────────────────────────────────


def map_trips(
    min_lat,
    min_lon,
    max_lat,
    max_lon,
    start_time,
    end_time,
    *,
    zoom=DEFAULT_ZOOM,
    timezone=None,
    transport=None,
    now=None,
):
    """Active trips in a bounding box and time window, mapped from TripSegment."""
    return _run(
        lambda: _map_trips(
            min_lat,
            min_lon,
            max_lat,
            max_lon,
            start_time,
            end_time,
            zoom=zoom,
            timezone=timezone,
            transport=transport,
            now=now,
        )
    )


def _coordinate(value, name, minimum, maximum):
    if not _is_finite_number(value):
        _fail_value(name, f"{name} must be a finite number", value)
    if value < minimum or value > maximum:
        _fail_value(name, f"{name} must be between {minimum} and {maximum}", value)
    return value


def _window_timestamp(frame):
    return frame.to_object()["interpreted_instant"]


def _map_trips(
    min_lat,
    min_lon,
    max_lat,
    max_lon,
    start_time,
    end_time,
    *,
    zoom,
    timezone,
    transport,
    now,
):
    min_lat = _coordinate(min_lat, "min_lat", -90, 90)
    max_lat = _coordinate(max_lat, "max_lat", -90, 90)
    min_lon = _coordinate(min_lon, "min_lon", -180, 180)
    max_lon = _coordinate(max_lon, "max_lon", -180, 180)
    if not min_lat < max_lat:
        _fail_value("min_lat", "min_lat must be strictly less than max_lat")
    if not min_lon < max_lon:
        _fail_value("min_lon", "min_lon must be strictly less than max_lon")
    zoom = _int_in_range(zoom, "zoom", MIN_ZOOM, MAX_ZOOM, default=DEFAULT_ZOOM)

    parsed_start = timeparse.classify_syntax(start_time)
    parsed_end = timeparse.classify_syntax(end_time)
    if parsed_start.form == timeparse.FORM_OMITTED:
        _fail_value("start_time", "start_time is required")
    if parsed_end.form == timeparse.FORM_OMITTED:
        _fail_value("end_time", "end_time is required")

    caller_timezone = _timezone_arg("timezone", timezone)
    if (parsed_start.needs_frame or parsed_end.needs_frame) and caller_timezone is None:
        errors.fail(
            errors.BBOX_WINDOW_REQUIRES_EXPLICIT_ZONE,
            "a naive map-trips window needs an explicit timezone: a bounding box has "
            "no endpoint zone to read a wall clock in",
            {
                "arguments": ["start_time", "end_time"],
                "policy": "a bounding box has no single endpoint",
                "remedy": "supply a caller timezone, or explicit-offset instants",
            },
        )
    choice = (
        timeparse.FrameChoice(caller_timezone, timezones.CALLER_TIMEZONE)
        if caller_timezone is not None
        else None
    )
    start_frame = timeparse.interpret(
        parsed_start,
        control=timeparse.CONTROL_DEPART_AFTER,
        frame=choice,
        display_zone=caller_timezone,
        now=now,
    )
    end_frame = timeparse.interpret(
        parsed_end,
        control=timeparse.CONTROL_DEPART_AFTER,
        frame=choice,
        display_zone=caller_timezone,
        now=now,
    )
    if start_frame.instant >= end_frame.instant:
        _fail_value("end_time", "start_time must be strictly before end_time")

    params = {
        "min": f"{min_lat},{min_lon}",
        "max": f"{max_lat},{max_lon}",
        "zoom": zoom,
        "startTime": start_frame.wire_value,
        "endTime": end_frame.wire_value,
    }
    data = http.api_get("/api/v6/map/trips", params, transport=transport)
    if not isinstance(data, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "map/trips response is not an array",
            {"member": "trips"},
        )

    warnings = []
    selected = data[:MAX_TRIP_SEGMENTS]
    segments = [
        _map_segment(raw, index, warnings) for index, raw in enumerate(selected)
    ]

    return errors.success_result(
        errors.OK,
        query={
            "bbox": {
                "min": [min_lat, min_lon],
                "max": [max_lat, max_lon],
            },
            "zoom": zoom,
        },
        time_window={
            "start_time": _window_timestamp(start_frame),
            "end_time": _window_timestamp(end_frame),
            "start_form": parsed_start.form,
            "end_form": parsed_end.form,
            "start_as_given": parsed_start.as_given,
            "end_as_given": parsed_end.as_given,
            # An explicit instant has no interpretation frame, even when a caller
            # timezone is supplied: that zone is display provenance only (§2.1).
            "frame_zone": caller_timezone
            if (parsed_start.needs_frame or parsed_end.needs_frame)
            else None,
            "frame_source": timezones.CALLER_TIMEZONE
            if caller_timezone is not None
            and (parsed_start.needs_frame or parsed_end.needs_frame)
            else timezones.NOT_APPLICABLE,
        },
        trips=segments,
        upstream_count=len(data),
        returned_count=len(segments),
        truncated=len(data) > MAX_TRIP_SEGMENTS,
        max_results=MAX_TRIP_SEGMENTS,
        warnings=warnings,
    )


def _place_coordinate(raw, key):
    value = raw.get(key)
    return value if _is_finite_number(value) else None


def _map_segment(raw, index, warnings):
    if not isinstance(raw, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "trip segment is not an object", {"index": index})
    trips = raw.get("trips")
    if not isinstance(trips, list) or not trips or not isinstance(trips[0], dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "TripSegment.trips must be a non-empty array of objects",
            {"index": index, "member": "trips"},
        )
    trip_id = _text(trips[0].get("tripId"))
    if trip_id is None:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "TripInfo.tripId is required and was absent",
            {"index": index, "member": "tripId"},
        )
    raw_from, raw_to = raw.get("from"), raw.get("to")
    if not isinstance(raw_from, dict) or not isinstance(raw_to, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "TripSegment endpoints must be place objects",
            {"index": index},
        )
    departure = _payload_instant(raw.get("departure"), member="departure")
    arrival = _payload_instant(raw.get("arrival"), member="arrival")
    from_zone, to_zone = _place_zone(raw_from), _place_zone(raw_to)

    # TripSegment.realTime is required by the pinned schema.
    if "realTime" not in raw:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "TripSegment.realTime is required and was absent",
            {"index": index, "member": "realTime"},
        )
    realtime = raw["realTime"]
    if not isinstance(realtime, bool):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "TripSegment.realTime is not a boolean",
            {"index": index, "member": "realTime", "value": _json_safe(realtime)},
        )

    distance = raw.get("distance")
    if distance is not None:
        if not _is_finite_number(distance) or distance < 0:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "TripSegment.distance is not a non-negative number",
                {"index": index, "member": "distance"},
            )

    polyline = raw.get("polyline")
    return {
        "trip_id": trip_id,
        "route": _text(trips[0].get("displayName"))
        or _text(trips[0].get("routeShortName")),
        "mode": _text(raw.get("mode")),
        "from": places.map_place(raw_from),
        "to": places.map_place(raw_to),
        "departure": _render(departure, from_zone, warnings),
        "arrival": _render(arrival, to_zone, warnings),
        "scheduled_departure": _payload_timestamp(
            raw.get("scheduledDeparture"), from_zone, warnings,
            member="scheduledDeparture",
        ),
        "scheduled_arrival": _payload_timestamp(
            raw.get("scheduledArrival"), to_zone, warnings, member="scheduledArrival"
        ),
        "from_lat": _place_coordinate(raw_from, "lat"),
        "from_lon": _place_coordinate(raw_from, "lon"),
        "to_lat": _place_coordinate(raw_to, "lat"),
        "to_lon": _place_coordinate(raw_to, "lon"),
        "distance_meters": distance,
        "polyline_present": isinstance(polyline, str) and polyline != "",
        "realtime": realtime,
        # No `vehicle_position`: the pinned TripSegment has no position slot.
    }


__all__ = [
    "MAX_DEPARTURES",
    "MAX_TRIP_SEGMENTS",
    "DEFAULT_DEPARTURE_COUNT",
    "DEFAULT_ZOOM",
    "MIN_ZOOM",
    "MAX_ZOOM",
    "departures",
    "trip",
    "map_trips",
]
