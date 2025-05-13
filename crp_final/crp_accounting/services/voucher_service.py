# crp_accounting/services/voucher_service.py

import logging
from decimal import Decimal
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.conf import settings
from typing import Optional, Dict, Any, List, Set, Union

from rest_framework.exceptions import PermissionDenied  # Using DRF's PermissionDenied for RBAC checks
from django.shortcuts import get_object_or_404  # Good for fetching Company

logger = logging.getLogger("crp_accounting.services.voucher")  # Specific logger

# --- Model Imports ---
from ..models.journal import (
    Voucher, VoucherLine, VoucherApproval,
    VoucherType, TransactionStatus, DrCrType, ApprovalActionType
)
from ..models.coa import Account
from ..models.party import Party
from ..models.period import AccountingPeriod

# --- Company Model & Membership ---
try:
    from company.models import Company, CompanyMembership
except ImportError:
    # This is a critical failure at startup.
    logger.critical(
        "Voucher Service: CRITICAL - Failed to import Company/CompanyMembership models. Multi-tenancy & RBAC WILL NOT WORK.")
    Company = None  # type: ignore
    CompanyMembership = None  # type: ignore

# --- Service Imports ---
from . import sequence_service  # Assumed fully tenant-aware and expects company_id, voucher_type_value, period_id

# --- Task Imports ---
from ..tasks import update_account_balances_task  # Assumed task is tenant-aware and expects voucher_id, company_id

# --- Custom Exception Imports ---
from ..exceptions import (
    VoucherWorkflowError, InvalidVoucherStatusError, PeriodLockedError,
    BalanceError
)

# =============================================================================
# RBAC Permission Checking Helper (Tenant Aware)
# =============================================================================
# Assuming PERMISSIONS_MAP is correctly defined using CompanyMembership.Role.OWNER.value, etc.
# (Your PERMISSIONS_MAP from the snippet looks fine if CompanyMembership.Role.VALUE.value is used,
# or just CompanyMembership.Role.VALUE if your _check_role_permission compares enum members directly)
# For this review, I'll assume CompanyMembership.Role.OWNER.value is correct for the map keys.

PERMISSIONS_MAP: Dict[str, Set[str]] = {
    'add_voucher': {
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value, CompanyMembership.Role.ACCOUNTANT.value,
        CompanyMembership.Role.DATA_ENTRY.value, CompanyMembership.Role.SALES_REP.value,  # Example custom roles
        CompanyMembership.Role.PURCHASE_OFFICER.value,  # Example custom roles
    },
    'change_voucher_draft': {  # Who can edit a DRAFT voucher
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value, CompanyMembership.Role.ACCOUNTANT.value,
        CompanyMembership.Role.DATA_ENTRY.value,  # Or based on created_by
    },
    'submit_voucher': {  # Who can move from DRAFT to PENDING_APPROVAL
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value, CompanyMembership.Role.ACCOUNTANT.value,
        CompanyMembership.Role.DATA_ENTRY.value, CompanyMembership.Role.SALES_REP.value,
        CompanyMembership.Role.PURCHASE_OFFICER.value,
    },
    'approve_voucher': {  # Who can move from PENDING_APPROVAL to (implicitly) ready for POSTED
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value,
    },
    'post_voucher': {  # Who can make the final POST (might be same as approve_voucher)
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value,
    },
    'reject_voucher': {  # Who can move from PENDING_APPROVAL to REJECTED
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value,
    },
    'create_reversal_voucher': {  # Who can initiate a reversal for a POSTED voucher
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value, CompanyMembership.Role.ACCOUNTANT.value,
    },
    'delete_draft_voucher': {  # Who can delete a DRAFT (or maybe REJECTED) voucher
        CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value,
        CompanyMembership.Role.ACCOUNTING_MANAGER.value,  # Or based on created_by
    },
}


