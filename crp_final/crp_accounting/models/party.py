# crp_accounting/models/party.py

import logging
from decimal import Decimal
from django.db import models, \
    transaction  # transaction is imported but not used, can be removed if not needed elsewhere in this file
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, RegexValidator
from django.utils import timezone

# --- Base Model Import ---
# Inherit from TenantScopedModel for automatic company scoping
try:
    from .base import TenantScopedModel
except ImportError:
    # This will likely cause a hard crash at startup if base.py is missing,
    # which is appropriate for a critical dependency.
    raise ImportError(
        "Could not import TenantScopedModel from .base. Ensure 'crp_accounting/models/base.py' exists and is accessible.")

# --- Enum Imports ---
# Assumes enums are defined correctly in crp_core/enums.py
try:
    from crp_core.enums import PartyType, AccountNature, DrCrType
except ImportError:
    raise ImportError(
        "Could not import core enums (PartyType, AccountNature, DrCrType). Ensure 'crp_core' app is installed and enums are defined.")

# --- Other Model Imports ---
# Import Account model for ForeignKey relationship
try:
    from .coa import Account
except ImportError:
    raise ImportError(
        "Could not import Account model from .coa. Ensure 'crp_accounting/models/coa.py' exists and is accessible.")

logger = logging.getLogger(__name__)


