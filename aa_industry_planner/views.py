from collections import defaultdict
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from eve_sde.models import ItemType

from . import __version__
from .engine import PlanningError
from .forms import (
    AssignmentForm,
    BlueprintFormSet,
    ManualStockForm,
    NotificationPreferencesForm,
    OperationsForm,
    PlanForm,
    PlanSettingsForm,
    PlanTargetFormSet,
    PolicyForm,
    PreferencesForm,
    SavedStorageLocationForm,
    StorageLocationForm,
    WatchForm,
    blueprint_rows,
)
from .industry import BONUS_SCOPE_OFFSET
from .models import (
    Activity,
    ActivityStorageLocation,
    Facility,
    FacilityAssignment,
    FacilityService,
    IndustryJob,
    InventorySource,
    InventoryWatch,
    ItemPolicy,
    JobAllocation,
    ManualStock,
    Notification,
    NotificationPreferences,
    PlanChecklistItem,
    PlanStep,
    ProductionPlan,
    ReplenishmentTask,
    SavedStorageLocation,
    SlotMonitorState,
    SystemCostIndex,
    UserPreferences,
)
from .services.blueprints import (
    FormulaSupply,
    installed_blueprints,
    reaction_formula_inventory,
    reaction_formula_key,
)
from .services.capacity import capacity_status
from .services.costs import plan_cost, purchase_quantities
from .services.gas import gas_multibuy
from .services.inventory import (
    assets_at_locations,
    authorized_sources,
    inventory_by_location,
    source_overview,
    stock_at_locations,
)
from .services.job_assignments import annotate_assignments
from .services.job_dashboard import job_dashboard
from .services.jobs import (
    allocate_job,
    installer_names,
    job_outputs,
    reconcile_jobs,
    unallocate_job,
)
from .services.locations import (
    InventoryLocations,
    is_station_id,
    lookup_storage_name,
    storage_choices,
)
from .services.planning import create_plan_for_targets
from .services.planning import refresh_plan as refresh_production_plan
from .services.presentation import (
    fee_presentation,
    plan_progress,
    plan_views,
    visible_stock_details,
)
from .services.production_queue import production_queue as build_production_queue
from .services.readiness import annotate_steps, multibuy_line, multibuy_text
from .services.scheduling import estimate_schedule, target_production
from .services.type_search import search_types
from .services.watches import evaluate_watches, plan_replenishment, plannable_tasks


def planner_access(view):
    return login_required(
        permission_required("aa_industry_planner.access_planner", raise_exception=True)(view)
    )


def page(request, template, **context):
    context["industry_version"] = __version__
    context["unread_count"] = Notification.objects.filter(
        user=request.user, read_at__isnull=True
    ).count()
    return render(request, f"aa_industry_planner/{template}.html", context)


def user_plans(user):
    return (
        ProductionPlan.objects.filter(user=user)
        .select_related("item_type")
        .prefetch_related("additional_targets__item_type")
    )


@planner_access
def dashboard(request):
    plans = user_plans(request.user).filter(archived=False).defer("snapshot")
    tasks = (
        ReplenishmentTask.objects.filter(watch__user=request.user)
        .exclude(state="resolved")
        .select_related("watch__item_type", "plan")
        .defer("plan__snapshot")
    )
    return page(
        request,
        "dashboard",
        plans=plans[:8],
        plan_count=plans.count(),
        tasks=tasks,
        active_jobs=IndustryJob.objects.filter(
            user=request.user,
            source__in=authorized_sources(request.user),
            status__in=["active", "paused"],
        ).count(),
    )


@planner_access
def type_search(request):
    return JsonResponse({"results": search_types(request.GET.get("q", ""))})


@planner_access
def plans(request):
    data = request.POST if request.method == "POST" else None
    form = PlanForm(data)
    if data is not None and "targets-TOTAL_FORMS" not in data and "item_type" in data:
        # Accept single-item submissions from an already-open production form.
        data = data.copy()
        data.update(
            {
                "targets-TOTAL_FORMS": 1,
                "targets-INITIAL_FORMS": 0,
                "targets-0-item_type": data["item_type"],
                "targets-0-quantity": data.get("quantity", ""),
            }
        )
    targets = PlanTargetFormSet(data, prefix="targets")
    if request.method == "POST" and all((form.is_valid(), targets.is_valid())):
        quantities = {}
        for row in targets:
            if not row.cleaned_data or row.cleaned_data.get("DELETE"):
                continue
            tid = row.cleaned_data["item_type"].pk
            quantities[tid] = quantities.get(tid, 0) + row.cleaned_data["quantity"]
        try:
            production = create_plan_for_targets(
                request.user,
                quantities,
                form.cleaned_data["name"],
                form.cleaned_data["use_inventory"],
                default_me=form.cleaned_data["default_material_efficiency"],
                default_te=form.cleaned_data["default_time_efficiency"],
            )
            return redirect("aa_industry_planner:plan", pk=production.pk)
        except PlanningError as exc:
            form.add_error(None, str(exc))
    saved = user_plans(request.user).defer("snapshot")
    return page(
        request,
        "plans",
        form=form,
        targets=targets,
        plans=saved.filter(archived=False),
        archived_plans=saved.filter(archived=True),
    )


