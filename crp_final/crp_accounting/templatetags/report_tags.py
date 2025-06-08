# crp_accounting/templatetags/report_tags.py

from django import template
from decimal import Decimal

# You only need to create the Library instance once per file.
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
        # 2 decimal places, with currency symbol and commas for thousands
        return f"{currency_symbol}{val:,.2f}"
    except (TypeError, ValueError):
        return value # Return original value if conversion fails

@register.filter(name='get_item')
def get_item(dictionary, key):
    """
    Allows accessing dictionary items with a variable key in templates.
    Usage: {{ my_dictionary|get_item:variable_key }}
    or     {{ my_dictionary|get_item:"string_key" }}
    """
    # The hasattr check is a good practice to prevent errors if the passed
    # object is not a dictionary.
    if hasattr(dictionary, 'get'):
        return dictionary.get(key)
    return None # Return None if the object is not a dictionary or key doesn't exist