def _check_role_permission(user: settings.AUTH_USER_MODEL, company: Company, action_codename: str):
    # This function looks robust and handles missing models/config well.
    # Ensure CompanyMembership.Role.SOME_ROLE.value is used if PERMISSIONS_MAP stores values.
    if not user or not user.is_authenticated:
        raise PermissionDenied(_("Authentication required to perform this action."))
    if not CompanyMembership or not Company:  # Guard against import issues
        logger.critical(
            f"RBAC System Error: Action '{action_codename}' by User PK {user.pk}. Company/Membership models missing.")
        raise PermissionDenied(_("System configuration error: Cannot verify permissions."))

    if action_codename not in PERMISSIONS_MAP:
        logger.error(
            f"RBAC Config Error: Unknown action '{action_codename}' for User PK {user.pk}, Co '{company.name}'.")
        raise PermissionDenied(
            _("Action '%(action)s' is not defined in the permission system.") % {'action': action_codename})

    allowed_roles_values = PERMISSIONS_MAP[action_codename]
    try:
        # Ensure membership is active and company is effectively active
        membership = CompanyMembership.objects.select_related('company').get(
            user=user, company=company, is_active_membership=True
        )
        # Additionally, ensure the company itself is active for operations
        if not membership.company.effective_is_active:  # Assuming Company has effective_is_active
            logger.warning(
                f"RBAC Denied: User PK {user.pk} for action '{action_codename}', Co '{company.name}'. Reason: Company is not effectively active.")
            raise PermissionDenied(_("The company '%(company_name)s' is not currently active or accessible.") % {
                'company_name': company.name})

        user_role_value = membership.role  # This is the value stored in the DB (e.g., 'OWNER', 'ADMIN')
    except CompanyMembership.DoesNotExist:
        logger.warning(
            f"RBAC Denied: User PK {user.pk} for action '{action_codename}', Co '{company.name}'. Reason: No active membership.")
        raise PermissionDenied(
            _("You do not have an active membership or required role in the company '%(company_name)s'.") % {
                'company_name': company.name})
    except Exception as e:  # Catch other potential errors during permission check
        logger.exception(
            f"RBAC Error: Checking permission for User PK {user.pk}, action '{action_codename}', Co '{company.name}'. Error: {e}")
        raise PermissionDenied(_("An error occurred while verifying your permissions."))

    if user_role_value not in allowed_roles_values:
        user_role_display = membership.get_role_display()  # Gets the human-readable label
        action_display = action_codename.replace('_', ' ').capitalize()
        logger.warning(
            f"RBAC Denied: User PK {user.pk} (Role: '{user_role_display}') for action '{action_codename}' "
            f"in Co '{company.name}'. Required one of: {allowed_roles_values}"
        )
        raise PermissionDenied(
            _("Your role ('%(user_role)s') in company '%(company_name)s' does not permit you to perform the action: '%(action_name)s'.") %
            {'user_role': user_role_display, 'company_name': company.name, 'action_name': action_display}
        )
    logger.debug(
        f"RBAC Granted: User PK {user.pk} (Role: {user_role_value}) for action '{action_codename}' in Co '{company.name}'")


# =============================================================================
# Core Workflow Service Functions
# =============================================================================
@transaction.atomic
def create_draft_voucher(
        company_id: int, created_by_user: settings.AUTH_USER_MODEL, voucher_type_value: str,
        date: timezone.datetime.date, narration: str, lines_data: List[Dict[str, Any]],
        party_pk: Optional[Union[int, str]] = None, reference: Optional[str] = None,
        # tags: Optional[List[str]] = None, # Removed tags as it's not in Voucher model
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)  # Fetch Company instance once
    _check_role_permission(created_by_user, company_instance, 'add_voucher')
    current_user_display = created_by_user.get_full_name() or created_by_user.get_username()  # Prefer full name
    log_prefix = f"[VchCreateDraft][Co:{company_instance.name}][User:{current_user_display}]"
    logger.info(f"{log_prefix} Creating DRAFT voucher. Type: {voucher_type_value}, Date: {date}")

    accounting_period = _get_valid_accounting_period(company_instance, date)  # Pass Company instance
    party_instance = _get_valid_party(company_instance,
                                      party_pk) if party_pk is not None else None  # Pass Company instance

    voucher = Voucher(
        company=company_instance,  # Assign Company instance
        voucher_type=voucher_type_value, date=date, effective_date=date,  # Set effective_date = date
        narration=narration, status=TransactionStatus.DRAFT.value, accounting_period=accounting_period,
        party=party_instance, reference=reference,
        created_by=created_by_user, updated_by=created_by_user  # Set audit fields
    )
    # Clean all fields except those auto-generated or set later in workflow
    voucher.full_clean(
        exclude=['voucher_number', 'posted_by', 'posted_at', 'approved_by', 'approved_at', 'balances_updated',
                 'is_reversed', 'is_reversal_for'])
    voucher.save()  # This will trigger TenantScopedModel.save() which sets company if needed (already set here)

    _create_or_update_voucher_lines(voucher, lines_data, company_instance, is_new_voucher=True)
    voucher.refresh_from_db()  # To get any DB defaults or trigger effects if lines were modified
    try:
        validate_voucher_balance(voucher)  # Validate balance after lines are added
    except BalanceError as be:
        logger.warning(f"{log_prefix} DRAFT Voucher {voucher.pk} created with imbalance: {be.message}.")
        # For drafts, imbalance might be temporarily allowed by some businesses.

    logger.info(f"{log_prefix} Successfully created DRAFT Voucher {voucher.pk}.")
    return voucher