@planner_access
def production_queue(request):
    plans = user_plans(request.user).filter(archived=False).order_by("created_at", "pk")
    if not plans.exists():
        return page(request, "production_queue")
    reconcile_jobs(request.user)
    steps = PlanStep.objects.select_related("item_type").prefetch_related("allocations__job")
    plans = list(plans.prefetch_related(Prefetch("steps", queryset=steps)))
    queue = build_production_queue(request.user, plans)
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)
    return page(
        request,
        "production_queue",
        plans=plans,
        capacity=capacity_status(request.user, preferences),
        multibuy=multibuy_text(queue["shortages"]),
        gas_multibuy=gas_multibuy(queue["shortages"], preferences.gas_decompression_yield),
        **queue,
    )


@planner_access
def plan_detail(request, pk):
    production = get_object_or_404(user_plans(request.user), pk=pk)
    return _plan_page(request, production)


def _plan_page(request, production, settings_form=None, blueprints_formset=None):
    reconcile_jobs(request.user)
    steps = list(
        production.steps.select_related("item_type", "service__facility").prefetch_related(
            "allocations__job"
        )
    )
    snapshot = production.snapshot
    locations = InventoryLocations(request.user)
    scoped = snapshot.get("stock_elsewhere_scoped")
    transfer_names = locations.transfer_names if scoped and snapshot.get("inventory_used") else {}
    legacy_transfers = not scoped and any(
        node.get("stock_elsewhere") for node in snapshot.get("nodes", {}).values()
    )
    nodes = {
        tid: visible_stock_details(node, transfer_names)
        for tid, node in snapshot.get("nodes", {}).items()
    }
    for step in steps:
        step.details = visible_stock_details(step.details, transfer_names)
    step_map = {step.item_type_id: step for step in steps}
    checked = set(production.checklist.filter(checked=True).values_list("item_type_id", flat=True))
    services = set(
        FacilityService.objects.filter(
            online=True, facility__in=Facility.visible_to(request.user)
        ).values_list("pk", flat=True)
    )
    if blueprints_formset is None:
        blueprints_formset = BlueprintFormSet(
            initial=blueprint_rows(production), prefix="blueprints"
        )
    blueprint_locations = set()
    for row in blueprints_formset:
        tid = row.initial.get("item_type_id")
        step = step_map.get(tid)
        row.blueprint_type_id = (
            step.blueprint_type_id if step else row.initial.get("blueprint_type_id")
        )
        service = step.service if step else None
        if step is None and tid:
            service = locations.service_for(tid, "manufacturing")
        if service and service.pk in services:
            row.facility = service.facility
            blueprint_locations.add(row.facility.structure_id)
        for name, label in [("material_efficiency", "ME"), ("time_efficiency", "TE")]:
            field = row.fields[name]
            field.disabled = production.archived
            field.widget.attrs.update(
                placeholder="Plan default",
                style="max-width: 8rem",
                **{"aria-label": f"{row.initial.get('name', 'Blueprint')} {label} override (%)"},
            )

    reaction_sites = {}
    for step in steps:
        if step.activity == "reaction":
            key = reaction_formula_key(step)
            facility = step.service.facility if step.service_id in services else None
            reaction_sites[key] = facility if facility and facility.structure_id == key[1] else None
    formula_keys = {key for key, facility in reaction_sites.items() if facility}
    blueprint_types = {row.blueprint_type_id for row in blueprints_formset if row.blueprint_type_id}
    blueprint_sites = locations.transfer_names if blueprint_types else {}
    asset_locations = (
        set(locations.names)
        | blueprint_locations
        | {loc for _, loc in formula_keys}
        | blueprint_sites.keys()
    )
    sources = authorized_sources(request.user).filter(include_in_planning=True)
    assets = list(assets_at_locations(sources, asset_locations))
    found = defaultdict(set)
    for asset, location in assets:
        if asset["type_id"] in blueprint_types and (
            asset["is_blueprint_copy"] or asset["quantity"]
        ):
            found[asset["type_id"]].add(location)
    installed = installed_blueprints(request.user, sources, blueprint_types, blueprint_locations)
    blueprint_names = ItemType.objects.in_bulk(blueprint_types)
    for row in blueprints_formset:
        tid = row.blueprint_type_id
        row.blueprint = blueprint_names.get(tid)
        row.found = False
        if getattr(row, "facility", None):
            destination = row.facility.structure_id
            row.found = destination in found[tid]
            row.installed = (tid, destination) in installed
            row.elsewhere = sorted(
                (
                    blueprint_sites[location]
                    for location in found[tid] & blueprint_sites.keys()
                    if location != destination
                ),
                key=str.casefold,
            )
    reaction_formulas = reaction_formula_inventory(request.user, sources, formula_keys, assets)
    formula_types = ItemType.objects.in_bulk({tid for tid, _ in reaction_sites})
    reaction_rows = [
        dict(
            name=formula_types[tid].name if tid in formula_types else f"Formula #{tid}",
            name_known=tid in formula_types,
            facility=facility,
            supply=reaction_formulas.get((tid, location), FormulaSupply()),
        )
        for (tid, location), facility in reaction_sites.items()
    ]
    annotate_steps(
        steps,
        stock_at_locations(sources, locations.names, assets),
        checked,
        available_services=services,
    )
    allocations = [a for s in steps for a in s.allocations.all()]
    outputs = job_outputs([a.job for a in allocations])
    for allocation in allocations:
        allocation.actual_output = outputs[allocation.job_id]
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)
    capacity = capacity_status(request.user, preferences)
    timing = estimate_schedule(
        steps,
        preferences,
        capacity["jobs"],
        target_demand=target_production(snapshot),
        reaction_formulas=reaction_formulas,
    )

    tree, dependency_graph = plan_views(
        dict(snapshot, nodes=nodes),
        {
            tid: {
                "position": step.position,
                "progress": step.progress,
                "ongoing": step.ongoing_progress,
                "ongoing_quantity": step.ongoing_quantity,
                "state": step.state,
            }
            for tid, step in step_map.items()
        },
        checked,
    )
    purchase_choices = snapshot.get("purchase_choices", {})
    inputs = sorted(
        (
            dict(nodes[tid], checked=int(tid) in checked, planned_purchase=tid in purchase_choices)
            for tid in purchase_quantities(nodes)
        ),
        key=lambda row: row["name"].casefold(),
    )
    classifications = {
        tid: (group, category)
        for tid, group, category in ItemType.objects.filter(
            pk__in=[row["type_id"] for row in inputs]
        ).values_list("pk", "group_id", "group__category_id")
    }
    for row in inputs:
        group, category = classifications.get(row["type_id"], (None, None))
        # CCP SDE groups Mineral (18), Moon Materials (427), and Planetary
        # Resources (1031). Category 43 contains the four commodity tiers.
        row["category"] = (
            "pi"
            if category == 43 or group == 1031
            else {
                18: "minerals",
                427: "moon-goo",
            }.get(group, "")
        )
        row["multibuy_line"] = multibuy_line(row)
    copy_categories = [("minerals", "Minerals"), ("pi", "PI"), ("moon-goo", "Moon goo")]
    available_categories = {row["category"] for row in inputs}
    graph_nodes = {n["id"]: n for n in dependency_graph["nodes"]}
    dependencies = {
        s.item_type_id: [
            step_map[int(t)].position for t in s.details.get("materials", {}) if int(t) in step_map
        ]
        for s in steps
    }
    cost = plan_cost(snapshot)
    for step in steps:
        step.dependency_positions = dependencies[step.item_type_id]
        graph_node = graph_nodes.get(step.item_type_id, {})
        step.shared = graph_node.get("shared", False)
        step.removed_runs = graph_node.get("removed_runs", 0)
        step.fee = fee_presentation(step.details)
        step.profitability = {
            side: values.get(str(step.item_type_id)) for side, values in cost["steps"].items()
        }
    visible_jobs = IndustryJob.objects.filter(
        user=request.user, source__in=authorized_sources(request.user), allocation__isnull=True
    ).exclude(status__in=["cancelled", "reverted"])
    if settings_form is None:
        settings_form = PlanSettingsForm(instance=production, prefix="settings")
    return page(
        request,
        "plan",
        plan=production,
        steps=steps,
        progress=plan_progress(steps),
        jobs_to_deliver=sum(a.quantity > 0 and a.job.status == "ready" for a in allocations),
        timing=timing,
        cost=cost,
        capacity=capacity,
        tree=tree,
        dependency_graph=dependency_graph,
        legacy_inventory_scope=snapshot.get("inventory_used", True)
        and "inventory_locations" not in snapshot,
        legacy_transfers=legacy_transfers,
        inputs=inputs,
        multibuy=multibuy_text(inputs),
        gas_multibuy=gas_multibuy(inputs, preferences.gas_decompression_yield),
        copy_categories=[
            (key, label, key in available_categories) for key, label in copy_categories
        ],
        purchase_choices=sorted(purchase_choices.items(), key=lambda pair: pair[1].casefold()),
        covered_steps=sorted(
            (
                n
                for n in graph_nodes.values()
                if n["covered_step"] and str(n["id"]) not in purchase_choices
            ),
            key=lambda n: n["name"].casefold(),
        ),
        candidate_jobs=visible_jobs[:300],
        settings_form=settings_form,
        blueprints_formset=blueprints_formset,
        reaction_rows=reaction_rows,
        legacy_formula_jobs=any(supply.unidentified for supply in reaction_formulas.values()),
    )


