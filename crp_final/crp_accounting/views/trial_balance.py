# crp_accounting/views/trial_balance.py (or reports/views.py)

import logging
from datetime import date
from decimal import Decimal
import copy # Keep for hierarchy filtering
from typing import Dict, List # Keep for type hints

# --- Django/DRF Imports ---
from django.utils.translation import gettext_lazy as _
from django.http import Http404 # Keep for potential future use, although mixin raises PermissionDenied
from rest_framework.response import Response
from rest_framework import status
# Permissions might be set on mixin or overridden here
from rest_framework import permissions
from rest_framework.exceptions import ParseError, PermissionDenied

# --- Local Imports ---
# Service needs to be tenant-aware
from ..services import reports_service
# Serializer needs to handle tenant-aware response format
from ..serializers.trial_balance import TrialBalanceStructuredResponseSerializer

# --- Import the Tenant-Aware Mixin ---
# Adjust path based on where you placed the mixin file
try:
    from crp_core.mixins import CompanyScopedAPIViewMixin
except ImportError:
    raise ImportError("Could not import CompanyScopedAPIViewMixin from crp_core.mixins. Ensure it exists.")

# --- Optional RBAC Permission Import ---
# Replace with your actual RBAC permission class checking roles for viewing reports
# from ..permissions import CanViewFinancialReportsRBAC

# --- Swagger/Spectacular Imports ---
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse

logger = logging.getLogger(__name__)
ZERO_DECIMAL = Decimal('0.00') # Keep zero constant

# =============================================================================
# Trial Balance View (Tenant Aware - FINAL)
# =============================================================================

# --- Helper function for filtering hierarchy (Keep as is) ---
def _filter_hierarchy_for_zero_balance(nodes: List[Dict]) -> List[Dict]:
    # ... (implementation remains the same) ...
    filtered_nodes = []
    for node in nodes:
        if node['type'] == 'account':
            if node['debit'] != ZERO_DECIMAL or node['credit'] != ZERO_DECIMAL:
                filtered_nodes.append(copy.deepcopy(node))
        elif node['type'] == 'group':
            filtered_children = _filter_hierarchy_for_zero_balance(node.get('children', []))
            if filtered_children:
                group_copy = copy.deepcopy(node)
                group_copy['children'] = filtered_children
                filtered_nodes.append(group_copy)
    return filtered_nodes