@transaction.atomic
def update_draft_voucher(
        company_id: int, voucher_id: Union[int, str], updated_by_user: settings.AUTH_USER_MODEL,
        data_to_update: Dict[str, Any]
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(updated_by_user, company_instance, 'change_voucher_draft')
    current_user_display = updated_by_user.get_full_name() or updated_by_user.get_username()
    log_prefix = f"[VchUpdateDraft][Co:{company_instance.name}][User:{current_user_display}][VchPK:{voucher_id}]"

    # Fetch with select_for_update, ensure it belongs to the company and is DRAFT
    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('accounting_period', 'party', 'company'),
        # company already on voucher
        pk=voucher_id, company=company_instance, status=TransactionStatus.DRAFT.value
    )
    logger.info(f"{log_prefix} Updating DRAFT Voucher.")

    update_fields_list = ['updated_by']  # updated_at is auto by TenantScopedModel or model.save
    voucher.updated_by = updated_by_user  # Always update this

    if 'date' in data_to_update and voucher.date != data_to_update['date']:
        new_date = data_to_update['date']
        voucher.accounting_period = _get_valid_accounting_period(company_instance, new_date)  # Re-validate/get period
        voucher.date = new_date
        voucher.effective_date = new_date  # Keep effective_date same as date for drafts
        update_fields_list.extend(['date', 'effective_date', 'accounting_period'])

    for field_name in ['narration', 'reference', 'voucher_type']:  # voucher_type can change for a draft
        if field_name in data_to_update and getattr(voucher, field_name) != data_to_update[field_name]:
            setattr(voucher, field_name, data_to_update[field_name])
            update_fields_list.append(field_name)

    if 'party_pk' in data_to_update:  # Allow changing/clearing party
        new_party_pk = data_to_update['party_pk']
        new_party_instance = _get_valid_party(company_instance, new_party_pk) if new_party_pk is not None else None
        if voucher.party_id != (new_party_instance.pk if new_party_instance else None):
            voucher.party = new_party_instance
            update_fields_list.append('party')

    if 'lines_data' in data_to_update:
        _create_or_update_voucher_lines(voucher, data_to_update['lines_data'], company_instance, is_new_voucher=False)
        # No specific field to add to update_fields_list here, but line changes necessitate a save

    if len(update_fields_list) > 1 or 'lines_data' in data_to_update:  # If more than just updated_by, or lines changed
        # Clean before saving, excluding fields not being changed or managed by workflow/DB
        exclude_from_clean = ['voucher_number', 'posted_by', 'posted_at', 'approved_by', 'approved_at',
                              'balances_updated', 'company', 'created_by', 'is_reversal_for', 'is_reversed']
        voucher.full_clean(exclude=exclude_from_clean)
        voucher.save(update_fields=list(set(update_fields_list)))  # Use set to ensure unique fields
    elif 'updated_by' in update_fields_list:  # Only audit field changed
        voucher.save(update_fields=['updated_by'])  # updated_at handled by model/base

    voucher.refresh_from_db()  # Get latest state, especially if lines were manipulated
    logger.info(f"{log_prefix} Successfully updated DRAFT Voucher.")
    return voucher


@transaction.atomic
def submit_voucher_for_approval(
        company_id: int, voucher_id: Union[int, str], submitted_by_user: settings.AUTH_USER_MODEL
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(submitted_by_user, company_instance, 'submit_voucher')
    current_user_display = submitted_by_user.get_full_name() or submitted_by_user.get_username()
    log_prefix = f"[VchSubmit][Co:{company_instance.name}][User:{current_user_display}][VchPK:{voucher_id}]"

    # Fetch voucher, ensuring it belongs to the correct company
    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('company', 'accounting_period'),  # Ensure company is loaded
        pk=voucher_id, company=company_instance
    )
    logger.info(f"{log_prefix} Submitting Voucher.")

    if voucher.status != TransactionStatus.DRAFT.value:
        raise InvalidVoucherStatusError(current_status=voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.DRAFT.label])

    _validate_voucher_essentials(voucher, voucher.company)  # Pass voucher.company

    update_fields_list = ['status', 'updated_by']
    if not voucher.voucher_number:
        assign_voucher_number(voucher, voucher.company)  # Call the utility correctly
        update_fields_list.append('voucher_number')

    original_status = voucher.status
    voucher.status = TransactionStatus.PENDING_APPROVAL.value
    voucher.updated_by = submitted_by_user
    voucher.save(update_fields=update_fields_list)

    _log_approval_action(voucher, submitted_by_user, ApprovalActionType.SUBMITTED.value, original_status,
                         voucher.status, _("Submitted for approval."))
    logger.info(f"{log_prefix} Successfully submitted Voucher {voucher.voucher_number or voucher.pk}.")
    return voucher