@planner_access
@require_POST
def refresh_plan(request, pk):
    production = get_object_or_404(ProductionPlan, pk=pk, user=request.user, archived=False)
    settings_form = None
    formset = None
    options = {}
    if request.POST.get("form") == "blueprints":
        settings_form = PlanSettingsForm(request.POST, instance=production, prefix="settings")
        formset = BlueprintFormSet(
            request.POST, initial=blueprint_rows(production), prefix="blueprints"
        )
        settings_valid, rows_valid = settings_form.is_valid(), formset.is_valid()
        if not settings_valid or not rows_valid:
            return _plan_page(request, production, settings_form, formset)
        options = {
            "default_me": settings_form.cleaned_data["default_material_efficiency"],
            "default_te": settings_form.cleaned_data["default_time_efficiency"],
            "use_inventory": settings_form.cleaned_data["use_inventory"],
            "blueprint_overrides": {
                row.cleaned_data["item_type_id"]: (
                    row.cleaned_data["material_efficiency"],
                    row.cleaned_data["time_efficiency"],
                )
                for row in formset
            },
        }
    try:
        production, detached = refresh_production_plan(request.user, pk, **options)
    except PlanningError as exc:
        messages.error(request, str(exc))
        production.refresh_from_db()
        return _plan_page(request, production, settings_form, formset)
    except ProductionPlan.DoesNotExist as exc:
        raise Http404 from exc
    messages.success(
        request,
        "Plan refreshed with current locations, structures, policies, and character settings.",
    )
    if detached:
        messages.warning(
            request,
            f"{detached} job link(s) no longer match this calculation and were released. The jobs remain in All jobs; no EVE jobs were cancelled.",
        )
    return redirect("aa_industry_planner:plan", pk=pk)


