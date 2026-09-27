#!/usr/bin/env python3
"""Transitous MCP Server — free public transit routing via MOTIS API."""

import json, sys, math, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

ERA_VERSION = "2026-07-28"
SERVER_INFO = {"name": "transitous-mcp", "version": "2.1.0"}   # D7 minor bump
ERA_RESULT_FIELDS = {"resultType": "complete", "ttlMs": 0, "cacheScope": "private"}
RESULT_META = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}

def era_result(payload):
    """A result carrying the era-strict fields D3 mandates on every response."""
    out = dict(payload)
    out.update(ERA_RESULT_FIELDS)
    out["_meta"] = RESULT_META
    return out

API_BASE = "https://api.transitous.org"
# User-Agent contact is the PROJECT's public repo URL, never a personal address.
UA = "HermesAgent/1.0 (transitous-mcp; +https://github.com/Dthen/transitous-mcp)"

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
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"transport_error": str(e)}
    except Exception as e:
        return {"error": str(e)}

def fmt_dur(s):
    if s is None or s < 0: return "?"
    s = int(round(s))
    if s < 60: return f"{s}s"
    h, m = divmod(s, 3600); m //= 60
    return f"{h}h{m:02d}m" if h else f"{m}min" if m == 1 else f"{m}mins"

def normalize_time(iso):
    """Ensure ISO timestamp has explicit timezone (appends Z if missing)."""
    if not iso: return iso
    iso = iso.strip()
    # Date-only strings like "2024-01-01" have no time separator
    if "T" not in iso and "t" not in iso and " " not in iso:
        return iso + "T00:00:00Z"
    iso = iso.replace(" ", "T")
    if not (iso.endswith("Z") or "+" in iso[-6:] or "-" in iso[-6:]):
        return iso + "Z"
    return iso

def fmt_time(iso):
    """Format an ISO timestamp to HH:MM (UTC)."""
    if not iso: return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%H:%M")
    except Exception: return iso

def _check_coords(lat, lon):
    """Return error dict if coordinates are out of range."""
    if not (-90 <= lat <= 90): return {"error": f"Latitude {lat} out of range (-90 to 90)"}
    if not (-180 <= lon <= 180): return {"error": f"Longitude {lon} out of range (-180 to 180)"}
    return None

# ── Core endpoints ──────────────────────────────────────────────────

