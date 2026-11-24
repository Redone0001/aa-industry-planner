from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

USER = settings.AUTH_USER_MODEL
TYPE = "eve_sde.ItemType"
ONE = Decimal("1")


class Activity(models.TextChoices):
    MANUFACTURING = "manufacturing", _("Manufacturing")
    REACTION = "reaction", _("Reactions")


ACTIVITY_IDS = {"manufacturing": 1, "reaction": 11}


def multiplier(default="1"):
    return models.DecimalField(
        max_digits=9,
        decimal_places=6,
        default=Decimal(default),
        validators=[MinValueValidator(Decimal("0.000001")), MaxValueValidator(ONE)],
    )


def rate(default="0"):
    return models.DecimalField(
        max_digits=9,
        decimal_places=6,
        default=Decimal(default),
        validators=[MinValueValidator(0), MaxValueValidator(ONE)],
    )


class Access(models.Model):
    class Meta:
        default_permissions = ()
        permissions = [
            ("access_planner", "Can access Industry Planner"),
            ("use_corporation_data", "Can connect own corporation assets and jobs"),
            ("manage_facilities", "Can monitor facilities and cost indices"),
        ]


class Facility(models.Model):
    name = models.CharField(max_length=200)
    structure_id = models.PositiveBigIntegerField(unique=True)
    solar_system = models.ForeignKey("eve_sde.SolarSystem", on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    allowed_groups = models.ManyToManyField(
        "auth.Group", blank=True, help_text="Empty permits all planner users."
    )

    class Meta:
        verbose_name_plural = "facilities"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def visible_to(cls, user):
        qs = cls.objects.filter(active=True)
        if user.is_superuser:
            return qs
        return qs.filter(
            models.Q(allowed_groups__isnull=True) | models.Q(allowed_groups__in=user.groups.all())
        ).distinct()


class FacilityService(models.Model):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="services")
    activity = models.CharField(max_length=20, choices=Activity.choices)
    online = models.BooleanField(default=True)
    material_multiplier = multiplier()
    time_multiplier = multiplier()
    cost_multiplier = multiplier()
    facility_tax = rate("0.0025")
    scc_surcharge = rate("0.04")
    service_name = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["facility", "activity"], name="aip_unique_service")
        ]

    def __str__(self):
        return f"{self.facility} · {self.get_activity_display()}"


class FacilityRig(models.Model):
    service = models.ForeignKey(FacilityService, on_delete=models.CASCADE, related_name="rigs")
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT, help_text="Installed rig.")
    applies_to_group = models.ForeignKey(
        "eve_sde.ItemGroup",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Empty applies to all products for this service.",
    )
    material_multiplier = multiplier()
    time_multiplier = multiplier()

    def __str__(self):
        return str(self.item_type)


class FacilityAssignment(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_assignments")
    activity = models.CharField(max_length=20, choices=Activity.choices)
    service = models.ForeignKey(FacilityService, on_delete=models.CASCADE)
    item_group = models.ForeignKey(
        "eve_sde.ItemGroup",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Optional product-group override; empty sets the activity default.",
    )
    # A non-null scope preserves uniqueness of the default on MariaDB as well as SQLite.
    scope_key = models.PositiveBigIntegerField(default=0, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "activity", "scope_key"], name="aip_unique_assignment"
            )
        ]

    def clean(self):
        self.scope_key = self.item_group_id or 0
        if self.service_id and self.service.activity != self.activity:
            raise ValidationError({"service": "The service must support the assigned activity."})

    def save(self, *args, **kwargs):
        self.scope_key = self.item_group_id or 0
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"scope_key"}
        super().save(*args, **kwargs)


class SystemCostIndex(models.Model):
    solar_system = models.ForeignKey("eve_sde.SolarSystem", on_delete=models.CASCADE)
    activity = models.CharField(max_length=40)
    cost_index = rate()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["solar_system", "activity", "recorded_at"], name="aip_unique_index_sample"
            )
        ]
        indexes = [models.Index(fields=["solar_system", "activity", "-recorded_at"])]


class AdjustedPrice(models.Model):
    """Dynamic ESI prices; static type metadata remains exclusively in eve_sde."""

    type_id = models.PositiveBigIntegerField(primary_key=True)
    adjusted_price = models.DecimalField(max_digits=28, decimal_places=4, null=True)
    updated_at = models.DateTimeField(default=timezone.now)


class UserPreferences(models.Model):
    user = models.OneToOneField(USER, on_delete=models.CASCADE, related_name="industry_preferences")
    reaction_runs = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(100000)]
    )
    reaction_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(720)],
        help_text="If set, use duration instead of run count for reaction batches.",
    )
    manufacturing_time_multiplier = multiplier()
    reaction_time_multiplier = multiplier()
    alpha_clone_tax = rate()


class ItemPolicy(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_item_policies")
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT)
    buy = models.BooleanField(default=False)
    material_efficiency = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(10)]
    )
    time_efficiency = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(20)]
    )
    blueprint_type = models.ForeignKey(
        TYPE,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Optional explicit recipe choice.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "item_type"], name="aip_unique_policy")
        ]


