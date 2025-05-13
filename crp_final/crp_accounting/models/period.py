# crp_accounting/models/period.py

import logging
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils import timezone # Import timezone

# --- Tenant Scoped Base Model Import ---
from .base import TenantScopedModel # Assuming base.py is in the same 'models' directory

# --- User Model Import for 'closed_by' ---
# Use settings.AUTH_USER_MODEL to be flexible
from django.conf import settings

logger = logging.getLogger(__name__)

# =============================================================================
# Fiscal Year Model (Tenant Scoped)
# =============================================================================
class FiscalYear(TenantScopedModel): # Inherit from TenantScopedModel
    """
    Represents a fiscal (financial) year for a specific company.
    Used to segregate accounting data and control period boundaries.
    """
    # 'id', 'company', 'created_at', 'updated_at' are inherited
    # 'objects' (CompanyManager) and 'unfiltered_objects' are inherited

    name = models.CharField(
        _("Fiscal Year Name"),
        max_length=100,
        # unique=True REMOVED - Uniqueness enforced per company in Meta.unique_together
        help_text=_("Label for the fiscal year (e.g., 2024-2025). Must be unique within the company.")
    )
    start_date = models.DateField(
        _("Start Date"),
        help_text=_("Start date of the fiscal year.")
    )
    end_date = models.DateField(
        _("End Date"),
        help_text=_("End date of the fiscal year.")
    )
    is_active = models.BooleanField(
        _("Is Active"),
        default=False,
        db_index=True, # Good to index if queried often
        help_text=_("Designates if this is the currently active fiscal year for the company. Only one can be active per company.")
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=[("Open", "Open"), ("Locked", "Locked"), ("Closed", "Closed")],
        default="Open",
        help_text=_("Operational status of the fiscal year.")
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, # Use settings.AUTH_USER_MODEL
        verbose_name=_("Closed By"),
        null=True, blank=True,
        on_delete=models.SET_NULL, # Keep record even if user is deleted
        related_name='closed_fiscal_years', # Added related_name
        help_text=_("User who closed the year.")
    )
    closed_at = models.DateTimeField(
        _("Closed At"),
        null=True, blank=True,
        help_text=_("Timestamp when the fiscal year was closed.")
    )

    class Meta:
        # --- Enforce uniqueness within the company ---
        unique_together = ('company', 'name')
        ordering = ['company__name', '-start_date'] # Order by company, then by start date
        verbose_name = _('Fiscal Year')
        verbose_name_plural = _('Fiscal Years')
        indexes = [
            models.Index(fields=['company', 'is_active']), # For quickly finding active year per company
            models.Index(fields=['company', 'start_date', 'end_date']),
        ]


    def __str__(self):
        # Include company name for clarity in admin/logs
        return f"{self.name} ({self.company.name})"

    def clean(self):
        """Custom model validation for FiscalYear within tenant context."""
        super().clean() # Call parent's clean if TenantScopedModel has one

        if self.end_date <= self.start_date:
            raise ValidationError(_("End date must be after the start date."))

        # Validate date overlaps within the SAME company
        overlapping_years = FiscalYear.objects.filter( # Use default manager (scoped)
            company=self.company, # Ensure we only check within this company
            start_date__lt=self.end_date,
            end_date__gt=self.start_date
        ).exclude(pk=self.pk) # Exclude self if updating

        if overlapping_years.exists():
            raise ValidationError(
                _("The dates for this fiscal year overlap with another fiscal year (%(other_year)s) in your company.") %
                {'other_year': ", ".join([str(fy.name) for fy in overlapping_years])}
            )

        # Ensure only one active fiscal year PER COMPANY
        if self.is_active:
            # Use default manager which is CompanyManager (already scoped by 'company' field implicitly)
            # OR be explicit if model doesn't have it set up (but it should with TenantScopedModel)
            active_years_in_company = FiscalYear.objects.filter(
                company=self.company,
                is_active=True
            ).exclude(pk=self.pk)

            if active_years_in_company.exists():
                raise ValidationError(
                    _("Another fiscal year (%(other_name)s) is already active for this company.") %
                    {'other_name': active_years_in_company.first().name}
                )

    @transaction.atomic # Ensure atomic operation
    def activate(self):
        """Activate this year and deactivate all others FOR THE SAME COMPANY."""
        if not self.company_id:
            raise ValueError("Cannot activate a fiscal year without an associated company.")

        # Deactivate other fiscal years within the same company
        FiscalYear.objects.filter(company=self.company).exclude(pk=self.pk).update(is_active=False)

        self.is_active = True
        if self.status != "Closed": # Don't re-open a closed year just by activating
             self.status = "Open"
        self.save(update_fields=['is_active', 'status', 'updated_at']) # Be specific
        logger.info(f"FiscalYear {self.name} (ID: {self.pk}) for Company {self.company.name} activated.")

    def close_year(self, user=None):
        """Closes the year for this company, locking further transactions."""
        if self.status == "Closed":
            logger.info(f"FiscalYear {self.name} (ID: {self.pk}) for Company {self.company.name} is already closed.")
            return # Or raise error?

        # Ensure all periods within this fiscal year are locked
        if self.periods.filter(locked=False).exists():
            raise ValidationError(_("Cannot close fiscal year. All accounting periods within it must be locked first."))

        self.status = "Closed"
        self.is_active = False # A closed year cannot be the active year
        self.closed_by = user
        self.closed_at = timezone.now()
        self.save(update_fields=['status', 'is_active', 'closed_by', 'closed_at', 'updated_at'])
        logger.info(f"FiscalYear {self.name} (ID: {self.pk}) for Company {self.company.name} closed by user {user.pk if user else 'System'}.")


