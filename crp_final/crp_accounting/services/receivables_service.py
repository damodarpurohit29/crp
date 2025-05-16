# crp_accounting/services/receivables_service.py

import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import List, Dict, Any, Optional, Tuple

from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.conf import settings
# from django.contrib.contenttypes.models import ContentType # For GFK

# --- Model Imports ---
from ..models.receivables import (
    CustomerInvoice, InvoiceLine, CustomerPayment, PaymentAllocation, InvoiceSequence,
    InvoiceStatus, PaymentStatus, PaymentMethod
)
from ..models.party import Party
from ..models.coa import Account
from ..models.period import AccountingPeriod
from ..models.journal import Voucher, VoucherType, DrCrType, TransactionStatus  # For GL posting
from company.models import Company

# --- Enum Imports from crp_core ---
from crp_core.enums import PartyType as CorePartyType, AccountType as CoreAccountType

# --- Service Imports ---
from . import voucher_service


# --- Custom Exceptions ---
class ReceivablesServiceError(Exception): pass


class InvoiceProcessingError(ReceivablesServiceError): pass


class PaymentProcessingError(ReceivablesServiceError): pass


class SequenceGenerationError(ReceivablesServiceError): pass


class GLPostingError(ReceivablesServiceError): pass


logger = logging.getLogger("crp_accounting.services.receivables")
ZERO = Decimal('0.00')


# =============================================================================
# Invoice Number Generation Service
# =============================================================================
@transaction.atomic
def generate_next_invoice_number_from_sequence(company: Company, target_date: date,
                                               default_prefix_override: Optional[str] = None) -> str:
    log_prefix = f"[GenInvNum][Co:{company.pk}]"
    sequence_criteria = {'company': company}  # Assuming one primary sequence config per company for invoices
    default_inv_prefix = default_prefix_override if default_prefix_override is not None else getattr(company,
                                                                                                     'default_invoice_prefix',
                                                                                                     'INV-')
    default_padding = getattr(company, 'invoice_number_padding_digits', 5)
    default_reset_fmt = getattr(company, 'invoice_number_reset_format', '%Y')  # e.g., yearly reset

    # Using select_for_update() with get_or_create ensures atomicity if multiple requests try to create simultaneously
    # This is slightly different from fetching first then locking, but achieves the same for get_or_create.
    sequence_config, created = InvoiceSequence.objects.select_for_update().get_or_create(
        **sequence_criteria,
        defaults={
            'prefix': default_inv_prefix, 'padding_digits': default_padding,
            'last_number': 0, 'period_format_for_reset': default_reset_fmt,
            'current_period_key': None  # Will be set below
        }
    )

    # This part needs to happen on the locked instance if it wasn't just created with defaults.
    # If it was created, current_period_key is None, so it will be set.
    # If it existed, select_for_update already locked it.

    calculated_period_key = sequence_config.get_period_key_for_date(target_date or timezone.now().date())

    if created or sequence_config.current_period_key != calculated_period_key:
        if not created:  # Only log reset if it wasn't just created
            logger.info(
                f"{log_prefix} InvSeq {sequence_config.pk}: Period reset. Old: {sequence_config.current_period_key}, New: {calculated_period_key}.")
        sequence_config.last_number = 0
        sequence_config.current_period_key = calculated_period_key
        if created:
            logger.info(
                f"{log_prefix} Created/Initialized InvSeq PK {sequence_config.pk}, Prefix '{sequence_config.prefix}', PeriodKey '{sequence_config.current_period_key}'.")

    next_number_val = sequence_config.last_number + 1
    sequence_config.last_number = next_number_val
    formatted_number = sequence_config.format_number(next_number_val)

    if CustomerInvoice.objects.filter(company=company, invoice_number=formatted_number).exists():
        logger.error(
            f"{log_prefix} CRITICAL: Generated invoice number {formatted_number} already exists! Seq PK={sequence_config.pk}, LastNo={sequence_config.last_number}")
        raise SequenceGenerationError(
            _("Generated invoice number '%(num)s' conflicts. Please check sequence configuration or retry.") % {
                'num': formatted_number})

    sequence_config.save(
        update_fields=['last_number', 'current_period_key', 'updated_at'])  # updated_at from TenantScopedModel
    logger.info(
        f"{log_prefix} Generated invoice number: {formatted_number} (SeqNo: {next_number_val}) from InvSeq {sequence_config.pk}")
    return formatted_number


