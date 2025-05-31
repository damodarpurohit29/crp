# crp_core/mixins.py
import logging
from typing import Optional, Any, Type

from django.core.exceptions import ImproperlyConfigured, FieldDoesNotExist
from django.db import models
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, viewsets, generics, status, serializers # ✅ ADDED serializers
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied, ValidationError as DRFValidationError

logger = logging.getLogger("crp_core.mixins")


# --- Company Model Import ---
try:
    from company.models import Company
except ImportError:
    Company = None  # type: ignore
    logger.critical(
        "CRP Core Mixins: CRITICAL - Could not import 'company.models.Company'. "
        "Tenant scoping features will be non-functional. Ensure 'company' app is installed and configured."
    )
    # If Company model is absolutely essential for the app to even load,
    # you might re-raise the ImportError here to make it fail fast.
    # raise # Uncomment to make app startup fail if Company cannot be imported


class CompanyContextMixin:
    """
    Establishes `self.current_company` on the view instance from `request.company`
    (which should be set by a middleware like `CompanyMiddleware`).
    Also adds this company to the serializer context as `company_context`.
    """
    current_company: Optional[Company] = None  # Attribute for the validated company for the request

    def initial(self, request: HttpRequest, *args: Any, **kwargs: Any) -> None:
        """
        Sets `self.current_company` based on `request.company`.
        Logs warnings if context is not as expected.
        """
        super().initial(request, *args, **kwargs)  # Call parent's initial

        company_from_request = getattr(request, 'company', None)
        user_for_log = request.user.name if request.user and request.user.is_authenticated else "AnonymousUser"
        view_name = self.__class__.__name__

        if Company and isinstance(company_from_request, Company):
            self.current_company = company_from_request
            # Ensure company is active for non-superusers, superusers can see all.
            if not request.user.is_superuser and not self.current_company.effective_is_active:
                logger.warning(
                    f"{view_name}: User '{user_for_log}' attempted to access inactive/suspended Company "
                    f"'{self.current_company.name}' (ID: {self.current_company.pk}). Context will be cleared."
                )
                self.current_company = None  # Clear context if company is not active for this user
            elif self.current_company:  # Log if company is valid and active (or SU access)
                logger.debug(
                    f"{view_name}: Company context set to '{self.current_company.name}' "
                    f"(ID: {self.current_company.pk}) for user '{user_for_log}'."
                )
        else:
            self.current_company = None  # Explicitly None if no valid company on request
            # Log this situation, as it's usually problematic if tenant-scoped views are reached.
            log_message = (
                f"{view_name}: No valid 'request.company' found for user '{user_for_log}'. "
                f"Value was: '{company_from_request}', Type: {type(company_from_request).__name__}. "
            )
            if not Company: log_message += "Company model itself not imported. "
            log_message += "Ensure CompanyMiddleware is active and correctly setting request.company."
            # For authenticated non-superusers, this is typically an error state.
            # For anonymous users or superusers without a specific company context, it might be expected.
            if request.user and request.user.is_authenticated and not request.user.is_superuser:
                logger.error(log_message)  # More severe for authenticated non-SUs
            else:
                logger.info(log_message)  # Informational for Anonymous or SU without specific context

    def get_serializer_context(self) -> dict:
        """
        Adds `request` and `company_context` (which is `self.current_company`)
        to the serializer context. Serializers should look for `company_context`.
        """
        context = super().get_serializer_context()
        context['request'] = self.request
        context['company_context'] = self.current_company  # Use this key consistently
        # The VoucherSerializer specifically looks for 'company_from_voucher_context'.
        # To make it generic, serializers should adapt to 'company_context'.
        # OR, we can add both if specific serializers need the old name.
        # For VoucherSerializer, which expects 'company_from_voucher_context':
        context['company_from_voucher_context'] = self.current_company  # Specifically for VoucherSerializer

        # Log what's being added for clarity during debugging
        company_name_for_log = self.current_company.name if self.current_company else "None"
        # logger.debug(
        #     f"{self.__class__.__name__}.get_serializer_context: Adding 'company_context' "
        #     f"({company_name_for_log}) to serializer context."
        # )
        return context