@planner_access
@require_POST
def set_plan_purchase(request, pk, item_id):
    action = request.POST.get("action")
    if action not in {"buy", "make"}:
        return HttpResponseBadRequest("Choose whether to buy or make the item.")
    try:
        _, detached = refresh_production_plan(
            request.user, pk, purchase_change=(item_id, action == "buy")
        )
    except ProductionPlan.DoesNotExist as exc:
        raise Http404 from exc
    except PlanningError as exc:
        messages.error(request, str(exc))
    else:
        if detached:
            messages.warning(
                request, f"{detached} job link(s) were released after recalculating the plan."
            )
    return redirect(reverse("aa_industry_planner:plan", args=[pk]) + "#buy-list")


@planner_access
@require_http_methods(["GET", "POST"])
def delete_archived_plans(request, pk=None):
    candidates = ProductionPlan.objects.filter(user=request.user, archived=True)
    selected = (
        get_object_or_404(user_plans(request.user).filter(archived=True).defer("snapshot"), pk=pk)
        if pk is not None
        else None
    )
    if selected:
        candidates = candidates.filter(pk=selected.pk)
    if request.method == "POST":
        with transaction.atomic():
            ids = list(candidates.select_for_update().values_list("pk", flat=True))
            candidates.filter(pk__in=ids).only("pk").delete()
            evaluate_watches(request.user)
        messages.success(request, f"{len(ids)} archived plan(s) removed.")
        return redirect("aa_industry_planner:plans")
    return page(
        request, "delete_archived_plans", selected_plan=selected, archived_count=candidates.count()
    )


@planner_access
@require_POST
def archive_plan(request, pk):
    production = get_object_or_404(ProductionPlan, pk=pk, user=request.user)
    production.archived = not production.archived
    production.save(update_fields=["archived"])
    evaluate_watches(request.user)
    return redirect("aa_industry_planner:plan", pk=pk)


@planner_access
def inventory(request):
    stock_id = (
        request.POST.get("stock_id") if request.method == "POST" else request.GET.get("edit_stock")
    )
    if stock_id and not str(stock_id).isdigit():
        raise Http404
    editing_stock = (
        get_object_or_404(ManualStock, pk=stock_id, source__user=request.user) if stock_id else None
    )
    form = ManualStockForm(request.POST or None, user=request.user, instance=editing_stock)
    if request.method == "POST" and form.is_valid():
        source, _ = InventorySource.objects.get_or_create(
            user=request.user, kind="manual", owner_id=0, defaults={"name": "Declared inventory"}
        )
        with transaction.atomic():
            saved, _ = ManualStock.objects.update_or_create(
                source=editing_stock.source if editing_stock else source,
                item_type=form.cleaned_data["item_type"],
                facility=form.cleaned_data["facility"],
                location=form.cleaned_data["location"],
                defaults={"quantity": form.cleaned_data["quantity"]},
            )
            if editing_stock and editing_stock.pk != saved.pk:
                editing_stock.delete()
        evaluate_watches(request.user)
        messages.success(request, "Declared inventory updated.")
        return redirect("aa_industry_planner:inventory")
    locations = InventoryLocations(request.user)
    stock = inventory_by_location(request.user, locations, include_elsewhere=True)
    locations.prime_items(stock)
    watches = {
        w.item_type_id: w for w in InventoryWatch.objects.filter(user=request.user, enabled=True)
    }
    item_ids = stock.keys() | watches.keys()
    names = dict(ItemType.objects.filter(pk__in=item_ids).values_list("pk", "name"))
    rows = []
    for tid in item_ids:
        allowed = set(locations.item_scope(tid).location_ids) if tid in stock else set()
        piles = stock.get(tid, {})
        quantity = sum(piles.get(loc, 0) for loc in allowed)
        elsewhere = {loc: amount for loc, amount in piles.items() if loc not in allowed and amount}
        if not quantity and not elsewhere and tid not in watches:
            continue
        minimum = watches[tid].minimum if tid in watches else None
        policy = locations.policies.get(tid)
        rows.append(
            dict(
                type_id=tid,
                name=names.get(tid, str(tid)),
                quantity=quantity,
                minimum=minimum,
                low=minimum is not None and quantity < minimum,
                input=bool(policy and policy.buy),
                elsewhere=elsewhere,
            )
        )
    for row in rows:
        row["elsewhere"] = [
            (locations.transfer_names[loc], amount)
            for loc, amount in sorted(row["elsewhere"].items())
        ]
    rows.sort(key=lambda r: (not r["low"], r["name"]))
    return page(
        request,
        "inventory",
        form=form,
        editing_stock=editing_stock,
        rows=rows,
        manual=ManualStock.objects.filter(source__user=request.user).select_related(
            "item_type", "facility"
        ),
        **source_overview(request.user),
    )


