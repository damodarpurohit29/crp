# crp_accounting/views/profit_loss.py

import logging
from datetime import date

# --- Django/DRF Imports ---
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist # To catch specific errors
from django.http import Http404 # Keep for potential future use
from rest_framework.response import Response
from rest_framework import status, permissions # Keep permissions
from rest_framework.exceptions import ParseError, PermissionDenied # Keep PermissionDenied

# --- Local Imports ---
# Service needs to be tenant-aware
from ..services import reports_service
# Serializer needs to handle tenant-aware response format
from ..serializers.profit_loss import ProfitLossResponseSerializer
# --- Import the Tenant-Aware Mixin ---
try:
    from crp_core.mixins import CompanyScopedAPIViewMixin
except ImportError:
    raise ImportError("Could not import CompanyScopedAPIViewMixin from crp_core.mixins. Ensure it exists.")

# --- Optional RBAC Permission Import ---
# Replace with your actual RBAC permission class
# from ..permissions import CanViewFinancialReportsRBAC

# --- Swagger/Spectacular Imports ---
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse

logger = logging.getLogger(__name__)

# Get default currency from service constants or settings
DEFAULT_REPORT_CURRENCY = getattr(reports_service, 'DEFAULT_REPORT_CURRENCY', 'INR')

# =============================================================================
# Profit & Loss View (Tenant Aware)
# =============================================================================

