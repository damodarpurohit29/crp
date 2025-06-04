# crp_accounting/views/journal.py

import logging
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.http import Http404  # Keep if used, though DRF handles some of this
from django.utils.translation import gettext_lazy as _

from rest_framework import viewsets, status, \
    permissions  # Removed: serializers (not used directly as 'serializers.Something')
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, \
    ValidationError as DRFValidationError  # Use DRF's ValidationError for consistency
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters

# Assuming crp_core.enums is directly importable
from crp_core.enums import TransactionStatus, VoucherType

# --- Model Imports (Tenant-Scoped) ---
from ..models.journal import Voucher
from ..models.coa import Account  # For filtering in serializer if needed (though ViewSet should do it)
from ..models.period import AccountingPeriod  # For filtering in serializer if needed
from ..models.party import Party  # For filtering in serializer if needed

# --- Serializer Imports (Tenant-Aware) ---
from ..serializers.journal import VoucherSerializer

# --- Service Function Imports (Tenant-Aware) ---
from ..services import voucher_service

# --- Custom Exception Imports ---
from ..exceptions import (
    VoucherWorkflowError, InvalidVoucherStatusError, PeriodLockedError,
    BalanceError, InsufficientPermissionError
)
# --- FilterSet Import (Needs to be Tenant-Aware) ---
from ..filters import VoucherFilterSet

# --- Permission Class Imports (RBAC) ---
# from ..permissions import ... # Your RBAC permission classes

# --- Base Mixin Import ---
# Ensure this path is correct and the Mixin is as defined previously
from .coa import CompanyScopedViewSetMixin  # If coa.py contains the mixin

# Or from crp_core.mixins import CompanyScopedViewSetMixin # If it's in a core app

logger = logging.getLogger("crp_accounting.views.journal")  # Specific logger


# =============================================================================
# Voucher ViewSet (Tenant Aware with RBAC)
# =============================================================================

