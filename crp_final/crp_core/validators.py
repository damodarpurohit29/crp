"""
core/validators.py

Contains reusable validation logic used in models and serializers.
Useful for enforcing business rules and data consistency across the ERP Accounting system.
"""

from django.core.exceptions import ValidationError
from datetime import date
from decimal import Decimal


def validate_fiscal_period(opening_date, closing_date):
    """
    Ensures that the fiscal period is valid.
    """
    if closing_date <= opening_date:
        raise ValidationError("Closing date must be after opening date.")


def validate_balanced_journal_entry(debit_total, credit_total):
    """
    Ensures that a journal entry is balanced (Dr = Cr).
    """
    if round(Decimal(debit_total), 2) != round(Decimal(credit_total), 2):
        raise ValidationError("Journal entry is not balanced. Dr and Cr totals must match.")


def validate_non_future_date(value: date):
    """
    Ensures the provided date is not in the future.
    """
    if value > date.today():
        raise ValidationError("Date cannot be in the future.")


def validate_currency_consistency(entries):
    """
    Validates that all line items in a transaction have the same currency.
    """
    currencies = {entry.currency for entry in entries}
    if len(currencies) > 1:
        raise ValidationError("All entries in a transaction must have the same currency.")


def validate_party_exists(party_id, PartyModel):
    """
    Ensures the referenced party exists in the system.
    """
    if not PartyModel.objects.filter(id=party_id).exists():
        raise ValidationError(f"Party with ID {party_id} does not exist.")


def validate_transaction_date_within_period(transaction_date, period):
    """
    Validates that the transaction date falls within the open period.
    """
    if not (period.start_date <= transaction_date <= period.end_date):
        raise ValidationError("Transaction date must be within the selected period.")


def validate_positive_amount(value):
    """
    Ensures the transaction amount is positive.
    """
    if value <= 0:
        raise ValidationError("Transaction amount must be positive.")