# =============================================================================
# Accounting Period Model (Tenant Scoped)
# =============================================================================
class AccountingPeriod(TenantScopedModel): # Inherit from TenantScopedModel
    """
    Represents an accounting period within a fiscal year FOR A SPECIFIC COMPANY.
    Allows for locking a period to prevent further transactions.
    """
    # 'id', 'company', 'created_at', 'updated_at' are inherited
    # 'objects' (CompanyManager) and 'unfiltered_objects' are inherited

    name = models.CharField(
        _("Period Name"), max_length=100,
        help_text=_("Descriptive name for the period (e.g., January 2024, Q1 2024). Optional, can be auto-generated.")
    )
    start_date = models.DateField(
        _("Start Date"),
        help_text=_("The start date of the accounting period.")
    )
    end_date = models.DateField(
        _("End Date"),
        help_text=_("The end date of the accounting period.")
    )
    fiscal_year = models.ForeignKey(
        FiscalYear, # Refers to the tenant-scoped FiscalYear
        verbose_name=_("Fiscal Year"),
        on_delete=models.CASCADE, # If FiscalYear is deleted, periods go too
        related_name="periods",
        help_text=_("The fiscal year to which this period belongs (must be from the same company).")
    )
    locked = models.BooleanField(
        _("Is Locked"),
        default=False,
        db_index=True,
        help_text=_("Indicates whether the period is locked and no more entries are allowed.")
    )

    class Meta:
        # --- Enforce uniqueness of period (e.g. name or dates) WITHIN the company & fiscal year ---
        # Option 1: Unique name within a fiscal year (and thus company)
        unique_together = ('fiscal_year', 'name') # Assuming name is how you identify periods uniquely
        # Option 2: Unique start_date within a fiscal year (implies unique periods)
        # unique_together = ('fiscal_year', 'start_date')
        ordering = ['fiscal_year__company__name', 'fiscal_year__start_date', 'start_date']
        verbose_name = _('Accounting Period')
        verbose_name_plural = _('Accounting Periods')
        indexes = [
            models.Index(fields=['fiscal_year', 'start_date', 'end_date']), # For date range queries
            models.Index(fields=['fiscal_year', 'locked']),
        ]

    def __str__(self):
        # Include fiscal year and company for clarity
        return f"{self.name} (FY: {self.fiscal_year.name}, Co: {self.company.name}) - {'Locked' if self.locked else 'Open'}"

    def clean(self):
        """Custom model validation for AccountingPeriod."""
        super().clean() # Call parent's clean

        if self.end_date <= self.start_date:
            raise ValidationError(_("Period end date must be after the start date."))

        if not self.fiscal_year_id: # fiscal_year must be set
            raise ValidationError({'fiscal_year': _("Fiscal Year is required.")})

        # --- Ensure Fiscal Year and Period company match (Critical for integrity) ---
        # This check assumes fiscal_year object is loaded. If fiscal_year_id is set,
        # the save method should ensure the FK constraint enforces company match if DB is well-designed.
        # However, adding it here is a good safeguard if fiscal_year object is pre-fetched/set.
        if self.fiscal_year and self.fiscal_year.company_id != self.company_id:
            raise ValidationError(
                _("The Accounting Period's company (%(period_co)s) must match the Fiscal Year's company (%(fy_co)s).") %
                {'period_co': self.company_id, 'fy_co': self.fiscal_year.company_id}
            )

        # Ensure period dates are within the fiscal year's dates
        if self.start_date < self.fiscal_year.start_date or self.end_date > self.fiscal_year.end_date:
            raise ValidationError(
                _("Period dates (%(p_start)s - %(p_end)s) must be within the fiscal year's dates (%(fy_start)s - %(fy_end)s).") %
                {'p_start': self.start_date, 'p_end': self.end_date, 'fy_start': self.fiscal_year.start_date, 'fy_end': self.fiscal_year.end_date}
            )

        # Validate date overlaps for periods within the SAME fiscal year (and thus same company)
        overlapping_periods = AccountingPeriod.objects.filter(
            fiscal_year=self.fiscal_year, # Scoped to the same fiscal year (implicitly same company)
            start_date__lt=self.end_date,
            end_date__gt=self.start_date
        ).exclude(pk=self.pk)

        if overlapping_periods.exists():
            raise ValidationError(
                 _("The dates for this accounting period overlap with another period (%(other_period)s) in the same fiscal year.") %
                 {'other_period': ", ".join([str(p.name) for p in overlapping_periods])}
            )

    def save(self, *args, **kwargs):
        """Ensure company of period matches company of fiscal year before saving."""
        if self.fiscal_year and not self.company_id:
            # If company isn't set on period, but fiscal_year is, infer it.
            # This is crucial when TenantScopedModel sets company in perform_create AFTER full_clean.
            self.company = self.fiscal_year.company
        elif self.fiscal_year and self.fiscal_year.company_id != self.company_id:
            # This should ideally be caught by clean(), but as a final safeguard.
            raise ValidationError(_("Mismatch between Accounting Period's company and Fiscal Year's company."))

        if not self.name: # Auto-generate name if not provided
            self.name = f"{self.start_date.strftime('%B %Y')}" # Example: "January 2024"

        super().save(*args, **kwargs) # Call TenantScopedModel's save after company is set

    def lock_period(self):
        """Locks the accounting period."""
        if self.locked:
            logger.info(f"Period {self.name} for Company {self.company.name} is already locked.")
            return # Or raise ValidationError("This period is already locked.")
        self.locked = True
        self.save(update_fields=['locked', 'updated_at'])
        logger.info(f"Period {self.name} for Company {self.company.name} locked.")

    def unlock_period(self):
        """Unlocks the accounting period if the fiscal year is not closed."""
        if not self.locked:
            logger.info(f"Period {self.name} for Company {self.company.name} is already open.")
            return # Or raise ValidationError("This period is already open.")
        if self.fiscal_year.status == "Closed":
            raise ValidationError(_("Cannot unlock period. The fiscal year '%(fy_name)s' is closed.") % {'fy_name': self.fiscal_year.name})

        self.locked = False
        self.save(update_fields=['locked', 'updated_at'])
        logger.info(f"Period {self.name} for Company {self.company.name} unlocked.")
