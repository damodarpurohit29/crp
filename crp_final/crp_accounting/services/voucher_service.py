# crp_accounting/services/voucher_service.py

import logging
from datetime import date
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

# Define role values assuming CompanyMembership.Role is an Enum or TextChoices
# These should match the .value attribute of your CompanyMembership.Role members
# If CompanyMembership model is not available at this point, these are placeholders.
# It's CRITICAL that these string values match exactly what's stored in the DB for roles
# and what CompanyMembership.Role.SOME_ROLE.value would resolve to.

_ROLE_OWNER = "OWNER"
_ROLE_ADMIN = "ADMIN"
_ROLE_ACCOUNTING_MANAGER = "ACCOUNTING_MANAGER"
_ROLE_ACCOUNTANT = "ACCOUNTANT"
_ROLE_DATA_ENTRY = "DATA_ENTRY"
_ROLE_SALES_REP = "SALES_REP"
_ROLE_PURCHASE_OFFICER = "PURCHASE_OFFICER"
_ROLE_AUDITOR = "AUDITOR"  # Assuming an AUDITOR role might exist or be added
_ROLE_VIEW_ONLY = "VIEW_ONLY"  # Assuming a VIEW_ONLY role might exist or be added

# Fallback if CompanyMembership can't be imported
if CompanyMembership:
    _ROLE_OWNER = CompanyMembership.Role.OWNER.value
    _ROLE_ADMIN = CompanyMembership.Role.ADMIN.value
    _ROLE_ACCOUNTING_MANAGER = CompanyMembership.Role.ACCOUNTING_MANAGER.value
    _ROLE_ACCOUNTANT = CompanyMembership.Role.ACCOUNTANT.value
    _ROLE_DATA_ENTRY = CompanyMembership.Role.DATA_ENTRY.value
    if hasattr(CompanyMembership.Role, 'SALES_REP'):  # Check if custom roles exist
        _ROLE_SALES_REP = CompanyMembership.Role.SALES_REP.value
    if hasattr(CompanyMembership.Role, 'PURCHASE_OFFICER'):
        _ROLE_PURCHASE_OFFICER = CompanyMembership.Role.PURCHASE_OFFICER.value
    if hasattr(CompanyMembership.Role, 'AUDITOR'):
        _ROLE_AUDITOR = CompanyMembership.Role.AUDITOR.value
    if hasattr(CompanyMembership.Role, 'VIEW_ONLY'):
        _ROLE_VIEW_ONLY = CompanyMembership.Role.VIEW_ONLY.value

PERMISSIONS_MAP: Dict[str, Set[str]] = {
    # Core Voucher Actions
    'add_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
        _ROLE_DATA_ENTRY, _ROLE_SALES_REP, _ROLE_PURCHASE_OFFICER,
    },
    'change_voucher_draft': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
        _ROLE_DATA_ENTRY,  # Or based on created_by for more granular control
    },
    'submit_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
        _ROLE_DATA_ENTRY, _ROLE_SALES_REP, _ROLE_PURCHASE_OFFICER,
    },
    'approve_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
    },
    'post_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
    },
    'reject_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER,
    },
    'create_reversal_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
    },
    'delete_draft_voucher': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER,  # Or based on created_by
    },

    # Financial Reporting Permissions (as discussed for ACCOUNTANT and higher)
    'generate_trial_balance': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT, _ROLE_AUDITOR,
    },
    'generate_profit_and_loss_statement': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT, _ROLE_AUDITOR,
    },
    'generate_balance_sheet': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT, _ROLE_AUDITOR,
    },
    'view_aging_reports': {  # For A/R, A/P
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT, _ROLE_AUDITOR,
    },
    'access_tax_reports': {
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT, _ROLE_AUDITOR,
    },
    'view_vouchers_and_reports_read_only': {  # General read-only for Auditor/View_Only
        _ROLE_OWNER, _ROLE_ADMIN, _ROLE_ACCOUNTING_MANAGER, _ROLE_ACCOUNTANT,
        _ROLE_DATA_ENTRY, _ROLE_SALES_REP, _ROLE_PURCHASE_OFFICER,
        # All operational roles can view what they interact with
        _ROLE_AUDITOR, _ROLE_VIEW_ONLY,
    }
}


# Clean up PERMISSIONS_MAP if some roles were not defined (e.g. "AUDITOR" as a string if CompanyMembership.Role.AUDITOR didn't exist)
# This is a safeguard if the string placeholders are used but the actual enum doesn't have that member.
# It assumes if a role placeholder (like _ROLE_AUDITOR = "AUDITOR") wasn't overridden by an actual
# CompanyMembership.Role.X.value, then it's a role not yet present in the enum.
# This part is tricky and depends heavily on CompanyMembership.Role structure.
# A simpler approach might be to only add roles if CompanyMembership is available.
# For now, keeping it as is, assuming the string values _are_ the definitive role identifiers if CompanyMembership is missing.


