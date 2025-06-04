# crp_accounting/serializers/balance_sheet.py

import logging
from rest_framework import serializers
from decimal import Decimal

logger = logging.getLogger(__name__)

# =============================================================================
# Balance Sheet Report Serializers (Refined)
# =============================================================================

class BalanceSheetNodeSerializer(serializers.Serializer):
    """
    Serializes a node (Account Group or Account) within the Balance Sheet hierarchy.
    Includes currency information for individual accounts.
    """
    id = serializers.IntegerField(read_only=True, help_text="PK of Account/Group (0 for Retained Earnings).")
    name = serializers.CharField(read_only=True, help_text="Name of Group or Account.")
    type = serializers.ChoiceField(choices=['group', 'account'], read_only=True, help_text="Node type.")
    level = serializers.IntegerField(read_only=True, help_text="Hierarchy depth.")
    balance = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Closing balance for the node. Note: Group totals may aggregate multiple currencies."
    )
    # Currency shown only for account-level nodes
    currency = serializers.CharField(
        read_only=True, required=False, allow_null=True, max_length=10, # Match model field length
        help_text="Currency code of the individual account (null for groups and Retained Earnings)."
    )
    children = serializers.ListField(
        child=serializers.DictField(), # Placeholder for recursion
        read_only=True, help_text="Child nodes (groups/accounts) within this group."
    )

    def get_fields(self):
        """Set child serializer for recursion."""
        fields = super().get_fields()
        fields['children'] = BalanceSheetNodeSerializer(many=True, read_only=True)
        return fields

    class Meta:
        ref_name = "BalanceSheetNode"


class BalanceSheetSectionSerializer(serializers.Serializer):
    """
    Serializes a major section (Assets, Liabilities, Equity).
    Contains the hierarchy and the section total.
    """
    total = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Total balance for this section. Note: Direct sum; may aggregate multiple currencies."
    )
    hierarchy = BalanceSheetNodeSerializer(
        many=True, read_only=True,
        help_text="Hierarchical structure within this section."
    )

    class Meta:
        ref_name = "BalanceSheetSection"


class BalanceSheetResponseSerializer(serializers.Serializer):
    """
    Serializes the complete response for the Balance Sheet report API endpoint.
    """
    # --- ADDED company_id ---
    company_id = serializers.IntegerField(
        read_only=True,
        help_text="ID of the Company this report belongs to."
    )
    as_of_date = serializers.DateField(read_only=True, help_text="Balance Sheet reporting date.")
    report_currency = serializers.CharField(
        read_only=True, max_length=10,
        help_text="Primary currency context assumed for report totals (e.g., 'INR'). Totals are direct sums and may include other currencies without conversion."
    )
    is_balanced = serializers.BooleanField(read_only=True, help_text="True if Assets = Liabilities + Equity.")
    assets = BalanceSheetSectionSerializer(read_only=True, help_text="Assets section details.")
    liabilities = BalanceSheetSectionSerializer(read_only=True, help_text="Liabilities section details.")
    equity = BalanceSheetSectionSerializer(read_only=True, help_text="Equity section details (includes Retained Earnings).")

    class Meta:
        ref_name = "BalanceSheetResponse"

# =============================================================================
# --- End of File ---
# =============================================================================
# # crp_accounting/serializers/balance_sheet.py
#
# import logging
# from rest_framework import serializers
# from decimal import Decimal
#
# logger = logging.getLogger(__name__)
#
# # =============================================================================
# # Balance Sheet Report Serializers
# # =============================================================================
#
# class BalanceSheetNodeSerializer(serializers.Serializer):
#     """
#     Serializes a node (Account Group or Account) within the Balance Sheet hierarchy.
#     Handles recursive nesting for child nodes.
#     """
#     id = serializers.IntegerField(
#         read_only=True,
#         help_text="Primary key of the Account or Account Group (0 for calculated Retained Earnings)."
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
#         help_text="Hierarchy level (depth) of the node, useful for indentation."
#     )
#     balance = serializers.DecimalField(
#         max_digits=20, decimal_places=2,
#         read_only=True,
#         help_text="Calculated closing balance for the node (account balance or group subtotal)."
#     )
#     currency = serializers.CharField(
#         read_only=True,
#         required=False, # Currency is only present for 'account' type nodes
#         allow_null=True, # Matches Optional[str] type hint from service
#         help_text="Currency code of the account (null for groups)."
#     )
#     # Recursive definition for children nodes.
#     children = serializers.ListField(
#         child=serializers.DictField(), # Placeholder, will be replaced in get_fields
#         read_only=True,
#         help_text="List of child nodes (groups or accounts) belonging to this group node."
#     )
#
#     def get_fields(self):
#         """Dynamically set the child serializer for recursion."""
#         fields = super().get_fields()
#         # This correctly sets the child serializer to handle nested nodes
#         fields['children'] = BalanceSheetNodeSerializer(many=True, read_only=True)
#         return fields
#
#     class Meta:
#         ref_name = "BalanceSheetNode" # For OpenAPI schema
#
#
# class BalanceSheetSectionSerializer(serializers.Serializer):
#     """
#     Serializes a major section of the Balance Sheet (Assets, Liabilities, or Equity).
#     Contains the hierarchical structure and the total for the section.
#     """
#     total = serializers.DecimalField(
#         max_digits=20, decimal_places=2,
#         read_only=True,
#         help_text="Total calculated balance for this section."
#     )
#     hierarchy = BalanceSheetNodeSerializer(
#         many=True,
#         read_only=True,
#         help_text="Hierarchical structure of account groups and accounts within this section."
#     )
#
#     class Meta:
#         ref_name = "BalanceSheetSection" # For OpenAPI schema
#
#
# class BalanceSheetResponseSerializer(serializers.Serializer):
#     """
#     Serializes the complete response payload for the Balance Sheet report API endpoint.
#     """
#     as_of_date = serializers.DateField(
#         read_only=True,
#         help_text="The date for which the Balance Sheet position is reported."
#     )
#     report_currency = serializers.CharField(
#         read_only=True,
#         max_length=10,
#         help_text="The primary currency context assumed for report totals (e.g., 'USD'). Note: Aggregation may still involve underlying accounts in multiple currencies."
#     )
#     is_balanced = serializers.BooleanField(
#         read_only=True,
#         help_text="Indicates if Total Assets equals Total Liabilities + Total Equity."
#     )
#     assets = BalanceSheetSectionSerializer(
#         read_only=True,
#         help_text="Details of the Assets section."
#     )
#     liabilities = BalanceSheetSectionSerializer(
#         read_only=True,
#         help_text="Details of the Liabilities section."
#     )
#     equity = BalanceSheetSectionSerializer(
#         read_only=True,
#         help_text="Details of the Equity section (including calculated Retained Earnings)."
#     )
#
#     class Meta:
#         ref_name = "BalanceSheetResponse" # For OpenAPI schema
#
# # =============================================================================
# # --- End of File ---
# # =============================================================================