# crp_accounting/models/coa.py
import logging
from datetime import date # For date type hinting and operations
from decimal import Decimal # For precise monetary calculations
from typing import Optional, List, Dict, Any  # For type hints to improve code readability and maintainability

from django.db import models, transaction # Django ORM and atomic transaction support
from django.db.models import Sum, Q # For complex database queries (Sum aggregation, Q objects for OR/AND)
from django.db.models.functions import Coalesce # For handling NULL values in database aggregations
from django.utils.translation import gettext_lazy as _ # For internationalization of strings
from django.core.exceptions import ValidationError # For raising data validation errors
from django.utils import timezone  # Django's timezone utility for datetime operations

from company.models import Company # Import the Company model for ForeignKey relationships
from .base import TenantScopedModel  # Your multi-tenant base model for common fields and logic

# --- Core Enum Imports ---
try:
    # Attempt to import essential enumerations for account classifications and statuses.
    from crp_core.enums import AccountType, AccountNature, CurrencyType, PartyType, DrCrType, TransactionStatus
except ImportError:
    # This is a critical dependency. If enums are not found, the application cannot function correctly.
    logger_init_coa = logging.getLogger(f"{__name__}.initialization") # Use a specific logger for init issues
    logger_init_coa.critical(
        "CRITICAL: Could not import core enums from 'crp_core'. Ensure 'crp_core' app "
        "is installed and enums (AccountType, AccountNature, etc.) are correctly defined."
    )
    raise ImportError(
        "Could not import core enums from 'crp_core'. Ensure 'crp_core' app "
        "is installed and enums (AccountType, AccountNature, etc.) are correctly defined."
    )

logger = logging.getLogger(__name__) # Standard logger for this module

# --- Constants ---
# Mapping from AccountType to its inherent AccountNature (Debit or Credit).
# This is used to automatically derive the nature of an account based on its type.
ACCOUNT_TYPE_TO_NATURE: Dict[str, str] = {
    AccountType.ASSET.value: AccountNature.DEBIT.value,
    AccountType.LIABILITY.value: AccountNature.CREDIT.value,
    AccountType.EQUITY.value: AccountNature.CREDIT.value,
    AccountType.INCOME.value: AccountNature.CREDIT.value,
    AccountType.EXPENSE.value: AccountNature.DEBIT.value,
    AccountType.COST_OF_GOODS_SOLD.value: AccountNature.DEBIT.value,
}


class PLSection(models.TextChoices):
    """
    Defines sections for structuring the Profit & Loss (P&L) statement.
    These choices categorize income and expense accounts for financial reporting.
    """
    REVENUE = 'REVENUE', _('Revenue') # For accounts directly generating revenue

    # --- COGS Components for Periodic Inventory System ---
    # These sections are typically used when COGS is calculated periodically (e.g., end of month).
    OPENING_STOCK_COGS = 'OPENING_STOCK_COGS', _('Opening Stock (COGS)')
    PURCHASES_COGS = 'PURCHASES_COGS', _('Purchases (COGS)')
    FREIGHT_IN_COGS = 'FREIGHT_IN_COGS', _('Freight-In (COGS)') # Costs to bring inventory to location
    PURCHASE_RETURNS_COGS_ADJ = 'PURCHASE_RETURNS_COGS_ADJ', _('Purchase Returns Adj. (COGS)') # Contra-purchases
    PURCHASE_DISCOUNTS_COGS_ADJ = 'PURCHASE_DISCOUNTS_COGS_ADJ', _('Purchase Discounts Adj. (COGS)') # Reductions in purchase cost
    CLOSING_STOCK_COGS_ADJ = 'CLOSING_STOCK_COGS_ADJ', _('Closing Stock Adj. (COGS)') # Adjustment for unsold inventory
    # --- End COGS Components ---

    # For direct COGS entries (e.g., in a perpetual inventory system) or the summarized COGS figure.
    COGS = 'COGS', _('Cost of Goods Sold (Direct/Perpetual)')

    OPERATING_EXPENSE = 'OPERATING_EXPENSE', _('Operating Expense') # Day-to-day business running costs
    DEPRECIATION_AMORTIZATION = 'DEPR_AMORT', _('Depreciation & Amortization') # Non-cash expenses
    OTHER_INCOME = 'OTHER_INCOME', _('Other Income') # Income not from primary business activities
    OTHER_EXPENSE = 'OTHER_EXPENSE', _('Other Expense') # Expenses not from primary business activities
    TAX_EXPENSE = 'TAX_EXPENSE', _('Tax Expense') # Corporate income taxes
    NONE = 'NONE', _('Not Applicable (Balance Sheet Accounts)') # Default for accounts not appearing on P&L (e.g., Assets, Liabilities, Equity)

