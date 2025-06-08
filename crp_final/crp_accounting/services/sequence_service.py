# crp_accounting/services/sequence_service.py

import logging
import math
from typing import Optional

from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.db import transaction, IntegrityError

# --- Model Imports ---
from ..models.journal import VoucherSequence  # Assuming this now inherits from TenantScopedModel or similar
from ..models.period import AccountingPeriod

# --- Company Model Import (for type hinting and validation) ---
try:
    from company.models import Company
except ImportError:
    Company = None
    logging.error("Sequence Service: Failed to import Company model. Some type checks might be skipped.")

logger = logging.getLogger(__name__)


def _calculate_quarter(date_obj) -> int:
    """Calculates the fiscal quarter (1-4) for a given date."""
    if not date_obj:
        # This case should ideally be prevented by checks on AccountingPeriod having a start_date
        logger.warning("Calculating quarter for a date_obj that is None. Returning 1 as fallback.")
        return 1  # Fallback, or raise error
    return math.ceil(date_obj.month / 3.0)


def _get_default_prefix(
        company: Optional[Company],  # Company instance for potential prefix customization
        voucher_type_value: str,
        period: AccountingPeriod
) -> str:
    """
    Generates a default prefix for a voucher sequence.
    Example: {COMSHORT}-JV-2024Q1-
    """
    if not period or not period.start_date:
        logger.error(
            f"Cannot generate default prefix for Period ID {period.pk if period else 'None'}: "
            "AccountingPeriod instance or its start_date is missing."
        )
        # Fallback prefix, consider making this more unique or raising an error
        return f"{voucher_type_value[:3].upper()}-DEF-"

    year_str = period.start_date.strftime('%Y')
    quarter = _calculate_quarter(period.start_date)
    period_code = f"{year_str}Q{quarter}"

    # Company-specific part of the prefix (optional)
    company_prefix_part = ""
    if company and hasattr(company, 'internal_company_code') and company.internal_company_code:
        company_prefix_part = f"{company.internal_company_code[:5].upper()}-"  # Use internal code if available
    elif company and hasattr(company, 'subdomain_prefix'):
        company_prefix_part = f"{company.subdomain_prefix[:5].upper()}-"  # Fallback to subdomain

    # Ensure voucher_type_value is a string and take first few chars
    vt_prefix = str(voucher_type_value)[:3].upper() if voucher_type_value else "GEN"

    return f"{company_prefix_part}{vt_prefix}-{period_code}-"


def get_or_create_sequence_config(
        company_id: int,
        voucher_type_value: str,  # Expecting the value, e.g., 'GENERAL_JOURNAL'
        period_id: int  # Expecting the ID of the accounting period
) -> VoucherSequence:
    """
    Retrieves the VoucherSequence configuration for the given company, voucher type,
    and accounting period ID. Creates it with default settings if it doesn't exist.

    Args:
        company_id: The ID of the Company this sequence belongs to.
        voucher_type_value: The voucher type identifier (e.g., 'GENERAL_JOURNAL', 'SALES_INVOICE').
        period_id: The ID of the AccountingPeriod.

    Returns:
        The VoucherSequence instance.

    Raises:
        ImproperlyConfigured: If essential arguments are missing or invalid.
        AccountingPeriod.DoesNotExist: If the period_id is invalid for the company.
        Company.DoesNotExist: If company_id is invalid (less likely if called from trusted context).
        DjangoValidationError: If validation fails during creation.
    """
    if not all([company_id, voucher_type_value, period_id]):
        raise ImproperlyConfigured(
            "company_id, voucher_type_value, and period_id are required for sequence configuration."
        )

    try:
        # Validate and fetch related objects
        company_instance = Company.objects.get(
            pk=company_id) if Company else None  # Fetch if Company model is available
        accounting_period_instance = AccountingPeriod.objects.get(pk=period_id, company_id=company_id)
    except (Company.DoesNotExist if Company else Exception) as e:  # Adjust exception if Company not imported
        logger.error(f"Failed to fetch Company with ID {company_id} for sequence config: {e}")
        raise ImproperlyConfigured(f"Invalid company_id: {company_id}") from e
    except AccountingPeriod.DoesNotExist as e:
        logger.error(
            f"AccountingPeriod ID {period_id} not found or does not belong to Company ID {company_id}."
        )
        raise ImproperlyConfigured(
            f"Invalid accounting_period_id: {period_id} for Company {company_id}."
        ) from e

    # VoucherSequence.objects should be the CompanyManager if VoucherSequence inherits TenantScopedModel
    # If so, it will use thread-local company context if available.
    # However, explicitly filtering by company_id is safer and clearer in a service.
    sequence_config, created = VoucherSequence.objects.get_or_create(
        company_id=company_id,
        voucher_type=voucher_type_value,
        accounting_period=accounting_period_instance,
        defaults={
            # company_id is already part of the lookup keys, but explicit in defaults is fine
            'prefix': _get_default_prefix(company_instance, voucher_type_value, accounting_period_instance),
            'padding_digits': 4,  # Sensible default
            'last_number': 0,
            # created_by/updated_by could be set here if your model supports it
            # and you have user context available in the service.
        }
    )

    if created:
        logger.info(
            f"Created new VoucherSequence config for Company ID {company_id}, Type '{voucher_type_value}', "
            f"Period '{accounting_period_instance.name}' (ID: {period_id}) with prefix '{sequence_config.prefix}'."
        )
    else:
        logger.debug(
            f"Retrieved existing VoucherSequence config for Company ID {company_id}, Type '{voucher_type_value}', "
            f"Period ID {period_id}."
        )
    return sequence_config


