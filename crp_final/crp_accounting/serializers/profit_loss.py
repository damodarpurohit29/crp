# crp_accounting/serializers/profit_loss.py

import logging
from rest_framework import serializers
from decimal import Decimal

logger = logging.getLogger(__name__)

# =============================================================================
# Profit & Loss Report Serializers (Refined)
# =============================================================================

class ProfitLossAccountDetailSerializer(serializers.Serializer):
    """
    Serializes the details of a single account contributing to a P&L line item.
    Indicates the movement amount in the account's specific currency.
    """
    account_pk = serializers.IntegerField(read_only=True, help_text="Primary key of the Account.")
    account_number = serializers.CharField(read_only=True, help_text="Unique account number within the company.")
    account_name = serializers.CharField(read_only=True, help_text="Name of the account.")
    amount = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Net movement amount in this account during the period (in account's currency)."
    )
    currency = serializers.CharField(
        read_only=True, max_length=10,
        help_text="Currency code of this specific account (e.g., 'USD', 'INR')."
    )

    class Meta:
        ref_name = "ProfitLossAccountDetail"


class ProfitLossLineItemSerializer(serializers.Serializer):
    """
    Serializes a single line item (section or subtotal) in the Profit & Loss report.
    """
    section_key = serializers.CharField(read_only=True, help_text="Identifier key for the section (e.g., 'REVENUE', 'GROSS_PROFIT').")
    title = serializers.CharField(read_only=True, help_text="Human-readable title for the P&L line.")
    amount = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Calculated total amount for this line. Note: This is a direct sum; may aggregate multiple currencies if underlying accounts differ."
    )
    is_subtotal = serializers.BooleanField(read_only=True, help_text="True if this line is a calculated subtotal.")
    accounts = ProfitLossAccountDetailSerializer(
        many=True, read_only=True, required=False, allow_null=True,
        help_text="List of individual accounts contributing to this section (null for subtotals)."
    )

    class Meta:
        ref_name = "ProfitLossLineItem"


class ProfitLossResponseSerializer(serializers.Serializer):
    """
    Serializes the complete response payload for the Profit & Loss report.
    """
    # --- ADDED company_id ---
    company_id = serializers.IntegerField(
        read_only=True,
        help_text="ID of the Company this report belongs to."
    )
    start_date = serializers.DateField(read_only=True, help_text="The start date of the reporting period.")
    end_date = serializers.DateField(read_only=True, help_text="The end date of the reporting period.")
    report_currency = serializers.CharField(
        read_only=True, max_length=10,
        help_text="The primary currency context assumed for report totals (e.g., 'INR'). Totals are direct sums and may include other currencies without conversion."
    )
    net_income = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True,
        help_text="Final Net Income/(Loss) for the period. Note: Direct sum; may aggregate multiple currencies."
    )
    report_lines = ProfitLossLineItemSerializer(
        many=True, read_only=True,
        help_text="Structured list of P&L lines: sections, account details (with currency), and subtotals."
    )

    class Meta:
        ref_name = "ProfitLossResponse"

# =============================================================================
# --- End of File ---
# =============================================================================
# # crp_accounting/serializers/profit_loss.py
#
# import logging
# from rest_framework import serializers
# from decimal import Decimal
#
# logger = logging.getLogger(__name__)
#
# # =============================================================================
# # Profit & Loss Report Serializers
# # =============================================================================
#
# class ProfitLossAccountDetailSerializer(serializers.Serializer):
#     """
#     Serializes the details of a single account contributing to a P&L line item.
#     Indicates the movement amount in the account's specific currency.
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
#     amount = serializers.DecimalField(
#         max_digits=20, decimal_places=2,
#         read_only=True,
#         help_text="Net movement amount contributed by this account during the period (in the account's currency)."
#     )
#     currency = serializers.CharField( # Added currency field
#         read_only=True,
#         max_length=10, # Should match the 'currency' field length in the Account model
#         help_text="Currency code of the account (e.g., 'USD', 'EUR')."
#     )
#
#     class Meta:
#         # ref_name helps drf-spectacular generate cleaner OpenAPI schema names
#         ref_name = "ProfitLossAccountDetail"
#
#
# class ProfitLossLineItemSerializer(serializers.Serializer):
#     """
#     Serializes a single line item (section or subtotal) in the Profit & Loss report.
#     Includes details of contributing accounts (with their currencies) for non-subtotal lines.
#     """
#     section_key = serializers.CharField(
#         read_only=True,
#         help_text="Identifier key for the section (e.g., 'REVENUE', 'COGS', 'GROSS_PROFIT')."
#     )
#     title = serializers.CharField(
#         read_only=True,
#         help_text="Human-readable title for the P&L line (e.g., 'Revenue', 'Gross Profit')."
#     )
#     amount = serializers.DecimalField(
#         max_digits=20, decimal_places=2,
#         read_only=True,
#         help_text="Calculated total amount for this line item. WARNING: May be an aggregation of multiple currencies if underlying accounts differ."
#     )
#     is_subtotal = serializers.BooleanField(
#         read_only=True,
#         help_text="True if this line represents a calculated subtotal, False otherwise."
#     )
#     # Nested serializer for account details - now includes currency
#     accounts = ProfitLossAccountDetailSerializer(
#         many=True,
#         read_only=True,
#         required=False, # Not present for subtotal lines
#         allow_null=True, # Matches Optional[...] type hint from service
#         help_text="List of individual accounts contributing to this line item (present only for non-subtotals)."
#     )
#
#     class Meta:
#         ref_name = "ProfitLossLineItem"
#
#
# class ProfitLossResponseSerializer(serializers.Serializer):
#     """
#     Serializes the complete response payload for the Profit & Loss report API endpoint.
#     """
#     start_date = serializers.DateField(
#         read_only=True,
#         help_text="The start date of the reporting period (inclusive)."
#     )
#     end_date = serializers.DateField(
#         read_only=True,
#         help_text="The end date of the reporting period (inclusive)."
#     )
#     report_currency = serializers.CharField( # Added report_currency field
#         read_only=True,
#         max_length=10,
#         help_text="The primary currency context assumed for report totals (e.g., 'USD'). Note: Aggregation may still involve underlying accounts in multiple currencies."
#     )
#     net_income = serializers.DecimalField(
#         max_digits=20, decimal_places=2,
#         read_only=True,
#         help_text="The final calculated Net Income / (Loss) for the period. WARNING: May be an aggregation of multiple currencies."
#     )
#     # The list of P&L lines, using the updated ProfitLossLineItemSerializer
#     report_lines = ProfitLossLineItemSerializer(
#         many=True,
#         read_only=True,
#         help_text="Structured list of Profit & Loss lines, including sections, account details (with currency), and subtotals."
#     )
#
#     class Meta:
#         ref_name = "ProfitLossResponse"
#
# # =============================================================================
# # --- End of File ---
# # =============================================================================