def _check_role_permission(user: settings.AUTH_USER_MODEL, company: Company, action_codename: str):
    if not user or not user.is_authenticated:
        raise PermissionDenied(_("Authentication required to perform this action."))
    if not CompanyMembership or not Company:
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
        membership = CompanyMembership.objects.select_related('company').get(
            user=user, company=company, is_active_membership=True
        )
        if not membership.company.effective_is_active:
            logger.warning(
                f"RBAC Denied: User PK {user.pk} for action '{action_codename}', Co '{company.name}'. Reason: Company is not effectively active.")
            raise PermissionDenied(_("The company '%(company_name)s' is not currently active or accessible.") % {
                'company_name': company.name})
        user_role_value = membership.role
    except CompanyMembership.DoesNotExist:
        logger.warning(
            f"RBAC Denied: User PK {user.pk} for action '{action_codename}', Co '{company.name}'. Reason: No active membership.")
        raise PermissionDenied(
            _("You do not have an active membership or required role in the company '%(company_name)s'.") % {
                'company_name': company.name})
    except Exception as e:
        logger.exception(
            f"RBAC Error: Checking permission for User PK {user.pk}, action '{action_codename}', Co '{company.name}'. Error: {e}")
        raise PermissionDenied(_("An error occurred while verifying your permissions."))

    if user_role_value not in allowed_roles_values:
        user_role_display = membership.get_role_display()
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
# Helper function for Admin display (as discussed)
# =============================================================================
def get_permissions_for_role(role_value: str) -> List[str]:
    """
    Returns a list of human-readable permissions for a given role value
    based on the global PERMISSIONS_MAP.
    """
    granted_actions = []
    if not PERMISSIONS_MAP:
        logger.warning("get_permissions_for_role called but PERMISSIONS_MAP is empty or not loaded.")
        return [_("Permission map not loaded or empty.")]

    # Ensure role_value is a string, in case an enum member itself is passed by mistake.
    # This depends on how CompanyMembership.Role is structured and used.
    # If role_value could be an enum member, you might need:
    # if hasattr(role_value, 'value'): role_value_str = role_value.value
    # else: role_value_str = str(role_value)
    # For now, assuming role_value is already the correct string value (e.g., "OWNER").

    for action_codename, allowed_roles_values in PERMISSIONS_MAP.items():
        if role_value in allowed_roles_values:
            action_display = action_codename.replace('_', ' ').capitalize()
            granted_actions.append(action_display)

    if not granted_actions:
        # This can happen if the role exists but has no permissions in the map.
        return [_("No specific voucher service permissions defined for this role.")]

    return sorted(granted_actions)