@planner_access
@require_POST
def delete_manual(request, pk):
    get_object_or_404(ManualStock, pk=pk, source__user=request.user).delete()
    evaluate_watches(request.user)
    return redirect("aa_industry_planner:inventory")


@planner_access
@require_POST
def source_action(request, pk):
    source = get_object_or_404(InventorySource, pk=pk, user=request.user)
    action = request.POST.get("action")
    if action == "refresh" and source.kind != "manual":
        if not authorized_sources(request.user).filter(pk=pk).exists():
            raise PermissionDenied("Reconnect the source to restore access.")
        if cache.add(f"aip:refresh-request:{pk}", True, 60):
            from .tasks import refresh_source

            refresh_source.delay(pk)
            messages.success(
                request, "Refresh queued. ESI may return cached data until its cache expires."
            )
        else:
            messages.info(request, "Refresh was already requested. Please wait a minute.")
    elif action == "include":
        source.include_in_planning = not source.include_in_planning
        source.save(update_fields=["include_in_planning"])
        evaluate_watches(request.user)
    elif action == "disconnect":
        source.delete()
        evaluate_watches(request.user)
    destination = request.POST.get("return_to", "configuration")
    return redirect(
        f"aa_industry_planner:{destination if destination in {'jobs', 'configuration', 'inventory'} else 'configuration'}"
    )


def connect(kind):
    from esi.decorators import token_required

    from .providers import missing_scopes, required_scopes

    scopes = required_scopes(kind)

    def decorator(view):
        @planner_access
        @token_required(scopes=scopes)
        @wraps(view)
        def wrapped(request, token):
            from allianceauth.authentication.models import CharacterOwnership

            if kind == "corporation" and not request.user.has_perm(
                "aa_industry_planner.use_corporation_data"
            ):
                raise PermissionDenied
            ownership = (
                CharacterOwnership.objects.filter(
                    user=request.user,
                    character__character_id=token.character_id,
                    owner_hash=token.character_owner_hash,
                )
                .select_related("character")
                .first()
            )
            if not ownership or token.user_id not in {None, request.user.pk}:
                raise PermissionDenied(
                    "Add this character to your Alliance Auth account before connecting it."
                )
            # django-esi checks selected tokens, but callbacks bypass that query.
            # Apply the same scope/validity requirements to every entry path.
            from esi.models import Token

            missing = missing_scopes(token, kind)
            if missing:
                raise PermissionDenied(
                    f"Missing ESI permissions: {', '.join(missing)}. Reconnect this source."
                )
            token = (
                Token.objects.filter(
                    pk=token.pk,
                    character_id=ownership.character.character_id,
                    character_owner_hash=ownership.owner_hash,
                    user_id=token.user_id,
                )
                .require_scopes(scopes)
                .require_valid()
                .first()
            )
            if not token:
                raise PermissionDenied("A valid ESI token with all required permissions is needed.")
            token.user = request.user
            token.save(update_fields=["user"])
            if kind == "structure":
                messages.success(request, "Structure name lookup is authorized.")
                return redirect(reverse("aa_industry_planner:configuration") + "#activity-storage")
            from .providers import ESI

            api = ESI()
            owner_id = token.character_id
            name = ownership.character.character_name
            if kind == "corporation":
                owner_id = api.character(token.character_id)["corporation_id"]
                if owner_id != ownership.character.corporation_id or "Director" not in api.roles(
                    token
                ).get("roles", []):
                    raise PermissionDenied(
                        "Refresh your AA character and connect a current corporation Director."
                    )
                name = f"Corporation {owner_id}"
            source, _ = InventorySource.objects.update_or_create(
                user=request.user,
                kind=kind,
                owner_id=owner_id,
                defaults={
                    "name": name,
                    "token": token,
                    "enabled": True,
                    "last_error": "",
                    "sync_error_since": None,
                    "sync_failed_at": None,
                },
            )
            from .tasks import refresh_source

            refresh_source.delay(source.pk)
            return redirect("aa_industry_planner:configuration")

        if kind == "corporation":
            return login_required(
                permission_required(
                    "aa_industry_planner.use_corporation_data", raise_exception=True
                )(wrapped)
            )
        return wrapped

    return decorator


