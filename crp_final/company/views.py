# company/views.py
import logging

from django.http import Http404
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
# from django.db import transaction # Not actively used but fine to keep
from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes
from django.utils.translation import gettext_lazy as _

from .models import Company, CompanyGroup, CompanyMembership # Ensure Company type hint is available
from .serializers import (
    CompanySerializer,
    CompanyGroupSerializer,
    CompanyMembershipSerializer,
    # BasicUserSerializer # Not used in this snippet, keep if used elsewhere
)

User = get_user_model()
logger = logging.getLogger(__name__)


# --- Custom Permission Classes ---
class IsSuperUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.is_superuser


class IsCompanyAdminForObject(permissions.BasePermission):
    """
    Allows access if user is superuser or an active Owner/Admin of the target company.
    The 'obj' can be a Company instance or an object with a 'company' attribute (e.g., CompanyMembership).
    """
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True

        target_company = None
        if isinstance(obj, Company):
            target_company = obj
        elif hasattr(obj, 'company') and isinstance(obj.company, Company):
            target_company = obj.company

        if not target_company:
            # Cannot determine target company from object
            return False

        return CompanyMembership.objects.filter(
            user=request.user,
            company=target_company,
            role__in=[CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN],
            is_active_membership=True,
            company__effective_is_active=True # Ensure parent company is also active
        ).exists()


class IsCompanyMemberForObject(permissions.BasePermission):
    """
    Allows access if user is superuser or an active member of the target company.
    The 'obj' can be a Company instance or an object with a 'company' attribute.
    """
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True

        target_company = None
        if isinstance(obj, Company):
            target_company = obj
        elif hasattr(obj, 'company') and isinstance(obj.company, Company):
            target_company = obj.company

        if not target_company:
            return False

        return CompanyMembership.objects.filter(
            user=request.user,
            company=target_company,
            is_active_membership=True,
            company__effective_is_active=True
        ).exists()


# --- ViewSets ---
@extend_schema_view(
    list=extend_schema(summary="List all Company Groups (Superuser only)"),
    retrieve=extend_schema(summary="Retrieve a Company Group (Superuser only)"),
    create=extend_schema(summary="Create a new Company Group (Superuser only)"),
    update=extend_schema(summary="Update a Company Group (Superuser only)"),
    partial_update=extend_schema(summary="Partially update a Company Group (Superuser only)"),
    destroy=extend_schema(summary="Delete a Company Group (Superuser only)"),
)
class CompanyGroupViewSet(viewsets.ModelViewSet):
    queryset = CompanyGroup.objects.all()
    serializer_class = CompanyGroupSerializer
    permission_classes = [IsSuperUser] # Only superusers can manage company groups


