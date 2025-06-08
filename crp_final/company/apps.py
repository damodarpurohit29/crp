# company/apps.py
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__) # Use __name__ for module-specific logger

class CompanyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'company'

    def ready(self):
        print("COMPANY APP CONFIG: ready() method CALLED") # <<< ADD THIS
        logger.critical("COMPANY APP CONFIG: ready() method CALLED") # <<< AND THIS
        try:
            import company.signals
            logger.info(f"Successfully imported signals for '{self.name}' app.")
        except ImportError:
            logger.warning(f"Signals module (company/signals.py) not found for '{self.name}' app.")
        except Exception as e:
            logger.error(f"Error importing signals for '{self.name}' app: {e}", exc_info=True)