from django.contrib.auth.views import LoginView
from django.urls import include, path

urlpatterns = [
    path("login/", LoginView.as_view(template_name="admin/login.html"), name="login"),
    path("", include("allianceauth.urls")),
]
