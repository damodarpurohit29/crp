# crp_accounting/templatetags/math_filters.py
from django import template
# import math # if you need it for more complex things

register = template.Library() # <--- THIS IS ESSENTIAL

@register.filter(name='multiply') # 'name' is optional if function name is same as filter name
def multiply(value, arg):
    try:
        num_value = float(value)
        num_arg = float(arg)
        result = num_value * num_arg
        return result
    except (ValueError, TypeError):
        return 0 # Or handle error as approp