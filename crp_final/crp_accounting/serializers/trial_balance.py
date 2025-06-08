# crp_accounting/serializers/trial_balance.py

import logging
from rest_framework import serializers
from decimal import Decimal

logger = logging.getLogger(__name__)

# =============================================================================
# Trial Balance Serializers (Refined for Clarity/Context)
# =============================================================================

class TrialBalanceFlatEntrySerializer(serializers.Serializer):
    """
    Serializes a single account line for the *flat list* view of the Trial Balance.
    Represents the final debit or credit balance for an active account as of the report date.
    """
    account_pk = serializers.IntegerField(read_only=True, help_text="Primary key of the Account.")
    account_number = serializers.CharField(read_only=True, help_text="Unique account number within the company.")
    account_name = serializers.CharField(read_only=True, help_text="Name of the account.")
    # Note: Standard Trial Balance typically sums amounts directly, regardless of
    # underlying currency, unless specifically designed for multi-currency conversion.
    # We will not add currency here unless the service explicitly provides it
    # and the business requirement dictates showing it per line.
    debit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Calculated closing debit balance as of the report date."
    )
    credit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Calculated closing credit balance as of the report date."
    )

    class Meta:
        ref_name = "TrialBalanceFlatEntry"


class TrialBalanceHierarchyNodeSerializer(serializers.Serializer):
    """
    Serializes a node (Account Group or Account) in the hierarchical Trial Balance view.
    Includes subtotals for groups and individual account balances.
    """
    id = serializers.IntegerField(read_only=True, help_text="Primary key of the Account or Account Group.")
    name = serializers.CharField(read_only=True, help_text="Name (Group or Account Number - Name).")
    type = serializers.ChoiceField(choices=['group', 'account'], read_only=True, help_text="Node type.")
    level = serializers.IntegerField(read_only=True, help_text="Hierarchy level (depth) for indentation.")
    debit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Debit total for the node (account balance or group subtotal)."
    )
    credit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Credit total for the node (account balance or group subtotal)."
    )
    # Recursive field for children (handled by get_fields)
    children = serializers.ListField(
        child=serializers.DictField(), # Placeholder, replaced in get_fields
        read_only=True,
        help_text="List of child nodes belonging to this group node (empty for accounts)."
    )

    def get_fields(self):
        """Dynamically set the child serializer for recursion."""
        fields = super().get_fields()
        fields['children'] = TrialBalanceHierarchyNodeSerializer(many=True, read_only=True)
        return fields

    class Meta:
        ref_name = "TrialBalanceHierarchyNode"


class TrialBalanceStructuredResponseSerializer(serializers.Serializer):
    """
    Serializes the complete response for the structured Trial Balance report.
    Includes company context, report date, totals, and both hierarchical and flat views.
    """
    # --- ADDED company_id for tenant context ---
    company_id = serializers.IntegerField(
        read_only=True,
        help_text="ID of the Company this report belongs to."
    )
    as_of_date = serializers.DateField(
        read_only=True,
        help_text="The date for which the Trial Balance was generated."
    )
    total_debit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Grand total of all debit balances."
    )
    total_credit = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Grand total of all credit balances."
    )
    is_balanced = serializers.BooleanField(
        read_only=True,
        help_text="True if total debit equals total credit."
    )
    # Hierarchical structure
    hierarchy = TrialBalanceHierarchyNodeSerializer(
        many=True, read_only=True,
        help_text="Hierarchical structure of account groups and accounts with subtotals."
    )
    # Flat list structure
    flat_entries = TrialBalanceFlatEntrySerializer(
        many=True, read_only=True,
        help_text="Flat list of all active accounts within the company with their calculated balances."
    )

    class Meta:
        ref_name = "TrialBalanceStructuredResponse"
