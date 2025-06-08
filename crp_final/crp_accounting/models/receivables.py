# crp_accounting/models/receivables.py

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.conf import settings

# --- Base Model & Company Import ---
try:
    from .base import TenantScopedModel
    from company.models import Company
except ImportError:
    # This is a critical error, so re-raising is appropriate.
    raise ImportError(
        "Could not import TenantScopedModel or Company. Critical dependency missing for receivables models."
    )

# --- Related Accounting Model Imports ---
try:
    from .party import Party
    from .coa import Account
    from .journal import Voucher
except ImportError as e:
    raise ImportError(f"Could not import related accounting models for receivables: {e}.")

ZERO_DECIMAL = Decimal('0.00')
# --- Enum Imports ---
# Fallback mock for enums if crp_core is not fully available
class BaseEnumMock:
    @classmethod
    def choices(cls): return []

    @classmethod
    def values(cls): return []  # If your code uses .values

    # Mock common enum members if defaults rely on them
    # Ensure these string values match the actual enum values if they are TextChoices/CharFields
    DRAFT = 'DRAFT'
    UNAPPLIED = 'UNAPPLIED'
    CUSTOMER = 'CUSTOMER'  # For CorePartyType
    INCOME = 'INCOME'  # For CoreAccountType
    ASSET = 'ASSET'  # For CoreAccountType
    VOID = 'VOID'
    CANCELLED = 'CANCELLED'
    PAID = 'PAID'
    PARTIALLY_PAID = 'PARTIALLY_PAID'
    OVERDUE = 'OVERDUE'
    SENT = 'SENT'
    APPLIED = 'APPLIED'


try:
    from crp_core.enums import (
        PartyType as CorePartyType,
        AccountType as CoreAccountType,
        CurrencyType,
        InvoiceStatus,
        PaymentStatus,
        PaymentMethod
    )

    # Handle case where crp_core.enums exists but a specific enum is None
    if CurrencyType is None:
        logging.warning("Receivables Models: CurrencyType from crp_core.enums is None. Using mock.")
        CurrencyType = BaseEnumMock
    if CorePartyType is None: CorePartyType = BaseEnumMock
    if CoreAccountType is None: CoreAccountType = BaseEnumMock
    if InvoiceStatus is None: InvoiceStatus = BaseEnumMock
    if PaymentStatus is None: PaymentStatus = BaseEnumMock
    if PaymentMethod is None: PaymentMethod = BaseEnumMock

except ImportError:
    logging.warning(
        "Receivables Models: Could not import enums from crp_core.enums. Using mocks. Functionality may be limited."
    )
    CorePartyType = BaseEnumMock
    CoreAccountType = BaseEnumMock
    CurrencyType = BaseEnumMock
    InvoiceStatus = BaseEnumMock
    PaymentStatus = BaseEnumMock
    PaymentMethod = BaseEnumMock

logger = logging.getLogger("crp_accounting.models.receivables")
ZERO = Decimal('0.00')
SMALL_TOLERANCE = Decimal('0.005')  # For float comparisons


