# Alliance Auth Industry Planner

An installable Alliance Auth application for industrialists. It plans manufacturing and reaction chains from EVE SDE recipes, tracks connected industry jobs, and maintains inventory targets.

Version 0.1.15 fixes ESI reaction activity 9 matching, adds assignment/reassignment controls and unmatched-job explanations in All jobs, and orders that list by newest start time first. Structure, service and rig bonuses are calculated automatically from CCP's SDE. Automated tests use real Alliance Auth 5.6, Django 5.2 and eveuniverse 2.2 models on SQLite, with both SDE 0.2.0 / django-esi 10.1.0 / modeltranslation 0.19.17 and SDE 2.0.0 / django-esi 10.2.0 / modeltranslation 0.20.6 compatibility profiles. A production AA installation and real ESI/Discord accounts are still needed for deployment acceptance testing.

## Features

Item search uses a dropdown with mouse and keyboard selection, a short typing debounce, and result caches in the browser and AA cache. Queries retrieve only item IDs and translated names, with prefix matches ranked first and substring matches retained. Names remain language-aware. Each cache expires after five minutes.

The interface inherits the selected Alliance Auth theme (including Flatly, Darkly and Materia). Cards, buttons, forms, tables, navigation and status badges use native Bootstrap components; charts and tree accents use the theme's CSS variables. Change the theme through AA's existing user settings menu.

- Select an item and quantity; expand its manufacturing and reaction dependencies down to raw inputs. Refresh saved plans with current configuration; archive/restore them or remove one/all archived plans through a confirmation page.
- Aggregate shared components before sizing jobs, allocate stock once within a plan at each consuming job’s permitted locations, and show a collapsible dependency tree, a zoomable graph with shared inputs and item inspection, and a numbered production sequence.
- Set buy/import boundaries and an explicit recipe in item policies. Manufacturing plans default to blueprint ME 10 / TE 20, with plan defaults and individual blueprint overrides editable inside the planner. Reactions ignore ME/TE. Set maximum reaction runs or duration per job and effective character time bonuses in Configuration.
- Select structure hulls, installed service modules, online states and industry rigs in Django admin. Derive product-specific material/time/cost multipliers from CCP's SDE, including security and Thukker/reactor rules. Restrict facilities to AA groups and let users assign locations per activity or product group.
- Snapshot installation estimates using unmodified-material EIV, CCP adjusted prices, the activity's system cost index, facility cost bonuses, facility tax, SCC surcharge and optional Alpha tax.
- Collect hourly indices for configured systems and retain 90 days, with history charts for authorized facility monitors.
- Connect multiple owned characters and authorized corporations through AA SSO. Import all pages of assets and industry jobs, periodically and on demand. Support additive, manually declared stock that can be excluded to avoid duplication.
- Display all industry jobs, automatically link compatible running jobs, manually link older jobs, and calculate step completion from ESI's `ready` / `delivered` statuses.
- Keep one replenishment task per inventory target, updating quantities as stock and known jobs change. Highlight buy/import shortages without creating production plans for them.
- Provide a notification inbox and per-event preferences. Optional Discord DMs use embedded cards and the user's activated Discord Multiverse account in an administrator-selected guild.

## Install in an existing AA instance

Requires Python 3.11+, Alliance Auth 5.6.x and its existing database, Redis, Celery worker and Beat infrastructure. Install from the public GitHub repository in the AA virtual environment:

```sh
python -m pip install "aa-industry-planner @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.15"
```

To include Discord Multiverse:

```sh
python -m pip install "aa-industry-planner[discord] @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.15"
```

The public HTTPS installation does not require GitHub authentication.

To upgrade an existing planner installation, activate the AA virtual environment and run:

```sh
python -m pip install --upgrade --no-deps \
  "aa-industry-planner @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.15"
python manage.py migrate aa_industry_planner
python manage.py collectstatic --noinput
```

