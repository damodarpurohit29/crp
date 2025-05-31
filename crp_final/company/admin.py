# company/admin.py
import logging
from django.contrib import admin, messages
from django.urls import reverse  # Added for linking to custom admin view
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html, format_html_join  # Added format_html_join
from django.utils import timezone
from django import forms  # For custom formset
from django.forms.models import BaseInlineFormSet  # For custom formset

from crp_accounting.models import Account  # Assuming this import is correct
# Import your models
from .models import Company, CompanyGroup, CompanyMembership
from .models_settings import CompanyAccountingSettings

# NEW: Import from voucher_service for permissions display
try:
    from crp_accounting.services.voucher_service import get_permissions_for_role
except ImportError:
    # Fallback or log error if the import fails
    def get_permissions_for_role(role_value: str) -> list[str]:
        # So the admin doesn't break completely if the import fails
        return ["Error: Permission checker (get_permissions_for_role) unavailable from voucher_service."]


    logging.error(
        "company.admin: Failed to import get_permissions_for_role from crp_accounting.services.voucher_service.")

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
        return obj.companies.count()

    company_count_display.short_description = _('No. of Companies')

    def has_module_permission(self, request) -> bool:
        return request.user.is_superuser


# =============================================================================
# CompanyMembership Inline FormSet (for custom validation)
# =============================================================================
class CompanyMembershipInlineFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)  # Store request
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()

        # Rule: Prevent current user from removing their own last active Owner/Admin role
        # if it leaves the company without any active Owner/Admin.
        if not self.request or self.request.user.is_superuser:
            return

        parent_company = self.instance  # This is the Company instance
        if not (
                parent_company and parent_company.pk and parent_company.is_active and not parent_company.is_suspended_by_admin):
            # Rule only applies if the parent company is active and not suspended.
            return

        active_admin_owner_count = 0
        current_user_form = None  # Form corresponding to the current request.user
        current_user_was_active_admin = False
        current_user_is_losing_admin_status = False

        for form in self.forms:
            if not form.is_valid() or not hasattr(form, 'cleaned_data'):
                continue

            cd = form.cleaned_data
            is_being_deleted = self.can_delete and cd.get('DELETE', False)

            membership_instance = form.instance  # This is the CompanyMembership instance
            role_from_form = cd.get('role')
            is_active_from_form = cd.get('is_active_membership', False)
            user_from_form = cd.get('user')  # This is a User instance from the form

            # Check if this form belongs to the current request.user
            if (
                    membership_instance and membership_instance.pk and membership_instance.user_id == self.request.user.id) or \
                    (not membership_instance.pk and user_from_form and user_from_form.id == self.request.user.id):
                current_user_form = form

                # Was the current user an active admin/owner *before* this change?
                if membership_instance and membership_instance.pk:  # Existing membership
                    current_user_was_active_admin = (
                            membership_instance.role in [CompanyMembership.Role.OWNER.value,
                                                         CompanyMembership.Role.ADMIN.value] and
                            membership_instance.is_active_membership
                    )

                # Is the current user losing their active admin/owner status *due to this change*?
                if is_being_deleted:
                    if current_user_was_active_admin:
                        current_user_is_losing_admin_status = True
                else:  # Not deleted, check if role/status changed from active admin
                    if current_user_was_active_admin and \
                            not (role_from_form in [CompanyMembership.Role.OWNER.value,
                                                    CompanyMembership.Role.ADMIN.value] and is_active_from_form):
                        current_user_is_losing_admin_status = True

            # Count all active admins/owners *after* considering deletions and changes in this formset
            if not is_being_deleted:
                if role_from_form in [CompanyMembership.Role.OWNER.value,
                                      CompanyMembership.Role.ADMIN.value] and is_active_from_form:
                    active_admin_owner_count += 1

        # Apply the rule if the current user is losing admin status and this results in zero active admins
        if current_user_form and current_user_is_losing_admin_status and active_admin_owner_count == 0:
            msg = _(
                "You cannot remove or deactivate your own Owner/Admin role as it would leave "
                "the company without any active Owner/Admin. Please assign another Owner/Admin first, "
                "or ensure another existing Owner/Admin remains active."
            )
            current_user_form.add_error(None, msg)


