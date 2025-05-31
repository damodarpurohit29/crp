from django.apps import AppConfig


class CrpAccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crp_accounting'

    def ready(self):
        try:
            import crp_accounting.signals  # noqa
            print("crp_accounting signals imported.")
        except ImportError:
            print("Could not import crp_accounting.signals.")
            pass
