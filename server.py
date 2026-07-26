#!/usr/bin/env python3
"""Transitous MCP Server — free public transit routing via MOTIS API."""

import json, sys, math, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

API_BASE = "https://api.transitous.org"
UA = "HermesAgent/1.0 (transitous-mcp; kimbo@hermes)"

TRANSIT_MODES = [
    "TRANSIT", "BUS", "COACH", "TRAM", "SUBWAY", "SUBURBAN",
    "REGIONAL_RAIL", "RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE",
    "NIGHT_RAIL", "FERRY", "AIRPLANE", "FUNICULAR", "AERIAL_LIFT",
]

def api_get(path, params=None):
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += "?" + urllib.parse.urlencode(clean, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}

def fmt_dur(s):
    """Format duration in seconds. Handles None, negative, and float values."""
    if s is None or s < 0:
        return "0s"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    h, m = divmod(s, 3600)
    m //= 60
    return f"{h}h{m:02d}m" if h else f"{m}min"

def fmt_time(iso):
    """Format ISO time to HH:MM. Guards against None/empty input."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone()  # convert to system local time (BST/GMT)
        return local.strftime("%H:%M")
    except Exception:
        return iso

def validate_coords(*pairs):
    """Validate (name, lat, lon) tuples."""
    for name, lat, lon in pairs:
        if not (-90 <= lat <= 90):
            return {"error": f"{name} latitude must be -90..90, got {lat}"}
        if not (-180 <= lon <= 180):
            return {"error": f"{name} longitude must be -180..180, got {lon}"}

def plan_route(from_lat, from_lon, to_lat, to_lon,
               time=None, arrive_by=False, max_transfers=5,
               transit_modes=None, num_itineraries=3, max_walk_sec=1800):
    err = validate_coords(("from", from_lat, from_lon), ("to", to_lat, to_lon))
    if err: return err
    if max_transfers < 0:
        return {"error": "max_transfers must be >= 0"}
    if num_itineraries < 1:
        return {"error": "num_itineraries must be >= 1"}
    if transit_modes is None:
        transit_modes = ["TRANSIT"]
    for m in transit_modes:
        if m not in TRANSIT_MODES:
            return {"error": f"Unknown mode: {m}. Valid: {', '.join(TRANSIT_MODES)}"}
    params = {
        "fromPlace": f"{from_lat},{from_lon}",
        "toPlace": f"{to_lat},{to_lon}",
        "maxTransfers": max_transfers,
        "numItineraries": num_itineraries,
        "maxPreTransitTime": max_walk_sec,
        "maxPostTransitTime": max_walk_sec,
        "transitModes": transit_modes,
        "arriveBy": str(arrive_by).lower(),
    }
    if time: params["time"] = time
    data = api_get("/api/v6/plan", params)
    if "error" in data: return data
    itins = data.get("itineraries", [])
    if not itins:
        return {"result": "No routes found.", "itineraries": [],
                "from": data.get("from", {}).get("name", "?"),
                "to": data.get("to", {}).get("name", "?")}
    results = []
    for itin in itins[:num_itineraries]:
        legs = []
        for leg in itin.get("legs", []):
            if not isinstance(leg, dict): continue
            l = {"mode": leg.get("mode", "?"),
                 "from": leg.get("from", {}).get("name", "?"),
                 "to": leg.get("to", {}).get("name", "?"),
                 "duration": fmt_dur(leg.get("duration", 0)),
                 "departure": fmt_time(leg.get("from", {}).get("departure", "")),
                 "arrival": fmt_time(leg.get("to", {}).get("arrival", ""))}
            if leg.get("routeShortName"): l["route"] = leg["routeShortName"]
            elif leg.get("routeLongName"): l["route"] = leg["routeLongName"]
            if leg.get("headsign"): l["headsign"] = leg["headsign"]
            if leg.get("tripId"): l["tripId"] = leg["tripId"]
            legs.append(l)
        results.append({"option": len(results) + 1,
                        "duration": fmt_dur(itin.get("duration", 0)),
                        "transfers": itin.get("transfers", 0),
                        "departure": fmt_time(itin.get("startTime", "")),
                        "arrival": fmt_time(itin.get("endTime", "")),
                        "legs": legs})
    return {"from": data.get("from", {}).get("name", "?"),
            "to": data.get("to", {}).get("name", "?"),
            "itineraries": results}

def geocode_stop(query):
    data = api_get("/api/v1/geocode", {"text": query})
    if "error" in data: return data
    if not isinstance(data, list):
        return {"error": "Unexpected API response", "raw": str(data)[:200]}
    return {"query": query, "results": [
        {"name": i.get("name", "?"), "lat": i.get("lat"), "lon": i.get("lon"),
         "type": i.get("type", "?"), "category": i.get("category", "?"),
         "id": i.get("id", "?")}
        for i in data[:5]]}

# ── New endpoint functions ──────────────────────────────────────────

def transit_departures(stop_id, time=None, count=10, direction="DEPARTURES"):
    """Live departure/arrival board for a stop."""
    params = {"stopId": stop_id, "n": count if count else 10}
    if time: params["time"] = time
    if direction and direction != "DEPARTURES":
        params["direction"] = direction
    data = api_get("/api/v6/stoptimes", params)
    if "error" in data: return data
    times = data.get("stopTimes", data) if isinstance(data, dict) else []
    if not isinstance(times, list): return {"error": "Unexpected API response"}
    results = []
    for st in times[:count]:
        place = st.get("place", {})
        entry = {"route": st.get("routeShortName", "?"),
                 "headsign": st.get("headsign", "?"),
                 "scheduled": fmt_time(place.get("scheduledDeparture", place.get("scheduledArrival", ""))),
                 "realTime": fmt_time(place.get("departure", place.get("arrival", "")))}
        if st.get("cancelled"): entry["cancelled"] = True
        delay = st.get("delaySeconds", 0)
        if delay: entry["delayMinutes"] = delay // 60
        if st.get("tripId"): entry["tripId"] = st["tripId"]
        results.append(entry)
    return {"stop": stop_id, "direction": direction, "departures": results}

def transit_nearby_stops(lat, lon, radius=1000):
    """Find transit stops near a location."""
    err = validate_coords(("center", lat, lon))
    if err: return err
    if radius > 5000: radius = 5000
    # rough bbox: 1° lat ≈ 111km, lon adjusted by cos(lat)
    dlat = radius / 111000.0
    dlon = radius / (111000.0 * math.cos(math.radians(lat)))
    bbox = f"{lat-dlat},{lon-dlon},{lat+dlat},{lon+dlon}"
    data = api_get("/api/v6/map/stops", {"bounds": bbox, "zoom": 15})
    if "error" in data: return data
    stops = data if isinstance(data, list) else data.get("stops", [])
    if not isinstance(stops, list): return {"error": "Unexpected API response"}
    def dist(s):
        slat = s.get("lat", lat); slon = s.get("lon", lon)
        return math.hypot((slat-lat)*111000, (slon-lon)*111000*math.cos(math.radians(lat)))
    results = []
    for s in sorted(stops, key=dist)[:20]:
        results.append({"id": s.get("id", "?"), "name": s.get("name", "?"),
                        "lat": s.get("lat"), "lon": s.get("lon"),
                        "modes": s.get("modes", []),
                        "distance_m": round(dist(s))})
    return {"center": [lat, lon], "radius_m": radius, "count": len(results), "stops": results}

def reverse_geocode(lat, lon):
    """Convert coordinates to the nearest transit stop or address."""
    err = validate_coords(("point", lat, lon))
    if err: return err
    data = api_get("/api/v1/reverse-geocode", {"place": f"{lat},{lon}"})
    if "error" in data: return data
    return {"lat": lat, "lon": lon, "result": data}

def trip_details(trip_id, date=None):
    """Get full trip details: stop sequence, route info, timing."""
    params = {"tripId": trip_id}
    if date: params["date"] = date
    data = api_get("/api/v6/trip", params)
    if "error" in data: return data
    if not isinstance(data, dict): return {"error": "Unexpected API response"}
    legs = []
    for leg in data.get("legs", []):
        if not isinstance(leg, dict): continue
        l = {"mode": leg.get("mode", "?"),
             "from": leg.get("from", {}).get("name", "?"),
             "to": leg.get("to", {}).get("name", "?"),
             "departure": fmt_time(leg.get("from", {}).get("departure", "")),
             "arrival": fmt_time(leg.get("to", {}).get("arrival", "")),
             "duration": fmt_dur(leg.get("duration", 0))}
        if leg.get("routeShortName"): l["route"] = leg["routeShortName"]
        if leg.get("headsign"): l["headsign"] = leg["headsign"]
        legs.append(l)
    return {"tripId": trip_id, "legs": legs}

def health():
    """Check server health."""
    return api_get("/api/v1/health")

def reachable(lat, lon, max_time_min, time=None, arrive_by=False):
    """Isochrones: what stops/destinations are reachable within a time budget?"""
    err = validate_coords(("origin", lat, lon))
    if err: return err
    params = {"fromPlace": f"{lat},{lon}",
              "maxTravelTime": max_time_min * 60,
              "arriveBy": str(arrive_by).lower()}
    if time: params["time"] = time
    data = api_get("/api/v6/one-to-all", params)
    if "error" in data: return data
    places = data.get("places", []) if isinstance(data, dict) else []
    results = []
    for p in places[:50]:
        results.append({"name": p.get("name", "?"), "lat": p.get("lat"), "lon": p.get("lon"),
                        "travelTime_min": (p.get("travelTime", 0) // 60)})
    return {"origin": [lat, lon], "maxTime_min": max_time_min, "reachable": results}

def refresh_itinerary(itinerary_id):
    """Re-fetch an itinerary with real-time updates."""
    data = api_get("/api/v6/refresh-itinerary", {"itineraryId": itinerary_id})
    return data

def rentals_nearby(lat, lon):
    """Find nearby rental bikes, scooters, or cars (GBFS)."""
    err = validate_coords(("center", lat, lon))
    if err: return err
    return api_get("/api/v1/rentals", {"lat": lat, "lon": lon})

def one_to_many(from_lat, from_lon, destinations, time=None):
    """Street routing from one origin to multiple destinations."""
    err = validate_coords(("origin", from_lat, from_lon))
    if err: return err
    if not isinstance(destinations, list) or len(destinations) < 1:
        return {"error": "destinations must be a non-empty list of [lat, lon] pairs"}
    params = {"fromPlace": f"{from_lat},{from_lon}"}
    if time: params["time"] = time
    for i, d in enumerate(destinations):
        params[f"toPlace.{i}"] = f"{d[0]},{d[1]}"
    return api_get("/api/v1/one-to-many", params)

# ── Tool registry ───────────────────────────────────────────────────

TOOLS = [
    {"name":"transit_plan","description":"Plan a public transit route between two locations. Returns step-by-step itineraries with modes, times, and transfers. Supports bus, train, tram, subway, ferry, etc.",
     "inputSchema":{"type":"object","properties":{
         "from_lat":{"type":"number","description":"Origin latitude"},
         "from_lon":{"type":"number","description":"Origin longitude"},
         "to_lat":{"type":"number","description":"Destination latitude"},
         "to_lon":{"type":"number","description":"Destination longitude"},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "arrive_by":{"type":"boolean","description":"If true, time=arrival time"},
         "max_transfers":{"type":"integer","description":"Max changes (default: 5)"},
         "transit_modes":{"type":"array","items":{"type":"string","enum":TRANSIT_MODES},
                          "description":"Transit modes (default: ['TRANSIT'] = all)"},
         "num_itineraries":{"type":"integer","description":"Routes to return (default: 3)"},
         "max_walking_minutes":{"type":"integer","description":"Max walk mins (default: 30)"}},
     "required":["from_lat","from_lon","to_lat","to_lon"]}},
    {"name":"transit_geocode","description":"Geocode a place name/address/stop to coordinates. Use before transit_plan.",
     "inputSchema":{"type":"object","properties":{
         "query":{"type":"string","description":"Place name, address, or stop"}},
     "required":["query"]}},
    {"name":"transit_departures","description":"Get a live departure/arrival board for a stop. Use stop IDs from transit_geocode or transit_nearby_stops.",
     "inputSchema":{"type":"object","properties":{
         "stop":{"type":"string","description":"Stop ID (from geocode or nearby_stops)"},
         "time":{"type":"integer","description":"Unix timestamp (default: now)"},
         "count":{"type":"integer","description":"Number of departures (default: 10)"},
         "direction":{"type":"string","enum":["DEPARTURES","ARRIVALS","BOTH"],"description":"DEPARTURES, ARRIVALS, or BOTH (default: DEPARTURES)"}},
     "required":["stop"]}},
    {"name":"transit_nearby_stops","description":"Find transit stops near a location. Returns stops with IDs, names, modes, and distances.",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number","description":"Latitude of search center"},
         "lon":{"type":"number","description":"Longitude of search center"},
         "radius":{"type":"integer","description":"Search radius in meters (default: 1000, max: 5000)"}},
     "required":["lat","lon"]}},
    {"name":"transit_reverse_geocode","description":"Convert coordinates to the nearest transit stop or address.",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number","description":"Latitude"},
         "lon":{"type":"number","description":"Longitude"}},
     "required":["lat","lon"]}},
    {"name":"transit_trip","description":"Get full trip details including stop sequence and route information. Use tripId from transit_plan or transit_departures results.",
     "inputSchema":{"type":"object","properties":{
         "trip_id":{"type":"string","description":"Trip ID from plan or departures results"},
         "date":{"type":"string","description":"Trip date (YYYY-MM-DD, optional)"}},
     "required":["trip_id"]}},
    {"name":"transit_health","description":"Check if the Transitous API server is healthy.",
     "inputSchema":{"type":"object","properties":{}}},
    {"name":"transit_reachable","description":"Find what destinations are reachable within a time budget (isochrones).",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number","description":"Origin latitude"},
         "lon":{"type":"number","description":"Origin longitude"},
         "max_time_min":{"type":"integer","description":"Maximum travel time in minutes"},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "arrive_by":{"type":"boolean","description":"If true, time=arrival time"}},
     "required":["lat","lon","max_time_min"]}},
    {"name":"transit_refresh","description":"Refresh an itinerary with real-time updates. Use itinerary ID from transit_plan results.",
     "inputSchema":{"type":"object","properties":{
         "itinerary_id":{"type":"string","description":"Itinerary ID from transit_plan"}},
     "required":["itinerary_id"]}},
    {"name":"transit_rentals","description":"Find nearby rental bikes, scooters, or cars (GBFS sharing systems).",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number","description":"Latitude"},
         "lon":{"type":"number","description":"Longitude"}},
     "required":["lat","lon"]}},
    {"name":"transit_one_to_many","description":"Street routing from one origin to multiple destinations.",
     "inputSchema":{"type":"object","properties":{
         "from_lat":{"type":"number","description":"Origin latitude"},
         "from_lon":{"type":"number","description":"Origin longitude"},
         "destinations":{"type":"array","items":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2},
                        "description":"List of [lat, lon] destination pairs"},
         "time":{"type":"string","description":"ISO datetime (default: now)"}},
     "required":["from_lat","from_lon","destinations"]}},
]

# ── Tool dispatch ───────────────────────────────────────────────────

def handle_call(name, args):
    if name == "transit_plan":
        wm = args.get("max_walking_minutes")
        if wm is None: wm = 30
        elif wm < 0: return {"error": "max_walking_minutes must be >= 0"}
        return plan_route(args["from_lat"], args["from_lon"],
                          args["to_lat"], args["to_lon"],
                          time=args.get("time"), arrive_by=args.get("arrive_by", False),
                          max_transfers=args.get("max_transfers", 5),
                          transit_modes=args.get("transit_modes"),
                          num_itineraries=args.get("num_itineraries", 3),
                          max_walk_sec=wm * 60)
    elif name == "transit_geocode":
        return geocode_stop(args["query"])
    elif name == "transit_departures":
        return transit_departures(args["stop"], time=args.get("time"),
                                  count=args.get("count", 10),
                                  direction=args.get("direction", "DEPARTURES"))
    elif name == "transit_nearby_stops":
        return transit_nearby_stops(args["lat"], args["lon"],
                                    radius=args.get("radius", 1000))
    elif name == "transit_reverse_geocode":
        return reverse_geocode(args["lat"], args["lon"])
    elif name == "transit_trip":
        return trip_details(args["trip_id"], date=args.get("date"))
    elif name == "transit_health":
        return health()
    elif name == "transit_reachable":
        return reachable(args["lat"], args["lon"], args["max_time_min"],
                         time=args.get("time"), arrive_by=args.get("arrive_by", False))
    elif name == "transit_refresh":
        return refresh_itinerary(args["itinerary_id"])
    elif name == "transit_rentals":
        return rentals_nearby(args["lat"], args["lon"])
    elif name == "transit_one_to_many":
        return one_to_many(args["from_lat"], args["from_lon"],
                           args["destinations"], time=args.get("time"))
    return {"error": f"Unknown tool: {name}"}

# ── MCP JSON-RPC loop ───────────────────────────────────────────────

def send(resp):
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: req = json.loads(line)
        except Exception:
            send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
            continue
        rid = req.get("id")
        method = req.get("method", "")
        if method == "initialize":
            send({"jsonrpc":"2.0","id":rid,"result":{
                "protocolVersion":"2024-11-05","capabilities":{"tools":{}},
                "serverInfo":{"name":"transitous-mcp","version":"2.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":rid,"result":{"tools":TOOLS}})
        elif method == "tools/call":
            try:
                result = handle_call(req["params"]["name"], req["params"].get("arguments", {}))
                send({"jsonrpc":"2.0","id":rid,"result":{
                    "content":[{"type":"text","text":json.dumps(result,indent=2)}]}})
            except Exception as e:
                send({"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":str(e)}})
        elif method.startswith("notifications/"):
            pass
        elif method == "ping":
            send({"jsonrpc":"2.0","id":rid,"result":{}})
        else:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}})

if __name__ == "__main__":
    main()