@connect("character")
def connect_character(request):
    pass


@connect("corporation")
def connect_corporation(request):
    pass


@connect("structure")
def connect_structure_lookup(request):
    pass


@planner_access
def jobs(request):
    reconcile_jobs(request.user)
    visible_jobs = IndustryJob.objects.filter(
        user=request.user, source__in=authorized_sources(request.user)
    )
    qs = visible_jobs.select_related("source", "allocation__step__plan").order_by(
        "-start_date", "-job_id"
    )
    jobs_page = Paginator(qs, 100).get_page(request.GET.get("page"))
    page_jobs = list(jobs_page)
    ids = {tid for job in page_jobs for tid in (job.product_type_id, job.blueprint_type_id) if tid}
    names = dict(ItemType.objects.filter(pk__in=ids).values_list("id", "name"))
    installers = installer_names(job.installer_id for job in page_jobs)
    for job in page_jobs:
        job.product_name = (
            names.get(job.product_type_id)
            or names.get(job.blueprint_type_id)
            or str(job.product_type_id or job.blueprint_type_id)
        )
        job.installer_name = installers[job.installer_id]
    annotate_assignments(request.user, page_jobs)
    overview = source_overview(request.user)
    job_sources = [source for source in overview["sources"] if source.kind != "manual"]
    return page(
        request,
        "jobs",
        jobs=jobs_page,
        operations=job_dashboard(request.user, visible_jobs, timezone.now()),
        preview_job=next((job for job in page_jobs if job.status == "ready"), None),
        job_sources=job_sources,
        latest_job_sync=max(
            (s.last_jobs_sync for s in job_sources if s.connected and s.last_jobs_sync),
            default=None,
        ),
        **overview,
    )


@planner_access
@require_POST
def assign_job(request, pk):
    job = get_object_or_404(
        IndustryJob, pk=pk, user=request.user, source__in=authorized_sources(request.user)
    )
    target = request.POST.get("step", "")
    if target == "unassigned":
        unallocate_job(request.user, job.pk)
        messages.success(
            request, "Job unassigned. Automatic matching is disabled until you assign it again."
        )
    elif target.isdecimal():
        step = get_object_or_404(PlanStep, pk=target, plan__user=request.user, plan__archived=False)
        try:
            allocate_job(request.user, job.pk, step.pk, reassign=True)
            messages.success(
                request, f"Job assigned to plan #{step.plan_id}, step {step.position}."
            )
        except ValueError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Choose a plan or select Unassigned.")
    query = urlencode({"page": request.POST.get("page", "1")})
    return redirect(reverse("aa_industry_planner:jobs") + "?" + query + "#all-industry-jobs")


@planner_access
@require_POST
def link_job(request, pk):
    production = get_object_or_404(ProductionPlan, pk=pk, user=request.user)
    step = get_object_or_404(PlanStep, pk=request.POST.get("step"), plan=production)
    job = get_object_or_404(
        IndustryJob,
        pk=request.POST.get("job"),
        user=request.user,
        source__in=authorized_sources(request.user),
    )
    try:
        allocate_job(request.user, job.pk, step.pk)
        messages.success(request, "Job linked to this production step.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("aa_industry_planner:plan", pk=pk)


@planner_access
@require_POST
def unlink_job(request, pk):
    allocation = get_object_or_404(JobAllocation, pk=pk, step__plan__user=request.user)
    plan_id = allocation.step.plan_id
    unallocate_job(request.user, allocation.job_id)
    return redirect("aa_industry_planner:plan", pk=plan_id)


@planner_access
@require_POST
def check_plan_item(request, pk, item_id):
    production = get_object_or_404(ProductionPlan, pk=pk, user=request.user, archived=False)
    if str(item_id) not in production.snapshot.get("nodes", {}):
        raise Http404
    checked = request.POST.get("checked") == "on"
    PlanChecklistItem.objects.update_or_create(
        plan=production, item_type_id=item_id, defaults={"checked": checked}
    )
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"checked": checked, "item_id": item_id})
    return redirect(reverse("aa_industry_planner:plan", args=[pk]) + f"#tree-node-{item_id}")


@planner_access
def watches(request):
    form = WatchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        InventoryWatch.objects.update_or_create(
            user=request.user,
            item_type=form.cleaned_data["item_type"],
            defaults={k: form.cleaned_data[k] for k in ["minimum", "enabled"]},
        )
        evaluate_watches(request.user)
        return redirect("aa_industry_planner:watches")
    tasks = (
        ReplenishmentTask.objects.filter(watch__user=request.user)
        .select_related("watch__item_type", "plan")
        .defer("plan__snapshot")
    )
    return page(
        request,
        "watches",
        form=form,
        tasks=tasks,
        can_plan_all=plannable_tasks(tasks).exists(),
    )