@transaction.atomic  # Crucial for atomicity
def get_next_voucher_number(
        company_id: int,
        voucher_type_value: str,
        period_id: int
) -> str:
    """
    Atomically retrieves, increments, and returns the next formatted voucher number
    for the given company, voucher type, and accounting period ID.

    Uses `select_for_update` to lock the sequence row during the transaction,
    preventing race conditions.

    Args:
        company_id: The ID of the Company.
        voucher_type_value: The voucher type identifier.
        period_id: The ID of the AccountingPeriod.

    Returns:
        The next formatted voucher number string.

    Raises:
        DjangoValidationError: If sequence configuration fails or number generation has issues.
        ValueError: For critical failures during atomic increment.
    """
    logger.debug(
        f"Attempting to get next voucher number for Co ID {company_id}, "
        f"Type '{voucher_type_value}', Period ID {period_id}."
    )
    # Get or create ensures the config row exists.
    # This call itself is not part of the atomic lock for incrementing,
    # but it ensures the row we want to lock exists.
    sequence_config_initial = get_or_create_sequence_config(company_id, voucher_type_value, period_id)

    try:
        # Re-fetch the sequence config *within the atomic transaction* and lock it for update.
        # Ensure VoucherSequence.objects is the correct manager (default or global).
        # If VoucherSequence inherits TenantScopedModel, its default `objects` manager is tenant-aware.
        # For safety, we can explicitly filter by company_id again, though it might be redundant
        # if the manager is already correctly scoping.
        sequence_locked = VoucherSequence.objects.select_for_update().get(
            pk=sequence_config_initial.pk,
            company_id=company_id  # Explicit company filter for safety within transaction
        )

        next_number_val = sequence_locked.last_number + 1
        sequence_locked.last_number = next_number_val
        sequence_locked.save(update_fields=['last_number', 'updated_at'])  # Also update 'updated_at'

        # Format the number using the (potentially updated) prefix and padding from the locked config
        number_str = str(next_number_val).zfill(sequence_locked.padding_digits)
        formatted_number = f"{sequence_locked.prefix}{number_str}"

        logger.info(
            f"Successfully generated next voucher number for Co ID {company_id}, "
            f"Type '{voucher_type_value}', Period ID {period_id}: '{formatted_number}' (Sequence No: {next_number_val})."
        )
        return formatted_number

    except VoucherSequence.DoesNotExist:  # Should not happen if get_or_create worked
        logger.critical(
            f"VoucherSequence (PK: {sequence_config_initial.pk}) disappeared during atomic lock for "
            f"Co ID {company_id}, Type '{voucher_type_value}', Period ID {period_id}."
        )
        raise DjangoValidationError(
            _("Sequence configuration was lost during number generation. Please try again.")
        )
    except IntegrityError as ie:  # e.g., if unique constraint fails on save (unlikely here)
        logger.exception(
            f"Database integrity error during atomic sequence update for Co ID {company_id}, "
            f"Type '{voucher_type_value}', Period ID {period_id}."
        )
        raise DjangoValidationError(
            _("A database error occurred while updating sequence number. %(error)s") % {'error': str(ie)}
        )
    except Exception as e:  # Catch other unexpected errors
        logger.exception(
            f"Unexpected failure to get next voucher number atomically for Co ID {company_id}, "
            f"Type '{voucher_type_value}', Period ID {period_id}."
        )
        # Re-raise as a more generic error to the caller, ensuring transaction rollback.
        raise ValueError(
            f"Failed to generate next voucher number for {voucher_type_value} due to an internal error."
        ) from e