Restart AA's web and Celery worker processes afterwards. Version 0.1.15 adds no migrations or dependencies. Migration `0007` from v0.1.13 remains required for reminders, ongoing quantities and explicit job unlink preferences; `--no-deps` preserves the installed SDE/ESI versions. Existing manual facilities remain unchanged until an administrator declares their hull and complete fit.

Existing apps may require SDE 0.2.x. The planner supports that version and does not require upgrading to SDE 2.x. To preserve the older stack, install with explicit constraints:

```sh
python -m pip install --upgrade \
  "aa-industry-planner @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.15" \
  "django-eveonline-sde==0.2.0" \
  "django-esi==10.1.0" \
  "django-modeltranslation==0.19.17"
python -m pip check
```

This command also restores the dependency versions if v0.1.1 upgraded the Python packages and no SDE database migrations or data imports have been run since. If SDE 2.x migrations or imports have already run, inspect the database migration state before downgrading; a Python package downgrade does not reverse database changes.

Alternatively, install from a local checkout:

```sh
pip install /path/to/aa-industry-planner
# Include this extra if Discord Multiverse is not already installed:
pip install '/path/to/aa-industry-planner[discord]'
```

In your AA `local.py`, add missing dependencies and the app. Keep `modeltranslation` first, before SDE and other applications. Preserve AA's standard `LANGUAGES`; SDE's translated-field migrations expect that language set.

```python
INSTALLED_APPS += [
    "eve_sde",                 # omit if already installed
    "eveuniverse",             # omit if already installed
    "aa_industry_planner",
]
INSTALLED_APPS = ["modeltranslation"] + [
    app for app in INSTALLED_APPS if app != "modeltranslation"
]

# Optional: install/configure Multiverse using its own instructions.
# INSTALLED_APPS += ["aadiscordmultiverse"]
```

The app registers `/industry/` and its sidebar entry through AA hooks; no project URL changes are needed.

Add these periodic tasks, preserving your existing Beat schedule:

```python
from celery.schedules import crontab

CELERYBEAT_SCHEDULE.update({
    "Industry Planner: refresh ESI sources": {
        "task": "aa_industry_planner.tasks.refresh_all_sources",
        "schedule": crontab(minute="*/10"),
    },
    "Industry Planner: hourly public data and retention": {
        "task": "aa_industry_planner.tasks.refresh_public_data",
        "schedule": crontab(minute="7"),
    },
    "Industry Planner: evaluate inventory targets": {
        "task": "aa_industry_planner.tasks.evaluate_all_watches",
        "schedule": crontab(minute="*/10"),
    },
    "EVE SDE: check updates": {  # omit if already scheduled
        "task": "eve_sde.tasks.check_for_sde_updates",
        "schedule": crontab(minute="0", hour="12"),
    },
})
```

Migrate, collect assets, load the SDE, and restart AA's web, worker and Beat processes using your installation's normal procedure:

```sh
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py esde_load_sde
```

After adding facilities, queue the initial public-data import from an AA Django shell:

```python
from aa_industry_planner.tasks import refresh_public_data
refresh_public_data.delay()
```

## Administrator setup

1. Grant `aa_industry_planner.access_planner` to the intended industrialist groups.
2. Grant `aa_industry_planner.use_corporation_data` only to users allowed to connect their own corporation data. This permission supplements ESI scopes and in-game roles.
3. Grant `aa_industry_planner.manage_facilities` to people who should see the facility/index monitor. Django admin model permissions separately control editing structures and other backend settings.
4. Add a **Facility**, entering its deployed structure ID, **structure hull type**, SDE solar system, and optional allowed groups. An empty group list makes it available to all planner users. Load the SDE first so the real hull, module and rig types are available in the selectors.
5. Add its **Installed industry service modules**, set their online states, and select its **Installed industry rigs** on the same page. Save to see material/time/cost multipliers by activity and product family. Security strength and rig coverage are calculated automatically. Manufacturing/reaction assignment records are created from the declared services; their taxes remain editable. Only enable **Supercapital ready** after confirming the required sovereign-nullsec infrastructure.
6. Existing facilities without a hull retain their legacy manual multipliers. To convert one, select its hull and declare its complete module/rig fit; the old manual multipliers then stop applying. Rates are fractions: `0.01` = 1%. Multipliers are remaining fractions: `0.98` = 2% reduction. The declared fit is not verified against the deployed structure's current fuel, fitting, or access rights.
7. Users choose default manufacturing and reaction locations in **Configuration**, and can override them for the product families used by industry rigs. Leave the family empty for a catch-all. Specific families take precedence over broad Ships assignments; existing exact SDE group overrides are retained as legacy entries and take precedence until removed. Changing settings affects newly calculated plans; use **Refresh plan** to update an existing calculation.

