# crp_accounting/services/payables_service.py

import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import List, Dict, Any, Optional, Union, Tuple

from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.contrib.auth import get_user_model  # For User model type
from django.conf import settings  # For settings related to default account codes if any

# --- Company & Settings Imports ---
from company.models import Company

try:
    from company.models_settings import CompanyAccountingSettings
except ImportError:
    CompanyAccountingSettings = None  # type: ignore
    logging.critical(
        "Payables Service: CRITICAL - CompanyAccountingSettings model not found. Default accounts will fail.")

# --- App-Specific Model Imports ---
from crp_accounting.models.payables import (
    BillSequence, VendorBill, BillLine,
    VendorPayment, VendorPaymentAllocation, PaymentSequence
)
from crp_accounting.models.party import Party
from crp_accounting.models.coa import Account
from crp_accounting.models.period import AccountingPeriod
# Use VoucherType from journal if that's its primary definition for GL posting
from crp_accounting.models.journal import Voucher, TransactionStatus, DrCrType, VoucherType as JournalVoucherType

# --- Core/Enum Imports ---
# If VoucherType is also in crp_core for other uses, ensure consistent usage or clear aliasing.
# For GL posting, JournalVoucherType is likely more relevant.
from crp_core.enums import PartyType, AccountType  # Assuming DrCrType, TransactionStatus are from journal.py

# --- Service Imports ---
from . import voucher_service

logger = logging.getLogger("crp_accounting.services.payables")
User = get_user_model()
ZERO_DECIMAL = Decimal('0.00')  # ✅ Ensure this is defined at module level


# --- Custom Service Exceptions ---
class PayablesServiceError(Exception): pass


class BillProcessingError(PayablesServiceError): pass


class PaymentProcessingError(PayablesServiceError): pass


class AllocationError(PayablesServiceError): pass


class SequenceGenerationError(PayablesServiceError): pass


class GLPostingError(PayablesServiceError): pass


# =============================================================================
# Sequence Generation Helpers (Bill & Payment - with Period Reset)
# =============================================================================
@transaction.atomic
def _generate_next_document_number(
        company: Company,
        sequence_model: Union[type[BillSequence], type[PaymentSequence]],
        default_prefix: str,
        target_date: date
) -> str:
    log_prefix = f"[GenDocNum][Co:{company.pk}][Prefix:{default_prefix}][Model:{sequence_model.__name__}]"

    sequence_config, created = sequence_model.objects.select_for_update().get_or_create(
        company=company,
        prefix=default_prefix,
        defaults={
            'current_number': 0,
            'padding_digits': getattr(sequence_model._meta.get_field('padding_digits'), 'default', 5),
            'period_format_for_reset': getattr(company,
                                               f"default_{sequence_model._meta.model_name.lower()}_reset_format", '%Y'),
            'current_period_key': None
        }
    )
    calculated_period_key_for_date = sequence_config.get_period_key_for_date(target_date or timezone.now().date())

    if created:
        sequence_config.current_period_key = calculated_period_key_for_date
        logger.info(
            f"{log_prefix} Created new {sequence_model.__name__} PK {sequence_config.pk}, PeriodKey '{sequence_config.current_period_key}'.")
    elif sequence_config.period_format_for_reset and sequence_config.current_period_key != calculated_period_key_for_date:
        logger.info(
            f"{log_prefix} {sequence_model.__name__} {sequence_config.pk}: Period reset. Old: {sequence_config.current_period_key}, New: {calculated_period_key_for_date}.")
        sequence_config.current_number = 0
        sequence_config.current_period_key = calculated_period_key_for_date

    next_number_val = sequence_config.current_number + 1
    sequence_config.current_number = next_number_val
    formatted_number = sequence_config.format_number(next_number_val)

    conflict_exists = False
    if sequence_model == BillSequence:
        if VendorBill.objects.filter(company=company, bill_number=formatted_number).exists(): conflict_exists = True
    elif sequence_model == PaymentSequence:
        if VendorPayment.objects.filter(company=company,
                                        payment_number=formatted_number).exists(): conflict_exists = True

    if conflict_exists:
        logger.error(
            f"{log_prefix} CRITICAL: Generated number {formatted_number} already exists! SeqPK={sequence_config.pk}, CurrentNo={sequence_config.current_number}")
        raise SequenceGenerationError(_("Generated document number '%(num)s' conflicts.") % {'num': formatted_number})

    fields_to_update = ['current_number', 'updated_at']  # ✅ Correctly defined and used
    if sequence_config.period_format_for_reset:
        fields_to_update.append('current_period_key')

    sequence_config.save(update_fields=list(set(fields_to_update)))
    logger.info(
        f"{log_prefix} Generated number: {formatted_number} (SeqNo: {next_number_val}) from SeqPK {sequence_config.pk}")
    return formatted_number