# =============================================================================
# CompanyMembership Inline (for CompanyAdmin)
# =============================================================================
class CompanyMembershipInlineForCompanyAdmin(admin.TabularInline):
    model = CompanyMembership
    formset = CompanyMembershipInlineFormSet  # Use custom formset
    extra = 1
    autocomplete_fields = ['user']
    fields = ('user', 'role', 'is_active_membership', 'can_manage_members')
    verbose_name = _("User Access")
    verbose_name_plural = _("Manage User Access for this Company")

    def get_formset(self, request, obj=None, **kwargs):
        # Request passing to the formset is now handled by get_formset_kwargs.
        return super().get_formset(request, obj, **kwargs)

    def get_formset_kwargs(self, request, obj):
        """
        Hook for returning keyword arguments to the formset constructor.
        """
        kwargs = super().get_formset_kwargs(request, obj)
        kwargs['request'] = request  # Pass request to the FormSet's __init__
        return kwargs

    def get_readonly_fields(self, request, obj=None):  # obj is the parent Company
        if obj and obj.pk:  # If editing an existing Company
            return ('user',)  # User in an existing membership cannot be changed, delete and re-add
        return ()

    def has_add_permission(self, request, obj=None) -> bool:  # obj is the parent Company instance
        if request.user.is_superuser: return True
        if obj is None: return False  # Cannot add members if parent Company (obj) doesn't exist yet or is not provided.

        # Parent Company must be effectively active for members to be added
        if not (obj.is_active and not obj.is_suspended_by_admin):
            return False

        # User must be Owner/Admin of the parent Company
        return CompanyMembership.objects.filter(
            user=request.user, company=obj,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:
        # obj is THE PARENT Company instance, or None (when checking general change perm for inline type).
        if request.user.is_superuser:
            return True

        if obj is None:  # Checking general change permission for this inline type.
            # obj (parent) might be None on parent's add page.
            parent_company_being_edited = getattr(request, '_company_admin_parent_obj', None)
            return self.has_add_permission(request, parent_company_being_edited)

        # If obj is not None, it's the parent Company instance.
        parent_company = obj
        if not (parent_company.is_active and not parent_company.is_suspended_by_admin):
            return False

        return CompanyMembership.objects.filter(
            user=request.user,
            company=parent_company,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True
        ).exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        # obj is THE PARENT Company instance, or None (when checking general delete perm for inline type).
        if request.user.is_superuser:
            return True

        if obj is None:  # Parent (Company) is being added, or general check.
            parent_company_being_edited = getattr(request, '_company_admin_parent_obj', None)
            return self.has_add_permission(request, parent_company_being_edited)

        # If obj is not None, it's the parent Company instance.
        parent_company = obj
        if not (parent_company.is_active and not parent_company.is_suspended_by_admin):
            return False

        return CompanyMembership.objects.filter(
            user=request.user,
            company=parent_company,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True
        ).exists()


# =============================================================================
# Company Admin (Tenant Entity Admin)
# =============================================================================
@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display_superuser = (
        'name', 'subdomain_prefix', 'primary_email', 'default_currency_code', 'country_code',
        'effective_is_active_display', 'company_group', 'created_at'
    )
    list_display_client_placeholder = ('display_name', 'primary_email', 'website', 'default_currency_code',
                                       'effective_is_active_display')

    list_filter_superuser = (
        'is_active', 'is_suspended_by_admin', 'country_code', 'company_group',
        'default_currency_code', 'financial_year_start_month', 'timezone_name'
    )
    search_fields = (
        'name', 'subdomain_prefix', 'display_name', 'primary_email', 'registration_number', 'tax_id_primary')

    autocomplete_fields = ['company_group', 'created_by_user']
    actions = ['admin_action_make_companies_active', 'admin_action_make_companies_inactive',
               'admin_action_suspend_companies', 'admin_action_unsuspend_companies']

    fieldsets_client_profile = (
        (_('My Company Profile'), {'fields': (
            'display_name', 'logo', ('primary_email', 'primary_phone'), 'website', 'internal_company_code_display')}),
        (_('Registered Address'), {'classes': ('collapse',), 'fields': (
            'address_line1', 'address_line2', 'city', 'state_province_region', 'postal_code', 'country_code')}),
        (_('Operational Settings'), {'classes': ('collapse',), 'fields': (
            'default_currency_code', 'default_currency_symbol', 'financial_year_start_month', 'timezone_name')}),
        (_('Tax Information'), {'classes': ('collapse',), 'fields': ('registration_number', 'tax_id_primary')}),
        (_('Account Status'), {'fields': ('effective_is_active_display_form',)}),
    )
    fieldsets_superuser_platform_admin = (
        (_('Platform Administration & SaaS Settings'), {'fields': (
            'name', 'subdomain_prefix', 'internal_company_code', 'company_group', 'is_active', 'is_suspended_by_admin',
            'created_by_user')}),
    )
    fieldsets_superuser_combined = (
        (_('Platform Administration & SaaS Settings'), {'fields': (
            'name', 'subdomain_prefix', 'internal_company_code', 'company_group', 'is_active', 'is_suspended_by_admin',
            'created_by_user')}),
        (_('Company Client-Visible Profile'),
         {'fields': ('display_name', 'logo', ('primary_email', 'primary_phone'), 'website')}),
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
        if obj.is_suspended_by_admin:
            return format_html('<span style="color: red;">❌ {}</span>', _('Suspended'))
        elif obj.is_active:
            return format_html('<span style="color: green;">✅ {}</span>', _('Active'))
        else:
            return format_html('<span style="color: orange;">➖ {}</span>', _('Inactive'))

    effective_is_active_display.short_description = _('Effective Status')
    effective_is_active_display.admin_order_field = 'is_active'

    def effective_is_active_display_form(self, obj: Company) -> str:
        if obj is None: return "---"
        if obj.is_suspended_by_admin:
            return _('❌ Account Suspended by Admin.')
        elif obj.is_active:
            return _('✅ Account is active.')
        else:
            return _('➖ Account Inactive.')

    effective_is_active_display_form.short_description = _('Current Account Status')

    def created_by_user_display(self, obj: Company) -> str:
        return obj.created_by_user.get_full_name() if obj.created_by_user else _("System/N/A")

    created_by_user_display.short_description = _('Registered By')

    def internal_company_code_display(self, obj: Company) -> str:
        return obj.internal_company_code or "---"

    internal_company_code_display.short_description = _('Company Code (Internal)')

    # --- Dynamic Admin Configurations ---
    def get_list_display(self, request):
        return self.list_display_superuser if request.user.is_superuser else self.list_display_client_placeholder

    def get_list_filter(self, request):
        return self.list_filter_superuser if request.user.is_superuser else ()

    def get_readonly_fields(self, request, obj=None):
        ro_fields = set(super().get_readonly_fields(request, obj) or [])
        ro_fields.update(['created_at', 'updated_at', 'created_by_user_display', 'effective_is_active_display_form'])

        if request.user.is_superuser:
            if obj and obj.pk: ro_fields.add('created_by_user')
        else:
            ro_fields.update([
                'name', 'subdomain_prefix', 'company_group', 'internal_company_code',
                'is_active', 'is_suspended_by_admin', 'created_by_user',
            ])
            if hasattr(self.model, 'default_currency_symbol'):
                ro_fields.add('default_currency_symbol')

        if obj and obj.pk:
            if 'subdomain_prefix' not in ro_fields: ro_fields.add('subdomain_prefix')
        if 'internal_company_code_display' not in ro_fields:
            ro_fields.add('internal_company_code_display')
        return tuple(ro_fields)

    def get_fieldsets(self, request, obj=None):
        request._company_admin_parent_obj = obj  # Used by inline permission checks
        if request.user.is_superuser:
            return self.fieldsets_superuser_combined
        return self.fieldsets_client_profile

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('company_group', 'created_by_user')
        if request.user.is_superuser:
            return qs

        user_administered_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).values_list('company_id', flat=True)
        return qs.filter(pk__in=list(user_administered_company_ids))

    def get_inlines(self, request, obj):  # obj is the parent Company instance
        # Ensure inlines are defined on the class to be referenced here
        self.inlines = [CompanyMembershipInlineForCompanyAdmin]

        if obj is None:  # No parent Company instance (e.g. on add page)
            if request.user.is_superuser:
                return self.inlines
            return []

        if request.user.is_superuser:
            return self.inlines

        if not (obj.is_active and not obj.is_suspended_by_admin):
            return []

        if CompanyMembership.objects.filter(
                user=request.user, company=obj,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
        ).exists():
            return self.inlines
        return []

    # --- Permission Methods ---
    def has_add_permission(self, request) -> bool:
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None) -> bool:
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            if not (obj.is_active and not obj.is_suspended_by_admin):
                return False
            return CompanyMembership.objects.filter(
                user=request.user, company=obj,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        return False  # Cannot change if obj is None and not superuser

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True
        base_access_qs = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            company__is_active=True,
            company__is_suspended_by_admin=False
        )
        if obj is not None:
            if not (obj.is_active and not obj.is_suspended_by_admin):
                return False
            return base_access_qs.filter(company=obj).exists()
        return base_access_qs.exists()

    # --- SAVE MODEL WITH DETAILED LOGGING ---
    def save_model(self, request, obj: Company, form, change):
        action = "Changing" if change else "Adding"
        user_name_for_log = getattr(request.user, 'name', None) or request.user.get_full_name() or str(request.user)
        log_prefix = f"CompanyAdmin SaveModel (User:{user_name_for_log}, Action:{action}, PK:{obj.pk or 'NEW'}):"
        logger.info(f"{log_prefix} --- Initiating save ---")

        original_currency_on_obj_entry = "N/A"
        if obj.pk:
            try:  # Get from DB to ensure it's the pre-form-binding value
                original_currency_on_obj_entry = Company.objects.values_list('default_currency_code', flat=True).get(
                    pk=obj.pk)
            except Company.DoesNotExist:
                original_currency_on_obj_entry = "N/A (PK exists but obj not in DB pre-form bind?)"
        else:
            original_currency_on_obj_entry = "N/A (New Record before form bind)"

        submitted_currency_via_form_cleaned_data = form.cleaned_data.get(
            'default_currency_code') if form.is_valid() else "Form Invalid or Field Missing"

        logger.info(
            f"{log_prefix} 1. Form cleaned_data['default_currency_code'] (if valid): '{submitted_currency_via_form_cleaned_data}'")
        # obj.default_currency_code here is after model instance is updated by form.cleaned_data
        logger.info(
            f"{log_prefix} 2. obj.default_currency_code AT ENTRY (after form bind): '{obj.default_currency_code}'")

        if obj.pk and original_currency_on_obj_entry != obj.default_currency_code and original_currency_on_obj_entry != "N/A (PK exists but obj not in DB pre-form bind?)":
            logger.info(
                f"{log_prefix} Note: obj.default_currency_code ('{obj.default_currency_code}') differs from original DB value ('{original_currency_on_obj_entry}') due to form binding.")

        if not change and not obj.created_by_user_id:
            if request.user.is_authenticated:  # Ensure user is authenticated (should be for admin)
                obj.created_by_user = request.user
                logger.info(
                    f"{log_prefix} Set created_by_user to: {user_name_for_log}")

        logger.info(
            f"{log_prefix} 3. obj.default_currency_code BEFORE super().save_model() call: '{obj.default_currency_code}'")

        try:
            super().save_model(request, obj, form, change)
            logger.info(f"{log_prefix} super().save_model() completed successfully.")
        except Exception as e:
            logger.error(f"{log_prefix} Error during super().save_model(): {e}", exc_info=True)
            raise

        try:
            # Re-fetch to ensure we have the very latest from DB, after all signals/model.save overrides
            refreshed_obj_for_check = Company.objects.get(pk=obj.pk)
            final_db_currency = refreshed_obj_for_check.default_currency_code
            logger.info(
                f"{log_prefix} 4. default_currency_code AFTER ALL SAVES (fetched from DB): '{final_db_currency}'")

            if form.is_valid() and 'default_currency_code' in form.cleaned_data:  # Ensure field was in form and form is valid
                if final_db_currency != submitted_currency_via_form_cleaned_data:
                    logger.critical(
                        f"{log_prefix} CRITICAL MISMATCH DETECTED: "
                        f"Form submitted currency '{submitted_currency_via_form_cleaned_data}', "
                        f"but database has '{final_db_currency}'. "
                        f"The value was likely changed in Company.save() or a pre_save/post_save signal."
                    )
                    messages.warning(request,
                                     _("The currency code may not have saved as expected. Please verify the Operational Settings."))
                else:
                    logger.info(f"{log_prefix} Currency code successfully saved as '{final_db_currency}'.")
            else:  # Form was invalid or field not in form
                logger.info(
                    f"{log_prefix} Currency code in DB is '{final_db_currency}'. Form was invalid or field not present/editable.")

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
# CompanyMembership Admin (Standalone)
# =============================================================================
@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ('user_display', 'company_display', 'role_display',
                    'display_key_permissions_for_role',  # MODIFIED: Added to list_display
                    'is_active_membership',
                    'is_default_for_user', 'effective_can_access_display', 'date_joined')
    list_filter_base = ('role', 'is_active_membership', 'is_default_for_user')
    search_fields = (
        'user__username', 'user__email', 'user__first_name', 'user__last_name',
        'company__name', 'company__subdomain_prefix')
    autocomplete_fields = ['user', 'company']

    # MODIFIED: Added new readonly fields and adjusted fieldsets
    readonly_fields = (
        'date_joined',
        'last_accessed_at_display',
        'role_permissions_list',
        'link_to_global_permissions_overview',
    )
    actions = ['admin_action_make_memberships_active', 'admin_action_make_memberships_inactive']

    # MODIFIED: Adjusted fieldsets to include new permission display fields
    fieldsets = (
        (None, {'fields': ('user', 'company', 'role')}),
        (_('Membership Details'), {'fields': ('is_active_membership', 'is_default_for_user', 'can_manage_members')}),
        (_('Voucher Permissions Information'), {  # NEW fieldset section
            'fields': ('role_permissions_list', 'link_to_global_permissions_overview'),
            'classes': ('collapse',),  # Optional: make it collapsible by default
            'description': _(
                "Displays voucher service permissions based on the selected role. Note: These are hardcoded in the system.")
        }),
        (_('Audit Information'), {'fields': ('date_joined', 'last_accessed_at_display'), 'classes': ('collapse',)}),
    )

    def user_display(self, obj: CompanyMembership):
        if not obj.user: return _("User Deleted")
        return obj.user.get_full_name() or obj.user.username  # Fallback to username

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
        can_access = False
        if obj.user and obj.company:  # Check if user and company exist
            can_access = (
                    obj.is_active_membership and
                    obj.company.is_active and
                    not obj.company.is_suspended_by_admin and
                    obj.user.is_active  # Assuming user model has is_active
            )
        return format_html('<span style="color: green;">✅ {}</span>', _('Can Access')) if can_access \
            else format_html('<span style="color: red;">❌ {}</span>', _('No Access'))

    effective_can_access_display.short_description = _('Effective Access')

    # --- NEW METHODS FOR PERMISSIONS DISPLAY ---
    def display_key_permissions_for_role(self, obj: CompanyMembership):
        """Displays a snippet of permissions for the membership's role in list_display."""
        if not obj.role:
            return "N/A"
        # obj.role is the value stored in the DB (e.g., 'ACCOUNTANT', 'ADMIN')
        permissions = get_permissions_for_role(obj.role)

        if not permissions or "Error:" in permissions[0]:
            return permissions[0] if permissions else "N/A"

        # Show first few for brevity
        max_perms_to_show = 2
        if len(permissions) > max_perms_to_show:
            return ", ".join(permissions[:max_perms_to_show]) + "..."
        return ", ".join(permissions)

    display_key_permissions_for_role.short_description = _('Key Role Permissions')

    def role_permissions_list(self, obj: CompanyMembership):
        """Displays all permissions for the membership's current role."""
        if not obj.role:
            return "No role assigned."
        permissions = get_permissions_for_role(obj.role)

        if not permissions or "Error:" in permissions[0]:
            # If the helper returned an error message, display it clearly
            return format_html("<p style='color:red;'>{}</p>", permissions[0])

        return format_html("<ul>{}</ul>", format_html_join('', "<li>{}</li>", ((p,) for p in permissions)))

    role_permissions_list.short_description = _('Voucher Permissions for this Role')

    def link_to_global_permissions_overview(self, obj: CompanyMembership):
        """Provides a link to the global voucher permissions overview page."""
        # IMPORTANT: Adjust the URL name if it's different.
        # This assumes you have a URL named 'admin_voucher_permissions_overview'
        # in an app namespace 'crp_accounting_admin'.
        # If your crp_accounting.urls are not namespaced or named differently, adjust this.
        try:
            # Example: if crp_accounting.urls is included like:
            # path('admin/crp_accounting_custom/', include('crp_accounting.urls', namespace='crp_accounting_admin')),
            url = reverse('crp_accounting_admin:admin_voucher_permissions_overview')
            # Add the current role as a query parameter for potential highlighting on the target page
            url_with_param = f"{url}?highlight_role={obj.role}"
            return format_html('<a href="{}" target="_blank">{}</a>',
                               url_with_param,
                               _("View Global Voucher Permissions Overview (All Roles)"))
        except Exception as e:
            logger.warning(f"CompanyMembershipAdmin: Could not reverse URL for global permissions overview: {e}")
            return _("Link to global overview not available (URL misconfiguration).")

    link_to_global_permissions_overview.short_description = _('Global Permissions Link')

    # link_to_global_permissions_overview.allow_tags = True # Not needed with format_html
    # --- END NEW METHODS FOR PERMISSIONS DISPLAY ---

    def get_list_filter(self, request):
        filters = self.list_filter_base
        if request.user.is_superuser:
            filters = ('company',) + filters
        return filters

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('user', 'company')
        if request.user.is_superuser: return qs

        user_manageable_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
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
                is_active=True,
                is_suspended_by_admin=False
            ).distinct().order_by('name')

            if 'company' in form.base_fields:
                form.base_fields['company'].queryset = user_manageable_companies_qs
                if user_manageable_companies_qs.count() == 1 and \
                        (obj is None or (obj.company and obj.company.pk == user_manageable_companies_qs.first().pk)):
                    form.base_fields['company'].initial = user_manageable_companies_qs.first()
                    form.base_fields['company'].disabled = True
                elif not user_manageable_companies_qs.exists():
                    form.base_fields['company'].queryset = Company.objects.none()
        return form

    # Permissions for standalone CompanyMembershipAdmin
    def has_add_permission(self, request) -> bool:
        if request.user.is_superuser:
            return True
        return CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:  # obj is a CompanyMembership instance
            if not obj.company: return False  # Safety check
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False
            return CompanyMembership.objects.filter(
                user=request.user, company=obj.company,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        # For general change on list view (obj is None), allow if user can add.
        return self.has_add_permission(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:  # obj is a CompanyMembership instance
            if not obj.company: return False  # Safety check
            can_change = self.has_change_permission(request, obj)  # User must be admin of the company
            if not can_change: return False

            # Rule: Prevent deleting own Owner/Admin membership if it's the last active one
            # for an effectively active company.
            if obj.user == request.user and \
                    obj.role in [CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value] and \
                    obj.is_active_membership:

                if obj.company.is_active and not obj.company.is_suspended_by_admin:
                    other_admins_or_owners = obj.company.memberships.filter(
                        role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                        is_active_membership=True
                    ).exclude(pk=obj.pk)
                    if not other_admins_or_owners.exists():
                        return False  # Cannot delete the last active admin/owner.
            return True
        # For general delete on list view (obj is None), allow if user can add.
        return self.has_add_permission(request)

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True

        # Query for companies where current user is an active Owner/Admin and company is effectively active
        current_user_admin_of_active_companies_qs = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            company__is_active=True,
            company__is_suspended_by_admin=False
        )

        if obj is not None:  # Viewing a specific CompanyMembership
            if not obj.company: return False  # Safety check
            # Membership's company must be effectively active
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False

            # Can view own active membership in an effectively active company
            if obj.user_id == request.user.pk and obj.is_active_membership:
                return True

            # Can view if they are Owner/Admin of the membership's company (and that company is active)
            return current_user_admin_of_active_companies_qs.filter(company=obj.company).exists()

        # For list view (obj is None): access granted if they are Owner/Admin of at least one effectively active company
        return current_user_admin_of_active_companies_qs.exists()

    # Admin Actions for CompanyMembership (Standalone)
    @admin.action(description=_("Activate selected memberships"))
    def admin_action_make_memberships_active(self, request, queryset):
        updated_count = 0
        pks_to_update = [item.pk for item in queryset if self.has_change_permission(request, item)]
        if pks_to_update:
            updated_count = CompanyMembership.objects.filter(pk__in=pks_to_update).update(is_active_membership=True)
        messages.success(request, _(f"{updated_count} memberships activated."))
        if queryset.count() != updated_count:
            messages.warning(request,
                             _("Some memberships were not activated due to permission restrictions or company status."))

    @admin.action(description=_("Deactivate selected memberships"))
    def admin_action_make_memberships_inactive(self, request, queryset):
        updated_count = 0
        skipped_msgs = []
        pks_to_update = []

        for m_obj in queryset:
            can_deactivate = True
            reason = ""

            if not self.has_change_permission(request, m_obj):
                can_deactivate = False
                reason = _("no change permission or company inactive/suspended")
            # Rule for not deactivating last active admin/owner (applies if current user is that admin/owner or SU)
            elif m_obj.role in [CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value] and \
                    m_obj.is_active_membership and m_obj.company and \
                    m_obj.company.is_active and not m_obj.company.is_suspended_by_admin and \
                    not m_obj.company.memberships.filter(  # Check other active admins/owners
                        role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                        is_active_membership=True
                    ).exclude(pk=m_obj.pk).exists():
                can_deactivate = False
                reason = _("sole active Owner/Admin in an active company")

            if can_deactivate:
                pks_to_update.append(m_obj.pk)
            elif reason:
                user_identifier = m_obj.user.get_full_name() or m_obj.user.username if m_obj.user else f"User PK {m_obj.user_id}"
                company_identifier = m_obj.company.name if m_obj.company else f"Company PK {m_obj.company_id}"
                skipped_msgs.append(f"Skipped {user_identifier} in {company_identifier} ({reason}).")

        if pks_to_update:
            updated_count = CompanyMembership.objects.filter(pk__in=pks_to_update).update(is_active_membership=False)

        if updated_count > 0:
            messages.success(request, _(f"{updated_count} memberships deactivated."))
        for msg in skipped_msgs:
            messages.warning(request, msg)
        if not pks_to_update and not skipped_msgs and queryset.exists():
            messages.info(request, _("No memberships were eligible for deactivation."))


# =============================================================================
# CompanyAccountingSettings Admin
# =============================================================================
@admin.register(CompanyAccountingSettings)
class CompanyAccountingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'company_name', 'default_retained_earnings_account', 'default_accounts_receivable_control', 'updated_at',
        'updated_by_user')
    readonly_fields = ('created_at', 'updated_at', 'updated_by')

    list_select_related = (
        'company', 'updated_by', 'default_retained_earnings_account', 'default_accounts_receivable_control')
    search_fields = ('company__name',)
    raw_id_fields = (
        'default_retained_earnings_account',
        'default_accounts_receivable_control',
        'default_sales_revenue_account',
        'default_sales_tax_payable_account',
        'default_unapplied_customer_cash_account',
        'default_accounts_payable_control',
        'default_purchase_expense_account',
        'default_purchase_tax_asset_account',
        'default_bank_account_for_payments_made',
    )
    fieldsets = (
        (None, {'fields': ('company',)}),
        (_("General Ledger Defaults"), {'fields': ('default_retained_earnings_account',)}),
        (_("Accounts Receivable Defaults"), {'fields': (
            'default_accounts_receivable_control', 'default_sales_revenue_account',
            'default_sales_tax_payable_account',
            'default_unapplied_customer_cash_account')}),
        (_("Accounts Payable Defaults"), {'fields': (
            'default_accounts_payable_control', 'default_purchase_expense_account',
            'default_purchase_tax_asset_account')}),
        (_("Banking Defaults"), {'fields': ('default_bank_account_for_payments_made',)}),
        (_("Audit"), {'fields': ('created_at', 'updated_at', 'updated_by'), 'classes': ('collapse',)}),
    )

    @admin.display(description=_("Company"), ordering='company__name')
    def company_name(self, obj):
        return obj.company.name if obj.company else "---"

    @admin.display(description=_("Updated By"), ordering='updated_by__username')  # Order by a user field
    def updated_by_user(self, obj):
        if not obj.updated_by: return "N/A"
        return obj.updated_by.get_full_name() or obj.updated_by.username

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        company_for_filtering = None
        obj_id = request.resolver_match.kwargs.get('object_id')

        if obj_id:  # Editing existing settings
            try:
                settings_instance = self.model.objects.select_related('company').get(pk=obj_id)
                company_for_filtering = settings_instance.company
            except self.model.DoesNotExist:
                pass
        elif 'company' in request.POST:  # Adding new, and company field might be submitted (e.g. validation error)
            company_pk = request.POST.get('company')
            if company_pk:
                try:
                    company_for_filtering = Company.objects.get(pk=company_pk)
                except (Company.DoesNotExist, ValueError):
                    pass

        # Filter Account FKs based on the determined company
        if company_for_filtering and db_field.remote_field.model == Account:
            kwargs["queryset"] = Account.objects.filter(company=company_for_filtering).select_related(
                'account_group').order_by('account_type', 'account_name')

        # For the 'company' field itself, restrict choices for non-superusers
        if db_field.name == "company" and not request.user.is_superuser:
            manageable_company_ids = CompanyMembership.objects.filter(
                user=request.user,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True,
                company__is_active=True,
                company__is_suspended_by_admin=False
            ).values_list('company_id', flat=True)
            kwargs["queryset"] = Company.objects.filter(pk__in=list(manageable_company_ids))

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated:  # Ensure user is authenticated
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        # MODIFIED: Ensure base readonly_fields are respected
        base_ro_fields = list(super().get_readonly_fields(request, obj) or [])
        # Add specific readonly fields for this admin, not overwriting base ones
        current_ro_fields = base_ro_fields + ['created_at', 'updated_at', 'updated_by']

        if obj and obj.pk:  # Editing an existing object
            if 'company' not in current_ro_fields:
                current_ro_fields.append('company')  # Company should not be changed after settings are created
        return tuple(set(current_ro_fields))  # Use set to avoid duplicates, then tuple

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('company', 'updated_by')

        manageable_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).values_list('company_id', flat=True)

        return qs.filter(company_id__in=list(manageable_company_ids)).select_related('company', 'updated_by')

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser: return True

        base_permission_qs = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        )

        if obj is not None:  # Viewing/changing specific CompanyAccountingSettings
            if not obj.company: return False  # Safety check
            # Company of these settings must be effectively active
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False
            # User must be Owner/Admin of this specific company
            return base_permission_qs.filter(company=obj.company).exists()

        return base_permission_qs.exists()  # For list view

    def has_add_permission(self, request):
        if request.user.is_superuser: return True
        # Non-SU can add if they are Owner/Admin of at least one effectively active company
        return CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).exists()

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)  # Change perm mirrors view perm

    def has_delete_permission(self, request, obj=None):
        # Generally, accounting settings are not deleted.
        if request.user.is_superuser: return True
        # Allow deletion if user can change (which implies admin of an active company).
        return self.has_change_permission(request, obj)