# =============================================================================
# InvoiceSequence Model (Tenant Scoped)
# =============================================================================
class InvoiceSequence(TenantScopedModel):
    prefix = models.CharField(_("Invoice Prefix"), max_length=20, default="INV-", blank=True)
    period_format_for_reset = models.CharField(
        _("Period Format for Reset"), max_length=10, blank=True, null=True,
        help_text=_("Strftime format (e.g., '%Y' yearly, '%Y%m' monthly). Blank for continuous.")
    )
    current_period_key = models.CharField(
        _("Current Period Key"), max_length=20, blank=True, null=True, db_index=True, editable=False
    )
    padding_digits = models.PositiveSmallIntegerField(_("Padding Digits"), default=5, validators=[MinValueValidator(1)])
    last_number = models.PositiveIntegerField(_("Last Number Used"), default=0)

    class Meta:
        verbose_name = _("Invoice Sequence Configuration")
        verbose_name_plural = _("Invoice Sequence Configurations")
        unique_together = (('company', 'prefix', 'current_period_key'),)
        ordering = ['company__name', 'prefix', '-current_period_key']

    def __str__(self):
        co_name = _('N/A Co.')
        if self.company_id:
            try:
                # Prefer loaded 'company' attribute if available
                co_name = (self.company.name if hasattr(self, 'company') and self.company else Company.objects.get(
                    pk=self.company_id).name)
            except ObjectDoesNotExist:
                co_name = f"Co ID {self.company_id} (Not Found)"
        period_info = f" (Period: {self.current_period_key})" if self.current_period_key else " (Continuous)"
        return f"InvSeq for {co_name} - Prefix: '{self.prefix}'{period_info}"

    def format_number(self, number_val: int) -> str:
        padding = int(self.padding_digits) if self.padding_digits is not None and self.padding_digits >= 1 else 1
        num_str = str(max(0, number_val)).zfill(padding)
        return f"{self.prefix}{num_str}"

    def get_period_key_for_date(self, target_date: date) -> Optional[str]:
        if self.period_format_for_reset and self.period_format_for_reset.strip():
            try:
                return target_date.strftime(self.period_format_for_reset.strip())
            except ValueError:
                logger.error(f"Invalid strftime format '{self.period_format_for_reset}' in InvoiceSequence {self.pk}.")
                return None
        return None

    def clean(self):
        super().clean()
        errors = {}
        if self.padding_digits is not None and self.padding_digits < 1:
            errors['padding_digits'] = _("Padding digits must be at least 1.")

        if self.period_format_for_reset:
            cleaned_format = self.period_format_for_reset.strip()
            if not cleaned_format:
                self.period_format_for_reset = None  # Standardize to None if only whitespace
            elif '%' not in cleaned_format:
                errors['period_format_for_reset'] = _(
                    "Period format must be a valid strftime string (e.g., '%Y') or blank.")
            else:
                try:
                    timezone.now().date().strftime(cleaned_format)
                except ValueError:
                    errors['period_format_for_reset'] = _("Invalid strftime format string provided.")
        if errors: raise DjangoValidationError(errors)

    # save() inherited from TenantScopedModel, which calls full_clean()


