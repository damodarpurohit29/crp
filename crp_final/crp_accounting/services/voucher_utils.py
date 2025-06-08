# crp_accounting/services/voucher_utils.py

import logging
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError

# --- Model Imports ---
# These models are assumed to be tenant-scoped where appropriate (e.g., Voucher, AccountingPeriod)
# VoucherSequence is managed by sequence_service and should also be tenant-scoped.
from ..models.journal import Voucher
from ..models.period import AccountingPeriod

# --- Service Imports ---
# sequence_service is now responsible for tenant-aware, atomic number generation
from . import sequence_service  # Ensure this service is fully tenant-aware

logger = logging.getLogger(__name__)


# --- Main Public Function (Tenant Aware) ---

def assign_voucher_number(company_id: int, voucher_instance: Voucher) -> None:
    """
    Orchestrates the assignment of a voucher number if one does not exist.

    It validates prerequisites, checks for period locks, and then calls the
    tenant-aware sequence_service to get the next number. The number is then
    assigned to the voucher_instance (saving is handled by the caller).

    Args:
        company_id: The ID of the company for which this operation is being performed.
        voucher_instance: The Voucher model instance. It must have `voucher_type`,
                          `accounting_period`, and `company` (matching company_id) set.

    Raises:
        DjangoValidationError: If validation fails (e.g., missing fields, locked period,
                               company mismatch, or sequence generation error).
    """
    if voucher_instance.voucher_number:
        logger.debug(
            f"Voucher {voucher_instance.pk} (Co ID: {company_id}) already has number "
            f"'{voucher_instance.voucher_number}'. Skipping assignment."
        )
        return

    logger.info(
        f"Attempting to assign voucher number for Voucher {voucher_instance.pk} "
        f"(Co ID: {company_id}, Type: {voucher_instance.voucher_type})."
    )

    # 1. Validate prerequisites, including company and period alignment
    _validate_voucher_prerequisites(company_id, voucher_instance)

    # Retrieve the period instance from the voucher (it's validated in _validate_voucher_prerequisites)
    # This assumes voucher_instance.accounting_period is already loaded or will be fetched correctly.
    # If not, _validate_voucher_prerequisites should ideally set it if it fetches it.
    period = voucher_instance.accounting_period
    if not period:  # Should be caught by _validate_voucher_prerequisites, but defensive check
        raise DjangoValidationError(
            {'accounting_period': _("Accounting period is unexpectedly missing after validation.")})

    # 2. Check if the accounting period is locked
    if period.locked:
        logger.warning(
            f"Cannot assign voucher number to Voucher {voucher_instance.pk} (Co ID: {company_id}): "
            f"Accounting Period '{period.name}' (ID: {period.pk}) is locked."
        )
        raise DjangoValidationError(
            _("Cannot generate voucher number: Accounting Period '%(period_name)s' is locked.") %
            {'period_name': period.name}
        )

    # 3. Get the next voucher number from the tenant-aware sequence service
    try:
        # The sequence_service.get_next_voucher_number handles atomicity and tenant scoping.
        formatted_number = sequence_service.get_next_voucher_number(
            company_id=company_id,
            voucher_type_value=voucher_instance.voucher_type,  # Pass the value
            period_id=period.pk  # Pass period ID
        )
    except DjangoValidationError as e:  # Catch validation errors from sequence_service
        logger.error(
            f"Validation error from sequence service for Voucher {voucher_instance.pk} (Co ID: {company_id}): {e}"
        )
        raise DjangoValidationError(
            _("Failed to generate voucher number due to a configuration or validation issue: %(error)s") %
            {'error': str(e)}
        ) from e
    except Exception as e:  # Catch any other unexpected errors from sequence_service
        logger.exception(
            f"Unexpected error from sequence service for Voucher {voucher_instance.pk} (Co ID: {company_id})."
        )
        raise DjangoValidationError(
            _("An unexpected system error occurred during voucher number generation. Please contact support.")
        ) from e

    # 4. Assign the generated number to the voucher instance
    voucher_instance.voucher_number = formatted_number
    logger.info(
        f"Successfully assigned voucher number '{formatted_number}' to Voucher {voucher_instance.pk} "
        f"(Co ID: {company_id}, Type: {voucher_instance.voucher_type}, Period: {period.name})."
    )
    # The caller (e.g., Voucher.save() or a service method) is responsible for saving the voucher_instance.


