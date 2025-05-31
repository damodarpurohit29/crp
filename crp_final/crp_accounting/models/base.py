# crp_accounting/models/base.py

import uuid
import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError, PermissionDenied
from django.conf import settings

from safedelete.models import SafeDeleteModel
from safedelete.managers import SafeDeleteManager, SafeDeleteAllManager, SafeDeleteDeletedManager
from safedelete import SOFT_DELETE_CASCADE
from simple_history.models import HistoricalRecords

# Attempt to import dependencies from 'company' app, with error handling for fail-safe logging
try:
    from company.models import Company, CompanyMembership
    from company.managers import CompanyManager as OriginalCompanyManager
    from company.managers import UnfilteredCompanyManager as OriginalUnfilteredCompanyManager
    from company.utils import get_current_company
except ImportError as e:
    logger_init = logging.getLogger(f"{__name__}.initialization")
    logger_init.critical(f"CRITICAL: Could not import from 'company' app: {e}. Accounting models will fail.")
    raise ImportError(f"TenantScopedModel: Could not import dependencies from 'company' app: {e}") from e

# Attempt to import currency enumeration (optional feature)
try:
    from crp_core.enums import CurrencyType
except ImportError as e:
    logger_init = logging.getLogger(f"{__name__}.initialization")
    logger_init.warning(f"WARNING: Could not import CurrencyType from 'crp_core.enums': {e}.")
    CurrencyType = None

logger = logging.getLogger(__name__)

# ============================================================================
# Custom Manager Combinations (Scoped by Company)
# ============================================================================

class TenantSafeDeleteManager(SafeDeleteManager, OriginalCompanyManager):
    """Manager with soft delete and tenant (company) scoping."""
    pass

class UnfilteredTenantSafeDeleteManager(SafeDeleteManager, OriginalUnfilteredCompanyManager):
    """Unfiltered soft delete manager with tenant scoping."""
    pass

class TenantDeletedManager(SafeDeleteDeletedManager, OriginalCompanyManager):
    """Manager to access only deleted objects scoped by tenant."""
    pass

class UnfilteredTenantDeletedManager(SafeDeleteDeletedManager, OriginalUnfilteredCompanyManager):
    """Unfiltered access to deleted objects for all tenants."""
    pass

class TenantAllIncludingDeletedManager(SafeDeleteAllManager, OriginalCompanyManager):
    """Access all objects (deleted or not) scoped by tenant."""
    pass

class UnfilteredTenantAllIncludingDeletedManager(SafeDeleteAllManager, OriginalUnfilteredCompanyManager):
    """Unfiltered access to all objects, including deleted, for all tenants."""
    pass

# ============================================================================
# Abstract Base Model with Tenant Scoping and Soft Delete
# ============================================================================