def get_next_bill_number(company: Company, bill_date: date, prefix_override: Optional[str] = None) -> str:
    default_prefix = prefix_override or getattr(company, 'default_bill_prefix',
                                                'BILL-')  # Example: Get from company setting
    return _generate_next_document_number(company, BillSequence, default_prefix, bill_date)


def get_next_payment_number(company: Company, payment_date: date, prefix_override: Optional[str] = None) -> str:
    default_prefix = prefix_override or getattr(company, 'default_vendor_payment_prefix', 'VPAY-')  # Example
    return _generate_next_document_number(company, PaymentSequence, default_prefix, payment_date)


# =============================================================================
# Vendor Bill Services
# =============================================================================
@transaction.atomic
def create_vendor_bill(
        company_id: Any, supplier_id: Any, issue_date: date, currency: str,
        lines_data: List[Dict[str, Any]], created_by_user: User,
        supplier_bill_reference: Optional[str] = None, due_date: Optional[date] = None,
        notes: Optional[str] = None, status: str = VendorBill.BillStatus.DRAFT.value,
        bill_number_override: Optional[str] = None
) -> VendorBill:
    log_prefix = f"[CreateBill][Co:{company_id}][User:{created_by_user.username}]"
    # ... (rest of the function as previously corrected - validation, model creation, line creation)
    # This part assumes the main body of create_vendor_bill from previous response is largely okay.
    # The focus here is fixing the specific errors you pointed out.
    # ... (ensure final_bill_number is assigned correctly using get_next_bill_number)
    try:
        company = Company.objects.get(pk=company_id)
        supplier = Party.objects.get(pk=supplier_id, company=company, party_type=PartyType.SUPPLIER.value,
                                     is_active=True)
    except Company.DoesNotExist:
        raise BillProcessingError(_("Invalid company for bill."))
    except Party.DoesNotExist:
        raise BillProcessingError(_("Active supplier not found or invalid for company."))
    if not lines_data: raise BillProcessingError(_("Vendor bill must have at least one line item."))
    if not currency: raise BillProcessingError(_("Bill currency is required."))

    final_bill_number = bill_number_override
    if not final_bill_number and status != VendorBill.BillStatus.DRAFT.value:
        final_bill_number = get_next_bill_number(company, issue_date)

    vendor_bill = VendorBill(company=company, supplier=supplier, bill_number=final_bill_number,
                             supplier_bill_reference=supplier_bill_reference, issue_date=issue_date, due_date=due_date,
                             currency=currency, status=status, notes=notes, created_by=created_by_user,
                             updated_by=created_by_user)
    try:
        exclude_clean = ['subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due',
                         'related_gl_voucher', 'approved_by', 'approved_at']
        if not final_bill_number and status == VendorBill.BillStatus.DRAFT.value: exclude_clean.append('bill_number')
        vendor_bill.full_clean(exclude=exclude_clean)
    except DjangoValidationError as e:
        raise BillProcessingError(e.message_dict)
    vendor_bill.save()
    for line_data in lines_data:
        try:
            expense_acc_id = line_data.get('expense_account_id');
            if not expense_acc_id: raise DjangoValidationError({'expense_account_id': _("Expense account ID missing.")})
            expense_account = Account.objects.get(pk=expense_acc_id, company=company, is_active=True,
                                                  allow_direct_posting=True)
            if expense_account.account_type in [AccountType.INCOME.value, AccountType.EQUITY.value] or (
                    expense_account.is_control_account and expense_account.control_account_party_type in [
                PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]):
                raise DjangoValidationError({'expense_account_id': _("Invalid account type for bill line expense.")})
            line = BillLine(company=company, vendor_bill=vendor_bill, expense_account=expense_account,
                            description=line_data.get('description', ''),
                            quantity=Decimal(str(line_data.get('quantity', '1'))),
                            unit_price=Decimal(str(line_data.get('unit_price', '0'))),
                            tax_amount_on_line=Decimal(str(line_data.get('tax_amount_on_line', '0'))),
                            created_by=created_by_user, updated_by=created_by_user)
            line.save()
        except (Account.DoesNotExist, DjangoValidationError, ValueError, TypeError) as e_line:
            err_msg = e_line.message_dict if hasattr(e_line, 'message_dict') else str(
                e_line); raise BillProcessingError(
                _("Error in bill line: %(e)s. Data: %(d)s") % {'e': err_msg, 'd': line_data})
    vendor_bill._recalculate_derived_fields(perform_save=True)
    logger.info(
        f"{log_prefix} Bill {vendor_bill.bill_number or vendor_bill.pk} created. Status: '{vendor_bill.get_status_display()}'")
    return vendor_bill


