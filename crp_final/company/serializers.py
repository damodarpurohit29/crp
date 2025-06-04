# company/serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from .models import Company, CompanyGroup, CompanyMembership

User = get_user_model()


# --- User Serializer (Simplified, for embedding) ---
class BasicUserSerializer(serializers.ModelSerializer):
    """Minimal user representation for embedding in other serializers."""
    name = serializers.CharField( read_only=True)

    class Meta:
        model = User
        fields = ('id', 'name', 'email')
        read_only_fields = fields  # All fields are read-only in this basic representation


# --- CompanyGroup Serializer ---
class CompanyGroupSerializer(serializers.ModelSerializer):
    """Serializer for CompanyGroup, typically for superuser management."""
    company_count = serializers.IntegerField(source='companies.count', read_only=True)

    class Meta:
        model = CompanyGroup
        fields = ('id', 'name', 'description', 'company_count', 'created_at', 'updated_at')
        read_only_fields = ('id', 'company_count', 'created_at', 'updated_at')


# --- CompanyMembership Serializer ---
class CompanyMembershipSerializer(serializers.ModelSerializer):
    """Serializer for managing user memberships within companies."""
    user = BasicUserSerializer(read_only=True)  # Display full user details on read
    user_id = serializers.PrimaryKeyRelatedField(  # For assigning a user on create/update
        queryset=User.objects.all(),
        source='user',
        write_only=True,
        help_text=_("ID of the user for this membership.")
    )
    # company field (FK) is typically set by the view context, not directly by API client when creating/updating memberships for a specific company.
    # It will be included in read operations.
    company_name = serializers.CharField(source='company.name', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = CompanyMembership
        fields = (
            'id', 'user', 'user_id', 'company', 'company_name', 'role', 'role_display',
            'is_active_membership', 'is_default_for_user', 'can_manage_members',
            'date_joined', 'last_accessed_at', 'effective_can_access'
        )
        read_only_fields = (
        'id', 'company', 'company_name', 'date_joined', 'last_accessed_at', 'effective_can_access', 'user')
        # 'company' is read-only because it's usually contextual (e.g., /api/companies/{id}/members/)
        # 'user' (the nested object) is read-only; 'user_id' is for writing.

    def validate(self, attrs):
        """Ensures a user is not added to the same company twice via API."""
        company = attrs.get('company')  # This would be set by the view if creating
        if not company and self.instance:  # If updating, get from instance
            company = self.instance.company

        user = attrs.get('user')  # This is the User instance from user_id

        # On creation, check for existing membership
        if not self.instance and company and user:  # self.instance is None during create
            if CompanyMembership.objects.filter(company=company, user=user).exists():
                raise serializers.ValidationError(
                    _("This user is already a member of this company.")
                )
        return attrs

    # The model's save() method handles the is_default_for_user uniqueness logic.


# --- Company Serializer ---
class CompanySerializer(serializers.ModelSerializer):
    """Serializer for Company (Tenant) information."""
    effective_is_active = serializers.BooleanField(read_only=True)
    created_by_user = BasicUserSerializer(read_only=True)
    company_group_name = serializers.CharField(source='company_group.name', read_only=True, allow_null=True)

    # memberships field is often better handled by a dedicated endpoint for scalability
    # e.g., /api/companies/{id}/members/
    # If included here, ensure your ViewSet uses prefetch_related('memberships', 'memberships__user')
    # memberships = CompanyMembershipSerializer(many=True, read_only=True)

    company_group_id = serializers.PrimaryKeyRelatedField(
        queryset=CompanyGroup.objects.all(),
        source='company_group',
        required=False,  # A company doesn't have to belong to a group
        allow_null=True,
        write_only=True  # Use company_group_name for reading
    )

    class Meta:
        model = Company
        fields = (
            'id', 'name', 'subdomain_prefix', 'display_name', 'company_group_id', 'company_group_name', 'logo',
            'address_line1', 'address_line2', 'city', 'state_province_region', 'postal_code', 'country_code',
            'primary_phone', 'primary_email', 'website',
            'registration_number', 'tax_id_primary',
            'default_currency_code', 'financial_year_start_month', 'timezone_name',
            'is_active', 'is_suspended_by_admin', 'effective_is_active',
            'created_by_user', 'created_at', 'updated_at',
            # 'memberships', # Consider if needed directly here or via separate endpoint
        )
        read_only_fields = (
            'id', 'effective_is_active', 'created_by_user',
            'created_at', 'updated_at', 'company_group_name',  # 'memberships' if included
        )
        # `subdomain_prefix` should be read-only after creation, enforced in validate method or view.

    def validate_subdomain_prefix(self, value):
        """Prevents changing subdomain_prefix after company creation."""
        if self.instance and self.instance.subdomain_prefix != value:  # self.instance is the object being updated
            raise serializers.ValidationError(_("Subdomain prefix cannot be changed after company creation."))
        # Model-level validators will handle character set validation.
        return value

    def create(self, validated_data):
        """Handles company creation, including setting creator and initial membership for non-superusers."""
        request = self.context.get('request')
        user = request.user if request and hasattr(request, 'user') and request.user.is_authenticated else None

        # `company_group` is already an instance due to source='company_group' on company_group_id
        company = Company.objects.create(**validated_data)

        if user:  # If a user context is available
            if not company.created_by_user:  # Set creator if not already set (e.g. by superuser in validated_data)
                company.created_by_user = user
                # No need to save yet, will be saved after membership creation or at end if no membership.

            if not user.is_superuser:
                # If a regular user creates a company (e.g., through a signup API),
                # make them the owner/admin and set company as default.
                CompanyMembership.objects.create(
                    company=company,
                    user=user,
                    role=CompanyMembership.Role.OWNER,  # Or ADMIN, based on your SaaS logic
                    is_active_membership=True,
                    is_default_for_user=True
                )

        if company.pk and (
                not company.created_by_user or getattr(company.created_by_user, 'pk', None) != getattr(user, 'pk',
                                                                                                       None)):
            # Save if created_by_user was just set and is different or was None.
            company.save(update_fields=['created_by_user'] if company.created_by_user else None)

        return company

    # Update logic and dynamic field exposure based on user roles (superuser vs. company admin)
    # are best handled in the ViewSet (e.g., using different serializers for PATCH/PUT,
    # or by filtering `validated_data` in `perform_update`).
    # The serializer defines the complete possible data structure.