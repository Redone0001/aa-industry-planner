# EVE structure industry bonuses: research and implementation

Researched 24 November 2026. The implementation uses CCP SDE release **20261120T165826Z**, build **3724492**, downloaded directly from CCP. The compiled catalog contains **14 hulls, 9 industry service modules, 125 Standup industry rigs, and 18 target filters**.

## Result and administrator workflow

The administrator selects a structure's hull and solar system, adds its installed industry service modules with their online states, and selects its installed industry rigs. The application derives material, time, and installation-cost multipliers for the actual job activity and product's SDE group/category. The system supplies the security band; the administrator never enters a security-adjusted rig percentage.

In Django admin, open **Facilities**, select the hull, and use **Installed industry service modules** and **Installed industry rigs**. Save to see the calculated table. Manufacturing/reaction assignment records are created automatically; their tax settings remain editable. The Facilities monitor also displays the fit and multiplier table. Rows summarize representative products; the planner evaluates each actual product ID.

The normal inputs are:

- Facility name, structure ID, hull type, solar system, and access groups.
- Actual industry service module types and whether each is online.
- Actual industry rig types, shared across the entire structure.
- Facility tax and SCC surcharge, which are separate from material/time efficiency.
- For a supercapital shipyard only: explicit confirmation that sovereign-nullsec supercapital infrastructure is active. Hull and system security alone cannot establish that condition.

A structure ID identifies a particular deployed structure; a hull type ID identifies its design. Neither the SDE nor a structure ID establishes the current fit, fuel, docking access, or live infrastructure state. These remain administrator declarations in this implementation.