# =============================================================================
# Customer Invoice Service Functions
# =============================================================================
@transaction.atomic
def create_customer_invoice(
        company_id: Any, created_by_user: settings.AUTH_USER_MODEL, customer_id: Any,
        invoice_date: date, due_date: date, lines_data: List[Dict[str, Any]],
        currency: Optional[str] = None, terms: Optional[str] = None,
        notes_to_customer: Optional[str] = None, internal_notes: Optional[str] = None,
        invoice_number_override: Optional[str] = None,
        initial_status: str = InvoiceStatus.DRAFT.value,  # Start as DRAFT by default
        post_to_gl_on_finalize: bool = True,  # If status is not DRAFT, try to post
        gl_voucher_type_value: str = VoucherType.SALES.value  # Use specific enum value
) -> CustomerInvoice:
    log_prefix = f"[CreateInvoice][Co:{company_id}][User:{created_by_user.name}]"  # Standardized to name
    logger.info(f"{log_prefix} For Customer ID {customer_id}, initial_status '{initial_status}'.")

    try:
        company = Company.objects.get(pk=company_id)
        customer = Party.objects.get(pk=customer_id, company=company, party_type=CorePartyType.CUSTOMER.value,
                                     is_active=True)
    except Company.DoesNotExist:
        raise InvoiceProcessingError(_("Invalid company specified for invoice."))
    except Party.DoesNotExist:
        raise InvoiceProcessingError(_("Active customer not found or invalid for company."))

    effective_currency = currency or getattr(company, 'default_currency_code', None)
    if not effective_currency: raise InvoiceProcessingError(_("Invoice currency required; company has no default."))

    final_invoice_number = invoice_number_override
    # Generate number only if not DRAFT and no override. Drafts can have blank numbers.
    if not final_invoice_number and initial_status != InvoiceStatus.DRAFT.value:
        final_invoice_number = generate_next_invoice_number_from_sequence(company, invoice_date)

    invoice = CustomerInvoice(
        company=company, customer=customer, invoice_number=final_invoice_number,  # Can be blank if DRAFT
        invoice_date=invoice_date, due_date=due_date, currency=effective_currency,
        terms=terms or "", notes_to_customer=notes_to_customer or "", internal_notes=internal_notes or "",
        status=initial_status, created_by=created_by_user, updated_by=created_by_user
    )
    try:
        # Exclude fields calculated by model or set later. Invoice number can be blank for draft.
        exclude_clean = ['subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due',
                         'related_gl_voucher']
        if not final_invoice_number and initial_status == InvoiceStatus.DRAFT.value: exclude_clean.append(
            'invoice_number')
        invoice.full_clean(exclude=exclude_clean)
    except DjangoValidationError as e:
        raise InvoiceProcessingError(e.message_dict)
    invoice.save()  # Save header

    if not lines_data: raise InvoiceProcessingError(_("Invoice must have at least one line item."))
    for line_data in lines_data:
        try:
            revenue_acc_id = line_data.get('revenue_account_id')
            if not revenue_acc_id: raise DjangoValidationError({'revenue_account_id': _("Revenue account ID missing.")})
            revenue_account = Account.objects.get(pk=revenue_acc_id, company=company,
                                                  account_type=CoreAccountType.INCOME.value, is_active=True,
                                                  allow_direct_posting=True)
            line = InvoiceLine(
                invoice=invoice, description=line_data.get('description', ''),
                quantity=Decimal(str(line_data.get('quantity', '1'))),
                unit_price=Decimal(str(line_data.get('unit_price', '0'))),
                revenue_account=revenue_account,
                tax_amount_on_line=Decimal(str(line_data.get('tax_amount_on_line', '0')))
            )
            line.save()  # This calls line.full_clean() and calculates line.line_total
        except (Account.DoesNotExist, DjangoValidationError, ValueError, TypeError) as e_line:
            err_msg = e_line.message_dict if hasattr(e_line, 'message_dict') else str(e_line)
            raise InvoiceProcessingError(
                _("Error in invoice line: %(e)s. Data: %(d)s") % {'e': err_msg, 'd': line_data})

    # After all lines are saved, explicitly trigger recalculation on the invoice object in memory
    # and then save it. This is better than InvoiceLine.save() triggering parent save.
    invoice._recalculate_totals_and_due(perform_save=True)  # This will save the invoice with updated totals.
    # Note: _recalculate_totals_and_due also calls update_payment_status.

    if invoice.status != InvoiceStatus.DRAFT.value and post_to_gl_on_finalize:
        try:
            gl_voucher = _post_invoice_to_gl_internal(company, created_by_user, invoice, gl_voucher_type_value)
            invoice.related_gl_voucher = gl_voucher
            # Status should already be non-DRAFT if we are here, e.g., SENT by mark_invoice_as_sent
            # No need to change status again unless GL posting implies a specific status.
            invoice.updated_by = created_by_user
            invoice.save(update_fields=['related_gl_voucher', 'updated_by', 'updated_at'])
            logger.info(
                f"{log_prefix} Invoice {invoice.invoice_number} (Status: {invoice.get_status_display()}) processing complete, posted to GL {gl_voucher.voucher_number}.")
        except Exception as e_gl:
            logger.error(
                f"{log_prefix} Invoice {invoice.invoice_number} (Status: {invoice.get_status_display()}) processed, but FAILED GL posting: {e_gl}",
                exc_info=True)
            raise InvoiceProcessingError(
                _("Invoice processed as '%(s)s', but GL posting failed: %(e)s") % {'s': invoice.get_status_display(),
                                                                                   'e': str(e_gl)})
    else:
        logger.info(
            f"{log_prefix} Invoice {invoice.invoice_number} processed with status '{invoice.get_status_display()}'. GL posting skipped/deferred.")
    return invoice