class InventorySource(models.Model):
    class Kind(models.TextChoices):
        CHARACTER = "character", _("Character ESI")
        CORPORATION = "corporation", _("Corporation ESI")
        MANUAL = "manual", _("Declared inventory")

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_sources")
    kind = models.CharField(max_length=15, choices=Kind.choices)
    owner_id = models.PositiveBigIntegerField(default=0)
    name = models.CharField(max_length=200)
    token = models.ForeignKey("esi.Token", null=True, blank=True, on_delete=models.SET_NULL)
    enabled = models.BooleanField(default=True)
    include_in_planning = models.BooleanField(
        default=True, help_text="Disable for a manual source that duplicates imported assets."
    )
    last_assets_sync = models.DateTimeField(null=True, blank=True)
    last_jobs_sync = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=250, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "kind", "owner_id"], name="aip_unique_source")
        ]

    def __str__(self):
        return self.name


class Asset(models.Model):
    source = models.ForeignKey(InventorySource, on_delete=models.CASCADE, related_name="assets")
    item_id = models.PositiveBigIntegerField()
    type_id = models.PositiveBigIntegerField(db_index=True)
    quantity = models.PositiveBigIntegerField()
    location_id = models.PositiveBigIntegerField()
    location_flag = models.CharField(max_length=100, blank=True)
    is_blueprint_copy = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "item_id"], name="aip_unique_asset")
        ]


class ManualStock(models.Model):
    source = models.ForeignKey(
        InventorySource, on_delete=models.CASCADE, related_name="manual_stock"
    )
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT)
    quantity = models.PositiveBigIntegerField(default=0)
    location = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "item_type", "location"], name="aip_unique_manual_stock"
            )
        ]


class ProductionPlan(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_plans")
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT)
    quantity = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    archived = models.BooleanField(default=False)
    snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"{self.item_type} × {self.quantity:,}"


class PlanStep(models.Model):
    plan = models.ForeignKey(ProductionPlan, on_delete=models.CASCADE, related_name="steps")
    position = models.PositiveIntegerField()
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT)
    blueprint_type_id = models.PositiveBigIntegerField()
    activity = models.CharField(max_length=20, choices=Activity.choices)
    runs = models.PositiveBigIntegerField()
    output_quantity = models.PositiveBigIntegerField()
    completed_quantity = models.PositiveBigIntegerField(default=0)
    service = models.ForeignKey(FacilityService, null=True, on_delete=models.SET_NULL)
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "item_type"], name="aip_unique_step")
        ]

    @property
    def progress(self):
        return (
            min(100, int(100 * self.completed_quantity / self.output_quantity))
            if self.output_quantity
            else 100
        )


class IndustryJob(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_jobs")
    job_id = models.PositiveBigIntegerField()
    source = models.ForeignKey(InventorySource, on_delete=models.CASCADE, related_name="jobs")
    installer_id = models.PositiveBigIntegerField()
    activity_id = models.PositiveSmallIntegerField()
    blueprint_type_id = models.PositiveBigIntegerField()
    product_type_id = models.PositiveBigIntegerField(null=True)
    facility_id = models.PositiveBigIntegerField()
    runs = models.PositiveIntegerField()
    successful_runs = models.PositiveIntegerField(null=True)
    status = models.CharField(max_length=30)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    cost = models.DecimalField(max_digits=24, decimal_places=2, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["end_date"]
        constraints = [models.UniqueConstraint(fields=["user", "job_id"], name="aip_unique_job")]

    @property
    def finished(self):
        return self.status in {"ready", "delivered"}


class JobAllocation(models.Model):
    job = models.OneToOneField(IndustryJob, on_delete=models.CASCADE, related_name="allocation")
    step = models.ForeignKey(PlanStep, on_delete=models.CASCADE, related_name="allocations")
    quantity = models.PositiveBigIntegerField()


class InventoryWatch(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_watches")
    item_type = models.ForeignKey(TYPE, on_delete=models.PROTECT)
    minimum = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "item_type"], name="aip_unique_watch")
        ]


class ReplenishmentTask(models.Model):
    watch = models.OneToOneField(InventoryWatch, on_delete=models.CASCADE, related_name="task")
    shortage = models.PositiveBigIntegerField(default=0)
    quantity = models.PositiveBigIntegerField(
        default=0, help_text="Additional units after linked or running jobs."
    )
    is_input = models.BooleanField(default=False)
    state = models.CharField(
        max_length=15,
        default="open",
        choices=[("open", "Open"), ("covered", "In production"), ("resolved", "Resolved")],
    )
    plan = models.OneToOneField(ProductionPlan, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationPreferences(models.Model):
    user = models.OneToOneField(USER, on_delete=models.CASCADE)
    job_finished = models.BooleanField(default=True)
    inventory_low = models.BooleanField(default=True)
    sync_failed = models.BooleanField(default=True)
    discord_dm = models.BooleanField(default=False)


class Notification(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="industry_notifications")
    event_key = models.CharField(max_length=200)
    kind = models.CharField(max_length=30)
    title = models.CharField(max_length=200)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    discord_sent_at = models.DateTimeField(null=True, blank=True)
    discord_error = models.CharField(max_length=250, blank=True)
    discord_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "event_key"], name="aip_unique_notification")
        ]


class PlannerConfiguration(models.Model):
    discord_guild_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="Discord Multiverse managed guild used to resolve linked accounts.",
    )

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Industry Planner configuration"