class BaseCompanyAccessPermission(permissions.BasePermission):
    """
    Permission class to ensure a valid and active `current_company` is set on the view
    for non-superusers. Superusers are generally permitted.
    Objects are checked to ensure they belong to the `current_company`.
    """
    message_no_company_context = _("A valid company context is required to access this resource.")
    message_company_inactive = _("Your company account is currently inactive or suspended.")
    message_object_permission_denied = _(
        "You do not have permission to access this specific object within your company.")

    def has_permission(self, request: HttpRequest, view: Any) -> bool:
        if request.user and request.user.is_superuser:
            # Superusers might still need a company context for *creating* tenant objects,
            # which perform_create handles. For viewing, they can often see all.
            # If a SU *must* act as a company, this check could be stricter.
            return True

        # Relies on CompanyContextMixin.initial() having set self.current_company on the view
        current_company_on_view: Optional[Company] = getattr(view, 'current_company', None)

        if not current_company_on_view:
            logger.warning(
                f"BaseCompanyAccessPermission: Denied for user '{request.user.name if request.user else 'Anonymous'}' "
                f"to view '{view.__class__.__name__}'. Reason: No 'current_company' on view. "
                f"Ensure CompanyMiddleware ran AND CompanyContextMixin.initial() set current_company."
            )
            self.message = self.message_no_company_context
            return False

        if not current_company_on_view.effective_is_active:  # Assumes Company has effective_is_active
            logger.warning(
                f"BaseCompanyAccessPermission: Denied for user '{request.user.name}' "
                f"to view '{view.__class__.__name__}'. Reason: Company '{current_company_on_view.name}' not active."
            )
            self.message = self.message_company_inactive
            return False
        return True

    def has_object_permission(self, request: HttpRequest, view: Any, obj: models.Model) -> bool:
        if request.user and request.user.is_superuser:
            return True  # Superusers can access any object (further checks can be in view)

        current_company_on_view: Optional[Company] = getattr(view, 'current_company', None)
        if not current_company_on_view:  # Should be caught by has_permission
            logger.error(
                f"BaseCompanyAccessPermission (Object): No 'current_company' on view for user '{request.user.name}'. This should not happen if has_permission passed.")
            return False

        # Check if object has a 'company' field that links to the Company model
        try:
            obj_company_field = obj._meta.get_field('company')
            if not (obj_company_field.is_relation and obj_company_field.remote_field.model == Company):
                logger.debug(
                    f"BaseCompanyAccessPermission (Object): Object {obj} (type {obj._meta.verbose_name}) 'company' field is not a direct FK to Company model. Permitting.")
                return True  # Not directly a tenant-scoped object by this 'company' field.
        except FieldDoesNotExist:
            logger.debug(
                f"BaseCompanyAccessPermission (Object): Object {obj} (type {obj._meta.verbose_name}) has no 'company' field. Permitting.")
            return True  # Object doesn't have a 'company' field.

        obj_actual_company = getattr(obj, 'company', None)
        if obj_actual_company != current_company_on_view:
            logger.warning(
                f"BaseCompanyAccessPermission (Object): User '{request.user.name}' (Context Co: {current_company_on_view.name}) "
                f"denied access to object '{obj}' (PK: {obj.pk}, Actual Co: {obj_actual_company.name if obj_actual_company else 'None'}) "
                f"of type {obj._meta.verbose_name}."
            )
            self.message = self.message_object_permission_denied
            return False
        return True