# =============================================================================
# CustomerInvoice Model
# =============================================================================
class CustomerInvoice(TenantScopedModel):
    customer = models.ForeignKey(Party, verbose_name=_("Customer"), on_delete=models.PROTECT, related_name='invoices',
                                 help_text=_("Customer from same company."))
    invoice_number = models.CharField(_("Invoice Number"), max_length=50, db_index=True, blank=True,
                                      help_text=_("Unique invoice number (system or manual)."))
    invoice_date = models.DateField(_("Invoice Date"), default=timezone.now, db_index=True)
    due_date = models.DateField(_("Due Date"), db_index=True)
    terms = models.TextField(_("Payment Terms"), blank=True)
    notes_to_customer = models.TextField(_("Notes to Customer"), blank=True)
    internal_notes = models.TextField(_("Internal Notes"), blank=True)
    subtotal_amount = models.DecimalField(_("Subtotal"), max_digits=20, decimal_places=2, default=ZERO, editable=False)
    tax_amount = models.DecimalField(_("Tax Amount"), max_digits=20, decimal_places=2, default=ZERO, editable=False)
    total_amount = models.DecimalField(_("Total Amount"), max_digits=20, decimal_places=2, default=ZERO, editable=False)
    amount_paid = models.DecimalField(_("Amount Paid"), max_digits=20, decimal_places=2, default=ZERO, editable=False)
    amount_due = models.DecimalField(_("Amount Due"), max_digits=20, decimal_places=2, default=ZERO, editable=False)
    currency = models.CharField(_("Currency"), max_length=10, choices=CurrencyType.choices)
    status = models.CharField(
        _("Invoice Status"), max_length=20, choices=InvoiceStatus.choices,
        default=getattr(InvoiceStatus, 'DRAFT', 'DRAFT'),  # Robust default
        db_index=True
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='created_invoices', editable=False)
    related_gl_voucher = models.OneToOneField(
        'crp_accounting.Voucher',
        verbose_name=_("Related GL Voucher"), on_delete=models.SET_NULL,
        null=True, blank=True, editable=False, related_name='source_customer_invoice'
    )

    class Meta:
        verbose_name = _("Customer Invoice")
        verbose_name_plural = _("Customer Invoices")
        unique_together = (('company', 'invoice_number'),)
        ordering = ['company__name', '-invoice_date', '-created_at']
        indexes = [models.Index(fields=['company', 'customer', 'invoice_date'], name='custinv_co_cust_date_idx'),
                   models.Index(fields=['company', 'status', 'due_date'], name='custinv_co_stat_due_idx')]

    def __str__(self):
        cust_name = _("N/A Customer")
        if self.customer_id:
            try:
                cust_name = (self.customer.name if hasattr(self, 'customer') and self.customer else Party.objects.get(
                    pk=self.customer_id).name)
            except ObjectDoesNotExist:
                cust_name = f"Cust ID {self.customer_id} (Not Found)"
        inv_num = self.invoice_number or (_("Draft Inv (PK:%(pk)s)") % {'pk': (self.pk or "New")})
        return f"{inv_num} - {cust_name}"

    def _recalculate_totals_and_due(self, perform_save: bool = False):
        if not self.pk: return

        logger.debug(f"Invoice PK {self.pk}: Recalculating totals and due amount (perform_save={perform_save}).")
        line_agg = self.lines.all().aggregate(
            sum_line_total=Sum('line_total', default=ZERO),
            sum_tax_amount=Sum('tax_amount_on_line', default=ZERO)
        )
        new_subtotal = line_agg['sum_line_total'] or ZERO
        new_tax = line_agg['sum_tax_amount'] or ZERO
        new_total = new_subtotal + new_tax

        new_paid = self.payment_allocations.all().aggregate(
            sum_applied=Sum('amount_applied', default=ZERO)
        )['sum_applied'] or ZERO
        new_due = new_total - new_paid

        changed_fields = []
        if self.subtotal_amount != new_subtotal: self.subtotal_amount = new_subtotal; changed_fields.append(
            'subtotal_amount')
        if self.tax_amount != new_tax: self.tax_amount = new_tax; changed_fields.append('tax_amount')
        if self.total_amount != new_total: self.total_amount = new_total; changed_fields.append('total_amount')
        if self.amount_paid != new_paid: self.amount_paid = new_paid; changed_fields.append('amount_paid')
        if self.amount_due != new_due: self.amount_due = new_due; changed_fields.append('amount_due')

        if changed_fields:
            logger.debug(f"Invoice PK {self.pk}: Monetary fields changed: {changed_fields}. New Due: {new_due}")
            if perform_save:
                update_fields_list = changed_fields + ['updated_at']
                self.save(update_fields=update_fields_list)
        # After totals are updated (in memory or saved), update status
        self.update_payment_status(save_instance=perform_save)

    def update_payment_status(self, save_instance: bool = False):
        void_val = getattr(InvoiceStatus, 'VOID', 'VOID')
        cancelled_val = getattr(InvoiceStatus, 'CANCELLED', 'CANCELLED')
        paid_val = getattr(InvoiceStatus, 'PAID', 'PAID')
        partially_paid_val = getattr(InvoiceStatus, 'PARTIALLY_PAID', 'PARTIALLY_PAID')
        draft_val = getattr(InvoiceStatus, 'DRAFT', 'DRAFT')
        overdue_val = getattr(InvoiceStatus, 'OVERDUE', 'OVERDUE')
        sent_val = getattr(InvoiceStatus, 'SENT', 'SENT')

        if self.status in [void_val, cancelled_val]: return

        original_status = self.status
        new_status = original_status
        # Use current in-memory values, which should have been updated by _recalculate_totals_and_due if called prior
        current_due = (self.total_amount or ZERO) - (self.amount_paid or ZERO)

        if current_due <= ZERO:  # Consider a small tolerance if necessary, e.g. current_due <= SMALL_TOLERANCE
            new_status = paid_val
        elif (self.amount_paid or ZERO) > ZERO:
            new_status = partially_paid_val
        else:  # No payments
            if self.status == draft_val:  # If it's already a draft, keep it as draft
                new_status = draft_val
            elif self.due_date and self.due_date < timezone.now().date():
                new_status = overdue_val
            else:
                # If not draft, not paid, not overdue, assume it's sent (or ready to be)
                new_status = sent_val

        status_changed = self.status != new_status
        # amount_due field should always reflect the calculation
        amount_due_changed = self.amount_due != current_due

        if status_changed or amount_due_changed:
            self.status = new_status
            self.amount_due = current_due  # Ensure amount_due is set to the calculated value
            if status_changed: logger.info(
                f"Invoice PK {self.pk}: Status '{original_status}'->'{new_status}'. Due:{self.amount_due}")
            if amount_due_changed and not status_changed: logger.debug(
                f"Invoice PK {self.pk}: Amount Due updated to {self.amount_due} (status '{self.status}' unchanged).")

            if save_instance:
                update_fields = []
                if status_changed: update_fields.append('status')
                if amount_due_changed: update_fields.append('amount_due')
                if update_fields:
                    update_fields_list = update_fields + ['updated_at']
                    self.save(update_fields=update_fields_list)

    def clean(self):
        super().clean()
        errors = {}
        # self.company should be set by TenantScopedModel.clean() or .save()
        effective_company: Optional[Company] = self.company
        if not effective_company and self.company_id:
            try:
                effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID on invoice.")

        if not effective_company:
            if not self._state.adding and 'company' not in errors:
                errors['company'] = _("Invoice company association missing.")
        else:  # Only do these checks if effective_company is determined
            if self.customer_id:
                try:
                    customer = Party.objects.select_related('company').get(pk=self.customer_id)
                    if customer.company != effective_company:
                        errors['customer'] = _("Customer must belong to the same company as the invoice.")
                    if customer.party_type != getattr(CorePartyType, 'CUSTOMER', 'CUSTOMER'):
                        errors['customer'] = _("Party selected as customer is not of 'Customer' type.")
                    if not customer.is_active:
                        errors['customer'] = _("Selected customer is inactive.")
                except Party.DoesNotExist:
                    errors['customer'] = _("Selected customer not found.")
            elif not self._state.adding:  # customer is required for existing records
                errors['customer'] = _("Customer is required for the invoice.")

            if self.invoice_number and self.invoice_number.strip():
                qs = CustomerInvoice.objects.filter(company=effective_company,
                                                    invoice_number=self.invoice_number.strip())
                if self.pk: qs = qs.exclude(pk=self.pk)
                if qs.exists():
                    errors['invoice_number'] = _("This invoice number is already in use for this company.")

        if self.due_date and self.invoice_date and self.due_date < self.invoice_date:
            errors['due_date'] = _("Due date cannot be before the invoice date.")

        if not self.currency:
            if effective_company and hasattr(effective_company,
                                             'default_currency_code') and effective_company.default_currency_code:
                self.currency = effective_company.default_currency_code
            elif 'currency' not in errors:  # Avoid overwriting other currency errors
                errors['currency'] = _("Currency is required for the invoice.")
        if errors: raise DjangoValidationError(errors)

    # save() inherited from TenantScopedModel, which calls full_clean()


