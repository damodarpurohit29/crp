# crp_accounting/views/balance_sheet.py

import logging
from datetime import date
from typing import Optional  # For Optional[Company]

# --- Django/DRF Imports ---
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.exceptions import ParseError, PermissionDenied

# --- Model Imports ---
from company.models import Company  # For type hinting and direct use

# --- Local Imports ---
from ..services import reports_service
from ..serializers.balance_sheet import BalanceSheetResponseSerializer

logger = logging.getLogger(__name__)
try:
    # Assuming CompanyScopedAPIViewMixin provides self.request and basic auth
    from crp_core.mixins import CompanyScopedAPIViewMixin
except ImportError:
    # Fallback if mixin doesn't exist or to make view self-contained for company logic
    logger.warning("BalanceSheetView: CompanyScopedAPIViewMixin not found. Implementing company logic directly.")
    from rest_framework.views import APIView  # Fallback to basic APIView


    class CompanyScopedAPIViewMixin(APIView):  # Dummy mixin
        permission_classes = [permissions.IsAuthenticated]

# --- Swagger/Spectacular Imports ---
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse

logger = logging.getLogger("crp_accounting.views.balance_sheet")  # Specific logger

DEFAULT_REPORT_CURRENCY = getattr(reports_service, 'DEFAULT_REPORT_CURRENCY',
                                  'USD')  # Ensure USD if company has no default


# =============================================================================
# Balance Sheet View (Tenant Aware and Robust)
# =============================================================================