@transaction.atomic
def approve_and_post_voucher(
        company_id: int, voucher_id: Union[int, str], approver_user: settings.AUTH_USER_MODEL, comments: str = ""
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    # Check permissions first
    _check_role_permission(approver_user, company_instance, 'approve_voucher')
    _check_role_permission(approver_user, company_instance, 'post_voucher')  # Can be combined if approve implies post
    current_user_display = approver_user.get_full_name() or approver_user.get_username()
    log_prefix = f"[VchApprovePost][Co:{company_instance.name}][User:{current_user_display}][VchPK:{voucher_id}]"

    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('company', 'accounting_period'),
        pk=voucher_id, company=company_instance
    )
    logger.info(f"{log_prefix} Approving & posting Voucher.")

    allowed_statuses = [TransactionStatus.PENDING_APPROVAL.value,
                        TransactionStatus.REJECTED.value]  # Can re-approve a rejected one
    if voucher.status not in allowed_statuses:
        raise InvalidVoucherStatusError(current_status=voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.PENDING_APPROVAL.label,
                                                           TransactionStatus.REJECTED.label])

    _validate_voucher_for_posting(voucher, voucher.company)  # Pass voucher.company

    original_status = voucher.status
    current_time = timezone.now()
    voucher.status = TransactionStatus.POSTED.value
    voucher.posted_by = approver_user
    voucher.posted_at = current_time
    voucher.approved_by = approver_user  # Typically same as poster if single level approval
    voucher.approved_at = current_time
    voucher.updated_by = approver_user
    # balances_updated will be set by the task signal after this commit.
    voucher.save(update_fields=['status', 'posted_by', 'posted_at', 'approved_by', 'approved_at', 'updated_by'])

    _log_approval_action(voucher, approver_user, ApprovalActionType.APPROVED.value, original_status, voucher.status,
                         comments or _("Approved and Posted."))
    # _trigger_balance_updates is now handled by signal on Voucher post_save
    # _trigger_balance_updates(voucher, voucher.company)
    logger.info(f"{log_prefix} Approved & posted Voucher {voucher.voucher_number or voucher.pk}.")
    return voucher


@transaction.atomic
def reject_voucher(
        company_id: int, voucher_id: Union[int, str], rejecting_user: settings.AUTH_USER_MODEL, comments: str
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(rejecting_user, company_instance, 'reject_voucher')
    current_user_display = rejecting_user.get_full_name() or rejecting_user.get_username()
    log_prefix = f"[VchReject][Co:{company_instance.name}][User:{current_user_display}][VchPK:{voucher_id}]"

    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('company'),
        pk=voucher_id, company=company_instance
    )
    logger.info(f"{log_prefix} Rejecting Voucher.")

    if voucher.status != TransactionStatus.PENDING_APPROVAL.value:
        raise InvalidVoucherStatusError(current_status=voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.PENDING_APPROVAL.label])
    if not comments or not comments.strip():  # Comments are mandatory for rejection
        raise DjangoValidationError({'comments': _("Rejection comments are mandatory for audit trail and clarity.")})

    original_status = voucher.status
    voucher.status = TransactionStatus.REJECTED.value
    voucher.updated_by = rejecting_user
    voucher.save(update_fields=['status', 'updated_by'])

    _log_approval_action(voucher, rejecting_user, ApprovalActionType.REJECTED.value, original_status, voucher.status,
                         comments)
    logger.warning(f"{log_prefix} Voucher {voucher.voucher_number or voucher.pk} REJECTED. Reason: {comments}")
    return voucher