[Ravworks' configuration page](https://ravworks.com/config) exposes the same useful concepts: a structure editor, per-product production locations, separate structure and rig ME/TE fields, and separate character time modifiers. It is a workflow reference; this implementation uses CCP data and does not use Ravworks' private APIs or source code.

## The primary data is available

The essential discovery is that modern CCP SDE includes explicit industry mapping tables. It is unnecessary to classify rigs by English names or scrape descriptions.

| CCP table | Used for |
| --- | --- |
| `industryModifierSources` | For a hull/rig type, maps each activity and metric (`material`, `time`, `cost`) to Dogma attribute IDs and optional target-filter IDs. |
| `industryTargetFilters` | Defines the eligible product group IDs and category IDs for each filter. |
| `typeDogma` | Actual hull multipliers, rig percentage values, security modifiers, fitting restrictions, slots, and calibration. |
| `dogmaEffects` | Resolves how raw rig percentages modify the structure's industry attributes, including security multiplication. |
| `dogmaAttributes` | Attribute defaults; some final structure attributes exist only through defaults and effects. |
| `industryInstallationTypes` | Maps service module type IDs to assembly lines. |
| `industryAssemblyLines` | Lists the service's activity, permitted product groups/categories, and base multipliers. The selected modern service lines have base multipliers of 1. |
| `industryActivities` | Activity definitions in the SDE assembly-line namespace. |
| `types`, `groups` | Type names and group/category relationships. |
| `mapSolarSystems` | System identity and raw security status; supplied through the installed `eve_sde` models. |
| `blueprints` | Base production inputs and time; already consumed by the planner's recipe provider. |

CCP documents direct table and record downloads in its [Static Data API documentation](https://developers.eveonline.com/docs/services/static-data/) and its [19 November 2026 announcement](https://developers.eveonline.com/blog/the-sde-over-http-static-data-dynamically). Resolve `latest/` once and use that response's release-specific table URLs so all downloaded tables belong to one release. CCP retains only the current and previous HTTP releases; keep source snapshots if later reproduction is required.

Direct primary data entry points:

- [Latest release manifest](https://sde.evetech.net/tranquility/latest/)
- [Industry modifier sources](https://sde.evetech.net/tranquility/latest/industryModifierSources.jsonl)
- [Industry target filters](https://sde.evetech.net/tranquility/latest/industryTargetFilters.jsonl)
- [Service installation types](https://sde.evetech.net/tranquility/latest/industryInstallationTypes.jsonl)
- [Assembly lines](https://sde.evetech.net/tranquility/latest/industryAssemblyLines.jsonl)
- [Type Dogma](https://sde.evetech.net/tranquility/latest/typeDogma.jsonl)
- [Dogma effects](https://sde.evetech.net/tranquility/latest/dogmaEffects.jsonl)
- [Dogma attributes](https://sde.evetech.net/tranquility/latest/dogmaAttributes.jsonl)

The bundled `aa_industry_planner/data/industry_catalog.json` records the release, build, each input table's version, and SHA-256. It contains the required subset of those records. The app reads this local file; a planning request does not download game data or contact Ravworks.

## Hull bonuses

These are facility-only multipliers extracted from the current Dogma and industry source tables. They apply only when the appropriate online service supports the requested job.

| Hull | Manufacturing material | Manufacturing/science time | Manufacturing/science cost | Reaction material | Reaction time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raitaru | 0.99 | 0.85 | 0.97 | — | — |
| Azbel | 0.99 | 0.80 | 0.96 | — | — |
| Sotiyo | 0.99 | 0.70 | 0.95 | — | — |
| Astrahus / Fortizar / Keepstar / Palatine Keepstar | 1 | 1 | 1 | — | — |
| Athanor | 1 | 1 | 1 | 1 | 1 |
| Tatara | 1 | 1 | 1 | 1 | 0.75 |
| 'Moreau' Fortizar | 1 | 0.90 | 0.98 | — | — |
| 'Draccous' / 'Horizon' Fortizar | 1 | 0.85 | 0.97 | — | — |
| 'Marginis' / 'Prometheus' Fortizar | 1 | 1 | 1 | — | — |

Science here means copying, invention, ME research, and TE research. A manufacturing material bonus does not reduce invention datacores or produce a research material discount. An unrigged Tatara has a reaction time bonus, not a reaction material discount. Citadels/refineries can offer manufacturing through compatible service modules, but do not acquire engineering-complex hull bonuses by doing so.

The original CCP [Engineering Complex announcement](https://www.eveonline.com/news/view/building-dreams-introducing-engineering-complexes) explains the multiplicative relationship. CCP's [Refinery announcement](https://www.eveonline.com/news/view/the-goo-must-flow-everything-about-refineries-and-moon-mining) describes the Tatara reaction time bonus and refinery-only reaction services. Those historical posts explain the design; current numeric rules come from the downloaded SDE.

## Service modules enable jobs

| Module type ID | Module | Capability |
| --- | --- | --- |
| 35878 | Standup Manufacturing Plant I | The categories and groups in assembly line 175; excludes capital combat hulls requiring a shipyard. Includes manufacturing components and structures. |
| 35881 | Standup Capital Shipyard I | Capital groups in line 176; requires an eligible large/XL hull and cannot operate in highsec. |
| 35877 | Standup Supercapital Shipyard I | Capital/supercapital groups in lines 176 and 177; restricted to Sotiyo and confirmed sovereign-nullsec infrastructure. |
| 35891 | Standup Research Lab I | ME research, TE research, copying. |
| 45550 | Standup Hyasyoda Research Lab | The same research activities; the exported industry lines do not grant an extra job time/material multiplier. |
| 35886 | Standup Invention Lab I | Invention. |
| 45537 | Standup Composite Reactor I | Groups 428, 429, 4932: composite reaction products. |
| 45538 | Standup Hybrid Reactor I | Group 974: hybrid reaction products. |
| 45539 | Standup Biochemical Reactor I | Groups 712, 4096: biochemical and molecular-forged materials. |

Reaction modules require refineries and cannot operate in high security space. An L reactor rig covers multiple reaction families but does not enable a missing reactor service. An offline service does not enable jobs.

Use SDE IDs and filters instead of assumptions such as "all capitals need the capital shipyard" based on a hull's apparent size: the basic manufacturing line explicitly lists eligible ship groups, including freighters. Likewise, building a capital component is a manufacturing job; it does not need a capital shipyard merely because its name contains "capital."

The SDE assembly-line reaction activity ID is **9** in this release. The planner's existing ESI job reconciliation uses its own activity mapping. The new module translates SDE line IDs to named activities and does not reuse the ESI numeric mapping for SDE lines.

## Rig coverage

Filters are unions: a product matches when its category **or** its group is listed. An absent filter means the modifier applies to the activity generally. Evaluate the product being built, not the blueprint's group or the input material's group.

| Filter | Product family |
| ---: | --- |
| 1 | Drones / fighters |
| 2 | Equipment: categories 7, 20, 22, plus groups 12, 340, 448, 649 |
| 3 | Ships: categories 6 and 32 |
| 4 | Charges |
| 5 / 6 | Small T1 / advanced small ships |
| 7 / 8 | Medium T1 / advanced medium ships |
| 9 / 10 | Large T1 / advanced large ships |
| 11 | Capital ships |
| 12 | Structures, structure equipment, relevant components, and fuel-block groups |
| 13 | Basic capital components: group 873 |
| 14 | Components: groups 332, 334, 716, 964 |
| 15 | Advanced capital components: group 913 |
| 16 | Hybrid reactions: group 974 |
| 17 | Biochemical reactions: groups 712, 4096 |
| 18 | Composite reactions: groups 428, 429, 4932 |

The actual group/category arrays are retained in the catalog. A single optional group field on a rig cannot express all of this coverage. Larger rigs often have several modifier/filter pairs; Thukker XL rigs can use different material percentages for different component filters.

The current XL ship rig uses the broad ship filter, which includes capital/supercapital ship categories when the service permits them. This follows the primary SDE mappings; it should be checked in an actual EVE industry preview during deployment acceptance, especially if comparing with older tools or reports.

## Multipliers and security

Typical Standup engineering rig base percentages are material **−2% / −2.4%** and time **−20% / −24%** for T1/T2. Some research rigs modify cost or time instead. Read each rig's actual attributes rather than assigning percentages solely from its tech level.

| Rig family | Highsec strength | Lowsec strength | Nullsec / wormhole strength |
| --- | ---: | ---: | ---: |
| Ordinary engineering rigs | 1 | 1.9 | 2.1 |
| Reactor rigs | Prohibited | 1 | 1.1 |
| Thukker engineering rigs | 0.1 | 1.9 | 0.1 |

Thukker's special component material source is **−3.7%** before its security multiplier. An XL Thukker rig can use the ordinary −2% source for other covered products. Its time bonus also uses that rig's own security attributes. Applying the ordinary nullsec 2.1 factor to Thukker or reactor rigs gives incorrect results.

The classifier follows the SDE solar-system convention used by this application's dependency: wormhole IDs are identified first; highsec starts at raw security **0.45**; positive security below that is lowsec; zero/negative security is nullsec. A missing security status is an explicit configuration error. Wormholes share rig strength with nullsec, but this does not grant sovereign-nullsec supercapital capability.

For a conventional single matching rig:

```text
rig_multiplier = 1 + (raw_negative_bonus_percent × security_strength / 100)
material_multiplier = hull_material_multiplier × matching_rig_material_multiplier
time_multiplier = hull_time_multiplier × matching_rig_time_multiplier
```

The implementation follows the general data rather than assuming one matching rig:

1. Collect the matching industry target attribute IDs for the requested activity/metric from the hull and installed rigs. Deduplicate IDs.
2. Initialize each target from the hull's explicit Dogma value or the attribute default.
3. Resolve each rig's local security multiplication (`ItemModifier`, operation 4).
4. Apply its projected percentage modifiers to the relevant structure attributes (`ItemModifier`, operation 6).
5. Multiply the distinct applicable final target attributes to obtain the facility multiplier.
6. Apply blueprint ME/TE and character time modifiers separately in the existing planner.

This avoids both ignoring effect-derived attributes and applying a shared final structure attribute twice. The existing material-quantity rounding and minimum-per-run behavior remains in the production engine.

### Verified examples

| Configuration and eligible product | Material multiplier | Time multiplier |
| --- | ---: | ---: |
| Sotiyo + T2 XL Ship Manufacturing Efficiency, highsec | 0.966240 | 0.532000 |
| Same, lowsec | 0.944856 | 0.380800 |
| Same, nullsec / wormhole | **0.940104** | **0.347200** |
| Tatara + T2 L Reactor Efficiency, lowsec | 0.976000 | 0.570000 |
| Same, nullsec / wormhole | **0.973600** | **0.552000** |
| Raitaru + Thukker basic capital component ME, lowsec | **0.920403** | 0.850000 |
| Same, nullsec / wormhole | 0.986337 | 0.850000 |

The Sotiyo nullsec material example is `0.99 × (1 − 0.024 × 2.1) = 0.940104`: a **5.9896%** facility material reduction. Time is `0.70 × (1 − 0.24 × 2.1) = 0.3472`: a **65.28%** reduction. These are facility bonuses before blueprint and character effects. Highsec XL examples apply to existing structures; new XL anchoring is restricted by the game.

## Implementation, compatibility, and limitations

- `aa_industry_planner/industry.py` is a pure calculation/validation module, using decimal arithmetic and the bundled catalog.
- `Facility.structure_type` enables the fitted mode. `FacilityModule` and `FacilityFittedRig` store the declared fit at structure scope. Rigs are not duplicated per service.
- Fitted service modules maintain stable manufacturing/reaction `FacilityService` records for assignments and taxes. Removing/offlining the last compatible module marks that activity unavailable. The planner also checks the exact product against the online modules.
- New calculations snapshot the applied multipliers, hull/module/rig IDs, security band, infrastructure declaration, and catalog release. Existing saved plan snapshots retain their previous assumptions until the owner explicitly refreshes the plan.
- Facilities without a hull remain in legacy manual mode. Their manual services and rig multipliers are preserved. After declaring a hull, enter the complete fit; legacy multipliers are ignored to prevent double-counting.
- The migration uses the app's existing shared SDE dependency, without requiring the SDE 2.x-only migrations. The calculator does not depend on Dogma models being available in `django-eveonline-sde` 0.2.x.
- Admin formsets validate the submitted replacement fit, including deletions, before saving: fitting hull/type restrictions, service slots, three rig slots, calibration, duplicate rig groups including tech/faction variants, rig size, highsec restrictions, and the supercapital declaration. This is not a complete ship fitting simulator: unrecorded combat modules, total remaining CPU/powergrid, fuel, and actual in-game online state are not checked.
- The calculator and preview cover manufacturing, reactions, copying, invention, ME research, and TE research **facility multipliers**. Full production planning remains manufacturing and reactions. Invention probability, datacore/decryptor planning, copying/research job scheduling, refining yield, planetary production, moon extraction, fuel budgets, and character skill import are not added here.
- Legacy Outpost Conversion Rigs are excluded from automatic selection. They contain cross-rig Dogma overrides and cannot safely be flattened into ordinary Standup rules. Their use requires a dedicated extension or retaining legacy manual configuration. Faction Fortizar hull bonuses themselves are included.
- Unknown fitting types and unsupported effect operations fail explicitly. The catalog is pinned to the app, not refreshed automatically during an SDE database update. Rebuild and validate it when CCP changes these rules.

## Refreshing the catalog

Download the ten JSONL tables named in `scripts/build_industry_catalog.py` and save their release manifest. Use that manifest's URLs, not ten independent `latest/` requests. The compiler accepts both HTTP keys such as `typeID` and JSONL archive `_key` records.

```sh
python scripts/build_industry_catalog.py \
  --sde-dir /path/to/saved-ccp-release \
  --release-file /path/to/saved-release-manifest.json \
  --output aa_industry_planner/data/industry_catalog.json
pytest -q
```

Review table/version/hash changes, altered target filters, new fitting types, new effect operations, and expected multipliers before shipping. Retain the source release outside the repository if reproducibility beyond CCP's HTTP retention window is required.

## Validation

Tests include current primary-data vectors for hulls, security classes, standard engineering rigs, reactors, Thukker component subtypes, research/invention separation, service permissions, and invalid fittings. All 125 shipped rigs are evaluated in a compatible fit. Database tests cover the real admin POST, invalid-fit rejection, planner integration, saved fit provenance, offline/delete transitions, tax preservation, and monitor rendering.

A live EVE client comparison remains a deployment acceptance step; no authenticated game client or production AA instance was used for this research. Compare at least an unrigged hull, one matching and nonmatching product, reactor subtypes, the lowsec Thukker case, and material quantities near rounding boundaries.

## Planner controls added in 0.1.5

New location assignments use these target-filter families instead of individual SDE item groups. An empty family remains the activity catch-all. Specific group families take precedence over broad category families such as Ships. Legacy exact-group assignments remain in force until removed.

Manufacturing plans default to blueprint ME 10 / TE 20, with per-plan defaults and nullable per-blueprint overrides; reactions use neither. ME/TE no longer belongs to ItemPolicy. The upgrade preserves historical saved ME/TE as per-plan overrides. An explicit refresh recalculates current facilities and configuration without changing the plan ID; it retains compatible job links and reports released incompatible links.