# --- Helper Functions (Tenant Aware) ---

def _validate_voucher_prerequisites(company_id: int, voucher_instance: Voucher) -> None:
    """
    Ensures the voucher instance has necessary fields set and that these align
    with the provided company_id context.

    Args:
        company_id: The ID of the company context.
        voucher_instance: The Voucher instance to validate.

    Raises:
        DjangoValidationError: If any prerequisite is not met.
    """
    errors = {}

    if not voucher_instance.voucher_type:
        errors['voucher_type'] = _("Voucher type must be set.")

    if not voucher_instance.accounting_period_id:  # Check ID first
        errors['accounting_period'] = _("Accounting period must be set.")
    else:
        # Ensure the accounting_period object is loaded and its company matches
        try:
            # Efficiently get or load the period.
            # If voucher_instance.accounting_period is already a loaded object, this avoids a query.
            # Otherwise, it fetches it.
            period = voucher_instance.accounting_period
            if period.company_id != company_id:
                errors['accounting_period'] = _("Accounting period does not belong to the current company.")
        except AccountingPeriod.DoesNotExist:  # Should not happen if FK is valid, but defensive
            errors['accounting_period'] = _("Associated accounting period not found.")
        except AttributeError:  # If .accounting_period somehow isn't loaded and FK is just an ID
            try:
                period = AccountingPeriod.objects.get(pk=voucher_instance.accounting_period_id, company_id=company_id)
                voucher_instance.accounting_period = period  # Cache on instance for later use
            except AccountingPeriod.DoesNotExist:
                errors['accounting_period'] = _(
                    "Accounting period not found or does not belong to the current company.")

    if not voucher_instance.company_id:
        # This indicates a more fundamental issue, as company_id should be set when the voucher is created.
        errors['company'] = _("Voucher company association is missing.")
    elif voucher_instance.company_id != company_id:
        logger.error(
            f"Prerequisite validation failed for Voucher {voucher_instance.pk}: "
            f"Instance company ID {voucher_instance.company_id} does not match context company ID {company_id}."
        )
        errors['company'] = _("Voucher's associated company does not match the current operational context.")

    if errors:
        raise DjangoValidationError(errors)

    logger.debug(f"Voucher {voucher_instance.pk} (Co ID: {company_id}) passed prerequisite validation.")