@transaction.atomic
def create_reversing_voucher(
        company_id: int, original_voucher_id: Union[int, str], user: settings.AUTH_USER_MODEL,
        reversal_date: Optional[timezone.datetime.date] = None,
        reversal_voucher_type_value: str = VoucherType.GENERAL.value,  # Default to General for reversals
        post_immediately: bool = False  # Option to directly post the reversal
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(user, company_instance, 'create_reversal_voucher')
    current_user_display = user.get_full_name() or user.get_username()
    log_prefix = f"[VchReversal][Co:{company_instance.name}][User:{current_user_display}]"

    original_voucher = get_object_or_404(
        Voucher.objects.prefetch_related('lines__account__company').select_related('company', 'party',
                                                                                   'accounting_period'),
        pk=original_voucher_id, company=company_instance
    )
    logger.info(
        f"{log_prefix} Creating reversal for Original Vch {original_voucher.voucher_number or original_voucher.pk}.")

    if original_voucher.status != TransactionStatus.POSTED.value:
        raise InvalidVoucherStatusError(current_status=original_voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.POSTED.label])
    if original_voucher.is_reversed:
        raise VoucherWorkflowError(
            _("Original voucher '%(num)s' has already been reversed by voucher '%(rev_vch)s'.") % {
                'num': original_voucher.voucher_number or original_voucher.pk,
                'rev_vch': original_voucher.reversed_by_voucher.voucher_number if original_voucher.reversed_by_voucher else 'Unknown'
            })

    effective_reversal_date = reversal_date if reversal_date else timezone.now().date()  # Default to today
    reversal_period = _get_valid_accounting_period(company_instance, effective_reversal_date)

    reversing_voucher = Voucher(
        company=company_instance, date=effective_reversal_date, effective_date=effective_reversal_date,
        narration=_(
            f"Reversal of: {original_voucher.voucher_number or original_voucher.pk}. Orig.Narr: {original_voucher.narration or ''}")[
                  :Voucher._meta.get_field('narration').max_length],
        voucher_type=reversal_voucher_type_value, status=TransactionStatus.DRAFT.value,
        party=original_voucher.party, accounting_period=reversal_period,
        reference=f"REV-{original_voucher.voucher_number or original_voucher.pk}"[
                  :Voucher._meta.get_field('reference').max_length],
        created_by=user, updated_by=user, is_reversal_for=original_voucher
    )
    reversing_voucher.full_clean(
        exclude=['voucher_number', 'posted_by', 'posted_at', 'approved_by', 'approved_at', 'balances_updated',
                 'is_reversed'])
    reversing_voucher.save()

    new_lines_data = []
    for line in original_voucher.lines.all():
        # Double check account's company, though lines should inherit from voucher's company implicitly
        if line.account.company_id != company_instance.id:
            logger.error(
                f"{log_prefix} Data integrity error: Account PK {line.account.pk} on original voucher line does not belong to company '{company_instance.name}'.")
            raise VoucherWorkflowError(f"Account on original voucher line does not belong to the reversal company.")
        if not line.account.is_active or not line.account.allow_direct_posting:
            raise DjangoValidationError(
                f"Cannot reverse: Original line account '{line.account.account_name}' (PK:{line.account.pk}) is inactive or disallows posting.")
        new_lines_data.append({
            'account_id': line.account_id,
            'dr_cr': DrCrType.CREDIT.value if line.dr_cr == DrCrType.DEBIT.value else DrCrType.DEBIT.value,
            # Reverse Dr/Cr
            'amount': line.amount,
            'narration': f"Reversal - {line.narration or ''}"[:VoucherLine._meta.get_field('narration').max_length]
        })

    if not new_lines_data:  # Should not happen if original voucher had lines
        raise VoucherWorkflowError(_("Original voucher has no lines to reverse."))
    _create_or_update_voucher_lines(reversing_voucher, new_lines_data, company_instance, is_new_voucher=True)
    reversing_voucher.refresh_from_db()
    validate_voucher_balance(reversing_voucher)

    log_from_status_reversal = TransactionStatus.DRAFT.value
    log_comment_reversal = f"Reversing voucher created for {original_voucher.voucher_number or original_voucher.pk}."
    update_fields_for_reversal_save = ['status', 'voucher_number', 'posted_by', 'posted_at', 'updated_by',
                                       'approved_by', 'approved_at']

    if post_immediately:
        _check_role_permission(user, company_instance, 'post_voucher')  # User posting reversal needs post permission
        assign_voucher_number(reversing_voucher, company_instance)  # Assign number before posting
        _validate_voucher_for_posting(reversing_voucher, company_instance)
        current_time = timezone.now()
        reversing_voucher.status = TransactionStatus.POSTED.value
        reversing_voucher.posted_by = user
        reversing_voucher.posted_at = current_time
        reversing_voucher.approved_by = user  # Auto-approved if posted immediately
        reversing_voucher.approved_at = current_time
        reversing_voucher.updated_by = user  # Ensure updated_by is set
        reversing_voucher.save(update_fields=update_fields_for_reversal_save)
        # _trigger_balance_updates(reversing_voucher, company_instance) # Handled by signal
        log_comment_reversal = f"Reversing voucher auto-posted for {original_voucher.voucher_number or original_voucher.pk}."
        logger.info(
            f"{log_prefix} Auto-posted Reversing Voucher {reversing_voucher.voucher_number or reversing_voucher.pk}.")
    else:
        logger.info(f"{log_prefix} Created Reversing Voucher {reversing_voucher.pk} in Draft status.")

    _log_approval_action(reversing_voucher, user, ApprovalActionType.SYSTEM.value, log_from_status_reversal,
                         reversing_voucher.status, log_comment_reversal)

    # Mark original voucher as reversed
    original_voucher.is_reversed = True
    original_voucher.updated_by = user  # Track who initiated the action leading to reversal
    original_voucher.save(update_fields=['is_reversed', 'updated_by'])  # updated_at will auto update

    logger.info(
        f"{log_prefix} Successfully created Reversing Voucher {reversing_voucher.voucher_number or reversing_voucher.pk}.")
    return reversing_voucher


