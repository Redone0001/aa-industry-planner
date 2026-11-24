"""SQLite integration environment with real AA / SDE / eveuniverse models."""

from pathlib import Path

from allianceauth.project_template.project_name.settings.base import *  # noqa: F403

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "local-test-settings-not-for-deployment"
DEBUG = True
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"
LANGUAGES = [
    (code, code)
    for code in [
        "en",
        "de",
        "es",
        "it-it",
        "ja",
        "ko-kr",
        "fr-fr",
        "nl-nl",
        "pl-pl",
        "ru",
        "uk",
        "zh-hans",
    ]
]
INSTALLED_APPS = [
    "modeltranslation",
    "tests.apps.LocalAllianceAuthConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "django_bootstrap5",
    "sri",
    "solo",
    "sortedm2m",
    "esi",
    "tests.apps.LocalAuthenticationConfig",
    "allianceauth.eveonline",
    "allianceauth.groupmanagement",
    "allianceauth.services",
    "allianceauth.notifications",
    "allianceauth.admin_status",
    "allianceauth.analytics",
    "allianceauth.crontab",
    "allianceauth.thirdparty.navhelper",
    "django_celery_beat",
    "allianceauth.menu",
    "allianceauth.framework",
    "allianceauth.theme",
    "allianceauth.theme.darkly",
    "allianceauth.theme.flatly",
    "allianceauth.theme.materia",
    "allianceauth.custom_css",
    "eve_sde",
    "eveuniverse",
    "aa_industry_planner",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dev.sqlite3"}}
ROOT_URLCONF = "tests.urls"
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/industry/"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SITE_URL = "http://localhost:8000"
SITE_NAME = "Alliance Operations"
ESI_SSO_CLIENT_ID = "test-only"
ESI_SSO_CLIENT_SECRET = "test-only"
ESI_SSO_CALLBACK_URL = "http://localhost:8000/sso/callback/"
ESI_USER_CONTACT_EMAIL = "developer@example.test"
CELERY_BROKER_URL = "memory://"
CELERY_TASK_ALWAYS_EAGER = False
CELERY_RESULT_BACKEND = "cache+memory://"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
LOGGING = {"version": 1, "disable_existing_loggers": False}
STATICFILES_DIRS = []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
WSGI_APPLICATION = None
