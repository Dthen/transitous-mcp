# Synthetic test fixtures

Every file in this directory is a **constructed** example body, written for this
test suite against the pinned API shapes. None of them is a captured live
response, a copy of a production response, or derived from one; the numbers in
them exist to express the vectors the tests assert. Bodies are deliberately small
and are read fresh per call by ``tests/fixture_support.py``.

The table below is the manifest checked by
``tests/test_fixtures_integrity.py``: one row per file, its kind (always
``synthetic``), the SHA-256 of the file's bytes, and the test concern it serves.
Synthetic bodies are named with a ``synthetic_`` prefix so no filename can be
mistaken for a capture.

| fixture | kind | content_sha256 | purpose |
| --- | --- | --- | --- |
| `synthetic_continuation_pages.json` | synthetic | `e38a05d8a01dd60ed49fff616aeaf92c3bc48c67d94ea22e1554d193cc0eeaa0` | two adjacent plan pages with their own cursors and itineraries |
| `synthetic_geocode_ayr.json` | synthetic | `687c37cf89fa6e703f68284cafcaf0983378eb4a48dbfedc6e81466f36d0f06d` | three Ayr matches: a place, a stop and an Australian namesake |
| `synthetic_geocode_ayr_station.json` | synthetic | `96840c2d82d6f4af65e5fd4351aec976fb977d6b54c66a55d32127ebbef10daa` | two Ayr station matches, the first a STOP with the required identity |
| `synthetic_geocode_man_picc_rrail.json` | synthetic | `14039fbf93c9667cc48d0b45be689e7764c4e778b173aec4f0034c1e5c4f2448` | one regional-rail Manchester Piccadilly stop match |
| `synthetic_geocode_manchester_piccadilly_station.json` | synthetic | `52fe9919f0cf0f94f3c74db23b1d871a223599689cc92754db38d718c15a214d` | five matches mixing STOP, PLACE and ADDRESS, including zero importance and a negative level |
| `synthetic_geocode_newcastle_central_station.json` | synthetic | `e4df5d09bdaaeb38976dbd091d27c7fc51038350cc0b247c389b0622ff80c32e` | ten geocode matches whose first is a SUBWAY/BUS stop and a rail stop ranks lower |
| `synthetic_geocode_newcastle_stop_rail.json` | synthetic | `5bdfb915644b6aa00f497a8db538c613a385e9dc7260390419e057706304c7ab` | two rail stop matches for the stop-type filter check |
| `synthetic_map_trips_window.json` | synthetic | `33172f43f5ca9c872a483535e9860db30c08249e0d7183fb5f0ca131381338a3` | two map-trip segments with fractional distance, polyline and realtime flags |
| `synthetic_naive_arriveby.json` | synthetic | `b1efb7d62a88db4eb94648a1f3cb84ed351df7c5d94213f5dc039c8c146a149e` | single itinerary ending before a naive arrive-by deadline |
| `synthetic_one_to_many_street.json` | synthetic | `925cf2798984ba8e94d5179f551068a8b80d934624530dfab4a7c0bfc3d4f4ee` | three street durations: fractional, zero and unavailable |
| `synthetic_one_to_many_transit.json` | synthetic | `7083ad7a23e7c5ff760b346b70bb6061813ab2fc1a6a09f1a2c21289c159c591` | intermodal body plus malformed length and missing-member variants |
| `synthetic_plan_ams_ber.json` | synthetic | `c54ff886bac054b0d03d775a9ff12c35af356eab37c313c18a5fe09b47387276` | two conserving Amsterdam-Berlin itineraries with Brussels/Berlin display zones |
| `synthetic_plan_ayr_man_stopids_n3.json` | synthetic | `eec9d7e677d4e5845f07bac471b295710da378ade795c6e625bd2ab8e1d6bde4` | three-itinerary Ayr-Manchester stop-id plan used for count and cap checks |
| `synthetic_plan_ayr_man_stopids_n8.json` | synthetic | `4bb9bea191c6e2e36e987aba358d15420619b9b7d747e62c08ad0fecde3288bb` | eight-itinerary Ayr-Manchester page: the 15780/15660/15480 elapsed series and change counts |
| `synthetic_plan_ayr_ncl_arriveby.json` | synthetic | `a7fa655ea89436a77ed64f9c6f1e5cc1c4e0df8a559645db2d8332f0f9476fe2` | single arrive-by itinerary arriving at the 22:00 deadline |
| `synthetic_plan_ayr_ncl_coord.json` | synthetic | `9d7ade275ef147c1920399e8a6b1b671582947ee6571ba064b1b6e1c94105a27` | five-itinerary Ayr-Newcastle plan: the 14100 s vector, two dummy internal walks, place cancellation tri-state and cursors |
| `synthetic_plan_ayr_ncl_stopids_n3.json` | synthetic | `ad7d2cde44b193a37084a64305901ae672d4d2106b87334a1651a86e53e76259` | three-itinerary stop-id plan: the 12540 s three-leg, two-change fastest option and both page cursors |
| `synthetic_plan_ber_muc.json` | synthetic | `beabbd09969af2a72a1e0a94a069b7fff21aacb54c39c806c905f31997571d4d` | two conserving Berlin-Munich itineraries for corpus-wide conservation |
| `synthetic_plan_cancelled_transit_leg.json` | synthetic | `04003dcedc2bb7e3064fe9b83ae54a36dd75c04d88b5fc99fc533503c5d7b7af` | itinerary whose single transit leg and both stops are genuinely cancelled and realtime |
| `synthetic_plan_direct_alternatives.json` | synthetic | `5bcc8e4fe61604fadf554fc15c75a4ff4d0422fb0298a34c90572fb6662195a3` | one 12600 s street-only direct alternative and no itineraries |
| `synthetic_plan_empty_constraints.json` | synthetic | `f19ea19778b531804f997c2f9302e0d0f5277500fcf82d67f661daf83cac5640` | empty plan used with both endpoint walk caps set to zero |
| `synthetic_plan_empty_no_service.json` | synthetic | `1e2e5bc373733fcb86c55dcc800c2beff983b24ae768e9725933978183495061` | structurally complete empty plan with both cursors, for the no-routes predicate |
| `synthetic_plan_glw_edi_coord.json` | synthetic | `8b3d6aa82d6eaa1b6069d4b7a25b1d6bbfb043a365a6508ecc2f208ef4c608a1` | three-itinerary Glasgow-Edinburgh plan: the 4620 s vector and a 660 s access walk |
| `synthetic_plan_glw_edi_naive_reject.json` | synthetic | `488cee03e7bf177343c982fb371dee36c8f58ba959a0f12da7c1b0a964d43fcf` | HTTP 400 body the plan layer must classify as bad_request |
| `synthetic_plan_glw_edi_offset.json` | synthetic | `506ce42adefe6bb70b7fba0fc453138019468dae180031c9c49c835f31d3ce70` | Glasgow-Edinburgh plan whose journey starts inside the 08:00Z hour, for offset-instant display checks |
| `synthetic_plan_glw_edi_stopids.json` | synthetic | `5859beb51b52960fc18c7a61d79fb2c54291ddf92c4fcedbbb930ecf636e7059` | minimal stop-id Glasgow-Edinburgh plan for inventory coverage |
| `synthetic_plan_glw_ncl_direct.json` | synthetic | `2efc143bb1a9fb2168860c5309ed2bc270b3ee6cce1bce87bcb75152ede5a307` | two direct Glasgow-Newcastle itineraries, one with five intermediate stops including Edinburgh |
| `synthetic_plan_glw_ncl_walk60.json` | synthetic | `d9d82cb569fdde67b905905bb47088bd42016e42f59d300b33c767ec524f653a` | two conserving Glasgow-Newcastle itineraries with 60 s endpoint walks, for cap conversion checks |
| `synthetic_plan_ncl_ayr_depart.json` | synthetic | `6b47801ce2c2702726b9e80858c21e1e71b510e4457b046e5e143a0ad163985c` | overnight Newcastle-Ayr plan: the 34380 s vector, two dummy walks and a next-day arrival |
| `synthetic_reachable_pareto.json` | synthetic | `e5391b6c1e9dc375970ea14480f7d3a1ca14ef110af60603fa706e6d787fef52` | one-to-all body with three places, one lacking duration and k |
| `synthetic_return_next_morning.json` | synthetic | `71ee7dbf06bf677387d52ad09da09b9cbbe33fd210cdc24a9986e7565985bed8` | overnight Ayr-Glasgow itinerary arriving the next morning |
| `synthetic_stop_metro_central.json` | synthetic | `6d2cdf6612ae9f4331cf423d8235844a9891c4f3cd5e9d7d2eb65950a29f24e0` | stop-metadata body with a zone and no parent id |
| `synthetic_stoptimes_ayr_p3.json` | synthetic | `ac5e6c9ac52b58ec66952c06d92190cec30e20ee7dbeecf01f56beaf32e1434a` | twenty scheduled-only Ayr departures with platform and parent ids |
| `synthetic_stoptimes_ber_hbf.json` | synthetic | `cc8d0b0f76eac4e3f3803ac75294cda4f9e344a76570a693e11c8bffda937185` | ten Berlin Hbf departures, one delayed and two scheduled-only |
| `synthetic_stoptimes_glc_p11.json` | synthetic | `3549d1c9ce16c7c90a26870fc8712d9ce3348ffdc8159ab5e87cfae64959cbf4` | eleven scheduled-only Glasgow Central departures |
| `synthetic_trip_cancelled_stoptime.json` | synthetic | `38570c2c99841ceb83c19aafac3bf4df8d28a9b6b441bfef59cf2892384c2746` | three stop-times covering trip-cancelled, stop-cancelled and neither |
| `synthetic_trip_intermediate_stops.json` | synthetic | `c2827b6cc05ea34d65358e2406a39ccb1ac7d2e5be6a4d8160e06f08de151257` | single-leg trip with three intermediate stops and a scheduled pair |
| `synthetic_westbound_date_line.json` | synthetic | `d9b79f752acd02a5690f2439a78dba0ca198dab57cdf5c5ed314aa6fc1fdd997` | Auckland-Honolulu itinerary crossing the date line westbound |
| `synthetic_window_error.json` | synthetic | `6627c5bb1ee2d24c179aaf28b626f34d88d5afc92bffeaaedd531447041dad3f` | two 400 bodies carrying the timetable-window interval, one per clock form |

Total: 39 files, 186740 bytes (182.4 KiB) of JSON. Bodies are plain JSON: they are far
below any size that would justify archive storage, so they are stored verbatim
and reviewed as text.