# =============================================
# INTERNAL HELPER & VALIDATION FUNCTIONS
# =============================================
def _get_valid_accounting_period(company: Company, for_date: timezone.datetime.date) -> AccountingPeriod:
    # This function looks robust.
    try:
        period = AccountingPeriod.objects.get(company=company, start_date__lte=for_date, end_date__gte=for_date)
        if period.locked:
            logger.warning(
                f"Attempt to use locked Accounting Period '{period.name}' for Co '{company.name}', Date {for_date}.")
            raise PeriodLockedError(period_name=str(period))
        return period
    except AccountingPeriod.DoesNotExist:
        logger.error(f"No open/valid Accounting Period found for Co '{company.name}' for Date {for_date}.")
        raise DjangoValidationError(  # More specific error field for forms
            {'accounting_period': _(
                "No open accounting period found for company '%(company_name)s' for date %(date)s.") %
                                  {'company_name': company.name, 'date': for_date}}
        )


def _get_valid_party(company: Company, party_pk: Optional[Union[int, str]]) -> Optional[Party]:
    # This function looks robust.
    if not party_pk: return None
    try:
        # Ensure party is active for new transactions
        return Party.objects.get(pk=party_pk, company=company, is_active=True)
    except Party.DoesNotExist:
        logger.warning(f"Party ID '{party_pk}' not found, inactive, or not for Co '{company.name}'.")
        raise DjangoValidationError(
            {'party': _(
                "Party ID '%(party_pk)s' not found, is inactive, or does not belong to company '%(company_name)s'.") %
                      {'party_pk': party_pk, 'company_name': company.name}}
        )


def _create_or_update_voucher_lines(voucher: Voucher, lines_data: List[Dict[str, Any]], company: Company,
                                    is_new_voucher: bool):
    # This function looks largely robust. Minor logging/error message tweaks.
    log_prefix = f"[VchLinesUpdate][Co:{company.name}][Vch:{voucher.pk}]"
    if not is_new_voucher:
        voucher.lines.all().delete()  # Clear existing lines for an update operation
        logger.debug(f"{log_prefix} Cleared old lines for update.")

    if not lines_data:  # No lines provided
        # For non-draft vouchers, lines are usually mandatory.
        if voucher.status != TransactionStatus.DRAFT.value:
            logger.warning(f"{log_prefix} No lines_data provided for a non-DRAFT voucher (Status: {voucher.status}).")
            raise DjangoValidationError(
                {'lines_data': _("Voucher must have at least one line item if not in draft status.")})
        logger.debug(f"{log_prefix} No lines_data provided (Status: {voucher.status}). Permitted for draft.")
        return  # OK for draft to have no lines initially

    new_lines_to_create_instances = []
    validation_errors_for_lines = []
    for idx, line_data in enumerate(lines_data):
        line_error_prefix = f"Line {idx + 1}: "
        try:
            account_id = line_data.get('account_id')
            if not account_id: validation_errors_for_lines.append(
                line_error_prefix + _("Account ID is missing.")); continue

            amount_str = str(line_data.get('amount', '0'))
            try:
                amount_decimal = Decimal(amount_str)
            except:
                validation_errors_for_lines.append(line_error_prefix + _("Invalid amount format.")); continue

            # Amount validation using MinValueValidator logic from model
            if amount_decimal <= Decimal('0.000000'):  # Check against actual zero or very small if MinValue allows
                validation_errors_for_lines.append(
                    line_error_prefix + _("Amount must be a positive value greater than zero."));
                continue

            dr_cr_val = line_data.get('dr_cr')
            if dr_cr_val not in [DrCrType.DEBIT.value, DrCrType.CREDIT.value]: validation_errors_for_lines.append(
                line_error_prefix + _("Invalid Dr/Cr value.")); continue

            # Fetch account ensuring it belongs to the correct company and is postable
            account_instance = Account.objects.get(pk=account_id, company=company, is_active=True,
                                                   allow_direct_posting=True)

            new_lines_to_create_instances.append(VoucherLine(
                voucher=voucher, account=account_instance, dr_cr=dr_cr_val, amount=amount_decimal,
                narration=line_data.get('narration', '')[:VoucherLine._meta.get_field('narration').max_length]
                # Max length
            ))
        except Account.DoesNotExist:
            validation_errors_for_lines.append(line_error_prefix + _(
                "Account (ID: %(id)s) is invalid for this company, inactive, or disallows direct posting.") % {
                                                   'id': account_id})
        except Exception as e_line:  # Catch any other unexpected error for this line
            logger.error(f"{log_prefix} Line {idx + 1}: Error processing line_data '{line_data}'. Error: {e_line}",
                         exc_info=True)
            validation_errors_for_lines.append(line_error_prefix + _("Unexpected error processing this line's data."))

    if validation_errors_for_lines:
        logger.warning(f"{log_prefix} Validation errors found in lines_data: {validation_errors_for_lines}")
        raise DjangoValidationError({'lines_data': validation_errors_for_lines})  # Raise as dict for form field errors

    if not new_lines_to_create_instances and voucher.status != TransactionStatus.DRAFT.value:
        raise DjangoValidationError(
            {'lines_data': _("Voucher requires at least one valid line item if not in draft status.")})

    if new_lines_to_create_instances:
        try:
            VoucherLine.objects.bulk_create(new_lines_to_create_instances)
            logger.debug(f"{log_prefix} Successfully bulk_created {len(new_lines_to_create_instances)} lines.")
        except IntegrityError as ie:  # Catch DB integrity errors
            logger.error(f"{log_prefix} IntegrityError during lines bulk_create: {ie}", exc_info=True)
            raise VoucherWorkflowError(
                _("Failed to save voucher lines due to a data integrity issue: %(error)s") % {'error': str(ie)})


