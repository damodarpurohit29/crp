# crp_accounting/templatetags/report_tags.py
from django import template
from decimal import Decimal

register = template.Library()

@register.filter(name='currency_format')
def currency_format(value, currency_symbol=""):
    """
    Formats a Decimal value as currency.
    Usage: {{ some_decimal_value|currency_format:"$" }}
    """
    if value is None:
        return ""
    try:
        # Ensure it's a Decimal for consistent formatting
        val = Decimal(value)
        # Example: 2 decimal places, with currency symbol
        # You might want more sophisticated formatting based on locale
        return f"{currency_symbol}{val:,.2f}"
    except (TypeError, ValueError):
        return value # Return original value if conversion fails

from django import template

register = template.Library()

@register.filter(name='get_item')  # Registering the filter with the name 'get_item'
def get_item(dictionary, key):
    """
    Allows accessing dictionary items with a variable key in templates.
    Usage: {{ my_dictionary|get_item:variable_key }}
    or     {{ my_dictionary|get_item:"string_key" }}
    """
    if hasattr(dictionary, 'get'):
        return dictionary.get(key)
    return None # Or raise an error, or return a d