class AccountGroup(TenantScopedModel):
    """
    Represents a hierarchical grouping for accounts within the Chart of Accounts (COA).
    Each group belongs to a specific company and can have a parent group,
    allowing for a tree-like structure (e.g., Assets > Current Assets > Cash).

    Inherits common fields like `company`, audit trails, and tenant-aware managers
    from `TenantScopedModel`.
    """
    name = models.CharField(
        _("Group Name"),
        max_length=150,
        db_index=True, # Indexed for faster lookups, especially within a company.
        help_text=_("Name for the account group (e.g., Current Assets). Must be unique within the company.")
    )
    description = models.TextField(
        _("Description"),
        blank=True, # Optional field.
        help_text=_("Optional description of the account group's purpose.")
    )
    parent_group = models.ForeignKey(
        'self', # Recursive relationship to allow nesting of groups.
        verbose_name=_("Parent Group"),
        on_delete=models.PROTECT, # Prevent deletion of a parent group if it has sub-groups.
        null=True, blank=True, # Top-level groups will have no parent.
        related_name='sub_groups', # Name to use for the reverse relation from parent to sub-groups.
        help_text=_("Assign parent for hierarchy. Leave blank for top-level group.")
    )

    class Meta:
        # Ensures that 'name' is unique within the scope of a single 'company'.
        unique_together = ('company', 'name')
        verbose_name = _('Account Group')
        verbose_name_plural = _('Account Groups')
        ordering = ['name'] # Default ordering when querying lists of account groups.

    def __str__(self) -> str:
        """
        String representation of the AccountGroup, primarily its name.
        """
        # Original commented-out line for consideration:
        # Company name can be added for superuser contexts if self.company is reliably loaded
        # e.g., f"{self.name} (Co: {self.company.name if self.company_id and self.company else 'N/A'})"
        return self.name

    def clean(self):
        """
        Custom validation logic for the AccountGroup model.
        Ensures data integrity, such as preventing circular dependencies and
        ensuring parent groups belong to the same company.
        """
        super().clean()  # Call the clean method of the parent class (TenantScopedModel).

        # 1. Prevent circular parent references: A group cannot be its own ancestor.
        parent = self.parent_group
        # Initialize with self.pk if instance is already saved, otherwise an empty set for new instances.
        visited_ancestors = {self.pk} if self.pk else set()
        while parent:
            # Check for unsaved instance pointing to itself as parent.
            if parent.pk is None and parent is self:
                raise ValidationError({'parent_group': _("An account group cannot be its own parent.")})
            # Check if the current parent's pk has already been visited in the chain.
            if parent.pk is not None and parent.pk in visited_ancestors:
                raise ValidationError(
                    {'parent_group': _("Circular dependency detected: This group cannot be an ancestor of itself.")})
            if parent.pk: # Add saved parent's pk to visited set.
                visited_ancestors.add(parent.pk)
            parent = parent.parent_group # Move up to the next parent.

        # 2. Ensure parent group (if any) belongs to the same company as this group.
        if self.parent_group and self.company_id and self.parent_group.company_id != self.company_id:
            raise ValidationError({
                'parent_group': _("Parent group must belong to the same company as this group.")
            })

    def get_all_child_accounts(self, include_inactive_accounts: bool = False) -> List['Account']:
        """
        Recursively retrieves all `Account` instances belonging to this group
        and all its sub-groups.

        Args:
            include_inactive_accounts (bool): If True, includes accounts that are
                marked as inactive or soft-deleted (if using SafeDeleteModel features).
                Defaults to False.

        Returns:
            List['Account']: A list of Account objects.
        """
        # Determine which queryset manager to use based on 'include_inactive_accounts'.
        # Assumes 'accounts' related manager has 'all_objects_including_deleted' if it's a SafeDelete related manager.
        if include_inactive_accounts and hasattr(self.accounts, 'all_objects_including_deleted'):
            accounts_qs = self.accounts.all_objects_including_deleted()
        else:
            accounts_qs = self.accounts.all() # Default manager (e.g., TenantSafeDeleteManager)

        accounts = list(accounts_qs) # Get accounts directly under this group.
        # Recursively get accounts from all sub-groups.
        for sub_group in self.sub_groups.all(): # .all() on related manager respects default manager filtering.
            accounts.extend(sub_group.get_all_child_accounts(include_inactive_accounts=include_inactive_accounts))
        return accounts

    def get_full_path(self, separator: str = " > ") -> str:
        """
        Constructs the full hierarchical path of the account group (e.g., "Assets > Current Assets > Cash").

        Args:
            separator (str): The string used to separate group names in the path.

        Returns:
            str: The full path string.
        """
        path_parts = [self.name]
        parent = self.parent_group
        recursion_limit, count = 20, 0 # Safety mechanism to prevent infinite loops in case of data corruption.
        while parent and count < recursion_limit:
            path_parts.insert(0, parent.name) # Prepend parent's name.
            parent = parent.parent_group
            count += 1
        if count >= recursion_limit: # If recursion limit was hit, indicate path truncation.
            path_parts.insert(0, "...")
            logger.warning(
                f"AccountGroup {self.pk or 'NEW'} full path calculation might be truncated due to excessive depth or a potential cycle not caught by clean().")
        return separator.join(path_parts)