@planner_access
@require_POST
def replenish(request, pk=None):
    try:
        production = plan_replenishment(request.user, pk)
        return redirect("aa_industry_planner:plan", pk=production.pk)
    except ReplenishmentTask.DoesNotExist as exc:
        raise Http404 from exc
    except (ValueError, PlanningError) as exc:
        messages.error(request, str(exc))
        return redirect("aa_industry_planner:watches")


@planner_access
def configuration(request):
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)
    prefs_form = PreferencesForm(
        request.POST if request.POST.get("form") == "preferences" else None, instance=preferences
    )
    operations_form = OperationsForm(
        request.POST if request.POST.get("form") == "operations" else None, instance=preferences
    )
    policy_form = PolicyForm(request.POST if request.POST.get("form") == "policy" else None)
    assignment_form = AssignmentForm(
        request.POST if request.POST.get("form") == "assignment" else None, user=request.user
    )
    storage_form = StorageLocationForm(
        request.POST if request.POST.get("form") == "storage" else None,
        user=request.user,
        prefix="storage",
    )
    saved_location_form = SavedStorageLocationForm(
        request.POST if request.POST.get("form") == "save_storage_location" else None,
        prefix="saved-location",
    )
    if request.method == "POST":
        action = request.POST.get("form")
        if action == "operations" and operations_form.is_valid():
            operations_form.save()
            for activity in ["manufacturing", "reaction"]:
                if any(field.startswith(f"{activity}_") for field in operations_form.changed_data):
                    SlotMonitorState.objects.filter(user=request.user, activity=activity).update(
                        warning_active=False
                    )
            messages.success(request, "Production slot and monitoring settings saved.")
            return redirect(reverse("aa_industry_planner:configuration") + "#production-slots")
        if action == "storage" and storage_form.is_valid():
            ActivityStorageLocation.objects.get_or_create(
                user=request.user,
                activity=storage_form.cleaned_data["activity"],
                facility=storage_form.cleaned_data["facility"],
            )
            evaluate_watches(request.user)
            return redirect("aa_industry_planner:configuration")
        if action == "save_storage_location" and saved_location_form.is_valid():
            location_id = saved_location_form.cleaned_data["location_id"]
            if Facility.visible_to(request.user).filter(structure_id=location_id).exists():
                messages.info(request, "This facility is already in your location list.")
                return redirect(reverse("aa_industry_planner:configuration") + "#activity-storage")
            if Facility.objects.filter(structure_id=location_id).exists():
                saved_location_form.add_error(
                    "location_id", "This facility is unavailable to your account."
                )
            elif not (is_station_id(location_id) or location_id >= 10**12):
                saved_location_form.add_error("location_id", "Enter a station or structure ID.")
            else:
                saved = SavedStorageLocation.objects.filter(
                    user=request.user, location_id=location_id
                )
                name = (
                    saved_location_form.cleaned_data["name"]
                    or saved.values_list("name", flat=True).first()
                    or lookup_storage_name(request.user, location_id)
                )
                if name:
                    saved.update_or_create(
                        user=request.user, location_id=location_id, defaults={"name": name}
                    )
                    messages.success(
                        request, "Location saved. Choose which activities can use its stock."
                    )
                    return redirect(
                        reverse("aa_industry_planner:configuration") + "#activity-storage"
                    )
                saved_location_form.add_error(
                    "name", "Lookup could not find a name. Enter one for this location."
                )
        if action == "preferences" and prefs_form.is_valid():
            prefs_form.save()
            return redirect("aa_industry_planner:configuration")
        if action == "policy" and policy_form.is_valid():
            ItemPolicy.objects.update_or_create(
                user=request.user,
                item_type=policy_form.cleaned_data["item_type"],
                defaults={k: policy_form.cleaned_data[k] for k in ["buy", "blueprint_type"]},
            )
            evaluate_watches(request.user)
            return redirect("aa_industry_planner:configuration")
        if action == "assignment" and assignment_form.is_valid():
            family = assignment_form.cleaned_data["bonus_family"]
            FacilityAssignment.objects.update_or_create(
                user=request.user,
                activity=assignment_form.cleaned_data["activity"],
                scope_key=BONUS_SCOPE_OFFSET + family if family else 0,
                defaults={
                    "service": assignment_form.cleaned_data["service"],
                    "bonus_family": family,
                    "item_group": None,
                },
            )
            evaluate_watches(request.user)
            return redirect("aa_industry_planner:configuration")
    return page(
        request,
        "configuration",
        prefs_form=prefs_form,
        operations_form=operations_form,
        capacity=capacity_status(request.user, preferences),
        policy_form=policy_form,
        assignment_form=assignment_form,
        storage_form=storage_form,
        saved_location_form=saved_location_form,
        storage_locations=storage_choices(request.user),
        policies=ItemPolicy.objects.filter(user=request.user).select_related(
            "item_type", "blueprint_type"
        ),
        assignments=FacilityAssignment.objects.filter(user=request.user).select_related(
            "service__facility", "item_group"
        ),
        **source_overview(request.user),
    )


@planner_access
@require_POST
def delete_policy(request, pk):
    get_object_or_404(ItemPolicy, pk=pk, user=request.user).delete()
    evaluate_watches(request.user)
    return redirect("aa_industry_planner:configuration")


