import logging
from datetime import date  # Use this directly
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING

from django.db import models, transaction
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.conf import settings

from . import SMALL_TOLERANCE

# --- Base Model & Company Import ---
try:
    from .base import TenantScopedModel
    from company.models import Company
except ImportError:
    raise ImportError("Could not import TenantScopedModel or Company. Critical dependency missing for payables models.")

# --- Related Accounting Model Imports ---
try:
    from .coa import Account
    from .journal import Voucher, TransactionStatus, DrCrType  # For related_gl_voucher and status checks
    from .party import Party
    from .period import AccountingPeriod  # For GL posting context
except ImportError as e:
    raise ImportError(f"Could not import related accounting models for payables: {e}.")

# --- Enum Imports ---
try:
    from crp_core.enums import PartyType, CurrencyType, AccountType, \
    VoucherType as CoreVoucherType, BillStatus  # Use a distinct alias if name clashes
except ImportError:
    raise ImportError("Could not import core enums (PartyType, CurrencyType, AccountType) from 'crp_core'.")

logger = logging.getLogger("crp_accounting.models.payables")
ZERO_DECIMAL = Decimal('0.00')


# =============================================================================
# Sequence Models for AP Documents
# =============================================================================
class BillSequence(TenantScopedModel):
    """
    Manages unique, sequential numbering for Vendor Bills within a specific company.
    Supports optional periodic reset of numbering (e.g., yearly, monthly).
    """
    prefix = models.CharField(
        _("Prefix"), max_length=20, default="BILL-", blank=True,
        help_text=_("Static prefix for generated bill numbers (e.g., 'BILL-', 'SUP-INV-').")
    )
    period_format_for_reset = models.CharField(
        _("Period Format for Reset"), max_length=10, blank=True, null=True,
        help_text=_(
            "Optional strftime format for period-based reset (e.g., '%Y' for yearly, '%Y%m' for monthly). Leave blank for continuous numbering across all periods for this prefix.")
    )
    current_period_key = models.CharField(
        _("Current Period Key"), max_length=20, blank=True, null=True, db_index=True, editable=False,
        help_text=_(
            "Internal: Stores the formatted period string (e.g., '2024' or '202405') if periodic reset is used. This key, along with company and prefix, defines a unique sequence counter.")
    )
    padding_digits = models.PositiveSmallIntegerField(
        _("Padding Digits"), default=5, validators=[MinValueValidator(1)],
        help_text=_(
            "Total number of digits for the sequential numeric part, including leading zeros (e.g., 5 for '00001'). Minimum 1.")
    )
    current_number = models.PositiveIntegerField(
        _("Current Number"), default=0,
        help_text=_("The last sequential number issued for the current company, prefix, and period_key combination.")
    )

    class Meta:
        verbose_name = _("Vendor Bill Sequence")
        verbose_name_plural = _("Vendor Bill Sequences")
        # Ensures that for a given company, prefix, and (if used) period key, the sequence counter is unique.
        unique_together = (('company', 'prefix', 'current_period_key'),)
        ordering = ['company__name', 'prefix', '-current_period_key']  # Show most recent period key first if used

    def __str__(self) -> str:
        co_name = _('N/A Co.')
        if self.company_id:
            try:
                co_name = (self.company.name if hasattr(self,
                                                        '_company_cache') and self._company_cache else Company.objects.get(
                    pk=self.company_id).name)
            except ObjectDoesNotExist:
                co_name = f"Co ID {self.company_id}?"

        period_info = ""
        if self.period_format_for_reset:
            period_info = f" (Period Key: {self.current_period_key or 'N/A'})" if self.current_period_key else f" (Format: {self.period_format_for_reset}, Awaiting First Use)"
        else:
            period_info = " (Continuous)"

        return f"BillSeq for {co_name} - Prefix:'{self.prefix}'{period_info} (Next: {self.current_number + 1})"

    def format_number(self, number_val: int) -> str:
        """Formats the given number with the sequence's prefix and padding."""
        padding = int(self.padding_digits) if self.padding_digits is not None and self.padding_digits >= 1 else 1
        num_str = str(max(0, number_val)).zfill(padding)
        return f"{self.prefix}{num_str}"

    def get_period_key_for_date(self, target_date: date) -> Optional[str]:
        """
        Generates a string key representing the period for the target_date,
        based on self.period_format_for_reset. Returns None if no reset format is defined.
        """
        if self.period_format_for_reset and self.period_format_for_reset.strip():
            try:
                return target_date.strftime(self.period_format_for_reset)
            except ValueError:  # Should be caught by clean, but defensive
                logger.error(
                    f"Invalid strftime format '{self.period_format_for_reset}' in BillSequence {self.pk} during get_period_key_for_date.")
                return None  # Or raise an error indicating misconfiguration
        return None  # Indicates continuous numbering, no specific period key relevant for reset

    def clean(self):
        """Validates the sequence configuration."""
        super().clean()  # From TenantScopedModel
        errors = {}
        if self.padding_digits is not None and self.padding_digits < 1:
            errors['padding_digits'] = _("Padding digits must be at least 1.")

        if self.period_format_for_reset:
            stripped_format = self.period_format_for_reset.strip()
            if not stripped_format:  # If only whitespace, treat as None
                self.period_format_for_reset = None
            elif '%' not in stripped_format:
                errors['period_format_for_reset'] = _(
                    "Period format for reset must be a valid strftime string (e.g., '%Y' for yearly, '%Y%m' for monthly) or blank for continuous numbering.")
            else:
                try:
                    # Test the format with a sample date
                    timezone.now().date().strftime(stripped_format)
                except ValueError:
                    errors['period_format_for_reset'] = _(
                        "The provided period format string is invalid for date formatting.")
        if errors: raise DjangoValidationError(errors)

    # save() is inherited from TenantScopedModel, which calls full_clean().