class Account(TenantScopedModel):
    """
    Represents an individual account in the Chart of Accounts (COA).
    Each account is scoped to a company and linked to an AccountGroup.
    It defines the properties of an account, such as its type, nature,
    currency, and whether it allows direct posting.

    Inherits common fields and behaviors from `TenantScopedModel`.
    """
    account_number = models.CharField(
        _("Account Number/Code"),
        max_length=50,
        db_index=True, # Indexed for fast lookups, unique within a company.
        help_text=_("Identifier code for the account. Must be unique within the company.")
    )
    account_name = models.CharField(
        _("Account Name"),
        max_length=255,
        db_index=True, # Indexed for fast lookups, unique within a company.
        help_text=_("Human-readable name (e.g., Cash On Hand). Must be unique within the company.")
    )
    description = models.TextField(
        _("Description"),
        blank=True, # Optional detailed description.
        help_text=_("Optional detailed description of the account's purpose.")
    )
    account_group = models.ForeignKey(
        AccountGroup,
        verbose_name=_("Account Group"),
        on_delete=models.PROTECT, # Prevent deletion of an AccountGroup if it has Accounts.
        related_name='accounts', # Name for reverse relation from AccountGroup to Account.
        help_text=_("The hierarchical group this account belongs to.")
    )
    account_type = models.CharField(
        _("Account Type"),
        max_length=30,
        choices=AccountType.choices, # Uses choices from the AccountType enum.
        db_index=True, # Indexed for filtering by type.
        help_text=_("Fundamental accounting classification (Asset, Liability, etc.).")
    )
    account_nature = models.CharField(
        _("Account Nature"),
        max_length=10,
        choices=AccountNature.choices, # Uses choices from the AccountNature enum.
        editable=False, # This field is system-derived based on account_type.
        help_text=_("System-inferred nature (Debit/Credit).")
    )
    pl_section = models.CharField(
        _("P&L Section"),
        max_length=30,
        choices=PLSection.choices, # Uses choices from the PLSection enum.
        default=PLSection.NONE.value, # Default for accounts not on P&L.
        blank=True, # Can be blank, will default to NONE.
        db_index=True, # Indexed for P&L report generation.
        help_text=_("Specific section classification for the Profit & Loss statement.")
    )
    currency = models.CharField(
        _("Account Currency"),
        max_length=10, # Typically 3-letter ISO currency codes.
        choices=CurrencyType.choices, # Uses choices from the CurrencyType enum.
        help_text=_("Currency for this account. Defaults to the company's main currency if not specified.")
    )
    is_active = models.BooleanField(
        _("Account is Active"),
        default=True, # New accounts are active by default.
        db_index=True, # Indexed for filtering active accounts.
        help_text=_("Inactive accounts cannot be selected for new transactions.")
    )
    allow_direct_posting = models.BooleanField(
        _("Allow Direct Posting"),
        default=True, # Most accounts allow direct posting by default.
        help_text=_("Can journal entries be posted directly to this account? (Usually False for summary/parent accounts).")
    )
    is_control_account = models.BooleanField(
        _("Is Control Account"),
        default=False, # Most accounts are not control accounts by default.
        help_text=_("True if this account summarizes a subsidiary ledger (e.g., Accounts Receivable controlling customer balances).")
    )
    control_account_party_type = models.CharField(
        _("Control Account Party Type"),
        max_length=20,
        choices=PartyType.choices, # Uses choices from the PartyType enum (e.g., CUSTOMER, VENDOR).
        null=True, blank=True, # Only applicable if is_control_account is True.
        db_index=True, # Indexed for linking to subsidiary ledgers.
        help_text=_("If 'Is Control Account' is true, specify which Party Type it controls (e.g., CUSTOMER).")
    )
    # Denormalized fields for performance, updated by specific processes.
    current_balance = models.DecimalField(
        _("Current Balance"),
        max_digits=20, decimal_places=2, # Adjust precision as needed.
        default=Decimal('0.00'),
        editable=False, # Not directly editable by users; updated by system.
        help_text=_("Denormalized current balance. Updated by system processes (e.g., daily batch job or triggers).")
    )
    balance_last_updated = models.DateTimeField(
        _("Balance Last Updated"),
        null=True, blank=True, # Timestamp of the last update to current_balance.
        editable=False,
        help_text=_("Timestamp of the last balance recalculation for 'current_balance'.")
    )

    class Meta:
        # Ensures account_number and account_name are unique within the scope of a single company.
        unique_together = (
            ('company', 'account_number'),
            ('company', 'account_name')
        )
        verbose_name = _('Account (COA Entry)')
        verbose_name_plural = _('Accounts (COA Entries)')
        # Default ordering for account lists.
        ordering = ['company__name', 'account_group__name', 'account_number']
        # Indexes for common query patterns to improve performance.
        indexes = [
            models.Index(fields=['company', 'account_type']),
            models.Index(fields=['company', 'pl_section']),
            models.Index(fields=['company', 'is_active', 'allow_direct_posting']),
            models.Index(fields=['company', 'is_control_account', 'control_account_party_type']),
            models.Index(fields=['company', 'account_group']), # Already indexed by FK, but explicit can be clearer.
        ]
        # Database-level constraints to enforce data integrity.
        constraints = [
            models.CheckConstraint(
                check=models.Q(is_control_account=False) | models.Q(control_account_party_type__isnull=False),
                name='coa_check_control_account_requires_party_type',
                violation_error_message=_("Control accounts must specify a Control Account Party Type.")
            ),
            models.CheckConstraint(
                check=models.Q(is_control_account=True) | models.Q(control_account_party_type__isnull=True),
                name='coa_check_party_type_requires_control_account',
                violation_error_message=_("Control Account Party Type can only be set if 'Is Control Account' is true.")
            ),
        ]

    def __str__(self) -> str:
        """
        String representation of the Account, showing name, number, and optionally the company name.
        """
        # Ensure self.company is loaded if you're accessing self.company.name
        # self._ensure_company_loaded() # Call this if company might not be loaded

        # Check if company attribute exists and is loaded
        company_name_part = ""
        # First, check if 'company' object is already loaded onto the instance.
        if hasattr(self, 'company') and self.company:
            company_name_part = f" - {self.company.name}"  # Or self.company.subdomain_prefix
        # Else, if only company_id is present and 'company' object isn't loaded, try to fetch it.
        elif self.company_id and not hasattr(self, 'company'):
            try:
                # This database hit can be a performance concern if __str__ is called for many objects
                # in a list display (e.g., admin dropdowns).
                # Consider using select_related('company') in the queryset that populates such displays.
                company_obj = Company.objects.get(pk=self.company_id)
                company_name_part = f" - {company_obj.name}"
            except Company.DoesNotExist:
                pass  # No company name part if company not found or if fetching is undesirable here.

        return f"{self.account_name} ({self.account_number}){company_name_part}"

    @property
    def account_type_display(self) -> str:
        """Returns the human-readable display name for the account_type."""
        return self.get_account_type_display()

    @property
    def account_nature_display(self) -> str:
        """Returns the human-readable display name for the account_nature."""
        return self.get_account_nature_display()

    @property
    def pl_section_display(self) -> str:
        """Returns the human-readable display name for the pl_section."""
        return self.get_pl_section_display()

    @property
    def currency_display(self) -> str:
        """Returns the human-readable display name for the currency."""
        return self.get_currency_display()

    @property
    def control_account_party_type_display(self) -> Optional[str]:
        """Returns the human-readable display name for the control_account_party_type, if set."""
        return self.get_control_account_party_type_display() if self.control_account_party_type else None

    def _ensure_company_loaded(self):
        """
        Internal helper to load the `company` object if only `company_id` is present
        and the object hasn't been cached on the instance yet.
        Primarily used by `_set_derived_fields` if company info is needed.
        """
        # Checks if the Django machinery for the 'company' FK is present (`_company_meta`)
        # and if `company_id` has a value, and if we haven't already tried to cache the company object.
        if not hasattr(self, '_company_meta') and self.company_id and not getattr(self, '_company_object_cached', None):
            try:
                # from company.models import Company # Already imported at module level
                self.company = Company.objects.get(pk=self.company_id) # Fetch the company object
                self._company_object_cached = True # Mark that we've attempted to load/cache it
            except Company.DoesNotExist:
                logger.warning(
                    f"Account {self.pk or 'NEW'}: company_id {self.company_id} set, but Company object not found during _ensure_company_loaded.")

    def _set_derived_fields(self):
        """
        Internal helper method called before saving/cleaning to set fields
        that are derived from other fields, such as `account_nature` and
        the default `currency`.

        IMPORTANT: This method respects an `account_nature` that might have been
        set externally (e.g., by a data seeding script or fixture) by only
        deriving it if `self.account_nature` is not already set.
        """
        # 1. Set Account Nature based on Account Type.
        #    Crucially, only sets `account_nature` IF IT IS NOT ALREADY SET and `account_type` is available.
        #    This allows for overriding the derived nature if necessary during data import or specific scenarios.
        if not self.account_nature and self.account_type:
            inferred_nature_value = ACCOUNT_TYPE_TO_NATURE.get(self.account_type)
            if not inferred_nature_value:
                # This typically indicates a configuration issue: either ACCOUNT_TYPE_TO_NATURE is incomplete
                # or self.account_type holds an unexpected value not present as a key in the map.
                raise ValidationError({
                    'account_type': _(
                        "System configuration error: Cannot map account type '%(type)s' to an account nature. "
                        "Please check ACCOUNT_TYPE_TO_NATURE mapping or the provided account type.") % {
                                        'type': self.get_account_type_display()}
                })
            self.account_nature = inferred_nature_value
        elif not self.account_type and not self.account_nature:
            # This case should ideally be prevented by model validation making account_type non-nullable.
            # If account_type is missing, nature cannot be derived.
            logger.warning(
                f"Account {self.pk or 'NEW'} (Co: {self.company_id}) is missing `account_type`, "
                "cannot derive `account_nature`. `account_nature` will remain as is (e.g., blank or previously set value)."
            )
            # Depending on business rules, one might raise a ValidationError here if account_type is strictly required for nature.

        # 2. Set Default Currency from Company's default currency, if account currency is not already set.
        if not self.currency:  # Only proceed if `self.currency` is not already explicitly set.
            self._ensure_company_loaded() # Ensure the `company` object is loaded to access its attributes.
            if hasattr(self, 'company') and self.company and \
                    hasattr(self.company, 'default_currency_code') and self.company.default_currency_code:
                self.currency = self.company.default_currency_code
            elif self.company_id: # Log a warning if company_id is set but currency couldn't be defaulted.
                logger.warning(
                    f"Account {self.pk or 'NEW'} for Company ID {self.company_id}: Could not set default currency. "
                    "Either the Company object was not loaded, or the company does not have a 'default_currency_code' set."
                )

    def clean(self):
        """
        Custom validation logic for the Account model.
        This method is called by `full_clean()` before saving.
        It ensures data integrity by checking various business rules.
        """
        super().clean() # Call parent's clean method.

        # Call _ensure_company_loaded() early if subsequent validations depend on the company object.
        self._ensure_company_loaded()
        # Call _set_derived_fields() to ensure `account_nature` and default `currency` are set
        # before other validations that might depend on them. This respects pre-set nature.
        self._set_derived_fields()

        # Validate that the AccountGroup belongs to the same company as the Account.
        if self.account_group and self.company_id and self.account_group.company_id != self.company_id:
            raise ValidationError({'account_group': _("Account Group must belong to the same company as the Account.")})

        # Validate control account rules (already covered by DB constraints, but good for earlier feedback).
        if self.is_control_account and not self.control_account_party_type:
            raise ValidationError(
                {'control_account_party_type': _("Control accounts must specify a Control Account Party Type.")})
        if not self.is_control_account and self.control_account_party_type:
            raise ValidationError(
                {'control_account_party_type': _("Control Account Party Type can only be set on Control Accounts.")})

        # Validate P&L section based on account type.
        is_pl_type = self.account_type in [
            AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value
        ]
        if is_pl_type and self.pl_section == PLSection.NONE.value:
            # P&L account types (Income, Expense, COGS) must have a specific P&L section.
            raise ValidationError(
                {'pl_section': _("P&L Section must be set for Income, Expense, or COGS account types.")})
        if not is_pl_type and self.pl_section != PLSection.NONE.value:
            # Non-P&L account types (Asset, Liability, Equity) should have P&L section as 'NONE'.
            raise ValidationError({'pl_section': _(
                "P&L Section should be 'Not Applicable' for Asset, Liability, or Equity account types.")})

        # Prevent changing account_type if there are posted transactions (for existing accounts).
        if self.pk and not self._state.adding: # Check if this is an existing record being updated.
            try:
                # Fetch the original account_type from the database.
                # Use global_objects if the default 'objects' manager is tenant-scoped to ensure fetching by PK.
                original_account = Account.global_objects.only('account_type').get(pk=self.pk)
                if original_account.account_type != self.account_type:
                    # Account type is being changed. Check for posted transactions.
                    from crp_accounting.models.journal import VoucherLine # Local import to avoid circular dependency.
                    if VoucherLine.objects.filter(account_id=self.pk,
                                                  voucher__status=TransactionStatus.POSTED.value).exists():
                        # If posted transactions exist, disallow account type change.
                        raise ValidationError({'account_type': _(
                            "Cannot change account type: Posted transactions exist for this account. "
                            "Consider creating a new account or reversing existing entries first.")})
                    logger.info(
                        f"Account type change detected for Account {self.pk} (Co: {self.company_id}). "
                        f"Original: {original_account.account_type}, New: {self.account_type}. No posted transactions found.")
            except Account.DoesNotExist:
                # This should not happen if self.pk exists and we are not adding.
                logger.error(f"Error fetching original account type for existing Account {self.pk} during clean(). Object might have been deleted concurrently.")

    def save(self, *args, **kwargs):
        """
        Overrides the default save method.
        `_set_derived_fields` is normally called within `self.clean()`, which is called by
        `self.full_clean()`. Django's `save()` calls `full_clean()` by default
        unless `full_clean=False` is passed or `update_fields` is used.

        The explicit calls to `_set_derived_fields` here act as a safeguard or
        could be for scenarios where `full_clean` might be bypassed.
        """
        # Original commented-out lines from your code, preserved:
        # self._ensure_company_loaded() # Ensure company is loaded if needed by derived fields
        # self._set_derived_fields()   # Apply logic just before hitting the DB

        # The main call to _set_derived_fields is within self.clean().
        # Django's model save() method calls self.full_clean() (which includes self.clean())
        # by default, unless explicitly told not to, or when using update_fields.
        # This check provides a final opportunity to set derived fields if `account_nature`
        # is still not set and `account_type` is available, covering edge cases where `clean()`
        # might have been bypassed or `account_nature` was not set for other reasons.
        if not self.account_nature and self.account_type:
            # This call ensures derived fields are set, especially if the full_clean path was skipped
            # AND account_nature wasn't pre-set.
            self._set_derived_fields()

        super().save(*args, **kwargs) # Call the parent's save method.

    @property
    def is_debit_nature(self) -> bool:
        """Returns True if the account's nature is Debit, False otherwise."""
        return self.account_nature == AccountNature.DEBIT.value

    @property
    def is_credit_nature(self) -> bool:
        """Returns True if the account's nature is Credit, False otherwise."""
        return self.account_nature == AccountNature.CREDIT.value

    def get_dynamic_balance(self, date_upto: Optional[date] = None, start_date: Optional[date] = None,
                            include_pending: bool = False) -> Decimal:
        """
        Calculates the dynamic balance of the account based on its VoucherLines up to a specified date.

        Args:
            date_upto (Optional[date]): Calculate balance up to this date (inclusive).
                                        If None, considers all transactions.
            start_date (Optional[date]): Calculate balance from this date (inclusive).
                                         If None, considers transactions from the beginning.
            include_pending (bool): If True, includes transactions with 'PENDING' status.
                                    Defaults to False (only 'POSTED' transactions).

        Returns:
            Decimal: The calculated balance of the account.
        """
        from crp_accounting.models.journal import VoucherLine  # Local import to avoid circular dependencies at module level.

        lines_qs = VoucherLine.objects.filter(account_id=self.pk)  # Use account_id for direct FK lookup.

        # Filter by transaction status if not including pending.
        if not include_pending:
            lines_qs = lines_qs.filter(voucher__status=TransactionStatus.POSTED.value)

        # Apply date filters.
        date_filter = Q() # Initialize an empty Q object for combining date conditions.
        if start_date:
            date_filter &= Q(voucher__date__gte=start_date) # Transactions on or after start_date.
        if date_upto:
            date_filter &= Q(voucher__date__lte=date_upto) # Transactions on or before date_upto.
        lines_qs = lines_qs.filter(date_filter) # Apply the combined date filter.

        # Aggregate total debit and credit amounts.
        # Coalesce ensures that if Sum returns NULL (no matching lines), it defaults to Decimal('0.00').
        aggregation = lines_qs.aggregate(
            total_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), Decimal('0.00'),
                                 output_field=models.DecimalField()),
            total_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), Decimal('0.00'),
                                  output_field=models.DecimalField())
        )
        debit_total = aggregation.get('total_debit', Decimal('0.00'))
        credit_total = aggregation.get('total_credit', Decimal('0.00'))

        # Calculate balance based on account nature.
        if self.is_debit_nature:
            balance = debit_total - credit_total
        elif self.is_credit_nature:
            balance = credit_total - debit_total
        else:
            # This case should ideally not occur if account_nature is always correctly set.
            logger.error(
                f"Account {self.pk} (Name: {self.account_name}, Co: {self.company_id}) "
                f"has an invalid or unset account nature ('{self.account_nature}') for balance calculation."
            )
            balance = Decimal('0.00')  # Default to zero or consider raising an error.
        return balance

    @classmethod
    def get_accounts_for_posting(cls, company: Optional[Any] = None, user: Optional[Any] = None) -> models.QuerySet['Account']:
        """
        Retrieves a queryset of accounts that are active and allow direct posting,
        optionally filtered by company.

        Args:
            company (Optional[Company]): The company to filter accounts for.
                                         If None, and using tenant-scoped default manager,
                                         it might filter by current context or require global_objects.
            user (Optional[User]): Currently unused, but could be used for future permission checks.

        Returns:
            models.QuerySet['Account']: A queryset of eligible accounts.
        """
        # Determine base queryset: global_objects if company is specified (to ensure filtering works across tenants),
        # otherwise, use the default 'objects' manager (which might be tenant-scoped by context).
        # This logic assumes TenantScopedModel provides `global_objects` for unfiltered access
        # and `objects` for tenant-scoped access (potentially by context).
        qs = cls.global_objects if company else cls.objects

        if company:
            # It's good practice to ensure 'company' is of the correct type if passed.
            # Original commented-out type check:
            # from company.models import Company as TenantCompanyModel
            # if not isinstance(company, TenantCompanyModel):
            #     raise ValueError("Invalid company instance provided.")
            qs = qs.filter(company=company) # Filter by the provided company instance.

        # Further filter for accounts that are active and allow direct posting.
        return qs.filter(is_active=True, allow_direct_posting=True)

    @transaction.atomic # Ensures the balance update is an atomic operation.
    def update_stored_balance(self, calculated_balance: Optional[Decimal] = None):
        """
        Updates the denormalized `current_balance` and `balance_last_updated` fields
        for this account instance.

        Args:
            calculated_balance (Optional[Decimal]): The balance to store. If None,
                it will be dynamically calculated using `get_dynamic_balance()`
                up to the current date.
        """
        if calculated_balance is None:
            # If no balance is provided, calculate it dynamically up to the current moment.
            calculated_balance = self.get_dynamic_balance(date_upto=timezone.now().date())

        if not self.company_id:  # Safety check: an account should always have a company.
            logger.error(f"Cannot update stored balance for Account {self.pk} (Name: {self.account_name}): company_id is missing.")
            return

        # Perform the update using global_objects to ensure the specific PK and company_id are targeted,
        # bypassing any default tenant scoping that might be on `Account.objects`.
        updated_rows = Account.global_objects.filter(pk=self.pk, company_id=self.company_id).update(
            current_balance=calculated_balance,
            balance_last_updated=timezone.now()
        )

        if updated_rows > 0:
            # If the update was successful, refresh the instance's fields from the database.
            self.refresh_from_db(fields=['current_balance', 'balance_last_updated'])
            logger.info(
                f"Stored balance for Account '{self.account_name}' (ID: {self.pk}, Co: {self.company_id}) "
                f"updated to {self.current_balance} as of {self.balance_last_updated}."
            )
        else:
            # This might happen if the account was deleted concurrently or if the company_id mismatch.
            logger.warning(
                f"Failed to update stored balance for Account '{self.account_name}' (ID: {self.pk}, Co: {self.company_id}). "
                "No rows updated (record might not exist with this primary key and company_id, "
                "or balance was already current and no actual change was needed by the DB)."
            )