@planner_access
@require_POST
def delete_assignment(request, pk):
    get_object_or_404(FacilityAssignment, pk=pk, user=request.user).delete()
    evaluate_watches(request.user)
    return redirect("aa_industry_planner:configuration")


@planner_access
@require_POST
def delete_storage_location(request, pk):
    get_object_or_404(ActivityStorageLocation, pk=pk, user=request.user).delete()
    evaluate_watches(request.user)
    return redirect("aa_industry_planner:configuration")


@planner_access
@require_POST
def set_activity_storage(request, location_id):
    selected = {activity for activity in Activity.values if request.POST.get(activity) == "on"}
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        facility = Facility.visible_to(request.user).filter(structure_id=location_id).first()
        saved = (
            SavedStorageLocation.available_to(request.user)
            .select_for_update()
            .filter(location_id=location_id)
            .first()
        )
        if not (facility or saved):
            raise Http404
        if facility:
            shared = ActivityStorageLocation.objects.filter(user=request.user, facility=facility)
            shared.exclude(activity__in=selected).delete()
            for activity in selected:
                shared.get_or_create(user=request.user, facility=facility, activity=activity)
        if saved:
            personal = selected if not facility else set()
            saved.manufacturing = "manufacturing" in personal
            saved.reaction = "reaction" in personal
            saved.save(update_fields=["manufacturing", "reaction"])
    evaluate_watches(request.user)
    messages.success(request, "Storage activities saved.")
    return redirect(reverse("aa_industry_planner:configuration") + "#activity-storage")


@planner_access
@require_POST
def delete_saved_storage_location(request, pk):
    get_object_or_404(SavedStorageLocation, pk=pk, user=request.user).delete()
    evaluate_watches(request.user)
    return redirect(reverse("aa_industry_planner:configuration") + "#activity-storage")


@planner_access
def notifications(request):
    preferences, _ = NotificationPreferences.objects.get_or_create(user=request.user)
    form = NotificationPreferencesForm(request.POST or None, instance=preferences)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("aa_industry_planner:notifications")
    return page(
        request,
        "notifications",
        form=form,
        notifications=Notification.objects.filter(user=request.user)[:200],
    )


@planner_access
@require_POST
def mark_read(request):
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )
    return redirect("aa_industry_planner:notifications")


@planner_access
@permission_required("aa_industry_planner.manage_facilities", raise_exception=True)
def facilities(request):
    from .industry import FitError

    facilities = list(
        Facility.visible_to(request.user)
        .select_related("solar_system", "structure_type")
        .prefetch_related(
            "services__rigs__item_type", "fitted_modules__item_type", "fitted_rigs__item_type"
        )
        .order_by("solar_system__name", "name", "pk")
    )
    systems = {}
    for facility in facilities:
        system = systems.setdefault(
            facility.solar_system_id,
            dict(
                id=facility.solar_system_id,
                name=facility.solar_system.name,
                facilities=[],
                indices=[],
            ),
        )
        system["facilities"].append(facility)
        if facility.structure_type_id:
            try:
                facility.bonus_rows = facility.industry_fit().preview()
            except FitError as exc:
                facility.bonus_error = str(exc)
    labels = {
        "manufacturing": "Manufacturing",
        "reaction": "Reactions",
        "researching_material_efficiency": "ME research",
        "researching_time_efficiency": "TE research",
        "copying": "Copying",
        "invention": "Invention",
    }
    activity_order = {activity: position for position, activity in enumerate(labels)}
    latest = set()
    indices = SystemCostIndex.objects.filter(
        solar_system_id__in=systems,
        recorded_at__gte=timezone.now() - timezone.timedelta(hours=2),
    ).order_by("-recorded_at", "activity")
    for index in indices:
        key = (index.solar_system_id, index.activity)
        if key not in latest:
            latest.add(key)
            index.label = labels.get(index.activity, index.activity.replace("_", " "))
            systems[index.solar_system_id]["indices"].append(index)
    for system in systems.values():
        system["indices"].sort(key=lambda index: activity_order.get(index.activity, len(labels)))
    return page(request, "facilities", systems=list(systems.values()))


@planner_access
@permission_required("aa_industry_planner.manage_facilities", raise_exception=True)
def index_history(request, pk):
    facility = get_object_or_404(
        Facility.visible_to(request.user).select_related("solar_system"), pk=pk
    )
    return _index_history(facility.solar_system)


@planner_access
@permission_required("aa_industry_planner.manage_facilities", raise_exception=True)
def system_index_history(request, pk):
    facility = (
        Facility.visible_to(request.user)
        .filter(solar_system_id=pk)
        .select_related("solar_system")
        .first()
    )
    if facility is None:
        raise Http404
    return _index_history(facility.solar_system)


def _index_history(system):
    rows = (
        SystemCostIndex.objects.filter(
            solar_system=system,
            recorded_at__gte=timezone.now() - timezone.timedelta(days=90),
        )
        .order_by("recorded_at")
        .values("activity", "cost_index", "recorded_at")
    )
    return JsonResponse({"system": str(system.name), "samples": list(rows)})