# =============================================================================
# Core Workflow Service Functions
# =============================================================================
@transaction.atomic
def create_draft_voucher(
        company_id: int, created_by_user: settings.AUTH_USER_MODEL, voucher_type_value: str,
        date: timezone.datetime.date, narration: str, lines_data: List[Dict[str, Any]],
        party_pk: Optional[Union[int, str]] = None, reference: Optional[str] = None,
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(created_by_user, company_instance, 'add_voucher')
    current_user_display = created_by_user.get_full_name() or created_by_user.get_username()
    log_prefix = f"[VchCreateDraft][Co:{company_instance.name}][User:{current_user_display}]"
    logger.info(f"{log_prefix} Creating DRAFT voucher. Type: {voucher_type_value}, Date: {date}")

    accounting_period = _get_valid_accounting_period(company_instance, date)
    party_instance = _get_valid_party(company_instance, party_pk) if party_pk is not None else None

    voucher = Voucher(
        company=company_instance,
        voucher_type=voucher_type_value, date=date, effective_date=date,
        narration=narration, status=TransactionStatus.DRAFT.value, accounting_period=accounting_period,
        party=party_instance, reference=reference,
        created_by=created_by_user, updated_by=created_by_user
    )
    voucher.full_clean(
        exclude=['voucher_number', 'posted_by', 'posted_at', 'approved_by', 'approved_at', 'balances_updated',
                 'is_reversed', 'is_reversal_for', 'reversed_by_voucher'])  # Added reversed_by_voucher
    voucher.save()

    _create_or_update_voucher_lines(voucher, lines_data, company_instance, is_new_voucher=True)
    voucher.refresh_from_db()
    try:
        validate_voucher_balance(voucher)
    except BalanceError as be:
        logger.warning(f"{log_prefix} DRAFT Voucher {voucher.pk} created with imbalance: {be.message}.")

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

    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('accounting_period', 'party', 'company'),
        pk=voucher_id, company=company_instance, status=TransactionStatus.DRAFT.value
    )
    logger.info(f"{log_prefix} Updating DRAFT Voucher.")

    update_fields_list = ['updated_by']
    voucher.updated_by = updated_by_user

    if 'date' in data_to_update and voucher.date != data_to_update['date']:
        new_date = data_to_update['date']
        voucher.accounting_period = _get_valid_accounting_period(company_instance, new_date)
        voucher.date = new_date
        voucher.effective_date = new_date
        update_fields_list.extend(['date', 'effective_date', 'accounting_period'])

    for field_name in ['narration', 'reference', 'voucher_type']:
        if field_name in data_to_update and getattr(voucher, field_name) != data_to_update[field_name]:
            setattr(voucher, field_name, data_to_update[field_name])
            update_fields_list.append(field_name)

    if 'party_pk' in data_to_update:
        new_party_pk = data_to_update['party_pk']
        new_party_instance = _get_valid_party(company_instance, new_party_pk) if new_party_pk is not None else None
        if voucher.party_id != (new_party_instance.pk if new_party_instance else None):
            voucher.party = new_party_instance
            update_fields_list.append('party')

    if 'lines_data' in data_to_update:
        _create_or_update_voucher_lines(voucher, data_to_update['lines_data'], company_instance, is_new_voucher=False)

    if len(update_fields_list) > 1 or 'lines_data' in data_to_update:
        exclude_from_clean = ['voucher_number', 'posted_by', 'posted_at', 'approved_by', 'approved_at',
                              'balances_updated', 'company', 'created_by', 'is_reversal_for', 'is_reversed',
                              'reversed_by_voucher']  # Added reversed_by_voucher
        voucher.full_clean(exclude=exclude_from_clean)
        voucher.save(update_fields=list(set(update_fields_list)))
    elif 'updated_by' in update_fields_list:
        voucher.save(update_fields=['updated_by'])

    voucher.refresh_from_db()
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

    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('company', 'accounting_period'),
        pk=voucher_id, company=company_instance
    )
    logger.info(f"{log_prefix} Submitting Voucher.")

    if voucher.status != TransactionStatus.DRAFT.value:
        raise InvalidVoucherStatusError(current_status=voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.DRAFT.label])

    _validate_voucher_essentials(voucher, voucher.company)

    update_fields_list = ['status', 'updated_by']
    if not voucher.voucher_number:
        assign_voucher_number(voucher, voucher.company)
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
    _check_role_permission(approver_user, company_instance, 'approve_voucher')
    _check_role_permission(approver_user, company_instance, 'post_voucher')
    current_user_display = approver_user.get_full_name() or approver_user.get_username()
    log_prefix = f"[VchApprovePost][Co:{company_instance.name}][User:{current_user_display}][VchPK:{voucher_id}]"

    voucher = get_object_or_404(
        Voucher.objects.select_for_update().select_related('company', 'accounting_period'),
        pk=voucher_id, company=company_instance
    )
    logger.info(f"{log_prefix} Approving & posting Voucher.")

    allowed_statuses = [TransactionStatus.PENDING_APPROVAL.value,
                        TransactionStatus.REJECTED.value]
    if voucher.status not in allowed_statuses:
        raise InvalidVoucherStatusError(current_status=voucher.get_status_display(),
                                        expected_statuses=[TransactionStatus.PENDING_APPROVAL.label,
                                                           TransactionStatus.REJECTED.label])

    # TODO: Threshold logic for ACCOUNTANT role (as discussed previously)
    # This requires fetching the user's role and checking against voucher properties.
    # Example placeholder:
    # user_membership = CompanyMembership.objects.get(user=approver_user, company=company_instance)
    # if user_membership.role == _ROLE_ACCOUNTANT:
    #     # Assuming voucher has a method or property like 'total_amount'
    #     if hasattr(voucher, 'get_total_amount') and voucher.get_total_amount() > settings.ACCOUNTANT_APPROVAL_THRESHOLD:
    #         raise PermissionDenied(_("Accountants can only approve vouchers below a certain threshold."))
    #     # Add checks for "routine recurring entries" if applicable, e.g., based on voucher_type or a flag

    _validate_voucher_for_posting(voucher, voucher.company)

    original_status = voucher.status
    current_time = timezone.now()
    voucher.status = TransactionStatus.POSTED.value
    voucher.posted_by = approver_user
    voucher.posted_at = current_time
    voucher.approved_by = approver_user
    voucher.approved_at = current_time
    voucher.updated_by = approver_user
    voucher.save(update_fields=['status', 'posted_by', 'posted_at', 'approved_by', 'approved_at', 'updated_by'])

    _log_approval_action(voucher, approver_user, ApprovalActionType.APPROVED.value, original_status, voucher.status,
                         comments or _("Approved and Posted."))
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
    if not comments or not comments.strip():
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
        reversal_voucher_type_value: str = VoucherType.GENERAL.value,
        post_immediately: bool = False
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(user, company_instance, 'create_reversal_voucher')
    current_user_display = user.get_full_name() or user.get_username()
    log_prefix = f"[VchReversal][Co:{company_instance.name}][User:{current_user_display}]"

    original_voucher = get_object_or_404(
        Voucher.objects.prefetch_related('lines__account__company').select_related('company', 'party',
                                                                                   'accounting_period',
                                                                                   'reversed_by_voucher'),
        # Added reversed_by_voucher
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

    effective_reversal_date = reversal_date if reversal_date else timezone.now().date()
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
                 'is_reversed', 'reversed_by_voucher'])  # Added reversed_by_voucher
    reversing_voucher.save()

    new_lines_data = []
    for line in original_voucher.lines.all():
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
            'amount': line.amount,
            'narration': f"Reversal - {line.narration or ''}"[:VoucherLine._meta.get_field('narration').max_length]
        })

    if not new_lines_data:
        raise VoucherWorkflowError(_("Original voucher has no lines to reverse."))
    _create_or_update_voucher_lines(reversing_voucher, new_lines_data, company_instance, is_new_voucher=True)
    reversing_voucher.refresh_from_db()
    validate_voucher_balance(reversing_voucher)

    log_from_status_reversal = TransactionStatus.DRAFT.value
    log_comment_reversal = f"Reversing voucher created for {original_voucher.voucher_number or original_voucher.pk}."
    update_fields_for_reversal_save = ['status', 'voucher_number', 'posted_by', 'posted_at', 'updated_by',
                                       'approved_by', 'approved_at']

    if post_immediately:
        _check_role_permission(user, company_instance, 'approve_voucher')
        _check_role_permission(user, company_instance, 'post_voucher')

        # TODO: Threshold logic for ACCOUNTANT role for reversals (as discussed)
        # user_membership = CompanyMembership.objects.get(user=user, company=company_instance)
        # if user_membership.role == _ROLE_ACCOUNTANT:
        #     # Assuming reversing_voucher has a method or property like 'total_amount'
        #     if hasattr(reversing_voucher, 'get_total_amount') and reversing_voucher.get_total_amount() > settings.ACCOUNTANT_APPROVAL_THRESHOLD:
        #         raise PermissionDenied(_("Accountants can only auto-post reversals below a certain threshold."))

        assign_voucher_number(reversing_voucher, company_instance)
        _validate_voucher_for_posting(reversing_voucher, company_instance)
        current_time = timezone.now()
        reversing_voucher.status = TransactionStatus.POSTED.value
        reversing_voucher.posted_by = user
        reversing_voucher.posted_at = current_time
        reversing_voucher.approved_by = user
        reversing_voucher.approved_at = current_time
        reversing_voucher.updated_by = user
        reversing_voucher.save(update_fields=update_fields_for_reversal_save)
        log_comment_reversal = f"Reversing voucher auto-posted for {original_voucher.voucher_number or original_voucher.pk}."
        logger.info(
            f"{log_prefix} Auto-posted Reversing Voucher {reversing_voucher.voucher_number or reversing_voucher.pk}.")
    else:
        logger.info(f"{log_prefix} Created Reversing Voucher {reversing_voucher.pk} in Draft status.")

    _log_approval_action(reversing_voucher, user, ApprovalActionType.SYSTEM.value, log_from_status_reversal,
                         reversing_voucher.status, log_comment_reversal)

    original_voucher.is_reversed = True
    original_voucher.reversed_by_voucher = reversing_voucher
    original_voucher.updated_by = user
    original_voucher.save(update_fields=['is_reversed', 'reversed_by_voucher', 'updated_by'])

    logger.info(
        f"{log_prefix} Successfully created Reversing Voucher {reversing_voucher.voucher_number or reversing_voucher.pk}.")
    return reversing_voucher


