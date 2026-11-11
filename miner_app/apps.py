from django.apps import AppConfig


class MinerAppConfig(AppConfig):
    name = 'miner_app'

    def ready(self):
        from . import signals  # noqa: F401
