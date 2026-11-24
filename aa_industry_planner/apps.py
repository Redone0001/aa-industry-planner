from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class IndustryPlannerConfig(AppConfig):
    name = "aa_industry_planner"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = _("Industry Planner")