class CompanyScopedViewSetMixin(CompanyContextMixin, viewsets.ModelViewSet):
    """
    Base mixin for ModelViewSets that are scoped to the `current_company`.
    Includes IsAuthenticated and BaseCompanyAccessPermission.
    Relies on the model's default manager being tenant-aware (CompanyManager).
    """
    permission_classes = [permissions.IsAuthenticated, BaseCompanyAccessPermission]

    def get_queryset(self) -> models.QuerySet:
        """
        The model's default manager (`self.queryset.model.objects`) is expected to be
        a CompanyManager that filters by company for non-superusers.
        Superusers typically get an unfiltered view from the CompanyManager, or
        this method could be overridden for SUs to use Model.global_objects.all().
        """
        # This mixin ensures self.current_company is set. The CompanyManager on the model
        # should use company.utils.get_current_company() which accesses the thread-local
        # value set by the middleware (which should align with self.current_company).
        model = self.queryset.model
        # Add a check for your specific CompanyManager attribute, e.g., '_is_tenant_aware_manager'
        if not (hasattr(model.objects, '_is_tenant_aware_manager') and model.objects._is_tenant_aware_manager is True):
            logger.warning(
                f"{self.__class__.__name__}: Model '{model.__name__}' default manager might not be tenant-aware "
                f"(missing '_is_tenant_aware_manager = True' flag). Data isolation risk."
            )
        # If user is superuser AND self.current_company is None (meaning SU is not acting as a specific company)
        # AND you want SUs to see all data by default from ViewSets, the CompanyManager needs to handle this.
        # Alternatively, an SU-specific ViewSet could override get_queryset to use Model.global_objects.all().
        return super().get_queryset()

    def perform_create(self, serializer: serializers.ModelSerializer) -> None:
        """
        Sets the 'company' (and optionally 'created_by') for new objects.
        Requires `self.current_company` to be set for non-superusers.
        """
        user_for_audit = self.request.user if self.request.user.is_authenticated else None

        if self.current_company:  # If a company context is established for the request
            # Non-SU will always have current_company if BaseCompanyAccessPermission passed.
            # SU might have current_company if "acting as" or if middleware sets one for them.
            serializer.save(company=self.current_company, created_by=user_for_audit, updated_by=user_for_audit)
            logger.info(
                f"{self.__class__.__name__}: Created {serializer.Meta.model.__name__} for Company "
                f"'{self.current_company.name}' by User '{user_for_audit.name if user_for_audit else 'System'}'.")
        elif self.request.user.is_superuser:
            # Superuser creating an object without a specific `request.company` context.
            # The 'company' MUST be provided in the request data and validated by the serializer.
            # `company_from_voucher_context` in serializer context will be None.
            # The serializer's `create` method must handle fetching/assigning `company` from `validated_data`.
            logger.info(
                f"{self.__class__.__name__}: Superuser '{user_for_audit.name if user_for_audit else 'SU'}' creating "
                f"{serializer.Meta.model.__name__}. 'company' must be in validated_data."
            )
            # The serializer.validated_data['company'] will be used if present.
            # Audit fields also passed.
            serializer.save(created_by=user_for_audit, updated_by=user_for_audit)
        else:
            # This case should be blocked by BaseCompanyAccessPermission for non-SUs.
            # If reached, it's a logic error.
            logger.error(
                f"{self.__class__.__name__}: Non-superuser '{user_for_audit.name if user_for_audit else 'Unknown'}' "
                f"attempting create without current_company. This should be blocked by permissions."
            )
            raise DRFPermissionDenied(_("A valid company context is required to create this object."))

    def perform_update(self, serializer: serializers.ModelSerializer) -> None:
        """Sets 'updated_by' for existing objects."""
        user_for_audit = self.request.user if self.request.user.is_authenticated else None
        serializer.save(updated_by=user_for_audit)
        logger.info(
            f"{self.__class__.__name__}: Updated {serializer.Meta.model.__name__} "
            f"(PK: {serializer.instance.pk}, Co: {serializer.instance.company.name if serializer.instance.company else 'N/A'}) "
            f"by User '{user_for_audit.name if user_for_audit else 'System'}'."
        )


# --- Mixins for APIView and GenericAPIView ---
class CompanyScopedAPIViewMixin(CompanyContextMixin, APIView):
    permission_classes = [permissions.IsAuthenticated, BaseCompanyAccessPermission]


class CompanyScopedGenericAPIViewMixin(CompanyContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, BaseCompanyAccessPermission]