class VoucherViewSet(CompanyScopedViewSetMixin, viewsets.ModelViewSet):
    serializer_class = VoucherSerializer
    # queryset will be set by get_queryset from CompanyScopedViewSetMixin
    # If CompanyScopedViewSetMixin sets self.queryset = self.model.objects.all(),
    # it's good practice to define a base queryset here for clarity, even if overridden.
    queryset = Voucher.objects.all()  # Base queryset, will be filtered by Mixin

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = VoucherFilterSet
    search_fields = ['voucher_number', 'narration', 'reference', 'lines__narration', 'party__name']
    ordering_fields = ['date', 'voucher_number', 'status', 'updated_at', 'created_at']
    ordering = ['-date', '-voucher_number']

    def get_queryset(self):
        """
        Extends the company-scoped queryset from the mixin with necessary
        select_related and prefetch_related for optimization.
        """
        # `super().get_queryset()` calls CompanyScopedViewSetMixin.get_queryset(),
        # which relies on Voucher.objects (CompanyManager) for tenant filtering.
        qs = super().get_queryset()
        return qs.select_related(
            'company',  # Already on Voucher, often useful
            'accounting_period__fiscal_year',  # Efficiently get period and its year
            'party',
            'created_by',  # Example: if you display creator's name
            'updated_by',
            'posted_by',
            'approved_by'
        ).prefetch_related(
            'lines__account',  # Prefetch lines and their related accounts
            'approvals__user'  # Prefetch approval logs and their users
        )

    # `get_serializer_context` is inherited from CompanyScopedViewSetMixin.
    # It already adds `request` and `company_context` (which is `self.current_company`).
    # We need to ensure `VoucherSerializer` uses `company_context` as `company_from_voucher_context`.

    def get_serializer(self, *args, **kwargs):
        """
        Override to:
        1. Inject the correct company context key for VoucherSerializer.
        2. Filter querysets for PrimaryKeyRelatedFields in write serializers.
        """
        serializer_class = self.get_serializer_class()

        # Get the base context from the mixin (includes request and current_company as 'company_context')
        context = self.get_serializer_context()  # This gets context from CompanyScopedViewSetMixin

        # VITAL: Pass the determined company context specifically for VoucherSerializer's needs.
        # VoucherSerializer expects 'company_from_voucher_context'.
        # self.current_company is set by CompanyScopedViewSetMixin.initial()
        if hasattr(self, 'current_company') and self.current_company:
            context['company_from_voucher_context'] = self.current_company
        else:
            # This case should ideally be prevented by CompanyScopedViewSetMixin.initial()
            # raising PermissionDenied if current_company cannot be determined.
            logger.error(
                f"VoucherViewSet.get_serializer: self.current_company not set for user {self.request.user}. Critical context missing.")
            # Potentially raise an error or ensure context key is None if company is truly optional.
            context['company_from_voucher_context'] = None

        kwargs['context'] = context  # Pass the augmented context to the serializer

        # Instantiate the serializer with the new context FIRST
        serializer = serializer_class(*args, **kwargs)

        # Now, filter related field querysets if it's a "write" serializer and company context exists
        # This logic is for Django Admin like behavior for browsable API or if forms use it directly.
        # DRF typically expects client to send valid PKs.
        # The serializer's own validate_ methods (e.g. validate_accounting_period) are the main gatekeeper for API.
        if isinstance(serializer, VoucherSerializer) and context.get('company_from_voucher_context'):
            company_for_filters = context['company_from_voucher_context']

            # Filter choices for related fields based on the current company.
            # This primarily affects how choices are presented in the browsable API
            # or if a frontend form dynamically fetches choices from OPTIONS.
            if 'accounting_period' in serializer.fields:
                serializer.fields['accounting_period'].queryset = AccountingPeriod.objects.filter(
                    company=company_for_filters,
                    locked=False  # Example: only show open/unlocked periods
                    # fiscal_year__status=FiscalYear.StatusChoices.OPEN # Example if you have FiscalYear status
                ).order_by('-start_date')  # Sensible ordering

            if 'party' in serializer.fields:
                serializer.fields['party'].queryset = Party.objects.filter(
                    company=company_for_filters,
                    is_active=True
                ).order_by('name')

            # For nested 'lines' (VoucherLineSerializer), its 'account' field:
            # The VoucherLineSerializer.__init__ or its field definition should handle
            # using the 'company_from_voucher_context' from its received context
            # to filter its own Account queryset if it defines one directly.
            # Or, if using default PrimaryKeyRelatedField, the context needs to be passed.
            # VoucherSerializer.__init__ passes its context to lines.
            # VoucherLineSerializer.validate_account uses this context.
            # The actual `queryset` for VoucherLineSerializer.account is best set in
            # VoucherLineSerializer.__init__ using the context, similar to how we handle it here.
            # However, for the *browsable API's* representation of the lines, this direct
            # filtering in VoucherViewSet for the line's account is not straightforward
            # because 'lines' is a ListSerializer. The filtering for line accounts happens
            # within VoucherLineSerializer itself using the passed context.

        return serializer

    # --- RBAC Permissions ---
    def get_permissions(self):
        """
        Example: Apply more granular RBAC permissions based on action.
        Replace with your actual permission classes.
        These classes should check user roles within self.current_company.
        """
        # self.current_company is available from CompanyScopedViewSetMixin
        permission_classes_to_use = list(self.permission_classes)  # Start with defaults from mixin

        # Example mapping (replace with your actual permission classes)
        # if self.action == 'submit':
        #     permission_classes_to_use.append(CanSubmitVoucherPermission)
        # elif self.action == 'approve':
        #     permission_classes_to_use.append(CanApproveVoucherPermission)
        # elif self.action == 'reject':
        #     permission_classes_to_use.append(CanRejectVoucherPermission)
        # elif self.action == 'reverse':
        #     permission_classes_to_use.append(CanReverseVoucherPermission)
        # elif self.action in ['create', 'update', 'partial_update']:
        #     permission_classes_to_use.append(CanEditDraftVoucherPermission) # Checks role and voucher status
        # elif self.action == 'destroy':
        #     permission_classes_to_use.append(CanDeleteDraftVoucherPermission)
        # Default is CanViewVoucherPermission or IsAuthenticated from mixin

        return [permission() for permission in permission_classes_to_use]

    # perform_create is inherited from CompanyScopedViewSetMixin
    # It calls serializer.save(company=self.current_company)

    def perform_destroy(self, instance: Voucher):
        """
        Deletes DRAFT or REJECTED vouchers.
        Permissions should be checked by get_permissions.
        Relies on model's delete or service layer if complex pre-delete logic needed.
        """
        # Permission checks are handled by DRF's permission_classes flow before this.
        # This method is only called if permissions allow.
        log_prefix = f"[VoucherViewSet PerformDestroy][User:{self.request.user.username}][Co:{instance.company_id}][Vch:{instance.pk}]"

        if instance.status not in [TransactionStatus.DRAFT.value, TransactionStatus.REJECTED.value]:
            logger.warning(
                f"{log_prefix} Attempt to delete non-draft/rejected Voucher (Status: {instance.get_status_display()}).")
            # This should ideally be caught by a permission class.
            raise DRFValidationError(  # Use DRF's ValidationError for API responses
                {"detail": _("Only 'Draft' or 'Rejected' vouchers can be deleted via this endpoint.")},
                code='delete_not_allowed_status'
            )

        # If there's complex pre-delete logic (e.g., unlinking, logging), call a service.
        # For simple soft-delete or hard-delete, model's delete() is fine.
        # voucher_service.delete_draft_voucher(company_id=instance.company_id, voucher_id=instance.pk, user=self.request.user)
        logger.info(f"{log_prefix} Deleting Voucher '{instance.voucher_number or instance.pk}'.")
        instance.delete()  # This will perform soft-delete if TenantScopedModel handles it.

    # --- Custom Workflow Actions ---
    # These actions call the tenant-aware voucher_service functions.
    # The service functions handle internal RBAC and business logic.

    def _call_service_for_action(self, service_function, user_param_name: str, pk=None, extra_params=None):
        """Helper to call a voucher service function and handle responses."""
        obj = self.get_object()  # Ensures object exists and user has base permission (company scope)
        # self.current_company is set by CompanyScopedViewSetMixin
        if not self.current_company or obj.company_id != self.current_company.id:
            # This is a sanity check; get_object() should already scope to current_company for non-SUs
            logger.error(
                f"Service call aborted: Company mismatch for Vch {obj.pk}. ObjCo: {obj.company_id}, CtxCo: {self.current_company.id if self.current_company else 'None'}")
            raise PermissionDenied(_("Operation not allowed due to company context mismatch."))

        log_prefix = f"[VoucherViewSet Action:{service_function.__name__}][User:{self.request.user.username}][Co:{self.current_company.id}][Vch:{obj.pk}]"

        service_kwargs = {
            'company_id': self.current_company.id,
            'voucher_id': obj.pk,
            user_param_name: self.request.user,
        }
        if extra_params: service_kwargs.update(extra_params)

        try:
            updated_obj = service_function(**service_kwargs)
            serializer = self.get_serializer(updated_obj)  # Use appropriate read serializer for response
            return Response(serializer.data, status=status.HTTP_200_OK)
        except (InvalidVoucherStatusError, PeriodLockedError, BalanceError, VoucherWorkflowError, DjangoValidationError,
                ObjectDoesNotExist) as e:
            error_detail = getattr(e, 'message_dict', None) or getattr(e, 'messages', [str(e)])
            if isinstance(error_detail, list): error_detail = ", ".join(error_detail)  # Flatten list of messages
            logger.warning(f"{log_prefix} Service call failed: {error_detail}")
            return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientPermissionError as e:  # Service layer's RBAC
            logger.warning(f"{log_prefix} Permission denied by service: {e}")
            raise PermissionDenied(str(e))  # Propagate as DRF's PermissionDenied
        except Exception as e:
            logger.exception(f"{log_prefix} Unexpected error during service call: {e}")
            return Response({"detail": _("An unexpected server error occurred.")},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='submit')
    def submit(self, request, pk=None):
        return self._call_service_for_action(
            voucher_service.submit_voucher_for_approval,
            user_param_name='submitted_by_user',
            pk=pk
        )

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        comments = request.data.get('comments', f"Approved via API by {request.user.get_username()}")
        return self._call_service_for_action(
            voucher_service.approve_and_post_voucher,
            user_param_name='approver_user',
            pk=pk,
            extra_params={'comments': comments}
        )

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        comments = request.data.get('comments')
        if not comments or not str(comments).strip():  # Ensure comments is not just whitespace
            # Use DRFValidationError for consistent API error response structure
            raise DRFValidationError({"comments": [_("Rejection comments are mandatory.")]}, code='comments_required')

        return self._call_service_for_action(
            voucher_service.reject_voucher,
            user_param_name='rejecting_user',
            pk=pk,
            extra_params={'comments': str(comments).strip()}
        )

    @action(detail=True, methods=['post'], url_path='reverse')
    def reverse(self, request, pk=None):
        reversal_date_str = request.data.get('reversal_date')
        reversal_voucher_type_value = request.data.get('reversal_voucher_type', VoucherType.GENERAL.value)
        post_immediately = request.data.get('post_immediately', False)
        reversal_date = None

        if reversal_date_str:
            try:
                from datetime import date as py_date  # Alias for clarity
                reversal_date = py_date.fromisoformat(reversal_date_str)
            except ValueError:
                raise DRFValidationError({"reversal_date": [_("Invalid date format. Use YYYY-MM-DD.")]},
                                         code='invalid_date')

        # Call service. The create_reversing_voucher returns the NEW reversing voucher.
        # The serializer for the response should be for this new voucher.
        # This means _call_service_for_action needs slight adaptation if the returned object is different.
        # For now, assuming service returns the updated *original* or the *new* one, and serializer handles it.
        # A more explicit way would be to handle the response serialization directly here if it's the new voucher.
        try:
            original_voucher = self.get_object()  # Get original voucher to pass its PK
            # current_company is from CompanyScopedViewSetMixin
            if not self.current_company or original_voucher.company_id != self.current_company.id:
                raise PermissionDenied(_("Company context mismatch for reversal."))

            reversing_voucher = voucher_service.create_reversing_voucher(
                company_id=self.current_company.id,
                original_voucher_id=original_voucher.pk,
                user=request.user,
                reversal_date=reversal_date,
                reversal_voucher_type_value=reversal_voucher_type_value,  # Pass value
                post_immediately=post_immediately
            )
            # Serialize the NEW reversing voucher for the response
            serializer = self.get_serializer(reversing_voucher)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except (InvalidVoucherStatusError, PeriodLockedError, BalanceError, VoucherWorkflowError, DjangoValidationError,
                ObjectDoesNotExist) as e:
            error_detail = getattr(e, 'message_dict', None) or getattr(e, 'messages', [str(e)])
            if isinstance(error_detail, list): error_detail = ", ".join(error_detail)
            logger.warning(
                f"[VoucherViewSet Action:reverse][User:{self.request.user.username}][Co:{self.current_company.id if self.current_company else 'N/A'}][Vch:{pk}] Service call failed: {error_detail}")
            return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientPermissionError as e:
            logger.warning(
                f"[VoucherViewSet Action:reverse][User:{self.request.user.username}][Co:{self.current_company.id if self.current_company else 'N/A'}][Vch:{pk}] Permission denied by service: {e}")
            raise PermissionDenied(str(e))
        except Exception as e:
            logger.exception(
                f"[VoucherViewSet Action:reverse][User:{self.request.user.username}][Co:{self.current_company.id if self.current_company else 'N/A'}][Vch:{pk}] Unexpected error: {e}")
            return Response({"detail": _("An unexpected server error occurred during reversal.")},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#import logging
# from rest_framework import viewsets, status, permissions, serializers
# from rest_framework.decorators import action
# from rest_framework.response import Response
# from django.core.exceptions import ValidationError as DjangoValidationError
# from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
# from django.http import Http404
# from django_filters.rest_framework import DjangoFilterBackend # For filtering
# from rest_framework import filters # For search and ordering
# from django.utils.translation import gettext_lazy as _
# from crp_core.enums import TransactionStatus, VoucherType
# # --- Model Imports ---
# from ..models.journal import Voucher
#
# # --- Serializer Imports ---
# from ..serializers import VoucherSerializer
#
# # --- Service Function Imports ---
# from ..services import voucher_service
#
#
#
# # --- Custom Exception Imports ---
# from ..exceptions import (
#     VoucherWorkflowError, InvalidVoucherStatusError, PeriodLockedError,
#     BalanceError, InsufficientPermissionError
# )
# # --- FilterSet Import ---
# from ..filters import VoucherFilterSet
#
# # --- Permission Class Imports ---
# from ..permissions import (
#     CanViewVoucher, CanManageDraftVoucher, CanSubmitVoucher,
#     CanApproveVoucher, CanRejectVoucher, CanReverseVoucher
# )
#
# logger = logging.getLogger(__name__)
#
# # =============================================================================
# # Voucher ViewSet (Updated)
# # =============================================================================
#
# class VoucherViewSet(viewsets.ModelViewSet):
#     """API endpoint for managing Vouchers."""
#     serializer_class = VoucherSerializer
#     # Set default permission - Can only view unless other permissions grant more
#     # permission_classes = [CanViewVoucher] # Base permission
#     permission_classes = [permissions.IsAuthenticated]
#
#     # --- Filtering, Searching, Ordering ---
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#     filterset_class = VoucherFilterSet # Use the defined FilterSet
#     search_fields = ['voucher_number', 'narration', 'reference', 'lines__narration', 'party__name']
#     ordering_fields = ['date', 'voucher_number', 'status', 'updated_at', 'created_at']
#     ordering = ['-date', '-voucher_number']
#
#     def get_queryset(self):
#         """Optimize queryset fetching."""
#         # Base queryset (permissions applied later by DRF)
#         queryset = Voucher.objects.all().select_related(
#             'accounting_period', 'party'
#         ).prefetch_related(
#             'lines__account', 'approvals__user'
#         )
#         return queryset
#
#     def get_serializer_context(self):
#         context = super().get_serializer_context()
#         context['request'] = self.request
#         return context
#
#     def get_permissions(self):
#         """
#         Instantiate and return the list of permissions that this view requires,
#         potentially varying by action.
#         """
#         if self.action == 'list':
#             # Anyone authenticated can list (if CanViewVoucher allows)
#             # permission_classes = [CanViewVoucher]
#             permission_classes = [permissions.IsAuthenticated]
#         elif self.action == 'create':
#
#             # permission_classes = [CanManageDraftVoucher] # Checks 'add_voucher'
#             permission_classes = [permissions.IsAuthenticated]
#         elif self.action in ['update', 'partial_update', 'destroy', 'retrieve']:
#             # Requires object-level checks handled within CanManageDraftVoucher
#             # permission_classes = [CanManageDraftVoucher]
#             permission_classes = [permissions.IsAuthenticated]
#         # Custom actions will have permissions set via decorator or implicitly inherit
#         else:
#              # permission_classes = self.permission_classes # Default defined on class
#              permission_classes = [permissions.IsAuthenticated]
#
#         return [permission() for permission in permission_classes]
#
#     # --- Overriding Standard Methods (No change needed here, handled by permissions) ---
#     def perform_create(self, serializer):
#         try:
#             # serializer.save(created_by=self.request.user) # If you have created_by
#             serializer.save()
#             logger.info(f"Voucher created by User {self.request.user.email}")
#         except DjangoValidationError as e:
#             raise serializers.ValidationError(e.message_dict if hasattr(e, 'message_dict') else str(e))
#
#     def perform_update(self, serializer):
#         try:
#             # serializer.save(updated_by=self.request.user) # If you have updated_by
#             serializer.save()
#             logger.info(f"Voucher {serializer.instance.pk} updated by User {self.request.user.email}")
#         except DjangoValidationError as e:
#             raise serializers.ValidationError(e.message_dict if hasattr(e, 'message_dict') else str(e))
#
#     def perform_destroy(self, instance):
#         # Permission check happens via get_permissions() and CanManageDraftVoucher
#         logger.warning(f"Attempting deletion: Voucher {instance.pk} by User {self.request.user.email}")
#         instance.delete() # Permission class already validated state/permission
#         logger.info(f"Deletion successful: Voucher {instance.pk} by User {self.request.user.email}")
#
#
#     # --- Custom Workflow Actions (Apply specific permissions) ---
#
#     @action(detail=True, methods=['post'], url_path='submit',permission_classes = [permissions.IsAuthenticated])
#             # permission_classes=[CanSubmitVoucher]) # Specific permission
#     def submit(self, request, pk=None):
#         """Submits a DRAFT voucher for approval."""
#         try:
#             voucher = self.get_object()
#             updated_voucher = voucher_service.submit_voucher_for_approval(
#                 voucher_id=voucher.pk, submitted_by_user=request.user
#             )
#             serializer = self.get_serializer(updated_voucher)
#             return Response(serializer.data, status=status.HTTP_200_OK)
#         # --- Error Handling (keep as before, InsufficientPermissionError caught automatically by DRF) ---
#         except (Http404):
#             return Response({"detail": _("Voucher not found.")}, status=status.HTTP_404_NOT_FOUND)
#         except (InvalidVoucherStatusError, PeriodLockedError, BalanceError, VoucherWorkflowError, DjangoValidationError) as e:
#             error_detail = e.message_dict if hasattr(e, 'message_dict') else str(e)
#             return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
#         except Exception as e:
#             logger.exception(f"Unexpected error submitting Voucher {pk}: {e}")
#             return Response({"detail": _("An unexpected server error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#
#     @action(detail=True, methods=['post'], url_path='approve',
#             permission_classes=[CanApproveVoucher]) # Specific permission
#     def approve(self, request, pk=None):
#         """Approves a voucher and posts it."""
#         comments = request.data.get('comments', "")
#         try:
#             voucher = self.get_object()
#             posted_voucher = voucher_service.approve_and_post_voucher(
#                 voucher_id=voucher.pk, approver_user=request.user, comments=comments
#             )
#             serializer = self.get_serializer(posted_voucher)
#             return Response(serializer.data, status=status.HTTP_200_OK)
#         # --- Error Handling ---
#         except (Http404):
#             return Response({"detail": _("Voucher not found.")}, status=status.HTTP_404_NOT_FOUND)
#         except (InvalidVoucherStatusError, PeriodLockedError, BalanceError, VoucherWorkflowError, DjangoValidationError) as e:
#             error_detail = e.message_dict if hasattr(e, 'message_dict') else str(e)
#             return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
#         except Exception as e:
#             logger.exception(f"Unexpected error approving Voucher {pk}: {e}")
#             return Response({"detail": _("An unexpected server error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#
#     @action(detail=True, methods=['post'], url_path='reject',
#             permission_classes=[CanRejectVoucher]) # Specific permission
#     def reject(self, request, pk=None):
#         """Rejects a PENDING_APPROVAL voucher."""
#         comments = request.data.get('comments')
#         if not comments or not comments.strip():
#             return Response({"comments": [_("Rejection comments are mandatory.")]}, status=status.HTTP_400_BAD_REQUEST)
#         try:
#             voucher = self.get_object()
#             rejected_voucher = voucher_service.reject_voucher(
#                 voucher_id=voucher.pk, rejecting_user=request.user, comments=comments
#             )
#             serializer = self.get_serializer(rejected_voucher)
#             return Response(serializer.data, status=status.HTTP_200_OK)
#         # --- Error Handling ---
#         except (Http404):
#             return Response({"detail": _("Voucher not found.")}, status=status.HTTP_404_NOT_FOUND)
#         except (InvalidVoucherStatusError, VoucherWorkflowError, DjangoValidationError) as e:
#             error_detail = e.message_dict if hasattr(e, 'message_dict') else str(e)
#             return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
#         except Exception as e:
#             logger.exception(f"Unexpected error rejecting Voucher {pk}: {e}")
#             return Response({"detail": _("An unexpected server error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#
#
#     @action(detail=True, methods=['post'], url_path='reverse',
#             permission_classes=[CanReverseVoucher]) # Specific permission
#     def reverse(self, request, pk=None):
#         """Creates a reversing entry for a POSTED voucher."""
#         reversal_date_str = request.data.get('reversal_date')
#         reversal_voucher_type = request.data.get('reversal_voucher_type', VoucherType.GENERAL)
#         post_immediately = request.data.get('post_immediately', False)
#         reversal_date = None
#         if reversal_date_str:
#             try:
#                 from datetime import date
#                 reversal_date = date.fromisoformat(reversal_date_str)
#             except ValueError:
#                  return Response({"reversal_date": [_("Invalid date format. Use YYYY-MM-DD.")]}, status=status.HTTP_400_BAD_REQUEST)
#
#         try:
#             original_voucher = self.get_object()
#             reversing_voucher = voucher_service.create_reversing_voucher(
#                 original_voucher_id=original_voucher.pk, user=request.user,
#                 reversal_date=reversal_date, reversal_voucher_type=reversal_voucher_type,
#                 post_immediately=post_immediately
#             )
#             serializer = self.get_serializer(reversing_voucher)
#             return Response(serializer.data, status=status.HTTP_201_CREATED)
#         # --- Error Handling ---
#         except (Http404):
#              return Response({"detail": _("Original voucher to reverse not found.")}, status=status.HTTP_404_NOT_FOUND)
#         except (InvalidVoucherStatusError, PeriodLockedError, BalanceError, VoucherWorkflowError, DjangoValidationError) as e:
#              error_detail = e.message_dict if hasattr(e, 'message_dict') else str(e)
#              return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
#         except Exception as e:
#              logger.exception(f"Unexpected error creating reversal for Voucher {pk}: {e}")
#              return Response({"detail": _("An unexpected server error occurred.")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
# #
# from rest_framework import viewsets, status
# from rest_framework.response import Response
# from rest_framework.permissions import IsAuthenticated
# from django.db import transaction
# from django.utils.translation import gettext_lazy as _
# 
# from crp_accounting.models.journal import JournalEntry
# from crp_accounting.models.period import AccountingPeriod
# from crp_accounting.serializers.journal import JournalEntrySerializer
# from crp_core.enums import DrCrType
# 
# 
# class JournalEntryViewSet(viewsets.ModelViewSet):
#     """
#     ViewSet to manage journal entries.
#     Implements create, list, retrieve, update, and delete with full accounting principles.
#     """
#     queryset = JournalEntry.objects.prefetch_related('lines').select_related('accounting_period').all()
#     serializer_class = JournalEntrySerializer
#     permission_classes = [IsAuthenticated]
# 
#     def create(self, request, *args, **kwargs):
#         """
#         Custom create logic with double-entry validation and real-time balance update.
#         Includes check for locked accounting periods.
#         """
#         serializer = self.get_serializer(data=request.data)
#         serializer.is_valid(raise_exception=True)
# 
#         accounting_period = serializer.validated_data.get('accounting_period')
#         if accounting_period and accounting_period.locked:
#             return Response(
#                 {'error': _("Cannot create entry. The selected accounting period is locked.")},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
# 
#         try:
#             with transaction.atomic():
#                 journal_entry = serializer.save()
#                 return Response(
#                     self.get_serializer(journal_entry).data,
#                     status=status.HTTP_201_CREATED
#                 )
#         except Exception as e:
#             return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
# 
#     def update(self, request, *args, **kwargs):
#         """
#         Full update with account balance recalculation.
#         Includes lock check to prevent modification in locked periods.
#         """
#         partial = kwargs.pop('partial', False)
#         instance = self.get_object()
# 
#         serializer = self.get_serializer(instance, data=request.data, partial=partial)
#         serializer.is_valid(raise_exception=True)
# 
#         # Check current or new accounting period is locked
#         new_period = serializer.validated_data.get('accounting_period') or instance.accounting_period
#         if new_period and new_period.locked:
#             return Response(
#                 {'error': _("This accounting period is locked. Cannot update journal entry.")},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
# 
#         try:
#             with transaction.atomic():
#                 updated_instance = serializer.save()
#                 return Response(
#                     self.get_serializer(updated_instance).data,
#                     status=status.HTTP_200_OK
#                 )
#         except Exception as e:
#             return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
# 
#     def destroy(self, request, *args, **kwargs):
#         """
#         Deletes journal entry and rolls back balances.
#         Prevent deletion if the accounting period is locked.
#         """
#         instance = self.get_object()
# 
#         if instance.accounting_period and instance.accounting_period.locked:
#             return Response(
#                 {'error': _("This accounting period is locked. Cannot delete journal entry.")},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
# 
#         try:
#             with transaction.atomic():
#                 for line in instance.lines.all():
#                     account = line.account
#                     if line.dr_cr == DrCrType.DEBIT.name:
#                         account.balance -= line.amount
#                     else:
#                         account.balance += line.amount
#                     account.save()
# 
#                 instance.delete()
#                 return Response(status=status.HTTP_204_NO_CONTENT)
# 
#         except Exception as e:
#             return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
