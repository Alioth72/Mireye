# Build Brief II: The Monitor

On June 9 this year, Seattle passed an emergency moratorium on new data centers. Anyone whose land was quietly worth data-center money went to bed with that option and woke up without it — and nothing they subscribed to told them. That is how it works everywhere: land values move in council chambers months before they move in listings, the entire record is public, and almost nobody monitors it. One tracker counts data-center and battery moratoria going from 59 in all of 2025 to ~294 in the first seven months of 2026. The chambers are busy.

##  The challenge

**Build a monitoring agent.** Give it a place to watch — a county, a town, or a single address and everything around it — and it keeps reading the public record, alerting the person who cares only when something material moves, before they would have heard any other way.

Monitoring is the point. Brief I asked for an agent that answers a question once; this one runs forever, and most of its job is deciding *not* to alert. The address version is the sharpest form: someone owns land, is buying it, or holds an option — they hand your agent the address (`POST /v1/geocode`, 1 credit) and it watches everything within reach of that coordinate.

Record-watching products already exist and charge $129 a month per market. They fire when someone says "rezoning". What none of them can answer is **does this matter *here*** — a physical question. A sewer line only moves value where slope and floodplain allow building; a moratorium only bites where transmission exists. Mireye answers the physical half. Wiring the two together is the project.

## The loop

1. **Watch** — agendas, minutes, meeting transcripts, local news for your place's jurisdiction. This is your second dataset: free, public, unstructured. Structuring it is the moat.
2. **Detect** — rezoning, annexation, comp-plan amendment, utility extension, moratorium, big permit.
3. **Stage** — *proposed / heard / adopted*. A first reading is not a done deal, and your alert must never pretend it is.
4. **Scope** — which ground does it touch?
5. **Score** — fetch Mireye there: can this land actually respond to this event?
6. **Alert or stay quiet** — citing the meeting *and* the fields. Silence is a decision too.

Steps 5–6 are what separate you from the keyword feed.

## Worked example

Your agent is monitoring a county and a data-center moratorium reaches adoption. Who should hear about it? Only owners of land that had the option. No field is called `had_data_center_optionality` — derive it: `nearest_transmission_line_voltage_kv`, `nearest_substation_distance_m`, `fiber_broadband_available`, `slope_degrees`, `within_floodplain_polygon`, `intersects_wetland`, `intersects_protected_area`. High score = real option value lost, alert. Zero = never in the game, quiet. Same event, opposite materiality. The sharp version also notices: county A's moratorium quietly upgrades qualifying land in county B next door.

## The score: beat the newspaper

An alert is worth something only if it arrives before the person would have heard anyway. Replay 6–12 months of your place's past meetings through your agent and measure **lead time**: your alert date versus the adoption date versus the first local press coverage. An alert after the news is worthless; one at the first reading is the product. Report precision too — a monitor that cries wolf gets unsubscribed — and report the misses honestly.

## Rules

- **Own one place.** An active docket beats a famous name — confirm the jurisdiction publishes agendas, minutes, ideally video (Legistar, Granicus, CivicClerk, PrimeGov, YouTube) before committing.
- **Event-driven, not polling.** A place produces a handful of material events a month; each costs one vicinity fetch. If you are burning credits, your agent is polling and the architecture is wrong. Quote first, every time.
- **Still out of scope:** site selection, dashboards, parcel/ownership fields (300 credits per location). Your monitor works on coordinates and vicinities, not owners.

