# crp_accounting/filters.py

import django_filters
from django.db.models import Q # For complex lookups if needed
from .models.journal import Voucher, VoucherType, TransactionStatus, Party, AccountingPeriod

class VoucherFilterSet(django_filters.FilterSet):
    """
    FilterSet for the Voucher model.
    Allows filtering by various fields including date ranges and text search hints.
    """
    # Date range filter
    date_from = django_filters.DateFilter(field_name='date', lookup_expr='gte', label='Date From (YYYY-MM-DD)')
    date_to = django_filters.DateFilter(field_name='date', lookup_expr='lte', label='Date To (YYYY-MM-DD)')
    date_range = django_filters.DateFromToRangeFilter(field_name='date', label='Date Range') # Convenience filter

    # Effective Date range filter
    effective_date_from = django_filters.DateFilter(field_name='effective_date', lookup_expr='gte', label='Effective Date From')
    effective_date_to = django_filters.DateFilter(field_name='effective_date', lookup_expr='lte', label='Effective Date To')

    # Choice filters for status and type
    status = django_filters.ChoiceFilter(choices=TransactionStatus.choices)
    voucher_type = django_filters.ChoiceFilter(choices=VoucherType.choices)

    # Foreign key filters (show dropdown or accept PK)
    party = django_filters.ModelChoiceFilter(queryset=Party.objects.all())
    accounting_period = django_filters.ModelChoiceFilter(queryset=AccountingPeriod.objects.all())

    # Example: Filter by voucher number (exact match or contains)
    voucher_number_exact = django_filters.CharFilter(field_name='voucher_number', lookup_expr='exact', label='Voucher Number (Exact)')
    voucher_number_contains = django_filters.CharFilter(field_name='voucher_number', lookup_expr='icontains', label='Voucher Number (Contains)')

    # Example: Filter by reference contains
    reference_contains = django_filters.CharFilter(field_name='reference', lookup_expr='icontains', label='Reference (Contains)')

    # Example: Filter by narration contains
    narration_contains = django_filters.CharFilter(field_name='narration', lookup_expr='icontains', label='Narration (Contains)')

    class Meta:
        model = Voucher
        fields = [ # Define fields available for filtering via exact match by default
            'status',
            'voucher_type',
            'party',
            'accounting_period',
            # Add other exact match fields if needed
        ]
        # Note: More specific filters defined above override these defaults if names match.