def plan_route(from_lat, from_lon, to_lat, to_lon,
               time=None, arrive_by=False, max_transfers=5,
               transit_modes=None, num_itineraries=3, max_walk_sec=1800):
    err = _check_coords(from_lat, from_lon) or _check_coords(to_lat, to_lon)
    if err: return err
    if not transit_modes: transit_modes = ["TRANSIT"]
    for m in transit_modes:
        if m not in TRANSIT_MODES:
            return {"error": f"Unknown mode: {m}"}
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
    if time: params["time"] = normalize_time(time)
    data = api_get("/api/v6/plan", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    itins = data.get("itineraries", [])
    if not itins:
        from_name = data.get("from",{}).get("name","?")
        to_name = data.get("to",{}).get("name","?")
        if from_name == "START": from_name = f"{from_lat},{from_lon}"
        if to_name == "END": to_name = f"{to_lat},{to_lon}"
        return {"result": "No routes found.", "itineraries": [],
                "from": from_name, "to": to_name}
    results = []
    for itin in itins[:num_itineraries]:
        legs = []
        for leg in itin.get("legs", []):
            if not isinstance(leg, dict): continue
            leg_from = leg.get("from",{}).get("name","?")
            leg_to = leg.get("to",{}).get("name","?")
            if leg_from == "START": leg_from = f"{from_lat},{from_lon}"
            if leg_to == "END": leg_to = f"{to_lat},{to_lon}"
            l = {"mode": leg.get("mode","?"),
                 "from": leg_from,
                 "to": leg_to,
                 "duration": fmt_dur(leg.get("duration",0)),
                 "departure": fmt_time(leg.get("from",{}).get("departure","")),
                 "arrival": fmt_time(leg.get("to",{}).get("arrival",""))}
            if leg.get("routeShortName"): l["route"] = leg["routeShortName"]
            elif leg.get("routeLongName"): l["route"] = leg["routeLongName"]
            if leg.get("headsign"): l["headsign"] = leg["headsign"]
            if leg.get("tripId"): l["tripId"] = leg["tripId"]
            if leg.get("cancelled"): l["cancelled"] = True
            legs.append(l)
        results.append({"option": len(results)+1,
                        "itin_id": itin.get("id", ""),
                        "duration": fmt_dur(itin.get("duration",0)),
                        "transfers": itin.get("transfers",0),
                        "departure": fmt_time(itin.get("startTime","")),
                        "arrival": fmt_time(itin.get("endTime","")),
                        "legs": legs})
    from_name = data.get("from",{}).get("name","?")
    to_name = data.get("to",{}).get("name","?")
    if from_name == "START": from_name = f"{from_lat},{from_lon}"
    if to_name == "END": to_name = f"{to_lat},{to_lon}"
    return {"result": "ok", "from": from_name, "to": to_name, "itineraries": results}

def geocode_stop(query, limit=5):
    data = api_get("/api/v1/geocode", {"text": query})
    if "error" in data: return data
    if "transport_error" in data: return data
    return {"query": query, "results": [
        {"name": i.get("name","?"), "lat": i.get("lat"), "lon": i.get("lon"),
         "type": i.get("type","?"), "id": i.get("id","?")}
        for i in data[:int(limit)] if isinstance(i, dict)]}

# ── New endpoints (using verified API parameters) ───────────────────

def departures(stop_id, count=10, time=None, window=None):
    """Live departure board. stopId from geocode/nearby_stops."""
    params = {"stopId": stop_id, "n": count}
    if time: params["time"] = normalize_time(time)
    if window: params["window"] = window
    data = api_get("/api/v6/stoptimes", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    if "stopTimes" not in data:
        return {"error": "Missing stopTimes in response"}
    place_name = data.get("place", {}).get("name", "")
    times = data["stopTimes"]
    if not isinstance(times, list): return {"error": "Unexpected response"}
    results = []
    for st in times[:count]:
        p = st.get("place", {})
        r = {"route": st.get("routeShortName", "?"),
             "headsign": st.get("headsign", "?"),
             "scheduled": fmt_time(p.get("scheduledDeparture", p.get("scheduledArrival", "")))}
        if p.get("departure"): r["actual"] = fmt_time(p["departure"])
        if st.get("cancelled"): r["cancelled"] = True
        results.append(r)
    return {"stopId": stop_id, "stopName": place_name, "departures": results}

def nearby_stops(lat, lon, radius=1000):
    """Find stops within radius (meters, max 5000)."""
    err = _check_coords(lat, lon)
    if err: return err
    dlat = radius / 111000.0
    dlon = radius / (111000.0 * math.cos(math.radians(lat)))
    params = {
        "min": f"{lat-dlat},{lon-dlon}",
        "max": f"{lat+dlat},{lon+dlon}",
    }
    data = api_get("/api/v6/map/stops", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    stops = data if isinstance(data, list) else []
    def dist(s):
        slat, slon = s.get("lat", lat), s.get("lon", lon)
        return math.hypot((slat-lat)*111000, (slon-lon)*111000*math.cos(math.radians(lat)))
    results = []
    for s in [s for s in sorted(stops, key=dist) if dist(s) <= radius][:20]:
        results.append({"id": s.get("stopId","?"), "name": s.get("name","?"),
                        "lat": s.get("lat"), "lon": s.get("lon"),
                        "distance_m": round(dist(s))})
    return {"center": [lat, lon], "radius_m": radius, "stops": results}

def reverse_geocode(lat, lon):
    """Coordinates → nearest stop/address."""
    err = _check_coords(lat, lon)
    if err: return err
    data = api_get("/api/v1/reverse-geocode", {"place": f"{lat},{lon}"})
    if "error" in data: return data
    if "transport_error" in data: return data
    return {"lat": lat, "lon": lon, "stops": data[:5] if isinstance(data, list) else [data]}

def trip_details(trip_id):
    """Full trip stop sequence. tripId from plan/departures results."""
    data = api_get("/api/v6/trip", {"tripId": trip_id})
    if "error" in data: return data
    if "transport_error" in data: return data
    if not isinstance(data, dict): return {"error": "Unexpected response"}
    legs = []
    for leg in data.get("legs", []):
        if not isinstance(leg, dict): continue
        stops = []
        for stop in leg.get("intermediateStops", []):
            if isinstance(stop, dict):
                s = {"name": stop.get("name","?")}
                if stop.get("arrival"): s["arrival"] = fmt_time(stop["arrival"])
                if stop.get("departure"): s["departure"] = fmt_time(stop["departure"])
                if stop.get("stopId"): s["stopId"] = stop["stopId"]
                stops.append(s)
        l = {"mode": leg.get("mode","?"),
             "from": leg.get("from",{}).get("name","?"),
             "to": leg.get("to",{}).get("name","?"),
             "departure": fmt_time(leg.get("from",{}).get("departure","")),
             "arrival": fmt_time(leg.get("to",{}).get("arrival",""))}
        if leg.get("routeShortName"): l["route"] = leg["routeShortName"]
        if leg.get("headsign"): l["headsign"] = leg["headsign"]
        if leg.get("cancelled"): l["cancelled"] = True
        if stops: l["intermediateStops"] = stops
        legs.append(l)
    return {"tripId": trip_id, "legs": legs}

def health():
    return api_get("/api/v1/health")

def reachable(lat, lon, max_time_min, time=None, arrive_by=False):
    """Isochrones: destinations reachable within time budget."""
    err = _check_coords(lat, lon)
    if err: return err
    params = {"one": f"{lat},{lon}", "maxTravelTime": max_time_min}
    if time: params["time"] = normalize_time(time)
    params["arriveBy"] = str(arrive_by).lower()
    data = api_get("/api/v6/one-to-all", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    places = data.get("all", []) if isinstance(data, dict) else []
    results = [{"name": p.get("place",{}).get("name","?"),
                "lat": p.get("place",{}).get("lat"),
                "lon": p.get("place",{}).get("lon"),
                "travelTime_min": round(p.get("duration",0)/60)} for p in places[:50] if isinstance(p, dict)]
    return {"origin": [lat, lon], "maxTime_min": max_time_min, "reachable": results}

def refresh(itinerary_id):
    """Re-fetch an itinerary with real-time updates."""
    return api_get("/api/v6/refresh-itinerary", {"itineraryId": itinerary_id})

def rentals_nearby(lat, lon, radius=1000):
    """Nearby shared bikes, scooters, and cars (GBFS)."""
    err = _check_coords(lat, lon)
    if err: return err
    data = api_get("/api/v1/rentals", {"point": f"{lat},{lon}", "radius": radius})
    if "error" in data: return data
    if "transport_error" in data: return data
    return data

def one_to_many(from_lat, from_lon, destinations, mode="WALK", max_sec=3600, arrive_by=False):
    """Street routing from one origin to multiple destinations."""
    err = _check_coords(from_lat, from_lon)
    if err: return err
    if not destinations: return {"error": "destinations must not be empty"}
    for d in destinations:
        if not isinstance(d, (list, tuple)) or len(d) < 2:
            return {"error": f"Invalid destination: {d!r}, expected [lat, lon]"}
    params = {"one": f"{from_lat};{from_lon}", "mode": mode, "max": max_sec,
              "maxMatchingDistance": 25, "arriveBy": str(arrive_by).lower(),
              "many": ",".join(f"{d[0]};{d[1]}" for d in destinations)}
    data = api_get("/api/v1/one-to-many", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    return data

def one_to_many_transit(from_lat, from_lon, destinations, time=None, arrive_by=False,
                         max_transfers=5, transit_modes=None, max_walk_sec=900,
                         max_travel_minutes=60):
    """Transit routing from one origin to multiple destinations."""
    err = _check_coords(from_lat, from_lon)
    if err: return err
    if not destinations: return {"error": "destinations must not be empty"}
    for d in destinations:
        if not isinstance(d, (list, tuple)) or len(d) < 2:
            return {"error": f"Invalid destination: {d!r}, expected [lat, lon]"}
    if not transit_modes: transit_modes = ["TRANSIT"]
    for m in transit_modes:
        if m not in TRANSIT_MODES:
            return {"error": f"Unknown mode: {m}"}
    params = {"one": f"{from_lat};{from_lon}", "maxTransfers": max_transfers,
              "maxTravelTime": max_travel_minutes, "maxMatchingDistance": 25,
              "maxPreTransitTime": max_walk_sec, "maxPostTransitTime": max_walk_sec,
              "arriveBy": str(arrive_by).lower(), "transitModes": transit_modes,
              "many": ",".join(f"{d[0]};{d[1]}" for d in destinations)}
    if time: params["time"] = normalize_time(time)
    data = api_get("/api/experimental/one-to-many-intermodal", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    return data

def map_trips(min_lat, min_lon, max_lat, max_lon, start_time, end_time,
              zoom=12):
    """Active transit trips in a bounding box with real-time positions."""
    err = _check_coords(min_lat, min_lon) or _check_coords(max_lat, max_lon)
    if err: return err
    params = {"min": f"{min_lat},{min_lon}", "max": f"{max_lat},{max_lon}",
              "zoom": zoom,
              "startTime": normalize_time(start_time),
              "endTime": normalize_time(end_time)}
    data = api_get("/api/v6/map/trips", params)
    if "error" in data: return data
    if "transport_error" in data: return data
    trips = data if isinstance(data, list) else []
    results = []
    for t in trips[:50]:
        if not isinstance(t, dict): continue
        inner = t.get("trips", [{}])[0] if t.get("trips") else {}
        r = {"tripId": inner.get("tripId","?"),
             "route": inner.get("displayName", t.get("routeShortName","?")),
             "mode": t.get("mode","?"),
             "from": t.get("from",{}).get("name","?"),
             "to": t.get("to",{}).get("name","?"),
             "departure": fmt_time(t.get("departure","")),
             "arrival": fmt_time(t.get("arrival",""))}
        if t.get("from",{}).get("lat"): r["from_lat"] = t["from"]["lat"]
        if t.get("from",{}).get("lon"): r["from_lon"] = t["from"]["lon"]
        if t.get("to",{}).get("lat"): r["to_lat"] = t["to"]["lat"]
        if t.get("to",{}).get("lon"): r["to_lon"] = t["to"]["lon"]
        if t.get("realTime"): r["realTime"] = True
        results.append(r)
    return {"bbox": {"min": [min_lat, min_lon], "max": [max_lat, max_lon]},
            "zoom": zoom, "trips": results}

# ── Tool registry ───────────────────────────────────────────────────

TOOLS = [
    {"name":"transit_plan","description":"Plan a public transit route between two locations. Returns step-by-step itineraries with modes, times, and transfers.",
     "inputSchema":{"type":"object","properties":{
         "from_lat":{"type":"number"},"from_lon":{"type":"number"},
         "to_lat":{"type":"number"},"to_lon":{"type":"number"},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "arrive_by":{"type":"boolean"},
         "max_transfers":{"type":"integer","description":"default: 5"},
         "transit_modes":{"type":"array","items":{"type":"string","enum":TRANSIT_MODES}},
         "num_itineraries":{"type":"integer","description":"default: 3"},
         "max_walking_minutes":{"type":"integer","description":"Max walk per leg in minutes","default":30}},
     "required":["from_lat","from_lon","to_lat","to_lon"]}},
    {"name":"transit_geocode","description":"Geocode a place name/address/stop to coordinates.",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","description":"Max results (default: 5)"}},
     "required":["query"]}},
    {"name":"transit_departures","description":"Live departure board for a stop. Use stop IDs from geocode or nearby_stops.",
     "inputSchema":{"type":"object","properties":{
         "stop":{"type":"string","description":"Stop ID"},
         "count":{"type":"integer","description":"default: 10"},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "window":{"type":"integer","description":"Time window in seconds"}},
     "required":["stop"]}},
    {"name":"transit_nearby_stops","description":"Find transit stops near a location.",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number"},"lon":{"type":"number"},
         "radius":{"type":"integer","description":"Meters (default: 1000, max: 5000)"}},
     "required":["lat","lon"]}},
    {"name":"transit_reverse_geocode","description":"Convert coordinates to the nearest transit stop or address.",
     "inputSchema":{"type":"object","properties":{"lat":{"type":"number"},"lon":{"type":"number"}},
     "required":["lat","lon"]}},
    {"name":"transit_trip","description":"Get full trip details including stop sequence. Use tripId from plan or departures results.",
     "inputSchema":{"type":"object","properties":{
         "trip_id":{"type":"string","description":"Trip ID from plan/departures"}},
     "required":["trip_id"]}},
    {"name":"transit_health","description":"Check if the Transitous API server is healthy.",
     "inputSchema":{"type":"object","properties":{}}},
    {"name":"transit_reachable","description":"Find destinations reachable within a time budget (isochrones).",
     "inputSchema":{"type":"object","properties":{
         "lat":{"type":"number"},"lon":{"type":"number"},
         "max_time_min":{"type":"integer","description":"Maximum travel time in minutes"},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "arrive_by":{"type":"boolean"}},
     "required":["lat","lon","max_time_min"]}},
    {"name":"transit_refresh","description":"Refresh an itinerary with real-time updates. Use itinerary ID from plan results.",
     "inputSchema":{"type":"object","properties":{
         "itinerary_id":{"type":"string","description":"Itinerary ID from transit_plan"}},
     "required":["itinerary_id"]}},
    {"name":"transit_rentals","description":"Find nearby rental bikes, scooters, and cars (GBFS sharing systems).",
     "inputSchema":{"type":"object","properties":{"lat":{"type":"number"},"lon":{"type":"number"},"radius":{"type":"integer","description":"Search radius in meters (default: 1000)"}},
     "required":["lat","lon"]}},
    {"name":"transit_one_to_many","description":"Street routing from one origin to multiple destinations.",
     "inputSchema":{"type":"object","properties":{
         "from_lat":{"type":"number"},"from_lon":{"type":"number"},
         "destinations":{"type":"array","items":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2}},
         "mode":{"type":"string","description":"WALK, BIKE, or CAR (default: WALK)"},
         "max_sec":{"type":"integer","description":"Max travel time in seconds (default: 3600)"},
         "arrive_by":{"type":"boolean","description":"If true, many-to-one routing"}},
     "required":["from_lat","from_lon","destinations"]}},
    {"name":"transit_one_to_many_transit","description":"Transit routing from one origin to multiple destinations. Like transit_plan but for N destinations in one call.",
     "inputSchema":{"type":"object","properties":{
         "from_lat":{"type":"number"},"from_lon":{"type":"number"},
         "destinations":{"type":"array","items":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2}},
         "time":{"type":"string","description":"ISO datetime (default: now)"},
         "arrive_by":{"type":"boolean","description":"If true, many-to-one routing"},
         "max_transfers":{"type":"integer","description":"Max transfers (default: 5)"},
         "transit_modes":{"type":"array","items":{"type":"string","enum":TRANSIT_MODES}},
         "max_walking_minutes":{"type":"integer","description":"Max walk per leg in minutes","default":15},
         "max_travel_minutes":{"type":"integer","description":"Max total travel time in minutes","default":60}},
     "required":["from_lat","from_lon","destinations"]}},
    {"name":"transit_map_trips","description":"Active transit trips in a bounding box. Like a live transit map — see what buses/trains are near a location right now.",
     "inputSchema":{"type":"object","properties":{
         "min_lat":{"type":"number"},"min_lon":{"type":"number"},
         "max_lat":{"type":"number"},"max_lon":{"type":"number"},
         "zoom":{"type":"integer","description":"Zoom level 1-19 (default: 12). High zoom=local, low=long-distance"},
         "start_time":{"type":"string","description":"ISO datetime (default: now)"},
         "end_time":{"type":"string","description":"ISO datetime (default: start + 30 min)"}},
     "required":["min_lat","min_lon","max_lat","max_lon","start_time","end_time"]}},
]

# ── Tool dispatch ───────────────────────────────────────────────────

def handle_call(name, args):
    try:
        return _dispatch(name, args)
    except KeyError as e:
        return {"error": f"Missing required parameter: {e}"}
    except (ValueError, TypeError, IndexError) as e:
        return {"error": f"Invalid parameter: {e}"}

def _dispatch(name, args):
    if name == "transit_plan":
        wm = args.get("max_walking_minutes")
        if wm is None: wm = 30
        return plan_route(args["from_lat"], args["from_lon"],
                          args["to_lat"], args["to_lon"],
                          time=args.get("time"), arrive_by=args.get("arrive_by",False),
                          max_transfers=args.get("max_transfers",5),
                          transit_modes=args.get("transit_modes"),
                          num_itineraries=args.get("num_itineraries",3),
                          max_walk_sec=wm * 60)
    elif name == "transit_geocode":
        return geocode_stop(args["query"], limit=args.get("limit",5))
    elif name == "transit_departures":
        return departures(args["stop"], count=args.get("count",10),
                         time=args.get("time"), window=args.get("window"))
    elif name == "transit_nearby_stops":
        return nearby_stops(args["lat"], args["lon"], radius=args.get("radius",1000))
    elif name == "transit_reverse_geocode":
        return reverse_geocode(args["lat"], args["lon"])
    elif name == "transit_trip":
        return trip_details(args["trip_id"])
    elif name == "transit_health":
        return health()
    elif name == "transit_reachable":
        return reachable(args["lat"], args["lon"], args["max_time_min"],
                        time=args.get("time"), arrive_by=args.get("arrive_by",False))
    elif name == "transit_refresh":
        return refresh(args["itinerary_id"])
    elif name == "transit_rentals":
        return rentals_nearby(args["lat"], args["lon"], radius=args.get("radius",1000))
    elif name == "transit_one_to_many":
        return one_to_many(args["from_lat"], args["from_lon"],
                          args["destinations"], mode=args.get("mode","WALK"),
                          max_sec=args.get("max_sec",3600),
                          arrive_by=args.get("arrive_by",False))
    elif name == "transit_one_to_many_transit":
        wm = args.get("max_walking_minutes")
        if wm is None: wm = 15
        tm = args.get("max_travel_minutes")
        if tm is None: tm = 60
        return one_to_many_transit(args["from_lat"], args["from_lon"],
                                   args["destinations"],
                                   time=args.get("time"),
                                   arrive_by=args.get("arrive_by",False),
                                   max_transfers=args.get("max_transfers",5),
                                   transit_modes=args.get("transit_modes"),
                                   max_walk_sec=wm * 60,
                                   max_travel_minutes=tm)
    elif name == "transit_map_trips":
        return map_trips(args["min_lat"], args["min_lon"],
                        args["max_lat"], args["max_lon"],
                        args["start_time"], args["end_time"],
                        zoom=args.get("zoom",12))
    return {"error": f"Unknown tool: {name}"}

# ── MCP JSON-RPC loop ───────────────────────────────────────────────

def send(resp):
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(errors="replace")

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: req = json.loads(line)
        except Exception: continue
        if not isinstance(req, dict): continue
        if "id" not in req: continue
        rid = req.get("id")
        method = req.get("method")
        if not isinstance(method, str): method = ""
        if method == "server/discover":
            send({"jsonrpc":"2.0","id":rid,"result":era_result({
                "supportedVersions":[ERA_VERSION],
                "capabilities":{"tools":{}}})})
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":rid,"result":era_result({"tools":TOOLS})})
        elif method == "tools/call":
            params = req.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                send({"jsonrpc":"2.0","id":rid,"error":{"code":-32602,
                    "message":"missing required param: params (with string 'name')"}})
                continue
            try:
                result = handle_call(params["name"], params.get("arguments", {}))
                is_err = isinstance(result, dict) and ("error" in result or "transport_error" in result)
                payload = {"content": [{"type":"text","text":json.dumps(result,indent=2)}]}
                if is_err:
                    payload["isError"] = True
                send({"jsonrpc":"2.0","id":rid,"result":era_result(payload)})
            except Exception as e:                       # dispatch-level only (shouldn't happen)
                send({"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":str(e)}})
        elif method.startswith("notifications/"): pass
        elif method == "ping":
            send({"jsonrpc":"2.0","id":rid,"result":{}})
        else:
            if rid is None and "id" not in req: continue   # §3/§1: no-id ⇒ never respond
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}})

if __name__ == "__main__":
    main()