# # crp_accounting/serializers/trial_balance.py
#
# import logging
# from rest_framework import serializers
# from decimal import Decimal # Required for DecimalField
#
# logger = logging.getLogger(__name__)
#
# # =============================================================================
# # Trial Balance Serializers (for Structured Report)
# # =============================================================================
#
# class TrialBalanceFlatEntrySerializer(serializers.Serializer):
#     """
#     Serializes a single account line for the *flat list* within the Trial Balance response.
#     Provides a simple tabular view of account balances.
#     """
#     account_pk = serializers.IntegerField(
#         read_only=True,
#         help_text="Primary key of the Account."
#     )
#     account_number = serializers.CharField(
#         read_only=True,
#         help_text="Unique account number."
#     )
#     account_name = serializers.CharField(
#         read_only=True,
#         help_text="Name of the account."
#     )
#     debit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Ensure precision matches models/settings
#         read_only=True,
#         help_text="Calculated debit balance for the account as of the report date."
#     )
#     credit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Ensure precision matches models/settings
#         read_only=True,
#         help_text="Calculated credit balance for the account as of the report date."
#     )
#
#     class Meta:
#         # Helps with schema generation if needed, though not strictly required for Serializer
#         ref_name = "TrialBalanceFlatEntry"
#
#
# class TrialBalanceHierarchyNodeSerializer(serializers.Serializer):
#     """
#     Serializes a node (representing either an Account Group or an Account)
#     within the hierarchical structure of the Trial Balance response.
#     Supports recursive nesting for child nodes.
#     """
#     id = serializers.IntegerField(
#         read_only=True,
#         help_text="Primary key of the Account or Account Group."
#     )
#     name = serializers.CharField(
#         read_only=True,
#         help_text="Name of the Account Group or combined number/name for an Account."
#     )
#     type = serializers.ChoiceField(
#         choices=['group', 'account'],
#         read_only=True,
#         help_text="Indicates whether the node is a group or an account."
#     )
#     level = serializers.IntegerField(
#         read_only=True,
#         help_text="Hierarchy level (depth) of the node, used for indentation."
#     )
#     debit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Match precision
#         read_only=True,
#         help_text="Calculated debit total for the node (account balance or group subtotal)."
#     )
#     credit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Match precision
#         read_only=True,
#         help_text="Calculated credit total for the node (account balance or group subtotal)."
#     )
#     # Recursive field definition for children nodes.
#     # This allows the serializer to handle nested structures of arbitrary depth.
#     children = serializers.ListField(
#         # CORRECT: Initialize without a child, it will be set in get_fields
#         read_only=True,
#         help_text="List of child nodes (groups or accounts) belonging to this group node."
#     )
#
#     # ... get_fields method remains the same ...
#     def get_fields(self):
#         fields = super().get_fields()
#         # This line correctly sets the child serializer
#         fields['children'] = TrialBalanceHierarchyNodeSerializer(many=True, read_only=True)
#         return fields
#
#     class Meta:
#         # Helps with schema generation if needed
#         ref_name = "TrialBalanceHierarchyNode"
#
#
# class TrialBalanceStructuredResponseSerializer(serializers.Serializer):
#     """
#     Serializes the complete response payload for the structured Trial Balance report API endpoint.
#     Includes summary totals, the hierarchical view, and the flat list view.
#     """
#     as_of_date = serializers.DateField(
#         read_only=True,
#         help_text="The date for which the Trial Balance was generated."
#     )
#     total_debit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Match precision
#         read_only=True,
#         help_text="Grand total of all debit balances in the report."
#     )
#     total_credit = serializers.DecimalField(
#         max_digits=20, decimal_places=2, # Match precision
#         read_only=True,
#         help_text="Grand total of all credit balances in the report."
#     )
#     is_balanced = serializers.BooleanField(
#         read_only=True,
#         help_text="Indicates if the grand total debit equals the grand total credit."
#     )
#     # Include both the hierarchical structure and the flat list
#     hierarchy = TrialBalanceHierarchyNodeSerializer(
#         many=True,
#         read_only=True,
#         help_text="Hierarchical structure of account groups and accounts with subtotals."
#     )
#     flat_entries = TrialBalanceFlatEntrySerializer(
#         many=True,
#         read_only=True,
#         help_text="Flat list of all active accounts with their calculated debit/credit balances."
#     )
#
#     class Meta:
#         # Helps with schema generation if needed
#         ref_name = "TrialBalanceStructuredResponse"