# =============================================
# INTERNAL HELPER & VALIDATION FUNCTIONS
# =============================================
def _get_valid_accounting_period(company: Company, for_date: timezone.datetime.date) -> AccountingPeriod:
    try:
        period = AccountingPeriod.objects.get(company=company, start_date__lte=for_date, end_date__gte=for_date)
        if period.locked:
            logger.warning(
                f"Attempt to use locked Accounting Period '{period.name}' for Co '{company.name}', Date {for_date}.")
            raise PeriodLockedError(period_name=str(period))
        return period
    except AccountingPeriod.DoesNotExist:
        logger.error(f"No open/valid Accounting Period found for Co '{company.name}' for Date {for_date}.")
        raise DjangoValidationError(
            {'accounting_period': _(
                "No open accounting period found for company '%(company_name)s' for date %(date)s.") %
                                  {'company_name': company.name, 'date': for_date}}
        )


def _get_valid_party(company: Company, party_pk: Optional[Union[int, str]]) -> Optional[Party]:
    if not party_pk: return None
    try:
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
    log_prefix = f"[VchLinesUpdate][Co:{company.name}][Vch:{voucher.pk}]"
    if not is_new_voucher:
        voucher.lines.all().delete()
        logger.debug(f"{log_prefix} Cleared old lines for update.")

    if not lines_data:
        if voucher.status != TransactionStatus.DRAFT.value:
            logger.warning(f"{log_prefix} No lines_data provided for a non-DRAFT voucher (Status: {voucher.status}).")
            raise DjangoValidationError(
                {'lines_data': _("Voucher must have at least one line item if not in draft status.")})
        logger.debug(f"{log_prefix} No lines_data provided (Status: {voucher.status}). Permitted for draft.")
        return

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
                validation_errors_for_lines.append(line_error_prefix + _("Invalid amount format."));
                continue
            if amount_decimal <= Decimal('0.000000'):
                validation_errors_for_lines.append(
                    line_error_prefix + _("Amount must be a positive value greater than zero."));
                continue

            dr_cr_val = line_data.get('dr_cr')
            if dr_cr_val not in [DrCrType.DEBIT.value, DrCrType.CREDIT.value]: validation_errors_for_lines.append(
                line_error_prefix + _("Invalid Dr/Cr value.")); continue

            account_instance = Account.objects.get(pk=account_id, company=company, is_active=True,
                                                   allow_direct_posting=True)

            new_lines_to_create_instances.append(VoucherLine(
                voucher=voucher, account=account_instance, dr_cr=dr_cr_val, amount=amount_decimal,
                narration=line_data.get('narration', '')[:VoucherLine._meta.get_field('narration').max_length]
            ))
        except Account.DoesNotExist:
            validation_errors_for_lines.append(line_error_prefix + _(
                "Account (ID: %(id)s) is invalid for this company, inactive, or disallows direct posting.") % {
                                                   'id': account_id})
        except Exception as e_line:
            logger.error(f"{log_prefix} Line {idx + 1}: Error processing line_data '{line_data}'. Error: {e_line}",
                         exc_info=True)
            validation_errors_for_lines.append(line_error_prefix + _("Unexpected error processing this line's data."))

    if validation_errors_for_lines:
        logger.warning(f"{log_prefix} Validation errors found in lines_data: {validation_errors_for_lines}")
        raise DjangoValidationError({'lines_data': validation_errors_for_lines})

    if not new_lines_to_create_instances and voucher.status != TransactionStatus.DRAFT.value:
        raise DjangoValidationError(
            {'lines_data': _("Voucher requires at least one valid line item if not in draft status.")})

    if new_lines_to_create_instances:
        try:
            VoucherLine.objects.bulk_create(new_lines_to_create_instances)
            logger.debug(f"{log_prefix} Successfully bulk_created {len(new_lines_to_create_instances)} lines.")
        except IntegrityError as ie:
            logger.error(f"{log_prefix} IntegrityError during lines bulk_create: {ie}", exc_info=True)
            raise VoucherWorkflowError(
                _("Failed to save voucher lines due to a data integrity issue: %(error)s") % {'error': str(ie)})


