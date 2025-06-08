# company/signals.py
import logging # Ensure logging is imported

from django.db.models.signals import post_save
from django.dispatch import receiver
# from django.conf import settings # Not strictly needed for this version of the file

from .models import Company  # Your Company model

# --- Import CompanyAccountingSettings ---
try:
    # Assuming CompanyAccountingSettings is in a file like 'models_settings.py'
    # within the same 'company' app directory. Adjust if path is different.
    from .models_settings import CompanyAccountingSettings
except ImportError:
    CompanyAccountingSettings = None # type: ignore
    # Log this critical failure once at module level
    logging.critical(
        "Company Signals: CRITICAL - CompanyAccountingSettings model could not be imported. "
        "Auto-creation of accounting settings for new companies will FAIL. "
        "Ensure 'company.models_settings.CompanyAccountingSettings' is correctly defined and importable."
    )

logger = logging.getLogger("company.signals") # Specific logger for this signals file


@receiver(post_save, sender=Company, dispatch_uid="company_created_onboarding_tasks_v2") # Added _v2 to uid if old one exists
def company_created_onboarding_handler(sender, instance: Company, created: bool, raw: bool = False, **kwargs):
    """
    Handles tasks that need to occur when a new Company is created.
    This includes:
    1. Auto-creating an associated CompanyAccountingSettings record.
    2. Initiating Chart of Accounts (CoA) seeding.
    3. (Placeholder for other onboarding services).
    """
    if created and not raw: # Only run for genuinely new instances, not during fixture loading
        log_prefix = f"[CompanyOnboard][CoName:'{instance.name}'][CoID:{instance.pk}]"
        logger.info(f"{log_prefix} New company created. Initiating onboarding tasks.")

        # --- 1. Ensure CompanyAccountingSettings record exists ---
        if CompanyAccountingSettings: # Check if the model was successfully imported
            try:
                # get_or_create is atomic and handles potential race conditions if signal fired multiple times.
                # The OneToOneField with primary_key=True on CompanyAccountingSettings ensures uniqueness.
                settings_obj, settings_created = CompanyAccountingSettings.objects.get_or_create(
                    company=instance,
                    # Optionally, you could provide some very basic defaults here if your model allows,
                    # but usually, it's better to have them null and configured by an admin.
                    # defaults={'some_default_field': 'initial_value'}
                )
                if settings_created:
                    logger.info(f"{log_prefix} Successfully auto-created CompanyAccountingSettings record.")
                else:
                    logger.info(f"{log_prefix} CompanyAccountingSettings record already existed.")
            except Exception as e_settings:
                logger.error(
                    f"{log_prefix} CRITICAL ERROR: Failed to create/ensure CompanyAccountingSettings. Error: {e_settings}",
                    exc_info=True
                )
        else:
            logger.error(f"{log_prefix} CompanyAccountingSettings model not available. Skipping settings creation.")


        # --- 2. Call COA Seeding Service from crp_accounting ---
        try:
            from crp_accounting.services.coa_seeding_service import seed_coa_for_company, COASeedingError # Assuming COASeedingError exists

            logger.info(f"{log_prefix} Attempting to seed COA...")
            seed_coa_for_company(company=instance)
            logger.info(f"{log_prefix} COA seeding task/call initiated. See crp_accounting logs for details.")
        except ImportError:
            logger.error(
                f"{log_prefix} Could not import 'crp_accounting.services.coa_seeding_service'. "
                "CoA seeding will be SKIPPED. Ensure 'crp_accounting' app and service are installed/configured."
            )
        except COASeedingError as e_coa_seed: # Catch your specific seeding error
            logger.error(
                f"{log_prefix} CRITICAL: COA seeding FAILED. Error: {e_coa_seed}",
                exc_info=True # Include traceback for COASeedingError
            )
        except Exception as e_coa_other: # Catch any other unexpected error during CoA seeding
            logger.error(
                f"{log_prefix} CRITICAL: An UNEXPECTED error occurred during COA seeding initiation. Error: {e_coa_other}",
                exc_info=True
            )

        # --- 3. Call Other Onboarding Services (Example Placeholder) ---
        # try:
        #     from user_management.services import create_default_company_admin
        #     create_default_company_admin(company=instance)
        #     logger.info(f"{log_prefix} Default company admin user creation initiated.")
        # except ImportError:
        #     logger.warning(f"{log_prefix} user_management service not found. Skipping default admin creation.")
        # except Exception as e_user_mgmt:
        #     logger.error(f"{log_prefix} Error during default admin creation: {e_user_mgmt}", exc_info=True)

        logger.info(f"{log_prefix} Onboarding tasks finished.")

    elif not created:
        logger.debug(f"Company '{instance.name}' (ID: {instance.pk}) was updated. No new onboarding tasks from this signal.")
    elif raw:
        logger.info(f"Skipping onboarding for Company '{instance.name}' (ID: {instance.pk}) during raw fixture loading.")
