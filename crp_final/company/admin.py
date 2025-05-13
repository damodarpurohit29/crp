# company/admin.py
import logging
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.utils import timezone  # For admin actions updating timestamps

# Import your models
from .models import Company, CompanyGroup, CompanyMembership

# Assuming CurrencyType is used in the model for choices, and might be needed for type checks if any
# from crp_core.enums import CurrencyType # Not strictly needed in admin.py unless doing specific checks

logger = logging.getLogger("company.admin")  # Specific logger for this admin module


# =============================================================================
# CompanyGroup Admin (Primarily for Superusers)
# =============================================================================
@admin.register(CompanyGroup)
class CompanyGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'description_short', 'company_count_display', 'created_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('name', 'description')}),
        (_('Timestamps'), {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def description_short(self, obj: CompanyGroup) -> str:
        if obj.description and len(obj.description) > 75:
            return obj.description[:72] + "..."
        return obj.description or "—"

    description_short.short_description = _('Description')

    def company_count_display(self, obj: CompanyGroup) -> int:
        return obj.companies.count()  # Assumes related_name='companies'

    company_count_display.short_description = _('No. of Companies')

    def has_module_permission(self, request) -> bool:
        return request.user.is_superuser


# =============================================================================
# CompanyMembership Inline (for CompanyAdmin)
# =============================================================================
class CompanyMembershipInlineForCompanyAdmin(admin.TabularInline):
    model = CompanyMembership
    extra = 1
    autocomplete_fields = ['user']
    fields = ('user', 'role', 'is_active_membership', 'can_manage_members')
    verbose_name = _("User Access")
    verbose_name_plural = _("Manage User Access for this Company")

    def get_readonly_fields(self, request, obj=None):  # obj is the parent Company
        if obj and obj.pk:  # If editing an existing Company
            return ('user',)  # User in an existing membership cannot be changed, delete and re-add
        return ()

    def get_formset(self, request, obj=None, **kwargs):
        # Pass parent Company object to the formset if needed by custom forms.
        # request._company_admin_parent_obj is set by CompanyAdmin.get_inlines/get_fieldsets
        return super().get_formset(request, obj, **kwargs)

    # Your permission logic for the inline - seems complex and specific, assuming correct
    def has_add_permission(self, request, obj=None) -> bool:  # obj is the parent Company
        if request.user.is_superuser: return True
        if obj is None: return False  # Cannot add members if company isn't defined yet
        return CompanyMembership.objects.filter(
            user=request.user, company=obj,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True, company__effective_is_active=True
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:  # obj is CompanyMembership instance
        if request.user.is_superuser: return True
        parent_company_being_edited = getattr(request, '_company_admin_parent_obj', None)
        if obj is None:  # Checking permission for adding new memberships within the inline
            return self.has_add_permission(request,
                                           parent_company_being_edited) if parent_company_being_edited else False
        # Check if current user is Owner/Admin of the membership's company
        return CompanyMembership.objects.filter(
            user=request.user, company=obj.company,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True
        ).exists()

    def has_delete_permission(self, request, obj=None) -> bool:  # obj is CompanyMembership instance
        if request.user.is_superuser: return True
        if obj is None:
            parent_company_being_edited = getattr(request, '_company_admin_parent_obj', None)
            return self.has_add_permission(request,
                                           parent_company_being_edited) if parent_company_being_edited else False

        can_change_this_membership = self.has_change_permission(request, obj)
        if not can_change_this_membership: return False

        if obj.user == request.user and obj.role in [CompanyMembership.Role.OWNER.value,
                                                     CompanyMembership.Role.ADMIN.value]:
            other_admins_or_owners = obj.company.memberships.filter(
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exclude(pk=obj.pk)
            if not other_admins_or_owners.exists():
                # messages.error(request, _("Cannot delete own membership: sole admin/owner.")) # Message in form save might be better
                return False
        return True


# =============================================================================
# Company Admin (Tenant Entity Admin)
# =============================================================================
@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display_superuser = (
        'name', 'subdomain_prefix', 'primary_email', 'default_currency_code', 'country_code',  # Added currency
        'effective_is_active_display', 'company_group', 'created_at'
    )
    list_display_client_placeholder = ('display_name', 'primary_email', 'website', 'default_currency_code',
                                       'effective_is_active_display')  # Added currency

    list_filter_superuser = (
        'is_active', 'is_suspended_by_admin', 'country_code', 'company_group',
        'default_currency_code', 'financial_year_start_month', 'timezone_name'  # Added currency
    )
    search_fields = (
    'name', 'subdomain_prefix', 'display_name', 'primary_email', 'registration_number', 'tax_id_primary')

    # Readonly fields are dynamically set in get_readonly_fields
    autocomplete_fields = ['company_group', 'created_by_user']  # created_by_user might be set in save_model for new
    actions = ['admin_action_make_companies_active', 'admin_action_make_companies_inactive',
               'admin_action_suspend_companies', 'admin_action_unsuspend_companies']

    # Ensure default_currency_code (and optional default_currency_symbol) are in the fieldsets
    # to be part of the form.
    fieldsets_client_profile = (
        (_('My Company Profile'), {'fields': (
        'display_name', 'logo', ('primary_email', 'primary_phone'), 'website', 'internal_company_code_display')}),
        (_('Registered Address'), {'classes': ('collapse',), 'fields': (
        'address_line1', 'address_line2', 'city', 'state_province_region', 'postal_code', 'country_code')}),
        (_('Operational Settings'), {'classes': ('collapse',), 'fields': (
        'default_currency_code', 'default_currency_symbol', 'financial_year_start_month', 'timezone_name')}),
        # default_currency_symbol added
        (_('Tax Information'), {'classes': ('collapse',), 'fields': ('registration_number', 'tax_id_primary')}),
        (_('Account Status'), {'fields': ('effective_is_active_display_form',)}),
    )
    fieldsets_superuser_platform_admin = (
        (_('Platform Administration & SaaS Settings'), {'fields': (
        'name', 'subdomain_prefix', 'internal_company_code', 'company_group', 'is_active', 'is_suspended_by_admin',
        'created_by_user')}),
    )
    # Simplified combined fieldsets to ensure all relevant fields are included for SU
    # This structure assumes client_profile fieldsets define all user-editable company data
    fieldsets_superuser_combined = (
        (_('Platform Administration & SaaS Settings'), {'fields': (
        'name', 'subdomain_prefix', 'internal_company_code', 'company_group', 'is_active', 'is_suspended_by_admin',
        'created_by_user')}),
        (_('Company Client-Visible Profile'),
         {'fields': ('display_name', 'logo', ('primary_email', 'primary_phone'), 'website')}),
        # Removed internal_company_code_display as internal_company_code is editable by SU
        (_('Company Client-Visible Address'), {'fields': (
        'address_line1', 'address_line2', 'city', 'state_province_region', 'postal_code', 'country_code')}),
        (_('Company Client-Visible Operational'), {'fields': (
        'default_currency_code', 'default_currency_symbol', 'financial_year_start_month', 'timezone_name')}),
        (_('Company Client-Visible Tax Info'), {'fields': ('registration_number', 'tax_id_primary')}),
        (_('Audit Trail'),
         {'fields': ('created_at', 'updated_at', 'created_by_user_display'), 'classes': ('collapse',)}),
    )

    # --- Custom Display Methods ---
    def effective_is_active_display(self, obj: Company) -> str:
        if obj.effective_is_active:
            return format_html('<span style="color: green;">✅ {}</span>', _('Active'))
        elif obj.is_suspended_by_admin:
            return format_html('<span style="color: red;">❌ {}</span>', _('Suspended'))
        return format_html('<span style="color: orange;">➖ {}</span>', _('Inactive'))

    effective_is_active_display.short_description = _('Effective Status')
    effective_is_active_display.admin_order_field = 'is_active'

    def effective_is_active_display_form(self, obj: Company) -> str:  # For readonly display in form
        if obj is None or not hasattr(obj, 'effective_is_active'): return "---"
        if obj.effective_is_active:
            return _('✅ Account is active.')
        elif obj.is_suspended_by_admin:
            return _('❌ Account Suspended by Admin.')
        return _('➖ Account Inactive.')

    effective_is_active_display_form.short_description = _('Current Account Status')

    def created_by_user_display(self, obj: Company) -> str:
        return obj.created_by_user.get_name() if obj.created_by_user else _("System/N/A")

    created_by_user_display.short_description = _('Registered By')
    created_by_user_display.admin_order_field = 'created_by_user__name'  # Ensure user model has name

    def internal_company_code_display(self, obj: Company) -> str:  # For readonly display
        return obj.internal_company_code or "---"

    internal_company_code_display.short_description = _('Company Code (Internal)')

    # --- Dynamic Admin Configurations ---
    def get_list_display(self, request):
        return self.list_display_superuser if request.user.is_superuser else self.list_display_client_placeholder

    def get_list_filter(self, request):
        return self.list_filter_superuser if request.user.is_superuser else ()  # Client admins likely don't filter global list

    def get_readonly_fields(self, request, obj=None):
        ro_fields = set()  # Start with a set to avoid duplicates
        is_su = request.user.is_superuser

        # Base readonly fields from Django's ModelAdmin (like 'pk' if not editable)
        ro_fields.update(super().get_readonly_fields(request, obj) or [])

        # Common audit fields are always readonly
        ro_fields.update(['created_at', 'updated_at', 'created_by_user_display', 'effective_is_active_display_form'])

        if is_su:
            if obj and obj.pk:  # If editing existing company as SU
                ro_fields.add('created_by_user')  # Typically not changed after creation
        else:  # Client admin
            ro_fields.update([
                'name', 'subdomain_prefix', 'company_group', 'internal_company_code',
                'is_active', 'is_suspended_by_admin', 'created_by_user',
                # Client admin should not change platform/SaaS settings directly
            ])
            # If default_currency_symbol is auto-derived, make it readonly for client.
            # If they can set it manually and it's not auto-derived, remove this.
            if hasattr(self.model, 'default_currency_symbol'):
                ro_fields.add('default_currency_symbol')

        if obj and obj.pk:  # For any existing object
            if 'subdomain_prefix' not in ro_fields: ro_fields.add(
                'subdomain_prefix')  # Usually not changed after creation
            # `internal_company_code` is editable for SU as per fieldsets, readonly for client.

        # Ensure display-only version of internal code is readonly
        if 'internal_company_code_display' not in ro_fields:
            ro_fields.add('internal_company_code_display')

        return tuple(ro_fields)

    def get_fieldsets(self, request, obj=None):
        # Store parent object on request for inlines to use for context
        request._company_admin_parent_obj = obj if obj else None
        if request.user.is_superuser:
            return self.fieldsets_superuser_combined
        return self.fieldsets_client_profile

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('company_group', 'created_by_user')
        if request.user.is_superuser:
            return qs
        # Non-SUs (client admins) can only see/manage companies they are Owner or Admin of.
        user_administered_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True  # <<< CORRECTED: Use the actual database field
        ).values_list('company_id', flat=True)
        return qs.filter(pk__in=list(user_administered_company_ids)) # Ensure list for safety if queryset is large

    def get_inlines(self, request, obj):  # obj is the Company instance
        # request._company_admin_parent_obj is set in get_fieldsets
        if obj is None:  # No inlines on "add" page for Company
            return []

        if request.user.is_superuser:
            return [CompanyMembershipInlineForCompanyAdmin]

        # Client admin (Owner/Admin of the company being viewed) can manage members
        # request.company from middleware might be the company they are "acting within"
        # obj is the company record being viewed/edited on this admin page
        if getattr(request, 'company', None) == obj and \
                CompanyMembership.objects.filter(
                    user=request.user, company=obj,
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True  # User's membership for this company must be active
                ).exists():
            return [CompanyMembershipInlineForCompanyAdmin]
        return []

    # --- Permission Methods ---
    def has_add_permission(self, request) -> bool:
        return request.user.is_superuser  # Only SUs can create new Company tenants via this global admin

    def has_delete_permission(self, request, obj=None) -> bool:
        # Deleting a company is a major operation. Usually restricted to SUs and might involve
        # a "soft delete" or "archive" process rather than true DB deletion.
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:  # Viewing/editing an existing company
            # Client admin can change their own company if their membership is Owner/Admin,
            # their membership is active, AND the company itself is effectively active.
            return CompanyMembership.objects.filter(
                user=request.user, company=obj,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists() and obj.effective_is_active
        return False  # Cannot change if no object (e.g. on general list view if not SU)

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True

        # Check if the current user has any active membership in any effectively active company.
        # This is a base check: if they have no such memberships, they can't view anything in this admin.
        base_access_qs = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            company__is_active=True  # <<< CORRECTED HERE
        )
        if not base_access_qs.exists():
            return False

        if obj is not None:  # Viewing a specific CompanyMembership detail page
            # User can view their own membership.
            if obj.user_id == request.user.pk:
                # Ensure the company of the membership being viewed is also active
                return obj.company.is_active  # Use the direct field

            # User can view other memberships if they are an Owner/Admin of THAT membership's company.
            can_manage_members_of_obj_company = base_access_qs.filter(
                company=obj.company,  # The company of the membership being viewed
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value]
            ).exists()
            if can_manage_members_of_obj_company:
                return True

            # Optional: Allow viewing colleagues within the same company if desired (adjust this rule if too broad)
            # This means if a user is an active member of obj.company (in any role), they can see other members of that company.
            # is_member_of_obj_company = base_access_qs.filter(company=obj.company).exists()
            # if is_member_of_obj_company:
            #     return True

            return False  # Default to deny if none of the above conditions for viewing a specific object are met

        # For the CompanyMembership list view (obj is None):
        # Allow access if the user is an Owner/Admin of at least one active company,
        # as their queryset will be filtered to only show memberships they can manage.
        return base_access_qs.filter(
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value]
        ).exists()
    # --- SAVE MODEL WITH DETAILED LOGGING ---
    def save_model(self, request, obj: Company, form, change):
        """
        Save the Company instance. Includes detailed logging to trace default_currency_code.
        """
        action = "Changing" if change else "Adding"
        log_prefix = f"CompanyAdmin SaveModel (User:{request.user.name}, Action:{action}, PK:{obj.pk or 'NEW'}):"
        logger.info(f"{log_prefix} --- Initiating save ---")

        original_currency_on_obj_entry = obj.default_currency_code if obj.pk else "N/A (New Record before form bind)"

        # The 'obj' passed by Django admin to save_model has ALREADY been updated
        # with form.cleaned_data by Django's ModelAdmin._changeform_view or _add_view.
        # So, obj.default_currency_code here should reflect the submitted form value if the field was part of the form.

        submitted_currency_via_form_cleaned_data = form.cleaned_data.get(
            'default_currency_code') if form.is_valid() else "Form Invalid or Field Missing"
        logger.info(
            f"{log_prefix} 1. Form cleaned_data['default_currency_code'] (if valid): '{submitted_currency_via_form_cleaned_data}'")
        logger.info(
            f"{log_prefix} 2. obj.default_currency_code AT ENTRY (should match form if field was bound): '{obj.default_currency_code}'")
        if obj.pk and original_currency_on_obj_entry != obj.default_currency_code:
            logger.info(
                f"{log_prefix} Note: obj.default_currency_code ('{obj.default_currency_code}') differs from original DB value ('{original_currency_on_obj_entry}') due to form binding.")

        # Apply custom logic (like setting created_by_user)
        if not change and not obj.created_by_user_id and request.user.is_superuser:
            obj.created_by_user = request.user
            logger.info(f"{log_prefix} Set created_by_user to: {request.user.name}")

        logger.info(
            f"{log_prefix} 3. obj.default_currency_code BEFORE super().save_model() call: '{obj.default_currency_code}'")

        try:
            # This call will trigger:
            # 1. obj.full_clean() (unless specific fields are excluded by the form meta or admin)
            # 2. pre_save signals for Company model
            # 3. obj.save() -> Company.save() method
            # 4. post_save signals for Company model
            super().save_model(request, obj, form, change)
            logger.info(f"{log_prefix} super().save_model() completed successfully.")
        except Exception as e:
            logger.error(f"{log_prefix} Error during super().save_model(): {e}", exc_info=True)
            # Let Django admin handle displaying the error message to the user
            raise  # Re-raise the exception to let admin process it

        # Verify the value in the database after all operations
        try:
            # Refresh only the specific field to see its final state in the DB
            # This avoids overwriting other in-memory changes if any happened in post_save signals
            # that didn't also save to DB.
            refreshed_obj_for_check = Company.objects.get(pk=obj.pk)
            final_db_currency = refreshed_obj_for_check.default_currency_code
            logger.info(
                f"{log_prefix} 4. default_currency_code AFTER ALL SAVES (fetched from DB): '{final_db_currency}'")

            # Compare with what was intended from the form
            if final_db_currency != submitted_currency_via_form_cleaned_data and \
                    submitted_currency_via_form_cleaned_data not in ["Form Invalid or Field Missing", None]:
                logger.critical(
                    f"{log_prefix} CRITICAL MISMATCH DETECTED: "
                    f"Form submitted currency '{submitted_currency_via_form_cleaned_data}', "
                    f"but database has '{final_db_currency}'. "
                    f"The value was likely changed in Company.save() or a pre_save signal."
                )
                messages.warning(request,
                                 _("The currency code may not have saved as expected. Please verify the Operational Settings."))
            elif final_db_currency == submitted_currency_via_form_cleaned_data:
                logger.info(f"{log_prefix} Currency code successfully saved as '{final_db_currency}'.")

        except Company.DoesNotExist:
            logger.error(
                f"{log_prefix} Company PK {obj.pk} not found after save_model. Cannot verify final currency in DB.")
        except Exception as e:
            logger.error(f"{log_prefix} Error refreshing Company object from DB after save: {e}", exc_info=True)

        logger.info(f"{log_prefix} --- Save complete ---")

    # --- Admin Actions ---
    @admin.action(description=_("Activate & Unsuspend selected companies"))
    def admin_action_make_companies_active(self, request, queryset):
        updated_count = queryset.update(is_active=True, is_suspended_by_admin=False, updated_at=timezone.now())
        self.message_user(request, _(f"{updated_count} companies activated & unsuspended."), messages.SUCCESS)

    @admin.action(description=_("Deactivate selected companies"))
    def admin_action_make_companies_inactive(self, request, queryset):
        updated_count = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(request, _(f"{updated_count} companies deactivated."), messages.SUCCESS)

    @admin.action(description=_("Suspend selected companies"))
    def admin_action_suspend_companies(self, request, queryset):
        updated_count = queryset.update(is_suspended_by_admin=True, updated_at=timezone.now())
        self.message_user(request, _(f"{updated_count} companies suspended."), messages.SUCCESS)

    @admin.action(description=_("Unsuspend selected companies"))
    def admin_action_unsuspend_companies(self, request, queryset):
        updated_count = queryset.update(is_suspended_by_admin=False, updated_at=timezone.now())
        self.message_user(request, _(f"{updated_count} companies unsuspended."), messages.SUCCESS)


# =============================================================================
# CompanyMembership Admin
# =============================================================================
@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    # ... (Your CompanyMembershipAdmin code - looks well-structured)
    list_display = ('user_display', 'company_display', 'role_display', 'is_active_membership',
                    'is_default_for_user', 'effective_can_access_display', 'date_joined')
    list_filter_base = ('role', 'is_active_membership', 'is_default_for_user')
    search_fields = ('user__name', 'user__email', 'user__first_name', 'user__last_name',
                     'company__name', 'company__subdomain_prefix')
    autocomplete_fields = ['user', 'company']
    readonly_fields = ('date_joined', 'last_accessed_at_display')  # Add 'id' if UUID and you want to see it
    actions = ['admin_action_make_memberships_active', 'admin_action_make_memberships_inactive']
    fieldsets = (
        (None, {'fields': ('user', 'company', 'role')}),
        (_('Membership Details'), {'fields': ('is_active_membership', 'is_default_for_user', 'can_manage_members')}),
        (_('Audit Information'), {'fields': ('date_joined', 'last_accessed_at_display'), 'classes': ('collapse',)}),
    )

    # Display methods
    def user_display(self, obj: CompanyMembership):
        return obj.user.get_full_name() or obj.user.get_name() if obj.user else _("User Deleted")

    user_display.short_description = _('User')
    user_display.admin_order_field = 'user__last_name'

    def company_display(self, obj: CompanyMembership) -> str:
        return obj.company.name if obj.company else _("Company Deleted")

    company_display.short_description = _('Company')
    company_display.admin_order_field = 'company__name'

    def role_display(self, obj: CompanyMembership) -> str:
        return obj.get_role_display()

    role_display.short_description = _('Role')
    role_display.admin_order_field = 'role'

    def last_accessed_at_display(self, obj: CompanyMembership) -> str:
        return obj.last_accessed_at.strftime("%Y-%m-%d %H:%M:%S %Z") if obj.last_accessed_at else _("Never")

    last_accessed_at_display.short_description = _('Last Accessed')

    def effective_can_access_display(self, obj: CompanyMembership) -> str:
        return format_html('<span style="color: green;">✅ {}</span>', _('Can Access')) if obj.effective_can_access \
            else format_html('<span style="color: red;">❌ {}</span>', _('No Access'))

    effective_can_access_display.short_description = _('Effective Access')

    # No easy admin_order_field for a property that involves multiple related models.

    # Dynamic configurations
    def get_list_filter(self, request):
        return ('company',) + self.list_filter_base if request.user.is_superuser else self.list_filter_base

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('user', 'company', 'company__created_by_user')
        if request.user.is_superuser: return qs
        user_manageable_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True, company__effective_is_active=True
        ).values_list('company_id', flat=True)
        return qs.filter(company_id__in=list(user_manageable_company_ids))

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser:
            user_manageable_companies_qs = Company.objects.filter(
                pk__in=CompanyMembership.objects.filter(
                    user=request.user,
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True
                ).values_list('company_id', flat=True),
                effective_is_active=True
            ).distinct().order_by('name')

            if 'company' in form.base_fields:
                form.base_fields['company'].queryset = user_manageable_companies_qs
                if user_manageable_companies_qs.count() == 1 and (
                        obj is None or obj.company_id == user_manageable_companies_qs.first().pk):
                    form.base_fields['company'].initial = user_manageable_companies_qs.first()
                    form.base_fields['company'].disabled = True
                elif not user_manageable_companies_qs.exists():
                    form.base_fields['company'].queryset = Company.objects.none()
        return form

    # Permissions
    def has_add_permission(self, request) -> bool:
        if request.user.is_superuser:
            return True
        # Non-superusers can add memberships if they are an active Owner or Admin
        # of at least one effectively active company.
        # The form itself (get_form) will limit which company they can add to.
        return CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True  # <<< CORRECTED
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            return CompanyMembership.objects.filter(
                user=request.user, company=obj.company,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        return self.has_add_permission(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            can_change = self.has_change_permission(request, obj)
            if not can_change: return False
            if obj.user == request.user and obj.role in [CompanyMembership.Role.OWNER.value,
                                                         CompanyMembership.Role.ADMIN.value]:
                # Check if there are other active Owner/Admin users in the same company
                other_admins_or_owners = obj.company.memberships.filter(
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True
                ).exclude(pk=obj.pk)
                if not other_admins_or_owners.exists():
                    # messages.error(request, _("Cannot delete sole admin/owner membership for {}.").format(obj.company.name)) # Message on save is better
                    return False
            return True
        return self.has_add_permission(request)

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True

        # Check if the current user has any active membership in any effectively active company.
        current_user_memberships = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            company__is_active=True  # <<< CORRECTED HERE
        )

        if not current_user_memberships.exists():
            return False  # If user has no active memberships in active companies, they can't view anything here.

        if obj is not None:  # Viewing a specific CompanyMembership detail page
            # User can view their own membership, provided its company is active (already checked by base_access_qs).
            if obj.user_id == request.user.pk:
                # We already know obj.company.is_active is true from the initial current_user_memberships check
                # if obj.company was one of their active companies.
                # However, if obj.company is NOT one of their active companies, this path might not be hit
                # if the previous `if not current_user_memberships.exists()` returned False.
                # To be safe, ensure obj.company is active.
                return obj.company.is_active  # Checking the specific company of the object being viewed

            # User can view other memberships if they are an Owner/Admin of THAT membership's company.
            can_manage_members_of_obj_company = current_user_memberships.filter(
                company=obj.company,  # The company of the membership being viewed
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value]
            ).exists()
            if can_manage_members_of_obj_company:  # And obj.company must be active (implicit from current_user_memberships)
                return True

            # Optional: Allow viewing colleagues (memberships of companies they are also part of)
            # is_member_of_obj_company = current_user_memberships.filter(company=obj.company).exists()
            # if is_member_of_obj_company:
            #     return True # Requires obj.company to be active

            return False  # Default to deny if not their own and not an admin of that company

        # For the CompanyMembership list view (obj is None):
        # Allow access if the user is an Owner/Admin of at least one active company,
        # as their `get_queryset` will be filtered to only show memberships they can manage/see.
        return current_user_memberships.filter(
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value]
        ).exists()

    # Admin Actions
    @admin.action(description=_("Activate selected memberships"))
    def admin_action_make_memberships_active(self, request, queryset):
        updated_count = 0;
        pks_to_update = [item.pk for item in queryset if self.has_change_permission(request, item)]
        if pks_to_update: updated_count = CompanyMembership.objects.filter(pk__in=pks_to_update).update(
            is_active_membership=True)
        messages.success(request, _(f"{updated_count} memberships activated."))
        if queryset.count() != updated_count: messages.warning(request,
                                                               _("Some memberships skipped due to permissions."))

    @admin.action(description=_("Deactivate selected memberships"))
    def admin_action_make_memberships_inactive(self, request, queryset):
        updated_count = 0;
        skipped_msgs = []
        pks_to_update = []
        for m in queryset:
            can_deactivate, reason = True, ""
            if not self.has_change_permission(request, m):
                can_deactivate, reason = False, _("no change permission")
            elif m.role in [CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value] and \
                    not m.company.memberships.filter(
                        role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                        is_active_membership=True).exclude(pk=m.pk).exists():
                can_deactivate, reason = False, _("sole active Owner/Admin")
            if can_deactivate:
                pks_to_update.append(m.pk)
            elif reason:
                skipped_msgs.append(f"Skipped {m.user.get_name()} in {m.company.name} ({reason}).")
        if pks_to_update: updated_count = CompanyMembership.objects.filter(pk__in=pks_to_update).update(
            is_active_membership=False)
        if updated_count > 0: messages.success(request, _(f"{updated_count} memberships deactivated."))
        for msg in skipped_msgs: messages.warning(request, msg)
        if not pks_to_update and not skipped_msgs: messages.info(request, _("No memberships eligible."))