def _validate_voucher_essentials(voucher: Voucher, company: Company):
    log_prefix = f"[VchValidateEss][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.company_id != company.id:
        raise VoucherWorkflowError(
            _("Voucher company (ID:%(v_co)s) is inconsistent with operational company context (ID:%(op_co)s).") % {
                'v_co': voucher.company_id, 'op_co': company.id})

    _get_valid_accounting_period(company, voucher.date)

    if not voucher.lines.exists() and voucher.status != TransactionStatus.DRAFT.value:
        raise DjangoValidationError({'lines': _("A non-Draft voucher must have at least one line item.")})

    line_errors = []
    for idx, line in enumerate(voucher.lines.select_related('account__company').all()):
        line_prefix = f"Line {idx + 1} (Acc:{line.account_id}): "
        if not line.account: line_errors.append(
            line_prefix + _("Missing account.")); continue
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
        raise DjangoValidationError({'lines_data_validation': line_errors})

    validate_voucher_balance(voucher)


def validate_voucher_balance(voucher: Voucher):
    if not voucher: return
    current_lines = list(voucher.lines.all())

    if not current_lines:
        if voucher.status != TransactionStatus.DRAFT.value:
            raise DjangoValidationError(_("A non-Draft voucher must have line items to check balance."))
        logger.debug(f"Vch {voucher.pk} (Co: {voucher.company_id}) is DRAFT with no lines. Balance check skipped.")
        return

    total_debit = sum(
        line.amount for line in current_lines if line.dr_cr == DrCrType.DEBIT.value and line.amount is not None)
    total_credit = sum(
        line.amount for line in current_lines if line.dr_cr == DrCrType.CREDIT.value and line.amount is not None)

    if abs(total_debit - total_credit) >= Decimal('0.01'):
        logger.warning(
            f"Voucher {voucher.pk} (Co: {voucher.company_id}) is imbalanced. Debits: {total_debit}, Credits: {total_credit}")
        raise BalanceError(debits=total_debit, credits=total_credit)

    logger.debug(
        f"Balance validated for Vch {voucher.pk} (Co: {voucher.company_id}). Dr:{total_debit}, Cr:{total_credit}")