# import logging
# import math
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ImproperlyConfigured
#
# # Use relative imports if services are in the same app level
# from ..models.journal import VoucherSequence # Import the model
# from ..models.period import AccountingPeriod
# # from crp_core.enums import VoucherType # Only needed if type checking VoucherType
#
# logger = logging.getLogger(__name__)
#
# def _calculate_quarter(date_obj):
#     """Calculates the fiscal quarter (1-4) for a given date."""
#     if not date_obj:
#         return "NODATE" # Handle cases where period might lack a start date initially
#     return math.ceil(date_obj.month / 3.0)
#
# def _get_default_prefix(voucher_type: str, period: AccountingPeriod) -> str:
#     """
#     Generates a sensible default prefix for a voucher sequence.
#     Example: JV-2024Q1-
#     """
#     if not period or not period.start_date:
#          logger.warning("Cannot generate default prefix: AccountingPeriod or start_date missing.")
#          # Return a generic prefix or raise error, depending on policy
#          return f"{voucher_type[:2].upper()}-DEF-"
#
#     # Correctly calculate quarter
#     quarter = _calculate_quarter(period.start_date)
#     # Format: Prefix-YearQ#- e.g., JV-2024Q1-
#     period_code = period.start_date.strftime('%Y') + f"Q{quarter}"
#     return f"{voucher_type[:2].upper()}-{period_code}-"
#
# def get_or_create_sequence_config(voucher_type: str, period: AccountingPeriod) -> VoucherSequence:
#     """
#     Retrieves the VoucherSequence configuration for the given scope,
#     or creates it with default settings if it doesn't exist.
#
#     This function handles getting the configuration row, not the atomic increment.
#
#     Args:
#         voucher_type: The voucher type identifier (e.g., 'GENERAL', 'SALES').
#         period: The AccountingPeriod instance.
#
#     Returns:
#         The VoucherSequence instance for the given scope.
#
#     Raises:
#         ImproperlyConfigured: If period is None.
#         Exception: For database errors during get_or_create.
#     """
#     if not period:
#         # This should ideally be caught earlier, but good to have a check
#         raise ImproperlyConfigured("AccountingPeriod cannot be None when getting/creating sequence config.")
#
#     try:
#         sequence_config, created = VoucherSequence.objects.get_or_create(
#             voucher_type=voucher_type,
#             accounting_period=period,
#             defaults={
#                 'prefix': _get_default_prefix(voucher_type, period),
#                 'padding_digits': 4, # Default padding
#                 'last_number': 0
#             }
#         )
#         if created:
#             logger.info(
#                 "Created new VoucherSequence config for Type=%s, Period=%s with prefix '%s'",
#                 voucher_type, str(period), sequence_config.prefix
#             )
#         return sequence_config
#     except Exception as e:
#         logger.exception(
#             "Database error getting/creating VoucherSequence for Type=%s, Period=%s: %s",
#             voucher_type, str(period), e
#         )
#         # Re-raise the exception to be handled by the calling function
#         raise