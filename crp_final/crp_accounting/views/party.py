# crp_accounting/views/party.py

import logging
from decimal import Decimal, InvalidOperation
from datetime import date

from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404 # Use this for single object fetches
from django.http import Http404 # Use this for 404 errors
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.conf import settings # For optional settings

from rest_framework import viewsets, permissions, status, filters
from rest_framework.exceptions import ValidationError, ParseError, PermissionDenied # Added PermissionDenied
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend

# --- Swagger/Spectacular Imports ---
from drf_spectacular.utils import (
    extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes, OpenApiResponse, inline_serializer
)

# --- Model & Serializer Imports (Tenant Aware) ---
from ..models.party import Party
from ..models.coa import Account # Needed for queryset filtering
from ..models.journal import Voucher # Needed for deletion check
from ..serializers.party import PartyReadSerializer, PartyWriteSerializer

# --- Enum Imports ---
from crp_core.enums import PartyType

# --- Mixin Imports (Ensure path is correct) ---
from .coa import CompanyScopedViewSetMixin # Import the tenant scoping mixin

logger = logging.getLogger(__name__)


# --- Standard Pagination ---
# Assuming this is defined elsewhere or keep it here
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 1000


# =============================================================================
# Party ViewSet (Tenant Aware)
# =============================================================================
@extend_schema_view(
    # ... (schemas remain same, just add 'Company Scoped' to summaries) ...
    list=extend_schema(summary="List Parties (Company Scoped)"),
    retrieve=extend_schema(summary="Retrieve Party (Company Scoped)"),
    create=extend_schema(summary="Create Party (Company Scoped)"),
    update=extend_schema(summary="Update Party (Company Scoped)"),
    partial_update=extend_schema(summary="Partial Update Party (Company Scoped)"),
    destroy=extend_schema(summary="Delete Party (Company Scoped)"),
    balance_as_of=extend_schema(summary="Party Balance As Of Date (Company Scoped)"),
    check_credit_limit=extend_schema(summary="Check Party Credit Limit (Company Scoped)"),
    bulk_activate=extend_schema(summary="Bulk Activate Parties (Company Scoped)"),
    bulk_deactivate=extend_schema(summary="Bulk Deactivate Parties (Company Scoped)"),
)
# --- Inherit from CompanyScopedViewSetMixin ---
class PartyViewSet(CompanyScopedViewSetMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing Parties (Customers, Suppliers, etc.)
    scoped to the current user's company.
    """
    # queryset uses CompanyManager via inheritance/model definition
    queryset = Party.objects.select_related('control_account', 'company').all()
    # permission_classes inherited from Mixin
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]

    # Filters operate on company-scoped data
    filterset_fields = {
        'party_type': ['exact'],
        'is_active': ['exact'],
        'control_account': ['exact', 'isnull'], # Allow filtering for parties without control account
        'control_account__account_name': ['icontains'],
        'name': ['icontains'],
        'contact_email': ['icontains'],
    }
    search_fields = [
        'name', 'contact_email', 'contact_phone',
        'control_account__account_number', 'control_account__account_name'
    ] # Company search implicit
    ordering_fields = ['name', 'party_type', 'is_active', 'created_at', 'control_account__name']
    ordering = ['name']

    def get_serializer_class(self):
        """Switch between Read and Write serializers."""
        if self.action in ['list', 'retrieve']:
            return PartyReadSerializer
        return PartyWriteSerializer

    # get_queryset and get_serializer_context inherited from Mixin
    # perform_create inherited from Mixin (sets company automatically)

    def get_serializer(self, *args, **kwargs):
        """Override to inject filtered queryset for 'control_account' in write actions."""
        serializer_class = self.get_serializer_class()
        kwargs['context'] = self.get_serializer_context()
        company = kwargs['context'].get('company')

        if serializer_class == PartyWriteSerializer and company:
            # Filter Account choices for the control_account field
            # Show only active, control accounts of the correct type for the company
            control_account_queryset = Account.objects.filter(
                company=company,
                is_active=True,
                is_control_account=True
                # Optionally further filter by control_account_party_type if needed early
            )
            serializer = serializer_class(*args, **kwargs)
            # Set the filtered queryset on the serializer field
            serializer.fields['control_account'].queryset = control_account_queryset
            return serializer
        else:
            # For read actions or if company context is missing
            return serializer_class(*args, **kwargs)

    def perform_destroy(self, instance: Party):
        """
        Prevents deletion if the party (within the company) has associated vouchers.
        """
        # instance is company-scoped via get_object() -> get_queryset()
        company = getattr(self.request, 'company', None)
        if not company or instance.company != company:
             logger.error(f"Attempt to delete Party {instance.pk} from wrong company context.")
             raise PermissionDenied(_("Operation not allowed in this company context."))

        # Filter Vouchers by party and company
        if Voucher.objects.filter(party=instance, company=company).exists():
            logger.warning(f"Attempted to delete Party '{instance.name}' (ID: {instance.id}, Co: {company.id}) which has associated Vouchers.")
            raise ValidationError(
                f"Cannot delete party '{instance.name}' because it has financial transactions recorded. Consider making the party inactive instead."
            )

        try:
            logger.info(f"User {self.request.user.pk} deleting Party '{instance.name}' (ID: {instance.id}, Co: {company.id}).")
            super().perform_destroy(instance)
        except ProtectedError as e:
            logger.error(f"Deletion failed for Party '{instance.name}' (ID: {instance.id}, Co: {company.id}) due to protected relationships: {e}")
            # Customize message based on common protected links if possible
            raise ValidationError(f"Deletion failed. This party might be linked to other records.")
        except Exception as e:
            logger.exception(f"Error deleting Party {instance.pk} for Company {company.id}")
            raise ValidationError(_("An unexpected error occurred during deletion."))


    # --- Custom Actions (Tenant Aware) ---
    @extend_schema(summary="Party Balance As Of Date (Company Scoped)") # Keep details
    @action(detail=True, methods=['get'], url_path='balance-as-of')
    def balance_as_of(self, request, pk=None):
        """Calculates balance for a specific party within the user's company."""
        party = self.get_object() # Ensures party belongs to user's company context
        date_str = request.query_params.get('date', None)
        if not date_str: raise ParseError(detail=_("Missing 'date' query parameter (YYYY-MM-DD)."))
        try: target_date = date.fromisoformat(date_str)
        except ValueError: raise ParseError(detail=_("Invalid date format for 'date'. Use YYYY-MM-DD."))

        try:
            # Model method is implicitly tenant-aware
            balance = party.calculate_outstanding_balance(date_upto=target_date)
            return Response({'party_id': party.id, 'date_as_of': target_date, 'balance': balance})
        except ValueError as e: return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e: logger.exception(f"Error in balance_as_of for Party {pk} (Co {party.company_id}): {e}"); return Response({"detail": _("An unexpected error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(summary="Check Party Credit Limit (Company Scoped)") # Keep details
    @action(detail=True, methods=['get'], url_path='check-credit')
    def check_credit_limit(self, request, pk=None):
        """Checks credit limit for a party within the user's company."""
        party = self.get_object() # Company-scoped
        amount_str = request.query_params.get('amount', None)
        if amount_str is None: raise ParseError(detail=_("Missing 'amount' query parameter."))
        try:
            transaction_amount = Decimal(amount_str)
            if transaction_amount <= 0: raise ValueError("Amount must be positive.")
        except (InvalidOperation, ValueError): raise ParseError(detail=_("Invalid 'amount'. Must be positive number."))

        try:
            party.check_credit_limit(transaction_amount) # Model method is implicitly tenant-aware
            return Response({'status': 'OK', 'message': _("Amount is within the credit limit.")})
        except ValidationError as e: return Response({'status': 'Exceeded', 'message': e.detail[0] if isinstance(e.detail, list) else str(e.detail)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e: return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST) # Catch model calc errors
        except Exception as e: logger.exception(f"Error in check_credit_limit for Party {pk} (Co {party.company_id}): {e}"); return Response({"detail": _("An unexpected error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    @extend_schema(summary="Bulk Activate Parties (Company Scoped)") # Keep details
    @action(detail=False, methods=['post'], url_path='bulk-activate')
    def bulk_activate(self, request):
        """Activates multiple parties within the user's company."""
        party_ids = request.data.get('ids', [])
        if not isinstance(party_ids, list) or not all(isinstance(i, int) for i in party_ids): return Response({'error': _("'ids' must be a list of integer party IDs.")}, status=status.HTTP_400_BAD_REQUEST)

        # Queryset uses CompanyManager via model inheritance
        queryset = Party.objects.filter(pk__in=party_ids)
        updated_count = queryset.update(is_active=True) # Automatically company-scoped
        logger.info(f"User {request.user.pk} bulk activated {updated_count} parties in Co {request.company.id}. IDs: {party_ids}")
        return Response({'message': _('Successfully activated %(count)d parties.') % {'count': updated_count}})


    @extend_schema(summary="Bulk Deactivate Parties (Company Scoped)") # Keep details
    @action(detail=False, methods=['post'], url_path='bulk-deactivate')
    def bulk_deactivate(self, request):
        """Deactivates multiple parties within the user's company."""
        party_ids = request.data.get('ids', [])
        if not isinstance(party_ids, list) or not all(isinstance(i, int) for i in party_ids): return Response({'error': _("'ids' must be a list of integer party IDs.")}, status=status.HTTP_400_BAD_REQUEST)

        company = getattr(request, 'company', None)
        if not company: raise PermissionDenied("Company context required.")

        # Queryset uses CompanyManager
        parties_to_check = Party.objects.filter(pk__in=party_ids, is_active=True)
        warnings = []
        allowed_ids_to_deactivate = list(party_ids) # Assume all initially

        check_balance_setting = getattr(settings, 'ACCOUNTING_CHECK_BALANCE_BEFORE_DEACTIVATION', True)

        if check_balance_setting:
            allowed_ids_to_deactivate = []
            for party in parties_to_check: # parties_to_check is already company-scoped
                try:
                    balance = party.calculate_outstanding_balance() # Implicitly tenant-aware
                    if balance == Decimal('0.00'):
                        allowed_ids_to_deactivate.append(party.pk)
                    else:
                        warnings.append(_("Party '%(name)s' not deactivated due to non-zero balance (%(balance)s).") % {'name': party.name, 'balance': balance})
                except Exception as e:
                    logger.error(f"Error checking balance for Party {party.pk} (Co {company.id}) during bulk deactivate: {e}")
                    warnings.append(_("Could not check balance for Party '%(name)s'. Skipped.") % {'name': party.name})

        # Update only the allowed IDs (still implicitly company-scoped)
        updated_count = 0
        if allowed_ids_to_deactivate:
             updated_count = Party.objects.filter(pk__in=allowed_ids_to_deactivate).update(is_active=False)

        logger.info(f"User {request.user.pk} bulk deactivated {updated_count} parties in Co {company.id}. Allowed IDs: {allowed_ids_to_deactivate}. Warnings: {len(warnings)}")
        return Response({'message': _('Successfully deactivated %(count)d parties.') % {'count': updated_count}, 'warnings': warnings})
# # crp_accounting/views/party.py
#
# import logging
# from decimal import Decimal, InvalidOperation
# from datetime import date
#
# from django.db.models import ProtectedError  # Importing only what's necessary
# from django.utils import timezone
# from rest_framework import viewsets, permissions, status, filters
# from rest_framework.exceptions import ValidationError, ParseError
# from rest_framework.response import Response
# from rest_framework.decorators import action
# from rest_framework.pagination import PageNumberPagination
# from django_filters.rest_framework import DjangoFilterBackend
# from django.utils.translation import gettext_lazy as _
# # --- Swagger/Spectacular Imports ---
# from drf_spectacular.utils import (
#     extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes, OpenApiResponse, inline_serializer
# )
#
# # Adjust project-specific imports
# from crp_accounting.models import Party, Account
# from crp_accounting.models.journal import Voucher  # Needed for deletion check
# from crp_accounting.serializers.party import (
#     PartyReadSerializer,
#     PartyWriteSerializer
# )
# from crp_core.enums import PartyType
# from django.conf import settings  # For optional toggle
#
# logger = logging.getLogger(__name__)
#
#
# # --- Standard Pagination (if not global) ---
# class StandardResultsSetPagination(PageNumberPagination):
#     page_size = 25
#     page_size_query_param = 'page_size'
#     max_page_size = 1000
#
#
# # --- Party ViewSet ---
# @extend_schema_view(
#     list=extend_schema(
#         summary="List Parties",
#         description="Retrieve a paginated list of parties (customers, suppliers, etc.), supporting filtering and searching. Balance information is calculated dynamically.",
#     ),
#     retrieve=extend_schema(
#         summary="Retrieve Party",
#         description="Get details of a specific party, including dynamically calculated balance and credit status."
#     ),
#     create=extend_schema(
#         summary="Create Party",
#         description="Create a new party, ensuring a valid control account is linked for active customers/suppliers."
#     ),
#     update=extend_schema(
#         summary="Update Party",
#         description="Update an existing party completely."
#     ),
#     partial_update=extend_schema(
#         summary="Partial Update Party",
#         description="Update parts of an existing party."
#     ),
#     destroy=extend_schema(
#         summary="Delete Party",
#         description="Delete a party. **Important:** Deletion is blocked if the party has any associated journal entries.",
#         responses={
#             204: OpenApiResponse(description="Party successfully deleted."),
#             400: OpenApiResponse(description="Deletion failed (e.g., transactions exist)."),
#         }
#     ),
#     # Custom Action Schemas...
# )
# class PartyViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint for managing Parties (Customers, Suppliers, etc.).
#
#     Handles CRUD operations and includes accounting-specific logic like
#     deletion prevention based on transactions and dynamic balance display.
#     """
#     queryset = Party.objects.select_related('control_account').all()
#     permission_classes = [permissions.IsAuthenticated]  # Adapt as needed
#     pagination_class = StandardResultsSetPagination
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#
#     # Define filterable fields
#     filterset_fields = {
#         'party_type': ['exact'],
#         'is_active': ['exact'],
#         'control_account': ['exact'],
#         'control_account__account_name': ['exact', 'icontains'],
#         'name': ['icontains'],
#         'contact_email': ['icontains'],
#     }
#     # Define searchable fields
#     search_fields = [
#         'name', 'contact_email', 'contact_phone',
#         'control_account__account_number', 'control_account__account_name'
#     ]
#     # Define orderable fields
#     ordering_fields = ['name', 'party_type', 'is_active', 'created_at', 'control_account__name']
#     ordering = ['name']  # Default ordering
#
#     def get_serializer_class(self):
#         """Switch between Read and Write serializers."""
#         if self.action in ['list', 'retrieve']:
#             return PartyReadSerializer
#         return PartyWriteSerializer
#
#     def perform_destroy(self, instance: Party):
#         """
#         **Accounting Logic:** Prevents deletion if the party has associated journal entries.
#         """
#         if Voucher.objects.filter(party=instance).exists():
#             logger.warning(
#                 f"Attempted to delete Party '{instance.name}' (ID: {instance.id}) which has associated Journal Entries.")
#             raise ValidationError(
#                 f"Cannot delete party '{instance.name}' because it has financial transactions recorded. "
#                 "Consider making the party inactive instead."
#             )
#
#         try:
#             logger.info(f"Deleting Party '{instance.name}' (ID: {instance.id}).")
#             super().perform_destroy(instance)
#         except ProtectedError as e:
#             logger.error(
#                 f"Deletion failed for Party '{instance.name}' (ID: {instance.id}) due to protected relationships: {e}")
#             raise ValidationError(f"Deletion failed. This party might be linked to other records.")
#
#     # --- Custom Actions ---
#     @action(detail=True, methods=['get'], url_path='balance-as-of')
#     def balance_as_of(self, request, pk=None):
#         """Calculates and returns the party's balance as of a specific date."""
#         party = self.get_object()  # Gets the party instance
#         date_str = request.query_params.get('date', None)
#         if not date_str:
#             raise ParseError(detail=_("Missing 'date' query parameter (YYYY-MM-DD)."))
#         try:
#             target_date = date.fromisoformat(date_str)
#         except ValueError:
#             raise ParseError(detail=_("Invalid date format for 'date'. Use YYYY-MM-DD."))
#
#         try:
#             balance = party.calculate_outstanding_balance(date_upto=target_date)
#             return Response({'party_id': party.id, 'date_as_of': target_date, 'balance': balance})
#         except ValueError as e:  # Catch potential errors from calculation
#             return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
#
#     @action(detail=True, methods=['get'], url_path='check-credit')
#     def check_credit_limit(self, request, pk=None):
#         """Checks if a potential transaction amount would exceed the credit limit."""
#         party = self.get_object()
#         amount_str = request.query_params.get('amount', None)
#
#         if amount_str is None:
#             raise ParseError(detail=_("Missing 'amount' query parameter."))
#         try:
#             transaction_amount = Decimal(amount_str)
#             if transaction_amount <= 0:
#                 raise ValueError("Amount must be positive.")
#         except (InvalidOperation, ValueError):
#             raise ParseError(detail=_("Invalid 'amount' provided. Must be a positive number."))
#
#         try:
#             party.check_credit_limit(transaction_amount)
#             return Response({
#                 'status': 'OK',
#                 'message': _("Amount is within the credit limit.")
#             })
#         except ValidationError as e:
#             return Response({
#                 'status': 'Exceeded',
#                 'message': e.detail[0] if isinstance(e.detail, list) else str(e.detail)
#             }, status=status.HTTP_400_BAD_REQUEST)
#         except ValueError as e:  # Catch calculation errors
#             return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
#
#     @action(detail=False, methods=['post'], url_path='bulk-activate')
#     def bulk_activate(self, request):
#         """Activates multiple parties specified by IDs."""
#         party_ids = request.data.get('ids', [])
#         if not isinstance(party_ids, list) or not all(isinstance(i, int) for i in party_ids):
#             return Response({'error': _("'ids' must be a list of integer party IDs.")},
#                             status=status.HTTP_400_BAD_REQUEST)
#
#         updated_count = Party.objects.filter(pk__in=party_ids).update(is_active=True)
#         logger.info(f"Bulk activated {updated_count} parties. IDs: {party_ids}")
#         return Response({'message': _('Successfully activated %(count)d parties.') % {'count': updated_count}})
#
#     @action(detail=False, methods=['post'], url_path='bulk-deactivate')
#     def bulk_deactivate(self, request):
#         """
#         Deactivates multiple parties specified by IDs.
#         Optional check to prevent deactivating parties with non-zero balances.
#         """
#         party_ids = request.data.get('ids', [])
#         if not isinstance(party_ids, list) or not all(isinstance(i, int) for i in party_ids):
#             return Response({'error': _("'ids' must be a list of integer party IDs.")},
#                             status=status.HTTP_400_BAD_REQUEST)
#
#         parties_to_check = Party.objects.filter(pk__in=party_ids, is_active=True)
#         warnings = []
#         allowed_ids_to_deactivate = list(party_ids)  # Start assuming all are allowed
#
#         # Get this flag from settings
#         check_balance_before_deactivation = getattr(settings, 'ACCOUNTING_CHECK_BALANCE_BEFORE_DEACTIVATION', True)
#
#         if check_balance_before_deactivation:
#             allowed_ids_to_deactivate = []
#             for party in parties_to_check:
#                 try:
#                     balance = party.calculate_outstanding_balance()
#                     if balance == Decimal('0.00'):
#                         allowed_ids_to_deactivate.append(party.pk)
#                     else:
#                         warning_msg = _(
#                             "Party '%(name)s' (ID: %(id)d) was not deactivated due to non-zero balance (%(balance)s).")
#                         warnings.append(warning_msg % {'name': party.name, 'id': party.pk, 'balance': balance})
#                 except Exception as e:
#                     logger.error(f"Error checking balance for Party {party.name} (ID: {party.pk}): {e}")
#                     continue
#
#         # Deactivate allowed parties
#         updated_count = Party.objects.filter(pk__in=allowed_ids_to_deactivate).update(is_active=False)
#         logger.info(f"Bulk deactivated {updated_count} parties. IDs: {allowed_ids_to_deactivate}")
#
#         return Response({
#             'message': _('Successfully deactivated %(count)d parties.') % {'count': updated_count},
#             'warnings': warnings
#         })
#