# =============================================================================
# InvoiceLine Model
# =============================================================================
class InvoiceLine(TenantScopedModel):
    invoice = models.ForeignKey(CustomerInvoice, verbose_name=_("Invoice"), on_delete=models.CASCADE,
                                related_name='lines')
    description = models.TextField(_("Description/Service"))
    quantity = models.DecimalField(_("Quantity"), max_digits=12, decimal_places=2, default=Decimal('1.0'))
    unit_price = models.DecimalField(_("Unit Price"), max_digits=20, decimal_places=2)
    line_total = models.DecimalField(_("Line Total (Pre-tax)"), max_digits=20, decimal_places=2, editable=False)
    tax_amount_on_line = models.DecimalField(_("Tax on Line"), max_digits=20, decimal_places=2, default=ZERO)
    revenue_account = models.ForeignKey(Account, verbose_name=_("Revenue Account"), on_delete=models.PROTECT,
                                        related_name='invoice_lines_revenue',
                                        help_text=_("Income account, same company as invoice."))

    class Meta:
        verbose_name = _("Invoice Line")
        verbose_name_plural = _("Invoice Lines")
        ordering = ['pk']

    def __str__(self):
        inv_ref = f"InvID:{self.invoice_id or 'N/A'}"
        if hasattr(self, 'invoice') and self.invoice:  # Check if invoice relation is loaded
            inv_ref = self.invoice.invoice_number or f"InvPK:{self.invoice.pk or 'New'}"

        desc_preview = (self.description[:47] + "...") if self.description and len(
            self.description) > 50 else self.description
        return f"Line for {inv_ref}: {desc_preview}"

    def clean(self):
        super().clean()
        errors = {}

        if self.quantity is not None and self.quantity <= ZERO:
            errors['quantity'] = _("Quantity must be positive.")
        if self.unit_price is not None and self.unit_price < ZERO:  # Allow zero unit price
            errors['unit_price'] = _("Unit price cannot be negative.")
        if self.tax_amount_on_line is not None and self.tax_amount_on_line < ZERO:
            errors['tax_amount_on_line'] = _("Tax amount on line cannot be negative.")

        # Revenue Account validation
        if not self.revenue_account_id:
            errors['revenue_account'] = _("Revenue account is required.")
        else:
            try:
                # Use self.revenue_account if already fetched (e.g., by form)
                acc = self.revenue_account if hasattr(self,
                                                      'revenue_account') and self.revenue_account else Account.objects.get(
                    pk=self.revenue_account_id)

                expected_income_type = getattr(CoreAccountType, 'INCOME', 'INCOME')
                if acc.account_type != expected_income_type:
                    errors['revenue_account'] = _("Revenue Account must be of type '%(type)s'.") % {
                        'type': expected_income_type}
                if not acc.is_active or not acc.allow_direct_posting:
                    errors['revenue_account'] = _(
                        "Selected Revenue Account is inactive or does not allow direct posting.")
            except Account.DoesNotExist:
                errors['revenue_account'] = _("Invalid Revenue Account selected (ID not found).")

        # Cross-validation with Invoice (Company match)
        # This check is done if both FKs are present. Formsets might handle cases where invoice_id isn't set yet for new objects.
        if self.invoice_id and self.revenue_account_id and 'revenue_account' not in errors:
            try:
                # Prefer using already loaded instances if available
                parent_invoice = self.invoice if hasattr(self,
                                                         'invoice') and self.invoice else CustomerInvoice.objects.select_related(
                    'company').get(pk=self.invoice_id)
                revenue_acc = self.revenue_account if hasattr(self,
                                                              'revenue_account') and self.revenue_account else Account.objects.select_related(
                    'company').get(pk=self.revenue_account_id)

                if not parent_invoice.company:  # Should be guaranteed by CustomerInvoice model
                    if 'invoice' not in errors: errors['invoice'] = _("Parent invoice lacks company information.")
                if not revenue_acc.company:  # Should be guaranteed by Account model
                    if 'revenue_account' not in errors: errors['revenue_account'] = _(
                        "Revenue account lacks company information.")

                if parent_invoice.company and revenue_acc.company and parent_invoice.company != revenue_acc.company:
                    errors['revenue_account'] = _(
                        "Revenue Account's company (%(acc_co)s) must match the Invoice's company (%(inv_co)s)."
                    ) % {'acc_co': revenue_acc.company.name, 'inv_co': parent_invoice.company.name}

            except CustomerInvoice.DoesNotExist:
                if 'invoice' not in errors: errors['invoice'] = _("Associated invoice not found for line validation.")
            except Account.DoesNotExist:  # Should have been caught above
                if 'revenue_account' not in errors: errors['revenue_account'] = _(
                    "Associated revenue account not found for line validation.")
        elif not self.invoice_id and not self._state.adding:  # If invoice_id is missing on an existing record
            errors['invoice'] = _("Line must be associated with an invoice.")

        if errors:
            logger.warning(f"InvoiceLine (PK:{self.pk or 'New'}) model validation errors: {errors}")
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        self.line_total = (self.quantity or ZERO) * (self.unit_price or ZERO)

        # --- START FIX: Ensure company is set from the parent invoice ---
        if not self.company_id and self.invoice_id:
            # self.invoice should be populated by the formset save process when associating with parent
            parent_invoice = None
            if hasattr(self, 'invoice') and self.invoice and self.invoice.pk:
                parent_invoice = self.invoice
            else:
                # Fallback if self.invoice is not yet fully populated or is just an ID
                try:
                    parent_invoice = CustomerInvoice.objects.only('company_id', 'company').get(pk=self.invoice_id)
                except CustomerInvoice.DoesNotExist:
                    logger.error(
                        f"InvoiceLine (PK:{self.pk or 'New'}) save error: Parent CustomerInvoice ID {self.invoice_id} not found."
                    )
                    # This situation should ideally not occur if the formset links correctly.
                    # Raising an error or handling gracefully is needed.
                    # For now, let full_clean catch it if company remains None, or raise explicitly:
                    # raise DjangoValidationError({'invoice': _("Associated invoice not found for company assignment.")})

            if parent_invoice and parent_invoice.company_id:
                self.company = parent_invoice.company  # Assign the Company *instance*
                logger.debug(
                    f"InvoiceLine (PK:{self.pk or 'New'}): Set company to '{self.company}' from Invoice {parent_invoice.pk}")
            elif parent_invoice and not parent_invoice.company_id:
                logger.warning(
                    f"InvoiceLine (PK:{self.pk or 'New'}): Parent Invoice {parent_invoice.pk} has no company_id.")
            elif not parent_invoice:
                logger.warning(
                    f"InvoiceLine (PK:{self.pk or 'New'}): Could not retrieve parent invoice {self.invoice_id} for company assignment.")

        # --- END FIX ---

        if not kwargs.pop('skip_model_full_clean', False):
            exclude_fields = []
            if not self.invoice_id and self._state.adding:
                # This logic might have been intended to exclude 'invoice' if parent is new,
                # but company is a separate concern.
                pass

            # Now, when full_clean is called, self.company should be set if self.invoice had a company.
            self.full_clean(exclude=exclude_fields or None)

        super().save(*args, **kwargs)  # This will call TenantScopedModel.save()
        logger.debug(
            f"InvoiceLine {self.pk or 'Unsaved'} for Invoice {self.invoice_id or 'N/A'} saved. Line total: {self.line_total}."
        )


