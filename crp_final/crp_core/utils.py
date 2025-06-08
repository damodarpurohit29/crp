"""
Utility functions used throughout the accounting system.

Why this file exists:
This module centralizes reusable logic such as currency rounding,
date handling, fiscal calculations, and validation helpers.
Instead of rewriting logic in views/serializers/models, we abstract it here
to follow DRY (Don't Repeat Yourself) and enhance maintainability.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional


def today() -> date:
    """
    Returns today's date. Useful for mocking/testing.
    """
    return date.today()


def round_decimal(value: Decimal, precision: str = '0.01') -> Decimal:
    """
    Rounds a Decimal to given precision using ROUND_HALF_UP method.

    Args:
        value (Decimal): The decimal number to round.
        precision (str): The decimal precision (default: 2 places).

    Returns:
        Decimal: Rounded decimal.
    """
    return value.quantize(Decimal(precision), rounding=ROUND_HALF_UP)


def to_decimal(value) -> Decimal:
    """
    Safely converts a number to Decimal for financial calculations.

    Args:
        value: Float, int or str value.

    Returns:
        Decimal: Converted Decimal value.
    """
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (ValueError, TypeError, InvalidOperation):
        return Decimal('0.00')


def calculate_balance(debits: Decimal, credits: Decimal) -> Decimal:
    """
    Calculates the net balance from debits and credits.

    Args:
        debits (Decimal): Total debits.
        credits (Decimal): Total credits.

    Returns:
        Decimal: Net balance (positive for Dr, negative for Cr).
    """
    return round_decimal(debits - credits)


def is_within_date_range(start: date, end: date, check_date: Optional[date] = None) -> bool:
    """
    Checks if a given date falls within a date range.

    Args:
        start (date): Start date.
        end (date): End date.
        check_date (Optional[date]): Date to check. Defaults to today.

    Returns:
        bool: True if within range, else False.
    """
    if not check_date:
        check_date = today()
    return start <= check_date <= end


def get_fiscal_year_from_date(input_date: date) -> str:
    """
    Determines fiscal year (e.g., 2023-2024) from a given date.

    Args:
        input_date (date): Date to determine FY from.

    Returns:
        str: Fiscal year in format "YYYY-YYYY"
    """
    year = input_date.year
    if input_date.month < 4:
        return f"{year - 1}-{year}"
    return f"{year}-{year + 1}"


def get_query_param(request, key: str, default=None):
    """
    Safely retrieves a query parameter from a DRF request.

    Args:
        request (HttpRequest): DRF request object.
        key (str): Parameter key.
        default: Default value if key doesn't exist.

    Returns:
        str: Value or default.
    """
    return request.query_params.get(key, default)