def _validate_voucher_essentials(voucher: Voucher, company: Company):
    # This function looks robust.
    log_prefix = f"[VchValidateEss][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.company_id != company.id:  # Should already be true if voucher fetched with company filter
        raise VoucherWorkflowError(
            _("Voucher company (ID:%(v_co)s) is inconsistent with operational company context (ID:%(op_co)s).") % {
                'v_co': voucher.company_id, 'op_co': company.id})

    _get_valid_accounting_period(company, voucher.date)  # Re-validate period for the voucher date

    if not voucher.lines.exists() and voucher.status != TransactionStatus.DRAFT.value:
        raise DjangoValidationError({'lines': _("A non-Draft voucher must have at least one line item.")})

    line_errors = []
    for idx, line in enumerate(voucher.lines.select_related('account__company').all()):
        line_prefix = f"Line {idx + 1} (Acc:{line.account_id}): "
        if not line.account: line_errors.append(
            line_prefix + _("Missing account.")); continue  # Should not happen if DB FK is enforced
        if line.account.company_id != company.id:
            line_errors.append(line_prefix + _(
                "Account '%(acc_name)s' (Co: %(acc_co_name)s) does not belong to the voucher's company ('%(vch_co_name)s').") %
                               {'acc_name': line.account.account_name, 'acc_co_name': line.account.company.name,
                                'vch_co_name': company.name})
        if not line.account.is_active: line_errors.append(
            line_prefix + _("Account '%(acc)s' is inactive.") % {'acc': line.account.account_name})
        if not line.account.allow_direct_posting: line_errors.append(
            line_prefix + _("Account '%(acc)s' does not allow direct posting.") % {'acc': line.account.account_name})

    if line_errors:
        logger.warning(f"{log_prefix} Validation errors in lines: {line_errors}")
        raise DjangoValidationError({'lines_data_validation': line_errors})  # For form field specific errors

    validate_voucher_balance(voucher)  # Check if debits == credits


def validate_voucher_balance(voucher: Voucher):
    # This function looks robust.
    if not voucher: return  # Should not happen
    current_lines = list(voucher.lines.all())  # Fetch all lines once for calculation

    if not current_lines:
        # A DRAFT voucher can be imbalanced or have no lines.
        # A non-DRAFT voucher usually must have lines and be balanced.
        if voucher.status != TransactionStatus.DRAFT.value:
            raise DjangoValidationError(_("A non-Draft voucher must have line items to check balance."))
        logger.debug(f"Vch {voucher.pk} (Co: {voucher.company_id}) is DRAFT with no lines. Balance check skipped.")
        return  # OK for draft with no lines

    total_debit = sum(
        line.amount for line in current_lines if line.dr_cr == DrCrType.DEBIT.value and line.amount is not None)
    total_credit = sum(
        line.amount for line in current_lines if line.dr_cr == DrCrType.CREDIT.value and line.amount is not None)

    if abs(total_debit - total_credit) >= Decimal('0.01'):  # Use a small tolerance for decimal comparisons
        logger.warning(
            f"Voucher {voucher.pk} (Co: {voucher.company_id}) is imbalanced. Debits: {total_debit}, Credits: {total_credit}")
        raise BalanceError(debits=total_debit, credits=total_credit)  # Custom exception

    logger.debug(
        f"Balance validated for Vch {voucher.pk} (Co: {voucher.company_id}). Dr:{total_debit}, Cr:{total_credit}")