class PaymentSequence(TenantScopedModel):
    """
    Manages unique, sequential numbering for Vendor Payments within a specific company.
    Supports optional periodic reset of numbering.
    (Structure is identical to BillSequence, just for payments)
    """
    prefix = models.CharField(_("Prefix"), max_length=20, default="VPAY-")
    period_format_for_reset = models.CharField(_("Period Format for Reset"), max_length=10, blank=True, null=True,
                                               help_text=_("Strftime format (e.g., '%Y'). Blank for continuous."))
    current_period_key = models.CharField(_("Current Period Key"), max_length=20, blank=True, null=True, db_index=True,
                                          editable=False)
    padding_digits = models.PositiveSmallIntegerField(_("Padding Digits"), default=5, validators=[MinValueValidator(1)])
    current_number = models.PositiveIntegerField(_("Current Number"), default=0)

    class Meta:
        verbose_name = _("Vendor Payment Sequence")
        verbose_name_plural = _("Vendor Payment Sequences")
        unique_together = (('company', 'prefix', 'current_period_key'),)
        ordering = ['company__name', 'prefix', '-current_period_key']

    def __str__(self) -> str:
        co_name = _('N/A Co.')
        if self.company_id:
            try:
                co_name = (self.company.name if hasattr(self,
                                                        '_company_cache') and self._company_cache else Company.objects.get(
                    pk=self.company_id).name)
            except ObjectDoesNotExist:
                co_name = f"Co ID {self.company_id}?"
        period_info = f" (Period: {self.current_period_key})" if self.current_period_key else " (Continuous)"
        return f"PaySeq for {co_name} - Prefix:'{self.prefix}'{period_info} (Next: {self.current_number + 1})"

    def format_number(self, number_val: int) -> str:
        padding = int(self.padding_digits) if self.padding_digits is not None and self.padding_digits >= 1 else 1
        num_str = str(max(0, number_val)).zfill(padding);
        return f"{self.prefix}{num_str}"

    def get_period_key_for_date(self, target_date: date) -> Optional[str]:
        if self.period_format_for_reset and self.period_format_for_reset.strip():
            try:
                return target_date.strftime(self.period_format_for_reset)
            except ValueError:
                logger.error(
                    f"Invalid strftime format '{self.period_format_for_reset}' in PaymentSequence {self.pk}."); return None
        return None

    def clean(self):
        super().clean();
        errors = {}
        if self.padding_digits is not None and self.padding_digits < 1: errors['padding_digits'] = _(
            "Padding digits must be at least 1.")
        if self.period_format_for_reset:
            stripped_format = self.period_format_for_reset.strip()
            if not stripped_format:
                self.period_format_for_reset = None
            elif '%' not in stripped_format:
                errors['period_format_for_reset'] = _("Period format must be valid strftime or blank.")
            else:
                try:
                    timezone.now().date().strftime(stripped_format)
                except ValueError:
                    errors['period_format_for_reset'] = _("Invalid strftime format string.")
        if errors: raise DjangoValidationError(errors)