# from django.db import models
# from django.utils import timezone
# from django.core.exceptions import ValidationError
# from django.utils.translation import gettext_lazy as _
#
#
# class FiscalYear(models.Model):
#     """
#     Represents a fiscal (financial) year for the organization.
#     Used to segregate accounting data and control period boundaries.
#     """
#
#     name = models.CharField(max_length=100, unique=True, help_text=_("Label for the fiscal year (e.g., 2024-2025)"))
#     start_date = models.DateField(help_text=_("Start date of the fiscal year."))
#     end_date = models.DateField(help_text=_("End date of the fiscal year."))
#     is_active = models.BooleanField(default=False, help_text=_("Only one fiscal year can be active at a time."))
#     status = models.CharField(
#         max_length=20,
#         choices=[("Open", "Open"), ("Locked", "Locked"), ("Closed", "Closed")],
#         default="Open",
#         help_text=_("Operational status of the fiscal year.")
#     )
#     closed_by = models.ForeignKey(
#         'accounts.User', null=True, blank=True, on_delete=models.SET_NULL,
#         help_text=_("User who closed the year.")
#     )
#     closed_at = models.DateTimeField(null=True, blank=True, help_text=_("Timestamp when the fiscal year was closed."))
#
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)
#
#     class Meta:
#         ordering = ['-start_date']
#         verbose_name = _('Fiscal Year')
#         verbose_name_plural = _('Fiscal Years')
#
#     def __str__(self):
#         return self.name
#
#     def clean(self):
#         if self.end_date <= self.start_date:
#             raise ValidationError(_("End date must be after the start date."))
#         if self.is_active:
#             # Ensure only one active year
#             if FiscalYear.objects.exclude(pk=self.pk).filter(is_active=True).exists():
#                 raise ValidationError(_("Another fiscal year is already active."))
#
#     def activate(self):
#         """Activate this year and deactivate all others."""
#         FiscalYear.objects.exclude(pk=self.pk).update(is_active=False)
#         self.is_active = True
#         self.status = "Open"
#         self.save()
#
#     def close_year(self, user=None):
#         """Closes the year, locking further transactions."""
#         self.status = "Closed"
#         self.closed_by = user
#         self.closed_at = timezone.now()
#         self.save()
#
#
#
# class AccountingPeriod(models.Model):
#     """
#     Model to represent an accounting period within a fiscal year.
#     This allows for locking a period to prevent any further transactions
#     after it has been closed.
#
#     Attributes:
#         - `start_date`: The start date of the accounting period.
#         - `end_date`: The end date of the accounting period.
#         - `fiscal_year`: The fiscal year this period belongs to.
#         - `locked`: Boolean flag to indicate if this period is closed and no more entries are allowed.
#     """
#
#
#     start_date = models.DateField(help_text=_("The start date of the accounting period."))
#     end_date = models.DateField(help_text=_("The end date of the accounting period."))
#     fiscal_year = models.ForeignKey(
#         'FiscalYear', on_delete=models.CASCADE, related_name="periods",
#         help_text=_("The fiscal year to which this period belongs.")
#     )
#     locked = models.BooleanField(default=False, help_text=_("Indicates whether the period is locked and no more entries are allowed."))
#
#     def __str__(self):
#         """
#         String representation of the AccountingPeriod model.
#         Returns a string indicating the period's start and end date.
#         """
#         return f"Period {self.start_date} to {self.end_date} ({'Locked' if self.locked else 'Open'})"
#
#     def lock_period(self):
#         """
#         Locks the accounting period to prevent further journal entries.
#         """
#         if self.locked:
#             raise ValidationError(_("This period is already locked."))
#         self.locked = True
#         self.save()
#
#     def unlock_period(self):
#         """
#         Unlocks the accounting period to allow further journal entries.
#         """
#         if not self.locked:
#             raise ValidationError(_("This period is already open."))
#         self.locked = False
#         self.save()
#
#     class Meta:
#         """
#         Meta options for the AccountingPeriod model.
#         """
#         verbose_name = _('Accounting Period')
#         verbose_name_plural = _('Accounting Periods')
