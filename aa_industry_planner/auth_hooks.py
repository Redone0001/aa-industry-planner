from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook
from django.utils.translation import gettext_lazy as _


class IndustryMenu(MenuItemHook):
    def __init__(self):
        super().__init__(
            str(_("Industry Planner")),
            "fa-solid fa-industry",
            "aa_industry_planner:dashboard",
            navactive=["aa_industry_planner:"],
        )

    def render(self, request):
        if request.user.has_perm("aa_industry_planner.access_planner"):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def menu_item():
    return IndustryMenu()


@hooks.register("url_hook")
def url_hook():
    return UrlHook("aa_industry_planner.urls", "aa_industry_planner", r"^industry/")
