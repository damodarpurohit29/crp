# crp_accounting/services/payables_service.py

import logging
from decimal import Decimal
from datetime import date
from typing import List, Dict, Any, Optional, Union, Tuple

from django.db import transaction, IntegrityError
from django.db.models import Sum, F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.contrib.auth import get_user_model
from django.conf import settings

# --- Company & Settings Imports ---
from company.models import Company

try:
    # Attempt to import CompanyAccountingSettings
    from company.models_settings import CompanyAccountingSettings
except ImportError:
    # If import fails, set to None and log a critical error.
    # Functions relying on these settings will need to handle its absence.
    CompanyAccountingSettings = None  # type: ignore
    logging.critical(
        "Payables Service: CRITICAL - CompanyAccountingSettings model not found. "
        "Default account lookups for GL posting will fail."
    )

# --- App-Specific Model Imports ---
from crp_accounting.models.payables import (
    BillSequence, VendorBill, BillLine,
    VendorPayment, VendorPaymentAllocation, PaymentSequence
)
from crp_accounting.models.party import Party
from crp_accounting.models.coa import Account
from crp_accounting.models.period import AccountingPeriod # Ensure this model is correctly imported
from crp_accounting.models.journal import Voucher, TransactionStatus, DrCrType, VoucherType as JournalVoucherType

# --- Core/Enum Imports ---
from crp_core.enums import PartyType, AccountType

# --- Service Imports ---
from . import voucher_service  # Assuming voucher_service provides create_and_post_voucher & create_reversing_voucher

logger = logging.getLogger("crp_accounting.services.payables")
User = get_user_model()  # Standard way to get the User model
ZERO_DECIMAL = Decimal('0.00')  # For consistent zero decimal comparisons and initializations


# --- Custom Service Exceptions ---
class PayablesServiceError(Exception):
    """Base exception for payables service errors."""
    pass


class BillProcessingError(PayablesServiceError):
    """Exception for errors during vendor bill processing."""
    pass


class PaymentProcessingError(PayablesServiceError):
    """Exception for errors during vendor payment processing."""
    pass


class AllocationError(PayablesServiceError):
    """Exception for errors during payment allocation."""
    pass


class SequenceGenerationError(PayablesServiceError):
    """Exception for errors during document number generation."""
    pass


class GLPostingError(PayablesServiceError):
    """Exception for errors during General Ledger posting."""
    pass


# =============================================================================
# Sequence Generation Helpers
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
            'period_format_for_reset': getattr(
                company,
                f"default_{sequence_model._meta.model_name.lower()}_reset_format",
                '%Y'
            ),
            'current_period_key': None
        }
    )
    calculated_period_key_for_date = sequence_config.get_period_key_for_date(target_date or timezone.now().date())
    if created:
        sequence_config.current_period_key = calculated_period_key_for_date
        logger.info(
            f"{log_prefix} Created new {sequence_model.__name__} PK {sequence_config.pk}, "
            f"PeriodKey '{sequence_config.current_period_key}' initialized."
        )
    elif sequence_config.period_format_for_reset and sequence_config.current_period_key != calculated_period_key_for_date:
        logger.info(
            f"{log_prefix} {sequence_model.__name__} {sequence_config.pk}: Period reset triggered. "
            f"Old PeriodKey: {sequence_config.current_period_key}, New PeriodKey: {calculated_period_key_for_date}."
        )
        sequence_config.current_number = 0
        sequence_config.current_period_key = calculated_period_key_for_date
    next_number_val = sequence_config.current_number + 1
    sequence_config.current_number = next_number_val
    formatted_number = sequence_config.format_number(next_number_val)
    conflict_exists = False
    if sequence_model == BillSequence:
        if VendorBill.objects.filter(company=company, bill_number=formatted_number).exists():
            conflict_exists = True
    elif sequence_model == PaymentSequence:
        if VendorPayment.objects.filter(company=company, payment_number=formatted_number).exists():
            conflict_exists = True
    if conflict_exists:
        logger.error(
            f"{log_prefix} CRITICAL: Generated number {formatted_number} already exists in "
            f"{'VendorBill' if sequence_model == BillSequence else 'VendorPayment'}! "
            f"Sequence PK={sequence_config.pk}, Current DB No={sequence_config.current_number} (before save)."
        )
        raise SequenceGenerationError(
            _("Generated document number '%(num)s' conflicts with an existing document. Please try again.") % {
                'num': formatted_number}
        )
    fields_to_update = ['current_number', 'updated_at']
    if sequence_config.period_format_for_reset or created:
        fields_to_update.append('current_period_key')
    try:
        sequence_config.save(update_fields=list(set(fields_to_update)))
    except IntegrityError as e:
        logger.error(f"{log_prefix} IntegrityError saving sequence {sequence_config.pk}: {e}", exc_info=True)
        raise SequenceGenerationError(_("Failed to save sequence due to a database integrity issue."))
    logger.info(
        f"{log_prefix} Generated number: {formatted_number} (Raw SeqNo: {next_number_val}) "
        f"from Sequence PK {sequence_config.pk}."
    )
    return formatted_number

def get_next_bill_number(company: Company, bill_date: date, prefix_override: Optional[str] = None) -> str:
    default_prefix = prefix_override or getattr(company, 'default_bill_prefix', 'BILL-')
    return _generate_next_document_number(company, BillSequence, default_prefix, bill_date)