@extend_schema(
    summary="Generate Balance Sheet Report (Company Scoped)",
    description=f"""Generates a Balance Sheet report for a specified company as of a specific date.
If the user is a superuser, 'company_id' query parameter is required unless a default/session company is active.
Non-superusers will have the report generated for their assigned company.
Shows Assets, Liabilities, and Equity (including calculated Retained Earnings).
Checks if Assets = Liabilities + Equity.
    """,
    parameters=[
        OpenApiParameter(name='as_of_date', description='Required. Report date (YYYY-MM-DD).', required=True,
                         type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
        OpenApiParameter(name='company_id',
                         description="Required for Superusers if no other company context is active. Company's PK.",
                         required=False, type=OpenApiTypes.UUID, location=OpenApiParameter.QUERY),
        # Assuming UUID PK for Company
        OpenApiParameter(name='currency',
                         description=f"Optional. Report currency (e.g., USD, INR). Defaults to company's default or system default ({DEFAULT_REPORT_CURRENCY}).",
                         required=False, type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
    ],
    responses={
        200: BalanceSheetResponseSerializer,
        400: OpenApiResponse(description="Bad Request - Invalid/missing parameters."),
        401: OpenApiResponse(description="Unauthorized - Authentication required."),
        403: OpenApiResponse(description="Forbidden - Insufficient permissions or company context error."),
        404: OpenApiResponse(description="Not Found - Specified company not found."),
        500: OpenApiResponse(description="Internal Server Error."),
    },
    tags=['Reports (API)']
)
class BalanceSheetView(CompanyScopedAPIViewMixin):  # Inherits from your mixin
    """
    API endpoint to generate the Balance Sheet report.
    Company context is determined from request.company (middleware) or company_id GET param for SUs.
    """

    def _get_target_company_for_report(self, request) -> Company:
        """
        Determines the target Company for the report based on user type and request params.
        This method encapsulates the logic previously in _get_company_for_report_or_raise from admin_views.
        """
        user = request.user
        log_prefix = f"[BSView GetCompany][User:{user.name}]"

        if not Company:  # Check if Company model was imported
            logger.critical(f"{log_prefix} Company model not available. Cannot determine company context.")
            raise Http404(_("System configuration error: Company module unavailable."))

        # 1. For Superusers: Check 'company_id' GET param first, then request.company
        if user.is_superuser:
            company_id_param = request.query_params.get('company_id')
            if company_id_param:
                try:
                    # Ensure company_id_param can be cast to Company's PK type
                    company_pk = Company._meta.pk.to_python(company_id_param)
                    selected_company = Company.objects.get(pk=company_pk,
                                                           effective_is_active=True)  # Also check if active
                    logger.info(
                        f"{log_prefix} SU using company '{selected_company.name}' (PK:{selected_company.pk}) from GET param.")
                    return selected_company
                except (Company.DoesNotExist, ValueError, TypeError):
                    logger.warning(
                        f"{log_prefix} SU provided invalid or inactive company_id '{company_id_param}' in GET param.")
                    raise ParseError(detail=_(
                        "The company specified via 'company_id' (%(id)s) does not exist, is inactive, or is invalid.") % {
                                                'id': company_id_param})

            # If no company_id GET param, check if SU is "acting as" a company via middleware
            middleware_company = getattr(request, 'company', None)
            if isinstance(middleware_company, Company) and middleware_company.effective_is_active:
                logger.info(
                    f"{log_prefix} SU using request.company '{middleware_company.name}' (PK:{middleware_company.pk}).")
                return middleware_company

            # SU, but no specific company selected via param or middleware context
            logger.info(f"{log_prefix} SU has no active company context from GET param or request.company.")
            raise ParseError(detail=_(
                "Superuser: A 'company_id' query parameter is required to specify which company's report to view, or ensure your session has an active company context."))

        # 2. For Non-Superusers: Must use request.company set by middleware
        else:
            non_su_company = getattr(request, 'company', None)
            if not isinstance(non_su_company, Company):
                logger.warning(
                    f"{log_prefix} Non-SU has no valid company context in request (request.company is '{non_su_company}').")
                raise PermissionDenied(_("You are not associated with an active company. Access denied."))  # 403

            if not non_su_company.effective_is_active:
                logger.warning(f"{log_prefix} Non-SU's company '{non_su_company.name}' is not effectively active.")
                raise PermissionDenied(
                    _("Your associated company ('%(name)s') is not currently active.") % {'name': non_su_company.name})

            logger.info(f"{log_prefix} Non-SU using request.company '{non_su_company.name}' (PK:{non_su_company.pk}).")
            return non_su_company

    def get(self, request, *args, **kwargs):
        # --- Determine Target Company ---
        try:
            target_company = self._get_target_company_for_report(request)
        except ParseError as pe:  # Raised by helper if SU needs to provide company_id
            return Response({"detail": str(pe)}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as pde:  # Raised if non-SU has no company
            return Response({"detail": str(pde)}, status=status.HTTP_403_FORBIDDEN)
        except Http404 as h404:  # Raised if company_id param is invalid
            return Response({"detail": str(h404)}, status=status.HTTP_404_NOT_FOUND)

        company_id_for_service = target_company.id
        log_prefix = f"[BSView Get][Co:{target_company.name}][User:{request.user.name}]"

        # --- Input Validation for Dates ---
        date_str = request.query_params.get('as_of_date')
        if not date_str:
            raise ParseError(detail=_("'as_of_date' (YYYY-MM-DD) query parameter is required."))
        try:
            as_of_date = date.fromisoformat(date_str)
        except ValueError:
            raise ParseError(detail=_("Invalid 'as_of_date' format. Use YYYY-MM-DD."))
        # Optional: if as_of_date > date.today(): raise ParseError(detail=_("Report date cannot be in the future."))

        # --- Determine Report Currency ---
        query_currency = request.query_params.get('currency')
        report_currency_context = query_currency.upper() if query_currency else \
            (target_company.default_currency_code or DEFAULT_REPORT_CURRENCY)
        # Optional: Add validation for query_currency against your CurrencyType enum

        logger.info(f"{log_prefix} Generating Balance Sheet as of {as_of_date} for currency {report_currency_context}.")

        # --- Service Layer Call ---
        try:
            report_data = reports_service.generate_balance_sheet(
                company_id=company_id_for_service,
                as_of_date=as_of_date,
                report_currency=report_currency_context
            )
            if not report_data.get('is_balanced', True):
                logger.critical(
                    f"{log_prefix} BALANCE SHEET OUT OF BALANCE! Date: {as_of_date}. Investigation REQUIRED!")
        except ValueError as ve:  # E.g., from date parsing within service or bad currency
            logger.warning(f"{log_prefix} Validation error from service: {ve}")
            return Response({"detail": str(ve)}, status=status.HTTP_400_BAD_REQUEST)
        except reports_service.ReportGenerationError as rge:  # Custom service error
            logger.error(f"{log_prefix} Report generation error: {rge}", exc_info=True)
            return Response({"detail": f"{_('Error generating report')}: {rge}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.exception(f"{log_prefix} Unexpected error generating Balance Sheet: {e}")
            return Response({"detail": _("An unexpected server error occurred while generating the report.")},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # --- Serialization and Response ---
        # Pass request and target_company to serializer context if needed by serializer fields
        serializer_context = {'request': request, 'company': target_company}
        serializer = BalanceSheetResponseSerializer(report_data, context=serializer_context)
        return Response(serializer.data, status=status.HTTP_200_OK)