# =============================================================================
# VendorBill Model
# =============================================================================
class VendorBill(TenantScopedModel):
    class BillStatus(models.TextChoices):  # Defined here for local use
        DRAFT = 'DRAFT', _('Draft')
        SUBMITTED_FOR_APPROVAL = 'SUBMITTED', _('Submitted for Approval')  # More explicit
        APPROVED = 'APPROVED', _('Approved')
        PARTIALLY_PAID = 'PARTIALLY_PAID', _('Partially Paid')
        PAID = 'PAID', _('Paid')
        VOID = 'VOID', _('Void')

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='vendor_bills',
                                verbose_name=_("Company"))
    supplier = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='bills_as_supplier',
                                 limit_choices_to={'party_type': PartyType.SUPPLIER.value}, verbose_name=_("Supplier"))
    bill_number = models.CharField(_("Bill Number (System)"), max_length=50, db_index=True, blank=True, null=True,
                                   help_text=_("System generated unique bill number."))
    supplier_bill_reference = models.CharField(_("Supplier Bill Reference"), max_length=100, blank=True, null=True,
                                               db_index=True, help_text=_("Supplier's own invoice/bill number."))
    issue_date = models.DateField(_("Issue Date"), default=timezone.now)
    due_date = models.DateField(_("Due Date"), blank=True, null=True)
    currency = models.CharField(_("Currency"), max_length=10,
                                choices=CurrencyType.choices if CurrencyType else None)  # From crp_core
    subtotal_amount = models.DecimalField(_("Subtotal (Excl. Tax)"), max_digits=20, decimal_places=2,
                                          default=ZERO_DECIMAL, editable=False)
    tax_amount = models.DecimalField(_("Tax Amount"), max_digits=20, decimal_places=2, default=ZERO_DECIMAL,
                                     editable=False)
    total_amount = models.DecimalField(_("Total Amount (Incl. Tax)"), max_digits=20, decimal_places=2,
                                       default=ZERO_DECIMAL, editable=False)
    amount_paid = models.DecimalField(_("Amount Paid"), max_digits=20, decimal_places=2, default=ZERO_DECIMAL,
                                      editable=False)
    amount_due = models.DecimalField(_("Amount Due"), max_digits=20, decimal_places=2, default=ZERO_DECIMAL,
                                     editable=False)
    status = models.CharField(_("Status"), max_length=20, choices=BillStatus.choices, default=BillStatus.DRAFT.value,
                              db_index=True)
    notes = models.TextField(_("Internal Notes"), blank=True, null=True)
    related_gl_voucher = models.OneToOneField(Voucher, on_delete=models.SET_NULL, blank=True, null=True,
                                              related_name='related_vendor_bill', verbose_name=_("Related GL Voucher"))
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Approved By"), on_delete=models.SET_NULL,
                                    related_name='approved_vendor_bills', null=True, blank=True, editable=False)
    approved_at = models.DateTimeField(_("Approved At"), null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("Vendor Bill");
        verbose_name_plural = _("Vendor Bills");
        ordering = ['company', '-issue_date', '-created_at']
        # Bill number (system generated) should be unique per company if not blank
        # Supplier bill reference should be unique for that supplier within the company
        unique_together = [('company', 'supplier', 'supplier_bill_reference'), ('company', 'bill_number')]
        constraints = [models.CheckConstraint(check=Q(bill_number__isnull=False) | Q(status=BillStatus.DRAFT.value),
                                              name='non_draft_bill_must_have_number',
                                              violation_error_message=_("Non-draft bills must have a bill number."))]

    def __str__(self):
        # Use the system number first, then the supplier's number, then a fallback.
        # This avoids adding the word "Bill" twice.
        identifier = self.bill_number or self.supplier_bill_reference or f"Draft Bill #{self.pk}"

        # Safely get the supplier's name.
        supplier_name = "N/A Supplier"
        if self.supplier:
            supplier_name = self.supplier.name

        return f"{identifier} - {supplier_name} ({self.total_amount} {self.currency})"

    def _recalculate_derived_fields(self, perform_save: bool = False, _triggering_save: bool = False):
        """Recalculates all monetary sum fields from lines and payments. Optionally saves."""
        if not self.pk: return
        logger.debug(f"VendorBill PK {self.pk}: Recalculating derived fields (save={perform_save}).")
        line_agg = self.lines.all().aggregate(sub=Sum('amount', default=ZERO_DECIMAL),
                                              tax=Sum('tax_amount_on_line', default=ZERO_DECIMAL))
        new_sub = line_agg['sub'] or ZERO_DECIMAL;
        new_tax = line_agg['tax'] or ZERO_DECIMAL;
        new_total = new_sub + new_tax
        new_paid = self.payment_allocations.all().aggregate(sum_alloc=Sum('allocated_amount', default=ZERO_DECIMAL))[
                       'sum_alloc'] or ZERO_DECIMAL
        new_due = new_total - new_paid

        changed = {}
        if self.subtotal_amount != new_sub: self.subtotal_amount = new_sub; changed['subtotal_amount'] = new_sub
        if self.tax_amount != new_tax: self.tax_amount = new_tax; changed['tax_amount'] = new_tax
        if self.total_amount != new_total: self.total_amount = new_total; changed['total_amount'] = new_total
        if self.amount_paid != new_paid: self.amount_paid = new_paid; changed['amount_paid'] = new_paid
        if self.amount_due != new_due: self.amount_due = new_due; changed['amount_due'] = new_due

        if changed: logger.debug(f"VendorBill PK {self.pk}: Monetary fields changed: {changed}. New Due: {new_due}")

        self.update_payment_status_internal(new_total, new_paid, new_due)  # Update status based on fresh values

        if perform_save and (
                changed or self.status != self._initial_status_for_recalc_save):  # Save if amounts or status changed
            update_fields = list(changed.keys()) + ['status']
            self.save(update_fields=list(set(update_fields)) + ['updated_at'], _recalculating_bill=True)  # Pass flag
            logger.debug(
                f"VendorBill PK {self.pk}: Saved with recalculated fields. Status: {self.get_status_display()}")

    def update_payment_status_internal(self, current_total: Decimal, current_paid: Decimal, current_due: Decimal):
        """Internal helper to update status based on provided current amounts. Does not save."""
        if self.status == self.BillStatus.VOID.value: return  # Do not change VOID status

        self._initial_status_for_recalc_save = self.status  # Store for save comparison in _recalculate
        original_status = self.status
        new_status = original_status

        if current_due <= ZERO_DECIMAL and current_total > ZERO_DECIMAL:  # Paid or overpaid
            new_status = self.BillStatus.PAID.value
        elif current_paid > ZERO_DECIMAL and current_due > ZERO_DECIMAL:  # Partially paid
            new_status = self.BillStatus.PARTIALLY_PAID.value
        elif current_paid == ZERO_DECIMAL:  # No payments made
            # If it was previously paid/partially paid and now amounts are zero (e.g., payment voided)
            # revert to APPROVED or SUBMITTED, not DRAFT, if it was already past DRAFT.
            if original_status in [self.BillStatus.PAID.value, self.BillStatus.PARTIALLY_PAID.value]:
                new_status = self.BillStatus.APPROVED.value  # Or SUBMITTED if that was the previous non-paid state
            # If it's DRAFT or SUBMITTED, it remains so if no payment.
            # If it's APPROVED and no payment, it remains APPROVED.
            elif original_status not in [self.BillStatus.DRAFT.value, self.BillStatus.SUBMITTED_FOR_APPROVAL.value,
                                         self.BillStatus.APPROVED.value]:
                new_status = self.BillStatus.APPROVED.value  # Fallback if in an unexpected state after payment removal

        if self.status != new_status:
            self.status = new_status
            logger.info(
                f"VendorBill PK {self.pk}: Status evaluated from '{original_status}' to '{new_status}'. Due: {current_due}")

    def clean(self):
        super().clean()
        errors = {}
        effective_company: Optional[Company] = self.company
        if not effective_company and self.company_id:
            try:
                effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID.")

        if not effective_company and not self._state.adding:
            errors['company'] = _("Bill company association missing.")
        else:  # Company context is available or being added
            if self.supplier_id:
                try:
                    supplier = Party.objects.select_related('company').get(pk=self.supplier_id)
                    if supplier.company != effective_company: errors['supplier'] = _(
                        "Supplier must belong to bill's company.")
                    if supplier.party_type != PartyType.SUPPLIER.value: errors['supplier'] = _(
                        "Party is not 'Supplier'.")
                    if not supplier.is_active: errors['supplier'] = _("Supplier is inactive.")
                except Party.DoesNotExist:
                    errors['supplier'] = _("Supplier not found.")
            elif not self._state.adding:
                errors['supplier'] = _("Supplier is required.")

            if self.supplier_bill_reference and self.supplier_bill_reference.strip():
                qs = VendorBill.objects.filter(company=effective_company, supplier_id=self.supplier_id,
                                               supplier_bill_reference=self.supplier_bill_reference)
                if self.pk: qs = qs.exclude(pk=self.pk)
                if qs.exists(): errors['supplier_bill_reference'] = _(
                    "This supplier bill reference is already recorded for this supplier and company.")

            if self.status != self.BillStatus.DRAFT.value and (not self.bill_number or not self.bill_number.strip()):
                errors['bill_number'] = _("A system Bill Number is required for non-Draft bills.")
            elif self.bill_number and self.bill_number.strip():
                qs_sys_num = VendorBill.objects.filter(company=effective_company, bill_number=self.bill_number)
                if self.pk: qs_sys_num = qs_sys_num.exclude(pk=self.pk)
                if qs_sys_num.exists(): errors['bill_number'] = _(
                    "This system bill number is already in use for this company.")

        if self.due_date and self.issue_date and self.due_date < self.issue_date: errors['due_date'] = _(
            "Due date cannot be before issue date.")
        if not self.currency:
            if effective_company and hasattr(effective_company, 'default_currency_code'):
                self.currency = effective_company.default_currency_code
            elif self.supplier and hasattr(self.supplier, 'default_currency') and self.supplier.default_currency:
                self.currency = self.supplier.default_currency
            else:
                errors['currency'] = _("Currency is required.")
        if self.total_amount < ZERO_DECIMAL: errors['total_amount'] = _("Total amount cannot be negative.")

        # Check for related_gl_voucher on DRAFT status is more of a service/workflow rule
        if self.status == self.BillStatus.DRAFT.value and self.related_gl_voucher_id:
            errors['status'] = _("DRAFT bills should not yet be linked to a GL Voucher.")

        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.pk:  # New bill
            if not self.currency:  # Default currency logic
                if self.supplier and hasattr(self.supplier, 'default_currency') and self.supplier.default_currency:
                    self.currency = self.supplier.default_currency
                elif self.company and hasattr(self.company, 'default_currency_code'):
                    self.currency = self.company.default_currency_code
                else:
                    self.currency = getattr(CurrencyType, 'USD', None).value if CurrencyType else "USD"  # Fallback
            # Initial amount_due (amount_paid is 0)
            self.amount_due = (self.total_amount or ZERO_DECIMAL) - (self.amount_paid or ZERO_DECIMAL)

        _recalculating = kwargs.pop('_recalculating_bill', False)
        if not kwargs.get('skip_clean', False) and not _recalculating: self.full_clean()
        super().save(*args, **kwargs)
        if not _recalculating and self.pk: self._recalculate_derived_fields(perform_save=True, _triggering_save=True)


# =============================================================================
# BillLine Model (FINAL CORRECTED AND COMMENTED VERSION)
# =============================================================================
class BillLine(TenantScopedModel):
    """
    Represents a single line item on a VendorBill.
    Each line item debits an expense or asset account.
    """
    # Foreign key to Company, inherited from TenantScopedModel but made explicit here
    # to ensure it's not editable in the admin form directly. It is set programmatically.
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='bill_lines_direct', editable=False)

    # The crucial link to the parent VendorBill. CASCADE means if the bill is deleted,
    # its lines are also deleted.
    vendor_bill = models.ForeignKey(VendorBill, on_delete=models.CASCADE, related_name='lines',
                                    verbose_name=_("Vendor Bill"))

    # The account being debited by this line item (e.g., 'Office Supplies', 'Prepaid Rent').
    expense_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='bill_lines_as_expense',
                                        verbose_name=_("Expense/Asset Account"))

    # Core details of the line item.
    description = models.CharField(_("Description"), max_length=255)
    quantity = models.DecimalField(_("Quantity"), max_digits=15, decimal_places=4, default=Decimal('1.0'))
    unit_price = models.DecimalField(_("Unit Price (Excl. Tax)"), max_digits=15, decimal_places=4)
    tax_amount_on_line = models.DecimalField(_("Tax Amount on Line"), max_digits=15, decimal_places=2,
                                             default=ZERO_DECIMAL)
    sequence = models.PositiveIntegerField(_("Line Sequence"), default=0,
                                           help_text=_("Controls the display order of lines."))

    # Calculated fields that are not meant to be edited directly by the user.
    # They are computed in the calculate_amounts() method.
    amount = models.DecimalField(_("Line Amount (Excl. Tax)"), max_digits=15, decimal_places=2, editable=False)
    line_total_inclusive_tax = models.DecimalField(_("Line Total (Incl. Tax)"), max_digits=15, decimal_places=2,
                                                   editable=False)

    class Meta:
        verbose_name = _("Vendor Bill Line")
        verbose_name_plural = _("Vendor Bill Lines")
        ordering = ['vendor_bill', 'sequence', 'id']

    def __str__(self):
        """
        Provides a clear, human-readable string representation of the bill line,
        which is useful in the Django admin and for debugging.
        """
        bill_ref = f"Bill PK:{self.vendor_bill_id}"
        # Check if the parent bill object is fully loaded to get its number
        if hasattr(self, 'vendor_bill') and self.vendor_bill:
            bill_ref = self.vendor_bill.bill_number or self.vendor_bill.supplier_bill_reference or f"Bill PK:{self.vendor_bill.pk}"

        desc = self.description or ""
        # Create a shortened description for brevity
        desc_short = (desc[:40] + '...') if len(desc) > 43 else desc

        return f"Line for {bill_ref}: {desc_short} - {self.line_total_inclusive_tax}"

    def calculate_amounts(self):
        """Helper method to calculate the line's subtotal and total based on quantity, price, and tax."""
        self.amount = ((self.quantity or ZERO_DECIMAL) * (self.unit_price or ZERO_DECIMAL)).quantize(Decimal('0.01'),
                                                                                                     rounding=ROUND_HALF_UP)
        self.line_total_inclusive_tax = (self.amount + (self.tax_amount_on_line or ZERO_DECIMAL)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)

    def clean(self):
        """
        Performs comprehensive model-level validation before an instance is saved.
        This is where we enforce business rules and prevent crashes on new objects.
        """
        super().clean()
        errors = {}

        # --- Basic Validations (don't require database access) ---
        if self.quantity is not None and self.quantity <= ZERO_DECIMAL:
            errors['quantity'] = _("Quantity must be positive.")
        if self.unit_price is not None and self.unit_price < ZERO_DECIMAL:
            errors['unit_price'] = _("Unit price cannot be negative.")
        if not self.expense_account_id:
            errors['expense_account'] = _("Expense/Asset account is required.")
        if errors:
            raise DjangoValidationError(errors)

        # --- Relational Validations (require database access) ---
        # FIX: We only run these checks if the parent bill has been saved to the database (has a pk).
        # This prevents a crash when creating a NEW bill with lines, as the parent doesn't exist yet.
        if hasattr(self, 'vendor_bill') and self.vendor_bill and self.vendor_bill.pk:
            try:
                parent_bill = self.vendor_bill

                # Check that the expense account belongs to the same company as the bill.
                if self.expense_account_id:
                    exp_acc = Account.objects.get(pk=self.expense_account_id)
                    if exp_acc.company_id != parent_bill.company_id:
                        errors['expense_account'] = _("Expense Account must belong to the same company as the Bill.")
            except Account.DoesNotExist:
                errors['expense_account'] = _("The selected Expense/Asset account was not found.")

        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Overrides the default save method to inject critical custom logic.
        1. Inherit the 'company' from the parent bill.
        2. Calculate line amounts.
        3. Trigger recalculation on the parent bill's totals.
        """
        # --- FIX: Inherit Company from Parent ---
        # This is the crucial fix for the "company cannot be null" error. When a BillLine is
        # saved as part of an inline formset, it doesn't automatically know its company.
        # This code explicitly finds its parent bill and copies the company from there.
        if self.vendor_bill_id and not self.company_id:
            try:
                # Most efficient: the formset attaches the parent object directly.
                self.company = self.vendor_bill.company
            except (AttributeError, VendorBill.DoesNotExist):
                # Fallback: if the parent isn't a full object, query the DB.
                try:
                    self.company = VendorBill.objects.get(pk=self.vendor_bill_id).company
                except VendorBill.DoesNotExist:
                    # If parent still can't be found, let the database integrity check handle it.
                    pass
        # --- End of Fix ---

        # Always calculate amounts before saving to ensure data is correct.
        self.calculate_amounts()

        # Call the original save method (from TenantScopedModel), which also runs full_clean().
        super().save(*args, **kwargs)

        # After saving this line, tell the parent bill to update its totals.
        if self.vendor_bill_id and hasattr(self.vendor_bill, '_recalculate_derived_fields'):
            self.vendor_bill._recalculate_derived_fields(perform_save=True)

    def delete(self, *args, **kwargs):
        """Overrides delete to ensure the parent bill's totals are updated."""
        # Store the parent before deleting the line.
        bill_to_update = self.vendor_bill if self.vendor_bill_id else None

        super().delete(*args, **kwargs)

        # After deletion, trigger the recalculation.
        if bill_to_update and hasattr(bill_to_update, '_recalculate_derived_fields'):
            bill_to_update._recalculate_derived_fields(perform_save=True)
