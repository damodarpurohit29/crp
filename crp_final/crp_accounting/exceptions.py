"""
Custom exceptions for the CRP Accounting application, particularly for
voucher workflow and validation logic.
"""

from django.utils.translation import gettext_lazy as _
from django.core.exceptions import PermissionDenied # For permission errors

class VoucherWorkflowError(Exception):
    """
    Base exception for errors specifically related to the voucher processing workflow.
    Allows catching all workflow-specific issues easily.
    """
    default_message = _("An error occurred during the voucher workflow.")
    code = 'workflow_error'

    def __init__(self, message=None, code=None):
        self.message = str(message or self.default_message) # Ensure message is a string
        self.code = code or self.code
        super().__init__(self.message)

class InvalidVoucherStatusError(VoucherWorkflowError):
    """
    Raised when a workflow operation is attempted on a voucher
    that is not in an appropriate status for that operation.
    """
    default_message = _("Operation invalid for the current voucher status.")
    code = 'invalid_status'

    def __init__(self, current_status, expected_statuses=None, message=None):
        self.current_status = current_status
        self.expected_statuses = expected_statuses or []
        if not message:
            expected_display = ', '.join(f"'{s.label if hasattr(s,'label') else s}'" for s in self.expected_statuses)
            status_display = f"'{current_status.label if hasattr(current_status,'label') else current_status}'"

            if self.expected_statuses:
                message = _("Operation invalid for status %(current)s. Expected one of: %(expected)s.") % {
                    'current': status_display,
                    'expected': expected_display
                }
            else:
                message = _("Operation invalid for status %(current)s.") % {'current': status_display}
        super().__init__(message=message, code=self.code)


class PeriodLockedError(VoucherWorkflowError):
    """
    Raised when an operation attempts to modify data within an
    accounting period that has been marked as locked/closed.
    """
    default_message = _("The accounting period is locked.")
    code = 'period_locked'

    def __init__(self, period_name, message=None):
        self.period_name = period_name
        if not message:
            message = _("Operation failed: Accounting Period '%(period_name)s' is locked.") % {'period_name': self.period_name}
        super().__init__(message=message, code=self.code)

class BalanceError(VoucherWorkflowError):
    """
    Raised when a voucher fails the balance validation check
    (Debits != Credits or Total is zero).
    """
    default_message = _("Voucher debits and credits do not balance or the total is zero.")
    code = 'unbalanced'

    def __init__(self, message=None):
        super().__init__(message=message or self.default_message, code=self.code)


class InsufficientPermissionError(PermissionDenied):
    """
    Custom permission error, inheriting from Django's PermissionDenied
    for compatibility with DRF/Django's standard handling.
    Used when a user lacks the necessary rights for a specific voucher action.
    """
    default_detail = _("You do not have permission to perform this action.") # Use default_detail for DRF compatibility
    status_code = 403 # HTTP Forbidden status

    def __init__(self, detail=None, code='permission_denied'):
        # We are intentionally overriding PermissionDenied's __init__ structure slightly
        # to match our other exceptions, but keeping default_detail for DRF.
        self.detail = detail or self.default_detail # Keep `detail` for DRF
        self.code = code
        # Call Exception's init directly if we don't need PermissionDenied's specific init logic
        Exception.__init__(self, self.detail)


class ReportGenerationError(Exception):
    """Custom exception for errors encountered during report generation."""
    # You can add custom __init__ or other methods if needed
    pass