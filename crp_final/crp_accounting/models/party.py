# crp_accounting/models/party.py

import logging
from decimal import Decimal
from django.db import models
# from django.db import transaction # Not used directly in this snippet
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, RegexValidator
from django.utils import timezone

# --- Base Model Import ---
try:
    from .base import TenantScopedModel
except ImportError:
    raise ImportError(
        "Could not import TenantScopedModel from .base. Ensure 'crp_accounting/models/base.py' exists and is accessible.")

# --- Enum Imports ---
try:
    from crp_core.enums import PartyType, AccountNature, DrCrType
except ImportError:
    raise ImportError(
        "Could not import core enums (PartyType, AccountNature, DrCrType). Ensure 'crp_core' app is installed and enums are defined.")

# --- Other Model Imports ---
try:
    from .coa import Account  # Assuming your Account model has is_debit_nature and is_credit_nature properties
except ImportError:
    raise ImportError(
        "Could not import Account model from .coa. Ensure 'crp_accounting/models/coa.py' exists and is accessible.")

logger = logging.getLogger(__name__)


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
        null=True, blank=True, help_text=_("Primary contact phone number.")
    )
    address = models.TextField(
        _("Address"), null=True, blank=True, help_text=_("Full postal address.")
    )

    # --- Financial Controls ---
    control_account = models.ForeignKey(
        Account,
        verbose_name=_("Control Account"),
        on_delete=models.PROTECT,
        related_name='controlled_parties',
        null=True,
        blank=True,
        help_text=_("The COA Account summarizing this party's balance (must belong to the same company).")
    )
    credit_limit = models.DecimalField(
        _("Credit Limit"), max_digits=15, decimal_places=2, default=Decimal('0.00'),
        help_text=_(
            "For Customers: Maximum credit amount extended to them. "
            "For Suppliers: An internal threshold for the maximum amount owed to them. "
            "0 means no limit is applied."
        )
    )

    # --- Status ---
    is_active = models.BooleanField(
        _("Is Active"), default=True, db_index=True,
        help_text=_("Inactive parties cannot be used in new transactions.")
    )

    class Meta:
        unique_together = ('company', 'name')
        verbose_name = _('Party')
        verbose_name_plural = _('Parties')
        ordering = ['company__name', 'name']
        indexes = [
            models.Index(fields=['company', 'party_type']),
            models.Index(fields=['company', 'name']),
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
        company_prefix_display = ""
        if self.company_id and hasattr(self, 'company') and self.company:
            company_prefix_display = f" (Co: {self.company.name or self.company.subdomain_prefix or self.company_id})"
        elif self.company_id:
            company_prefix_display = f" (Co ID: {self.company_id})"

        return f"{self.name} ({self.get_party_type_display()}){company_prefix_display}"

    def clean(self):
        super().clean()
        if self.control_account:
            if self.company_id and self.control_account.company_id != self.company_id:
                raise ValidationError({
                    'control_account': _("Control Account must belong to the same company as the Party.")
                })
            if not self.control_account.is_control_account:
                raise ValidationError(
                    {'control_account': _("The selected account is not marked as a Control Account.")})
            expected_control_party_type = None
            if self.party_type == PartyType.CUSTOMER.value:
                expected_control_party_type = PartyType.CUSTOMER.value
            elif self.party_type == PartyType.SUPPLIER.value:
                expected_control_party_type = PartyType.SUPPLIER.value
            if expected_control_party_type and self.control_account.control_account_party_type != expected_control_party_type:
                raise ValidationError({
                    'control_account': _(
                        "The selected Control Account is not configured for Party Type '%(party_type)s'. Check the account's 'Control Account Party Type' setting.") % {
                                           'party_type': self.get_party_type_display()}
                })
        requires_control_account_types = [PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]
        if self.is_active and self.party_type in requires_control_account_types and not self.control_account:
            raise ValidationError(
                {'control_account': _(
                    "An active Party of type '%(party_type)s' must have a Control Account assigned.") % {
                                        'party_type': self.get_party_type_display()}}
            )

    def save(self, *args, **kwargs):
        exclude_from_clean = []
        if not self._state.adding:
            exclude_from_clean.append('company')
        self.full_clean(exclude=exclude_from_clean or None)
        super().save(*args, **kwargs)

    def calculate_outstanding_balance(self, date_upto=None):
        if not self.control_account:
            logger.warning(
                f"Cannot calculate balance for Party '{self.name}' (ID: {self.id}, Company ID: {self.company_id}): No Control Account assigned.")
            return Decimal('0.00')
        # Ensure TransactionStatus is defined or imported if not already
        from crp_accounting.models.journal import VoucherLine, TransactionStatus

        lines_qs = VoucherLine.objects.filter(
            account=self.control_account,
            voucher__party=self,
            voucher__company_id=self.company_id,
            voucher__status=TransactionStatus.POSTED.value
        )
        if date_upto:
            lines_qs = lines_qs.filter(voucher__date__lte=date_upto)
        aggregation = lines_qs.aggregate(
            total_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), Decimal('0.00'),
                                 output_field=models.DecimalField()),
            total_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), Decimal('0.00'),
                                  output_field=models.DecimalField())
        )
        debit_total = aggregation['total_debit']
        credit_total = aggregation['total_credit']
        balance = Decimal('0.00')

        # Assuming Account model has 'is_debit_nature' and 'is_credit_nature' properties
        if self.control_account.is_debit_nature:
            balance = debit_total - credit_total
        elif self.control_account.is_credit_nature:
            balance = credit_total - debit_total
        else:
            logger.error(
                f"Party '{self.name}' (ID: {self.id}, Company ID: {self.company_id}): Control Account '{self.control_account.account_number}' (ID: {self.control_account.id}) has an invalid or unexpected nature: '{self.control_account.account_nature}'.")
            raise ValueError(
                f"Invalid account nature on control account '{self.control_account.account_number}'. Balance calculation failed.")
        return balance

    def check_credit_limit(self, transaction_amount: Decimal):
        """
        Checks if a new transaction would exceed the party's credit limit.
        This check is primarily relevant for Customers (debit nature control accounts).
        For Suppliers, this might represent an internal threshold.
        """
        if not self.control_account or self.credit_limit <= Decimal('0.00'):
            return  # No limit to check against

        trans_amount_decimal = Decimal(transaction_amount)
        current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())
        potential_balance = current_balance

        # For customers (debit nature), a new transaction increasing their balance (e.g., a sales invoice)
        # would add to the debit side.
        # For suppliers (credit nature), a new transaction increasing what you owe them (e.g., a purchase bill)
        # would effectively increase their "balance" from your perspective of "amount owed".
        # The calculate_outstanding_balance already gives us the "net amount" from the party's perspective.
        # If control account is debit nature (Customer), an increase in their debt is positive.
        # If control account is credit nature (Supplier), an increase in your debt to them is positive.
        # So, we just need to consider how the transaction_amount affects this balance.
        # This part might need refinement based on whether transaction_amount is always positive
        # and how it relates to Dr/Cr of the *party's control account line* in the new transaction.

        # Simplified assumption: transaction_amount is an increase in the party's balance from their perspective
        # (e.g. more owed by customer, or more owed to supplier by us).
        # This logic is more straightforward for Customers.
        if self.control_account.is_debit_nature:  # Typically Customers
            # Assuming transaction_amount is the value of a new sale increasing customer debt
            potential_balance += trans_amount_decimal
            if potential_balance > self.credit_limit:
                raise ValidationError(
                    _("Credit limit of %(limit).2f for customer '%(party)s' will be exceeded. "
                      "Current balance: %(balance).2f, Transaction amount: %(transaction).2f, Potential balance: %(potential).2f.") % {
                        'limit': self.credit_limit, 'party': self.name,
                        'balance': current_balance, 'transaction': trans_amount_decimal, 'potential': potential_balance
                    }
                )
        elif self.control_account.is_credit_nature:  # Typically Suppliers
            # Assuming transaction_amount is the value of a new purchase increasing our debt to supplier
            # The 'current_balance' from calculate_outstanding_balance for a supplier is already positive if we owe them.
            # So we add the new amount we will owe.
            potential_balance += trans_amount_decimal  # This line assumes transaction_amount is always increasing the payable
            if potential_balance > self.credit_limit:
                logger.info(  # Log as info, as this might be a soft limit for suppliers
                    f"Internal credit threshold of {self.credit_limit:.2f} for supplier '{self.name}' "
                    f"may be exceeded. Current payable: {current_balance:.2f}, "
                    f"Transaction amount: {trans_amount_decimal:.2f}, Potential payable: {potential_balance:.2f}."
                )
                # Decide if you want to raise ValidationError for suppliers or just log/warn
                # For now, let's not raise a hard error for suppliers from this method by default,
                # as the primary use is often for customer credit control.
                # If you want a hard stop:
                # raise ValidationError(
                # _("Internal credit threshold of %(limit).2f for supplier '%(party)s' will be exceeded. "
                #   "Current payable: %(balance).2f, Transaction amount: %(transaction).2f, Potential payable: %(potential).2f.") % {
                # 'limit': self.credit_limit, 'party': self.name,
                # 'balance': current_balance, 'transaction': trans_amount_decimal, 'potential': potential_balance
                # }
                # )
        # Note: The logic for 'potential_balance' for suppliers in check_credit_limit might need
        # more nuance depending on how 'transaction_amount' is passed and its sign.

    def get_credit_status(self) -> str:
        """
        Determines the credit status based on the current balance and credit limit.
        Applies to both Customers (credit extended by us) and Suppliers (internal threshold for payables).
        """
        if not self.control_account or self.credit_limit <= Decimal('0.00'):
            # If credit limit is 0, it means "no limit", so they are effectively "Within Limit"
            # unless there's no control account, then it's truly N/A for calculation.
            return "N/A" if not self.control_account else "Within Limit (No Limit Set)"

        current_balance = self.calculate_outstanding_balance(date_upto=timezone.now().date())

        # The balance from calculate_outstanding_balance is positive if:
        # - Customer owes us (debit nature control account)
        # - We owe Supplier (credit nature control account)
        # In both cases, if this positive balance exceeds the credit_limit, they are "Over Credit Limit".
        # This unified logic works because calculate_outstanding_balance normalizes the balance view.

        is_over_limit = False
        if current_balance > self.credit_limit:
            is_over_limit = True

        # Differentiate message slightly for clarity if needed, though the logic is the same
        if is_over_limit:
            if self.party_type == PartyType.CUSTOMER.value:
                return _("Over Credit Limit")
            elif self.party_type == PartyType.SUPPLIER.value:
                return _("Over Limit (Payables Threshold)")  # More descriptive for suppliers
            else:
                return _("Over Limit")  # Generic
        else:
            return _("Within Limit")

    def get_associated_vouchers(self, start_date=None, end_date=None):
        from crp_accounting.models.journal import Voucher
        qs = Voucher.objects.filter(party=self)
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
        return qs.order_by('date', 'voucher_number', 'id')
# Assuming voucher_number exists for ordering
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