# =============================================================================
# VendorPayment Model
# =============================================================================
class VendorPayment(TenantScopedModel):
    class PaymentStatus(models.TextChoices):  # Local enum for VendorPayment
        DRAFT = 'DRAFT', _('Draft')
        PENDING_APPROVAL = 'PENDING_APPROVAL', _('Pending Approval')
        APPROVED_FOR_PAYMENT = 'APPROVED_PAYMENT', _('Approved for Payment')
        PAID_COMPLETED = 'PAID_COMPLETED', _('Paid / Completed')  # GL Posted
        VOID = 'VOID', _('Void')

    class PaymentMethod(models.TextChoices):  # Local enum
        BANK_TRANSFER = 'BANK_TRANSFER', _('Bank Transfer')
        CHECK = 'CHECK', _('Check')
        CREDIT_CARD = 'CREDIT_CARD', _('Credit Card (Company)')
        CASH = 'CASH', _('Cash')
        OTHER = 'OTHER', _('Other')

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='vendor_payments')
    supplier = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='payments_made_to_supplier',
                                 limit_choices_to={'party_type': PartyType.SUPPLIER.value})
    payment_number = models.CharField(_("Payment Number"), max_length=50, db_index=True, blank=True, null=True,
                                      help_text=_("System generated or external reference."))
    payment_date = models.DateField(_("Payment Date"), default=timezone.now)
    payment_method = models.CharField(_("Payment Method"), max_length=30, choices=PaymentMethod.choices, blank=True,
                                      null=True)
    payment_account = models.ForeignKey(Account, verbose_name=_("Paid From Account (Bank/Cash)"),
                                        on_delete=models.PROTECT, related_name='payments_made_from_account',
                                        limit_choices_to=Q(account_type=AccountType.ASSET.value))
    currency = models.CharField(_("Currency"), max_length=10, choices=CurrencyType.choices if CurrencyType else None)
    payment_amount = models.DecimalField(_("Payment Amount"), max_digits=20, decimal_places=2)
    allocated_amount = models.DecimalField(_("Allocated Amount"), max_digits=20, decimal_places=2, default=ZERO_DECIMAL,
                                           editable=False)
    unallocated_amount = models.DecimalField(_("Unallocated Amount"), max_digits=20, decimal_places=2,
                                             default=ZERO_DECIMAL, editable=False)
    status = models.CharField(_("Status"), max_length=20, choices=PaymentStatus.choices,
                              default=PaymentStatus.DRAFT.value, db_index=True)
    reference_details = models.CharField(_("Payment Reference (e.g., Check #)"), max_length=100, blank=True, null=True)
    notes = models.TextField(_("Internal Notes"), blank=True, null=True)
    related_gl_voucher = models.OneToOneField(Voucher, on_delete=models.SET_NULL, blank=True, null=True,
                                              related_name='related_vendor_payment')

    class Meta:
        verbose_name = _("Vendor Payment")
        verbose_name_plural = _("Vendor Payments")
        ordering = ['company', '-payment_date', '-created_at']
        unique_together = [('company', 'payment_number')]

    def __str__(self):
        supp_name = _("N/A Supplier")
        if self.supplier_id:
            try:
                supp_name = (self.supplier.name if hasattr(self, 'supplier') and self.supplier else Party.objects.get(
                    pk=self.supplier_id).name)
            except ObjectDoesNotExist:
                supp_name = f"Supplier ID {self.supplier_id} (Not Found)"
        num = self.payment_number or f"PK:{self.pk or 'New'}"
        return f"Payment {num} to {supp_name} ({self.payment_amount} {self.currency})"

    def _recalculate_derived_fields(self, perform_save: bool = False, _triggering_save: bool = False):
        if not self.pk: return
        logger.debug(f"VendorPayment PK {self.pk}: Recalculating derived fields (save={perform_save}).")

        # Use self.bill_allocations if the related_name is 'bill_allocations'
        # from VendorPaymentAllocation's vendor_payment ForeignKey
        self.allocated_amount = self.bill_allocations.all().aggregate(
            total=Coalesce(Sum('allocated_amount'), ZERO_DECIMAL)
        )['total']

        calculated_unallocated = (self.payment_amount or ZERO_DECIMAL) - self.allocated_amount

        amount_changed = self.unallocated_amount != calculated_unallocated

        if amount_changed:
            self.unallocated_amount = calculated_unallocated
            logger.debug(f"VendorPayment PK {self.pk}: Unallocated amount updated to {self.unallocated_amount}")

        if perform_save and amount_changed and not _triggering_save:
            update_fields = ['allocated_amount', 'unallocated_amount', 'updated_at']
            self.save(update_fields=update_fields, _recalculating_payment=True)
            logger.debug(f"VendorPayment PK {self.pk}: Saved with recalculated amounts.")

    def clean(self):
        super().clean()
        errors = {}
        effective_company: Optional[Company] = getattr(self, 'company', None)
        if not effective_company and self.company_id:
            try:
                effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID.")

        if not effective_company and not self._state.adding and 'company' not in errors:
            errors['company'] = _("Payment company missing.")
        elif effective_company:  # Only proceed if company context is established
            if self.supplier_id:
                try:
                    supplier = Party.objects.select_related('company').get(pk=self.supplier_id)
                    if supplier.company != effective_company: errors['supplier'] = _(
                        "Supplier must belong to payment company.")
                    if supplier.party_type != PartyType.SUPPLIER.value: errors['supplier'] = _(
                        "Party is not 'Supplier'.")
                except Party.DoesNotExist:
                    errors['supplier'] = _("Supplier not found.")
            elif not self._state.adding:
                errors['supplier'] = _("Supplier is required.")

            if self.payment_account_id:
                try:
                    pay_acc = Account.objects.select_related('company').get(pk=self.payment_account_id)
                    if pay_acc.company != effective_company: errors['payment_account'] = _(
                        "Payment Account must belong to company.")
                    if pay_acc.account_type != AccountType.ASSET.value: errors['payment_account'] = _(
                        "Payment Account must be 'Asset' type.")
                except Account.DoesNotExist:
                    errors['payment_account'] = _("Payment Account not found.")
            elif not self._state.adding:
                errors['payment_account'] = _("Payment Account is required.")

            if self.payment_number and self.payment_number.strip():
                qs = VendorPayment.objects.filter(company=effective_company, payment_number=self.payment_number.strip())
                if self.pk: qs = qs.exclude(pk=self.pk)
                if qs.exists(): errors['payment_number'] = _("Payment number already used for this company.")

        if self.payment_amount is not None and self.payment_amount <= ZERO_DECIMAL:
            errors['payment_amount'] = _("Payment amount must be positive.")

        if not self.currency:
            if effective_company and hasattr(effective_company,
                                             'default_currency_code') and effective_company.default_currency_code:
                self.currency = effective_company.default_currency_code
            elif hasattr(self, 'supplier') and self.supplier and hasattr(self.supplier,
                                                                         'default_currency') and self.supplier.default_currency:
                self.currency = self.supplier.default_currency
            elif 'currency' not in errors:  # Avoid overwriting if already errored
                errors['currency'] = _("Currency is required.")

        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        _recalculating = kwargs.pop('_recalculating_payment', False)
        _skip_clean = kwargs.pop('skip_clean', False) or kwargs.pop('skip_model_full_clean', False)

        if not self.pk:  # New payment
            if not self.currency:
                current_supplier = getattr(self, 'supplier', None)
                current_company = getattr(self, 'company', None)
                if current_supplier and hasattr(current_supplier,
                                                'default_currency') and current_supplier.default_currency:
                    self.currency = current_supplier.default_currency
                elif current_company and hasattr(current_company,
                                                 'default_currency_code') and current_company.default_currency_code:
                    self.currency = current_company.default_currency_code
                else:  # Last resort fallback, though clean() should catch missing currency
                    self.currency = getattr(CurrencyType, 'USD', None).value if CurrencyType else "USD"

            self.unallocated_amount = self.payment_amount if self.payment_amount is not None else ZERO_DECIMAL
            self.allocated_amount = ZERO_DECIMAL

        # If it's an existing payment, ensure its amounts are up-to-date before full_clean.
        if self.pk and not _recalculating:
            # This updates self.allocated_amount and self.unallocated_amount in memory
            self._recalculate_derived_fields(perform_save=False)

        if not _skip_clean and not _recalculating:
            try:
                self.full_clean()
            except DjangoValidationError as e:
                logger.error(
                    f"Full clean failed for VendorPayment PK {self.pk}. Status: '{self.status}', Pmt Amt: {self.payment_amount}, Alloc: {self.allocated_amount}, Unalloc: {self.unallocated_amount}. Errors: {e.error_dict}")
                raise

        super().save(*args, **kwargs)

        if not _recalculating and self.pk:
            # Call recalculate again, this time with perform_save=True.
            # This will save if the allocated_amount or unallocated_amount changed
            # due to allocations being processed elsewhere or if the status needs update
            # after the main save.
            self._recalculate_derived_fields(perform_save=True, _triggering_save=True)