# =============================================================================
# CustomerPayment Model
# =============================================================================
class CustomerPayment(TenantScopedModel):
    customer = models.ForeignKey(Party, verbose_name=_("Customer"), on_delete=models.PROTECT,
                                 related_name='payments_received')
    payment_date = models.DateField(_("Payment Date"), default=timezone.now, db_index=True)
    reference_number = models.CharField(_("Payment Reference"), max_length=100, blank=True, db_index=True)
    amount_received = models.DecimalField(_("Amount Received"), max_digits=20, decimal_places=2)
    amount_applied = models.DecimalField(_("Amount Applied"), max_digits=20, decimal_places=2, default=ZERO,
                                         editable=False)
    amount_unapplied = models.DecimalField(_("Amount Unapplied"), max_digits=20, decimal_places=2, default=ZERO,
                                           editable=False)
    currency = models.CharField(_("Currency"), max_length=10, choices=CurrencyType.choices)
    payment_method = models.CharField(_("Payment Method"), max_length=30, choices=PaymentMethod.choices, blank=True)
    bank_account_credited = models.ForeignKey(Account, verbose_name=_("Bank Account Credited"),
                                              on_delete=models.PROTECT, related_name='customer_payments_deposited',
                                              help_text=_("Company's bank/cash Asset account."))
    notes = models.TextField(_("Notes"), blank=True)
    status = models.CharField(
        _("Payment Status"), max_length=20, choices=PaymentStatus.choices,
        default=getattr(PaymentStatus, 'UNAPPLIED', 'UNAPPLIED'),  # Robust default
        db_index=True
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='created_customer_payments', editable=False)
    related_gl_voucher = models.OneToOneField('crp_accounting.Voucher', verbose_name=_("Related GL Voucher"),
                                              on_delete=models.SET_NULL, null=True, blank=True, editable=False,
                                              related_name='source_customer_payment')

    class Meta:
        verbose_name = _("Customer Payment")
        verbose_name_plural = _("Customer Payments")
        ordering = ['company__name', '-payment_date', '-created_at']
        indexes = [models.Index(fields=['company', 'customer', 'payment_date'], name='custpay_co_cust_date_idx'),
                   models.Index(fields=['company', 'status'], name='custpay_co_stat_idx')]

    def __str__(self):
        cust_name = _("N/A Customer")
        if self.customer_id:
            try:
                cust_name = (self.customer.name if hasattr(self, 'customer') and self.customer else Party.objects.get(
                    pk=self.customer_id).name)
            except ObjectDoesNotExist:
                cust_name = f"Cust ID {self.customer_id} (Not Found)"
        amt, curr, dt = (self.amount_received or ZERO), (self.currency or ""), (self.payment_date or _("N/A"))
        return f"Payment from {cust_name} - {amt} {curr} on {dt}"

    def _recalculate_applied_amounts_and_status(self, save_instance: bool = False, _triggering_save: bool = False):
        if not self.pk: return
        logger.debug(f"Payment PK {self.pk}: Recalculating applied amounts and status (perform_save={save_instance}).")

        self.amount_applied = self.allocations.all().aggregate(sum_val=Sum('amount_applied', default=ZERO))[
                                  'sum_val'] or ZERO
        calculated_unapplied = (self.amount_received or ZERO) - self.amount_applied

        original_status = self.status
        new_status = original_status

        void_val = getattr(PaymentStatus, 'VOID', 'VOID')
        applied_val = getattr(PaymentStatus, 'APPLIED', 'APPLIED')
        partially_applied_val = getattr(PaymentStatus, 'PARTIALLY_APPLIED', 'PARTIALLY_APPLIED')
        unapplied_val = getattr(PaymentStatus, 'UNAPPLIED', 'UNAPPLIED')

        if self.status != void_val:
            if abs(calculated_unapplied) < SMALL_TOLERANCE:  # Using defined tolerance
                new_status = applied_val
            elif self.amount_applied > ZERO:
                new_status = partially_applied_val
            else:
                new_status = unapplied_val

        status_changed = self.status != new_status
        amount_unapplied_changed = self.amount_unapplied != calculated_unapplied

        if status_changed or amount_unapplied_changed:
            self.status = new_status
            self.amount_unapplied = calculated_unapplied  # Always update to the fresh calculation

            if status_changed: logger.info(
                f"Payment PK {self.pk}: Status '{original_status}'->'{new_status}'. Unapplied:{self.amount_unapplied}")
            if amount_unapplied_changed and not status_changed: logger.debug(
                f"Payment PK {self.pk}: Amount Unapplied updated to {self.amount_unapplied} (status '{self.status}' unchanged).")

            if save_instance and not _triggering_save:
                update_fields = ['amount_applied', 'amount_unapplied', 'status', 'updated_at']
                self.save(update_fields=update_fields, _recalculating_payment=True)

    def clean(self):
        super().clean()
        errors = {}
        effective_company: Optional[Company] = self.company
        if not effective_company and self.company_id:
            try:
                effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID.")
        if not effective_company and not self._state.adding and 'company' not in errors:
            errors['company'] = _("Payment company association missing.")

        if effective_company:
            if self.customer_id:
                try:
                    customer = Party.objects.select_related('company').get(pk=self.customer_id)
                    if customer.company != effective_company:
                        errors['customer'] = _("Customer must belong to the same company as the payment.")
                    if customer.party_type != getattr(CorePartyType, 'CUSTOMER', 'CUSTOMER'):
                        errors['customer'] = _("Party selected as customer is not of 'Customer' type.")
                    if not customer.is_active:
                        errors['customer'] = _("Selected customer is inactive.")
                except Party.DoesNotExist:
                    errors['customer'] = _("Selected customer not found.")
            elif not self._state.adding:
                errors['customer'] = _("Customer is required for the payment.")

            if self.bank_account_credited_id:
                try:
                    bank_acc = Account.objects.select_related('company').get(pk=self.bank_account_credited_id)
                    if bank_acc.company != effective_company:
                        errors['bank_account_credited'] = _(
                            "Bank Account must belong to the same company as the payment.")
                    if bank_acc.account_type != getattr(CoreAccountType, 'ASSET', 'ASSET'):
                        errors['bank_account_credited'] = _("Bank Account credited must be an 'Asset' type account.")
                    if not bank_acc.is_active or not bank_acc.allow_direct_posting:
                        errors['bank_account_credited'] = _(
                            "Bank Account is inactive or does not allow direct posting.")
                except Account.DoesNotExist:
                    errors['bank_account_credited'] = _("Selected Bank Account not found.")
            elif not self._state.adding:
                errors['bank_account_credited'] = _("Bank Account Credited is required.")

        if self.amount_received is not None and self.amount_received <= ZERO:
            errors['amount_received'] = _("Amount received must be positive.")

        if not self.currency:
            if effective_company and hasattr(effective_company,
                                             'default_currency_code') and effective_company.default_currency_code:
                self.currency = effective_company.default_currency_code
            elif 'currency' not in errors:
                errors['currency'] = _("Currency is required for the payment.")
        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        _recalculating = kwargs.pop('_recalculating_payment', False)

        if not self.pk:  # Actions for new instances only
            if not self.currency and self.company_id:
                try:
                    company_obj = Company.objects.get(pk=self.company_id)
                    if hasattr(company_obj, 'default_currency_code') and company_obj.default_currency_code:
                        self.currency = company_obj.default_currency_code
                except Company.DoesNotExist:
                    logger.warning(f"Cannot set default currency for new payment; company {self.company_id} not found.")
            self.amount_unapplied = self.amount_received or ZERO
            self.amount_applied = ZERO  # Ensure applied is zero for new payments

        # TenantScopedModel's save calls full_clean. If not inheriting or want to be explicit:
        if not kwargs.pop('skip_model_full_clean', False) and not _recalculating:
            self.full_clean()  # Ensure clean is called before super().save() if not handled by TenantScopedModel

        super().save(*args, **kwargs)

        # After the main save, if it's not part of a recalculation loop and PK exists,
        # trigger the status/amount update. Crucial after allocations might change.
        if not _recalculating and self.pk:
            self._recalculate_applied_amounts_and_status(save_instance=True, _triggering_save=True)