# --- Inherit from TenantScopedModel ---
class Party(TenantScopedModel):
    """
    Represents a financial party (Customer, Supplier, etc.) scoped to a specific company.
    Balances are calculated dynamically based on the linked Control Account.
    'company', 'created_at', 'updated_at', 'deleted_at', 'history' and company-aware managers
    are inherited from TenantScopedModel.
    """

    party_type = models.CharField(
        _("Party Type"),
        max_length=20,
        choices=PartyType.choices,
        db_index=True,
        help_text=_("Classifies the party (e.g., Customer, Supplier).")
    )
    name = models.CharField(
        _("Party Name"),
        max_length=255,
        db_index=True,
        help_text=_("Name of the party.")
    )
    # --- Contact Information ---
    contact_email = models.EmailField(
        _("Contact Email"), max_length=254, validators=[EmailValidator()],
        null=True, blank=True, help_text=_("Primary contact email address.")
    )
    contact_phone = models.CharField(
        _("Contact Phone"), max_length=20,
        validators=[RegexValidator(r'^\+?1?\d{9,19}$', message=_("Enter a valid phone number (e.g., +12125552368)."))],
        # Added i18n to message
        null=True, blank=True, help_text=_("Primary contact phone number.")
    )
    address = models.TextField(
        _("Address"), null=True, blank=True, help_text=_("Full postal address.")
    )

    # --- Financial Controls ---
    control_account = models.ForeignKey(
        Account,
        verbose_name=_("Control Account"),
        on_delete=models.PROTECT,  # Protect Account if Parties linked
        related_name='controlled_parties',
        null=True,  # Allow null, but clean() should enforce if active and type requires it
        blank=True,
        help_text=_("The COA Account summarizing this party's balance (must belong to the same company).")
        # limit_choices_to should be handled dynamically in forms/APIs based on the Party's company context.
    )
    credit_limit = models.DecimalField(
        _("Credit Limit"), max_digits=15, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Maximum credit amount extended (typically for Customers). 0 means no limit.")
    )

    # --- Status ---
    is_active = models.BooleanField(
        _("Is Active"), default=True, db_index=True,
        help_text=_("Inactive parties cannot be used in new transactions.")
    )

    # 'company', 'created_at', 'updated_at', 'deleted_at', 'history' are inherited from TenantScopedModel.
    # Default manager 'objects' is TenantSafeDeleteManager (company-scoped, non-deleted).

    class Meta:
        # Enforce uniqueness of party name within the company.
        # If party name should be unique per type within a company, use:
        # unique_together = ('company', 'party_type', 'name')
        unique_together = ('company', 'name')
        verbose_name = _('Party')
        verbose_name_plural = _('Parties')
        # Ordering considers company context, then party name.
        ordering = ['company__name', 'name']
        indexes = [
            # Indexes including 'company' for better query performance on scoped data.
            models.Index(fields=['company', 'party_type']),
            models.Index(fields=['company', 'name']),  # Covered by unique_together if DB creates index for it
            models.Index(fields=['company', 'is_active']),
            models.Index(fields=['company', 'control_account']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(credit_limit__gte=Decimal('0.00')),
                name='party_credit_limit_non_negative',
                violation_error_message=_("Credit limit cannot be negative.")
            )
        ]

    def __str__(self):
        # This __str__ is specific to Party. The base TenantScopedModel has a more generic one.
        # To include company info like in base:
        # company_prefix = self.company.subdomain_prefix if self.company_id and self.company else 'N/A'
        # return f"{self.name} ({self.get_party_type_display()}) (Co: {company_prefix})"
        return f"{self.name} ({self.get_party_type_display()})"

    def clean(self):
        """Custom model validation, ensuring data integrity within the party's company context."""
        # Run parent's clean method first (from TenantScopedModel, which might have its own checks)
        super().clean()

        # --- Validate Control Account (within the party's company) ---
        if self.control_account:
            # 1. Control Account must belong to the same company as the Party.
            #    self.company (or self.company_id) is set by TenantScopedModel's save() or passed in.
            if self.company_id and self.control_account.company_id != self.company_id:
                raise ValidationError({
                    'control_account': _("Control Account must belong to the same company as the Party.")
                })

            # 2. Ensure Account is marked as a Control Account.
            if not self.control_account.is_control_account:
                raise ValidationError(
                    {'control_account': _("The selected account is not marked as a Control Account.")})

            # 3. Ensure Account's designated party type matches this Party's type.
            #    Using .value for robust comparison with Django's TextChoices/IntegerChoices from enums.
            expected_control_party_type = None
            if self.party_type == PartyType.CUSTOMER.value:
                expected_control_party_type = PartyType.CUSTOMER.value
            elif self.party_type == PartyType.SUPPLIER.value:
                expected_control_party_type = PartyType.SUPPLIER.value
            # Add other party types if they have specific control account party type requirements.

            if expected_control_party_type and self.control_account.control_account_party_type != expected_control_party_type:
                raise ValidationError({
                    'control_account': _(
                        "The selected Control Account is not configured for Party Type '%(party_type)s'. Check the account's 'Control Account Party Type' setting.") % {
                                           'party_type': self.get_party_type_display()}
                })

        # 4. Control Account is required for certain active party types.
        requires_control_account_types = [PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]  # Example
        if self.is_active and self.party_type in requires_control_account_types and not self.control_account:
            raise ValidationError(
                {'control_account': _(
                    "An active Party of type '%(party_type)s' must have a Control Account assigned.") % {
                                        'party_type': self.get_party_type_display()}}
            )

        # Other validations can be added here. Credit limit non-negativity is handled by constraint.

    def save(self, *args, **kwargs):
        """
        Overrides TenantScopedModel.save() if specific pre-save full_clean logic for Party is needed.
        The base save() method already handles company assignment for new instances and calls full_clean.
        If this custom save method is kept, be mindful of full_clean being called by Party's save,
        and then potentially again by TenantScopedModel's save if update_fields is not used.
        """
        # Perform full_clean, excluding 'company' field from this validation call during updates,
        # as company assignment is managed by TenantScopedModel or should not change post-creation.
        exclude_from_clean = []
        if not self._state.adding:  # If updating an existing instance
            exclude_from_clean.append('company')

        # self.full_clean calls self.clean() internally.
        self.full_clean(exclude=exclude_from_clean or None)

        super().save(*args, **kwargs)  # Call parent's save method

    # --- Balance Calculation & Related Methods (Tenant Aware) ---

    def calculate_outstanding_balance(self, date_upto=None):
        """
        Calculates the outstanding balance for this party by querying its Control Account transactions.
        This method is company-aware because:
        1. `self.control_account` is tied to `self.company`.
        2. `VoucherLine.objects` (if VoucherLine is also TenantScopedModel) will be company-scoped.
        3. Explicit `voucher__company_id=self.company_id` filter adds robustness.
        """
        if not self.control_account:
            logger.warning(
                f"Cannot calculate balance for Party '{self.name}' (ID: {self.id}, Company ID: {self.company_id}): No Control Account assigned.")
            return Decimal('0.00')

        # Dynamically import VoucherLine to avoid circular dependencies at import time.
        from crp_accounting.models.journal import VoucherLine  # Assuming VoucherLine exists

        # Build a queryset for VoucherLines.
        # The default manager `VoucherLine.objects` should be company-scoped if VoucherLine inherits TenantScopedModel.
        lines_qs = VoucherLine.objects.filter(
            account=self.control_account,  # Filter by this party's control account
            voucher__party=self,  # Filter by vouchers associated with this party
            voucher__company_id=self.company_id  # Explicitly ensure voucher belongs to party's company
        )

        if date_upto:
            lines_qs = lines_qs.filter(voucher__date__lte=date_upto)

        # Aggregate debit and credit amounts.
        aggregation = lines_qs.aggregate(
            total_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), Decimal('0.00'),
                                 output_field=models.DecimalField()),
            total_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), Decimal('0.00'),
                                  output_field=models.DecimalField())
        )
        debit_total = aggregation['total_debit']
        credit_total = aggregation['total_credit']

        # Calculate balance based on the control account's nature.
        balance = Decimal('0.00')
        if self.control_account.account_nature == AccountNature.DEBIT.value:  # Typically Assets (e.g., Receivables)
            balance = debit_total - credit_total
        elif self.control_account.account_nature == AccountNature.CREDIT.value:  # Typically Liabilities (e.g., Payables)
            balance = credit_total - debit_total
        else:
            # This case should ideally be prevented by Account model validation.
            logger.error(
                f"Party '{self.name}' (ID: {self.id}, Company ID: {self.company_id}): Control Account '{self.control_account.account_number}' (ID: {self.control_account.id}) has an invalid or unexpected nature: '{self.control_account.account_nature}'.")
            raise ValueError(
                f"Invalid account nature on control account '{self.control_account.account_number}'. Balance calculation failed.")

        return balance

    def check_credit_limit(self, transaction_amount: Decimal):  # Added type hint for clarity
        """
        Checks if adding a transaction amount would exceed the party's credit limit.
        Assumes transaction_amount increases the amount owed by the party (for debit-nature control accounts).
        Company context is implicit via `self.calculate_outstanding_balance()`.
        """
        if not self.control_account or self.credit_limit <= Decimal('0.00'):
            return  # No credit limit to check or control account not set.

        # Ensure transaction_amount is a Decimal
        trans_amount_decimal = Decimal(transaction_amount)

        # Calculate current balance (company-scoped)
        current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())
        potential_balance = current_balance

        # Determine if control account is debit nature (e.g., Accounts Receivable)
        # Using getattr for flexibility if Account model's 'is_debit_nature' property changes.
        is_debit_nature_account = getattr(
            self.control_account,
            'is_debit_nature',  # Assumes Account model might have a property/method like this
            lambda: self.control_account.account_nature == AccountNature.DEBIT.value  # Fallback
        )()  # Call the retrieved method/lambda

        if is_debit_nature_account:
            potential_balance += trans_amount_decimal  # For customers, new sale increases balance owed.
        # Add logic for credit nature accounts if credit limits apply differently (e.g., supplier advances).

        if potential_balance > self.credit_limit:
            raise ValidationError(
                _("Credit limit of %(limit).2f for party '%(party)s' will be exceeded. "
                  "Current balance: %(balance).2f, Transaction amount: %(transaction).2f, Potential balance: %(potential).2f.") % {
                    'limit': self.credit_limit, 'party': self.name,
                    'balance': current_balance, 'transaction': trans_amount_decimal, 'potential': potential_balance
                }
            )

    def get_credit_status(self) -> str:  # Added type hint for clarity
        """
        Returns the credit status of the party: 'Within Limit', 'Over Credit Limit', or 'N/A'.
        Company context is implicit.
        """
        if not self.control_account or self.credit_limit <= Decimal('0.00'):
            return 'N/A'

        current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())

        is_debit_nature_account = getattr(
            self.control_account,
            'is_debit_nature',
            lambda: self.control_account.account_nature == AccountNature.DEBIT.value
        )()

        is_over_limit = False
        if is_debit_nature_account and current_balance > self.credit_limit:
            is_over_limit = True
        # Add logic for credit nature accounts if applicable for "over limit" status.

        return "Over Credit Limit" if is_over_limit else "Within Limit"

    def get_associated_vouchers(self, start_date=None, end_date=None):
        """
        Retrieves Voucher records associated with this party, filtered by the current company context.
        Relies on `Voucher.objects` being a company-scoped manager (e.g., if Voucher inherits TenantScopedModel).
        """
        from crp_accounting.models.journal import Voucher  # Assuming Voucher model exists

        # `Voucher.objects` should be the company-scoped manager from TenantScopedModel.
        # This query will find Vouchers in the current active company that are linked to this Party.
        # If `self` (Party instance) belongs to a different company than the current active context,
        # this query will correctly return no Vouchers.
        qs = Voucher.objects.filter(party=self)

        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)

        return qs.order_by('date', 'voucher_number', 'id')  # Assuming voucher_number exists for ordering