# ... (submit_vendor_bill_for_approval, approve_vendor_bill functions as previously defined, ensure they are complete)

@transaction.atomic
def post_vendor_bill_to_gl(bill_id: Any, company_id: Any, posting_user: User,
                           posting_date: Optional[date] = None) -> VendorBill:
    # ... (function body as previously corrected for tax account lookup via CompanyAccountingSettings)
    # This function also needs to ensure ZERO_DECIMAL is used correctly.
    # ...
    log_prefix = f"[PostBillGL][Co:{company_id}][User:{posting_user.username}][Bill:{bill_id}]"
    try:
        bill = VendorBill.objects.select_related('company', 'supplier', 'related_gl_voucher').get(pk=bill_id,
                                                                                                  company_id=company_id)
    except VendorBill.DoesNotExist:
        raise BillProcessingError(_("Vendor bill not found for GL posting."))
    if bill.status != VendorBill.BillStatus.APPROVED.value: raise BillProcessingError(
        _("Only APPROVED bills can be posted to GL. Status: %(s)s") % {'s': bill.get_status_display()})
    if bill.related_gl_voucher_id and bill.related_gl_voucher.status == TransactionStatus.POSTED.value: logger.info(
        f"{log_prefix} Bill {bill.bill_number} already posted. Skipping."); return bill
    bill._recalculate_derived_fields(perform_save=True)
    final_posting_date = posting_date or bill.issue_date
    try:
        acc_settings = bill.company.accounting_settings;
        ap_control_account = acc_settings.default_ap_control_account;
        input_tax_account = acc_settings.default_input_tax_account
        if not ap_control_account: raise GLPostingError(_("Default AP Control Account not set."))
    except Company.accounting_settings.RelatedObjectDoesNotExist:
        raise GLPostingError(_("Company Accounting Settings missing."))
    except AttributeError:
        raise GLPostingError(_("Required default accounts missing in Company Settings."))
    if ap_control_account.company_id != bill.company_id: raise GLPostingError(_("AP Control Account Co. mismatch."))
    if ap_control_account.account_type != AccountType.LIABILITY.value: raise GLPostingError(
        _("AP Control Account not Liability type."))
    if input_tax_account and input_tax_account.company_id != bill.company_id: raise GLPostingError(
        _("Input Tax Account Co. mismatch."))
    if input_tax_account and input_tax_account.account_type != AccountType.ASSET.value: raise GLPostingError(
        _("Input Tax Account not Asset type for recoverable tax."))
    try:
        accounting_period = AccountingPeriod.objects.get(company=bill.company, start_date__lte=final_posting_date,
                                                         end_date__gte=final_posting_date,
                                                         status=AccountingPeriod.PeriodStatus.OPEN)
    except AccountingPeriod.DoesNotExist:
        raise GLPostingError(_("No open period for posting date: %(d)s") % {'d': final_posting_date})
    except AccountingPeriod.MultipleObjectsReturned:
        raise GLPostingError(_("Multiple open periods for posting date: %(d)s.") % {'d': final_posting_date})
    voucher_lines_data = []
    for line in bill.lines.all(): voucher_lines_data.append(
        {'account_id': line.expense_account_id, 'dr_cr': DrCrType.DEBIT.value, 'amount': line.amount,
         'narration': f"Bill {bill.bill_number}: {line.description}"})
    if bill.tax_amount > ZERO_DECIMAL and input_tax_account:
        voucher_lines_data.append(
            {'account_id': input_tax_account.pk, 'dr_cr': DrCrType.DEBIT.value, 'amount': bill.tax_amount,
             'narration': f"Input Tax for Bill {bill.bill_number}"})
    elif bill.tax_amount > ZERO_DECIMAL and not input_tax_account:
        raise GLPostingError(_("Bill has tax, but no Default Input Tax Account configured."))
    voucher_lines_data.append(
        {'account_id': ap_control_account.pk, 'dr_cr': DrCrType.CREDIT.value, 'amount': bill.total_amount,
         'narration': f"A/P for Bill {bill.bill_number} from {bill.supplier.name}"})

    # Assuming voucher_service.create_and_post_voucher handles draft, submit, approve, post
    gl_voucher = voucher_service.create_and_post_voucher(company=bill.company, accounting_period=accounting_period,
                                                         voucher_type_val=JournalVoucherType.PURCHASE_INVOICE.value,
                                                         date=final_posting_date,
                                                         narration=f"Vendor Bill {bill.bill_number} - {bill.supplier.name}",
                                                         currency=bill.currency, lines_data=voucher_lines_data,
                                                         created_by_user=posting_user, party_pk=bill.supplier_id,
                                                         reference=bill.supplier_bill_reference or bill.bill_number)

    bill.related_gl_voucher = gl_voucher;
    bill.updated_by = posting_user
    bill.save(update_fields=['related_gl_voucher', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Bill {bill.bill_number} posted to GL {gl_voucher.voucher_number}.")
    return bill


# ... (void_vendor_bill)

@transaction.atomic
def create_vendor_payment(
        company_id: Any, supplier_id: Any, payment_date: date,
        payment_account_id: Any, currency: str, payment_amount: Decimal,
        created_by_user: User, payment_method: Optional[str] = None,
        reference_details: Optional[str] = None, notes: Optional[str] = None,
        status: str = VendorPayment.PaymentStatus.DRAFT.value,
        payment_number_override: Optional[str] = None
) -> VendorPayment:
    # ... (validation and fetching company, supplier, payment_account as before)
    # ... (payment_amount > ZERO_DECIMAL check)
    # ...
    log_prefix = f"[CreateVPay][Co:{company_id}][User:{created_by_user.username}]"
    try:
        company = Company.objects.get(pk=company_id);
        supplier = Party.objects.get(pk=supplier_id, company=company, party_type=PartyType.SUPPLIER.value,
                                     is_active=True)
        payment_account = Account.objects.get(pk=payment_account_id, company=company,
                                              account_type=AccountType.ASSET.value, is_active=True,
                                              allow_direct_posting=True)
    except Company.DoesNotExist:
        raise PaymentProcessingError(_("Invalid company."))
    except Party.DoesNotExist:
        raise PaymentProcessingError(_("Active supplier not found/invalid."))
    except Account.DoesNotExist:
        raise PaymentProcessingError(_("Payment account invalid/inactive/no direct posting."))
    if payment_amount <= ZERO_DECIMAL: raise PaymentProcessingError(
        _("Payment amount must be positive."))  # ✅ Use ZERO_DECIMAL
    if not currency: raise PaymentProcessingError(_("Payment currency required."))

    final_payment_number = payment_number_override
    if not final_payment_number and status != VendorPayment.PaymentStatus.DRAFT.value:
        final_payment_number = get_next_payment_number(company, payment_date)

    vendor_payment = VendorPayment(company=company, supplier=supplier, payment_number=final_payment_number,
                                   payment_date=payment_date,
                                   payment_method=payment_method or VendorPayment.PaymentMethod.OTHER.value,
                                   payment_account=payment_account, currency=currency, payment_amount=payment_amount,
                                   status=status, reference_details=reference_details, notes=notes,
                                   created_by=created_by_user, updated_by=created_by_user)
    try:
        exclude_clean = ['allocated_amount', 'unallocated_amount', 'related_gl_voucher']
        if not final_payment_number and status == VendorPayment.PaymentStatus.DRAFT.value: exclude_clean.append(
            'payment_number')
        vendor_payment.full_clean(exclude=exclude_clean)
    except DjangoValidationError as e:
        raise PaymentProcessingError(e.message_dict)
    vendor_payment.save()  # Model save sets initial unallocated_amount
    logger.info(
        f"{log_prefix} Vendor Payment {vendor_payment.payment_number or vendor_payment.pk} created. Status: '{vendor_payment.get_status_display()}'")
    return vendor_payment


# ... (approve_vendor_payment)

@transaction.atomic
def post_vendor_payment_to_gl(
        payment_id: Any, company_id: Any, posting_user: User, posting_date: Optional[date] = None
) -> VendorPayment:
    # ... (fetch payment, validation for status and related_gl_voucher as before)
    # ... (determine AP control account from CompanyAccountingSettings)
    # ... (find AccountingPeriod)
    log_prefix = f"[PostVPayGL][Co:{company_id}][User:{posting_user.username}][Pmt:{payment_id}]"
    try:
        payment = VendorPayment.objects.select_related('company', 'supplier', 'payment_account',
                                                       'related_gl_voucher').get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        raise PaymentProcessingError(_("Vendor payment not found for GL posting."))
    if payment.status != VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value: raise PaymentProcessingError(
        _("Only payments 'Approved for Payment' can be posted. Status: %(s)s") % {'s': payment.get_status_display()})
    if payment.related_gl_voucher_id and payment.related_gl_voucher.status == TransactionStatus.POSTED.value: logger.info(
        f"{log_prefix} Payment {payment.payment_number} already posted. Skipping."); return payment
    if payment.payment_amount <= ZERO_DECIMAL: raise PaymentProcessingError(
        _("Cannot post zero/negative payment."))  # ✅ Use ZERO_DECIMAL

    final_posting_date = posting_date or payment.payment_date
    try:
        acc_settings = payment.company.accounting_settings;
        ap_control_account = acc_settings.default_ap_control_account
        if not ap_control_account: raise GLPostingError(_("Default AP Control Account not set."))
    except Company.accounting_settings.RelatedObjectDoesNotExist:
        raise GLPostingError(_("Company Accounting Settings missing."))
    except AttributeError:
        raise GLPostingError(_("AP Control Account missing in Company Settings."))
    if ap_control_account.company_id != payment.company_id: raise GLPostingError(_("AP Control Account Co. mismatch."))
    if ap_control_account.account_type != AccountType.LIABILITY.value: raise GLPostingError(
        _("AP Control Account not Liability type."))
    try:
        accounting_period = AccountingPeriod.objects.get(company=payment.company, start_date__lte=final_posting_date,
                                                         end_date__gte=final_posting_date,
                                                         status=AccountingPeriod.PeriodStatus.OPEN)
    except AccountingPeriod.DoesNotExist:
        raise GLPostingError(_("No open period for posting date: %(d)s") % {'d': final_posting_date})
    except AccountingPeriod.MultipleObjectsReturned:
        raise GLPostingError(_("Multiple open periods for posting date: %(d)s.") % {'d': final_posting_date})

    voucher_lines_data = [
        {'account_id': ap_control_account.pk, 'dr_cr': DrCrType.DEBIT.value, 'amount': payment.payment_amount,
         'narration': f"Payment {payment.payment_number} to {payment.supplier.name}"},
        {'account_id': payment.payment_account_id, 'dr_cr': DrCrType.CREDIT.value, 'amount': payment.payment_amount,
         'narration': f"Payment {payment.payment_number} from {payment.payment_account.account_name}"}
    ]

    gl_voucher = voucher_service.create_and_post_voucher(company=payment.company, accounting_period=accounting_period,
                                                         voucher_type_val=JournalVoucherType.VENDOR_PAYMENT.value,
                                                         # ✅ Use JournalVoucherType or consistent VoucherType
                                                         date=final_posting_date,
                                                         narration=f"Vendor Pmt {payment.payment_number} - {payment.supplier.name}",
                                                         currency=payment.currency, lines_data=voucher_lines_data,
                                                         created_by_user=posting_user,
                                                         party_pk=payment.supplier_id,
                                                         reference=payment.reference_details
                                                         )

    payment.related_gl_voucher = gl_voucher
    payment.status = VendorPayment.PaymentStatus.PAID_COMPLETED.value
    payment.updated_by = posting_user
    payment.save(update_fields=['related_gl_voucher', 'status', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Vendor Pmt {payment.payment_number} posted to GL {gl_voucher.voucher_number}.")
    return payment


@transaction.atomic
def allocate_payment_to_bills(
        payment_id: Any, company_id: Any, allocation_user: User,
        allocations_data: List[Dict[str, Any]],  # [{'bill_id': pk, 'amount_allocated': Decimal}, ...]
        allocation_date: Optional[date] = None
) -> VendorPayment:
    # ... (function body as previously defined and corrected for validation)
    # Ensure ZERO_DECIMAL is used correctly here.
    # ...
    log_prefix = f"[AllocateVPay][Co:{company_id}][User:{allocation_user.username}][Pmt:{payment_id}]"
    try:
        payment = VendorPayment.objects.select_for_update().get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        raise AllocationError(_("Vendor payment not found."))
    if payment.status != VendorPayment.PaymentStatus.PAID_COMPLETED.value: raise AllocationError(
        _("Only COMPLETED payments can be allocated. Status: %(s)s") % {'s': payment.get_status_display()})
    final_allocation_date = allocation_date or payment.payment_date
    total_new_allocation_amount = sum(Decimal(str(data.get('amount_allocated', '0'))) for data in allocations_data)
    if total_new_allocation_amount > payment.unallocated_amount + Decimal('0.01'): raise AllocationError(
        _("Total new alloc (%(new)s) > payment unalloc (%(un)s).") % {'new': total_new_allocation_amount,
                                                                      'un': payment.unallocated_amount})

    for alloc_data in allocations_data:
        bill_id = alloc_data.get('bill_id');
        amount_to_allocate = Decimal(str(alloc_data.get('amount_allocated', '0')))
        if amount_to_allocate <= ZERO_DECIMAL: continue  # ✅ Use ZERO_DECIMAL
        try:
            vendor_bill = VendorBill.objects.select_for_update().get(pk=bill_id, company=payment.company,
                                                                     supplier=payment.supplier)
        except VendorBill.DoesNotExist:
            raise AllocationError(_("Bill ID %(id)s for alloc not found for supplier/co.") % {'id': bill_id})
        if vendor_bill.currency != payment.currency: raise AllocationError(
            _("Currency mismatch: Pmt (%(pc)s) vs Bill (%(bc)s).") % {'pc': payment.currency,
                                                                      'bc': vendor_bill.currency})
        if vendor_bill.status == VendorBill.BillStatus.VOID.value: raise AllocationError(
            _("Cannot alloc to VOID bill %(b)s.") % {'b': vendor_bill.bill_number})
        vendor_bill._recalculate_derived_fields(perform_save=True);
        vendor_bill.refresh_from_db(fields=['amount_due'])
        if amount_to_allocate > vendor_bill.amount_due + Decimal('0.01'): raise AllocationError(
            _("Alloc for Bill %(b)s (Amt:%(a)s) exceeds due (%(d)s).") % {'b': vendor_bill.bill_number,
                                                                          'a': amount_to_allocate,
                                                                          'd': vendor_bill.amount_due})
        current_pmt_applied_total = \
        payment.bill_allocations.all().aggregate(s=Sum('allocated_amount', default=ZERO_DECIMAL))['s'] or ZERO_DECIMAL
        if (current_pmt_applied_total + amount_to_allocate).quantize(Decimal('0.01')) > payment.payment_amount.quantize(
            Decimal('0.01')) + Decimal('0.005'): raise AllocationError(
            _("Alloc Err Pmt %(p)s: Total applied (%(ta)s + %(na)s) > Rcvd (%(r)s).") % {
                'p': payment.reference_number or payment.pk, 'ta': current_pmt_applied_total, 'na': amount_to_allocate,
                'r': payment.payment_amount})
        allocation, created = VendorPaymentAllocation.objects.update_or_create(vendor_payment=payment,
                                                                               vendor_bill=vendor_bill,
                                                                               company=payment.company, defaults={
                'allocated_amount': amount_to_allocate, 'allocation_date': final_allocation_date,
                'created_by': allocation_user if created else payment.created_by, 'updated_by': allocation_user})
        logger.info(
            f"{log_prefix} {'Created' if created else 'Updated'} allocation of {amount_to_allocate} to Bill {vendor_bill.bill_number}.")
    payment._recalculate_derived_fields(perform_save=True)
    logger.info(
        f"{log_prefix} Payment {payment.payment_number} allocations processed. Unallocated: {payment.unallocated_amount}")
    return payment


# ... (void_vendor_bill and void_vendor_payment should be complete and use ZERO_DECIMAL correctly)
@transaction.atomic
def void_vendor_bill(vendor_bill_id: Any, company_id: Any, voiding_user: User,
                     void_reason: str = "Voided as per request", void_date: Optional[date] = None) -> VendorBill:
    log_prefix = f"[VoidBill][Co:{company_id}][User:{voiding_user.username}][Bill:{vendor_bill_id}]"  # etc.
    # ... (Fetch bill, check status, recalculate fields, set status to VOID, reverse GL, de-allocate payments, save) ...
    # Ensure all ZERO_DECIMAL checks are correct.
    # ...
    effective_void_date = void_date or timezone.now().date()
    if not void_reason: raise BillProcessingError(_("Reason required to void bill."))
    try:
        bill = VendorBill.objects.select_for_update().get(pk=vendor_bill_id, company_id=company_id)
    except VendorBill.DoesNotExist:
        raise BillProcessingError(_("Vendor bill not found."))
    if bill.status == VendorBill.BillStatus.VOID.value: logger.info(
        f"{log_prefix} Bill {bill.bill_number} already VOID."); return bill
    bill._recalculate_derived_fields(perform_save=True);
    bill.refresh_from_db(fields=['amount_paid'])
    if bill.amount_paid > ZERO_DECIMAL: raise BillProcessingError(
        _("Cannot VOID bill '%(n)s' with payments (%(p)s). Reverse payments first.") % {'n': bill.bill_number,
                                                                                        'p': bill.amount_paid})
    original_status_log = bill.status;
    gl_reversal_num: Optional[str] = None
    if bill.related_gl_voucher_id:
        try:
            original_gl = Voucher.objects.get(pk=bill.related_gl_voucher_id, company_id=company_id)
            if original_gl.status == TransactionStatus.POSTED.value:
                reversing_gl = voucher_service.create_reversing_voucher(company_id=company_id,
                                                                        original_voucher_id=original_gl.pk,
                                                                        user=voiding_user,
                                                                        reversal_date=effective_void_date,
                                                                        reversal_voucher_type_value=getattr(
                                                                            JournalVoucherType, 'PURCHASE_REVERSAL',
                                                                            JournalVoucherType.GENERAL_REVERSAL).value,
                                                                        post_immediately=True)
                gl_reversal_num = reversing_gl.voucher_number;
                logger.info(f"{log_prefix} Created GL reversal {gl_reversal_num}.")
            else:
                logger.warning(f"{log_prefix} Original GL {original_gl.voucher_number} not POSTED. No GL reversal.")
        except Voucher.DoesNotExist:
            logger.error(
                f"{log_prefix} CRITICAL: Original GL Vch ID {bill.related_gl_voucher_id} not found. Manual GL adj. needed.")
        except Exception as e_rev:
            logger.error(f"{log_prefix} Error creating GL reversal: {e_rev}", exc_info=True); raise BillProcessingError(
                _("Failed GL reversal: %(e)s") % {'e': str(e_rev)})
    bill.status = VendorBill.BillStatus.VOID.value;
    bill.notes = (
                             bill.notes or "") + f"\nVOIDED {effective_void_date.strftime('%Y-%m-%d')} by {voiding_user.username}. Reason: {void_reason}. OrigStat: {original_status_log}. " + (
                     f"GL Reversal: {gl_reversal_num}." if gl_reversal_num else "No GL reversal.")
    bill.updated_by = voiding_user;
    bill.subtotal_amount, bill.tax_amount, bill.total_amount, bill.amount_paid, bill.amount_due = (
    ZERO_DECIMAL, ZERO_DECIMAL, ZERO_DECIMAL, ZERO_DECIMAL, ZERO_DECIMAL)
    bill.save(
        update_fields=['status', 'notes', 'subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due',
                       'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Bill '{bill.bill_number}' VOIDED.")
    return bill


@transaction.atomic
def void_vendor_payment(payment_id: Any, company_id: Any, voiding_user: User,
                        void_reason: str = "Voided as per request", void_date: Optional[date] = None) -> VendorPayment:
    log_prefix = f"[VoidVPay][Co:{company_id}][User:{voiding_user.username}][Pmt:{payment_id}]"  # etc.
    # ... (Fetch payment, check status, recalculate fields, set status to VOID, reverse GL, de-allocate from bills, save) ...
    # Ensure all ZERO_DECIMAL checks are correct.
    # ...
    effective_void_date = void_date or timezone.now().date()
    if not void_reason: raise PaymentProcessingError(_("Reason required to void payment."))
    try:
        payment = VendorPayment.objects.select_for_update().get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        raise PaymentProcessingError(_("Vendor payment not found."))
    if payment.status == VendorPayment.PaymentStatus.VOID.value: logger.info(
        f"{log_prefix} Payment {payment.pk} already VOID."); return payment
    payment._recalculate_derived_fields(perform_save=True);
    payment.refresh_from_db(fields=['allocated_amount'])
    if payment.allocated_amount > ZERO_DECIMAL: raise PaymentProcessingError(
        _("Cannot VOID payment '%(ref)s' with allocations (%(alloc)s). Unallocate first.") % {
            'ref': payment.reference_number or payment.pk, 'alloc': payment.allocated_amount})
    original_status_log = payment.status;
    gl_reversal_num: Optional[str] = None
    if payment.related_gl_voucher_id:
        try:
            original_gl = Voucher.objects.get(pk=payment.related_gl_voucher_id, company_id=company_id)
            if original_gl.status == TransactionStatus.POSTED.value:
                reversing_gl = voucher_service.create_reversing_voucher(company_id=company_id,
                                                                        original_voucher_id=original_gl.pk,
                                                                        user=voiding_user,
                                                                        reversal_date=effective_void_date,
                                                                        reversal_voucher_type_value=getattr(
                                                                            JournalVoucherType, 'PAYMENT_REVERSAL',
                                                                            JournalVoucherType.GENERAL_REVERSAL).value,
                                                                        post_immediately=True)
                gl_reversal_num = reversing_gl.voucher_number;
                logger.info(f"{log_prefix} Created GL reversal {gl_reversal_num}.")
            else:
                logger.warning(
                    f"{log_prefix} Original GL {original_gl.voucher_number} for payment not POSTED. No GL reversal.")
        except Voucher.DoesNotExist:
            logger.error(
                f"{log_prefix} CRITICAL: Orig GL Vch ID {payment.related_gl_voucher_id} for payment not found. Manual GL adj. needed.")
        except Exception as e_rev:
            logger.error(f"{log_prefix} Error creating GL reversal for payment void: {e_rev}",
                         exc_info=True); raise PaymentProcessingError(
                _("Failed GL reversal: %(e)s") % {'e': str(e_rev)})
    payment.status = VendorPayment.PaymentStatus.VOID.value;
    payment.notes = (
                                payment.notes or "") + f"\nVOIDED {effective_void_date.strftime('%Y-%m-%d')} by {voiding_user.username}. Reason: {void_reason}. OrigStat: {original_status_log}. " + (
                        f"GL Reversal: {gl_reversal_num}." if gl_reversal_num else "No GL reversal.")
    payment.updated_by = voiding_user;
    payment.allocated_amount = ZERO_DECIMAL;
    payment.unallocated_amount = ZERO_DECIMAL  # Voided payment has no value/allocations
    payment.save(
        update_fields=['status', 'notes', 'allocated_amount', 'unallocated_amount', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Payment {payment.pk} VOIDED.")
    return payment