# --- REMOVED redundant atomic increment helper ---
# The logic is now encapsulated within sequence_service.get_next_voucher_number
# def _increment_sequence_and_get_next_number(...)
# # crp_accounting/services/voucher_utils.py (CORRECTED)
#
# import logging
# from django.db import transaction, IntegrityError
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ValidationError as DjangoValidationError
#
# # --- Model & Service Imports ---
# from ..models.journal import Voucher, VoucherSequence
# from ..models.period import AccountingPeriod
# from .sequence_service import get_or_create_sequence_config
# # from ..exceptions import VoucherWorkflowError, PeriodLockedError # Optional custom exceptions
#
# logger = logging.getLogger(__name__)
#
# # --- Main Public Function ---
#
# def assign_voucher_number(voucher_instance: Voucher):
#     """
#     Generates and assigns the next automatic voucher number if needed.
#
#     - Checks if a voucher number already exists; if so, it returns early.
#     - Proceeds with automatic generation only if `voucher_number` is blank.
#
#     Designed to be called from Voucher.save() under appropriate conditions.
#
#     Args:
#         voucher_instance: The Voucher instance. Period and Type MUST be set.
#
#     Raises:
#         DjangoValidationError: If prerequisites are missing, period is locked,
#                                or generation fails due to configuration/database issues.
#     """
#     # --- Handle Pre-existing Numbers ---
#     if voucher_instance.voucher_number:
#         logger.debug(f"Voucher number '{voucher_instance.voucher_number}' already exists for Voucher {voucher_instance.pk}. Skipping generation.")
#         return # Number already exists, nothing to do.
#
#     # --- Proceed with Automatic Generation ---
#     # If we reach here, voucher_number is blank.
#     logger.info(f"Proceeding with automatic voucher number generation for Voucher {voucher_instance.pk}")
#
#     # 1. Validate Prerequisites (Type and Period must be set for sequence lookup)
#     _validate_voucher_prerequisites(voucher_instance) # Raises DjangoValidationError
#
#     period = voucher_instance.accounting_period # Get related object
#
#     # 2. Check Period Lock (Still relevant for auto-generation timing)
#     if period.locked:
#         logger.warning(f"Attempt to generate voucher number for locked period {str(period)} for Voucher PK {voucher_instance.pk}")
#         # Consider using custom PeriodLockedError here if defined
#         raise DjangoValidationError(
#              _("Cannot generate voucher number: Accounting Period '%(period_name)s' is locked.") % {'period_name': str(period)}
#         )
#
#     # 3. Perform Atomic Increment and Formatting
#     try:
#         sequence_config, next_num = _increment_sequence_and_get_next_number(
#             voucher_instance.voucher_type, period
#         )
#         formatted_number = sequence_config.format_number(next_num)
#
#         # 4. Assign the generated number back to the instance
#         # The actual saving of the instance happens outside this function (in Voucher.save)
#         voucher_instance.voucher_number = formatted_number
#
#         logger.info(
#             "Assigned auto-generated voucher number '%s' to Voucher PK %s (%s / %s)",
#             formatted_number, voucher_instance.pk, voucher_instance.voucher_type, str(period)
#         )
#     except (IntegrityError, DjangoValidationError) as e:
#         logger.error(
#             "Failed during auto-generation process for Voucher PK %s (Type=%s, Period=%s): %s",
#             voucher_instance.pk, voucher_instance.voucher_type, period.name, e
#         )
#         raise DjangoValidationError(_("Failed to generate voucher number. Please check sequence configuration or try again.")) from e
#     except Exception as e:
#         logger.exception(
#             "Unexpected error during auto-generation for Voucher PK %s (Type=%s, Period=%s): %s",
#              voucher_instance.pk, voucher_instance.voucher_type, str(period), e
#         )
#         raise DjangoValidationError(_("An unexpected system error occurred during voucher number generation.")) from e
#
# # --- Helper Functions (Unchanged) ---
#
# def _validate_voucher_prerequisites(voucher_instance: Voucher):
#     """Ensure necessary fields are set on the Voucher for number generation."""
#     if not voucher_instance.voucher_type:
#         raise DjangoValidationError({'voucher_type': _("Voucher type must be set before generating voucher number.")})
#     if not voucher_instance.accounting_period_id:
#         raise DjangoValidationError({'accounting_period': _("Accounting period must be set before generating voucher number.")})
#
# @transaction.atomic
# def _increment_sequence_and_get_next_number(voucher_type: str, period: AccountingPeriod) -> tuple[VoucherSequence, int]:
#     """
#     Atomically retrieves sequence config, increments its counter using DB lock,
#     saves it, and returns the config object and the *new* number.
#     (Implementation remains the same)
#     """
#     # ... (existing implementation of _increment_sequence_and_get_next_number) ...
#     try:
#         sequence_config = get_or_create_sequence_config(voucher_type, period)
#         locked_sequence = VoucherSequence.objects.select_for_update().get(pk=sequence_config.pk)
#         next_number = locked_sequence.last_number + 1
#         locked_sequence.last_number = next_number
#         locked_sequence.save(update_fields=['last_number', 'updated_at'])
#         return locked_sequence, next_number
#     except VoucherSequence.DoesNotExist:
#         logger.error(f"VoucherSequence row disappeared unexpectedly for Type={voucher_type}, Period={str(period)} during atomic increment.")
#         raise DjangoValidationError(_("Sequence configuration lock failed unexpectedly."))
#     except Exception as e:
#         logger.exception(f"Error during atomic sequence increment for Type=%s, Period=%s: {e}", voucher_type, str(period), e)
#         raise