# # crp_accounting/models/coa.py
#
# import logging
# from decimal import Decimal
# from django.db import models, transaction
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ValidationError
# from django.utils import timezone # Needed for balance_last_updated
#
# # Assuming enums are defined correctly in crp_core/enums.py
# # Ensure these enums exist and are properly defined.
# from crp_core.enums import AccountType, AccountNature, CurrencyType, PartyType, DrCrType
#
# logger = logging.getLogger(__name__)
#
# # --- Constants ---
# # --- CORRECTED Dictionary: Using Enum VALUES as Keys ---
# # This dictionary is used by Account.save() to determine the nature.
# # The keys MUST match the values stored in the Account.account_type field.
# ACCOUNT_TYPE_TO_NATURE = {
#     AccountType.ASSET.value: AccountNature.DEBIT.name,           # e.g., 'ASSET': 'DEBIT'
#     AccountType.LIABILITY.value: AccountNature.CREDIT.name,      # e.g., 'LIABILITY': 'CREDIT'
#     AccountType.EQUITY.value: AccountNature.CREDIT.name,         # e.g., 'EQUITY': 'CREDIT'
#     AccountType.INCOME.value: AccountNature.CREDIT.name,         # e.g., 'INCOME': 'CREDIT'
#     AccountType.EXPENSE.value: AccountNature.DEBIT.name,         # e.g., 'EXPENSE': 'DEBIT'
#     AccountType.COST_OF_GOODS_SOLD.value: AccountNature.DEBIT.name, # e.g., 'COGS': 'DEBIT' <-- Corrected Key
# }
#
#
# # =============================================================================
# # P&L Section Enum
# # =============================================================================
# class PLSection(models.TextChoices):
#     """
#     Defines sections for structuring the Profit & Loss statement.
#     Allows for standard reporting like Gross Profit calculation.
#     """
#     REVENUE = 'REVENUE', _('Revenue')
#     COGS = 'COGS', _('Cost of Goods Sold')
#     OPERATING_EXPENSE = 'OPERATING_EXPENSE', _('Operating Expense')
#     OTHER_INCOME = 'OTHER_INCOME', _('Other Income')
#     OTHER_EXPENSE = 'OTHER_EXPENSE', _('Other Expense')
#     TAX_EXPENSE = 'TAX_EXPENSE', _('Tax Expense')
#     DEPRECIATION_AMORTIZATION = 'DEPR_AMORT', _('Depreciation & Amortization')
#     NONE = 'NONE', _('Not Applicable (Balance Sheet)') # Default for non-P&L accounts
#
# # =============================================================================
# # Account Group Model
# # =============================================================================
# class AccountGroup(models.Model):
#     """
#     Represents a hierarchical grouping for the Chart of Accounts (COA).
#     Allows structuring accounts into logical categories for reporting and organization.
#     """
#     name = models.CharField(
#         _("Group Name"),
#         max_length=150,
#         unique=True,
#         db_index=True,
#         help_text=_("Unique name for the account group (e.g., Current Assets, Operating Expenses).")
#     )
#     description = models.TextField(
#         _("Description"),
#         blank=True,
#         help_text=_("Optional description of the account group's purpose.")
#     )
#     parent_group = models.ForeignKey(
#         'self',
#         verbose_name=_("Parent Group"),
#         on_delete=models.PROTECT, # Prevent deleting a group if it has sub-groups
#         null=True,
#         blank=True,
#         related_name='sub_groups',
#         help_text=_("Assign parent for hierarchy. Leave blank for top-level.")
#     )
#
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#
#     class Meta:
#         verbose_name = _('Account Group')
#         verbose_name_plural = _('Account Groups')
#         ordering = ['name']
#
#     def __str__(self):
#         return self.name
#
#     def get_all_child_accounts(self):
#         """Recursively gets all accounts under this group and its sub-groups."""
#         accounts = list(self.accounts.all())
#         for sub_group in self.sub_groups.all():
#             accounts.extend(sub_group.get_all_child_accounts())
#         return accounts
#
# # =============================================================================
# # Account Model
# # =============================================================================
# class Account(models.Model):
#     """
#     Represents a specific ledger account within the Chart of Accounts (COA).
#     Transactions are posted here (if allowed). Defines classification and nature.
#     Stores the calculated current balance.
#     """
#     # --- Identification ---
#     account_number = models.CharField(
#         _("Account Number"),
#         max_length=50,
#         unique=True,
#         db_index=True,
#         help_text=_("Unique identifier code for the account (e.g., 10100, 40001).")
#     )
#     account_name = models.CharField(
#         _("Account Name"),
#         max_length=255,
#         db_index=True,
#         help_text=_("Human-readable name (e.g., Cash On Hand, Sales Revenue - Services).")
#     )
#     description = models.TextField(
#         _("Description"),
#         blank=True,
#         help_text=_("Optional detailed description of the account's purpose.")
#     )
#
#     # --- Classification & Hierarchy ---
#     account_group = models.ForeignKey(
#         AccountGroup,
#         verbose_name=_("Account Group"),
#         on_delete=models.PROTECT,
#         related_name='accounts',
#         help_text=_("The hierarchical group this account belongs to.")
#     )
#     account_type = models.CharField(
#         _("Account Type"),
#         max_length=20, # Should match the longest value in AccountType (e.g., 'LIABILITY')
#         choices=AccountType.choices,
#         db_index=True,
#         help_text=_("Fundamental accounting classification (Asset, Liability, etc.).")
#     )
#     account_nature = models.CharField(
#         _("Account Nature"),
#         max_length=10, # Should match 'DEBIT' or 'CREDIT'
#         choices=AccountNature.choices,
#         editable=False,
#         help_text=_("System-inferred nature (Debit/Credit). Based on Account Type.")
#     )
#     pl_section = models.CharField(
#         _("P&L Section"),
#         max_length=25, # Should match the longest value in PLSection
#         choices=PLSection.choices,
#         default=PLSection.NONE,
#         blank=True,
#         db_index=True,
#         help_text=_("Specific section classification for the Profit & Loss statement (e.g., Revenue, COGS, Operating Expense). Required for detailed P&L structure.")
#     )
#
#     # --- Settings & Controls ---
#     currency = models.CharField(
#         _("Currency"),
#         max_length=10, # Should match longest currency code (e.g., 'USD')
#         choices=CurrencyType.choices,
#         default=CurrencyType.USD.value, # Store the value ('USD')
#         help_text=_("Primary currency for transactions posted to this account.")
#     )
#     is_active = models.BooleanField(
#         _("Is Active"),
#         default=True,
#         db_index=True,
#         help_text=_("Inactive accounts cannot be selected for new transactions.")
#     )
#     allow_direct_posting = models.BooleanField(
#         _("Allow Direct Posting"),
#         default=True,
#         help_text=_("Can journal entries be posted directly to this account? (False for summary accounts).")
#     )
#     is_control_account = models.BooleanField(
#         _("Is Control Account"),
#         default=False,
#         help_text=_("Mark True if this account summarizes a subsidiary ledger (e.g., Accounts Receivable).")
#     )
#     control_account_party_type = models.CharField(
#         _("Control Account Party Type"),
#         max_length=20, # Should match longest value in PartyType
#         choices=PartyType.choices,
#         null=True, blank=True, db_index=True,
#         help_text=_("If Control Account, specify which Party Type it controls (e.g., CUSTOMER).")
#     )
#
#     # --- Ledger Balance Fields ---
#     current_balance = models.DecimalField(
#         _("Current Balance"),
#         max_digits=20, decimal_places=2,
#         default=Decimal('0.00'),
#         editable=False,
#         help_text=_("Calculated current balance based on posted transactions (updated asynchronously).")
#     )
#     balance_last_updated = models.DateTimeField(
#         _("Balance Last Updated"),
#         null=True, blank=True, editable=False,
#         help_text=_("Timestamp when current_balance was last recalculated.")
#     )
#
#     # --- Audit Fields ---
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#
#     class Meta:
#         verbose_name = _('Account')
#         verbose_name_plural = _('Accounts')
#         ordering = ['account_group__name', 'account_number']
#         indexes = [
#             models.Index(fields=['account_type']),
#             models.Index(fields=['pl_section']),
#             models.Index(fields=['is_active', 'allow_direct_posting']),
#             models.Index(fields=['is_control_account', 'control_account_party_type']),
#         ]
#         constraints = [
#             models.CheckConstraint(
#                 check=models.Q(is_control_account=False) | models.Q(control_account_party_type__isnull=False),
#                 name='control_account_requires_party_type',
#                 violation_error_message=_("Control accounts must specify a Control Account Party Type.")
#             ),
#             models.CheckConstraint(
#                 check=models.Q(is_control_account=True) | models.Q(control_account_party_type__isnull=True),
#                 name='party_type_requires_control_account',
#                 violation_error_message=_("Control Account Party Type can only be set on Control Accounts.")
#             ),
#         ]
#         permissions = [
#             ("view_financial_reports", "Can view financial reports"),
#         ]
#
#     def __str__(self):
#         return f"{self.account_name} ({self.account_number})"
#
#     def clean(self):
#         """Custom model validation logic run before saving."""
#         super().clean()
#         # Validation for control account setup
#         if self.is_control_account and not self.control_account_party_type:
#              raise ValidationError({'control_account_party_type': _("Control accounts must specify a Control Account Party Type.")})
#         if not self.is_control_account and self.control_account_party_type:
#              raise ValidationError({'control_account_party_type': _("Cannot set Control Account Party Type on a non-control account.")})
#
#         # --- Validate pl_section against account_type using VALUES ---
#         # Check the value stored in self.account_type
#         is_pl_type = self.account_type in [
#             AccountType.INCOME.value,
#             AccountType.EXPENSE.value,
#             AccountType.COST_OF_GOODS_SOLD.value
#         ]
#         # Check the value stored in self.pl_section
#         if is_pl_type and self.pl_section == PLSection.NONE.value:
#             raise ValidationError({
#                 'pl_section': _("P&L Section must be set (cannot be 'NONE') for Income, Expense, or COGS account types.")
#             })
#         if not is_pl_type and self.pl_section != PLSection.NONE.value:
#             raise ValidationError({
#                 'pl_section': _("P&L Section must be 'NONE' for Asset, Liability, or Equity account types.")
#             })
#         # --- End pl_section validation ---
#
#         # --- Validate account_nature logic consistency ---
#         # This ensures clean() catches mapping errors even before save() is called
#         inferred_nature = ACCOUNT_TYPE_TO_NATURE.get(self.account_type)
#         if not inferred_nature:
#             # Raise error here during clean if the mapping is missing
#              raise ValidationError({
#                  'account_type': _("System configuration error: Cannot determine nature for account type '%(type)s'. Check ACCOUNT_TYPE_TO_NATURE mapping.") % {'type': self.account_type}
#              })
#         # Temporarily set nature for other potential clean checks (optional)
#         # self.account_nature = inferred_nature
#
#
#     def save(self, *args, **kwargs):
#         """Overrides save to auto-set account nature and run full validation."""
#         # 1. Auto-set nature from account type reliably before saving
#         #    Uses the ACCOUNT_TYPE_TO_NATURE dictionary defined at the top of this file.
#         #    Looks up based on the VALUE of self.account_type (e.g., 'ASSET', 'COGS').
#         inferred_nature = ACCOUNT_TYPE_TO_NATURE.get(self.account_type)
#         if inferred_nature:
#             self.account_nature = inferred_nature
#         else:
#             # This should ideally be caught by clean(), but acts as a final safeguard.
#             logger.critical(f"Account nature mapping missing for type {self.account_type} on account {self.account_number}!")
#             raise ValidationError(_(f"System Error: Cannot save Account, missing nature mapping for type '{self.account_type}'."))
#
#         # 2. Run full validation including clean() method and constraints
#         #    Use exclude for fields calculated/set elsewhere (like by async tasks)
#         self.full_clean(exclude=['current_balance', 'balance_last_updated'])
#
#         # 3. Call original save
#         super().save(*args, **kwargs)
#
#     # --- Helper Properties & Methods ---
#     @property
#     def is_debit_nature(self) -> bool:
#         """Helper property to check if the account naturally increases with debits."""
#         return self.account_nature == AccountNature.DEBIT.value # Compare against value
#
#     @property
#     def is_credit_nature(self) -> bool:
#         """Helper property to check if the account naturally increases with credits."""
#         return self.account_nature == AccountNature.CREDIT.value # Compare against value
#
#     def get_dynamic_balance(self, date_upto=None, start_date=None):
#         """Dynamically calculates the balance or movement based on posted transactions."""
#         # Import locally to avoid circular dependency
#         from crp_accounting.models.journal import VoucherLine, TransactionStatus, DrCrType
#
#         lines = VoucherLine.objects.filter(
#             account=self,
#             voucher__status=TransactionStatus.POSTED
#         )
#         date_filter = models.Q()
#         if start_date: date_filter &= models.Q(voucher__date__gte=start_date)
#         if date_upto: date_filter &= models.Q(voucher__date__lte=date_upto)
#         lines = lines.filter(date_filter)
#
#         aggregation = lines.aggregate(
#             total_debit=models.functions.Coalesce(
#                 models.Sum('amount', filter=models.Q(dr_cr=DrCrType.DEBIT.value)), # Use .value
#                 Decimal('0.00'), output_field=models.DecimalField()
#             ),
#             total_credit=models.functions.Coalesce(
#                 models.Sum('amount', filter=models.Q(dr_cr=DrCrType.CREDIT.value)), # Use .value
#                 Decimal('0.00'), output_field=models.DecimalField()
#             )
#         )
#         debit_total = aggregation['total_debit']
#         credit_total = aggregation['total_credit']
#
#         # Use helper properties which now compare values
#         if self.is_debit_nature:
#             balance = debit_total - credit_total
#         elif self.is_credit_nature:
#             balance = credit_total - debit_total
#         else:
#             logger.error(f"Account {self.account_number} has invalid nature '{self.account_nature}' during balance calculation.")
#             balance = Decimal('0.00')
#         return balance
#
#     @classmethod
#     def get_accounts_for_posting(cls):
#         """Class method returns active accounts where direct posting is allowed."""
#         return cls.objects.filter(is_active=True, allow_direct_posting=True)

