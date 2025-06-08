# crp_accounting/models/journal.py (or wherever VoucherSequence is defined)

import logging
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

# --- Base Model Import ---
try:
    from .base import TenantScopedModel
except ImportError:
    raise ImportError("Could not import TenantScopedModel from .base.")

# --- Related Model Imports (Assume tenant-scoped) ---
try:
    from .period import AccountingPeriod
    # Assuming VoucherType enum is defined/imported correctly
    from crp_core.enums import VoucherType
except ImportError as e:
    raise ImportError(f"Could not import related models/enums for VoucherSequence: {e}")

logger = logging.getLogger(__name__)


# --- Inherit from TenantScopedModel ---
class VoucherSequence(TenantScopedModel):
    """
    Manages the next available voucher number for a specific scope within a company.
    Scope is defined by Company, Voucher Type, and Accounting Period.
    """
    # 'company' field inherited from TenantScopedModel

    # Renamed back to voucher_type for consistency
    voucher_type = models.CharField(
        max_length=20,
        choices=VoucherType.choices,
        db_index=True, # Add index
        help_text=_("The type of voucher this sequence is for.")
    )
    accounting_period = models.ForeignKey(
        AccountingPeriod,
        on_delete=models.CASCADE, # If period deleted, sequence is invalid
        db_index=True, # Add index
        help_text=_("The accounting period this sequence applies to (must belong to same company).")
    )
    prefix = models.CharField(
        max_length=20, # Increased length slightly
        blank=True,
        help_text=_("Optional prefix for the voucher number (e.g., 'JV-24Q1-').")
    )
    padding_digits = models.PositiveSmallIntegerField(
        default=4,
        help_text=_("Number of digits for padding (e.g., 4 means 0001).")
    )
    last_number = models.PositiveIntegerField(
        default=0,
        help_text=_("The last number used in this sequence.")
    )

    # 'created_at', 'updated_at' inherited from TenantScopedModel
    # 'objects' manager inherited (CompanyManager)

    class Meta:
        verbose_name = _("Voucher Sequence")
        verbose_name_plural = _("Voucher Sequences")
        # Ensure unique combination within the company
        unique_together = ('company', 'voucher_type', 'accounting_period')
        # Update ordering to include company
        ordering = ['company__name', 'accounting_period__start_date', 'voucher_type']
        indexes = [
             # Index for the unique_together constraint
             models.Index(fields=['company', 'voucher_type', 'accounting_period']),
        ]

    def __str__(self):
        period_display = str(self.accounting_period) if self.accounting_period else 'N/A'
        # Optionally include company ID for admin clarity
        # return f"{self.get_voucher_type_display()} sequence for {period_display} (Co: {self.company_id})"
        return f"{self.get_voucher_type_display()} sequence for {period_display}"

    def clean(self):
        """Validate that the accounting period belongs to the same company."""
        super().clean() # Call parent clean method if it exists
        if self.accounting_period and hasattr(self, 'company') and self.company_id:
            # Check if accounting_period has company_id populated and matches
            if hasattr(self.accounting_period, 'company_id') and self.accounting_period.company_id != self.company_id:
                 raise ValidationError({
                     'accounting_period': _("Accounting Period must belong to the same company as the Voucher Sequence.")
                 })
        elif not self.accounting_period_id:
             raise ValidationError({'accounting_period': _("Accounting Period is required.")})


    # Renamed back to format_number for consistency with previous service layer code
    def format_number(self, number: int) -> str:
        """Formats the given number according to prefix and padding."""
        if not isinstance(number, int) or number <= 0:
             # Handle invalid input number gracefully
             logger.error(f"Invalid number '{number}' passed to format_number for Sequence PK {self.pk}")
             number = 0 # Or raise error? Defaulting to 0 for formatting.

        number_str = str(number).zfill(self.padding_digits)
        return f"{self.prefix}{number_str}"
# from django.db import models
# from django.utils.translation import gettext_lazy as _
# from .period import AccountingPeriod # Import your AccountingPeriod model
# from .journal import VoucherType # Assuming VoucherType enum/choices are defined here or imported
#
# class VoucherSequence(models.Model):
#     """
#     Manages the next available voucher number for a specific scope.
#     Scope is typically defined by Journal Type and Accounting Period.
#     """
#     journal_type = models.CharField(
#         max_length=20,
#         choices=VoucherType.choices,
#         help_text=_("The type of journal this sequence is for.")
#     )
#     accounting_period = models.ForeignKey(
#         AccountingPeriod,
#         on_delete=models.CASCADE, # If period deleted, sequence makes no sense
#         help_text=_("The accounting period this sequence applies to.")
#     )
#     prefix = models.CharField(
#         max_length=10,
#         blank=True,
#         help_text=_("Prefix for the voucher number (e.g., 'JV-').")
#     )
#     padding_digits = models.PositiveSmallIntegerField(
#         default=4,
#         help_text=_("Number of digits for padding (e.g., 4 means 0001, 0010, 0100).")
#     )
#     last_number = models.PositiveIntegerField(
#         default=0,
#         help_text=_("The last number used in this sequence.")
#     )
#
#     class Meta:
#         verbose_name = _("Voucher Sequence")
#         verbose_name_plural = _("Voucher Sequences")
#         # Ensure only one sequence exists per type/period combination
#         unique_together = ('journal_type', 'accounting_period')
#         ordering = ['accounting_period', 'journal_type']
#
#     def __str__(self):
#         return f"{self.get_journal_type_display()} sequence for {self.accounting_period}"
#
#     def get_next_formatted_number(self, current_number: int) -> str:
#         """Formats the next number according to prefix and padding."""
#         number_str = str(current_number).zfill(self.padding_digits)
#         return f"{self.prefix}{number_str}"