def _validate_voucher_for_posting(voucher: Voucher, company: Company):
    log_prefix = f"[VchValidatePost][Co:{company.name}][Vch:{voucher.voucher_number or voucher.pk}]"
    logger.debug(f"{log_prefix} Performing pre-posting validation...")
    _validate_voucher_essentials(voucher, company)
    if not voucher.voucher_number:
        logger.error(f"{log_prefix} Attempt to post voucher without a voucher number.")
        raise VoucherWorkflowError(_("Voucher number is missing. Please submit the voucher first to assign a number."))
    logger.info(f"{log_prefix} Pre-Posting Validation Passed.")


def _trigger_balance_updates(voucher: Voucher, company: Company):
    """ Enqueues task to update account balances for a POSTED voucher. (Currently unused, handled by signal) """
    log_prefix = f"[TriggerBalUpd][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.status != TransactionStatus.POSTED.value:
        logger.debug(f"{log_prefix} Balance update trigger skipped (Status: {voucher.get_status_display()}).")
        return
    if voucher.balances_updated:
        logger.info(f"{log_prefix} Balances already marked updated. Skipping Celery task enqueue.")
        return

    company_id_for_task = voucher.company_id
    if not company_id_for_task:
        logger.critical(f"{log_prefix} CRITICAL: Voucher missing company_id. CANNOT ENQUEUE balance update task.")
        return

    logger.info(
        f"{log_prefix} Enqueuing balance update task for POSTED Voucher '{voucher.voucher_number or voucher.pk}'.")
    try:
        update_account_balances_task.apply_async(args=[voucher.id, company_id_for_task])
        logger.debug(f"{log_prefix} Celery task enqueued successfully.")
    except Exception as e:
        logger.critical(f"{log_prefix} CRITICAL - FAILED TO ENQUEUE balance update task. Error: {e}", exc_info=True)


def _log_approval_action(voucher: Voucher, user: settings.AUTH_USER_MODEL, action_type: str, from_status: str,
                         to_status: str, comments: str):
    try:
        VoucherApproval.objects.create(
            voucher=voucher, user=user, company=voucher.company,
            action_type=action_type, from_status=from_status, to_status=to_status, comments=comments
        )
        current_user_display = user.get_full_name() or user.get_username()
        logger.debug(
            f"Logged action '{action_type}' for Vch {voucher.pk} by user '{current_user_display}' for Co '{voucher.company.name}'.")
    except Exception as e:
        logger.error(
            f"Failed to log approval action '{action_type}' for Vch {voucher.pk} (Co '{voucher.company.name}'): {e}",
            exc_info=True)


def assign_voucher_number(voucher: Voucher, company: Company):
    log_prefix = f"[AssignVchNum][Co:{company.name}][Vch:{voucher.pk}]"
    if voucher.voucher_number:
        logger.debug(f"{log_prefix} Already has number '{voucher.voucher_number}'. Skipping assignment.")
        return

    if not voucher.accounting_period_id:
        logger.error(f"{log_prefix} Accounting period ID is missing. Cannot assign number.")
        raise DjangoValidationError(
            {'accounting_period': _("Accounting period is required to assign a voucher number.")})

    if voucher.company_id != company.id:
        logger.error(
            f"{log_prefix} Company mismatch: Voucher Co ID {voucher.company_id} vs Context Co ID {company.id}.")
        raise PermissionDenied(_("Company context mismatch during voucher numbering operation."))

    try:
        logger.debug(
            f"{log_prefix} Calling sequence_service for Type:'{voucher.voucher_type}', PeriodID:{voucher.accounting_period_id}")
        next_number_str = sequence_service.get_next_voucher_number(
            company_id=company.id,
            voucher_type_value=voucher.voucher_type,
            period_id=voucher.accounting_period_id
        )
        voucher.voucher_number = next_number_str
        logger.info(f"{log_prefix} Successfully assigned voucher number '{next_number_str}'.")
    except Exception as e:
        logger.error(f"{log_prefix} Error from sequence_service: {e}", exc_info=True)
        if isinstance(e, DjangoValidationError):
            raise
        raise VoucherWorkflowError(_("Failed to generate voucher number: %(error)s") % {'error': str(e)}) from e


