# crp_accounting/views/period.py

import logging
from rest_framework import viewsets, status, permissions, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models # For models.ProtectedError

# --- Core Mixin & Pagination Imports ---
from crp_core.mixins import CompanyScopedViewSetMixin # Ensure this path is correct
from .coa import StandardResultsSetPagination # Assuming coa.py has this, or move to a common location

# --- Model Imports (Tenant-Aware) ---
from ..models.period import FiscalYear, AccountingPeriod
# from ..models.enums import FiscalYearStatus # Assuming you have this or similar

# --- Serializer Imports (Tenant-Aware) ---
from ..serializers.period import (
    FiscalYearReadSerializer,
    FiscalYearWriteSerializer,
    AccountingPeriodReadSerializer,
    AccountingPeriodWriteSerializer
)

logger = logging.getLogger(__name__)

# =============================================================================
# FiscalYear ViewSet (Multi-Tenant Aware)
# =============================================================================
class FiscalYearViewSet(CompanyScopedViewSetMixin):
    """
    ViewSet for managing Fiscal Years within the current user's company.
    Tenant scoping, company context injection, and company assignment on create
    are handled by the CompanyScopedViewSetMixin.
    """
    queryset = FiscalYear.objects.all() # Default manager (CompanyManager) handles scoping
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter] # DjangoFilterBackend if you add filterset_fields
    search_fields = ['name'] # Add more if needed, e.g., 'status'
    ordering_fields = ['name', 'start_date', 'status', 'is_active', 'created_at']
    ordering = ['-start_date'] # Default ordering

    def get_queryset(self):
        # Optimize the tenant-scoped queryset from the mixin
        return super().get_queryset().select_related('company', 'closed_by')

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return FiscalYearWriteSerializer
        return FiscalYearReadSerializer

    @action(detail=True, methods=['post'], url_path='activate')
    def activate_year(self, request, pk=None):
        """Activates this fiscal year (and deactivates others in the same company)."""
        fiscal_year = self.get_object() # Tenant-scoped by mixin's get_queryset
        try:
            # Model's activate() method is tenant-aware and handles deactivating others
            fiscal_year.activate()
            serializer = FiscalYearReadSerializer(fiscal_year, context=self.get_serializer_context())
            return Response(serializer.data)
        except DjangoValidationError as e:
            logger.warning(f"Validation error activating FY {pk} for Co {self.current_company.pk}: {e.messages_joined}")
            return Response({'detail': e.messages_joined}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error activating FY {pk} for Co {self.current_company.pk}: {e}")
            return Response({'detail': _("An unexpected error occurred during activation.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='close')
    def close_year(self, request, pk=None):
        """Closes this fiscal year."""
        fiscal_year = self.get_object() # Tenant-scoped
        try:
            # Model's close_year() method handles all validation (e.g., checking periods)
            fiscal_year.close_year(user=request.user)
            serializer = FiscalYearReadSerializer(fiscal_year, context=self.get_serializer_context())
            return Response(serializer.data)
        except DjangoValidationError as e:
            logger.warning(f"Validation error closing FY {pk} for Co {self.current_company.pk}: {e.messages_joined}")
            return Response({'detail': e.messages_joined}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error closing FY {pk} for Co {self.current_company.pk}: {e}")
            return Response({'detail': _("An unexpected error occurred during closing.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='lock')
    def lock_year_action(self, request, pk=None): # Renamed to avoid clash with potential model method
        """Locks this fiscal year."""
        fiscal_year = self.get_object() # Tenant-scoped
        try:
            # Assuming FiscalYear model has lock_year() method, or direct status change
            if hasattr(fiscal_year, 'lock_year'): fiscal_year.lock_year()
            else: # Fallback if model method doesn't exist
                # Consider FiscalYearStatus enum from models
                fiscal_year.status = "Locked" # Use FiscalYearStatus.LOCKED.value
                fiscal_year.save(update_fields=['status', 'updated_at'])
            serializer = FiscalYearReadSerializer(fiscal_year, context=self.get_serializer_context())
            return Response(serializer.data)
        except DjangoValidationError as e:
            logger.warning(f"Validation error locking FY {pk} for Co {self.current_company.pk}: {e.messages_joined}")
            return Response({'detail': e.messages_joined}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Error locking FY {pk} for Co {self.current_company.pk}: {e}")
            return Response({'detail': _("Error locking fiscal year.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Add reopen_year action if needed, similar to close_year and lock_year_action

# =============================================================================
# AccountingPeriod ViewSet (Multi-Tenant Aware)
# =============================================================================
class AccountingPeriodViewSet(CompanyScopedViewSetMixin):
    """
    ViewSet for managing Accounting Periods within the current user's company.
    Tenant scoping, company context injection, and company assignment on create
    are handled by the CompanyScopedViewSetMixin.
    Filtering of FiscalYear choices for 'fiscal_year_id' in write actions
    is handled by the AccountingPeriodWriteSerializer's __init__ method.
    """
    queryset = AccountingPeriod.objects.all() # Default manager (CompanyManager) handles scoping
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter] # Add DjangoFilterBackend if needed
    search_fields = ['name', 'fiscal_year__name']
    ordering_fields = ['name', 'start_date', 'fiscal_year__name', 'locked', 'created_at']
    ordering = ['-fiscal_year__start_date', '-start_date'] # Default ordering

    def get_queryset(self):
        # Optimize the tenant-scoped queryset from the mixin
        return super().get_queryset().select_related('fiscal_year', 'fiscal_year__company', 'company')

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return AccountingPeriodWriteSerializer
        return AccountingPeriodReadSerializer

    # The `get_serializer` override previously here is NO LONGER NEEDED if
    # `AccountingPeriodWriteSerializer.__init__` handles filtering `fiscal_year_id`
    # queryset based on `company_context`. This makes the serializer more self-contained.

    def perform_destroy(self, instance: AccountingPeriod):
        """Soft delete the accounting period if allowed."""
        # Add checks if period can be deleted (e.g., no posted vouchers)
        # For example:
        # if instance.vouchers.filter(status='POSTED', deleted_at__isnull=True).exists():
        #     raise DjangoValidationError(_("Cannot delete period with posted vouchers. Lock it instead."))
        try:
            logger.info(f"User {self.request.user.username} (Co: {self.current_company.name}) soft deleting AcctPeriod {instance.pk} ('{instance.name}')")
            instance.delete() # This will be a soft delete if TenantScopedModel uses django-safedelete
        except models.ProtectedError as e: # Catch DB level protection if not soft-deleting or other FKs
            logger.warning(f"ProtectedError soft deleting AcctPeriod {instance.pk} for {self.current_company.name}: {e}")
            raise DjangoValidationError(_("Cannot delete this period due to existing relationships."))
        except Exception as e:
            logger.exception(f"Error soft deleting AcctPeriod {instance.pk} for {self.current_company.name}: {e}")
            raise DjangoValidationError(_("An unexpected error occurred. The period was not deleted."))


    @action(detail=True, methods=['post'], url_path='lock')
    def lock_period(self, request, pk=None):
        """Locks this accounting period."""
        period = self.get_object() # Tenant-scoped
        try:
            period.lock_period() # Model method handles internal checks
            serializer = AccountingPeriodReadSerializer(period, context=self.get_serializer_context())
            return Response(serializer.data)
        except DjangoValidationError as e:
            logger.warning(f"Validation error locking Period {pk} for Co {self.current_company.pk}: {e.messages_joined}")
            return Response({'detail': e.messages_joined}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Error locking Period {pk} for Co {self.current_company.pk}: {e}")
            return Response({'detail': _("Error locking period.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='unlock')
    def unlock_period(self, request, pk=None):
        """Unlocks this accounting period."""
        period = self.get_object() # Tenant-scoped
        try:
            period.unlock_period() # Model method handles internal checks (e.g., FY status)
            serializer = AccountingPeriodReadSerializer(period, context=self.get_serializer_context())
            return Response(serializer.data)
        except DjangoValidationError as e:
            logger.warning(f"Validation error unlocking Period {pk} for Co {self.current_company.pk}: {e.messages_joined}")
            return Response({'detail': e.messages_joined}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Error unlocking Period {pk} for Co {self.current_company.pk}: {e}")
            return Response({'detail': _("Error unlocking period.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
# from rest_framework import viewsets, status
# from rest_framework.response import Response
# from rest_framework.decorators import action
# from django.utils import timezone
# from crp_accounting.models.period import FiscalYear, AccountingPeriod
# from crp_accounting.serializers.period import FiscalYearSerializer, AccountingPeriodSerializer
#
#
# class FiscalYearViewSet(viewsets.ModelViewSet):
#     """
#     ViewSet for managing fiscal years.
#     Enforces immutability on closed years and allows soft-close logic.
#     """
#     queryset = FiscalYear.objects.all().order_by('-start_date')
#     serializer_class = FiscalYearSerializer
#
#     @action(detail=True, methods=['post'])
#     def close_year(self, request, pk=None):
#         """
#         Endpoint to soft-close a fiscal year, preventing further edits or period creation.
#         Only allowed if all accounting periods inside the year are locked.
#         """
#         fiscal_year = self.get_object()
#
#         if fiscal_year.status == 'Closed':
#             return Response({'detail': 'Fiscal year already closed.'}, status=status.HTTP_400_BAD_REQUEST)
#
#         open_periods = AccountingPeriod.objects.filter(fiscal_year=fiscal_year, is_locked=False)
#         if open_periods.exists():
#             return Response({'detail': 'Cannot close fiscal year. Some periods are still unlocked.'},
#                             status=status.HTTP_400_BAD_REQUEST)
#
#         fiscal_year.status = 'Closed'
#         fiscal_year.closed_by = request.user
#         fiscal_year.closed_at = timezone.now()
#         fiscal_year.save()
#
#         return Response({'detail': 'Fiscal year successfully closed.'})
#
#
# class AccountingPeriodViewSet(viewsets.ModelViewSet):
#     """
#     ViewSet for managing accounting periods.
#     Ensures validation of period range within fiscal year and lock behavior.
#     """
#     queryset = AccountingPeriod.objects.select_related('fiscal_year').all().order_by('-start_date')
#     serializer_class = AccountingPeriodSerializer
#
#     @action(detail=True, methods=['post'])
#     def lock(self, request, pk=None):
#         """
#         Lock an accounting period. Locked periods cannot be edited or used for future entries.
#         """
#         period = self.get_object()
#         if period.is_locked:
#             return Response({'detail': 'Accounting period is already locked.'}, status=status.HTTP_400_BAD_REQUEST)
#
#         period.is_locked = True
#         period.save()
#         return Response({'detail': 'Accounting period locked successfully.'})