#
# import logging
# from decimal import Decimal
# from django.db import models, transaction
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ValidationError
# from django.core.validators import EmailValidator, RegexValidator
# from django.utils import timezone
#
# # Adjust imports based on your project structure
# from crp_core.enums import PartyType, TaxType, \
#     AccountNature  # Keep TaxType if used elsewhere, but removed from this model for now
# from crp_accounting.models.coa import Account # Import Account model
# from crp_core.enums import DrCrType # Needed for balance logic based on control account nature
#
# logger = logging.getLogger(__name__)
#
# class Party(models.Model):
#     """
#     Represents a financial party (sub-ledger entity) like a Customer, Supplier,
#     Employee, etc.
#
#     Key Principles:
#     - Balances are NOT stored directly but calculated dynamically from associated
#       journal entries hitting the party's designated Control Account in the COA.
#     - Linked to a specific Control Account (e.g., Accounts Receivable for Customers).
#     - Provides methods for credit limit checking based on calculated balances.
#     """
#
#     party_type = models.CharField(
#         _("Party Type"),
#         max_length=20,
#         choices=PartyType.choices, # Assumes PartyType enum has .choices attribute
#         db_index=True,
#         help_text=_("Classifies the party (e.g., Customer, Supplier). Important for determining control accounts and reporting.")
#     )
#     name = models.CharField(
#         _("Party Name"),
#         max_length=255,
#         db_index=True,
#         help_text=_("Name of the party (e.g., Company Name or Individual's Full Name).")
#     )
#     # --- Contact Information ---
#     contact_email = models.EmailField(
#         _("Contact Email"),
#         max_length=254,
#         validators=[EmailValidator()],
#         null=True, blank=True,
#         help_text=_("Primary contact email address.")
#     )
#     contact_phone = models.CharField(
#         _("Contact Phone"),
#         max_length=20, # Slightly increased length
#         validators=[RegexValidator(r'^\+?1?\d{9,19}$', message="Enter a valid phone number (e.g., +12125552368).")],
#         null=True, blank=True,
#         help_text=_("Primary contact phone number.")
#     )
#     address = models.TextField(
#         _("Address"),
#         null=True, blank=True,
#         help_text=_("Full postal address.")
#     )
#     # --- Financial Controls ---
#     control_account = models.ForeignKey(
#         Account,
#         verbose_name=_("Control Account"),
#         on_delete=models.PROTECT, # Prevent deleting Account if Parties are linked
#         related_name='controlled_parties',
#         null=True, # Allow null during creation/migration, but should be set for active parties
#         blank=True, # Allow blank in forms initially
#         help_text=_("The specific Account in the COA that summarizes this party's balance (e.g., Accounts Receivable for Customers). Required for balance calculation.")
#     )
#     credit_limit = models.DecimalField(
#         _("Credit Limit"),
#         max_digits=15,
#         decimal_places=2,
#         default=Decimal('0.00'),
#         help_text=_("Maximum credit amount extended to this party (typically for Customers). 0 means no limit set (or no credit).")
#     )
#     # --- Status ---
#     is_active = models.BooleanField(
#         _("Is Active"),
#         default=True,
#         db_index=True,
#         help_text=_("Inactive parties cannot be selected for new transactions.")
#     )
#     # --- Removed Fields ---
#     # outstanding_balance: Stored balance is removed. Use calculate_outstanding_balance().
#     # tax_type: Removed due to oversimplification. Implement tax logic at transaction level.
#
#     # --- Audit Fields ---
#     created_at = models.DateTimeField(_("Created At"), auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(_("Updated At"), auto_now=True, editable=False)
#
#     class Meta:
#         verbose_name = _('Party')
#         verbose_name_plural = _('Parties')
#         ordering = ['name']
#         constraints = [
#             models.CheckConstraint(
#                 check=models.Q(credit_limit__gte=Decimal('0.00')),
#                 name='party_credit_limit_non_negative',
#                 violation_error_message=_("Credit limit cannot be negative.")
#             )
#         ]
#
#     def __str__(self):
#         """String representation showing party name and type."""
#         return f"{self.name} ({self.get_party_type_display()})"
#
#     def clean(self):
#         """Custom model validation."""
#         super().clean()
#         if self.credit_limit < Decimal('0.00'):
#             # Also covered by constraint, but good practice in clean()
#             raise ValidationError({'credit_limit': _("Credit limit cannot be negative.")})
#
#         # Enforce control account based on type and active status
#         # These party types typically require a control account to function meaningfully
#         requires_control_account = self.party_type in [PartyType.CUSTOMER.name, PartyType.SUPPLIER.name] # Add others if needed (e.g., EMPLOYEE for advances)
#
#         if self.is_active and requires_control_account and not self.control_account:
#             raise ValidationError(
#                  {'control_account': _("An active Customer or Supplier must have a Control Account assigned.")}
#              )
#
#         # Validate that the assigned control account is appropriate
#         if self.control_account:
#             is_correct_control_type = (
#                 (self.party_type == PartyType.CUSTOMER.name and self.control_account.control_account_party_type == PartyType.CUSTOMER.name) or
#                 (self.party_type == PartyType.SUPPLIER.name and self.control_account.control_account_party_type == PartyType.SUPPLIER.name)
#                 # Add checks for other party types if needed
#             )
#             if not self.control_account.is_control_account or not is_correct_control_type:
#                  raise ValidationError({
#                      'control_account': _("The selected account is not a valid Control Account for Party Type '%(party_type)s'.") % {'party_type': self.get_party_type_display()}
#                  })
#
#     def save(self, *args, **kwargs):
#         """Ensure validation is run before saving."""
#         self.full_clean() # Run model validation including clean()
#         super().save(*args, **kwargs)
#
#     # --- Balance Calculation & Related Methods ---
#
#     def calculate_outstanding_balance(self, date_upto=None):
#         """
#         Calculates the outstanding balance for this party dynamically.
#
#         Queries Journal Lines linked to this party via its Journal Entries,
#         summing debits and credits against the party's assigned Control Account.
#
#         Args:
#             date_upto (date, optional): Calculate balance up to this date (inclusive).
#                                         If None, calculates the lifetime balance.
#
#         Returns:
#             Decimal: The calculated outstanding balance. Returns 0 if no control
#                      account is assigned or no transactions exist.
#
#         Raises:
#             ValueError: If the assigned control account has an invalid nature.
#         """
#         if not self.control_account:
#             logger.warning(f"Cannot calculate balance for Party '{self.name}' (ID: {self.id}): No Control Account assigned.")
#             return Decimal('0.00')
#
#         # Import dynamically to avoid potential app loading issues/circular imports
#         from crp_accounting.models.journal import VoucherLine
#
#         # Base queryset: Lines hitting the control account AND related to this party
#         lines = VoucherLine.objects.filter(
#             account=self.control_account,
#             voucher__party=self
#         )
#
#         if date_upto:
#             lines = lines.filter(voucher__date__lte=date_upto)
#
#         # Aggregate debits and credits
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
#         debit_total = aggregation.get('total_debit') or Decimal('0.00')
#         credit_total = aggregation.get('total_credit') or Decimal('0.00')
#
#         # Calculate balance based on the *control account's* nature
#         if self.control_account.account_nature == AccountNature.DEBIT.name:
#             # Typically Assets/Receivables: Balance = Debits - Credits
#             balance = debit_total - credit_total
#         elif self.control_account.account_nature == AccountNature.CREDIT.name:
#             # Typically Liabilities/Payables: Balance = Credits - Debits
#             balance = credit_total - debit_total
#         else:
#             # Should not happen with proper setup
#             logger.error(f"Control Account '{self.control_account}' for Party '{self.name}' has invalid nature: {self.control_account.account_nature}")
#             raise ValueError(f"Invalid account nature '{self.control_account.account_nature}' on control account.")
#
#         return balance
#
#     def check_credit_limit(self, transaction_amount):
#         """
#         Checks if adding a transaction amount would exceed the party's credit limit.
#         Relevant typically for Customers (Debit balance control accounts).
#
#         Args:
#             transaction_amount (Decimal): The amount of the new transaction that would
#                                           increase the party's balance (usually positive).
#
#         Raises:
#             ValidationError: If the credit limit is exceeded or balance calculation fails.
#         """
#         if not self.control_account or self.credit_limit <= 0:
#             return # No limit to check or calculation not possible
#
#         # For credit limit checks, we usually care about the *current* balance
#         current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())
#
#         # Credit limit applies when the balance represents money owed *by* the party
#         # For a DEBIT nature control account (like Accounts Receivable), a positive balance
#         # means the customer owes us. We check if this owed amount exceeds the limit.
#         potential_balance = current_balance
#         if self.control_account.is_debit_nature():
#              potential_balance += transaction_amount # Assume transaction increases amount owed by customer
#         # Note: Add logic for credit nature accounts if credit limits apply differently
#
#         if potential_balance > self.credit_limit:
#             raise ValidationError(
#                 _("Credit limit of %(limit)s for party '%(party)s' exceeded. Current balance: %(balance)s, Potential balance: %(potential)s") % {
#                     'limit': self.credit_limit,
#                     'party': self.name,
#                     'balance': current_balance,
#                     'potential': potential_balance
#                 }
#             )
#
#     def get_credit_status(self):
#         """
#         Indicates if the party is currently within their credit limit.
#
#         Returns:
#             str: 'Within Limit', 'Over Credit Limit', or 'N/A' (if no limit/control account).
#         """
#         if not self.control_account or self.credit_limit <= 0:
#             return 'N/A'
#
#         current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())
#
#         # Check based on control account nature
#         is_over_limit = False
#         if self.control_account.is_debit_nature and current_balance > self.credit_limit:
#             is_over_limit = True
#         # Add logic for credit nature accounts if applicable
#
#         return "Over Credit Limit" if is_over_limit else "Within Limit"
#
#     # --- Other Helper Methods ---
#
#     def get_associated_journal_entries(self, start_date=None, end_date=None):
#         """
#         Retrieves JournalEntry records associated with this party, optionally filtered by date.
#         """
#         # Import dynamically
#         from crp_accounting.models.journal import Voucher
#
#         qs = Voucher.objects.filter(party=self)
#         if start_date:
#             qs = qs.filter(date__gte=start_date)
#         if end_date:
#             qs = qs.filter(date__lte=end_date)
#         return qs.order_by('date', 'id') # Order chronologically