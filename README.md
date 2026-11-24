# Alliance Auth Industry Planner

An installable Alliance Auth application for industrialists. It plans manufacturing and reaction chains from EVE SDE recipes, tracks connected industry jobs, and maintains inventory targets.

This is the first implementation (0.1.2). Automated tests use real Alliance Auth 5.6, Django 5.2 and eveuniverse 2.2 models on SQLite, with both SDE 0.2.0 / django-esi 10.1.0 / modeltranslation 0.19.17 and SDE 2.0.0 / django-esi 10.2.0 / modeltranslation 0.20.6 compatibility profiles. A production AA installation and real ESI/Discord accounts are still needed for deployment acceptance testing.

## Features

The interface inherits the selected Alliance Auth theme (including Flatly, Darkly and Materia). Cards, buttons, forms, tables, navigation and status badges use native Bootstrap components; charts and tree accents use the theme's CSS variables. Change the theme through AA's existing user settings menu.

- Select an item and quantity; expand its manufacturing and reaction dependencies down to raw inputs.
- Aggregate shared components before sizing jobs, allocate stock once within a plan, and show a collapsible dependency tree plus a numbered production sequence.
- Set buy/import boundaries, blueprint ME/TE, an explicit blueprint for alternative recipes, reaction batch runs or duration, and effective character time bonuses.
- Configure structures, service availability, taxes, effective structure bonuses and installed rig bonuses in Django admin. Restrict facilities to AA groups and let users assign an available facility per activity, with optional product-group overrides.
- Snapshot installation estimates using unmodified-material EIV, CCP adjusted prices, the activity's system cost index, facility cost bonuses, facility tax, SCC surcharge and optional Alpha tax.
- Collect hourly indices for configured systems and retain 90 days, with history charts for authorized facility monitors.
- Connect multiple owned characters and authorized corporations through AA SSO. Import all pages of assets and industry jobs, periodically and on demand. Support additive, manually declared stock that can be excluded to avoid duplication.
- Display all industry jobs, automatically link compatible running jobs, manually link older jobs, and calculate step completion from ESI's `ready` / `delivered` statuses.
- Keep one replenishment task per inventory target, updating quantities as stock and known jobs change. Highlight buy/import shortages without creating production plans for them.
- Provide a notification inbox and per-event preferences. Optional Discord DMs use embedded cards and the user's activated Discord Multiverse account in an administrator-selected guild.

## Install in an existing AA instance

Requires Python 3.11+, Alliance Auth 5.6.x and its existing database, Redis, Celery worker and Beat infrastructure. Install from the public GitHub repository in the AA virtual environment:

```sh
python -m pip install "aa-industry-planner @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.2"
```

To include Discord Multiverse:

```sh
python -m pip install "aa-industry-planner[discord] @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.2"
```

The public HTTPS installation does not require GitHub authentication.

Existing apps may require SDE 0.2.x. The planner supports that version and does not require upgrading to SDE 2.x. To preserve the older stack, install with explicit constraints:

```sh
python -m pip install --upgrade \
  "aa-industry-planner @ git+https://github.com/Redone0001/aa-industry-planner.git@v0.1.2" \
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
4. Add a **Facility**, entering its EVE structure/station ID, SDE solar system, and optional allowed groups. An empty group list makes it available to all planner users.
5. Add its **Facility services** (manufacturing and/or reactions), installed service names, online status and effective bonuses. Open a service to add **Facility rigs**, optionally limited to a product group.
6. Rates are fractions: `0.01` = 1%. Multipliers are the remaining fraction after a bonus: `0.98` = 2% reduction. Rig multipliers must include the applicable security-space effect. Bonuses are configured explicitly; the app does not infer an operational fit or docking rights from a structure ID.
7. Users choose default manufacturing and reaction locations in **Configuration**, and can override them for particular SDE product groups. Changing settings affects newly calculated plans; existing snapshots retain their original assumptions.

SCC defaults to 4% for the two supported planning activities and is editable per service. Missing/stale prices (older than two days), unavailable system indices (older than six hours), and missing facilities are reported explicitly; an unknown installation estimate is never presented as zero.

## ESI authorization

Characters must already belong to the current AA account. Connect each desired character from **Inventory**. EVE SSO cannot discover other characters on an EVE account automatically.

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

Corporation asset access requires a current Director. The module checks AA ownership and owner hash, token ownership and scopes, its corporation-data permission, current ESI corporation membership and the Director role. Jobs and assets are private to the connecting AA user; facilities are shared configuration. A corporation can have one source per user, avoiding duplicate imports when several of that user's characters have access.

Refreshing uses django-esi's token refresh, ETags, pagination and rate-limit handling. A refresh button queues work; it cannot bypass CCP's cache lifetime. The worker fetches complete asset and job datasets before replacing assets in one transaction. Failed refreshes retain the previous snapshot and report the failure. Local authorization failures disable the source until it is reconnected. Cached corporate data is not an assertion of live access; ESI and AA membership changes become known when those systems refresh.

## Job progress and inventory semantics

Plans contain one production step per produced type, in dependency order. Jobs must match the product, blueprint, activity and configured facility. Automatically matched jobs must be installed by one of the user's AA characters. Explicit linking can assign another visible corporation job. A job can be allocated to only one step; its output is capped at that step's remaining requirement. Overproduction is shown in the plan's calculated output.

`ready` and `delivered` complete an allocated step. Elapsed time alone does not complete a job, and cancelled/reverted jobs release their allocation. Ready output is not included in inventory before delivery. The initial import does not notify for historical completions.

Inventory is pooled across included sources and locations for material sufficiency. Moving inputs between locations is a player action. A saved plan is a calculation snapshot: it does **not** reserve inventory across separate plans, submit EVE jobs, deliver jobs or transport items.

Watches compare included stock with the configured minimum and subtract known active/paused/ready output. Unallocated output in the linked replenishment plan also covers demand. A user can turn an open production task into a saved plan. The task updates automatically; an already saved plan is kept stable. If a target grows beyond an existing linked plan, the task shows the extra requirement; archive the old linked plan before replacing it. Inputs are highlighted for buying/importing. Assets and job endpoints may update at different times after delivery, so review a temporary shortage before acting.

## Discord Multiverse notifications

Configure Multiverse's bot and managed server according to the [Multiverse documentation](https://github.com/Solar-Helix-Independent-Transport/allianceauth-discord-multiverse). This module reuses its bot token and **MultiDiscordUser** link; it does not store another token or infer a user from their Discord display name.

In Django admin, add **Industry Planner → Planner configuration** and set `discord_guild_id` to the managed guild. Each user must have access to that guild and an activated link in Multiverse. Users opt in to **Discord DM** under Notifications, and choose completion, low-inventory and sync-failure events. Failed DM delivery remains visible in the notification panel. Rate limits and transient failures retry with bounded backoff; denied DMs or missing links stop retrying.

## Development and validation

```sh
python -m pytest
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

Planning currently covers deterministic **manufacturing and reactions** from SDE blueprints. Other ESI job activities appear in the jobs list, but invention probability, copying, blueprint ownership/BPC inventory, PI extraction/processing, refining, skills import, market buy-order costing, automatic logistics and EVE job submission are outside this version. Blueprint access and appropriate character skills are assumed. ME/TE and time bonuses are user declarations. Material requirements and costs are estimates for review in EVE before installation.

Before deploying to a live community, verify the SSO round trip with multiple owned characters; personal and Director corporation imports; all-page import with a large hangar; a real running → ready → delivered job; source revocation; a watch dropping and recovering; 90-day cleanup; configured structure/rig comparisons against EVE; and one opt-in Discord DM. MariaDB and live worker acceptance tests have not been run in this workspace.

## Design references

- [Ravworks](https://ravworks.com/) — workflow inspiration; no Ravworks code or private API is used.
- [Django EVE SDE](https://github.com/Solar-Helix-Independent-Transport/django-eveonline-sde) — translated type, blueprint, material, product and solar-system models.
- [EVE Universe](https://github.com/AllianceAuth-Apps/django-eveuniverse) — cached EVE character/corporation entity-name resolution.
- [CCP ESI documentation](https://developers.eveonline.com/docs/services/esi/overview/) and [OpenAPI schema](https://esi.evetech.net/meta/openapi.json?compatibility_date=2026-11-19) — endpoint contracts, authorization and compatibility date.
- [CCP industry tax formula](https://www.eveonline.com/fr/news/view/viridian-expansion-notes) — taxes applied to EIV.

Released under the MIT license.