@transaction.atomic
def mark_invoice_as_sent(company_id: Any, user: settings.AUTH_USER_MODEL, invoice_id: Any,
                         post_to_gl: bool = True) -> CustomerInvoice:
    log_prefix = f"[MarkInvSent][Co:{company_id}][User:{user.name}][Inv:{invoice_id}]"
    logger.info(f"{log_prefix} Attempting to mark invoice as SENT.")
    try:
        company = Company.objects.get(pk=company_id)
        # Lock invoice for update
        invoice = CustomerInvoice.objects.select_for_update().get(pk=invoice_id, company=company)
    except Company.DoesNotExist:
        raise InvoiceProcessingError(_("Company not found for marking invoice sent."))
    except CustomerInvoice.DoesNotExist:
        raise InvoiceProcessingError(_("Invoice not found or invalid for company."))

    if invoice.status != InvoiceStatus.DRAFT.value:
        raise InvoiceProcessingError(_("Only DRAFT invoices can be marked as SENT. Current status: %(s)s.") % {
            's': invoice.get_status_display()})

    # Ensure totals are correct before potentially generating number or posting
    invoice._recalculate_totals_and_due(perform_save=True)

    if not invoice.invoice_number or not invoice.invoice_number.strip():
        invoice.invoice_number = generate_next_invoice_number_from_sequence(company, invoice.invoice_date)
        logger.info(f"{log_prefix} Generated invoice number '{invoice.invoice_number}' as it was blank.")

    gl_voucher_to_link: Optional[Voucher] = None
    if post_to_gl:
        gl_voucher_to_link = _post_invoice_to_gl_internal(company, user, invoice, VoucherType.SALES.value)

    invoice.status = InvoiceStatus.SENT.value
    invoice.updated_by = user
    if gl_voucher_to_link: invoice.related_gl_voucher = gl_voucher_to_link

    update_fields = ['status', 'invoice_number', 'updated_by', 'updated_at']
    if gl_voucher_to_link: update_fields.append('related_gl_voucher')

    try:
        invoice.full_clean(exclude=['subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid',
                                    'amount_due'] if gl_voucher_to_link else ['subtotal_amount', 'tax_amount',
                                                                              'total_amount', 'amount_paid',
                                                                              'amount_due', 'related_gl_voucher'])
    except DjangoValidationError as e:
        raise InvoiceProcessingError(e.message_dict)
    invoice.save(update_fields=list(set(update_fields)))  # Use set to avoid duplicate fields

    logger.info(f"{log_prefix} Invoice '{invoice.invoice_number}' marked SENT. GL Posted: {bool(gl_voucher_to_link)}")
    return invoice


@transaction.atomic
def post_selected_invoices_to_gl(company_id: Any, user: settings.AUTH_USER_MODEL, invoice_ids_list: List[Any]) -> Tuple[
    int, int, List[str]]:
    log_prefix = f"[PostSelectedInvs][Co:{company_id}][User:{user.name}]"
    logger.info(f"{log_prefix} Processing {len(invoice_ids_list)} invoices for GL posting.")
    if not invoice_ids_list: return 0, 0, []

    try:
        company = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise InvoiceProcessingError(_("Invalid company for batch GL posting."))

    success_count, error_count = 0, 0;
    errors_detail: List[str] = []
    # Process one by one in a loop to handle individual errors and ensure atomicity per invoice
    for invoice_pk in invoice_ids_list:
        log_inv_pk = f"PK:{invoice_pk}"
        try:
            with transaction.atomic():  # Create a savepoint for each invoice
                invoice = CustomerInvoice.objects.select_for_update().get(pk=invoice_pk, company=company)
                log_inv_num = invoice.invoice_number or log_inv_pk

                if invoice.status not in [InvoiceStatus.DRAFT.value, InvoiceStatus.SENT.value]:
                    errors_detail.append(_("Inv %(n)s: Not DRAFT/SENT (is %(s)s). Skipped.") % {'n': log_inv_num,
                                                                                                's': invoice.get_status_display()});
                    error_count += 1;
                    continue

                # _post_invoice_to_gl_internal handles idempotency by checking related_gl_voucher
                if not invoice.invoice_number and invoice.status == InvoiceStatus.DRAFT.value:  # Assign number if draft and posting
                    invoice.invoice_number = generate_next_invoice_number_from_sequence(company, invoice.invoice_date)

                invoice._recalculate_totals_and_due(perform_save=True)  # Ensure totals are correct before posting

                gl_voucher = _post_invoice_to_gl_internal(company, user, invoice, VoucherType.SALES.value)
                invoice.related_gl_voucher = gl_voucher
                invoice.status = InvoiceStatus.SENT.value  # Mark as SENT (or a specific "GL Posted" status)
                invoice.updated_by = user
                invoice.save(
                    update_fields=['status', 'invoice_number', 'related_gl_voucher', 'updated_by', 'updated_at'])
                success_count += 1
                logger.info(
                    f"{log_prefix} Batch: Posted Inv {invoice.invoice_number} to GL {gl_voucher.voucher_number}.")
        except CustomerInvoice.DoesNotExist:
            errors_detail.append(_("Inv ID %(id)s: Not found for company.") % {'id': invoice_pk});
            error_count += 1
        except Exception as e:
            error_count += 1;
            err_msg = f"Invoice ID {invoice_pk}: {str(e)}";
            errors_detail.append(err_msg)
            logger.error(f"{log_prefix} Batch: Error posting Inv ID {invoice_pk}: {e}", exc_info=True)
            # The transaction.atomic() for this specific invoice will rollback.

    logger.info(f"{log_prefix} Batch GL Posting Result: Success={success_count}, Errors/Skipped={error_count}.")
    return success_count, error_count, errors_detail