SCC defaults to 4% for the two supported planning activities and is editable per service. Missing/stale prices (older than two days), unavailable system indices (older than six hours), and missing facilities are reported explicitly; an unknown installation estimate is never presented as zero.

See [the industry bonus research and rules](docs/industry-bonuses.md) for primary sources, formulas, coverage, examples and catalog refresh instructions. The bundled rules use CCP release `20261120T165826Z` (14 hulls, 9 service modules, 125 industry rigs). Legacy Outpost Conversion Rigs require manual configuration. Blueprint and character bonuses apply separately from the calculated facility multipliers.

## Plan controls and blueprint settings

New manufacturing plans assume **ME 10 / TE 20**. Set different defaults on the production form, then edit individual manufacturing blueprints under **Blueprint settings** in the saved plan. Empty per-blueprint ME or TE fields inherit that plan's defaults; `0` is an explicit unresearched value. **Apply blueprint settings & refresh** saves the changes and recalculates. Reactions never receive blueprint ME/TE discounts. Item policies now contain only buy/import and explicit recipe selection.

**Max reaction runs per job** and optional **Max reaction duration (hours)** limit job size; duration takes precedence when set. Neither setting is a production target. For example, 105 required runs with a 100-run cap create jobs of 100 and 5 runs. Existing stock reduces demand first, then required runs round up only to the recipe’s whole-run output. SDE production limits and the 30-day job limit still apply. Materials, time and installation fees use the actual runs in each job, including the smaller final batch. Refresh saved plans to apply this behavior.

**Refresh plan** recalculates the same plan with its saved blueprint settings and the current location assignments, fitted structures/rigs, item policies, character settings, prices, system indices, and stock when inventory is enabled. The plan ID and creation time remain stable; the displayed calculation time updates. Compatible job links and step IDs are retained. Obsolete/incompatible links are released with a warning while the imported EVE jobs remain recorded; refreshing never cancels or modifies in-game jobs. If recalculation fails, the previous plan remains intact. Restore an archived plan before refreshing it.

Archived plans have **Remove** controls on the production list and plan detail. **Remove all archived plans** operates only on the signed-in user's archived plans. A confirmation page precedes permanent deletion. Imported jobs and inventory remain; plan snapshots, overrides, steps and their job links are removed. Active plans cannot be removed through these controls.

Upgrade migration `0005` moves each saved manufacturing calculation's ME/TE values into per-plan overrides and preserves its inventory choice. Historic snapshots remain unchanged until explicitly refreshed. Existing exact-group location assignments remain usable as legacy overrides; new assignments use CCP's 18 bonus families and retain the empty catch-all.

## Inventory locations and storage

A job can draw existing inputs only from its **assigned production structure** and optional **storage structures for that activity**. Bonus-family and legacy group overrides choose the job location using the same precedence as facility bonuses. For example, reaction inputs at a manufacturing factory do not reduce a reactor’s material requirements unless that factory is explicitly added as reaction storage. An intermediate’s existing stock must be at its consuming job’s location, even when the intermediate would be produced elsewhere.

Under **Configuration → Activity storage**, select an activity and a structure, then **Add storage location**. Multiple storage structures are supported, including facilities with no industry services. Storage applies to every job of that activity; inaccessible/inactive facilities are excluded. Stock already at the job location is preferred, and a shared storage pile is allocated at most once within the plan. When storage serves multiple jobs, allocation respects each job’s eligible locations without inventing stock or creating avoidable shortages.

