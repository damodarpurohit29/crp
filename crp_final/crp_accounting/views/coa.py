# crp_accounting/views/coa.py
import logging
from decimal import Decimal
from datetime import date

from django.db import models  # For models.ProtectedError
from django.utils.translation import gettext_lazy as _
from django.utils.text import get_text_list  # For perform_destroy error message formatting

from rest_framework import viewsets, status, filters, generics, serializers  # Added serializers for inline_serializer
from rest_framework.exceptions import ValidationError, ParseError  # PermissionDenied handled by mixin
from rest_framework.generics import get_object_or_404  # Correct import
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend

from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes, inline_serializer

# --- Model Imports ---
from ..models.coa import Account, AccountGroup
from ..models.journal import VoucherLine, TransactionStatus  # For perform_destroy check

# --- Serializer Imports ---
from ..serializers.coa import (
    AccountGroupReadSerializer, AccountGroupWriteSerializer,
    AccountReadSerializer, AccountWriteSerializer,
    AccountSummarySerializer, AccountLedgerEntrySerializer, AccountLedgerResponseSerializer,
    # Define or import BulkAccountIDsSerializer if using Option B for bulk actions
    # BulkAccountIDsSerializer,
)
# --- Service Imports ---
from ..services import ledger_service

# --- Core Mixin Imports ---
from crp_core.mixins import CompanyScopedViewSetMixin, CompanyScopedAPIViewMixin  # Ensure this path is correct

logger = logging.getLogger(__name__)


# --- Standard Pagination ---
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 1000


# =============================================================================
# AccountGroup ViewSet
# =============================================================================
@extend_schema_view(
    # Ensure these actions exist on ModelViewSet if not overridden
    list=extend_schema(summary="List Account Groups (Scoped to Current Company)"),
    retrieve=extend_schema(summary="Retrieve Account Group (Scoped to Current Company)"),
    create=extend_schema(summary="Create Account Group (Scoped to Current Company)"),
    update=extend_schema(summary="Update Account Group (Scoped to Current Company)"),
    partial_update=extend_schema(summary="Partial Update Account Group (Scoped to Current Company)"),
    destroy=extend_schema(summary="Delete Account Group (Scoped to Current Company, soft delete)"),
)
class AccountGroupViewSet(CompanyScopedViewSetMixin):
    queryset = AccountGroup.objects.all()  # CompanyManager on model handles scoping
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['parent_group__id', 'name']
    search_fields = ['name', 'description', 'parent_group__name']
    ordering_fields = ['name', 'parent_group__name', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        # super().get_queryset() from CompanyScopedViewSetMixin handles schema generation case
        # and relies on CompanyManager for tenant scoping in actual requests.
        # Add select_related/prefetch_related for performance on the actual data queryset.
        qs = super().get_queryset()
        if not getattr(self, 'swagger_fake_view', False):  # Don't do heavy lifting for schema
            qs = qs.select_related('parent_group', 'company').prefetch_related('sub_groups', 'accounts')
        return qs

    def get_serializer_class(self):
        if self.action in ['list', 'retrieve']:
            return AccountGroupReadSerializer
        return AccountGroupWriteSerializer  # Serializer __init__ handles its related field querysets

    def perform_destroy(self, instance: AccountGroup):
        if instance.sub_groups.filter(deleted_at__isnull=True).exists():  # Check active sub-groups
            raise ValidationError(
                _("Cannot delete group '%(name)s' as it has active sub-groups. Please delete or reassign them first.") % {
                    'name': instance.name})
        if instance.accounts.filter(deleted_at__isnull=True).exists():  # Check active accounts
            raise ValidationError(
                _("Cannot delete group '%(name)s' as it has active accounts linked. Please delete or reassign them first.") % {
                    'name': instance.name})
        try:
            logger.info(
                f"User {self.request.user.username} (Co: {self.current_company.name}) soft deleting AccountGroup {instance.pk} ('{instance.name}')")
            instance.delete()  # Soft delete
        except models.ProtectedError as e:  # Catch DB-level protection if any
            logger.warning(
                f"ProtectedError soft deleting AccountGroup {instance.pk} for {self.current_company.name}: {e}")
            protected_details = []
            if hasattr(e, 'protected_objects'):  # Django 3.0+
                for model_class, protected_objects_set in e.protected_objects.items():
                    obj_names = get_text_list([str(obj) for obj in list(protected_objects_set)[:5]], _('and'))
                    protected_details.append(f"{model_class._meta.verbose_name_plural.capitalize()}: {obj_names}")
            error_message = _("Cannot delete this group. It is still referenced by: %(details)s.") % {
                'details': "; ".join(protected_details) if protected_details else _("other records")}
            raise ValidationError(error_message)
        except Exception as e:
            logger.exception(
                f"Unexpected error soft deleting AccountGroup {instance.pk} for {self.current_company.name}")
            raise ValidationError(_("An unexpected error occurred. The group was not deleted."))


# =============================================================================
# Account ViewSet
# =============================================================================
# Define a reusable serializer for bulk ID requests if structure is same
class AccountBulkIDsRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), help_text=_("List of Account UUIDs."))


class AccountBulkActionResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    updated_count = serializers.IntegerField()


@extend_schema_view(
    # Ensure these actions exist on ModelViewSet
    list=extend_schema(summary="List Accounts (Scoped to Current Company)"),
    retrieve=extend_schema(summary="Retrieve Account (Scoped to Current Company)"),
    create=extend_schema(summary="Create Account (Scoped to Current Company)"),
    update=extend_schema(summary="Update Account (Scoped to Current Company)"),
    partial_update=extend_schema(summary="Partial Update Account (Scoped to Current Company)"),
    destroy=extend_schema(summary="Delete Account (Scoped to Current Company, soft delete)"),
    balance_as_of=extend_schema(
        summary="Get Account Balance As Of Date (Scoped to Current Company)",
        parameters=[OpenApiParameter(name='id', location=OpenApiParameter.PATH, type=OpenApiTypes.UUID, required=True,
                                     description="Account UUID"),
                    OpenApiParameter(name='date', description='Target date (YYYY-MM-DD)', required=True,
                                     type=OpenApiTypes.DATE)],
        responses={200: inline_serializer(name='BalanceAsOfResponse', fields={'account_id': serializers.UUIDField(),
                                                                              'date_as_of': serializers.DateField(),
                                                                              'balance': serializers.DecimalField(
                                                                                  max_digits=20, decimal_places=2)})}
    ),
    bulk_activate=extend_schema(
        summary="Bulk Activate Accounts (Scoped to Current Company)",
        request=AccountBulkIDsRequestSerializer,  # Use defined serializer
        responses={200: AccountBulkActionResponseSerializer}
    ),
    bulk_deactivate=extend_schema(
        summary="Bulk Deactivate Accounts (Scoped to Current Company)",
        request=AccountBulkIDsRequestSerializer,  # Use defined serializer
        responses={200: AccountBulkActionResponseSerializer}
    ),
)
class AccountViewSet(CompanyScopedViewSetMixin):
    queryset = Account.objects.all()  # CompanyManager handles scoping
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'account_group__id': ['exact'], 'account_group__name': ['exact', 'icontains'],
        'account_type': ['exact', 'in'], 'account_nature': ['exact'], 'currency': ['exact'],
        'is_active': ['exact'], 'allow_direct_posting': ['exact'], 'is_control_account': ['exact'],
        'control_account_party_type': ['exact', 'isnull'],
    }
    search_fields = ['account_number', 'account_name', 'description', 'account_group__name']
    ordering_fields = ('account_number', 'account_name', 'account_group__name', 'account_type',
                       'is_active', 'created_at', 'current_balance', 'updated_at')
    ordering = ['account_group__name', 'account_number']

    def get_queryset(self):
        qs = super().get_queryset()
        if not getattr(self, 'swagger_fake_view', False):
            qs = qs.select_related('account_group', 'company')
        return qs

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return AccountWriteSerializer
        return AccountReadSerializer

    def perform_destroy(self, instance: Account):
        if VoucherLine.objects.filter(
                account=instance,
                voucher__status=TransactionStatus.POSTED.value,
                deleted_at__isnull=True
        ).exists():  # Assuming VoucherLine.objects is also tenant-scoped
            raise ValidationError(
                _("Cannot delete account '%(name)s' (%(number)s) as it has posted journal entries. Deactivate instead.") %
                {'name': instance.account_name, 'number': instance.account_number}
            )
        try:
            logger.info(
                f"User {self.request.user.username} (Co: {self.current_company.name}) soft deleting Account {instance.pk} ('{instance.account_name}')")
            instance.delete()
        except models.ProtectedError as e:
            logger.warning(f"ProtectedError soft deleting Account {instance.pk} for {self.current_company.name}: {e}")
            protected_details = []
            if hasattr(e, 'protected_objects'):
                for model_class, protected_objects_set in e.protected_objects.items():
                    obj_names = get_text_list([str(obj) for obj in list(protected_objects_set)[:5]], _('and'))
                    protected_details.append(f"{model_class._meta.verbose_name_plural.capitalize()}: {obj_names}")
            error_message = _("Cannot delete this account. Referenced by: %(details)s.") % {
                'details': "; ".join(protected_details) if protected_details else _("other records")}
            raise ValidationError(error_message)
        except Exception as e:
            logger.exception(f"Unexpected error soft deleting Account {instance.pk} for {self.current_company.name}")
            raise ValidationError(_("Unexpected error. Account not deleted."))

    @action(detail=True, methods=['get'], url_path='balance-as-of')
    def balance_as_of(self, request, pk=None):  # pk type (UUID) is defined in @extend_schema_view
        account = self.get_object()
        date_str = request.query_params.get('date')
        if not date_str: raise ParseError(detail=_("Missing 'date' query parameter (YYYY-MM-DD)."))
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            raise ParseError(detail=_("Invalid date format. Use YYYY-MM-DD."))
        try:
            balance = account.get_dynamic_balance(date_upto=target_date)
            return Response({'account_id': account.id, 'date_as_of': target_date, 'balance': balance})
        except ValueError as ve:
            logger.error(f"Value error in balance_as_of for Account {pk} (Co: {self.current_company.name}): {ve}")
            raise ValidationError(str(ve))
        except Exception as e:
            logger.exception(f"Unexpected error in balance_as_of for Account {pk} (Co: {self.current_company.name})")
            return Response({"detail": _("Error calculating balance.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _get_ids_from_request_data(self, request_data) -> list:  # Type hint for return
        item_ids = request_data.get('ids', [])
        if not isinstance(item_ids, list):
            raise ParseError(_("'ids' must be a list of account primary keys (UUIDs)."))
        if not item_ids:
            raise ParseError(_("No account IDs ('ids' list) provided."))
        # Add UUID validation if needed:
        # for item_id in item_ids:
        #     try: uuid.UUID(str(item_id))
        #     except ValueError: raise ParseError(_(f"Invalid UUID format in 'ids': {item_id}"))
        return item_ids

    @action(detail=False, methods=['post'], url_path='bulk-activate')
    def bulk_activate(self, request):
        try:
            account_ids = self._get_ids_from_request_data(request.data)
        except ParseError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Account.objects is tenant-scoped.
        # The queryset from self.get_queryset() would also be tenant-scoped,
        # but for bulk actions on IDs, filtering the base manager is common.
        queryset_to_update = Account.objects.filter(pk__in=account_ids)

        # Log if not all requested IDs were found within the current company's scope
        if queryset_to_update.count() != len(account_ids):
            logger.warning(f"Bulk activate for Co {self.current_company.name}: Requested {len(account_ids)} IDs, "
                           f"but only {queryset_to_update.count()} found in scope. Proceeding with found IDs.")

        updated_count = queryset_to_update.update(is_active=True)
        return Response({'message': _("Successfully activated accounts."), 'updated_count': updated_count})

    @action(detail=False, methods=['post'], url_path='bulk-deactivate')
    def bulk_deactivate(self, request):
        try:
            account_ids = self._get_ids_from_request_data(request.data)
        except ParseError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        queryset_to_update = Account.objects.filter(pk__in=account_ids)  # Tenant-scoped
        if queryset_to_update.count() != len(account_ids):
            logger.warning(f"Bulk deactivate for Co {self.current_company.name}: Requested {len(account_ids)} IDs, "
                           f"but only {queryset_to_update.count()} found in scope. Proceeding with found IDs.")
        updated_count = queryset_to_update.update(is_active=False)
        return Response({'message': _("Successfully deactivated accounts."), 'updated_count': updated_count})


# =============================================================================
# Ledger Views
# =============================================================================
# Define a response serializer for AccountBalanceAPIView if not using inline_serializer
class AccountBalanceResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    account_number = serializers.CharField()
    account_name = serializers.CharField()
    current_balance = serializers.DecimalField(max_digits=20, decimal_places=2)
    balance_last_updated = serializers.DateTimeField(allow_null=True)
    currency = serializers.CharField()


@extend_schema_view(
    get=extend_schema(
        summary="Get Current Stored Account Balance (Scoped to Current Company)",
        parameters=[
            OpenApiParameter(name='account_pk', location=OpenApiParameter.PATH, type=OpenApiTypes.UUID, required=True,
                             description="Account UUID")],
        responses={200: AccountBalanceResponseSerializer}
    )
)
class AccountBalanceAPIView(CompanyScopedAPIViewMixin):
    # No serializer_class needed for request body if it's a GET only view
    # serializer_class = None

    def get(self, request, account_pk, format=None):  # account_pk type defined in @extend_schema
        account_obj = get_object_or_404(Account, pk=account_pk, company=self.current_company)
        serializer = AccountBalanceResponseSerializer({
            'id': account_obj.id, 'account_number': account_obj.account_number,
            'account_name': account_obj.account_name, 'current_balance': account_obj.current_balance,
            'balance_last_updated': account_obj.balance_last_updated, 'currency': account_obj.currency,
        })
        return Response(serializer.data)


@extend_schema_view(
    get=extend_schema(
        summary="Get Account Ledger (Scoped to Current Company)",
        parameters=[
            OpenApiParameter(name='account_pk', location=OpenApiParameter.PATH, type=OpenApiTypes.UUID, required=True,
                             description="Account UUID"),
            OpenApiParameter(name='start_date', location=OpenApiParameter.QUERY, required=False,
                             type=OpenApiTypes.DATE),
            OpenApiParameter(name='end_date', location=OpenApiParameter.QUERY, required=False, type=OpenApiTypes.DATE),
        ],
        responses={200: AccountLedgerResponseSerializer}
    )
)
class AccountLedgerAPIView(CompanyScopedAPIViewMixin, generics.GenericAPIView):
    serializer_class = AccountLedgerEntrySerializer  # For paginating the 'entries'
    pagination_class = StandardResultsSetPagination

    def _parse_ledger_dates(self, request_params):
        start_date_str = request_params.get('start_date')
        end_date_str = request_params.get('end_date')
        s_date, e_date = None, None
        try:
            if start_date_str: s_date = date.fromisoformat(start_date_str)
            if end_date_str: e_date = date.fromisoformat(end_date_str)
        except ValueError:
            raise ParseError(detail=_("Invalid date format for date query parameters. Use YYYY-MM-DD."))
        return s_date, e_date

    def get(self, request, account_pk, format=None):  # account_pk type defined in @extend_schema_view
        start_date, end_date = self._parse_ledger_dates(request.query_params)
        account_for_summary_qs = Account.objects.filter(pk=account_pk, company=self.current_company)
        account_for_summary = get_object_or_404(
            account_for_summary_qs.values('pk', 'account_number', 'account_name', 'currency', 'account_type',
                                          'is_active')
        )  # Fetch as dict
        try:
            ledger_data_from_service = ledger_service.get_account_ledger_data(
                company_id=self.current_company.id, account_pk=account_pk,
                start_date=start_date, end_date=end_date
            )
            page_entries = self.paginate_queryset(ledger_data_from_service.get('entries', []))

            # Prepare data for the AccountLedgerResponseSerializer
            # The 'account' field in AccountLedgerResponseSerializer expects AccountSummarySerializer data
            # So, we pass the dict `account_for_summary` which matches AccountSummarySerializer structure.
            response_data_for_main_serializer = {
                'account': account_for_summary,  # Pass the dictionary
                'start_date': ledger_data_from_service.get('start_date'),
                'end_date': ledger_data_from_service.get('end_date'),
                'opening_balance': ledger_data_from_service.get('opening_balance'),
                'total_debit': ledger_data_from_service.get('total_debit'),
                'total_credit': ledger_data_from_service.get('total_credit'),
                'closing_balance': ledger_data_from_service.get('closing_balance'),
                # 'entries' will be replaced by paginated data if pagination is active
            }

            if page_entries is not None:
                # Serializer for the 'entries' part of the paginated response
                entries_page_serializer = self.get_serializer(page_entries, many=True)
                # Get the paginated structure (with count, next, previous)
                paginated_response_shell = self.get_paginated_response(entries_page_serializer.data)

                # Now, merge the summary data into the paginated response shell
                # The AccountLedgerResponseSerializer is not used to serialize the *entire* paginated response object,
                # but rather to define the structure of the summary parts.
                # We manually construct the final response data structure.
                final_response_data = paginated_response_shell.data  # This has 'count', 'next', 'previous', 'results'
                final_response_data['account'] = AccountSummarySerializer(account_for_summary).data
                final_response_data['start_date'] = response_data_for_main_serializer['start_date']
                final_response_data['end_date'] = response_data_for_main_serializer['end_date']
                final_response_data['opening_balance'] = response_data_for_main_serializer['opening_balance']
                final_response_data['total_debit'] = response_data_for_main_serializer['total_debit']
                final_response_data['total_credit'] = response_data_for_main_serializer['total_credit']
                final_response_data['closing_balance'] = response_data_for_main_serializer['closing_balance']

                # Rename 'results' (from paginator) to 'entries' to match AccountLedgerResponseSerializer
                if 'results' in final_response_data:
                    final_response_data['entries'] = final_response_data.pop('results')

                return Response(final_response_data)
            else:
                # No pagination, or empty entries list. Serialize the whole thing with AccountLedgerResponseSerializer.
                response_data_for_main_serializer['entries'] = ledger_data_from_service.get('entries', [])
                full_response_serializer = AccountLedgerResponseSerializer(response_data_for_main_serializer)
                return Response(full_response_serializer.data)

        except ledger_service.LedgerGenerationError as lge:
            logger.warning(f"Ledger service error for Co {self.current_company.id}, Acc {account_pk}: {lge}")
            return Response({"detail": str(lge)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(
                f"Unexpected error in AccountLedgerAPIView for Co {self.current_company.id}, Acc {account_pk}")
            return Response({"detail": _("An unexpected server error occurred.")},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)