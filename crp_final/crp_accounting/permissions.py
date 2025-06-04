import logging

from rest_framework import permissions

from crp_core.enums import TransactionStatus

logger = logging.getLogger(__name__)
# from .models.journal import TransactionStatus # Import status enum

class CanViewVoucher(permissions.BasePermission):
    """Allows access only to authenticated users."""
    message = "Authentication required to view vouchers."

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

class CanManageDraftVoucher(permissions.BasePermission):
    """
    Allows creating new vouchers (defaults to Draft).
    Allows updating or deleting vouchers ONLY if they are in Draft or Rejected status.
    Requires standard 'add', 'change', 'delete' model permissions.
    """
    message = "Permission denied to manage this voucher in its current state."

    def has_permission(self, request, view):
        # Check standard model permissions for list/create
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if view.action == 'create':
            return user.has_perm('crp_accounting.add_voucher')
        if view.action == 'list':
            return user.has_perm('crp_accounting.view_voucher')
        # For detail views (retrieve, update, delete), check object permissions
        return True # Let has_object_permission handle detail views

    def has_object_permission(self, request, view, obj):
        user = request.user
        # Always allow viewing if user has basic view permission (checked in has_permission implicitly for list)
        if request.method in permissions.SAFE_METHODS: # GET, HEAD, OPTIONS
            return user.has_perm('crp_accounting.view_voucher')

        # Check 'change' permission for updates
        if request.method in ('PUT', 'PATCH'):
            if not user.has_perm('crp_accounting.change_voucher'):
                 self.message = "You do not have permission to change vouchers."
                 return False
            # Additionally, only allow editing Draft or Rejected
            if obj.status not in [TransactionStatus.DRAFT, TransactionStatus.REJECTED]:
                self.message = f"Cannot edit voucher: Status is '{obj.get_status_display()}'. Only Draft or Rejected can be edited."
                return False
            return True # Has change perm and status is editable

        # Check specific delete permission for deletion
        if request.method == 'DELETE':
            if not user.has_perm('crp_accounting.delete_draft_voucher'):
                 self.message = "You do not have permission to delete vouchers."
                 return False
            # Additionally, only allow deleting Draft or Rejected
            if obj.status not in [TransactionStatus.DRAFT, TransactionStatus.REJECTED]:
                self.message = f"Cannot delete voucher: Status is '{obj.get_status_display()}'. Only Draft or Rejected can be deleted."
                return False
            return True # Has delete perm and status is deletable

        return False # Deny other methods by default

class CanSubmitVoucher(permissions.BasePermission):
    message = "Permission denied or voucher not in correct state for submission."

    def has_object_permission(self, request, view, obj):
        user = request.user
        perm_code = 'crp_accounting.submit_voucher' # Define for clarity

        # --- Add Logging ---
        has_perm_result = user.has_perm(perm_code)
        logger.debug(
            f"[Permission Check] User: {user}, Auth: {request.auth}, "
            f"Action: Submit, Voucher PK: {obj.pk}, Status: {obj.status}, "
            f"Required Perm: '{perm_code}', Has Perm?: {has_perm_result}"
        )
        # --- End Logging ---

        return (
            has_perm_result and
            obj.status == TransactionStatus.DRAFT
        )

class CanApproveVoucher(permissions.BasePermission):
    message = "Permission denied or voucher not in correct state for approval."

    def has_object_permission(self, request, view, obj):
        user = request.user
        perm_code = 'crp_accounting.approve_voucher' # Define for clarity
        allowed_statuses = [TransactionStatus.PENDING_APPROVAL, TransactionStatus.REJECTED]

        # --- Add Logging ---
        has_perm_result = user.has_perm(perm_code)
        logger.debug(
            f"[Permission Check] User: {user}, Auth: {request.auth}, "
            f"Action: Approve, Voucher PK: {obj.pk}, Status: {obj.status}, "
            f"Required Perm: '{perm_code}', Has Perm?: {has_perm_result}"
        )
        # --- End Logging ---

        return (
            has_perm_result and
            obj.status in allowed_statuses)
class CanRejectVoucher(permissions.BasePermission):
    """Allows rejecting ONLY if user has 'reject_voucher' perm AND status is PENDING_APPROVAL."""
    message = "Permission denied or voucher not in correct state for rejection."

    def has_object_permission(self, request, view, obj):
        return (
            request.user.has_perm('crp_accounting.reject_voucher') and
            obj.status == TransactionStatus.PENDING_APPROVAL
        )

class CanReverseVoucher(permissions.BasePermission):
    """Allows reversing ONLY if user has 'reverse_voucher' perm AND status is POSTED."""
    message = "Permission denied or voucher not in correct state for reversal."

    def has_object_permission(self, request, view, obj):
        return (
            request.user.has_perm('crp_accounting.reverse_voucher') and
            obj.status == TransactionStatus.POSTED
        )

class CanViewFinancialReports(permissions.BasePermission):
    """
    Allows access only to users with the 'view_financial_reports' permission.
    """
    message = "You do not have permission to view financial reports."

    def has_permission(self, request, view):
        # Check if user is authenticated AND has the specific permission
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.has_perm('crp_accounting.view_financial_reports') # Adjust app_label if needed
        )