# # company/signals.py
# from django.db.models.signals import post_save
# from django.dispatch import receiver
# from django.conf import settings
# import logging
#
# from .models import Company  # Assuming Company model is in company/models.py
#
# logger = logging.getLogger(__name__)
#
#
# @receiver(post_save, sender=Company, dispatch_uid="company_created_onboarding_tasks")
# def company_created_onboarding_handler(sender, instance: Company, created: bool, raw: bool = False, **kwargs):
#     """
#     Handles tasks that need to occur when a new Company is created.
#     This acts as a central point to dispatch setup tasks to other apps.
#     """
#     if created and not raw:
#         logger.info(f"New company '{instance.name}' created (ID: {instance.pk}). Initiating onboarding tasks.")
#
#         # --- Call COA Seeding Service from crp_accounting ---
#         try:
#             # Import the service function dynamically or at the top if crp_accounting is a hard dependency
#             from crp_accounting.services.coa_seeding_service import seed_coa_for_company, COASeedingError
#
#             logger.info(f"Attempting to seed COA for '{instance.name}'...")
#             seed_coa_for_company(company=instance)  # Call the service
#             logger.info(f"COA seeding task initiated for '{instance.name}'. Check crp_accounting logs for details.")
#
#         except ImportError:
#             logger.error(
#                 f"Onboarding for '{instance.name}': Could not import 'crp_accounting.services.coa_seeding_service'. "
#                 "CoA seeding will be skipped. Ensure 'crp_accounting' app is installed and configured."
#             )
#         except COASeedingError as e:
#             logger.error(
#                 f"CRITICAL: COA seeding FAILED for new company '{instance.name}' (ID: {instance.pk}). Error: {e}",
#                 exc_info=True
#             )
#         except Exception as e:
#             logger.error(
#                 f"CRITICAL: An UNEXPECTED error occurred during COA seeding initiation for company '{instance.name}' "
#                 f"(ID: {instance.pk}). Error: {e}",
#                 exc_info=True
#             )
#
#         # --- Call Other Onboarding Services ---
#         # Example: if you have a user_management app to create default admin for the company
#         # try:
#         #     from user_management.services import create_default_company_admin
#         #     create_default_company_admin(company=instance)
#         #     logger.info(f"Default admin creation initiated for '{instance.name}'.")
#         # except ImportError:
#         #     logger.warning(f"Onboarding for '{instance.name}': user_management service not found. Skipping default admin creation.")
#         # except Exception as e:
#         #     logger.error(f"Error during default admin creation for '{instance.name}': {e}", exc_info=True)
#
#         logger.info(f"Onboarding tasks initiated for company '{instance.name}'.")
#
#     elif not created:
#         logger.debug(f"Company '{instance.name}' (ID: {instance.pk}) was updated. No new onboarding tasks triggered.")
#     elif raw:
#         logger.info(
#             f"Skipping onboarding tasks for Company '{instance.name}' (ID: {instance.pk}) during raw fixture loading.")