def get_next_payment_number(company: Company, payment_date: date, prefix_override: Optional[str] = None) -> str:
    default_prefix = prefix_override or getattr(company, 'default_vendor_payment_prefix', 'VPAY-')
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
    log_prefix = f"[CreateBill][Co:{company_id}][User:{created_by_user.name}]"
    try:
        company = Company.objects.get(pk=company_id)
        supplier = Party.objects.get(pk=supplier_id, company=company, party_type=PartyType.SUPPLIER.value, is_active=True)
    except Company.DoesNotExist:
        raise BillProcessingError(_("Invalid company ID provided for the bill."))
    except Party.DoesNotExist:
        raise BillProcessingError(_("Active supplier not found or is invalid for the specified company."))
    if not lines_data: raise BillProcessingError(_("A vendor bill must have at least one line item."))
    if not currency: raise BillProcessingError(_("Bill currency is required."))
    final_bill_number = bill_number_override
    if not final_bill_number and status != VendorBill.BillStatus.DRAFT.value:
        final_bill_number = get_next_bill_number(company, issue_date)
    vendor_bill = VendorBill(
        company=company, supplier=supplier, bill_number=final_bill_number,
        supplier_bill_reference=supplier_bill_reference, issue_date=issue_date, due_date=due_date,
        currency=currency, status=status, notes=notes, created_by=created_by_user, updated_by=created_by_user
    )
    try:
        exclude_from_clean = ['subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due',
                              'related_gl_voucher', 'approved_by', 'approved_at']
        if not final_bill_number and status == VendorBill.BillStatus.DRAFT.value:
            exclude_from_clean.append('bill_number')
        vendor_bill.full_clean(exclude=exclude_from_clean)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation error for bill header: {e.message_dict}", exc_info=True)
        raise BillProcessingError(e.message_dict)
    vendor_bill.save()
    for line_data in lines_data:
        try:
            expense_acc_id = line_data.get('expense_account_id')
            if not expense_acc_id:
                raise DjangoValidationError({'expense_account_id': _("Expense account ID is missing for a line item.")})
            expense_account = Account.objects.get(pk=expense_acc_id, company=company, is_active=True, allow_direct_posting=True)
            if expense_account.account_type in [AccountType.INCOME.value, AccountType.EQUITY.value] or \
               (expense_account.is_control_account and expense_account.control_account_party_type in [PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]):
                raise DjangoValidationError({'expense_account_id': _("Invalid account type ('%(type)s') for bill line expense. Account: %(acc)s.") %
                                              {'type': expense_account.get_account_type_display(), 'acc': expense_account.account_name}})
            line = BillLine(
                company=company, vendor_bill=vendor_bill, expense_account=expense_account,
                description=line_data.get('description', ''),
                quantity=Decimal(str(line_data.get('quantity', '1'))),
                unit_price=Decimal(str(line_data.get('unit_price', '0'))),
                tax_amount_on_line=Decimal(str(line_data.get('tax_amount_on_line', '0'))),
                created_by=created_by_user, updated_by=created_by_user
            )
            line.full_clean(); line.save()
        except (Account.DoesNotExist, DjangoValidationError, ValueError, TypeError) as e_line:
            err_msg = e_line.message_dict if hasattr(e_line, 'message_dict') else str(e_line)
            logger.error(f"{log_prefix} Error processing bill line: {err_msg}. Data: {line_data}", exc_info=True)
            raise BillProcessingError(_("Error in bill line: %(error)s. Line Data: %(data)s") % {'error': err_msg, 'data': line_data})
    vendor_bill._recalculate_derived_fields(perform_save=True)
    logger.info(f"{log_prefix} Vendor Bill {vendor_bill.bill_number or vendor_bill.pk} created successfully. Status: '{vendor_bill.get_status_display()}'.")
    return vendor_bill

@transaction.atomic
def submit_vendor_bill_for_approval(bill_id: Any, company_id: Any, user: User) -> VendorBill:
    log_prefix = f"[SubmitBillApproval][Co:{company_id}][User:{user.name}][Bill:{bill_id}]"
    logger.info(f"{log_prefix} Attempting to submit bill for approval.")
    try:
        bill = VendorBill.objects.select_related('company').get(pk=bill_id, company_id=company_id)
    except VendorBill.DoesNotExist:
        logger.error(f"{log_prefix} Vendor bill not found.")
        raise BillProcessingError(_("Vendor bill not found."))
    if bill.status != VendorBill.BillStatus.DRAFT.value:
        logger.warning(f"{log_prefix} Bill {bill.bill_number or bill.pk} is not in DRAFT status. Current status: {bill.get_status_display()}.")
        raise BillProcessingError(_("Only DRAFT bills can be submitted for approval. Current status is '%(status)s'.") % {'status': bill.get_status_display()})
    if not bill.lines.exists():
        logger.error(f"{log_prefix} Bill {bill.bill_number or bill.pk} has no line items.")
        raise BillProcessingError(_("Cannot submit a bill with no line items. Please add lines first."))
    try:
        bill._recalculate_derived_fields(perform_save=False)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation error during recalculation for bill {bill.bill_number or bill.pk}: {e.message_dict if hasattr(e, 'message_dict') else e}", exc_info=True)
        raise BillProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    fields_to_update = ['status', 'updated_by', 'updated_at']
    if not bill.bill_number or not bill.bill_number.strip():
        if not bill.company:
            logger.error(f"{log_prefix} Bill {bill.pk} is missing company information for bill number generation.")
            raise BillProcessingError(_("Bill is missing company information; cannot generate bill number."))
        try:
            bill.bill_number = get_next_bill_number(bill.company, bill.issue_date)
            fields_to_update.append('bill_number')
            logger.info(f"{log_prefix} Generated bill number {bill.bill_number} for bill {bill.pk}.")
        except SequenceGenerationError as e:
            logger.error(f"{log_prefix} Failed to generate bill number for bill {bill.pk}: {e}", exc_info=True)
            raise BillProcessingError(str(e))
    bill.status = VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value
    bill.updated_by = user
    try:
        exclude_fields_from_clean = ['approved_by', 'approved_at', 'related_gl_voucher', 'amount_paid', 'amount_due']
        bill.full_clean(exclude=exclude_fields_from_clean)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation failed for bill {bill.bill_number or bill.pk} before submission: {e.message_dict if hasattr(e, 'message_dict') else e}", exc_info=True)
        raise BillProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    bill.save(update_fields=fields_to_update)
    logger.info(f"{log_prefix} Bill {bill.bill_number or bill.pk} successfully submitted for approval.")
    return bill

@transaction.atomic
def approve_vendor_bill(bill_id: Any, company_id: Any, user: User, approval_notes: Optional[str] = None) -> VendorBill:
    log_prefix = f"[ApproveBill][Co:{company_id}][User:{user.name}][Bill:{bill_id}]"
    logger.info(f"{log_prefix} Attempting to approve bill.")
    try:
        bill = VendorBill.objects.get(pk=bill_id, company_id=company_id)
    except VendorBill.DoesNotExist:
        logger.error(f"{log_prefix} Vendor bill not found.")
        raise BillProcessingError(_("Vendor bill not found."))
    if bill.status != VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value:
        logger.warning(f"{log_prefix} Bill {bill.bill_number or bill.pk} is not SUBMITTED FOR APPROVAL. Current status: {bill.get_status_display()}.")
        raise BillProcessingError(_("Only bills 'Submitted for Approval' can be approved. Current status is '%(status)s'.") % {'status': bill.get_status_display()})
    if not bill.bill_number or not bill.bill_number.strip():
        logger.error(f"{log_prefix} Bill {bill.pk} is missing a bill number at approval stage. This indicates a potential data integrity issue from the submission step.")
        raise BillProcessingError(_("Bill is missing a bill number and cannot be approved. Please review the bill's submission process."))
    try:
        bill._recalculate_derived_fields(perform_save=False)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation error during recalculation for bill {bill.bill_number}: {e.message_dict if hasattr(e, 'message_dict') else e}", exc_info=True)
        raise BillProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    bill.status = VendorBill.BillStatus.APPROVED.value
    bill.approved_by = user
    bill.approved_at = timezone.now()
    bill.updated_by = user
    fields_to_update = ['status', 'approved_by', 'approved_at', 'updated_by', 'updated_at']
    if approval_notes and approval_notes.strip():
        bill.notes = (bill.notes.strip() + "\n" if bill.notes and bill.notes.strip() else "") + f"Approval Notes: {approval_notes.strip()}"
        fields_to_update.append('notes')
    try:
        exclude_fields_from_clean = ['related_gl_voucher', 'amount_paid', 'amount_due']
        bill.full_clean(exclude=exclude_fields_from_clean)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation failed for bill {bill.bill_number} before approval: {e.message_dict if hasattr(e, 'message_dict') else e}", exc_info=True)
        raise BillProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    bill.save(update_fields=fields_to_update)
    logger.info(f"{log_prefix} Bill {bill.bill_number} successfully approved.")
    return bill