@extend_schema(
    summary="Generate Trial Balance Report (Company Scoped)", # Updated Summary
    description="""Generates a structured Trial Balance report for the user's company as of a specific date.

Requires authentication and appropriate role/permission to view financial reports.

Includes all active accounts within the company by default. Use the 'include_zero_balance' query parameter
set to 'false' or '0' to exclude accounts with a zero balance on the report date.
    """,
    parameters=[
        OpenApiParameter(name='as_of_date', description='Required. Report date (YYYY-MM-DD).', required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
        OpenApiParameter(name='include_zero_balance', description="Optional. Exclude zero-balance accounts ('false' or '0'). Defaults to true.", required=False, type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY),
        # Optional currency parameter
    ],
    responses={
        200: TrialBalanceStructuredResponseSerializer,
        400: OpenApiResponse(description="Bad Request - Invalid/missing parameters."),
        403: OpenApiResponse(description="Forbidden - Insufficient permissions or company context error."), # Mixin raises 403
        500: OpenApiResponse(description="Internal Server Error."),
    },
    tags=['Reports']
)
# --- Inherit from CompanyScopedAPIViewMixin ---
class TrialBalanceView(CompanyScopedAPIViewMixin):
    """
    Provides the Trial Balance report for the user's company context.
    Inherits authentication and company checks from CompanyScopedAPIViewMixin.
    Requires appropriate RBAC permissions for viewing reports.
    """
    # --- Optional: Define Specific RBAC Permissions ---
    # Override permission_classes from the mixin if needed
    # permission_classes = [permissions.IsAuthenticated, CanViewFinancialReportsRBAC] # Example

    def get(self, request, *args, **kwargs):
        """Handles GET requests to generate and return the Trial Balance."""
        # --- Company Context is available via self.company (from Mixin) ---
        company_id = self.company.id # Get the ID for service call & logging

        # --- 1. Input Validation ---
        date_str = request.query_params.get('as_of_date')
        include_zero_str = request.query_params.get('include_zero_balance', 'true').lower()

        if not date_str: raise ParseError(detail=_("'as_of_date' (YYYY-MM-DD) required."))
        try: as_of_date = date.fromisoformat(date_str)
        except ValueError: raise ParseError(detail=_("Invalid 'as_of_date' format. Use YYYY-MM-DD."))
        # Optional future date check
        # if as_of_date > date.today(): raise ParseError(detail=_("Report date cannot be in the future."))

        include_zero_balance = include_zero_str not in ['false', '0']

        # --- 2. Service Layer Call (Pass Company ID) ---
        try:
            # Call the tenant-aware service function
            report_data = reports_service.generate_trial_balance_structured(
                company_id=company_id, # <<< Pass Company ID
                as_of_date=as_of_date
            )

            if not report_data.get('is_balanced', False):
                 logger.critical(f"COMPANY {company_id} TB OUT OF BALANCE! Date: {as_of_date}. Investigation REQUIRED!")

        except Exception as e: # Catch broad errors from service
            logger.exception(f"Error generating Trial Balance for Co {company_id}, Date {as_of_date}: {e}")
            # Re-raise for DRF's 500 handling
            raise

        # --- 3. Optional Filtering (Post-Processing - Logic Unchanged) ---
        if not include_zero_balance:
            logger.debug(f"Filtering zero balance accounts for TB Co {company_id}, Date {as_of_date}")
            report_data['flat_entries'] = [e for e in report_data['flat_entries'] if e['debit'] != ZERO_DECIMAL or e['credit'] != ZERO_DECIMAL]
            report_data['hierarchy'] = _filter_hierarchy_for_zero_balance(report_data['hierarchy'])

        # --- 4. Serialization and Response ---
        # Pass context (including company) to serializer if it needs it
        context = {'request': request, 'company': self.company}
        # Use the updated serializer which expects company_id in response data
        serializer = TrialBalanceStructuredResponseSerializer(report_data, context=context)
        return Response(serializer.data, status=status.HTTP_200_OK)
# import logging
# from datetime import date
# from decimal import Decimal # Needed for ZERO_DECIMAL comparison
# import copy # Needed for deep copying hierarchy for filtering
# from typing import Dict, List
#
# # Django & DRF Imports
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ObjectDoesNotExist
#
# from rest_framework.views import APIView
# from rest_framework.response import Response
# from rest_framework import status, permissions, authentication
# from rest_framework.exceptions import ParseError
#
# # --- Local Imports ---
# from ..services import reports_service
# from ..serializers.trial_balance import TrialBalanceStructuredResponseSerializer
# from ..permissions import CanViewFinancialReports
#
# # --- Swagger/Spectacular Imports ---
# from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse
#
# logger = logging.getLogger(__name__)
# ZERO_DECIMAL = Decimal('0.00') # Define zero once
#
# # =============================================================================
# # Report Views
# # =============================================================================
#
# # --- Helper function for filtering hierarchy ---
# def _filter_hierarchy_for_zero_balance(nodes: List[Dict]) -> List[Dict]:
#     """
#     Recursively filters a hierarchy tree to remove zero-balance accounts
#     and any groups that become empty after filtering.
#     Operates on a deep copy to avoid modifying the original structure.
#     """
#     filtered_nodes = []
#     for node in nodes:
#         if node['type'] == 'account':
#             # Keep account only if balance is non-zero
#             if node['debit'] != ZERO_DECIMAL or node['credit'] != ZERO_DECIMAL:
#                 filtered_nodes.append(copy.deepcopy(node)) # Add copy
#         elif node['type'] == 'group':
#             # Recursively filter children first
#             filtered_children = _filter_hierarchy_for_zero_balance(node.get('children', []))
#             # Keep group only if it STILL has children after filtering OR its own balance was non-zero
#             # Note: Group balance check isn't strictly needed if it only sums children,
#             # but kept for robustness if group logic changes. Check children is key.
#             if filtered_children: # Keep group if it has non-zero children remaining
#                 group_copy = copy.deepcopy(node)
#                 group_copy['children'] = filtered_children
#                 # Recalculate group totals based on filtered children? Optional, can be complex.
#                 # For simplicity, we often keep the original group totals even if children are filtered.
#                 # Or, recalculate here:
#                 # group_copy['debit'] = sum(c['debit'] for c in filtered_children)
#                 # group_copy['credit'] = sum(c['credit'] for c in filtered_children)
#                 filtered_nodes.append(group_copy)
#     return filtered_nodes
#
#
# @extend_schema(
#     summary="Generate Trial Balance Report",
#     description="""Generates a structured Trial Balance report as of a specific date.
#
# Requires 'view_financial_reports' permission.
#
# Includes all active accounts by default. Use the 'include_zero_balance' query parameter
# set to 'false' or '0' to exclude accounts with a zero balance on the report date.
#     """,
#     parameters=[
#         OpenApiParameter(
#             name='as_of_date',
#             description='Required. Generate Trial Balance as of this date (YYYY-MM-DD).',
#             required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY
#         ),
#         OpenApiParameter(
#             name='include_zero_balance',
#             description="Optional. Set to 'false' or '0' to exclude zero-balance accounts. Defaults to true.",
#             required=False,
#             # CORRECT TYPE:
#             type=OpenApiTypes.BOOL,
#             location=OpenApiParameter.QUERY
#         ),
#     ],
#     responses={
#         200: TrialBalanceStructuredResponseSerializer,
#         400: OpenApiResponse(description="Bad Request - Invalid date/parameter format, missing parameter, or data validation error."),
#         403: OpenApiResponse(description="Forbidden - User does not have permission."),
#         500: OpenApiResponse(description="Internal Server Error."),
#     },
#     tags=['Reports']
# )
# class TrialBalanceView(APIView):
#     """
#     Provides the Trial Balance report as of a specific date.
#     Allows optional filtering to exclude zero-balance accounts.
#     Requires 'view_financial_reports' permission.
#     """
#     # permission_classes = [CanViewFinancialReports]
#     authentication_classes = [authentication.SessionAuthentication]  # <<< Add/Modify this line
#
#     permission_classes = [permissions.IsAuthenticated]
#     def get(self, request, *args, **kwargs):
#         """Handles GET requests to generate and return the Trial Balance."""
#
#         # --- 1. Input Validation ---
#         date_str = request.query_params.get('as_of_date')
#         include_zero_str = request.query_params.get('include_zero_balance', 'true').lower() # Default to true
#
#         if not date_str:
#             raise ParseError(detail=_("Query parameter 'as_of_date' (YYYY-MM-DD) is required."))
#
#         try:
#             as_of_date = date.fromisoformat(date_str)
#             if as_of_date > date.today():
#                  logger.warning(f"Trial Balance requested for future date {as_of_date} by user {request.user.email}")
#                  raise ParseError(detail=_("Report date cannot be in the future."))
#         except ValueError:
#              raise ParseError(detail=_("Invalid date format for 'as_of_date'. Use YYYY-MM-DD."))
#
#         # Parse boolean parameter (handle 'false', '0')
#         include_zero_balance = include_zero_str not in ['false', '0']
#
#         # --- 2. Service Layer Call ---
#         try:
#             # Service always returns the full data including zero balances
#             report_data = reports_service.generate_trial_balance_structured(as_of_date=as_of_date)
#
#             if not report_data.get('is_balanced', False):
#                  logger.critical(
#                      f"Trial Balance generation resulted in imbalance for date {as_of_date}. "
#                      f"Debit: {report_data.get('total_debit')}, Credit: {report_data.get('total_credit')}. "
#                      f"Investigation needed!"
#                  )
#
#         except Exception as e:
#             # Let central handler manage exceptions, re-raise after logging
#             logger.exception(f"Unhandled exception during Trial Balance generation for {as_of_date}: {e}")
#             raise e
#
#         # --- 3. Optional Filtering (Post-Processing) ---
#         if not include_zero_balance:
#             logger.debug(f"Filtering zero balance accounts for TB {as_of_date}")
#             # Filter flat entries
#             report_data['flat_entries'] = [
#                 e for e in report_data['flat_entries']
#                 if e['debit'] != ZERO_DECIMAL or e['credit'] != ZERO_DECIMAL
#             ]
#             # Filter hierarchy (use helper function on a copy)
#             report_data['hierarchy'] = _filter_hierarchy_for_zero_balance(report_data['hierarchy'])
#             # Note: Grand totals remain the same (based on all accounts) for balancing check.
#
#         # --- 4. Serialization and Response ---
#         serializer = TrialBalanceStructuredResponseSerializer(report_data)
#         return Response(serializer.data, status=status.HTTP_200_OK)