class TenantScopedModel(SafeDeleteModel):
    """
    Abstract base model that includes:
    - Soft deletion support
    - Tenant scoping via a 'company' foreign key
    - Audit fields (created/updated timestamps and users)
    - Historical tracking
    """
    _safedelete_policy = SOFT_DELETE_CASCADE

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False, verbose_name=_("ID")
    )
    company = models.ForeignKey(
        Company, verbose_name=_("Company"), on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_related', db_index=True,
        help_text=_("The company this record belongs to.")
    )
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Created By"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_%(app_label)s_%(class)s_set', editable=False
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Last Updated By"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='updated_%(app_label)s_%(class)s_set', editable=False
    )

    # Managers for various access scopes
    objects = TenantSafeDeleteManager()
    global_objects = UnfilteredTenantSafeDeleteManager()
    deleted_objects = TenantDeletedManager()
    all_objects_including_deleted = TenantAllIncludingDeletedManager()
    global_deleted_objects = UnfilteredTenantDeletedManager()
    global_all_objects_including_deleted = UnfilteredTenantAllIncludingDeletedManager()

    # Historical audit tracking
    history = HistoricalRecords(inherit=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['company', 'created_at'], name='%(app_label)s_%(class)s_co_created_idx'),
            models.Index(fields=['company', 'updated_at'], name='%(app_label)s_%(class)s_co_updated_idx'),
        ]

    def save(self, *args, **kwargs):
        """
        Override save to enforce company scoping and validation rules.
        Ensures:
        - Company is auto-filled from context if missing
        - Only allows save for active companies
        - Performs full_clean unless update_fields is used
        """
        is_new = not self.pk
        current_company_from_context = None

        if is_new and not self.company_id:
            current_company_from_context = get_current_company()
            if current_company_from_context:
                self.company = current_company_from_context
            elif not getattr(self, '_allow_missing_company_on_save', False):
                raise ValueError(
                    f"Cannot save new {self.__class__.__name__}: 'company' is required and no company context found."
                )

        # Validate company is active
        if self.company_id:
            company_instance_for_check = (
                self.company if hasattr(self, 'company') and self.company_id == self.company.pk
                else (current_company_from_context if is_new and current_company_from_context else None)
            )
            if not company_instance_for_check and self.company_id:
                try:
                    company_instance_for_check = Company.objects.get(pk=self.company_id)
                except Company.DoesNotExist:
                    raise ValidationError({'company': _("Invalid company reference.")})

            if company_instance_for_check and not company_instance_for_check.effective_is_active:
                raise ValidationError({
                    'company': _("Operations cannot be performed for an inactive or suspended company: %(company_name)s") %
                               {'company_name': company_instance_for_check.name}
                })

        # Remove force_soft from kwargs if present (bug workaround)
        kwargs_for_super_save = kwargs.copy()
        kwargs_for_super_save.pop('force_soft', None)

        if not kwargs_for_super_save.get('update_fields'):
            excluded_fields = []
            if is_new and self.company_id:
                excluded_fields.append('company')

            if hasattr(self, '_set_derived_fields') and callable(self._set_derived_fields):
                self._set_derived_fields()

            self.full_clean(exclude=excluded_fields or None)

        super().save(*args, **kwargs_for_super_save)

    def clean(self):
        """Base model clean logic; extend in child models."""
        super().clean()

    @classmethod
    def create_for_company(cls, company: Company, created_by_user: settings.AUTH_USER_MODEL, **kwargs):
        """
        Class helper to create a model instance for a company with audit fields.
        Ensures:
        - Valid active company
        - Valid user
        - Audit fields set
        """
        if not isinstance(company, Company):
            raise TypeError("A valid Company instance must be provided.")
        if not company.effective_is_active:
            raise PermissionDenied(f"Cannot create {cls.__name__} records for inactive company: {company.name}")
        if not isinstance(created_by_user, get_user_model()):
            raise TypeError("A valid User instance must be provided for 'created_by_user'.")

        # Clean kwargs
        for field in ['company', 'company_id', 'created_by', 'created_by_id', 'updated_by', 'updated_by_id']:
            kwargs.pop(field, None)

        instance = cls(company=company, created_by=created_by_user, updated_by=created_by_user, **kwargs)
        instance.save()
        logger.info(f"Created new {cls.__name__} (ID: {instance.pk}) for Company '{company.name}' by {created_by_user.name}.")
        return instance

    def can_be_edited_by_user(self, user) -> bool:
        """
        Checks whether the given user has edit permission for this record.
        Permissions:
        - Superusers can always edit
        - Active company membership required
        """
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        if not CompanyMembership:
            logger.error("Permission check failed: CompanyMembership model not available.")
            return False
        try:
            return CompanyMembership.objects.filter(
                user=user, company_id=self.company_id, is_active_membership=True
            ).exists()
        except Exception as e:
            logger.error(f"Error checking edit permission for {self.pk} by {user.name}: {e}")
            return False

    def __str__(self):
        """
        String representation of the model instance, trying to show a name-like field and company context.
        """
        name_attrs = ['name', 'title', 'account_name', 'voucher_number', 'bill_number', 'payment_number', 'prefix']
        display_name = None
        for attr in name_attrs:
            if hasattr(self, attr) and getattr(self, attr):
                display_name = str(getattr(self, attr))
                break
        company_prefix = "N/A Co"
        if self.company_id:
            if hasattr(self, 'company') and self.company and self.company.pk == self.company_id:
                company_prefix = self.company.subdomain_prefix or str(self.company_id)
            else:
                try:
                    company_obj = Company.objects.only('subdomain_prefix').get(pk=self.company_id)
                    company_prefix = company_obj.subdomain_prefix or str(self.company_id)
                except Company.DoesNotExist:
                    company_prefix = f"InvalidCo:{self.company_id}"
        if display_name:
            return f"{display_name} (Co: {company_prefix})"
        return f"{self.__class__.__name__} (ID: {self.pk}, Co: {company_prefix})"