# =============================================================================
# VendorPaymentAllocation Model (FINAL CORRECTED AND COMMENTED VERSION)
# =============================================================================
class VendorPaymentAllocation(TenantScopedModel):
    vendor_payment = models.ForeignKey(VendorPayment, on_delete=models.CASCADE, related_name='bill_allocations',
                                       verbose_name=_("Vendor Payment"))
    vendor_bill = models.ForeignKey(VendorBill, on_delete=models.CASCADE, related_name='payment_allocations',
                                    verbose_name=_("Vendor Bill"))
    allocated_amount = models.DecimalField(_("Allocated Amount"), max_digits=20, decimal_places=2,
                                           validators=[MinValueValidator(SMALL_TOLERANCE)])
    allocation_date = models.DateField(_("Allocation Date"), default=timezone.now)

    class Meta:
        verbose_name = _("Vendor Payment Allocation")
        verbose_name_plural = _("Vendor Payment Allocations")
        unique_together = (('vendor_payment', 'vendor_bill'),)
        ordering = ['-allocation_date']

    def __str__(self):
        p_ref = f"PmtPK:{self.vendor_payment_id}"
        b_ref = f"BillPK:{self.vendor_bill_id}"
        return f"Allocation: {p_ref} to {b_ref} - Amt: {self.allocated_amount}"

    def clean(self):
        super().clean()
        errors = {}

        if not (self.vendor_payment_id and self.vendor_bill_id):
            return

        try:
            payment_instance = self.vendor_payment
            bill_instance = self.vendor_bill
        except (VendorPayment.DoesNotExist, VendorBill.DoesNotExist):
            return

        # --- Standard cross-object validation ---
        if payment_instance.company_id != bill_instance.company_id:
            errors['vendor_bill'] = _("Payment and Bill must belong to the same company.")
        if payment_instance.supplier_id != bill_instance.supplier_id:
            errors['vendor_bill'] = _("Payment and Bill must be for the same supplier.")
        if payment_instance.currency != bill_instance.currency:
            errors['vendor_bill'] = _("Payment and Bill must have the same currency.")

        # --- FIX: Robust, stateless amount validation ---
        if self.allocated_amount is not None and self.allocated_amount > ZERO_DECIMAL:
            # 1. Check against available amount on the payment
            available_on_payment = ZERO_DECIMAL
            # This is the key fix: Check if the parent payment has a primary key (is saved).
            if payment_instance.pk:
                # If the parent is saved, we can safely query its other allocations.
                other_allocations_sum = \
                payment_instance.bill_allocations.exclude(pk=self.pk).aggregate(s=Sum('allocated_amount'))[
                    's'] or ZERO_DECIMAL
                available_on_payment = (payment_instance.payment_amount or ZERO_DECIMAL) - other_allocations_sum
            else:
                # If the parent is NEW, we can't query its allocations. The total available
                # is simply the full amount of the new payment being created.
                available_on_payment = payment_instance.payment_amount or ZERO_DECIMAL

            if self.allocated_amount > (available_on_payment + SMALL_TOLERANCE):
                errors['allocated_amount'] = _(
                    "Amount applied (%(applied)s) exceeds payment's available amount (%(available)s)."
                ) % {'applied': self.allocated_amount, 'available': available_on_payment}

            # 2. Check against bill's due amount
            other_allocations_on_bill = \
            bill_instance.payment_allocations.exclude(pk=self.pk).aggregate(s=Sum('allocated_amount'))[
                's'] or ZERO_DECIMAL
            bill_due = (bill_instance.total_amount or ZERO_DECIMAL) - other_allocations_on_bill
            if self.allocated_amount > (bill_due + SMALL_TOLERANCE):
                errors['allocated_amount'] = _(
                    "Amount applied (%(applied)s) exceeds bill's due amount (%(due)s)."
                ) % {'applied': self.allocated_amount, 'due': bill_due}

        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        # --- FIX: Inherit Company from Parent ---
        # This is the same critical fix as in BillLine. It ensures the 'company' field
        # is set before the model is saved to the database.
        if self.vendor_payment_id and not self.company_id:
            try:
                self.company = self.vendor_payment.company
            except (AttributeError, VendorPayment.DoesNotExist):
                try:
                    self.company = VendorPayment.objects.get(pk=self.vendor_payment_id).company
                except VendorPayment.DoesNotExist:
                    pass
        # --- End of Fix ---

        super().save(*args, **kwargs)

        if self.vendor_payment_id:
            self.vendor_payment._recalculate_derived_fields(perform_save=True)
        if self.vendor_bill_id:
            self.vendor_bill._recalculate_derived_fields(perform_save=True)

    def delete(self, *args, **kwargs):
        payment = self.vendor_payment
        bill = self.vendor_bill
        super().delete(*args, **kwargs)
        if payment:
            payment._recalculate_derived_fields(perform_save=True)
        if bill:
            bill._recalculate_derived_fields(perform_save=True)