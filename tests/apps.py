from allianceauth.apps import AllianceAuthConfig
from allianceauth.authentication.apps import AuthenticationConfig


class LocalAllianceAuthConfig(AllianceAuthConfig):
    """AA template discovery without checks for production Redis/MySQL services."""

    def ready(self):
        pass


class LocalAuthenticationConfig(AuthenticationConfig):
    """Keep AA ownership signals, omit unrelated Redis task-statistics startup."""

    def ready(self):
        from unittest.mock import patch

        from allianceauth.authentication import signals  # noqa: F401
        from allianceauth.authentication.task_statistics.helpers import _RedisStub

        with patch(
            "allianceauth.authentication.task_statistics.helpers.get_redis_client_or_stub",
            return_value=_RedisStub(),
        ):
            from allianceauth.authentication.task_statistics import counters  # noqa: F401