# =============================================================================
# PaymentAllocation Model (Final Corrected Version)
# =============================================================================
class PaymentAllocation(TenantScopedModel):
    payment = models.ForeignKey(CustomerPayment, verbose_name=_("Payment"), on_delete=models.CASCADE,
                                related_name='allocations')
    invoice = models.ForeignKey(CustomerInvoice, verbose_name=_("Invoice"), on_delete=models.CASCADE,
                                related_name='payment_allocations')
    amount_applied = models.DecimalField(_("Amount Applied"), max_digits=20, decimal_places=2,
                                         validators=[MinValueValidator(SMALL_TOLERANCE)])
    allocation_date = models.DateField(_("Allocation Date"), default=timezone.now)

    class Meta:
        verbose_name = _("Payment Allocation")
        verbose_name_plural = _("Payment Allocations")
        unique_together = (('payment', 'invoice'),)
        ordering = ['-allocation_date']

    def __str__(self):
        p_ref = f"PmtPK:{self.payment_id}"
        i_ref = f"InvPK:{self.invoice_id}"
        return f"Allocation: {p_ref} to {i_ref} - Amt: {self.amount_applied}"

    def clean(self):
        super().clean()
        errors = {}

        # Ensure parent objects are available for validation
        try:
            payment_instance = self.payment
            invoice_instance = self.invoice
        except (CustomerPayment.DoesNotExist, CustomerInvoice.DoesNotExist):
            return  # Let default FK validation handle this.

        # --- Standard cross-object validation ---
        if payment_instance.company_id != invoice_instance.company_id:
            errors['invoice'] = _("Payment and Invoice must belong to the same company.")
        if payment_instance.customer_id != invoice_instance.customer_id:
            errors['invoice'] = _("Payment and Invoice must be for the same customer.")
        if payment_instance.currency != invoice_instance.currency:
            errors['invoice'] = _("Payment currency must match Invoice currency.")

        # --- FIX START: Robust amount validation that handles both ADD and CHANGE views ---
        if self.amount_applied is not None and self.amount_applied > ZERO_DECIMAL:
            # 1. Check against available amount on the payment
            available_on_payment = ZERO_DECIMAL

            # This is the key change: Check if the parent payment is saved yet.
            if payment_instance.pk:  # CHANGE view: The payment exists in the DB.
                # This logic is safe now because the parent has a PK.
                other_allocations_sum = \
                payment_instance.allocations.exclude(pk=self.pk).aggregate(s=Sum('amount_applied'))['s'] or ZERO_DECIMAL
                available_on_payment = (payment_instance.amount_received or ZERO_DECIMAL) - other_allocations_sum
            else:  # ADD view: The payment is new and not yet in the DB.
                # We can't query '.allocations'. The total available is the full amount of the new payment.
                # Note: This logic assumes only one new allocation is being added at a time in the 'add' view,
                # which is a safe assumption for most use cases.
                available_on_payment = payment_instance.amount_received or ZERO_DECIMAL

            if self.amount_applied > (available_on_payment + SMALL_TOLERANCE):
                errors['amount_applied'] = _(
                    "Amount applied (%(applied)s) exceeds the payment's available unapplied amount (%(available)s)."
                ) % {'applied': self.amount_applied, 'available': available_on_payment}

            # 2. Check against the invoice's due amount (This part is fine as the invoice always exists)
            other_allocations_on_invoice = \
            invoice_instance.payment_allocations.exclude(pk=self.pk).aggregate(s=Sum('amount_applied'))[
                's'] or ZERO_DECIMAL
            invoice_due = (invoice_instance.total_amount or ZERO_DECIMAL) - other_allocations_on_invoice
            if self.amount_applied > (invoice_due + SMALL_TOLERANCE):
                errors['amount_applied'] = _(
                    "Amount applied (%(applied)s) exceeds the invoice's due amount (%(due)s)."
                ) % {'applied': self.amount_applied, 'due': invoice_due}
        # --- FIX END ---

        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        # This part, which we fixed before, remains crucial.
        if not self.company_id and self.payment_id:
            try:
                self.company = self.payment.company
            except (AttributeError, CustomerPayment.DoesNotExist):
                try:
                    self.company = CustomerPayment.objects.get(pk=self.payment_id).company
                except CustomerPayment.DoesNotExist:
                    pass

        super().save(*args, **kwargs)