@extend_schema_view(
    list=extend_schema(summary="List Companies", description="Superusers see all. Company Admins see their own."),
    retrieve=extend_schema(summary="Retrieve a Company", description="Superusers see any. Company Members see their own."),
    create=extend_schema(summary="Create Company (Authenticated users for SaaS signup, Superusers)"),
    update=extend_schema(summary="Update Company details (Company Admins/Owners or Superusers)"),
    partial_update=extend_schema(summary="Partially update Company details (Company Admins/Owners or Superusers)"),
    destroy=extend_schema(summary="Delete Company (Superuser only)"),
)
class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Company.objects.none()
        if user.is_superuser:
            return Company.objects.all().prefetch_related('memberships__user', 'company_group')

        # Authenticated non-superusers:
        # Show companies where the user is an active Owner or Admin.
        return Company.objects.filter(
            memberships__user=user,
            memberships__role__in=[CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN],
            memberships__is_active_membership=True,
            effective_is_active=True
        ).distinct().prefetch_related('memberships__user', 'company_group')

    def get_permissions(self):
        if self.action == 'create':
            # Any authenticated user can attempt to create (e.g., SaaS signup flow).
            # The serializer/perform_create will handle linking the user.
            permission_classes = [permissions.IsAuthenticated]
        elif self.action == 'destroy':
            permission_classes = [IsSuperUser]
        elif self.action in ['update', 'partial_update']:
            # Superuser OR an Admin/Owner of that specific company object.
            permission_classes = [IsSuperUser | IsCompanyAdminForObject]
        elif self.action == 'retrieve':
            # Superuser OR any active member of that specific company object.
            permission_classes = [IsSuperUser | IsCompanyMemberForObject]
        elif self.action == 'list':
            # Authenticated users can list (queryset filters what they see).
            permission_classes = [permissions.IsAuthenticated]
        else:
            # Default for other actions (e.g., custom actions if any)
            permission_classes = [permissions.IsAdminUser] # Django's IsAdminUser (staff or superuser)
        return [permission() for permission in permission_classes]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    def perform_create(self, serializer):
        user = self.request.user
        # Serializer's create method handles setting created_by_user
        # and initial membership for non-superusers by accessing self.context['request'].user.
        if user.is_superuser and 'created_by_user' not in serializer.validated_data:
            serializer.save(created_by_user=user)
        else:
            # For non-superusers, serializer.create should handle setting created_by_user=user
            # and creating the initial Owner membership.
            serializer.save() # Relies on serializer.create() to use request.user from context

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance # The Company instance

        # Permissions are checked by get_permissions and IsCompanyAdminForObject.
        # This method handles which fields non-superusers can update.
        if user.is_superuser:
            serializer.save()
        elif IsCompanyAdminForObject().has_object_permission(self.request, self, instance):
            allowed_fields_for_client_update = getattr(self.serializer_class.Meta, 'client_writable_fields', [])
            update_data = {}
            restricted_fields_attempted = {}

            for field_name, value in serializer.validated_data.items():
                if field_name in allowed_fields_for_client_update:
                    update_data[field_name] = value
                # Check if field_name is an actual model field before getattr
                elif field_name in [f.name for f in instance._meta.get_fields()] and field_name != instance._meta.pk.name:
                    current_value = getattr(instance, field_name)
                    if current_value != value:
                        restricted_fields_attempted[field_name] = value
            if restricted_fields_attempted:
                 logger.warning(
                    f"User {user.username} (Company Admin for {instance.name}) "
                    f"attempted to update restricted fields: {restricted_fields_attempted}. Changes ignored."
                )
            if update_data:
                serializer.save(**update_data) # Save only allowed fields
            else:
                logger.info(
                    f"User {user.username} submitted update for {instance.name} with no changed allowed fields or only restricted fields.")
                # Optionally, raise ValidationError if only restricted fields were sent and no allowed fields
                # raise ValidationError(_("No updatable fields provided or only restricted fields attempted."))
        else:
            # This should ideally be caught by get_permissions
            self.permission_denied(self.request, message=_("You do not have permission to update this company."))

    @extend_schema(
        summary="List members of a specific company",
        responses={200: CompanyMembershipSerializer(many=True)}
    )
    @action(detail=True, methods=['get'], permission_classes=[IsSuperUser | IsCompanyMemberForObject])
    def members(self, request, pk=None):
        company = self.get_object() # self.get_object() handles 404 and permission checks via get_permissions
        memberships = company.memberships.filter(is_active_membership=True).select_related('user', 'company')
        serializer_instance = CompanyMembershipSerializer(memberships, many=True, context={'request': request})
        return Response(serializer_instance.data)


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='company_pk',
            location=OpenApiParameter.PATH,
            description='The primary key of the Company to which these memberships belong.',
            required=True,
            type=OpenApiTypes.INT # Or OpenApiTypes.UUID if your Company PK is UUID
        )
    ]
)
@extend_schema_view(
    list=extend_schema(summary="List Company Memberships for a Company"),
    retrieve=extend_schema(summary="Retrieve a Company Membership"),
    create=extend_schema(summary="Create a new Company Membership (Invite user)"),
    update=extend_schema(summary="Update a Company Membership (e.g., change role)"),
    partial_update=extend_schema(summary="Partially update a Company Membership"),
    destroy=extend_schema(summary="Delete a Company Membership (Remove user)"),
)
class CompanyMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyMembershipSerializer

    def _get_parent_company(self) -> Company:
        if not hasattr(self, '_parent_company_cached_instance'):
            company_pk = self.kwargs.get('company_pk')
            if not company_pk:
                logger.warning(
                    "_get_parent_company called without 'company_pk' in kwargs. "
                    "This is unexpected at runtime for methods that require it."
                )
                raise Http404("Company PK not found in URL for membership operations.")
            try:
                # Attempt to convert to int, assuming integer PKs for Company.
                # Adjust if Company PK is UUID or another type.
                company_pk_validated = int(company_pk)
                self._parent_company_cached_instance = get_object_or_404(Company, pk=company_pk_validated)
            except ValueError:
                logger.error(f"Invalid company_pk format: {company_pk}. Expected an integer.")
                raise Http404("Invalid Company PK format.")
            except Company.DoesNotExist: # get_object_or_404 will raise this
                logger.warning(f"Company with pk={company_pk} not found.")
                raise Http404("Company not found.") # Should be handled by get_object_or_404
        return self._parent_company_cached_instance

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return CompanyMembership.objects.none()

        # _get_parent_company will raise Http404 if company_pk is missing/invalid,
        # which is appropriate for get_queryset at runtime.
        parent_company = self._get_parent_company()

        # Runtime permission check: Superusers or active members of the parent company can list/view.
        can_view_memberships = user.is_superuser or \
                               IsCompanyMemberForObject().has_object_permission(self.request, self, parent_company)

        if not can_view_memberships:
            # This is a runtime check. Let DRF handle the 403 response.
            raise PermissionDenied(_("You do not have permission to view memberships for this company."))

        return CompanyMembership.objects.filter(company=parent_company).select_related('user', 'company')

    def get_permissions(self):
        company_pk_from_kwargs = self.kwargs.get('company_pk')

        if not company_pk_from_kwargs:
            # Context: Likely schema generation by `manage.py spectacular` or a similar tool
            # where full URL kwargs might not be available for early permission introspection.
            # Return a default permission set that allows schema generation to proceed.
            # The actual endpoint will be protected by URL routing (requiring company_pk)
            # and subsequent checks in get_queryset or perform_* methods at runtime.
            logger.debug(
                "CompanyMembershipViewSet.get_permissions: 'company_pk' not in kwargs. "
                "Assuming schema generation context. Using IsAuthenticated as default."
            )
            return [permissions.IsAuthenticated()]

        # If company_pk is present, proceed with normal permission logic.
        try:
            # We need the parent_company to instantiate some permission checks.
            parent_company = self._get_parent_company()
        except Http404:
            # If company_pk is present but invalid (e.g., not found, bad format),
            # _get_parent_company would have raised Http404.
            # For get_permissions, if parent_company cannot be resolved here,
            # it means the request is fundamentally flawed.
            # A restrictive permission is safest. DRF will likely return 404 anyway.
            logger.warning(
                f"CompanyMembershipViewSet.get_permissions: Could not retrieve parent company for pk {company_pk_from_kwargs}."
                " This request will likely result in a 404. Defaulting to IsAdminUser for permission check."
            )
            return [permissions.IsAdminUser()] # Or simply raise Http404 here

        # Define inline helper permission classes that operate on the resolved parent_company.
        # These are instantiated, so they need parent_company.
        class CanAccessParentCompanyMembers(IsCompanyMemberForObject):
            def has_permission(self, request, view):
                return super().has_object_permission(request, view, parent_company)

        class CanAdminParentCompanyMembers(IsCompanyAdminForObject):
            def has_permission(self, request, view):
                return super().has_object_permission(request, view, parent_company)

        # Determine permissions based on action
        if self.action in ['list', 'retrieve']:
            # For 'retrieve', IsCompanyMemberForObject will be called by DRF with the membership obj.
            # For 'list', CanAccessParentCompanyMembers (which checks parent_company) is used.
            permission_classes = [
                IsSuperUser | (CanAccessParentCompanyMembers if self.action == 'list' else IsCompanyMemberForObject)
            ]
        elif self.action in ['create', 'update', 'partial_update', 'destroy']:
            # For detail views (update, partial_update, destroy), IsCompanyAdminForObject works on the membership obj.
            # For 'create', CanAdminParentCompanyMembers (checking parent_company) is used.
            permission_classes = [
                IsSuperUser | (CanAdminParentCompanyMembers if self.action == 'create' else IsCompanyAdminForObject)
            ]
        else:
            permission_classes = [permissions.IsAdminUser] # Default for other actions

        return [permission() for permission in permission_classes]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        if 'company_pk' in self.kwargs: # For create, so serializer knows the company
            try:
                context['parent_company'] = self._get_parent_company()
            except Http404:
                 logger.warning(
                     f"get_serializer_context: Could not add 'parent_company' to context. "
                     f"company_pk '{self.kwargs.get('company_pk')}' resolution failed. "
                     "Serializer might not function as expected if it relies on parent_company."
                 )
        return context

    def perform_create(self, serializer):
        # _get_parent_company will raise Http404 if company_pk is missing/invalid at runtime.
        parent_company = self._get_parent_company()

        # Explicit runtime permission check (defense-in-depth, get_permissions should cover 'create')
        if not (self.request.user.is_superuser or
                IsCompanyAdminForObject().has_object_permission(self.request, self, parent_company)):
            self.permission_denied(self.request, _("You do not have permission to add members to this company."))

        user_to_add_id = serializer.validated_data.get('user') # Assuming serializer expects user_id
        user_to_add = get_object_or_404(User, pk=user_to_add_id.id if hasattr(user_to_add_id, 'id') else user_to_add_id)


        if CompanyMembership.objects.filter(company=parent_company, user=user_to_add).exists():
            raise ValidationError({"user": _("This user is already a member of this company.")})

        serializer.save(company=parent_company, added_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance # This is a CompanyMembership instance
        user = self.request.user
        is_self_modification = instance.user == user

        # Permissions are handled by get_permissions (IsCompanyAdminForObject for the membership).
        # Additional business logic for role/status changes:
        if is_self_modification and instance.role in [CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN]:
            new_role = serializer.validated_data.get('role', instance.role)
            new_is_active = serializer.validated_data.get('is_active_membership', instance.is_active_membership)

            # Check if admin is demoting themselves or deactivating themselves
            is_demoting_self = new_role not in [CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN]
            is_deactivating_self = not new_is_active and instance.is_active_membership

            if is_demoting_self or is_deactivating_self:
                # Check if this is the last active admin/owner
                other_active_admins = instance.company.memberships.filter(
                    role__in=[CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN],
                    is_active_membership=True
                ).exclude(pk=instance.pk) # Exclude self from the count
                if not other_active_admins.exists():
                    raise ValidationError(
                        _("Cannot change role or deactivate the sole active Owner/Administrator of the company.")
                    )
        serializer.save()

    def perform_destroy(self, instance: CompanyMembership): # Added type hint for clarity
        # Permissions are handled by get_permissions (IsCompanyAdminForObject for the membership).
        # Additional business logic for self-deletion of the sole admin:
        is_self_deletion = instance.user == self.request.user

        if is_self_deletion and \
           instance.role in [CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN] and \
           instance.is_active_membership: # Check if they are an active admin/owner

            other_active_admins = instance.company.memberships.filter(
                role__in=[CompanyMembership.Role.OWNER, CompanyMembership.Role.ADMIN],
                is_active_membership=True
            ).exclude(pk=instance.pk) # Exclude self
            if not other_active_admins.exists():
                # Using ValidationError as it's a business rule violation.
                raise ValidationError(_("Cannot delete the sole active Owner/Administrator of the company."))

        instance.delete()