# ============================================================================
# ExchangeRate Model
# ============================================================================

class ExchangeRate(models.Model):
    """
    Stores currency exchange rates optionally scoped by company.
    Includes:
    - From/To currency codes
    - Effective date
    - Conversion rate
    - Optional source and audit fields
    """
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name='exchange_rates',
        verbose_name=_("Company (Optional)"), null=True, blank=True, db_index=True,
        help_text=_("Leave blank for global rate, or select company for tenant-specific rate.")
    )
    from_currency = models.CharField(
        _("From Currency"), max_length=10,
        choices=CurrencyType.choices if CurrencyType else [], db_index=True,
        help_text=_("The currency code to convert from (e.g., USD).")
    )
    to_currency = models.CharField(
        _("To Currency"), max_length=10,
        choices=CurrencyType.choices if CurrencyType else [], db_index=True,
        help_text=_("The currency code to convert to (e.g., INR).")
    )
    date = models.DateField(
        _("Effective Date"), db_index=True,
        help_text=_("The date this exchange rate is effective from (inclusive).")
    )
    rate = models.DecimalField(
        _("Exchange Rate"), max_digits=20, decimal_places=10,
        help_text=_("1 unit of 'From Currency' = 'Rate' units of 'To Currency'.")
    )
    source = models.CharField(
        _("Rate Source (Optional)"), max_length=100, blank=True, null=True,
        help_text=_("Optional: Source of this exchange rate.")
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    history = HistoricalRecords(inherit=True)

    class Meta:
        verbose_name = _("Exchange Rate")
        verbose_name_plural = _("Exchange Rates")
        unique_together = ('company', 'from_currency', 'to_currency', 'date')
        ordering = ['company', 'from_currency', 'to_currency', '-date']

    def __str__(self):
        """
        Human-readable string representation for logging/debug.
        """
        company_str = f"Co: {self.company.name} " if self.company else "Global "
        return (f"{company_str}{self.from_currency} to {self.to_currency} "
                f"on {self.date.strftime('%Y-%m-%d')} = {self.rate:.6f}")

    def clean(self):
        """
        Validation logic:
        - From and To currencies must differ
        - Rate must be positive
        - Company must be active if specified
        """
        super().clean()
        if self.from_currency == self.to_currency:
            raise ValidationError(_("From Currency and To Currency cannot be the same."))
        if self.rate <= Decimal('0'):
            raise ValidationError(_("Exchange rate must be positive."))
        if self.company and not self.company.effective_is_active:
            raise ValidationError(
                _("Cannot set exchange rates for an inactive or suspended company: %(company_name)s") %
                {'company_name': self.company.name}
            )

# # crp_accounting/models/base.py
# import uuid
# import logging
# from django.db import models
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ValidationError, PermissionDenied
# from django.conf import settings  # For settings.AUTH_USER_MODEL
# 
# # --- Library Imports ---
# from safedelete.models import SafeDeleteModel
# from safedelete.managers import SafeDeleteManager, SafeDeleteAllManager, SafeDeleteDeletedManager
# from safedelete import SOFT_DELETE_CASCADE
# from simple_history.models import HistoricalRecords
# 
# # --- Dependency Imports from 'company' app ---
# try:
#     from company.models import Company, CompanyMembership
#     from company.managers import CompanyManager as OriginalCompanyManager
#     from company.managers import UnfilteredCompanyManager as OriginalUnfilteredCompanyManager
#     from company.utils import get_current_company
# except ImportError as e:
#     logger_init = logging.getLogger(__name__)
#     logger_init.critical(f"CRITICAL: Could not import from 'company' app: {e}. Accounting models will fail.")
#     # Re-raise as ImportError which is more standard for dependency failures
#     raise ImportError(f"TenantScopedModel: Could not import dependencies from 'company' app: {e}") from e
# 
# # --- Enum Imports (Ensure this path is correct for your CurrencyType) ---
# try:
#     from crp_core.enums import CurrencyType
# except ImportError as e:
#     logger_init = logging.getLogger(__name__)
#     logger_init.critical(
#         f"CRITICAL: Could not import CurrencyType from 'crp_core.enums': {e}. ExchangeRate model will fail.")
#     # If CurrencyType is critical for ExchangeRate, raise error or define a dummy
#     CurrencyType = None  # Allow module to load, but ExchangeRate will be problematic
#     # raise ImportError(f"TenantScopedModel: Could not import CurrencyType from 'crp_core.enums': {e}") from e
# 
# logger = logging.getLogger(__name__)
# 
# 
# # --- Combined Managers: Safedelete Variants + Company Scoping ---
# # ... (All your TenantSafeDeleteManager, UnfilteredTenantSafeDeleteManager, etc. remain here, unchanged) ...
# class TenantSafeDeleteManager(SafeDeleteManager, OriginalCompanyManager):
#     """ Default: Scoped by company, respects soft-delete (shows only non-deleted). """
#     pass
# 
# 
# class UnfilteredTenantSafeDeleteManager(SafeDeleteManager, OriginalUnfilteredCompanyManager):
#     """ Unfiltered by company, respects soft-delete (shows only non-deleted from all companies). """
#     pass
# 
# 
# class TenantDeletedManager(SafeDeleteDeletedManager, OriginalCompanyManager):
#     """ Scoped by company, shows ONLY soft-deleted records. """
#     pass
# 
# 
# class UnfilteredTenantDeletedManager(SafeDeleteDeletedManager, OriginalUnfilteredCompanyManager):
#     """ Unfiltered by company, shows ONLY soft-deleted records from all companies. """
#     pass
# 
# 
# class TenantAllIncludingDeletedManager(SafeDeleteAllManager, OriginalCompanyManager):
#     """ Scoped by company, shows ALL records (including soft-deleted). """
#     pass
# 
# 
# class UnfilteredTenantAllIncludingDeletedManager(SafeDeleteAllManager, OriginalUnfilteredCompanyManager):
#     """ Unfiltered by company, shows ALL records (including soft-deleted) from all companies. """
#     pass
# 
# 
# class TenantScopedModel(SafeDeleteModel):
#     _safedelete_policy = SOFT_DELETE_CASCADE
# 
#     id = models.UUIDField(
#         primary_key=True, default=uuid.uuid4, editable=False, verbose_name=_("ID")
#     )
#     company = models.ForeignKey(
#         Company,
#         verbose_name=_("Company"),
#         on_delete=models.PROTECT,  # Protect company from deletion if related objects exist
#         related_name='%(app_label)s_%(class)s_related',  # More specific related_name
#         db_index=True,
#         # editable=False,  # Should be set programmatically
#         help_text=_("The company this record belongs to.")
#     )
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#     created_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         verbose_name=_("Created By"),
#         on_delete=models.SET_NULL,
#         null=True, blank=True,  # Allow system creation or if user is deleted
#         related_name='created_%(app_label)s_%(class)s_set',  # Unique related name
#         editable=False
#     )
#     updated_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         verbose_name=_("Last Updated By"),
#         on_delete=models.SET_NULL,
#         null=True, blank=True,
#         related_name='updated_%(app_label)s_%(class)s_set',  # Unique related name
#         editable=False
#     )
# 
#     # --- Managers ---
#     objects = TenantSafeDeleteManager()
#     global_objects = UnfilteredTenantSafeDeleteManager()
#     deleted_objects = TenantDeletedManager()
#     all_objects_including_deleted = TenantAllIncludingDeletedManager()
#     global_deleted_objects = UnfilteredTenantDeletedManager()
#     global_all_objects_including_deleted = UnfilteredTenantAllIncludingDeletedManager()
# 
#     history = HistoricalRecords(inherit=True)
# 
#     class Meta:
#         abstract = True
#         indexes = [
#             models.Index(fields=['company', 'created_at']),  # Example useful index
#         ]
# 
#     def save(self, *args, **kwargs):
#         is_new = not self.pk
#         current_company_from_context = None
# 
#         if is_new and not self.company_id:
#             current_company_from_context = get_current_company()
#             if current_company_from_context:
#                 self.company = current_company_from_context
#             elif not getattr(self, '_allow_missing_company_on_save',
#                              False):  # Allow override for specific models if needed
#                 raise ValueError(
#                     f"Cannot save new {self.__class__.__name__}: 'company' is required and no company context found, "
#                     "and model does not allow missing company on save."
#                 )
# 
#         # Validate company activity before saving, if company is set
#         if self.company_id:  # Check if company is set (either newly or previously)
#             # Efficiently check company status without fetching full object if not needed
#             # This relies on `company` attribute being populated or `company_id` being valid
#             company_instance_for_check = self.company if self.pk else current_company_from_context  # Use context company if new
#             if company_instance_for_check and not company_instance_for_check.effective_is_active:
#                 raise ValidationError({
#                     'company': _(
#                         "Operations cannot be performed for an inactive or suspended company: %(company_name)s") %
#                                {'company_name': company_instance_for_check.name}
#                 })
# 
#         # Perform full_clean only if update_fields is not used, to allow partial updates to bypass it.
#         if not kwargs.get('update_fields'):
#             # Exclude 'company' from full_clean if it was just set programmatically for a new instance.
#             # This assumes 'company' field is not meant to be part of user-submitted form validation here.
#             excluded_fields = []
#             if is_new and self.company_id:
#                 excluded_fields.append('company')
#             self.full_clean(exclude=excluded_fields or None)
# 
#         super().save(*args, **kwargs)
# 
#     def clean(self):
#         super().clean()
#         # The company activity check is now primarily in save() to ensure company is set first.
#         # Additional clean logic specific to derived models can go in their own clean() methods.
# 
#     @classmethod
#     def create_for_company(cls, company: Company, **kwargs):
#         """
#         Helper method to create an instance of this model, ensuring it's associated
#         with the provided active company and that company context is bypassed.
#         """
#         if not isinstance(company, Company):
#             raise TypeError("A valid Company instance must be provided.")
#         if not company.effective_is_active:
#             raise PermissionDenied(
#                 f"Cannot create {cls.__name__} records for inactive/suspended company: {company.name}")
# 
#         # Remove company/company_id from kwargs if present, as we're setting it explicitly
#         kwargs.pop('company', None)
#         kwargs.pop('company_id', None)
# 
#         # For models that might set company based on context, temporarily clear it
#         # to ensure our explicit company is used.
#         # This part is tricky if get_current_company() is used deep in model init/save.
#         # The most robust way is to ensure save() prioritizes explicitly passed company or self.company.
# 
#         instance = cls(company=company, **kwargs)
#         instance.save()  # save() method will handle full_clean and other logic
#         logger.info(f"Created new {cls.__name__} (ID: {instance.pk}) for Company '{company.name}'.")
#         return instance
# 
#     def can_be_edited_by_user(self, user) -> bool:  # Renamed for clarity
#         """
#         Checks if a given user has rights to edit this specific instance.
#         Basic check: superuser or active member of the instance's company.
#         More granular role-based permissions should be handled by dedicated permission classes or services.
#         """
#         if not user or not user.is_authenticated: return False
#         if user.is_superuser: return True  # Superusers can edit anything
# 
#         # Ensure CompanyMembership model is available
#         if not CompanyMembership:
#             logger.error(
#                 f"Cannot check edit permission for {self.__class__.__name__} {self.pk}: CompanyMembership model not available.")
#             return False  # Fail safe
# 
#         # Check if the user is an active member of the company owning this instance
#         try:
#             is_member = CompanyMembership.objects.filter(
#                 user=user,
#                 company_id=self.company_id,  # Use company_id for direct FK check
#                 is_active_membership=True
#             ).exists()
#             if not is_member:
#                 logger.debug(
#                     f"User {user.name} cannot edit {self.__class__.__name__} {self.pk}: Not an active member of Company {self.company_id}.")
#             return is_member
#         except Exception as e:
#             logger.error(
#                 f"Error checking edit permission for {self.__class__.__name__} {self.pk} by user {user.name}: {e}")
#             return False  # Fail safe
# 
#     def __str__(self):
#         # Attempt to get a 'name' or 'title' attribute for a more descriptive string
#         name_attr = getattr(self, 'name', None) or \
#                     getattr(self, 'title', None) or \
#                     getattr(self, 'account_name', None) or \
#                     getattr(self, 'voucher_number', None)
# 
#         if name_attr:
#             return f"{name_attr} (Co: {self.company.subdomain_prefix if self.company_id and self.company else 'N/A'})"
#         return f"{self.__class__.__name__} (ID: {self.pk}, Co: {self.company.subdomain_prefix if self.company_id and self.company else 'N/A'})"
# 
# 
# # =============================================================================
# # ExchangeRate Model (Tenant-Aware or Global)
# # =============================================================================
# class ExchangeRate(models.Model):  # Does not inherit TenantScopedModel if rates can be global
#     """
#     Stores exchange rates between currencies, effective from a specific date.
#     Can be company-specific or global (if 'company' is Null).
#     """
#     # Allow company to be Null for global rates shared across all tenants
#     company = models.ForeignKey(
#         Company,
#         on_delete=models.CASCADE,  # If a company is deleted, its specific rates are gone
#         related_name='exchange_rates',
#         verbose_name=_("Company (Optional)"),
#         null=True,
#         blank=True,
#         db_index=True,
#         help_text=_("Leave blank for a global rate, or select a company for tenant-specific rates.")
#     )
#     from_currency = models.CharField(
#         _("From Currency"),
#         max_length=10,
#         choices=CurrencyType.choices if CurrencyType else [],  # Use choices if CurrencyType loaded
#         db_index=True,
#         help_text=_("The currency code to convert from (e.g., USD).")
#     )
#     to_currency = models.CharField(
#         _("To Currency"),
#         max_length=10,
#         choices=CurrencyType.choices if CurrencyType else [],
#         db_index=True,
#         help_text=_("The currency code to convert to (e.g., INR).")
#     )
#     date = models.DateField(
#         _("Effective Date"),
#         db_index=True,
#         help_text=_("The date this exchange rate is effective from (inclusive).")
#     )
#     rate = models.DecimalField(
#         _("Exchange Rate"),
#         max_digits=20,  # Increased precision for rates
#         decimal_places=10,  # Store with high precision
#         help_text=_("The rate to multiply by: 1 unit of 'From Currency' = 'Rate' units of 'To Currency'.")
#     )
#     source = models.CharField(
#         _("Rate Source (Optional)"),
#         max_length=100,
#         blank=True, null=True,
#         help_text=_("Optional: Source of this exchange rate (e.g., 'Central Bank', 'API Provider').")
#     )
#     created_at = models.DateTimeField(auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(auto_now=True, editable=False)
# 
#     # If you want history for exchange rates too
#     history = HistoricalRecords(inherit=True)
# 
#     # Managers (if you need special querying, e.g., for global rates specifically)
#     # objects = models.Manager() # Default Django manager
#     # global_rates = GlobalExchangeRateManager() # Custom manager example
# 
#     class Meta:
#         verbose_name = _("Exchange Rate")
#         verbose_name_plural = _("Exchange Rates")
#         # Ensures one rate per day/currency-pair/company (or global where company is Null)
#         # For databases that treat NULLs as distinct in unique constraints (like PostgreSQL),
#         # this works as intended. For others (like MySQL), you might need a partial index
#         # or handle uniqueness at the application level for global rates if strictness is needed.
#         unique_together = ('company', 'from_currency', 'to_currency', 'date')
#         ordering = ['company', 'from_currency', 'to_currency', '-date']  # Latest rate first
# 
#     def __str__(self):
#         company_str = f"Co: {self.company.name} " if self.company else "Global "
#         return (f"{company_str}{self.from_currency} to {self.to_currency} "
#                 f"on {self.date.strftime('%Y-%m-%d')} = {self.rate:.6f}")
# 
#     def clean(self):
#         super().clean()
#         if self.from_currency == self.to_currency:
#             raise ValidationError(_("From Currency and To Currency cannot be the same."))
#         if self.rate <= 0:
#             raise ValidationError(_("Exchange rate must be positive."))
#         # Optional: Check if company is active if a company is assigned
#         if self.company and not self.company.effective_is_active:
#             raise ValidationError(
#                 _("Cannot set exchange rates for an inactive or suspended company: %(company_name)s") %
#                 {'company_name': self.company.name}
#             )
# 
#     # Optional: If you add custom managers for global rates
#     # class GlobalExchangeRateManager(models.Manager):
#     #     def get_queryset(self):
#     #         return super().get_queryset().filter(company__isnull=True)