def _validate_voucher_for_posting(voucher: Voucher, company: Company):
    # This function looks robust.
    log_prefix = f"[VchValidatePost][Co:{company.name}][Vch:{voucher.voucher_number or voucher.pk}]"
    logger.debug(f"{log_prefix} Performing pre-posting validation...")
    _validate_voucher_essentials(voucher, company)  # Includes balance check
    if not voucher.voucher_number:  # Must have a number before posting
        logger.error(f"{log_prefix} Attempt to post voucher without a voucher number.")
        raise VoucherWorkflowError(_("Voucher number is missing. Please submit the voucher first to assign a number."))
    # is_balanced check is inside _validate_voucher_essentials via validate_voucher_balance
    logger.info(f"{log_prefix} Pre-Posting Validation Passed.")


def _trigger_balance_updates(voucher: Voucher, company: Company):
    """ Enqueues task to update account balances for a POSTED voucher. """
    log_prefix = f"[TriggerBalUpd][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.status != TransactionStatus.POSTED.value:
        logger.debug(f"{log_prefix} Balance update trigger skipped (Status: {voucher.get_status_display()}).")
        return
    if voucher.balances_updated:  # Idempotency for enqueueing
        logger.info(f"{log_prefix} Balances already marked updated. Skipping Celery task enqueue.")
        return

    company_id_for_task = voucher.company_id
    if not company_id_for_task:  # Should be set if voucher.company is set
        logger.critical(f"{log_prefix} CRITICAL: Voucher missing company_id. CANNOT ENQUEUE balance update task.")
        return

    logger.info(
        f"{log_prefix} Enqueuing balance update task for POSTED Voucher '{voucher.voucher_number or voucher.pk}'.")
    try:
        # Call with only voucher.id and company_id_for_task
        update_account_balances_task.apply_async(args=[voucher.id, company_id_for_task])
        logger.debug(f"{log_prefix} Celery task enqueued successfully.")
    except Exception as e:
        logger.critical(f"{log_prefix} CRITICAL - FAILED TO ENQUEUE balance update task. Error: {e}", exc_info=True)


def _log_approval_action(voucher: Voucher, user: settings.AUTH_USER_MODEL, action_type: str, from_status: str,
                         to_status: str, comments: str):
    # This function looks robust.
    try:
        VoucherApproval.objects.create(
            voucher=voucher, user=user, company=voucher.company,  # Ensure company is set here from voucher
            action_type=action_type, from_status=from_status, to_status=to_status, comments=comments
        )
        current_user_display = user.get_full_name() or user.get_username()
        logger.debug(
            f"Logged action '{action_type}' for Vch {voucher.pk} by user '{current_user_display}' for Co '{voucher.company.name}'.")
    except Exception as e:
        logger.error(
            f"Failed to log approval action '{action_type}' for Vch {voucher.pk} (Co '{voucher.company.name}'): {e}",
            exc_info=True)


# --- Voucher Number Assignment Utility ---
def assign_voucher_number(voucher: Voucher, company: Company):
    """
    Assigns a voucher number using the sequence_service.
    The voucher instance is modified but NOT saved here; the caller saves.
    """
    log_prefix = f"[AssignVchNum][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.voucher_number:  # Idempotency
        logger.debug(f"{log_prefix} Already has number '{voucher.voucher_number}'. Skipping assignment.")
        return

    if not voucher.accounting_period_id:  # CRITICAL for sequence_service
        logger.error(f"{log_prefix} Accounting period ID is missing. Cannot assign number.")
        raise DjangoValidationError(
            {'accounting_period': _("Accounting period is required to assign a voucher number.")})

    if voucher.company_id != company.id:  # Integrity check
        logger.error(
            f"{log_prefix} Company mismatch: Voucher Co ID {voucher.company_id} vs Context Co ID {company.id}.")
        raise PermissionDenied(_("Company context mismatch during voucher numbering operation."))

    try:
        logger.debug(
            f"{log_prefix} Calling sequence_service for Type:'{voucher.voucher_type}', PeriodID:{voucher.accounting_period_id}")
        next_number_str = sequence_service.get_next_voucher_number(
            company_id=company.id,  # Pass company ID
            voucher_type_value=voucher.voucher_type,  # Pass voucher_type string value
            period_id=voucher.accounting_period_id  # Pass period ID
        )
        voucher.voucher_number = next_number_str  # Assign to the instance
        logger.info(f"{log_prefix} Successfully assigned voucher number '{next_number_str}'.")
    except Exception as e:  # Catch errors from sequence_service (e.g., DjangoValidationError, ValueError)
        logger.error(f"{log_prefix} Error from sequence_service: {e}", exc_info=True)
        # Re-raise as a VoucherWorkflowError or allow original exception to propagate
        # If sequence_service raises DjangoValidationError, it might already be good for forms.
        # For clarity, wrapping in a domain-specific error can be useful.
        if isinstance(e, DjangoValidationError):
            raise  # Re-raise DjangoValidationError directly for form display
        raise VoucherWorkflowError(_("Failed to generate voucher number: %(error)s") % {'error': str(e)}) from e