@extend_schema(
    summary="Generate Profit & Loss Statement (Company Scoped)", # <<< Updated Summary
    description=f"""Generates a Profit and Loss (Income Statement) report for the user's company for a specified date range.

Requires authentication and appropriate role/permission to view financial reports.

Calculates revenue, expenses, and profit based on posted transactions.

**Currency Context:** Includes currency for individual account details. Section totals and Net Income are direct sums and may aggregate multiple currencies. The 'report_currency' field indicates the assumed primary currency context (default: {DEFAULT_REPORT_CURRENCY}).
    """,
    parameters=[
        OpenApiParameter(name='start_date', description='Required. Start date (YYYY-MM-DD).', required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
        OpenApiParameter(name='end_date', description='Required. End date (YYYY-MM-DD).', required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
        # Optional currency parameter
        # OpenApiParameter(name='currency', description=f"Optional. Report currency context (default: {DEFAULT_REPORT_CURRENCY}).", required=False, type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
    ],
    responses={
        200: ProfitLossResponseSerializer,
        400: OpenApiResponse(description="Bad Request - Invalid/missing parameters."),
        403: OpenApiResponse(description="Forbidden - Insufficient permissions or company context error."), # Mixin raises 403
        500: OpenApiResponse(description="Internal Server Error."),
    },
    tags=['Reports']
)
# --- Inherit from CompanyScopedAPIViewMixin ---
class ProfitLossView(CompanyScopedAPIViewMixin):
    """
    API endpoint to generate the Profit and Loss statement for the user's company context.
    """
    # --- Optional: Define Specific RBAC Permissions ---
    # Override permission_classes from the mixin if needed
    # permission_classes = [permissions.IsAuthenticated, CanViewFinancialReportsRBAC] # Example

    def get(self, request, *args, **kwargs):
        """Handles GET requests to generate and return the P&L statement."""
        # --- Company Context is available via self.company (from Mixin) ---
        company_id = self.company.id # Get the ID for service call & logging

        # --- 1. Input Validation ---
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        query_currency = request.query_params.get('currency') # Optional

        if not start_date_str: raise ParseError(detail=_("'start_date' (YYYY-MM-DD) required."))
        if not end_date_str: raise ParseError(detail=_("'end_date' (YYYY-MM-DD) required."))

        try: start_date = date.fromisoformat(start_date_str)
        except ValueError: raise ParseError(detail=_("Invalid 'start_date' format. Use YYYY-MM-DD."))
        try: end_date = date.fromisoformat(end_date_str)
        except ValueError: raise ParseError(detail=_("Invalid 'end_date' format. Use YYYY-MM-DD."))

        if start_date > end_date:
            raise ParseError(detail=_("'start_date' cannot be after 'end_date'."))

        # --- 2. Determine Report Currency ---
        report_currency_context = query_currency.upper() if query_currency else DEFAULT_REPORT_CURRENCY
        # Optional: Validate query_currency

        # --- 3. Service Layer Call (Pass Company ID) ---
        try:
            # Call the tenant-aware service function
            report_data = reports_service.generate_profit_loss(
                company_id=company_id, # <<< Pass Company ID
                start_date=start_date,
                end_date=end_date,
                report_currency=report_currency_context
            )
        except ValueError as ve: # Catch specific validation errors from service (e.g., date mismatch)
             logger.warning(f"Validation error generating P&L for Co {company_id} ({start_date}-{end_date}): {ve}")
             raise ParseError(detail=str(ve)) # Return 400
        except ObjectDoesNotExist as obj_err: # Catch if related data missing (e.g., period)
             logger.warning(f"Data not found during P&L generation for Co {company_id}: {obj_err}")
             raise ParseError(detail=_("Required data for report generation not found.")) from obj_err
        except Exception as e:
            # Catch broader exceptions
            logger.exception(f"Unexpected error generating P&L for Co {company_id} ({start_date}-{end_date}): {e}")
            # Re-raise for DRF 500 handling
            raise

        # --- 4. Serialization and Response ---
        context = {'request': request, 'company': self.company}
        # Use the tenant-aware serializer (which includes company_id)
        serializer = ProfitLossResponseSerializer(report_data, context=context)
        return Response(serializer.data, status=status.HTTP_200_OK)

# =============================================================================
# --- End of File ---
# =============================================================================
# # crp_accounting/views/profit_loss.py
#
# import logging
# from datetime import date
#
# # Django & DRF Imports
# from django.utils.translation import gettext_lazy as _
# from django.core.exceptions import ObjectDoesNotExist # Potentially caught by central handler
# from django.core.cache import cache # For manual caching logic
# from django.conf import settings # To get cache timeout setting
#
# from rest_framework.views import APIView
# from rest_framework.response import Response
# from rest_framework import status, permissions
# from rest_framework.exceptions import ParseError, ValidationError
# # Optional: Import throttle classes only if overriding global settings per-view
# # from rest_framework.throttling import UserRateThrottle, AnonRateThrottle
#
# # --- Local Imports ---
# # Adjust these paths based on your actual project structure
# try:
#     from ..services import reports_service
#     from ..serializers.profit_loss import ProfitLossStructuredResponseSerializer
#     from ..permissions import CanViewFinancialReports
# except ImportError as e:
#     # Raise configuration error early if imports fail
#     raise ImportError(f"Could not import necessary modules for ProfitLossView. Check paths and dependencies: {e}")
#
# # --- Swagger/Spectacular Imports ---
# from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse
#
# # --- Initialize Logger ---
# logger = logging.getLogger(__name__)
#
# # --- Cache Configuration ---
# # Fetches 'PNL_REPORT_CACHE_TIMEOUT' from settings.py, defaults to 900 seconds (15 minutes)
# # Recommendation: Keep this relatively short for financial reports unless data changes infrequently.
# PNL_REPORT_CACHE_TIMEOUT = getattr(settings, 'PNL_REPORT_CACHE_TIMEOUT', 900)
# # Define a clear prefix for P&L cache keys for better namespacing and potential management
# PNL_CACHE_KEY_PREFIX = "crp_acct:pnl_report"
#
# # =============================================================================
# # Profit & Loss Report View
# # =============================================================================
#
# @extend_schema(
#     summary="Generate Profit & Loss Report",
#     description="""Generates a structured Profit and Loss (P&L) or Income Statement
# report for a specified date range. Requires 'view_financial_reports' permission.
#
# The report follows standard accounting principles, calculating Gross Profit and
# Net Income, presenting data hierarchically within standard P&L sections.
#
# **Caching:** Results for a given date range are cached server-side for performance
# (typical duration: ~15 minutes, configured in settings). This uses a time-based
# expiration strategy. Note that recently posted transactions might only appear
# after the cache expires, as complex real-time invalidation is not implemented
# by default. Cache size should also be monitored.
#
# **Rate Limiting:** This endpoint is subject to global API rate limits defined
# in `settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']` to prevent abuse.
#     """,
#     parameters=[
#         OpenApiParameter(
#             name='start_date',
#             description='Required. Start date of the reporting period (YYYY-MM-DD).',
#             required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY
#         ),
#         OpenApiParameter(
#             name='end_date',
#             description='Required. End date of the reporting period (YYYY-MM-DD).',
#             required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY
#         ),
#     ],
#     responses={
#         200: ProfitLossStructuredResponseSerializer,
#         400: OpenApiResponse(description="Bad Request - Missing/invalid parameters or date range."),
#         403: OpenApiResponse(description="Forbidden - User lacks 'view_financial_reports' permission."),
#         429: OpenApiResponse(description="Too Many Requests - Rate limit exceeded."),
#         500: OpenApiResponse(description="Internal Server Error - Unexpected issue during report generation."),
#     },
#     tags=['Reports'] # Group this endpoint in the API documentation
# )
# class ProfitLossView(APIView):
#     """
#     API endpoint for the structured Profit & Loss Statement.
#
#     Features:
#     - Requires specific permissions ('view_financial_reports').
#     - Implements time-based caching for performance.
#     - Relies on globally configured DRF rate limiting.
#     - Delegates report generation logic to the service layer.
#     - Uses a dedicated serializer for response structure.
#     - Assumes a centralized DRF exception handler is configured.
#
#     *Cache Considerations:* Uses time-based expiration (`PNL_REPORT_CACHE_TIMEOUT`).
#     Does not implement signal-based invalidation for simplicity. Monitor cache
#     backend performance and memory usage for large/frequent reports.
#     """
#     # permission_classes = [CanViewFinancialReports]
#     permission_classes = [permissions.IsAuthenticated]
#     # --- Throttling Configuration ---
#     # This view relies on DEFAULT_THROTTLE_CLASSES and DEFAULT_THROTTLE_RATES
#     # defined in settings.py. Uncomment below ONLY if you need specific overrides
#     # for this *particular* endpoint.
#     # throttle_classes = [UserRateThrottle, AnonRateThrottle] # Example override
#     # throttle_scope = 'reports' # Assigns a specific scope rate from settings
#
#     def get(self, request, *args, **kwargs):
#         """Handles GET requests to generate and return the Profit & Loss data."""
#
#         # --- 1. Input Extraction and Validation ---
#         # Get date parameters from the request query string.
#         start_date_str = request.query_params.get('start_date')
#         end_date_str = request.query_params.get('end_date')
#         logger.debug(f"P&L Request received for {start_date_str} to {end_date_str}")
#
#         # Validate presence of required parameters.
#         if not start_date_str:
#             raise ParseError(detail=_("Query parameter 'start_date' (YYYY-MM-DD) is required."))
#         if not end_date_str:
#             raise ParseError(detail=_("Query parameter 'end_date' (YYYY-MM-DD) is required."))
#
#         # Validate date format and logical range.
#         try:
#             start_date = date.fromisoformat(start_date_str)
#             end_date = date.fromisoformat(end_date_str)
#         except ValueError:
#              raise ParseError(detail=_("Invalid date format. Use YYYY-MM-DD for 'start_date' and 'end_date'."))
#
#         if start_date > end_date:
#             raise ValidationError(detail=_("The 'start_date' cannot be after the 'end_date'."))
#         logger.debug(f"P&L Request validated for date range: {start_date} to {end_date}")
#
#         # --- 2. Caching - Attempt to Retrieve Cached Data ---
#         # Construct a descriptive cache key including versioning (v1) if format changes.
#         cache_key = f"{PNL_CACHE_KEY_PREFIX}:v1:{start_date.isoformat()}:{end_date.isoformat()}"
#         cached_data = None # Initialize
#         try:
#             # Attempt to fetch data from the configured cache backend.
#             cached_data = cache.get(cache_key)
#             if cached_data is not None:
#                 logger.info(f"P&L View: Cache HIT for key: {cache_key}")
#                 # Return the cached JSON response directly.
#                 return Response(cached_data, status=status.HTTP_200_OK)
#             else:
#                 logger.info(f"P&L View: Cache MISS for key: {cache_key}. Proceeding to generate report.")
#         except Exception as e:
#             # Log errors during cache retrieval but don't fail the request.
#             # Proceed as if it was a cache miss.
#             logger.error(f"P&L View: Cache GET failed for key {cache_key}: {e}", exc_info=True)
#             cached_data = None # Ensure it's treated as a miss
#
#         # --- 3. Service Layer Call (Executed on Cache Miss) ---
#         # Delegate the complex report generation logic to the service layer.
#         logger.debug(f"P&L View: Calling reports_service for period {start_date} to {end_date}")
#         try:
#             report_data = reports_service.generate_profit_loss_structured(
#                 start_date=start_date,
#                 end_date=end_date
#             )
#         except Exception as e:
#             # Log unexpected errors originating from the service layer.
#             user_pk = request.user.pk if request.user and request.user.is_authenticated else 'Anonymous'
#             logger.exception(
#                 f"P&L View: Unhandled exception during report generation for "
#                 f"{start_date} to {end_date} by user {user_pk}: {e}",
#                 exc_info=True # Includes stack trace
#             )
#             # Re-raise the original exception. The configured central DRF exception
#             # handler is expected to catch this and return a formatted error response (e.g., 500).
#             raise e
#
#         # --- 4. Serialization (Executed on Cache Miss) ---
#         # Convert the generated Python dictionary data into JSON format using the serializer.
#         logger.debug("P&L View: Serializing generated report data.")
#         try:
#             serializer = ProfitLossStructuredResponseSerializer(report_data)
#             response_data = serializer.data # This step performs the serialization.
#         except Exception as e:
#             # Log unexpected errors during the serialization process.
#             logger.exception(
#                 f"P&L View: Serialization error for P&L report ({start_date} to {end_date}): {e}",
#                 exc_info=True
#             )
#             # Re-raise for the central exception handler -> likely results in 500 error.
#             raise e
#
#         # --- 5. Caching - Store Generated Data (Executed on Cache Miss) ---
#         # Store the newly generated and serialized data in the cache.
#         logger.debug(f"P&L View: Attempting to cache result for key {cache_key}")
#         try:
#             cache.set(cache_key, response_data, timeout=PNL_REPORT_CACHE_TIMEOUT)
#             logger.info(f"P&L View: Stored report in cache. Key: {cache_key}, Timeout: {PNL_REPORT_CACHE_TIMEOUT}s")
#         except Exception as e:
#             # Log errors during cache storage but don't fail the request delivery.
#             # The user still gets the data, just the caching failed.
#             logger.error(f"P&L View: Failed to cache report for key {cache_key}: {e}", exc_info=True)
#
#         # --- 6. Return Freshly Generated Response ---
#         # Return the newly generated and serialized data.
#         logger.debug("P&L View: Returning newly generated response.")
#         return Response(response_data, status=status.HTTP_200_OK)
# Recommended location: crp_accounting/views/profit_loss.py

# # crp_accounting/views/profit_loss.py
#
# import logging
# from datetime import date
#
# # Django & DRF Imports
# from django.utils.translation import gettext_lazy as _
# # from django.conf import settings # Uncomment if getting default currency from settings
# from rest_framework.views import APIView
# from rest_framework.response import Response
# from rest_framework import status, permissions, authentication
# from rest_framework.exceptions import ParseError
#
# # --- Local Imports ---
# from ..services import reports_service
# from ..serializers.profit_loss import ProfitLossResponseSerializer
# # from ..permissions import CanViewFinancialReports # Use your custom permission if available
#
# # --- Swagger/Spectacular Imports ---
# from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse
#
# logger = logging.getLogger(__name__)
#
# # Consider making default currency configurable via Django settings or other mechanism
# DEFAULT_REPORT_CURRENCY = 'INR'
#
# # =============================================================================
# # Profit & Loss View
# # =============================================================================
#
# @extend_schema(
#     summary="Generate Profit & Loss Statement",
#     description=f"""Generates a Profit and Loss (Income Statement) report for a specified date range.
#
# Requires authentication.
#
# Calculates revenue, cost of goods sold, gross profit, operating expenses, operating profit,
# other income/expenses, pre-tax profit, tax, and net income based on posted transactions
# within the start and end dates (inclusive).
#
# **Currency Context:** The report includes currency codes for individual account details.
# Totals and subtotals are currently aggregated directly, which might be misleading if multiple
# currencies exist within a section without conversion. The 'report_currency' field indicates
# the assumed primary currency context (default: {DEFAULT_REPORT_CURRENCY}).
#     """,
#     parameters=[
#         OpenApiParameter(
#             name='start_date',
#             description='Required. The start date of the reporting period (YYYY-MM-DD).',
#             required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY
#         ),
#         OpenApiParameter(
#             name='end_date',
#             description='Required. The end date of the reporting period (YYYY-MM-DD).',
#             required=True, type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY
#         ),
#         # Optional: Add a currency parameter if you want user selection (would require service changes for filtering)
#         # OpenApiParameter(
#         #     name='currency',
#         #     description=f"Optional. Specify the currency for the report (e.g., 'USD', 'EUR'). Defaults to {DEFAULT_REPORT_CURRENCY}.",
#         #     required=False, type=OpenApiTypes.STR, location=OpenApiParameter.QUERY
#         # ),
#     ],
#     responses={
#         200: ProfitLossResponseSerializer,
#         400: OpenApiResponse(description="Bad Request - Invalid or missing parameters, or date validation error."),
#         403: OpenApiResponse(description="Forbidden - User does not have permission."),
#         500: OpenApiResponse(description="Internal Server Error - Error during report generation."),
#     },
#     tags=['Reports']
# )
# class ProfitLossView(APIView):
#     """
#     API endpoint to generate and retrieve the Profit and Loss statement.
#     Handles date parameters and calls the report generation service.
#     """
#     # Define appropriate permissions
#     authentication_classes = [authentication.SessionAuthentication]
#     permission_classes = [permissions.IsAuthenticated] # Basic auth check
#     # permission_classes = [permissions.IsAuthenticated, CanViewFinancialReports] # If using custom permission
#
#     def get(self, request, *args, **kwargs):
#         """Handles GET requests to generate and return the P&L statement."""
#
#         # 1. --- Input Validation ---
#         start_date_str = request.query_params.get('start_date')
#         end_date_str = request.query_params.get('end_date')
#         # Optional: Get currency override from query param if needed later
#         # query_currency = request.query_params.get('currency')
#
#         if not start_date_str:
#             raise ParseError(detail=_("Query parameter 'start_date' (YYYY-MM-DD) is required."))
#         if not end_date_str:
#             raise ParseError(detail=_("Query parameter 'end_date' (YYYY-MM-DD) is required."))
#
#         try:
#             start_date = date.fromisoformat(start_date_str)
#         except ValueError:
#              raise ParseError(detail=_("Invalid date format for 'start_date'. Use YYYY-MM-DD."))
#
#         try:
#             end_date = date.fromisoformat(end_date_str)
#         except ValueError:
#              raise ParseError(detail=_("Invalid date format for 'end_date'. Use YYYY-MM-DD."))
#
#         if start_date > end_date:
#             raise ParseError(detail=_("The 'start_date' cannot be after the 'end_date'."))
#
#         # 2. --- Determine Report Currency ---
#         # Using a default for now. Could be enhanced to use settings or query param.
#         report_currency_context = DEFAULT_REPORT_CURRENCY
#         # if query_currency:
#         #     report_currency_context = query_currency.upper() # Validate if needed
#
#         # 3. --- Service Layer Call ---
#         try:
#             report_data = reports_service.generate_profit_loss(
#                 start_date=start_date,
#                 end_date=end_date,
#                 report_currency=report_currency_context # Pass currency context
#             )
#         except ValueError as ve:
#              logger.warning(f"Validation error generating P&L ({start_date}-{end_date}): {ve}")
#              raise ParseError(detail=str(ve)) # Return 400 for user input errors
#         except Exception:
#             # Log the full exception details from the service layer or unexpected issues
#             logger.exception(
#                 f"Unexpected error generating P&L for {start_date} to {end_date} (User: {request.user.id})"
#             )
#             # Let DRF's default handler return a 500 Internal Server Error response
#             raise # Re-raise the caught exception
#
#         # 4. --- Serialization and Response ---
#         serializer = ProfitLossResponseSerializer(report_data)
#         return Response(serializer.data, status=status.HTTP_200_OK)
#
# # =============================================================================
# # --- End of File ---
# # =============================================================================