@transaction.atomic
def void_customer_invoice(company_id: Any, user: settings.AUTH_USER_MODEL, invoice_id: Any, void_reason: str,
                          void_date: Optional[date] = None) -> CustomerInvoice:
    log_prefix = f"[VoidInvoice][Co:{company_id}][User:{user.name}][Inv:{invoice_id}]"
    logger.info(f"{log_prefix} Reason: {void_reason}")
    effective_void_date = void_date or timezone.now().date()
    if not void_reason or not void_reason.strip(): raise InvoiceProcessingError(_("Reason required to void invoice."))

    try:
        company = Company.objects.get(pk=company_id)
        invoice = CustomerInvoice.objects.select_for_update().get(pk=invoice_id, company=company)
    except Company.DoesNotExist:
        raise InvoiceProcessingError(_("Company not found."))
    except CustomerInvoice.DoesNotExist:
        raise InvoiceProcessingError(_("Invoice not found or invalid for company."))

    if invoice.status == InvoiceStatus.VOID.value: logger.info(
        f"{log_prefix} Inv {invoice.invoice_number} already VOID."); return invoice

    invoice._recalculate_totals_and_due(perform_save=True)  # Ensure amount_paid is current
    if invoice.amount_paid > ZERO: raise InvoiceProcessingError(
        _("Cannot VOID inv '%(n)s' with payments (%(p)s). Reverse payments or use credit note.") % {
            'n': invoice.invoice_number, 'p': invoice.amount_paid})

    original_status_log = invoice.status;
    gl_reversal_voucher_num: Optional[str] = None
    if invoice.related_gl_voucher_id:
        try:
            original_gl = Voucher.objects.get(pk=invoice.related_gl_voucher_id, company=company)
            if original_gl.status == TransactionStatus.POSTED.value:
                reversing_gl = voucher_service.create_reversing_voucher(company_id=company.id,
                                                                        original_voucher_id=original_gl.pk, user=user,
                                                                        reversal_date=effective_void_date,
                                                                        reversal_voucher_type_value=getattr(VoucherType,
                                                                                                            'SALES_REVERSAL',
                                                                                                            VoucherType.GENERAL_REVERSAL).value,
                                                                        post_immediately=True)
                gl_reversal_voucher_num = reversing_gl.voucher_number
                logger.info(f"{log_prefix} Created GL reversal {gl_reversal_voucher_num} for voided invoice.")
            else:
                logger.warning(
                    f"{log_prefix} Original GL {original_gl.voucher_number} for invoice not POSTED. No GL reversal created.")
        except Voucher.DoesNotExist:
            logger.error(
                f"{log_prefix} CRITICAL: Original GL Voucher ID {invoice.related_gl_voucher_id} not found. Manual GL adjustment may be needed.")
        except Exception as e_rev:
            logger.error(f"{log_prefix} Error creating GL reversal: {e_rev}",
                         exc_info=True); raise InvoiceProcessingError(
                _("Failed to create GL reversal: %(e)s") % {'e': str(e_rev)})

    invoice.status = InvoiceStatus.VOID.value
    invoice.internal_notes = (
                                         invoice.internal_notes or "") + f"\nVOIDED {effective_void_date.strftime('%Y-%m-%d')} by {user.name}. Reason: {void_reason}. OrigStat: {original_status_log}. " + (
                                 f"GL Reversal: {gl_reversal_voucher_num}." if gl_reversal_voucher_num else "No GL reversal made.")
    invoice.updated_by = user
    # A voided invoice effectively has zero value.
    invoice.subtotal_amount, invoice.tax_amount, invoice.total_amount, invoice.amount_paid, invoice.amount_due = (
    ZERO, ZERO, ZERO, ZERO, ZERO)
    invoice.save(
        update_fields=['status', 'internal_notes', 'subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid',
                       'amount_due', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Invoice '{invoice.invoice_number}' VOIDED successfully.")
    return invoice


# --- _post_invoice_to_gl_internal (With Idempotency) ---
def _post_invoice_to_gl_internal(company: Company, user: settings.AUTH_USER_MODEL, invoice: CustomerInvoice,
                                 gl_voucher_type_value: str) -> Voucher:
    log_prefix = f"[PostInvGLInternal][Co:{company.pk}][Inv:{invoice.invoice_number or invoice.pk}]"
    # Idempotency: Check if already linked to a POSTED GL voucher
    if invoice.related_gl_voucher_id:
        try:
            existing_voucher = Voucher.objects.get(pk=invoice.related_gl_voucher_id, company=company)
            if existing_voucher.status == TransactionStatus.POSTED.value:
                logger.info(
                    f"{log_prefix} Already linked to POSTED GL Voucher {existing_voucher.voucher_number}. Skipping.")
                return existing_voucher
            else:  # Linked, but not posted - unusual. Clear link and proceed.
                logger.warning(
                    f"{log_prefix} Linked to non-POSTED GL Voucher {existing_voucher.voucher_number}. Clearing link, will create new.")
                invoice.related_gl_voucher = None
                # No save here, will be saved if new voucher is linked later
        except Voucher.DoesNotExist:
            logger.error(
                f"{log_prefix} Linked to non-existent GL Voucher ID {invoice.related_gl_voucher_id}. Clearing.")
            invoice.related_gl_voucher = None

    if not invoice.customer.control_account_id:
        raise GLPostingError(_("Customer '%(n)s' lacks AR Control Account.") % {'n': invoice.customer.name})

    invoice._recalculate_totals_and_due(perform_save=True)  # Ensure invoice totals are current and saved
    invoice.refresh_from_db(fields=['total_amount', 'subtotal_amount', 'tax_amount'])  # Get saved values

    ar_control_account = Account.objects.get(pk=invoice.customer.control_account_id, company=company)
    try:
        accounting_period = AccountingPeriod.objects.get(company=company, start_date__lte=invoice.invoice_date,
                                                         end_date__gte=invoice.invoice_date, locked=False)
    except AccountingPeriod.DoesNotExist:
        raise GLPostingError(_("No open accounting period for invoice date %(d)s.") % {'d': invoice.invoice_date})
    except AccountingPeriod.MultipleObjectsReturned:
        raise GLPostingError(
            _("Multiple open accounting periods for invoice date %(d)s.") % {'d': invoice.invoice_date})

    lines_gl_data = [
        {'account_id': ar_control_account.pk, 'dr_cr': DrCrType.DEBIT.value, 'amount': invoice.total_amount,
         'narration': _("A/R for Inv# %(n)s") % {'n': invoice.invoice_number}}]
    sum_of_gl_credits = ZERO
    for line in invoice.lines.all():
        lines_gl_data.append(
            {'account_id': line.revenue_account_id, 'dr_cr': DrCrType.CREDIT.value, 'amount': line.line_total,
             'narration': _("Rev Inv# %(n)s - %(d)s") % {'n': invoice.invoice_number, 'd': line.description[:20]}})
        sum_of_gl_credits += line.line_total

    if invoice.tax_amount > ZERO:
        # Assume CompanyAccountingSettings has default_sales_tax_payable_account
        try:
            acc_settings = company.accounting_settings  # OneToOne reverse from Company
            tax_payable_account = acc_settings.default_sales_tax_payable_account
            if not tax_payable_account: raise GLPostingError(
                _("Default Sales Tax Payable account not set in Company Settings for '%(co)s'.") % {'co': company.name})
            if tax_payable_account.company_id != company.id: raise GLPostingError(
                _("Configured Tax Acct Co mismatch."))  # Sanity check
            lines_gl_data.append(
                {'account_id': tax_payable_account.pk, 'dr_cr': DrCrType.CREDIT.value, 'amount': invoice.tax_amount,
                 'narration': _("Tax Inv# %(n)s") % {'n': invoice.invoice_number}})
            sum_of_gl_credits += invoice.tax_amount
        except Company.accounting_settings.RelatedObjectDoesNotExist:
            raise GLPostingError(_("Company Accounting Settings not found for '%(co)s'.") % {'co': company.name})
        except AttributeError:
            raise GLPostingError(
                _("Sales Tax Payable account not configured in settings for '%(co)s'.") % {'co': company.name})

    if abs(invoice.total_amount - sum_of_gl_credits).quantize(Decimal('0.01')) != ZERO:  # Use quantize for comparison
        raise GLPostingError(_("GL for inv %(n)s imbalanced. DR:%(dr)s CR:%(cr)s") % {'n': invoice.invoice_number,
                                                                                      'dr': invoice.total_amount,
                                                                                      'cr': sum_of_gl_credits})

    gl_v = voucher_service.create_draft_voucher(company_id=company.id, created_by_user=user,
                                                voucher_type_value=gl_voucher_type_value, date=invoice.invoice_date,
                                                narration=_("Sales Inv# %(n)s - %(c)s") % {'n': invoice.invoice_number,
                                                                                           'c': invoice.customer.name},
                                                lines_data=lines_gl_data, party_pk=invoice.customer_id,
                                                reference=invoice.invoice_number)
    sub_v = voucher_service.submit_voucher_for_approval(company.id, gl_v.pk, user)
    post_v = voucher_service.approve_and_post_voucher(company.id, sub_v.pk, user, _("Auto-approved: Sales Invoice GL"))
    return post_v


# =============================================================================
# Customer Payment Service Functions (Full, with Idempotency)
# =============================================================================
@transaction.atomic
def record_customer_payment(
        company_id: Any, created_by_user: settings.AUTH_USER_MODEL, customer_id: Any,
        payment_date: date, amount_received: Decimal, currency: str,
        bank_account_credited_id: Any, payment_method: Optional[str] = None,
        reference_number: Optional[str] = None, notes: Optional[str] = None,
        allocations_data: Optional[List[Dict[str, Any]]] = None,
        post_to_gl_immediately: bool = True,
        gl_voucher_type_value: str = VoucherType.RECEIPT.value
) -> CustomerPayment:
    log_prefix = f"[RecordPayment][Co:{company_id}][User:{created_by_user.name}]"
    logger.info(f"{log_prefix} For Customer ID {customer_id}, Amt: {amount_received} {currency}.")
    try:
        company = Company.objects.get(pk=company_id)
        customer = Party.objects.get(pk=customer_id, company=company, party_type=CorePartyType.CUSTOMER.value,
                                     is_active=True)
        bank_account = Account.objects.get(pk=bank_account_credited_id, company=company,
                                           account_type=CoreAccountType.ASSET.value, is_active=True,
                                           allow_direct_posting=True)
    except Company.DoesNotExist:
        raise PaymentProcessingError(_("Invalid company for payment."))
    except Party.DoesNotExist:
        raise PaymentProcessingError(_("Active customer not found or invalid."))
    except Account.DoesNotExist:
        raise PaymentProcessingError(_("Bank account invalid/inactive/no direct posting."))

    if amount_received <= ZERO: raise PaymentProcessingError(_("Amount received must be positive."))
    if not currency: raise PaymentProcessingError(_("Payment currency required."))

    payment = CustomerPayment(
        company=company, customer=customer, payment_date=payment_date, amount_received=amount_received,
        currency=currency, bank_account_credited=bank_account,
        payment_method=payment_method or PaymentMethod.OTHER.value, reference_number=reference_number or "",
        notes=notes or "", status=PaymentStatus.UNAPPLIED.value, created_by=created_by_user, updated_by=created_by_user
    )
    try:
        payment.full_clean(exclude=['amount_applied', 'amount_unapplied', 'related_gl_voucher'])
    except DjangoValidationError as e:
        raise PaymentProcessingError(e.message_dict)
    payment.save()  # Model save sets initial amount_unapplied

    if allocations_data:
        for alloc_data in allocations_data:
            inv_id = alloc_data.get('invoice_id');
            amt_apply_str = str(alloc_data.get('amount_applied', '0'))
            amt_apply = Decimal(amt_apply_str).quantize(Decimal('0.01'), ROUND_HALF_UP)
            if not inv_id or amt_apply <= ZERO: raise PaymentProcessingError(
                _("Invalid allocation: Missing invoice ID or non-positive amount."))
            _allocate_payment_to_invoice_internal(payment, inv_id, amt_apply,
                                                  payment.payment_date)  # This updates the invoice

    # After all allocations, update the payment's own totals and status
    payment._recalculate_applied_amounts_and_status(save_instance=True)

    if payment.amount_unapplied < (ZERO - Decimal('0.01')): raise PaymentProcessingError(
        _("Payment over-applied. Unapplied is negative."))

    if post_to_gl_immediately:
        try:
            gl_voucher = _post_payment_to_gl_internal(company, created_by_user, payment, gl_voucher_type_value)
            payment.related_gl_voucher = gl_voucher;
            payment.updated_by = created_by_user
            payment.save(update_fields=['related_gl_voucher', 'updated_by', 'updated_at'])
            logger.info(f"{log_prefix} Payment {payment.pk} recorded & posted to GL {gl_voucher.voucher_number}.")
        except Exception as e_gl:
            logger.error(f"{log_prefix} Payment {payment.pk} recorded, FAILED GL posting: {e_gl}",
                         exc_info=True); raise PaymentProcessingError(
                _("Payment recorded, GL posting failed: %(err)s") % {'err': str(e_gl)})
    else:
        logger.info(f"{log_prefix} Payment {payment.pk} recorded status '{payment.get_status_display()}'. GL deferred.")
    return payment


def _allocate_payment_to_invoice_internal(payment: CustomerPayment, invoice_id: Any, amount_to_apply: Decimal,
                                          allocation_date: date):
    log_prefix = f"[AllocatePmt][Pmt:{payment.pk}][Inv:{invoice_id}]"
    try:
        invoice = CustomerInvoice.objects.select_for_update().get(pk=invoice_id, company=payment.company,
                                                                  customer=payment.customer)
    except CustomerInvoice.DoesNotExist:
        raise PaymentProcessingError(_("Inv ID %(id)s for alloc not found for customer/co.") % {'id': invoice_id})
    if invoice.currency != payment.currency: raise PaymentProcessingError(
        _("Currency mismatch: Pmt (%(pc)s) vs Inv (%(ic)s).") % {'pc': payment.currency, 'ic': invoice.currency})
    if invoice.status in [InvoiceStatus.PAID.value, InvoiceStatus.VOID.value,
                          InvoiceStatus.CANCELLED.value]: raise PaymentProcessingError(
        _("Cannot alloc to Inv %(n)s: Status is %(s)s.") % {'n': invoice.invoice_number,
                                                            's': invoice.get_status_display()})

    invoice._recalculate_totals_and_due(perform_save=True)  # Ensure invoice.amount_due is fresh
    invoice.refresh_from_db(fields=['amount_due'])

    if amount_to_apply.quantize(Decimal('0.01')) > invoice.amount_due.quantize(Decimal('0.01')) + Decimal(
        '0.005'): raise PaymentProcessingError(
        _("Alloc Err Inv %(n)s: Apply (%(a)s) > Due (%(d)s).") % {'n': invoice.invoice_number, 'a': amount_to_apply,
                                                                  'd': invoice.amount_due})

    current_payment_total_applied = payment.allocations.all().aggregate(s=Sum('amount_applied', default=ZERO))[
                                        's'] or ZERO
    if (current_payment_total_applied + amount_to_apply).quantize(Decimal('0.01')) > payment.amount_received.quantize(
        Decimal('0.01')) + Decimal('0.005'): raise PaymentProcessingError(
        _("Alloc Err Pmt %(p)s: Total applied (%(ta)s + %(na)s) > Rcvd (%(r)s).") % {
            'p': payment.reference_number or payment.pk, 'ta': current_payment_total_applied, 'na': amount_to_apply,
            'r': payment.amount_received})

    allocation = PaymentAllocation(payment=payment, invoice=invoice, amount_applied=amount_to_apply,
                                   allocation_date=allocation_date)
    try:
        allocation.full_clean()
    except DjangoValidationError as e:
        raise PaymentProcessingError(e.message_dict)
    allocation.save();
    logger.info(f"{log_prefix} Created allocation of {amount_to_apply} {payment.currency}.")

    # After saving allocation, update the INVOICE
    invoice._recalculate_totals_and_due(
        perform_save=True)  # This updates invoice.amount_paid, due, and status, and saves.
    logger.info(
        f"{log_prefix} Updated Inv {invoice.invoice_number}: Paid={invoice.amount_paid}, Due={invoice.amount_due}, Status={invoice.get_status_display()}")


def _post_payment_to_gl_internal(company: Company, user: settings.AUTH_USER_MODEL, payment: CustomerPayment,
                                 gl_voucher_type_value: str) -> Voucher:
    log_prefix = f"[PostPmtGLInternal][Co:{company.pk}][Pmt:{payment.pk}]"
    if payment.related_gl_voucher_id:  # Idempotency
        try:
            existing_voucher = Voucher.objects.get(pk=payment.related_gl_voucher_id, company=company)
            if existing_voucher.status == TransactionStatus.POSTED.value:
                logger.info(f"{log_prefix} Already linked to POSTED GL Vch. Skipping."); return existing_voucher
            else:
                logger.warning(
                    f"{log_prefix} Linked to non-POSTED GL Vch. Will create new."); payment.related_gl_voucher = None
        except Voucher.DoesNotExist:
            logger.error(f"{log_prefix} Linked to non-existent GL Vch ID. Clearing."); payment.related_gl_voucher = None
    if not payment.customer.control_account_id: raise GLPostingError(
        _("Customer for pmt (ID: %(cid)s) lacks AR Ctrl Acct.") % {'cid': payment.customer_id})

    payment._recalculate_applied_amounts_and_status(save_instance=True)  # Ensure payment amounts are current
    payment.refresh_from_db(fields=['amount_received', 'amount_applied', 'amount_unapplied'])

    ar_ctrl_acct = Account.objects.get(pk=payment.customer.control_account_id, company=company);
    bank_acct = payment.bank_account_credited
    try:
        acc_period = AccountingPeriod.objects.get(company=company, start_date__lte=payment.payment_date,
                                                  end_date__gte=payment.payment_date, locked=False)
    except AccountingPeriod.DoesNotExist:
        raise GLPostingError(_("No open period for pmt date %(d)s.") % {'d': payment.payment_date})
    except AccountingPeriod.MultipleObjectsReturned:
        raise GLPostingError(_("Multiple open periods for pmt date %(d)s.") % {'d': payment.payment_date})

    # GL for Payment: Debit Bank, Credit A/R for the amount_received.
    # The allocation of this payment against specific invoices is an AR sub-ledger detail.
    # The GL impact is that cash came in, and the overall A/R for the customer decreased.
    lines_data_gl = [
        {'account_id': bank_acct.pk, 'dr_cr': DrCrType.DEBIT.value, 'amount': payment.amount_received,
         'narration': _("Cash/Bank from %(c)s - Ref:%(r)s") % {'c': payment.customer.name,
                                                               'r': payment.reference_number or payment.pk}},
        {'account_id': ar_ctrl_acct.pk, 'dr_cr': DrCrType.CREDIT.value, 'amount': payment.amount_received,
         'narration': _("Pmt applied from %(c)s - Ref:%(r)s") % {'c': payment.customer.name,
                                                                 'r': payment.reference_number or payment.pk}}
    ]
    if payment.amount_unapplied > ZERO and getattr(company.accounting_settings, 'default_unapplied_cash_account', None):
        # Advanced: If company has a specific account for unapplied cash (liability)
        # Dr Bank (total received)
        # Cr A/R (total applied to invoices)
        # Cr Unapplied Cash (the unapplied portion)
        # This would require adjusting the lines_data_gl above based on payment.amount_applied
        # and payment.amount_unapplied, and fetching the default_unapplied_cash_account.
        # For simplicity, the current GL posts full amount received to AR.
        logger.info(
            f"{log_prefix} Payment has unapplied amount {payment.amount_unapplied}. Standard GL (Dr Bank, Cr A/R) used.")

    gl_v = voucher_service.create_draft_voucher(company_id=company.id, created_by_user=user,
                                                voucher_type_value=gl_voucher_type_value, date=payment.payment_date,
                                                narration=_("PmtRcvd: %(c)s - Ref:%(r)s") % {'c': payment.customer.name,
                                                                                             'r': payment.reference_number or payment.pk},
                                                lines_data=lines_data_gl, party_pk=payment.customer_id,
                                                reference=payment.reference_number)
    sub_v = voucher_service.submit_voucher_for_approval(company.id, gl_v.pk, user)
    post_v = voucher_service.approve_and_post_voucher(company.id, sub_v.pk, user, _("Auto-approved: Cust Pmt GL"))
    return post_v


# =============================================================================
# Admin Action Oriented Service Functions
# =============================================================================
@transaction.atomic
def post_selected_payments_to_gl(company_id: Any, user: settings.AUTH_USER_MODEL, payment_ids_list: List[Any]) -> Tuple[
    int, int, List[str]]:
    log_prefix = f"[PostSelectedPmts][Co:{company_id}][User:{user.name}]"
    logger.info(f"{log_prefix} Processing {len(payment_ids_list)} payments for GL posting.")
    if not payment_ids_list: return 0, 0, []

    try:
        company = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise PaymentProcessingError(_("Invalid company for batch GL posting of payments."))

    success_count, error_count = 0, 0;
    errors_detail: List[str] = []
    # Fetch all at once with select_for_update if underlying _post_payment_to_gl_internal might not lock individually.
    # However, _post_payment_to_gl_internal does its own checks and GL voucher is atomic.
    candidate_payments_qs = CustomerPayment.objects.filter(pk__in=payment_ids_list, company=company).select_related(
        'customer', 'company', 'bank_account_credited')

    for payment in candidate_payments_qs:
        log_pmt_ref = payment.reference_number or f"PK:{payment.pk}"
        try:
            with transaction.atomic():  # Savepoint for each payment
                if payment.status == PaymentStatus.VOID.value:
                    errors_detail.append(_("Pmt %(ref)s: Is VOID. Skipped.") % {'ref': log_pmt_ref});
                    error_count += 1;
                    continue
                # Idempotency is handled within _post_payment_to_gl_internal

                gl_voucher = _post_payment_to_gl_internal(company, user, payment, VoucherType.RECEIPT.value)
                payment.related_gl_voucher = gl_voucher
                payment.updated_by = user
                payment.save(update_fields=['related_gl_voucher', 'updated_by', 'updated_at'])
                success_count += 1
                logger.info(
                    f"{log_prefix} Batch: Successfully posted Payment {log_pmt_ref} to GL {gl_voucher.voucher_number}.")
        except Exception as e:
            error_count += 1;
            err_msg = f"Payment {log_pmt_ref}: {str(e)}";
            errors_detail.append(err_msg)
            logger.error(f"{log_prefix} Batch: Error posting Payment {log_pmt_ref}: {e}", exc_info=True)

    logger.info(f"{log_prefix} Batch Pmt GL Posting: Success={success_count}, Errors={error_count}.")
    return success_count, error_count, errors_detail


@transaction.atomic
def void_customer_payment(company_id: Any, user: settings.AUTH_USER_MODEL, payment_id: Any, void_reason: str,
                          void_date: Optional[date] = None) -> CustomerPayment:
    log_prefix = f"[VoidPayment][Co:{company_id}][User:{user.name}][Pmt:{payment_id}]"
    logger.info(f"{log_prefix} Reason: {void_reason}")
    effective_void_date = void_date or timezone.now().date()
    if not void_reason or not void_reason.strip(): raise PaymentProcessingError(_("Reason required to void payment."))

    try:
        company = Company.objects.get(pk=company_id)
        payment = CustomerPayment.objects.select_for_update().get(pk=payment_id, company=company)
    except Company.DoesNotExist:
        raise PaymentProcessingError(_("Company not found."))
    except CustomerPayment.DoesNotExist:
        raise PaymentProcessingError(_("Payment not found or invalid for company."))

    if payment.status == PaymentStatus.VOID.value: logger.info(
        f"{log_prefix} Payment {payment.pk} already VOID."); return payment

    payment._recalculate_applied_amounts_and_status(save_instance=True)
    if payment.amount_applied > ZERO:
        raise PaymentProcessingError(_("Cannot VOID pmt '%(ref)s' with allocations (%(app)s). Unallocate first.") % {
            'ref': payment.reference_number or payment.pk, 'app': payment.amount_applied})

    original_status_log = payment.status;
    gl_reversal_num: Optional[str] = None
    if payment.related_gl_voucher_id:
        try:
            original_gl = Voucher.objects.get(pk=payment.related_gl_voucher_id, company=company)
            if original_gl.status == TransactionStatus.POSTED.value:
                reversing_gl = voucher_service.create_reversing_voucher(company_id=company.id,
                                                                        original_voucher_id=original_gl.pk, user=user,
                                                                        reversal_date=effective_void_date,
                                                                        reversal_voucher_type_value=getattr(VoucherType,
                                                                                                            'RECEIPT_REVERSAL',
                                                                                                            VoucherType.GENERAL_REVERSAL).value,
                                                                        post_immediately=True)
                gl_reversal_num = reversing_gl.voucher_number;
                logger.info(f"{log_prefix} Created reversal GL {gl_reversal_num}.")
            else:
                logger.warning(
                    f"{log_prefix} Original GL {original_gl.voucher_number} for pmt not POSTED. No GL reversal.")
        except Voucher.DoesNotExist:
            logger.error(
                f"{log_prefix} CRITICAL: Orig GL Vch ID {payment.related_gl_voucher_id} for pmt not found. Manual GL adj. needed.")
        except Exception as e_rev:
            logger.error(f"{log_prefix} Error creating GL reversal for pmt void: {e_rev}",
                         exc_info=True); raise PaymentProcessingError(
                _("Failed GL reversal for pmt: %(e)s") % {'e': str(e_rev)})

    payment.status = PaymentStatus.VOID.value
    payment.notes = (
                                payment.notes or "") + f"\nVOIDED {effective_void_date.strftime('%Y-%m-%d')} by {user.name}. Reason: {void_reason}. OrigStat: {original_status_log}. " + (
                        f"GL Reversal: {gl_reversal_num}." if gl_reversal_num else "No GL reversal.")
    payment.updated_by = user;
    payment.amount_applied = ZERO;
    payment.amount_unapplied = ZERO
    payment.save(update_fields=['status', 'notes', 'amount_applied', 'amount_unapplied', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Payment {payment.pk} VOIDED.")
    return payment