The configured facility’s deployed `structure_id` must match its physical ESI location ID. Assets inside containers are traced through their source’s asset hierarchy to the enclosing configured structure. Unresolved or cyclic container references are excluded. Excluded sources, revoked ownership, inaccessible structures, and blueprint copies remain excluded.

Corporation assets follow the same hierarchy: an item in a division (`CorpSAG1` through `CorpSAG7`) points to its office, whose `OfficeFolder` record points to the physical station or structure. Nested containers are traced through that chain. The virtual office record locates the contents but is not counted as usable inventory. All divisions at an eligible structure are pooled; this version does not select divisions or enforce each job installer’s in-game hangar Take roles. See [CCP’s asset location reference](https://docs.esi.evetech.net/docs/asset_location_id.html) and [corporation role requirements](https://support.eveonline.com/hc/en-us/articles/203217712-Roles-Listing).

**Inventory → Declare inventory** now requires a structure and optionally a hangar/label. Quantities are absolute per item, structure and label. Migration `0006` preserves existing declarations and labels without guessing their physical location: records without a structure do not count until you use **Edit location / quantity** to assign one. The inventory overview shows configured stock; produced items use their own activity assignment, while raw inputs are shown across configured locations and checked per consuming job during planning.

Plan material details show required units, stock allocated from allowed locations, remaining production/buying needs and the actual stock locations. Stock drawn from separate storage is marked for hauling. Planned outputs and storage supplies may still need transport between structures; no hauling happens automatically.

Saved calculations retain their original snapshot. Old calculations that used pooled inventory show a notice; use **Refresh plan** to apply the location restrictions and current storage settings. Stock use is not a reservation across separate plans. If an activity has no assigned production structure, only storage explicitly configured for that activity can supply its jobs.

## Reading the production chain

The graph, tree and production sequence share availability colours: **solid green** for stock/completed output, **blue** for ready to craft, **purple** for active/paused jobs, **amber** for buy/import and **grey** for missing inputs. A **dashed border** identifies a shared input while retaining its availability colour. Activity names still distinguish manufacturing and reactions.

Readiness uses the latest authorized, included inventory at each step’s saved production/storage locations, with a visible online production service. It requires all remaining input materials, accounts for completed runs and per-job material rounding, and allocates shared stock once in production order. Storage supplies are marked for hauling. Running or undelivered job output is never assumed to be available stock; downstream steps become ready after delivery and the next asset import. Readiness is a view-time check, not a reservation or live EVE access/skills check. Historic plans without saved location scopes need a refresh.

**Production sequence → Show steps** filters the numbered steps: **All steps**, **To complete** (job completion below 100%, including ongoing jobs), **Inputs ready** (blue ready-to-craft steps), or **Checked reminders** (the saved Reviewed checkboxes, independent of job completion). The visible count and empty state update immediately; saving a checkbox updates the checked filter as well. Filters retain the original step numbers and production order. Following a graph, tree or dependency link to a hidden step switches back to All steps so the target is visible.

**Reviewed** checkboxes are private, persistent reminders for every item, available in the tree, graph inspector, production steps and buy list. Matching entries stay synchronized. Checks survive plan refreshes, visually cross out the item name and do not change stock, quantities, readiness or job completion. Archived plans display the saved reminders without editing them.

**Buy / import list → Copy buy all** copies each net shortage as `Item name quantity`, one line per item with ungrouped integer quantities, for EVE Multibuy. **Copy unchecked** excludes reviewed buy items. The expandable text area provides a manual-copy fallback. These quantities come from the saved calculation; refresh the plan to recalculate shortages after stock changes.

**Build overview** switches between **Graph** and **Tree**, using the same saved plan snapshot. The graph places the finished item on the left and inputs to the right, with each shared input shown once. Cards show plan-wide demand, stock and output or buying needs. Select an item to highlight its direct connections, inspect quantities for each input and consumer, or jump to its production step.

Drag the graph background to pan. Use the zoom buttons or Ctrl/Command + scroll; **Fit graph** shows the whole chain and **Readable zoom** returns to legible cards near the finished product. Fullscreen provides more room. Keyboard users can focus the graph and use arrows to pan, `+`/`-` to zoom and `0` to fit. Tabbing to an offscreen item brings it into view.

The tree starts at the top level, with **Expand all**, **Collapse all** and **Top level** controls. Shared inputs show **This branch** alongside **Plan total**; their stock and production quantities remain plan totals. Repeated entries link to the first full branch, avoiding duplicated subtrees. Item colours distinguish manufacturing, reactions, buying/importing and stock.

## ESI authorization

Characters must already belong to the current AA account. Connect each desired character from **Inventory**. Inventory and All jobs show the number of connected characters and corporations, including **0 characters connected** when none are authorized. Owning a character in AA or adding manual stock does not connect its ESI source. EVE SSO cannot discover other characters on an EVE account automatically.

Personal sources request:

```text
esi-assets.read_assets.v1
esi-industry.read_character_jobs.v1
```

Corporation sources request:

```text
esi-assets.read_corporation_assets.v1
esi-industry.read_corporation_jobs.v1
esi-characters.read_corporation_roles.v1
```

The EVE developer application configured by AA's `ESI_SSO_CLIENT_ID` must have these scopes enabled in its application registration. Enable the personal pair for character connections and the three corporation scopes for corporation connections. The planner requests the full scope set for the chosen source; an ordinary AA login token is insufficient. An existing token is offered only when that single token has **all** required scopes. A token with additional scopes is accepted. Separate tokens with one permission each are not combined.

Select **Connect character** (or **Connect corporation**) to choose an existing eligible token. Use **Add Token / New Character** on that selection page if the desired character has no eligible token, then authorize it through EVE SSO. Refreshing an old token does not add newly requested scopes. The planner rechecks granted scopes and token validity after the SSO callback and before enabling the source. These checks supplement AA ownership and user permissions. See [Django-ESI's token selection documentation](https://django-esi.readthedocs.io/en/latest/developer.html) and [CCP's SSO authorization documentation](https://developers.eveonline.com/docs/services/sso/).

Corporation asset access requires a current Director. The module checks AA ownership and owner hash, token ownership and scopes, its corporation-data permission, current ESI corporation membership and the Director role. Jobs and assets are private to the connecting AA user; facilities are shared configuration. A corporation can have one source per user, avoiding duplicate imports when several of that user's characters have access.

Jobs are read from these ESI endpoints:

| Source | Endpoint | Required jobs scope |
| --- | --- | --- |
| Character | `GET /characters/{character_id}/industry/jobs/` | `esi-industry.read_character_jobs.v1` |
| Corporation | `GET /corporations/{corporation_id}/industry/jobs/` | `esi-industry.read_corporation_jobs.v1` |

Both imports request `include_completed=true`; ESI documents completed jobs within the past 90 days. **All jobs** includes manufacturing, reactions, ME/TE research, copying and invention. Research/copying jobs can lack an output item ID; the list then uses the blueprint name. Manufacturing and reactions remain the supported production-planning activities.

Under **All jobs → Job sources and import status**, each source shows its last jobs sync, imported count and errors, with a refresh control. A new source remains **Never synced** until the AA Celery worker processes the queued import. A revoked or disabled source needs reconnection. A token missing required scopes is excluded from connected-source counts and authorized data reads; the exact missing permission names appear in Inventory and job source status. No token secrets are displayed. Excluding stock from planning does not hide that source's jobs.

Refreshing uses django-esi's token refresh, HTTP response cache, pagination and rate-limit handling. After validity checking, the import uses the refreshed token instance so it does not retry with stale rotated credentials. The planner needs complete response bodies, so its ESI calls use `use_etag=False` while leaving `use_cache` and `store_cache` enabled. Django-ESI's conditional ETag mode raises `HTTPNotModified` for unchanged or matching cached responses; that is not an empty dataset and must not erase inventory or prevent job imports. Valid cached bodies are reused; expired bodies are fetched again without an old conditional ETag. The same policy applies to character/role checks, prices and system indices. No force refresh or cache clearing is used. See [Django-ESI's caching documentation](https://django-esi.readthedocs.io/en/latest/developer/openapi31.html). A refresh button queues work; it cannot bypass CCP's cache lifetime. The worker fetches complete asset and job datasets before replacing assets in one transaction. Optional asset values from the SDK are normalized before storage: absent/null `is_blueprint_copy` becomes `False`, and absent/null `location_flag` becomes an empty string. Explicit blueprint copies remain excluded from usable stock. Nullable job properties, including science jobs without a product ID, remain nullable. Failures before or during data storage retain the previous snapshot. Failure alerts report the stage (authorization, fetching assets/jobs, saving data, or post-import updates), exception class, HTTP status when available, and running worker version. Raw HTTP bodies, headers and token secrets are never included. A storage or service error does not automatically revoke the connection; local authorization failures do. Successful imports clear the prior error. Local authorization failures disable the source until it is reconnected. Cached corporate data is not an assertion of live access; ESI and AA membership changes become known when those systems refresh.

## Job progress and inventory semantics

Reaction job activity IDs **9 and 11** both map to the SDE’s `reaction` recipes for matching, output quantities and inventory-watch coverage. Earlier releases labelled both as Reactions but only matched ID 11, leaving ID-9 jobs unlinked with no known output. [ESI issue #997](https://github.com/esi/esi-issues/issues/997) includes ID 9 for Silicon Diborite formula 46179; [issue #894](https://github.com/esi/esi-issues/issues/894) lists both reaction IDs. Existing imported jobs work without reimporting after this update. Open All jobs or a plan to reconcile them.

Plans contain one production step per produced type, in dependency order. Jobs must match the product, blueprint, activity and configured facility. Automatically matched jobs must be installed by one of the user's AA characters. Explicit linking can assign another visible corporation job. A job can be allocated to only one step; its output is capped at that step's remaining requirement. Linked jobs show their actual output alongside the quantity credited to the plan. Multiple jobs can cover a single step: two active jobs of 50 units against a 98-unit step show **100% ongoing and 0% completed**, with 98 units credited and 100 units of actual job output. Finished jobs receive credit first; completed plus ongoing coverage is capped at the planned output. Active/paused quantities are separate from completion.

Jobs reconcile after ESI imports, when creating/refreshing a plan, and when opening a plan or All jobs. The oldest compatible active plan takes priority when several plans match; jobs are never counted twice across plans. Unlinking a job disables its automatic matching until it is explicitly linked again.

**All jobs** sorts by descending start time, then descending EVE job ID for ties, and displays both Started and Finishes. Each row’s **Assign plan / Change plan** control lists compatible active steps with remaining demand. **Save assignment** moves the job atomically, preserving the original link if validation fails, and recalculates both plans’ coverage (including the old step when its plan is archived). Product, blueprint/formula, activity, structure, current source authorization and capacity checks still apply. Another visible corporation installer’s job and finished history can be assigned manually. **Unassigned (disable auto-link)** removes the assignment and prevents it being immediately claimed again; assigning a plan re-enables matching. Status and page filters are retained.

Unassigned jobs show why they are not linked: no matching active product/recipe/activity, a structure mismatch (including actual and expected IDs), an unavailable SDE output, covered steps, disabled automatic matching, a different installer or historical finished work. Unsupported science activities remain visible without production assignments.

`ready` and `delivered` complete an allocated step. Elapsed time alone does not complete a job, and cancelled/reverted jobs release their allocation. Ready output is not included in inventory before delivery. The initial import does not notify for historical completions.

Inventory is pooled across included sources only at the locations permitted for each consuming job; stock elsewhere is ignored. Moving inputs between locations is a player action. A saved plan is a calculation snapshot: it does **not** reserve inventory across separate plans, submit EVE jobs, deliver jobs or transport items.

Watches compare stock at the item’s configured activity locations with the minimum and subtract known active/paused/ready output at those locations. Raw-input targets use the union of configured industry/storage locations. Unallocated output in the linked replenishment plan also covers demand. A user can turn an open production task into a saved plan. The task updates automatically; an already saved plan is kept stable. If a target grows beyond an existing linked plan, the task shows the extra requirement; archive the old linked plan before replacing it. Inputs are highlighted for buying/importing. Assets and job endpoints may update at different times after delivery, so review a temporary shortage before acting.

## Discord Multiverse notifications

Configure Multiverse's bot and managed server according to the [Multiverse documentation](https://github.com/Solar-Helix-Independent-Transport/allianceauth-discord-multiverse). This module reuses its bot token and **MultiDiscordUser** link; it does not store another token or infer a user from their Discord display name.

In Django admin, add **Industry Planner → Planner configuration** and set `discord_guild_id` to the managed guild. Each user must have access to that guild and an activated link in Multiverse. Users opt in to **Discord DM** under Notifications, and choose completion, low-inventory and sync-failure events. Failed DM delivery remains visible in the notification panel. Rate limits and transient failures retry with bounded backoff; denied DMs or missing links stop retrying.

## Development and validation

```sh
python -m pytest
node --test tests/*.test.cjs
python -m django check --settings=tests.settings
python -m django makemigrations aa_industry_planner --check --dry-run --settings=tests.settings
ruff check aa_industry_planner tests
python -m build
```

CI runs both profiles in `constraints/` on Python 3.11 and 3.12. Install development dependencies with `python -m pip install -c constraints/aa-legacy.txt ".[test,discord]"` to check the older stack, or use `constraints/aa-current.txt` for the newer stack.

`tests.settings` is a local SQLite environment using AA's real models, templates and URL hooks. It replaces production Redis task counters with AA's stub and skips production service-version checks. Do not use it for deployment.

For a local visual demo after installing dependencies:

```sh
python -m django migrate --settings=tests.settings --noinput
python scripts/seed_demo.py
python -m django runserver 127.0.0.1:8000 --settings=tests.settings
```

The seed script prints the local demo credentials and uses synthetic recipes labelled as demo data. It only runs against this repository's SQLite database.

## Scope and acceptance checks

Planning currently covers deterministic **manufacturing and reactions** from SDE blueprints. Other ESI job activities appear in the jobs list, but invention probability, copying, blueprint ownership/BPC inventory, PI extraction/processing, refining, skills import, market buy-order costing, automatic logistics and EVE job submission are outside this version. Blueprint access and appropriate character skills are assumed. Blueprint ME/TE default to 10/20 for manufacturing and can be overridden within each plan; effective character time bonuses are user declarations. Material requirements and costs are estimates for review in EVE before installation.

Before deploying to a live community, verify the SSO round trip with multiple owned characters; personal and Director corporation imports; all-page import with a large hangar; a real running → ready → delivered job; source revocation; a watch dropping and recovering; 90-day cleanup; configured structure/rig comparisons against EVE; and one opt-in Discord DM. MariaDB and live worker acceptance tests have not been run in this workspace.

## Design references

- [Ravworks](https://ravworks.com/) — workflow inspiration; no Ravworks code or private API is used.
- [Django EVE SDE](https://github.com/Solar-Helix-Independent-Transport/django-eveonline-sde) — translated type, blueprint, material, product and solar-system models.
- [EVE Universe](https://github.com/AllianceAuth-Apps/django-eveuniverse) — cached EVE character/corporation entity-name resolution.
- [CCP ESI documentation](https://developers.eveonline.com/docs/services/esi/overview/) and [OpenAPI schema](https://esi.evetech.net/meta/openapi.json?compatibility_date=2026-11-19) — endpoint contracts, authorization and compatibility date.
- [CCP industry tax formula](https://www.eveonline.com/fr/news/view/viridian-expansion-notes) — taxes applied to EIV.

Released under the MIT license.
