# company/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
import logging

from .models import Company  # Assuming Company model is in company/models.py

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Company, dispatch_uid="company_created_onboarding_tasks")
def company_created_onboarding_handler(sender, instance: Company, created: bool, raw: bool = False, **kwargs):
    """
    Handles tasks that need to occur when a new Company is created.
    This acts as a central point to dispatch setup tasks to other apps.
    """
    if created and not raw:
        logger.info(f"New company '{instance.name}' created (ID: {instance.pk}). Initiating onboarding tasks.")

        # --- Call COA Seeding Service from crp_accounting ---
        try:
            # Import the service function dynamically or at the top if crp_accounting is a hard dependency
            from crp_accounting.services.coa_seeding_service import seed_coa_for_company, COASeedingError

            logger.info(f"Attempting to seed COA for '{instance.name}'...")
            seed_coa_for_company(company=instance)  # Call the service
            logger.info(f"COA seeding task initiated for '{instance.name}'. Check crp_accounting logs for details.")

        except ImportError:
            logger.error(
                f"Onboarding for '{instance.name}': Could not import 'crp_accounting.services.coa_seeding_service'. "
                "CoA seeding will be skipped. Ensure 'crp_accounting' app is installed and configured."
            )
        except COASeedingError as e:
            logger.error(
                f"CRITICAL: COA seeding FAILED for new company '{instance.name}' (ID: {instance.pk}). Error: {e}",
                exc_info=True
            )
        except Exception as e:
            logger.error(
                f"CRITICAL: An UNEXPECTED error occurred during COA seeding initiation for company '{instance.name}' "
                f"(ID: {instance.pk}). Error: {e}",
                exc_info=True
            )

        # --- Call Other Onboarding Services ---
        # Example: if you have a user_management app to create default admin for the company
        # try:
        #     from user_management.services import create_default_company_admin
        #     create_default_company_admin(company=instance)
        #     logger.info(f"Default admin creation initiated for '{instance.name}'.")
        # except ImportError:
        #     logger.warning(f"Onboarding for '{instance.name}': user_management service not found. Skipping default admin creation.")
        # except Exception as e:
        #     logger.error(f"Error during default admin creation for '{instance.name}': {e}", exc_info=True)

        logger.info(f"Onboarding tasks initiated for company '{instance.name}'.")

    elif not created:
        logger.debug(f"Company '{instance.name}' (ID: {instance.pk}) was updated. No new onboarding tasks triggered.")
    elif raw:
        logger.info(
            f"Skipping onboarding tasks for Company '{instance.name}' (ID: {instance.pk}) during raw fixture loading.")