@transaction.atomic
def create_reversing_voucher(
        company_id: int,  # Changed to int for consistency
        original_voucher_id: Union[int, str],  # Changed for consistency
        user: settings.AUTH_USER_MODEL,  # User performing the reversal
        reversal_date: Optional[date] = None,
        reversal_voucher_type_value: str = VoucherType.GENERAL.value,  # Ensure this type exists
        post_immediately: bool = False,  # If True, the new reversing voucher is also posted
        reversal_narration_prefix: str = _("Reversal of:")  # Use the parameter
) -> Voucher:
    company_instance = get_object_or_404(Company, pk=company_id)
    _check_role_permission(user, company_instance, 'create_reversal_voucher')

    current_user_display = user.get_full_name() or user.get_username()
    log_prefix = f"[VchReversal][Co:{company_instance.name}][User:{current_user_display}][OrigVch:{original_voucher_id}]"
    logger.info(f"{log_prefix} Initiating reversal process.")

    try:
        # Ensure reversed_by_voucher is prefetched if you plan to use it in checks.
        original_voucher = Voucher.objects.select_related(
            'company', 'party', 'accounting_period', 'currency'  # Add currency if not already eager loaded
        ).prefetch_related(
            'lines__account', 'reversed_by_voucher'  # For checking if already reversed
        ).get(pk=original_voucher_id, company=company_instance)
    except Voucher.DoesNotExist:
        logger.error(f"{log_prefix} Original voucher not found.")
        raise VoucherWorkflowError(_("Original voucher to reverse not found or invalid for this company."))

    if original_voucher.status != TransactionStatus.POSTED.value:
        logger.warning(
            f"{log_prefix} Original voucher {original_voucher.voucher_number} is not POSTED (Status: {original_voucher.get_status_display()}).")
        raise VoucherWorkflowError(_("Only 'Posted' vouchers can be reversed. Original status: %(s)s") % {
            's': original_voucher.get_status_display()})

    if original_voucher.is_reversed:
        # It's good to provide info about which voucher reversed it, if possible
        rev_vch_num = "Unknown"
        if hasattr(original_voucher, 'reversed_by_voucher') and original_voucher.reversed_by_voucher:
            rev_vch_num = original_voucher.reversed_by_voucher.voucher_number or original_voucher.reversed_by_voucher.pk
        logger.warning(
            f"{log_prefix} Original voucher {original_voucher.voucher_number} already reversed by {rev_vch_num}.")
        raise VoucherWorkflowError(_("Voucher '%(num)s' has already been reversed by voucher '%(rev_vch)s'.") % {
            'num': original_voucher.voucher_number or original_voucher.pk,
            'rev_vch': rev_vch_num
        })

    effective_reversal_date = reversal_date or timezone.now().date()
    try:
        reversal_period = _get_valid_accounting_period(company_instance, effective_reversal_date)
    except DjangoValidationError as e:  # _get_valid_accounting_period raises DjangoValidationError
        logger.error(
            f"{log_prefix} Accounting period validation failed for date {effective_reversal_date}: {e.messages}")
        # Convert to PeriodLockedError or re-raise as appropriate for your API
        if "No open accounting period" in str(e):
            raise PeriodLockedError(
                _("No open accounting period for reversal date %(d)s.") % {'d': effective_reversal_date}) from e
        raise  # Re-raise other DjangoValidationErrors

    # Construct narration for the reversing voucher
    new_narration = f"{reversal_narration_prefix} {original_voucher.voucher_number or original_voucher.pk}"
    if original_voucher.narration:
        new_narration += f" - Orig: {original_voucher.narration[:150]}"
    new_narration = new_narration[:Voucher._meta.get_field('narration').max_length]

    # Prepare lines for the reversing voucher
    reversing_lines_data = []
    for line in original_voucher.lines.all():
        if not line.account or not line.account.is_active or not line.account.allow_direct_posting:
            logger.error(
                f"{log_prefix} Account {line.account.account_code if line.account else 'N/A'} on original voucher is invalid for reversal.")
            raise VoucherWorkflowError(
                _("Cannot reverse: Account '%(acc)s' on original voucher line is inactive, missing, or disallows direct posting.") % {
                    'acc': line.account.account_name if line.account else 'Unknown Account'
                }
            )

        reversing_lines_data.append({
            'account_id': line.account_id,
            'dr_cr': DrCrType.CREDIT.value if line.dr_cr == DrCrType.DEBIT.value else DrCrType.DEBIT.value,
            'amount': line.amount,
            'narration': (_("Reversal - %(orig_narr)s") % {'orig_narr': (line.narration or '')})[
                         :VoucherLine._meta.get_field('narration').max_length]
        })

    if not reversing_lines_data:
        logger.error(f"{log_prefix} Original voucher has no valid lines to reverse.")
        raise VoucherWorkflowError(_("Original voucher has no valid lines to reverse."))

    # Create the new reversing voucher as DRAFT first
    # Assuming original_voucher.currency is the FK object, so original_voucher.currency_id
    # If original_voucher.currency is just the currency code (char), then pass it directly.
    # Check your Voucher model definition for currency.
    # For this example, I'll assume original_voucher.currency is the Currency model instance.
    if not original_voucher.currency:
        logger.error(
            f"{log_prefix} Original voucher {original_voucher.voucher_number} is missing currency information.")
        raise VoucherWorkflowError(_("Original voucher is missing currency information, cannot create reversal."))

    reversing_voucher = create_draft_voucher(
        company_id=company_instance.id,
        created_by_user=user,
        voucher_type_value=reversal_voucher_type_value,
        date=effective_reversal_date,
        narration=new_narration,
        # currency=original_voucher.currency, # If currency is a CharField
        currency_id=original_voucher.currency_id,  # If currency is a ForeignKey to a Currency model
        lines_data=reversing_lines_data,
        party_pk=original_voucher.party_id,
        reference=f"REV-{original_voucher.voucher_number or original_voucher.pk}"[
                  :Voucher._meta.get_field('reference').max_length]
    )
    logger.info(f"{log_prefix} Draft Reversing Voucher {reversing_voucher.pk} created.")

    # Link the reversing voucher to the original (is_reversal_for is set after reversing_voucher has a PK)
    reversing_voucher.is_reversal_for = original_voucher
    # Save this specific field.
    reversing_voucher.save(update_fields=['is_reversal_for'])

    # Log initial creation (as DRAFT)
    _log_approval_action(
        reversing_voucher, user, ApprovalActionType.SYSTEM.value,  # Or a specific "CREATED_REVERSAL_DRAFT"
        TransactionStatus.DRAFT.value, TransactionStatus.DRAFT.value,  # from/to status for draft creation
        _("Reversing voucher (Draft) created for original voucher: %(num)s") % {
            'num': original_voucher.voucher_number or original_voucher.pk}
    )

    if post_immediately:
        logger.info(f"{log_prefix} Attempting to post reversing voucher {reversing_voucher.pk} immediately.")
        # User needs permission to approve and post
        _check_role_permission(user, company_instance, 'approve_voucher')
        _check_role_permission(user, company_instance, 'post_voucher')

        # Assign voucher number (typically done on submission, but needed before posting)
        assign_voucher_number(reversing_voucher, company_instance)  # This will save voucher_number if changed

        # Validate for posting
        _validate_voucher_for_posting(reversing_voucher, company_instance)  # This includes balance checks

        original_status_for_log = reversing_voucher.status  # Should be DRAFT
        current_time = timezone.now()
        reversing_voucher.status = TransactionStatus.POSTED.value
        reversing_voucher.posted_by = user
        reversing_voucher.posted_at = current_time
        reversing_voucher.approved_by = user  # User initiating immediate post is also approver
        reversing_voucher.approved_at = current_time
        reversing_voucher.updated_by = user

        reversing_voucher.save(update_fields=[
            'status', 'voucher_number', 'posted_by', 'posted_at',
            'approved_by', 'approved_at', 'updated_by'
        ])

        _log_approval_action(
            reversing_voucher, user, ApprovalActionType.APPROVED.value,  # Or SYSTEM_POSTED
            original_status_for_log, reversing_voucher.status,
            _("Reversing voucher auto-approved and posted for: %(num)s") % {
                'num': original_voucher.voucher_number or original_voucher.pk}
        )
        logger.info(f"{log_prefix} Reversing Voucher {reversing_voucher.voucher_number} CREATED AND POSTED.")
    else:
        logger.info(
            f"{log_prefix} Reversing Voucher {reversing_voucher.pk} remains in DRAFT status for normal workflow.")

    # Mark original voucher as reversed and link it to the new reversing voucher
    original_voucher.is_reversed = True
    original_voucher.reversed_by_voucher = reversing_voucher  # Link back
    original_voucher.updated_by = user
    original_voucher.updated_at = timezone.now()
    original_voucher.save(update_fields=['is_reversed', 'reversed_by_voucher', 'updated_by', 'updated_at'])
    logger.info(
        f"{log_prefix} Original Voucher {original_voucher.voucher_number} marked as reversed by {reversing_voucher.voucher_number or reversing_voucher.pk}.")

    return reversing_voucher