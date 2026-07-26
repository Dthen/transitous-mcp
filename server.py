#!/usr/bin/env python3
"""Transitous MCP Server — free public transit routing via MOTIS API."""

import json, sys, urllib.request, urllib.parse, urllib.error
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
    if s < 60: return f"{s}s"
    h, m = divmod(s, 3600); m //= 60
    return f"{h}h{m:02d}m" if h else f"{m}min"

def fmt_time(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone()  # convert to system local time (BST/GMT)
        return local.strftime("%H:%M")
    except: return iso

def plan_route(from_lat, from_lon, to_lat, to_lon,
               time=None, arrive_by=False, max_transfers=5,
               transit_modes=None, num_itineraries=3, max_walk_sec=900):
    if transit_modes is None: transit_modes = ["TRANSIT"]
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
                "from": data.get("from",{}).get("name","?"),
                "to": data.get("to",{}).get("name","?")}
    results = []
    for itin in itins[:num_itineraries]:
        legs = []
        for leg in itin.get("legs", []):
            l = {"mode": leg.get("mode","?"),
                 "from": leg.get("from",{}).get("name","?"),
                 "to": leg.get("to",{}).get("name","?"),
                 "duration": fmt_dur(leg.get("duration",0)),
                 "departure": fmt_time(leg.get("from",{}).get("departure","")),
                 "arrival": fmt_time(leg.get("to",{}).get("arrival",""))}
            if leg.get("routeShortName"): l["route"] = leg["routeShortName"]
            elif leg.get("routeLongName"): l["route"] = leg["routeLongName"]
            if leg.get("headsign"): l["headsign"] = leg["headsign"]
            legs.append(l)
        results.append({"option": len(results)+1,
                        "duration": fmt_dur(itin.get("duration",0)),
                        "transfers": itin.get("transfers",0),
                        "departure": fmt_time(itin.get("startTime","")),
                        "arrival": fmt_time(itin.get("endTime","")),
                        "legs": legs})
    return {"from": data.get("from",{}).get("name","?"),
            "to": data.get("to",{}).get("name","?"),
            "itineraries": results}

def geocode_stop(query):
    data = api_get("/api/v1/geocode", {"text": query})
    if "error" in data: return data
    return {"query": query, "results": [
        {"name": i.get("name","?"), "lat": i.get("lat"), "lon": i.get("lon"),
         "type": i.get("type","?"), "category": i.get("category","?"),
         "id": i.get("id","?")}
        for i in data[:5]]}

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
         "max_walking_minutes":{"type":"integer","description":"Max walk mins (default: 15)"}},
     "required":["from_lat","from_lon","to_lat","to_lon"]}},
    {"name":"transit_geocode","description":"Geocode a place name/address/stop to coordinates. Use before transit_plan.",
     "inputSchema":{"type":"object","properties":{
         "query":{"type":"string","description":"Place name, address, or stop"}},
     "required":["query"]}},
]

def handle_call(name, args):
    if name == "transit_plan":
        w = (args.get("max_walking_minutes") or 30) * 60  # default 30 min walk
        return plan_route(args["from_lat"], args["from_lon"],
                          args["to_lat"], args["to_lon"],
                          time=args.get("time"), arrive_by=args.get("arrive_by",False),
                          max_transfers=args.get("max_transfers",5),
                          transit_modes=args.get("transit_modes"),
                          num_itineraries=args.get("num_itineraries",3),
                          max_walk_sec=w)
    elif name == "transit_geocode":
        return geocode_stop(args["query"])
    return {"error": f"Unknown tool: {name}"}

def send(resp):
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()

def main():
    # Read lines from stdin — MCP uses \n-delimited JSON
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: req = json.loads(line)
        except: continue
        rid = req.get("id")
        method = req.get("method","")
        if method == "initialize":
            send({"jsonrpc":"2.0","id":rid,"result":{
                "protocolVersion":"2024-11-05",
                "capabilities":{"tools":{}},
                "serverInfo":{"name":"transitous-mcp","version":"1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":rid,"result":{"tools":TOOLS}})
        elif method == "tools/call":
            try:
                result = handle_call(req["params"]["name"], req["params"].get("arguments",{}))
            except Exception as e:
                result = {"error": str(e)}
            send({"jsonrpc":"2.0","id":rid,"result":{
                "content":[{"type":"text","text":json.dumps(result,indent=2)}]}})
        elif method.startswith("notifications/"):
            pass  # no response for notifications
        elif method == "ping":
            send({"jsonrpc":"2.0","id":rid,"result":{}})

if __name__ == "__main__":
    main()
