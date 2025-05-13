"""
core/exceptions.py

Contains custom exception classes for domain-specific error handling in the ERP Accounting system.
These exceptions help centralize error messaging and improve readability/consistency in the codebase.
"""

from rest_framework.exceptions import APIException
from rest_framework import status


class FiscalYearClosedException(APIException):
    """
    Raised when a transaction is attempted in a closed fiscal year.
    """
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'This fiscal year is closed. No further transactions are allowed.'
    default_code = 'fiscal_year_closed'


class InvalidJournalEntryException(APIException):
    """
    Raised when a journal entry is unbalanced or structurally incorrect.
    """
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = 'Journal entry is invalid. Debits and credits must match.'
    default_code = 'invalid_journal_entry'


class DuplicateAccountCodeException(APIException):
    """
    Raised when an attempt is made to create a COA account with an existing code.
    """
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Account with this code already exists.'
    default_code = 'duplicate_account_code'


class InvalidAccountTypeOperationException(APIException):
    """
    Raised when an operation is attempted on an account type that does not support it.
    E.g., trying to post to a summary/control account.
    """
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Operation not allowed for this account type.'
    default_code = 'invalid_account_type'


class TransactionPeriodMismatchException(APIException):
    """
    Raised when the date of a transaction doesn't fall within the selected fiscal period.
    """
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Transaction date does not match the open fiscal period.'
    default_code = 'period_date_mismatch'


class CurrencyMismatchException(APIException):
    """
    Raised when a transaction contains entries with multiple currencies unexpectedly.
    """
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Currency mismatch detected in transaction.'
    default_code = 'currency_mismatch'


class UnauthorizedActionException(APIException):
    """
    Raised when a user attempts an action they are not permitted to perform.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = 'You are not authorized to perform this action.'
    default_code = 'unauthorized'