#
# import logging
# from decimal import Decimal
# from django.db import models, transaction
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ValidationError
# from crp_core.enums import AccountType, AccountNature, CurrencyType, PartyType, DrCrType
#
# logger = logging.getLogger(__name__)
#
# # --- Constants ---
# # Mapping used to auto-set account nature. Ensure this is accessible.
# # It's often defined in a core constants file.
# ACCOUNT_TYPE_TO_NATURE = {
#     AccountType.ASSET.name: AccountNature.DEBIT.name,
#     AccountType.EXPENSE.name: AccountNature.DEBIT.name,
#     AccountType.LIABILITY.name: AccountNature.CREDIT.name,
#     AccountType.INCOME.name: AccountNature.CREDIT.name,
#     AccountType.EQUITY.name: AccountNature.CREDIT.name,
# }
#
#
# class AccountGroup(models.Model):
#     """
#     Represents a hierarchical grouping for the Chart of Accounts (COA).
#
#     Similar to Tally Groups, this allows structuring accounts into logical
#     categories (e.g., Assets -> Current Assets -> Bank Accounts).
#     It facilitates reporting aggregation and COA organization.
#     """
#     name = models.CharField(
#         _("Group Name"),
#         max_length=150,
#         unique=True,
#         db_index=True,
#         help_text=_("Unique name for the account group (e.g., Current Assets, Operating Expenses).")
#     )
#     is_primary = models.BooleanField(default=False)
#     description = models.TextField(
#         _("Description"),
#         blank=True,
#         help_text=_("Optional description of the account group's purpose.")
#     )
#     parent_group = models.ForeignKey(
#         'self',
#         verbose_name=_("Parent Group"),
#         on_delete=models.PROTECT,  # Prevent deleting a group if it has sub-groups
#         null=True,
#         blank=True,
#         related_name='sub_groups',
#         help_text=_("Assign a parent group to create a hierarchy (e.g., 'Current Assets' is under 'Assets'). Leave blank for top-level groups.")
#     )
#     is_primary = models.BooleanField(
#         _("Is Primary Group"),
#         default=False,
#         help_text=_("Mark as True if this is a top-level group like Assets, Liabilities, Equity, Income, or Expenses.")
#     )
#
#     # Standard audit fields
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#
#     class Meta:
#         verbose_name = _('Account Group')
#         verbose_name_plural = _('Account Groups')
#         ordering = ['name']  # Default ordering
#
#     def __str__(self):
#         """String representation showing the group name."""
#         return self.name
#
#     def get_all_child_accounts(self):
#         """Recursively gets all accounts under this group and its sub-groups."""
#         accounts = list(self.accounts.all())
#         for sub_group in self.sub_groups.all():
#             accounts.extend(sub_group.get_all_child_accounts())
#         return accounts
#
#
# class Account(models.Model):
#     """
#     Represents a specific ledger account within the Chart of Accounts (COA).
#
#     This is the level where transactions are typically posted (unless direct posting is disallowed).
#     It defines the account's classification, nature, and links it to its group.
#     Balances are calculated dynamically from associated JournalLine entries.
#     """
#
#     account_number = models.CharField(
#         _("Account Number/Code"),
#         max_length=50,
#         unique=True,
#         db_index=True,
#         help_text=_("Unique identifier code for the account (e.g., 10100, 40001).")
#     )
#     account_name = models.CharField(
#         _("Account Name"),
#         max_length=255,
#         db_index=True,
#         help_text=_("Human-readable name of the account (e.g., Cash On Hand, Sales Revenue - Services).")
#     )
#     description = models.TextField(
#         _("Description"),
#         blank=True,
#         help_text=_("Optional detailed description of the account's purpose or usage.")
#     )
#     account_group = models.ForeignKey(
#         AccountGroup,
#         verbose_name=_("Account Group"),
#         on_delete=models.PROTECT, # Prevent deleting group if accounts exist under it
#         related_name='accounts',
#         help_text=_("The hierarchical group this account belongs to (e.g., 'Cash' belongs to 'Bank Accounts' group).")
#     )
#     account_type = models.CharField(
#         _("Account Type"),
#         max_length=20,
#         choices=AccountType.choices,
#         help_text=_("Fundamental accounting classification (Asset, Liability, Income, Expense, Equity). Determines the account's role in financial statements.")
#     )
#     account_nature = models.CharField(
#         _("Account Nature"),
#         max_length=10,
#         choices=AccountNature.choices,
#         editable=False, # Automatically set based on Account Type
#         help_text=_("System-inferred Dr/Cr nature (Debit/Credit). Based on the Account Type.")
#     )
#     currency = models.CharField(
#         _("Currency"),
#         max_length=10,
#         choices=CurrencyType.choices,
#         default=CurrencyType.USD.name, # Set your system's default currency
#         help_text=_("The primary currency for transactions posted to this account.")
#     )
#     is_active = models.BooleanField(
#         _("Is Active"),
#         default=True,
#         db_index=True,
#         help_text=_("Inactive accounts cannot be selected for new transactions.")
#     )
#     allow_direct_posting = models.BooleanField(
#         _("Allow Direct Posting"),
#         default=True,
#         help_text=_("Can journal entries be posted directly to this account? Set to False for summary or group-level accounts where posting should only happen to sub-accounts.")
#     )
#     is_control_account = models.BooleanField(
#         _("Is Control Account"),
#         default=False,
#         help_text=_("Mark True if this account summarizes a subsidiary ledger (e.g., Accounts Receivable controls the Customer ledger, Accounts Payable controls the Supplier ledger).")
#     )
#     control_account_party_type = models.CharField(
#         _("Control Account Party Type"),
#         max_length=20,
#         choices=PartyType.choices,
#         null=True, blank=True,
#         help_text=_("If 'Is Control Account' is True, specify which Party Type this account controls (e.g., CUSTOMER for Accounts Receivable).")
#     )
#
#     # Standard audit fields
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#
#     class Meta:
#         verbose_name = _('Account')
#         verbose_name_plural = _('Accounts')
#         ordering = ['account_group__name', 'account_number'] # Order logically by group then number
#         constraints = [
#             models.CheckConstraint(
#                 check=models.Q(is_control_account=False) | models.Q(control_account_party_type__isnull=False),
#                 name='control_account_requires_party_type',
#                 violation_error_message=_("Control accounts must specify a Control Account Party Type.")
#             ),
#             models.CheckConstraint(
#                 check=models.Q(is_control_account=True) | models.Q(control_account_party_type__isnull=True),
#                 name='party_type_requires_control_account',
#                 violation_error_message=_("Control Account Party Type can only be set on Control Accounts.")
#             )
#         ]
#
#     def __str__(self):
#         """String representation including name and number."""
#         return f"{self.account_name} ({self.account_number})"
#
#     def clean(self):
#         """Custom validation logic run before saving."""
#         super().clean()
#         # Ensure control account setup is valid
#         if self.is_control_account and not self.control_account_party_type:
#              # This is also covered by constraints, but good practice to have in clean()
#              raise ValidationError(_("Control accounts must specify a Control Account Party Type."))
#         if not self.is_control_account and self.control_account_party_type:
#              raise ValidationError(_("Cannot set Control Account Party Type on a non-control account."))
#
#         # Prevent direct posting to inactive accounts if desired (though usually handled in transaction forms)
#         # if not self.is_active and self.allow_direct_posting:
#         #     raise ValidationError(_("Inactive accounts cannot allow direct posting."))
#
#     def save(self, *args, **kwargs):
#         """
#         Overrides save to auto-set account nature before saving.
#         """
#         # 1. Auto-set nature from account type
#         inferred_nature = ACCOUNT_TYPE_TO_NATURE.get(self.account_type)
#         if inferred_nature:
#             self.account_nature = inferred_nature
#         else:
#             # This indicates a setup issue (missing mapping in ACCOUNT_TYPE_TO_NATURE)
#             logger.error(f"Could not determine account nature for type {self.account_type} on account {self.account_number}. Check ACCOUNT_TYPE_TO_NATURE mapping.")
#             # Depending on strictness, you might raise ValidationError here
#             # raise ValidationError(_(f"System configuration error: Cannot determine nature for account type '{self.account_type}'."))
#             # Or default to a safe value if appropriate (less recommended)
#             # self.account_nature = AccountNature.DEBIT.name # Example default - use with caution
#
#         # 2. Run full validation
#         self.full_clean() # Ensures `clean()` and field validations run
#
#         # 3. Call original save
#         super().save(*args, **kwargs)
#
#     def get_balance(self, date_upto=None, start_date=None):
#         """
#         Dynamically calculates the balance or movement of the account.
#
#         - If only `date_upto` is provided, calculates the closing balance as of that date.
#         - If `start_date` and `date_upto` are provided, calculates the net movement
#           within that period (useful for P&L accounts).
#         - If neither is provided, calculates the lifetime balance.
#
#         Args:
#             date_upto (date, optional): Calculate balance up to this date (inclusive).
#             start_date (date, optional): Calculate movement starting from this date (inclusive).
#
#         Returns:
#             Decimal: The calculated balance or movement.
#         """
#         # Import locally to avoid circular dependency issues at module load time
#         from crp_accounting.models.journal import VoucherLine
#
#         lines = VoucherLine.objects.filter(account=self)
#
#         # Apply date filters
#         if start_date:
#             lines = lines.filter(journal_entry__date__gte=start_date)
#         if date_upto:
#             lines = lines.filter(journal_entry__date__lte=date_upto)
#
#         # Aggregate debits and credits within the filtered range
#         aggregation = lines.aggregate(
#             total_debit=models.Sum(
#                 models.Case(
#                     models.When(dr_cr=DrCrType.DEBIT.name, then='amount'),
#                     default=Decimal('0.00'),
#                     output_field=models.DecimalField()
#                 )
#             ),
#             total_credit=models.Sum(
#                 models.Case(
#                     models.When(dr_cr=DrCrType.CREDIT.name, then='amount'),
#                     default=Decimal('0.00'),
#                     output_field=models.DecimalField()
#                 )
#             )
#         )
#
#         debit_total = aggregation.get('total_debit') or Decimal('0.00')
#         credit_total = aggregation.get('total_credit') or Decimal('0.00')
#
#         # Determine balance based on account nature
#         # For closing balance (no start_date or start_date is very early)
#         # For period movement (start_date is provided) - net change is typically Dr - Cr
#         if self.account_nature == AccountNature.DEBIT.name:
#             balance = debit_total - credit_total
#         elif self.account_nature == AccountNature.CREDIT.name:
#             balance = credit_total - debit_total
#         else:
#             # Should not happen if nature is always set
#             logger.warning(f"Account {self.account_number} has undefined nature '{self.account_nature}'. Returning raw Dr-Cr.")
#             balance = debit_total - credit_total
#
#         return balance
#
#     def is_debit_nature(self):
#         """Helper method to check if the account has a debit nature."""
#         return self.account_nature == AccountNature.DEBIT.name
#
#     def is_credit_nature(self):
#         """Helper method to check if the account has a credit nature."""
#         return self.account_nature == AccountNature.CREDIT.name
#
#     @classmethod
#     def get_accounts_for_posting(cls):
#         """Returns active accounts where direct posting is allowed."""
#         return cls.objects.filter(is_active=True, allow_direct_posting=True)