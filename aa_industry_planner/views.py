from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
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
    PlanForm,
    PlanSettingsForm,
    PolicyForm,
    PreferencesForm,
    WatchForm,
    blueprint_rows,
)
from .industry import BONUS_SCOPE_OFFSET
from .models import (
    Facility,
    FacilityAssignment,
    IndustryJob,
    InventorySource,
    InventoryWatch,
    ItemPolicy,
    JobAllocation,
    ManualStock,
    Notification,
    NotificationPreferences,
    PlanStep,
    ProductionPlan,
    ReplenishmentTask,
    SystemCostIndex,
    UserPreferences,
)
from .services.inventory import authorized_sources, inventory_totals, source_overview
from .services.jobs import allocate_job, update_progress
from .services.planning import create_plan
from .services.planning import refresh_plan as refresh_production_plan
from .services.presentation import plan_views
from .services.type_search import search_types
from .services.watches import evaluate_watches, plan_replenishment


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


@planner_access
def dashboard(request):
    plans = ProductionPlan.objects.filter(user=request.user, archived=False).select_related(
        "item_type"
    )
    tasks = (
        ReplenishmentTask.objects.filter(watch__user=request.user)
        .exclude(state="resolved")
        .select_related("watch__item_type", "plan")
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
    form = PlanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            production = create_plan(
                request.user,
                form.cleaned_data["item_type"].pk,
                form.cleaned_data["quantity"],
                form.cleaned_data["name"],
                form.cleaned_data["use_inventory"],
                default_me=form.cleaned_data["default_material_efficiency"],
                default_te=form.cleaned_data["default_time_efficiency"],
            )
            return redirect("aa_industry_planner:plan", pk=production.pk)
        except PlanningError as exc:
            form.add_error(None, str(exc))
    return page(
        request,
        "plans",
        form=form,
        plans=ProductionPlan.objects.filter(user=request.user, archived=False)
        .select_related("item_type")
        .defer("snapshot"),
        archived_plans=ProductionPlan.objects.filter(user=request.user, archived=True)
        .select_related("item_type")
        .defer("snapshot"),
    )


@planner_access
def plan_detail(request, pk):
    production = get_object_or_404(ProductionPlan, pk=pk, user=request.user)
    return _plan_page(request, production)


def _plan_page(request, production, settings_form=None, blueprints_formset=None):
    update_progress(request.user)
    steps = list(
        production.steps.select_related("item_type", "service__facility").prefetch_related(
            "allocations__job"
        )
    )
    snapshot = production.snapshot
    nodes = snapshot.get("nodes", {})
    step_map = {step.item_type_id: step for step in steps}

    tree, dependency_graph = plan_views(
        snapshot,
        {
            tid: {"position": step.position, "progress": step.progress}
            for tid, step in step_map.items()
        },
    )
    inputs = [n for n in nodes.values() if n["missing"] and not n["runs"]]
    dependencies = {
        s.item_type_id: [
            step_map[int(t)].position for t in s.details.get("materials", {}) if int(t) in step_map
        ]
        for s in steps
    }
    for step in steps:
        step.dependency_positions = dependencies[step.item_type_id]
    visible_jobs = IndustryJob.objects.filter(
        user=request.user, source__in=authorized_sources(request.user), allocation__isnull=True
    ).exclude(status__in=["cancelled", "reverted"])
    if settings_form is None:
        settings_form = PlanSettingsForm(instance=production, prefix="settings")
    if blueprints_formset is None:
        blueprints_formset = BlueprintFormSet(
            initial=blueprint_rows(production), prefix="blueprints"
        )
    for row in blueprints_formset:
        row.fields["material_efficiency"].widget.attrs["placeholder"] = "Plan default"
        row.fields["time_efficiency"].widget.attrs["placeholder"] = "Plan default"
        for name, label in [("material_efficiency", "ME"), ("time_efficiency", "TE")]:
            row.fields[name].widget.attrs["aria-label"] = (
                f"{row.initial.get('name', 'Blueprint')} {label} override (%)"
            )
            row.fields[name].widget.attrs["style"] = "max-width: 8rem"
    return page(
        request,
        "plan",
        plan=production,
        steps=steps,
        tree=tree,
        dependency_graph=dependency_graph,
        inputs=inputs,
        candidate_jobs=visible_jobs[:300],
        settings_form=settings_form,
        blueprints_formset=blueprints_formset,
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
@require_http_methods(["GET", "POST"])
def delete_archived_plans(request, pk=None):
    candidates = ProductionPlan.objects.filter(user=request.user, archived=True)
    selected = (
        get_object_or_404(candidates.select_related("item_type").defer("snapshot"), pk=pk)
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
    form = ManualStockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source, _ = InventorySource.objects.get_or_create(
            user=request.user, kind="manual", owner_id=0, defaults={"name": "Declared inventory"}
        )
        ManualStock.objects.update_or_create(
            source=source,
            item_type=form.cleaned_data["item_type"],
            location=form.cleaned_data["location"],
            defaults={"quantity": form.cleaned_data["quantity"]},
        )
        evaluate_watches(request.user)
        messages.success(request, "Declared inventory updated.")
        return redirect("aa_industry_planner:inventory")
    totals = inventory_totals(request.user)
    watches = {
        w.item_type_id: w for w in InventoryWatch.objects.filter(user=request.user, enabled=True)
    }
    names = {t.pk: str(t.name) for t in ItemType.objects.filter(pk__in=set(totals) | set(watches))}
    policies = set(
        ItemPolicy.objects.filter(user=request.user, buy=True).values_list(
            "item_type_id", flat=True
        )
    )
    rows = [
        {
            "type_id": t,
            "name": names.get(t, str(t)),
            "quantity": totals.get(t, 0),
            "minimum": watches[t].minimum if t in watches else None,
            "low": t in watches and totals.get(t, 0) < watches[t].minimum,
            "input": t in policies,
        }
        for t in set(totals) | set(watches)
    ]
    rows.sort(key=lambda r: (not r["low"], r["name"]))
    return page(
        request,
        "inventory",
        form=form,
        rows=rows,
        manual=ManualStock.objects.filter(source__user=request.user).select_related("item_type"),
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
    return redirect(
        "aa_industry_planner:jobs"
        if request.POST.get("return_to") == "jobs"
        else "aa_industry_planner:inventory"
    )


def connect(kind):
    from esi.decorators import token_required

    from .providers import CHARACTER_SCOPES, CORPORATION_SCOPES

    scopes = CORPORATION_SCOPES if kind == "corporation" else CHARACTER_SCOPES

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
            token.user = request.user
            token.save(update_fields=["user"])
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
                defaults={"name": name, "token": token, "enabled": True, "last_error": ""},
            )
            from .tasks import refresh_source

            refresh_source.delay(source.pk)
            return redirect("aa_industry_planner:inventory")

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


@planner_access
def jobs(request):
    qs = IndustryJob.objects.filter(
        user=request.user, source__in=authorized_sources(request.user)
    ).select_related("source", "allocation__step__plan")
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    from django.core.paginator import Paginator

    jobs_page = Paginator(qs, 100).get_page(request.GET.get("page"))
    ids = {tid for job in jobs_page for tid in [job.product_type_id, job.blueprint_type_id] if tid}
    names = dict(ItemType.objects.filter(pk__in=ids).values_list("id", "name"))
    from eveuniverse.models import EveEntity

    installers = dict(
        EveEntity.objects.filter(pk__in=[j.installer_id for j in jobs_page]).values_list(
            "id", "name"
        )
    )
    for job in jobs_page:
        job.product_name = (
            names.get(job.product_type_id)
            or names.get(job.blueprint_type_id)
            or str(job.product_type_id or job.blueprint_type_id)
        )
        job.installer_name = installers.get(job.installer_id) or str(job.installer_id)
    return page(
        request,
        "jobs",
        jobs=jobs_page,
        status=status,
        statuses=["active", "paused", "ready", "delivered", "cancelled", "reverted"],
        **source_overview(request.user),
    )


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
    allocation.delete()
    update_progress(request.user)
    return redirect("aa_industry_planner:plan", pk=plan_id)


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
    return page(
        request,
        "watches",
        form=form,
        tasks=ReplenishmentTask.objects.filter(watch__user=request.user).select_related(
            "watch__item_type", "plan"
        ),
    )


@planner_access
@require_POST
def replenish(request, pk):
    get_object_or_404(ReplenishmentTask, pk=pk, watch__user=request.user)
    try:
        production = plan_replenishment(request.user, pk)
        return redirect("aa_industry_planner:plan", pk=production.pk)
    except (ValueError, PlanningError) as exc:
        messages.error(request, str(exc))
        return redirect("aa_industry_planner:watches")


@planner_access
def configuration(request):
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)
    prefs_form = PreferencesForm(
        request.POST if request.POST.get("form") == "preferences" else None, instance=preferences
    )
    policy_form = PolicyForm(request.POST if request.POST.get("form") == "policy" else None)
    assignment_form = AssignmentForm(
        request.POST if request.POST.get("form") == "assignment" else None, user=request.user
    )
    if request.method == "POST":
        action = request.POST.get("form")
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
            return redirect("aa_industry_planner:configuration")
    return page(
        request,
        "configuration",
        prefs_form=prefs_form,
        policy_form=policy_form,
        assignment_form=assignment_form,
        policies=ItemPolicy.objects.filter(user=request.user).select_related(
            "item_type", "blueprint_type"
        ),
        assignments=FacilityAssignment.objects.filter(user=request.user).select_related(
            "service__facility", "item_group"
        ),
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
    return redirect("aa_industry_planner:configuration")


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
    )
    for facility in facilities:
        recent = SystemCostIndex.objects.filter(
            solar_system=facility.solar_system,
            recorded_at__gte=timezone.now() - timezone.timedelta(hours=2),
        )
        latest = {}
        for index in recent:
            latest.setdefault(index.activity, index)
        facility.current_indices = list(latest.values())
        if facility.structure_type_id:
            try:
                facility.bonus_rows = facility.industry_fit().preview()
            except FitError as exc:
                facility.bonus_error = str(exc)
    return page(request, "facilities", facilities=facilities)


@planner_access
@permission_required("aa_industry_planner.manage_facilities", raise_exception=True)
def index_history(request, pk):
    facility = get_object_or_404(Facility.visible_to(request.user), pk=pk)
    rows = (
        SystemCostIndex.objects.filter(
            solar_system=facility.solar_system,
            recorded_at__gte=timezone.now() - timezone.timedelta(days=90),
        )
        .order_by("recorded_at")
        .values("activity", "cost_index", "recorded_at")
    )
    return JsonResponse({"system": str(facility.solar_system.name), "samples": list(rows)})