@transaction.atomic
def void_vendor_bill(vendor_bill_id: Any, company_id: Any, voiding_user: User, void_reason: str, void_date: Optional[date] = None) -> VendorBill:
    log_prefix = f"[VoidBill][Co:{company_id}][User:{voiding_user.name}][Bill:{vendor_bill_id}]"
    effective_void_date = void_date or timezone.now().date()
    if not void_reason or not void_reason.strip():
        raise BillProcessingError(_("A reason is mandatory to void a bill."))
    try:
        bill = VendorBill.objects.select_for_update().select_related('company', 'related_gl_voucher').get(pk=vendor_bill_id, company_id=company_id)
    except VendorBill.DoesNotExist:
        raise BillProcessingError(_("Vendor bill not found."))
    if bill.status == VendorBill.BillStatus.VOID.value:
        logger.info(f"{log_prefix} Bill {bill.bill_number or bill.pk} is already VOID. No action taken.")
        return bill
    bill._recalculate_derived_fields(perform_save=True)
    bill.refresh_from_db(fields=['amount_paid'])
    if bill.amount_paid > ZERO_DECIMAL:
        raise BillProcessingError(_("Cannot VOID bill '%(num)s' because it has payments applied (Amount Paid: %(paid)s). Please reverse or unallocate associated payments first.") % {'num': bill.bill_number or bill.pk, 'paid': bill.amount_paid.quantize(Decimal('0.01'))})
    original_status_log = bill.get_status_display()
    gl_reversal_voucher_number: Optional[str] = None
    if bill.related_gl_voucher_id and bill.related_gl_voucher:
        original_gl_voucher = bill.related_gl_voucher
        if original_gl_voucher.status == TransactionStatus.POSTED.value:
            try:
                # Assuming create_reversing_voucher also derives period from reversal_date and company_id
                reversing_gl_voucher = voucher_service.create_reversing_voucher(
                    company_id=company_id,
                    original_voucher_id=original_gl_voucher.pk,
                    user=voiding_user,
                    reversal_date=effective_void_date,
                    reversal_voucher_type_value=getattr(JournalVoucherType, 'PURCHASE_REVERSAL', JournalVoucherType.GENERAL).value,
                    post_immediately=True
                )
                gl_reversal_voucher_number = reversing_gl_voucher.voucher_number
                logger.info(f"{log_prefix} Created GL reversal voucher {gl_reversal_voucher_number} for original voucher {original_gl_voucher.voucher_number}.")
            except Exception as e_reversal:
                logger.error(f"{log_prefix} Error creating GL reversal for bill {bill.bill_number or bill.pk}: {e_reversal}", exc_info=True)
                raise BillProcessingError(_("Failed to create GL reversal for the bill (%(bill_num)s): %(error)s. Bill not voided.") % {'bill_num': bill.bill_number or bill.pk, 'error': str(e_reversal)})
        else:
            logger.warning(f"{log_prefix} Original GL voucher {original_gl_voucher.voucher_number} (ID: {original_gl_voucher.pk}) for bill {bill.bill_number or bill.pk} was not POSTED (Status: {original_gl_voucher.get_status_display()}). No GL reversal needed or performed.")
    elif bill.related_gl_voucher_id and not bill.related_gl_voucher:
        logger.error(f"{log_prefix} CRITICAL: Bill {bill.bill_number or bill.pk} has related_gl_voucher_id {bill.related_gl_voucher_id}, but the voucher object could not be loaded. Manual GL review may be needed.")
    bill.status = VendorBill.BillStatus.VOID.value
    void_note_user_name = voiding_user.get_full_name() or voiding_user.name
    void_note = f"VOIDED on {effective_void_date.strftime('%Y-%m-%d')} by {void_note_user_name}. Reason: {void_reason.strip()}. Original Status: {original_status_log}. "
    if gl_reversal_voucher_number: void_note += f"GL Reversal Voucher: {gl_reversal_voucher_number}."
    else: void_note += "No GL reversal performed (either not posted or original voucher was not POSTED)."
    bill.notes = (bill.notes.strip() + "\n" if bill.notes and bill.notes.strip() else "") + void_note
    bill.updated_by = voiding_user
    bill.subtotal_amount = ZERO_DECIMAL; bill.tax_amount = ZERO_DECIMAL; bill.total_amount = ZERO_DECIMAL
    bill.amount_paid = ZERO_DECIMAL; bill.amount_due = ZERO_DECIMAL
    bill.save(update_fields=['status', 'notes', 'subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Bill '{bill.bill_number or bill.pk}' successfully VOIDED.")
    return bill

# =============================================================================
# Vendor Payment Services
# =============================================================================
@transaction.atomic
def create_vendor_payment(
        company_id: Any, supplier_id: Any, payment_date: date, payment_account_id: Any, currency: str,
        payment_amount: Decimal, created_by_user: User, payment_method: Optional[str] = None,
        reference_details: Optional[str] = None, notes: Optional[str] = None,
        status: str = VendorPayment.PaymentStatus.DRAFT.value, payment_number_override: Optional[str] = None
) -> VendorPayment:
    log_prefix = f"[CreateVPay][Co:{company_id}][User:{created_by_user.name}]"
    try:
        company = Company.objects.get(pk=company_id)
        supplier = Party.objects.get(pk=supplier_id, company=company, party_type=PartyType.SUPPLIER.value, is_active=True)
        payment_account = Account.objects.get(pk=payment_account_id, company=company, account_type=AccountType.ASSET.value, is_active=True, allow_direct_posting=True)
    except Company.DoesNotExist: raise PaymentProcessingError(_("Invalid company ID provided for the payment."))
    except Party.DoesNotExist: raise PaymentProcessingError(_("Active supplier not found or is invalid for the specified company."))
    except Account.DoesNotExist: raise PaymentProcessingError(_("Payment account (ID: %(acc_id)s) is invalid. It must be an active, direct-posting Asset account (e.g., Bank/Cash).") % {'acc_id': payment_account_id})
    if not isinstance(payment_amount, Decimal):
        try: payment_amount = Decimal(str(payment_amount))
        except Exception: raise PaymentProcessingError(_("Payment amount must be a valid number."))
    if payment_amount <= ZERO_DECIMAL: raise PaymentProcessingError(_("Payment amount (%(amount)s) must be positive.") % {'amount': payment_amount})
    if not currency: raise PaymentProcessingError(_("Payment currency is required."))
    final_payment_number = payment_number_override
    if not final_payment_number and status != VendorPayment.PaymentStatus.DRAFT.value:
        final_payment_number = get_next_payment_number(company, payment_date)
    vendor_payment = VendorPayment(
        company=company, supplier=supplier, payment_number=final_payment_number, payment_date=payment_date,
        payment_method=payment_method or VendorPayment.PaymentMethod.OTHER.value, payment_account=payment_account,
        currency=currency, payment_amount=payment_amount, status=status, reference_details=reference_details,
        notes=notes, created_by=created_by_user, updated_by=created_by_user
    )
    try:
        exclude_from_clean = ['allocated_amount', 'unallocated_amount', 'related_gl_voucher']
        if not final_payment_number and status == VendorPayment.PaymentStatus.DRAFT.value: exclude_from_clean.append('payment_number')
        vendor_payment.full_clean(exclude=exclude_from_clean)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation error for payment header: {e.message_dict}", exc_info=True)
        raise PaymentProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    vendor_payment.save()
    logger.info(f"{log_prefix} Vendor Payment {vendor_payment.payment_number or vendor_payment.pk} created successfully. Status: '{vendor_payment.get_status_display()}'.")
    return vendor_payment

@transaction.atomic
def approve_vendor_payment(payment_id: Any, company_id: Any, user: User, approval_notes: Optional[str] = None) -> VendorPayment:
    log_prefix = f"[ApproveVPay][Co:{company_id}][User:{user.name}][Pmt:{payment_id}]"
    logger.info(f"{log_prefix} Attempting to approve vendor payment.")
    try:
        payment = VendorPayment.objects.select_related('company').get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        logger.error(f"{log_prefix} Vendor payment not found.")
        raise PaymentProcessingError(_("Vendor payment not found."))
    if payment.status != VendorPayment.PaymentStatus.DRAFT.value:
        logger.warning(f"{log_prefix} Payment {payment.payment_number or payment.pk} is not in DRAFT status. Current status: {payment.get_status_display()}.")
        raise PaymentProcessingError(_("Only DRAFT payments can be approved. Current status is '%(status)s'.") % {'status': payment.get_status_display()})
    fields_to_update = ['status', 'updated_by', 'updated_at']
    if not payment.payment_number or not payment.payment_number.strip():
        if not payment.company:
            logger.error(f"{log_prefix} Payment {payment.pk} is missing company information for payment number generation.")
            raise PaymentProcessingError(_("Payment is missing company information; cannot generate payment number."))
        try:
            payment.payment_number = get_next_payment_number(payment.company, payment.payment_date)
            fields_to_update.append('payment_number')
            logger.info(f"{log_prefix} Generated payment number {payment.payment_number} for payment {payment.pk}.")
        except SequenceGenerationError as e:
            logger.error(f"{log_prefix} Failed to generate payment number for payment {payment.pk}: {e}", exc_info=True)
            raise PaymentProcessingError(str(e))
    payment.status = VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value
    payment.updated_by = user
    if approval_notes and approval_notes.strip():
        payment.notes = (payment.notes.strip() + "\n" if payment.notes and payment.notes.strip() else "") + f"Approval Notes: {approval_notes.strip()}"
        fields_to_update.append('notes')
    try:
        exclude_fields_from_clean = ['allocated_amount', 'unallocated_amount', 'related_gl_voucher']
        payment.full_clean(exclude=exclude_fields_from_clean)
    except DjangoValidationError as e:
        logger.error(f"{log_prefix} Validation failed for payment {payment.payment_number or payment.pk} before approval: {e.message_dict if hasattr(e, 'message_dict') else e}", exc_info=True)
        raise PaymentProcessingError(e.message_dict if hasattr(e, 'message_dict') else str(e))
    payment.save(update_fields=fields_to_update)
    logger.info(f"{log_prefix} Vendor Payment {payment.payment_number or payment.pk} successfully approved for payment.")
    return payment

@transaction.atomic
def post_vendor_bill_to_gl(
        bill_id: Any,
        company_id: Any,
        posting_user: User,
        posting_date: Optional[date] = None
) -> VendorBill:
    log_prefix = f"[PostBillGL][Co:{company_id}][User:{posting_user.name}][Bill:{bill_id}]"
    try:
        bill = VendorBill.objects.select_related('company', 'supplier', 'related_gl_voucher').prefetch_related('lines__expense_account').get(pk=bill_id, company_id=company_id)
    except VendorBill.DoesNotExist:
        raise BillProcessingError(_("Vendor bill not found for GL posting."))

    if bill.status != VendorBill.BillStatus.APPROVED.value:
        raise BillProcessingError(_("Only APPROVED bills can be posted to GL. Current status: %(s)s") % {'s': bill.get_status_display()})

    if bill.related_gl_voucher_id and bill.related_gl_voucher and bill.related_gl_voucher.status == TransactionStatus.POSTED.value:
        logger.info(f"{log_prefix} Bill {bill.bill_number} already posted to GL (Voucher: {bill.related_gl_voucher.voucher_number}). Skipping.")
        return bill

    bill._recalculate_derived_fields(perform_save=True)
    bill.refresh_from_db()
    final_posting_date = posting_date or bill.issue_date

    # ... (CompanyAccountingSettings and account validation checks remain the same) ...
    if not CompanyAccountingSettings:
        raise GLPostingError(_("System Error: CompanyAccountingSettings model is not available. GL posting cannot proceed."))
    try:
        acc_settings = bill.company.accounting_settings
        if not acc_settings: raise Company.accounting_settings.RelatedObjectDoesNotExist()
        ap_control_account = acc_settings.default_accounts_payable_control
        input_tax_account = acc_settings.default_purchase_tax_asset_account
        if not ap_control_account:
            raise GLPostingError(_("Default Accounts Payable (AP) Control Account is not set for company '%(company_name)s'.") % {'company_name': bill.company.name})
    except Company.accounting_settings.RelatedObjectDoesNotExist:
        raise GLPostingError(_("Company Accounting Settings are missing for company '%(company_name)s'. Please configure them.") % {'company_name': bill.company.name})
    except AttributeError as e:
        logger.error(f"{log_prefix} AttributeError accessing accounting settings: {e}", exc_info=True)
        raise GLPostingError(_("Required default accounts (e.g., AP Control) are missing or misconfigured in Company Settings for '%(company_name)s'. Error: %(err)s") % {'company_name': bill.company.name, 'err': str(e)})

    if ap_control_account.company_id != bill.company_id:
        raise GLPostingError(_("AP Control Account's company does not match the bill's company."))
    if ap_control_account.account_type != AccountType.LIABILITY.value:
        raise GLPostingError(_("The configured AP Control Account ('%(acc_name)s') is not a Liability type.") % {'acc_name': ap_control_account.account_name})

    if bill.tax_amount > ZERO_DECIMAL:
        if not input_tax_account:
            raise GLPostingError(_("Bill has a tax amount (%(tax_amt)s), but no Default Purchase Tax Asset Account is configured for company '%(company_name)s'.") % {'tax_amt': bill.tax_amount, 'company_name': bill.company.name})
        if input_tax_account.company_id != bill.company_id:
            raise GLPostingError(_("Purchase Tax Asset Account's company does not match the bill's company."))
        if input_tax_account.account_type not in [AccountType.ASSET.value, AccountType.EXPENSE.value]:
            raise GLPostingError(_("The configured Purchase Tax Asset Account ('%(acc_name)s') must be an Asset or Expense type.") % {'acc_name': input_tax_account.account_name})
    elif bill.tax_amount < ZERO_DECIMAL:
        raise GLPostingError(_("Bill has a negative tax amount (%(tax_amt)s), which is not supported for standard GL posting.") % {'tax_amt': bill.tax_amount})
    if bill.total_amount <= ZERO_DECIMAL:
        raise GLPostingError(_("Bill total amount (%(total)s) must be positive for GL posting. Review bill lines.") % {'total': bill.total_amount})


    # Pre-check for accounting period
    try:
        AccountingPeriod.objects.get(
            company=bill.company,
            start_date__lte=final_posting_date,
            end_date__gte=final_posting_date,
            locked=False
        )
    except AccountingPeriod.DoesNotExist:
        raise GLPostingError(_("No open accounting period found for posting date: %(date)s for company '%(company_name)s'.") % {'date': final_posting_date, 'company_name': bill.company.name})
    except AccountingPeriod.MultipleObjectsReturned:
        raise GLPostingError(_("Multiple open accounting periods found for posting date: %(date)s for company '%(company_name)s'. Please check period configuration.") % {'date': final_posting_date, 'company_name': bill.company.name})

    voucher_lines_data = []
    # ... (voucher_lines_data population remains the same) ...
    for line in bill.lines.all():
        if line.amount is None or line.amount < ZERO_DECIMAL:
            raise GLPostingError(_("Bill line for account '%(acc)s' has an invalid or zero/negative amount (%(amt)s). Amounts must be positive.") % {'acc': line.expense_account.account_name, 'amt': line.amount})
        if line.amount > ZERO_DECIMAL:
            voucher_lines_data.append({
                'account_id': line.expense_account_id,
                'dr_cr': DrCrType.DEBIT.value,
                'amount': line.amount,
                'narration': f"Bill {bill.bill_number}: {line.description or line.expense_account.account_name}"
            })
    if bill.tax_amount > ZERO_DECIMAL and input_tax_account:
        voucher_lines_data.append({
            'account_id': input_tax_account.pk,
            'dr_cr': DrCrType.DEBIT.value,
            'amount': bill.tax_amount,
            'narration': f"Input Tax for Bill {bill.bill_number}"
        })
    voucher_lines_data.append({
        'account_id': ap_control_account.pk,
        'dr_cr': DrCrType.CREDIT.value,
        'amount': bill.total_amount,
        'narration': f"A/P for Bill {bill.bill_number} from {bill.supplier.name}"
    })

    if not voucher_lines_data or len(voucher_lines_data) < 2:
        raise GLPostingError(_("Cannot create GL voucher: Not enough valid lines derived from the bill."))

    try:
        # Step 1: Create a Draft Voucher
        draft_voucher = voucher_service.create_draft_voucher(
            company_id=bill.company_id,
            created_by_user=posting_user,
            voucher_type_value=JournalVoucherType.PURCHASE.value,
            date=final_posting_date,
            narration=f"Vendor Bill {bill.bill_number} - {bill.supplier.name}",
            lines_data=voucher_lines_data,
            party_pk=bill.supplier_id,
            reference=bill.supplier_bill_reference or bill.bill_number
        )
        logger.info(f"{log_prefix} Draft voucher {draft_voucher.pk} created for bill {bill.bill_number}.")

        # Step 2: Submit the Draft Voucher for Approval
        submitted_voucher = voucher_service.submit_voucher_for_approval(
            company_id=bill.company_id,
            voucher_id=draft_voucher.pk,
            submitted_by_user=posting_user # The user posting the bill is also submitting the GL voucher
        )
        logger.info(f"{log_prefix} Voucher {submitted_voucher.voucher_number or submitted_voucher.pk} submitted for approval.")

        # Step 3: Approve and Post the Submitted Voucher
        gl_voucher = voucher_service.approve_and_post_voucher(
            company_id=bill.company_id,
            voucher_id=submitted_voucher.pk, # Use the ID of the (now submitted) voucher
            approver_user=posting_user,
            comments=f"Auto-approved and posted from Vendor Bill {bill.bill_number}."
        )

    except Exception as e_voucher:
        logger.error(f"{log_prefix} Error creating/posting GL voucher for bill {bill.bill_number}: {e_voucher}", exc_info=True)
        if isinstance(e_voucher, DjangoValidationError):
            error_detail = e_voucher.message_dict if hasattr(e_voucher, 'message_dict') else str(e_voucher)
            raise GLPostingError(_("Failed to create or post GL voucher for bill %(bill_num)s: %(error)s") % {
                'bill_num': bill.bill_number, 'error': error_detail
            })
        raise GLPostingError(_("Failed to create or post GL voucher for bill %(bill_num)s: %(error)s") % {
            'bill_num': bill.bill_number, 'error': str(e_voucher)
        })

    bill.related_gl_voucher = gl_voucher
    bill.updated_by = posting_user
    bill.save(update_fields=['related_gl_voucher', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Bill {bill.bill_number} posted successfully to GL. Voucher: {gl_voucher.voucher_number}.")
    return bill
@transaction.atomic
def allocate_payment_to_bills(
        payment_id: Any, company_id: Any, allocation_user: User,
        allocations_data: List[Dict[str, Any]], allocation_date: Optional[date] = None
) -> VendorPayment:
    log_prefix = f"[AllocateVPay][Co:{company_id}][User:{allocation_user.name}][Pmt:{payment_id}]"
    try:
        payment = VendorPayment.objects.select_for_update().get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        raise AllocationError(_("Vendor payment (ID: %(id)s) not found.") % {'id': payment_id})
    if payment.status not in [VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value, VendorPayment.PaymentStatus.PAID_COMPLETED.value]:
        raise AllocationError(_("Only 'Approved for Payment' or 'Paid/Completed' payments can be allocated. Current status of payment %(num)s: %(s)s") % {'num': payment.payment_number or payment.pk, 's': payment.get_status_display()})
    final_allocation_date = allocation_date or payment.payment_date
    payment._recalculate_derived_fields(perform_save=True)
    payment.refresh_from_db(fields=['unallocated_amount'])
    total_new_allocation_this_transaction = sum(Decimal(str(data.get('amount_allocated', '0'))) for data in allocations_data if Decimal(str(data.get('amount_allocated', '0'))) > ZERO_DECIMAL)
    tolerance = Decimal('0.005')
    if total_new_allocation_this_transaction > payment.unallocated_amount + tolerance:
        raise AllocationError(_("Total new allocation amount (%(new_alloc)s) exceeds payment's currently unallocated amount (%(unalloc)s). Payment: %(num)s") % {'new_alloc': total_new_allocation_this_transaction.quantize(Decimal('0.01')), 'unalloc': payment.unallocated_amount.quantize(Decimal('0.01')), 'num': payment.payment_number or payment.pk})
    for alloc_data in allocations_data:
        bill_id = alloc_data.get('bill_id')
        try: amount_to_allocate_for_bill = Decimal(str(alloc_data.get('amount_allocated', '0')))
        except Exception: raise AllocationError(_("Invalid allocation amount ('%(amt_str)s') provided for bill ID %(id)s.") % {'amt_str': alloc_data.get('amount_allocated'), 'id': bill_id})
        if amount_to_allocate_for_bill <= ZERO_DECIMAL: continue
        try:
            vendor_bill = VendorBill.objects.select_for_update().get(pk=bill_id, company=payment.company, supplier=payment.supplier)
        except VendorBill.DoesNotExist:
            raise AllocationError(_("Vendor Bill (ID: %(id)s) for allocation not found, or it does not match the payment's supplier/company.") % {'id': bill_id})
        if vendor_bill.currency != payment.currency:
            raise AllocationError(_("Currency mismatch: Payment %(p_num)s (%(p_curr)s) vs Bill %(b_num)s (%(b_curr)s).") % {'p_num': payment.payment_number or payment.pk, 'p_curr': payment.currency, 'b_num': vendor_bill.bill_number or vendor_bill.pk, 'b_curr': vendor_bill.currency})
        if vendor_bill.status not in [VendorBill.BillStatus.APPROVED.value, VendorBill.BillStatus.PARTIALLY_PAID.value]:
            raise AllocationError(_("Cannot allocate to Bill %(b_num)s as it is not Approved or Partially Paid. Current status: %(status)s.") % {'b_num': vendor_bill.bill_number or vendor_bill.pk, 'status': vendor_bill.get_status_display()})
        vendor_bill._recalculate_derived_fields(perform_save=True)
        vendor_bill.refresh_from_db(fields=['amount_due'])
        if amount_to_allocate_for_bill > vendor_bill.amount_due + tolerance:
            raise AllocationError(_("Allocation amount (%(alloc_amt)s) for Bill %(b_num)s exceeds its current amount due (%(due_amt)s).") % {'alloc_amt': amount_to_allocate_for_bill.quantize(Decimal('0.01')), 'b_num': vendor_bill.bill_number or vendor_bill.pk, 'due_amt': vendor_bill.amount_due.quantize(Decimal('0.01'))})
        allocation_obj, created = VendorPaymentAllocation.objects.get_or_create(
            vendor_payment=payment, vendor_bill=vendor_bill, company=payment.company,
            defaults={'allocated_amount': amount_to_allocate_for_bill, 'allocation_date': final_allocation_date, 'created_by': allocation_user, 'updated_by': allocation_user}
        )
        if not created:
            allocation_obj.allocated_amount = amount_to_allocate_for_bill
            allocation_obj.allocation_date = final_allocation_date
            allocation_obj.updated_by = allocation_user
            allocation_obj.save(update_fields=['allocated_amount', 'allocation_date', 'updated_by', 'updated_at'])
        logger.info(f"{log_prefix} {'Created' if created else 'Updated'} allocation of {amount_to_allocate_for_bill.quantize(Decimal('0.01'))} from Payment {payment.payment_number or payment.pk} to Bill {vendor_bill.bill_number or vendor_bill.pk}.")
        vendor_bill._recalculate_derived_fields(perform_save=True)
    payment._recalculate_derived_fields(perform_save=True)
    logger.info(f"{log_prefix} Payment {payment.payment_number or payment.pk} allocations processed. New Unallocated Amount: {payment.unallocated_amount.quantize(Decimal('0.01'))}")
    return payment

@transaction.atomic
def void_vendor_payment(payment_id: Any, company_id: Any, voiding_user: User, void_reason: str, void_date: Optional[date] = None) -> VendorPayment:
    log_prefix = f"[VoidVPay][Co:{company_id}][User:{voiding_user.name}][Pmt:{payment_id}]"
    effective_void_date = void_date or timezone.now().date()
    if not void_reason or not void_reason.strip():
        raise PaymentProcessingError(_("A reason is mandatory to void a payment."))
    try:
        payment = VendorPayment.objects.select_for_update().select_related('company', 'related_gl_voucher').get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        raise PaymentProcessingError(_("Vendor payment (ID: %(id)s) not found.") % {'id': payment_id})
    if payment.status == VendorPayment.PaymentStatus.VOID.value:
        logger.info(f"{log_prefix} Payment {payment.payment_number or payment.pk} is already VOID. No action taken.")
        return payment
    payment._recalculate_derived_fields(perform_save=True)
    payment.refresh_from_db(fields=['allocated_amount'])
    if payment.allocated_amount > ZERO_DECIMAL:
        raise PaymentProcessingError(_("Cannot VOID payment '%(num)s' because it has active allocations (Allocated Amount: %(alloc)s). Please unallocate from bills first.") % {'num': payment.payment_number or payment.pk, 'alloc': payment.allocated_amount.quantize(Decimal('0.01'))})
    original_status_log = payment.get_status_display()
    gl_reversal_voucher_number: Optional[str] = None
    if payment.related_gl_voucher_id and payment.related_gl_voucher:
        original_gl_voucher = payment.related_gl_voucher
        if original_gl_voucher.status == TransactionStatus.POSTED.value:
            try:
                # Assuming create_reversing_voucher also derives period from reversal_date and company_id
                reversing_gl_voucher = voucher_service.create_reversing_voucher(
                    company_id=company_id,
                    original_voucher_id=original_gl_voucher.pk,
                    user=voiding_user,
                    reversal_date=effective_void_date,
                    reversal_voucher_type_value=getattr(JournalVoucherType, 'PAYMENT_REVERSAL', JournalVoucherType.GENERAL).value,
                    post_immediately=True
                )
                gl_reversal_voucher_number = reversing_gl_voucher.voucher_number
                logger.info(f"{log_prefix} Created GL reversal voucher {gl_reversal_voucher_number} for original payment voucher {original_gl_voucher.voucher_number}.")
            except Exception as e_reversal:
                logger.error(f"{log_prefix} Error creating GL reversal for payment (Pmt: {payment.payment_number or payment.pk}): {e_reversal}", exc_info=True)
                raise PaymentProcessingError(_("Failed to create GL reversal for the payment (%(pmt_num)s): %(error)s. Payment not voided.") % {'pmt_num': payment.payment_number or payment.pk, 'error': str(e_reversal)})
        else:
            logger.warning(f"{log_prefix} Original GL voucher {original_gl_voucher.voucher_number} (ID: {original_gl_voucher.pk}) for payment {payment.payment_number or payment.pk} was not POSTED (Status: {original_gl_voucher.get_status_display()}). No GL reversal needed or performed.")
    elif payment.related_gl_voucher_id and not payment.related_gl_voucher:
        logger.error(f"{log_prefix} CRITICAL: Payment {payment.payment_number or payment.pk} has related_gl_voucher_id {payment.related_gl_voucher_id}, but the voucher object could not be loaded. Manual GL review may be needed.")
    payment.status = VendorPayment.PaymentStatus.VOID.value
    void_note_user_name = voiding_user.get_full_name() or voiding_user.name
    void_note = f"VOIDED on {effective_void_date.strftime('%Y-%m-%d')} by {void_note_user_name}. Reason: {void_reason.strip()}. Original Status: {original_status_log}. "
    if gl_reversal_voucher_number: void_note += f"GL Reversal Voucher: {gl_reversal_voucher_number}."
    else: void_note += "No GL reversal performed (either not posted or original voucher was not POSTED)."
    payment.notes = (payment.notes.strip() + "\n" if payment.notes and payment.notes.strip() else "") + void_note
    payment.updated_by = voiding_user
    payment.allocated_amount = ZERO_DECIMAL
    payment.unallocated_amount = ZERO_DECIMAL
    payment.save(update_fields=['status', 'notes', 'allocated_amount', 'unallocated_amount', 'updated_by', 'updated_at'])
    logger.info(f"{log_prefix} Payment {payment.payment_number or payment.pk} successfully VOIDED.")
    return payment


# Add this function to your crp_accounting/services/payables_service.py file

@transaction.atomic
def post_vendor_payment_to_gl(
        payment_id: Any,
        company_id: Any,
        posting_user: User,
        posting_date: Optional[date] = None
) -> VendorPayment:
    """
    Posts a vendor payment to the General Ledger.

    This involves creating a GL voucher that:
    - Debits the Accounts Payable (AP) control account.
    - Credits the payment account (e.g., Bank/Cash).

    Args:
        payment_id: The ID of the VendorPayment to post.
        company_id: The ID of the company owning the payment.
        posting_user: The user performing the posting action.
        posting_date: Optional date for the GL posting. Defaults to payment_date.

    Returns:
        The updated VendorPayment instance with a link to the GL voucher.

    Raises:
        PaymentProcessingError: If the payment is not in a valid state or other payment-specific issues.
        GLPostingError: If there are issues related to GL accounts, periods, or voucher creation.
    """
    log_prefix = f"[PostVPayGL][Co:{company_id}][User:{posting_user.name}][Pmt:{payment_id}]"
    logger.info(f"{log_prefix} Attempting to post vendor payment to GL.")

    try:
        payment = VendorPayment.objects.select_related(
            'company', 'supplier', 'payment_account', 'related_gl_voucher'
        ).get(pk=payment_id, company_id=company_id)
    except VendorPayment.DoesNotExist:
        logger.error(f"{log_prefix} Vendor payment not found.")
        raise PaymentProcessingError(_("Vendor payment (ID: %(id)s) not found for GL posting.") % {'id': payment_id})

    if payment.status not in [
        VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value,
        VendorPayment.PaymentStatus.PAID_COMPLETED.value  # Allow re-posting attempt if GL was unlinked/failed
    ]:
        logger.warning(
            f"{log_prefix} Payment {payment.payment_number or payment.pk} is not in an eligible status "
            f"for GL posting. Current status: {payment.get_status_display()}."
        )
        raise PaymentProcessingError(
            _("Only 'Approved for Payment' or 'Paid/Completed' payments can be posted to GL. Current status: %(s)s") %
            {'s': payment.get_status_display()}
        )

    if payment.related_gl_voucher_id and payment.related_gl_voucher and \
            payment.related_gl_voucher.status == TransactionStatus.POSTED.value:
        logger.info(
            f"{log_prefix} Payment {payment.payment_number or payment.pk} already posted to GL "
            f"(Voucher: {payment.related_gl_voucher.voucher_number}). Skipping."
        )
        return payment

    # Refresh derived fields in memory; payment_amount is the key for GL.
    payment._recalculate_derived_fields(perform_save=False)

    final_posting_date = posting_date or payment.payment_date

    if not CompanyAccountingSettings:
        logger.critical(f"{log_prefix} System Error: CompanyAccountingSettings model is not available.")
        raise GLPostingError(
            _("System Error: CompanyAccountingSettings model is not available. GL posting cannot proceed."))

    try:
        acc_settings = payment.company.accounting_settings
        if not acc_settings:
            # This case should ideally be caught by Company model validation or signals
            # ensuring accounting_settings are always present for an active company.
            raise Company.accounting_settings.RelatedObjectDoesNotExist()  # type: ignore

        ap_control_account = acc_settings.default_accounts_payable_control
        if not ap_control_account:
            raise GLPostingError(
                _("Default Accounts Payable (AP) Control Account is not set for company '%(company_name)s'.") %
                {'company_name': payment.company.name}
            )

    except Company.accounting_settings.RelatedObjectDoesNotExist:  # type: ignore
        logger.error(f"{log_prefix} Company Accounting Settings are missing for company {payment.company.name}.")
        raise GLPostingError(
            _("Company Accounting Settings are missing for company '%(company_name)s'. Please configure them.") %
            {'company_name': payment.company.name}
        )
    except AttributeError as e:  # Catches if default_accounts_payable_control itself is missing from settings model
        logger.error(f"{log_prefix} AttributeError accessing accounting settings for {payment.company.name}: {e}",
                     exc_info=True)
        raise GLPostingError(
            _("Required default accounts (e.g., AP Control) are missing or misconfigured in Company Settings for '%(company_name)s'. Error: %(err)s") %
            {'company_name': payment.company.name, 'err': str(e)}
        )

    # Validate AP Control Account
    if ap_control_account.company_id != payment.company_id:
        raise GLPostingError(
            _("AP Control Account's company ('%(acc_co)s') does not match the payment's company ('%(pmt_co)s').") %
            {'acc_co': ap_control_account.company.name, 'pmt_co': payment.company.name}
        )
    if ap_control_account.account_type != AccountType.LIABILITY.value:
        raise GLPostingError(
            _("The configured AP Control Account ('%(acc_name)s') is not a Liability type. It's '%(type)s'.") %
            {'acc_name': ap_control_account.account_name, 'type': ap_control_account.get_account_type_display()}
        )
    if not ap_control_account.allow_direct_posting:
        raise GLPostingError(
            _("The configured AP Control Account ('%(acc_name)s') does not allow direct posting.") %
            {'acc_name': ap_control_account.account_name}
        )

    # Validate Payment Account (from the payment record itself)
    payment_bank_cash_account = payment.payment_account
    if not payment_bank_cash_account:
        # This should ideally be caught at payment creation.
        logger.error(f"{log_prefix} Payment {payment.payment_number or payment.pk} is missing a payment_account.")
        raise GLPostingError(
            _("Payment ('%(pmt_num)s') does not have a Payment Account assigned.") %
            {'pmt_num': payment.payment_number or payment.pk}
        )
    if payment_bank_cash_account.company_id != payment.company_id:
        raise GLPostingError(
            _("Payment Account ('%(acc_name)s') on the payment record has a different company ('%(acc_co)s') than the payment's company ('%(pmt_co)s').") %
            {'acc_name': payment_bank_cash_account.account_name, 'acc_co': payment_bank_cash_account.company.name,
             'pmt_co': payment.company.name}
        )
    if payment_bank_cash_account.account_type != AccountType.ASSET.value:  # Typically Asset (Bank, Cash)
        raise GLPostingError(
            _("The Payment Account ('%(acc_name)s') on the payment record is not an Asset type. It's '%(type)s'.") %
            {'acc_name': payment_bank_cash_account.account_name,
             'type': payment_bank_cash_account.get_account_type_display()}
        )
    if not payment_bank_cash_account.allow_direct_posting:
        raise GLPostingError(
            _("The Payment Account ('%(acc_name)s') on the payment record does not allow direct posting.") %
            {'acc_name': payment_bank_cash_account.account_name}
        )

    if payment.payment_amount <= ZERO_DECIMAL:
        logger.warning(
            f"{log_prefix} Payment amount {payment.payment_amount} for payment {payment.payment_number or payment.pk} is not positive.")
        raise GLPostingError(
            _("Payment amount (%(amount)s) for payment '%(num)s' must be positive for GL posting.") %
            {'amount': payment.payment_amount, 'num': payment.payment_number or payment.pk}
        )

    # Pre-check for accounting period
    try:
        AccountingPeriod.objects.get(
            company=payment.company,
            start_date__lte=final_posting_date,
            end_date__gte=final_posting_date,
            locked=False
        )
    except AccountingPeriod.DoesNotExist:
        logger.error(
            f"{log_prefix} No open accounting period found for posting date {final_posting_date} for company {payment.company.name}.")
        raise GLPostingError(
            _("No open accounting period found for posting date: %(date)s for company '%(company_name)s'.") %
            {'date': final_posting_date, 'company_name': payment.company.name}
        )
    except AccountingPeriod.MultipleObjectsReturned:
        logger.error(
            f"{log_prefix} Multiple open accounting periods found for posting date {final_posting_date} for company {payment.company.name}.")
        raise GLPostingError(
            _("Multiple open accounting periods found for posting date: %(date)s for company '%(company_name)s'. Please check period configuration.") %
            {'date': final_posting_date, 'company_name': payment.company.name}
        )

    # GL Entries:
    # Debit: Accounts Payable (Liability) - Reducing what is owed to supplier
    # Credit: Bank/Cash Account (Asset) - Money going out
    voucher_lines_data = [
        {
            'account_id': ap_control_account.pk,
            'dr_cr': DrCrType.DEBIT.value,
            'amount': payment.payment_amount,
            'narration': f"Payment to {payment.supplier.name} - Ref: {payment.payment_number or payment.pk}"
        },
        {
            'account_id': payment_bank_cash_account.pk,
            'dr_cr': DrCrType.CREDIT.value,
            'amount': payment.payment_amount,
            'narration': f"Payment made to {payment.supplier.name} - Ref: {payment.payment_number or payment.pk}"
        }
    ]

    # Determine VoucherType for Vendor Payment
    # Using VENDOR_PAYMENT or similar from JournalVoucherType. Fallback if not defined.
    try:
        # Ensure JournalVoucherType has an appropriate type like VENDOR_PAYMENT or CASH_DISBURSEMENT
        payment_voucher_type_value = getattr(JournalVoucherType, 'VENDOR_PAYMENT',
                                             JournalVoucherType.CASH_DISBURSEMENT).value
        if payment_voucher_type_value == JournalVoucherType.CASH_DISBURSEMENT.value and \
                not hasattr(JournalVoucherType, 'VENDOR_PAYMENT'):  # Log if primary choice isn't there
            logger.info(f"{log_prefix} Using JournalVoucherType.CASH_DISBURSEMENT as VENDOR_PAYMENT is not defined.")
    except AttributeError:  # Fallback if neither common payment types are defined
        logger.warning(
            f"{log_prefix} Neither JournalVoucherType.VENDOR_PAYMENT nor CASH_DISBURSEMENT found. "
            f"Falling back to GENERAL_JOURNAL for payment {payment.payment_number or payment.pk}."
        )
        payment_voucher_type_value = JournalVoucherType.GENERAL.value

    try:
        # Step 1: Create a Draft Voucher
        draft_voucher = voucher_service.create_draft_voucher(
            company_id=payment.company_id,
            created_by_user=posting_user,
            voucher_type_value=payment_voucher_type_value,
            date=final_posting_date,
            narration=f"Vendor Payment {payment.payment_number or payment.pk} to {payment.supplier.name}",
            lines_data=voucher_lines_data,
            party_pk=payment.supplier_id,
            reference=payment.reference_details or payment.payment_number
        )
        logger.info(
            f"{log_prefix} Draft voucher PK {draft_voucher.pk} created for payment {payment.payment_number or payment.pk}.")

        # Step 2: Submit the Draft Voucher for Approval
        submitted_voucher = voucher_service.submit_voucher_for_approval(
            company_id=payment.company_id,
            voucher_id=draft_voucher.pk,
            submitted_by_user=posting_user
        )
        logger.info(
            f"{log_prefix} Voucher {submitted_voucher.voucher_number or submitted_voucher.pk} submitted for approval.")

        # Step 3: Approve and Post the Submitted Voucher
        gl_voucher = voucher_service.approve_and_post_voucher(
            company_id=payment.company_id,
            voucher_id=submitted_voucher.pk,
            approver_user=posting_user,  # User initiating posting also approves the GL entry
            comments=f"Auto-approved and posted from Vendor Payment {payment.payment_number or payment.pk}."
        )
        logger.info(
            f"{log_prefix} GL Voucher {gl_voucher.voucher_number} (PK: {gl_voucher.pk}) "
            f"successfully posted for payment {payment.payment_number or payment.pk}."
        )

    except Exception as e_voucher:
        logger.error(
            f"{log_prefix} Error creating/posting GL voucher for payment {payment.payment_number or payment.pk}: {e_voucher}",
            exc_info=True
        )
        if isinstance(e_voucher, DjangoValidationError):
            error_detail = e_voucher.message_dict if hasattr(e_voucher, 'message_dict') else str(e_voucher)
        else:
            error_detail = str(e_voucher)
        raise GLPostingError(
            _("Failed to create or post GL voucher for payment %(pmt_num)s: %(error)s") %
            {'pmt_num': payment.payment_number or payment.pk, 'error': error_detail}
        )

    payment.related_gl_voucher = gl_voucher
    payment.updated_by = posting_user

    fields_to_update = ['related_gl_voucher', 'updated_by', 'updated_at']
    if payment.status == VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value:
        payment.status = VendorPayment.PaymentStatus.PAID_COMPLETED.value
        fields_to_update.append('status')
        logger.info(f"{log_prefix} Payment {payment.payment_number or payment.pk} status updated to PAID_COMPLETED.")

    payment.save(update_fields=list(set(fields_to_update)))  # Use set to avoid duplicate fields if any
    logger.info(
        f"{log_prefix} Payment {payment.payment_number or payment.pk} posted successfully to GL. "
        f"Voucher: {gl_voucher.voucher_number}."
    )
    return payment