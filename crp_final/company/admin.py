# company/admin.py
import logging
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.utils import timezone  # For admin actions updating timestamps

from crp_accounting.models import Account
# Import your models
from .models import Company, CompanyGroup, CompanyMembership
from .models_settings import CompanyAccountingSettings

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
        return super().get_formset(request, obj, **kwargs)

    def has_add_permission(self, request, obj=None) -> bool:  # obj is the parent Company
        if request.user.is_superuser: return True
        if obj is None: return False
        # Company must be effectively active for members to be added
        if not (obj.is_active and not obj.is_suspended_by_admin):
            return False
        return CompanyMembership.objects.filter(
            user=request.user, company=obj,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:  # obj is CompanyMembership instance
        if request.user.is_superuser: return True
        parent_company_being_edited = getattr(request, '_company_admin_parent_obj', None)
        if obj is None:
            return self.has_add_permission(request,
                                           parent_company_being_edited) if parent_company_being_edited else False

        # Company of the membership must be effectively active
        if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
            return False
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

        # Company of the membership must be effectively active
        if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
            return False

        can_change_this_membership = self.has_change_permission(request,
                                                                obj)  # This already checks company status via obj.company
        if not can_change_this_membership: return False

        if obj.user == request.user and obj.role in [CompanyMembership.Role.OWNER.value,
                                                     CompanyMembership.Role.ADMIN.value]:
            other_admins_or_owners = obj.company.memberships.filter(
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exclude(pk=obj.pk)
            if not other_admins_or_owners.exists():
                return False
        return True


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
        if obj.is_suspended_by_admin:  # Check suspension first
            return format_html('<span style="color: red;">❌ {}</span>', _('Suspended'))
        elif obj.is_active:
            return format_html('<span style="color: green;">✅ {}</span>', _('Active'))
        else:  # Not active and not suspended (effectively inactive)
            return format_html('<span style="color: orange;">➖ {}</span>', _('Inactive'))

    effective_is_active_display.short_description = _('Effective Status')
    effective_is_active_display.admin_order_field = 'is_active'  # or a custom annotation if order by effective status

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
        return obj.created_by_user.get_name() if obj.created_by_user else _("System/N/A")

    created_by_user_display.short_description = _('Registered By')

    # Ensure user model has name or get_name() works as expected. For ordering:
    # created_by_user_display.admin_order_field = 'created_by_user__name' # or relevant field

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
        request._company_admin_parent_obj = obj
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
            company__is_suspended_by_admin=False  # Company must be effectively active
        ).values_list('company_id', flat=True)
        return qs.filter(pk__in=list(user_administered_company_ids))

    def get_inlines(self, request, obj):
        if obj is None: return []
        if request.user.is_superuser:
            return [CompanyMembershipInlineForCompanyAdmin]

        # Company must be effectively active for members to be managed
        if not (obj.is_active and not obj.is_suspended_by_admin):
            return []

        if getattr(request, 'company', None) == obj and \
                CompanyMembership.objects.filter(
                    user=request.user, company=obj,
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True
                ).exists():
            return [CompanyMembershipInlineForCompanyAdmin]
        return []

    # --- Permission Methods ---
    def has_add_permission(self, request) -> bool:
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None) -> bool:
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            # Company must be effectively active to be changed by non-SU
            if not (obj.is_active and not obj.is_suspended_by_admin):
                return False
            return CompanyMembership.objects.filter(
                user=request.user, company=obj,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        return False

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True

        base_access_qs = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False  # Company must be effectively active
        )
        if not base_access_qs.exists():
            return False

        if obj is not None:  # Viewing a specific Company detail page
            # Company itself must be effectively active to be viewed by non-SU
            if not (obj.is_active and not obj.is_suspended_by_admin):
                return False
            # User can view if they are Owner/Admin of this specific company
            return base_access_qs.filter(company=obj).exists()

        # For list view, allow if they are Owner/Admin of at least one effectively active company
        return base_access_qs.filter(
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value]
        ).exists()

    # --- SAVE MODEL WITH DETAILED LOGGING ---
    def save_model(self, request, obj: Company, form, change):
        action = "Changing" if change else "Adding"
        # Assuming request.user.name exists; otherwise use request.user.get_name() or str(request.user)
        log_prefix = f"CompanyAdmin SaveModel (User:{getattr(request.user, 'name', request.user.get_name())}, Action:{action}, PK:{obj.pk or 'NEW'}):"
        logger.info(f"{log_prefix} --- Initiating save ---")

        original_currency_on_obj_entry = obj.default_currency_code if obj.pk else "N/A (New Record before form bind)"
        submitted_currency_via_form_cleaned_data = form.cleaned_data.get(
            'default_currency_code') if form.is_valid() else "Form Invalid or Field Missing"

        logger.info(
            f"{log_prefix} 1. Form cleaned_data['default_currency_code'] (if valid): '{submitted_currency_via_form_cleaned_data}'")
        logger.info(
            f"{log_prefix} 2. obj.default_currency_code AT ENTRY (should match form if field was bound): '{obj.default_currency_code}'")
        if obj.pk and original_currency_on_obj_entry != obj.default_currency_code:
            logger.info(
                f"{log_prefix} Note: obj.default_currency_code ('{obj.default_currency_code}') differs from original DB value ('{original_currency_on_obj_entry}') due to form binding.")

        if not change and not obj.created_by_user_id and request.user.is_superuser:
            obj.created_by_user = request.user
            logger.info(
                f"{log_prefix} Set created_by_user to: {getattr(request.user, 'name', request.user.get_name())}")

        logger.info(
            f"{log_prefix} 3. obj.default_currency_code BEFORE super().save_model() call: '{obj.default_currency_code}'")

        try:
            super().save_model(request, obj, form, change)
            logger.info(f"{log_prefix} super().save_model() completed successfully.")
        except Exception as e:
            logger.error(f"{log_prefix} Error during super().save_model(): {e}", exc_info=True)
            raise

        try:
            refreshed_obj_for_check = Company.objects.get(pk=obj.pk)
            final_db_currency = refreshed_obj_for_check.default_currency_code
            logger.info(
                f"{log_prefix} 4. default_currency_code AFTER ALL SAVES (fetched from DB): '{final_db_currency}'")

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
        updated_count = queryset.update(is_active=False,
                                        updated_at=timezone.now())  # Note: this doesn't touch is_suspended_by_admin
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
    list_display = ('user_display', 'company_display', 'role_display', 'is_active_membership',
                    'is_default_for_user', 'effective_can_access_display', 'date_joined')
    list_filter_base = ('role', 'is_active_membership', 'is_default_for_user')
    search_fields = (
    'user__name', 'user__email', 'user__first_name', 'user__last_name',  # Assuming user model has 'name'
    'company__name', 'company__subdomain_prefix')
    autocomplete_fields = ['user', 'company']
    readonly_fields = ('date_joined', 'last_accessed_at_display')
    actions = ['admin_action_make_memberships_active', 'admin_action_make_memberships_inactive']
    fieldsets = (
        (None, {'fields': ('user', 'company', 'role')}),
        (_('Membership Details'), {'fields': ('is_active_membership', 'is_default_for_user', 'can_manage_members')}),
        (_('Audit Information'), {'fields': ('date_joined', 'last_accessed_at_display'), 'classes': ('collapse',)}),
    )

    def user_display(self, obj: CompanyMembership):
        return obj.user.get_full_name() or obj.user.get_name() if obj.user else _("User Deleted")

    user_display.short_description = _('User')
    user_display.admin_order_field = 'user__last_name'  # or user__name if that's the primary sort field

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
        # This uses the property on CompanyMembership model
        can_access = obj.is_active_membership and \
                     obj.company.is_active and \
                     not obj.company.is_suspended_by_admin and \
                     obj.user.is_active

        return format_html('<span style="color: green;">✅ {}</span>', _('Can Access')) if can_access \
            else format_html('<span style="color: red;">❌ {}</span>', _('No Access'))

    effective_can_access_display.short_description = _('Effective Access')

    def get_list_filter(self, request):
        return ('company',) + self.list_filter_base if request.user.is_superuser else self.list_filter_base

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('user', 'company', 'company__created_by_user')
        if request.user.is_superuser: return qs

        # Non-SUs see memberships of companies they are Owner/Admin of,
        # and those companies must be effectively active.
        user_manageable_company_ids = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False  # THIS WAS THE KEY FIX AREA
        ).values_list('company_id', flat=True)
        return qs.filter(company_id__in=list(user_manageable_company_ids))

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser:
            # Companies non-SU can manage memberships for (must be effectively active)
            user_manageable_companies_qs = Company.objects.filter(
                pk__in=CompanyMembership.objects.filter(
                    user=request.user,
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True
                    # The company__is_active/is_suspended checks apply to the Company query below
                ).values_list('company_id', flat=True),
                is_active=True,  # Company itself must be active
                is_suspended_by_admin=False  # And not suspended
            ).distinct().order_by('name')

            if 'company' in form.base_fields:
                form.base_fields['company'].queryset = user_manageable_companies_qs
                if user_manageable_companies_qs.count() == 1 and \
                        (obj is None or (obj.company and obj.company.pk == user_manageable_companies_qs.first().pk)):
                    form.base_fields['company'].initial = user_manageable_companies_qs.first()
                    # Make it readonly, not disabled, if there's only one choice and it matches
                    # or if adding new and only one choice.
                    # Disabling can prevent submission.
                    form.base_fields['company'].widget.attrs['readonly'] = True
                    form.base_fields['company'].widget.attrs['disabled'] = False  # Ensure not disabled
                elif not user_manageable_companies_qs.exists():
                    form.base_fields['company'].queryset = Company.objects.none()
        return form

    # Permissions
    def has_add_permission(self, request) -> bool:
        if request.user.is_superuser:
            return True
        # Non-SU can add if they are Owner/Admin of at least one effectively active company
        return CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).exists()

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            # Company of the membership must be effectively active
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False
            # User must be Owner/Admin of that company
            return CompanyMembership.objects.filter(
                user=request.user, company=obj.company,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        # For add page context (obj is None), rely on has_add_permission
        return self.has_add_permission(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser: return True
        if obj is not None:
            # Check change permission first (includes company active status and user role)
            can_change = self.has_change_permission(request, obj)
            if not can_change: return False

            # Prevent deleting own Owner/Admin membership if it's the last one
            if obj.user == request.user and obj.role in [CompanyMembership.Role.OWNER.value,
                                                         CompanyMembership.Role.ADMIN.value]:
                other_admins_or_owners = obj.company.memberships.filter(
                    role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                    is_active_membership=True,
                    # Ensure the company itself is considered effectively active for these other memberships
                    company__is_active=True,
                    company__is_suspended_by_admin=False
                ).exclude(pk=obj.pk)
                if not other_admins_or_owners.exists():
                    return False
            return True
        # For add page context (obj is None), rely on has_add_permission (though delete isn't typical here)
        return self.has_add_permission(request)

    def has_view_permission(self, request, obj=None) -> bool:
        if request.user.is_superuser:
            return True

        # Base check: user must have an active membership in at least one effectively active company
        # where they are an Owner/Admin to view the CompanyMembership list or specific items they manage.
        current_user_admin_memberships = CompanyMembership.objects.filter(
            user=request.user,
            is_active_membership=True,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            company__is_active=True,
            company__is_suspended_by_admin=False
        )

        if not current_user_admin_memberships.exists():
            # Allow viewing own memberships even if not admin, provided company is active
            if obj and obj.user_id == request.user.pk:
                return obj.company.is_active and not obj.company.is_suspended_by_admin and obj.is_active_membership
            return False  # No admin rights to any relevant company, cannot view list or other's memberships

        if obj is not None:  # Viewing a specific CompanyMembership
            # Company of the membership must be effectively active
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False

            # Can view own membership
            if obj.user_id == request.user.pk and obj.is_active_membership:  # Own active membership in an active company
                return True

            # Can view if they are Owner/Admin of the membership's company
            return current_user_admin_memberships.filter(company=obj.company).exists()

        # For list view (obj is None): access granted if they are Owner/Admin of at least one effectively active company
        return True  # Already checked by current_user_admin_memberships.exists() at the top for this path

    # Admin Actions for CompanyMembership
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

        for m in queryset:
            can_deactivate = True
            reason = ""

            if not self.has_change_permission(request, m):
                can_deactivate = False
                reason = _("no change permission or company inactive/suspended")
            elif m.role in [CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value] and \
                    not m.company.memberships.filter(
                        role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                        is_active_membership=True,
                        # Ensure the company is effectively active for this check
                        company__is_active=True,
                        company__is_suspended_by_admin=False
                    ).exclude(pk=m.pk).exists():
                can_deactivate = False
                reason = _("sole active Owner/Admin in an active company")

            if can_deactivate:
                pks_to_update.append(m.pk)
            elif reason:
                skipped_msgs.append(f"Skipped {m.user.get_name()} in {m.company.name} ({reason}).")

        if pks_to_update:
            updated_count = CompanyMembership.objects.filter(pk__in=pks_to_update).update(is_active_membership=False)

        if updated_count > 0:
            messages.success(request, _(f"{updated_count} memberships deactivated."))
        for msg in skipped_msgs:
            messages.warning(request, msg)
        if not pks_to_update and not skipped_msgs and queryset.exists():  # Check if queryset was not empty
            messages.info(request, _("No memberships were eligible for deactivation."))


@admin.register(CompanyAccountingSettings)
class CompanyAccountingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'company_name', 'default_retained_earnings_account', 'default_accounts_receivable_control', 'updated_at',
        'updated_by_user')
    readonly_fields = ('created_at', 'updated_at', 'updated_by')  # 'company' is handled in get_readonly_fields

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

    @admin.display(description=_("Updated By"), ordering='updated_by__name')  # or updated_by__name
    def updated_by_user(self, obj):
        return obj.updated_by.get_full_name() if obj.updated_by else "N/A"  # Use get_name for flexibility

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        company_for_filtering = None

        # Try to get company from instance being edited
        obj_id = request.resolver_match.kwargs.get('object_id')
        if obj_id:
            try:
                # Using self.model to fetch to avoid recursion if get_object is overridden complexly
                settings_instance = self.model.objects.select_related('company').get(pk=obj_id)
                company_for_filtering = settings_instance.company
            except self.model.DoesNotExist:
                pass

        # If adding, try to get company from selected value in the form (if already submitted/partially filled)
        # This relies on the 'company' field ID in the form.
        if not company_for_filtering and 'company' in request.POST:
            company_pk = request.POST.get('company')
            if company_pk:
                try:
                    company_for_filtering = Company.objects.get(pk=company_pk)
                except (Company.DoesNotExist, ValueError):
                    pass

        # If company_for_filtering is determined, filter Account FKs
        if company_for_filtering and db_field.remote_field.model == Account:
            kwargs["queryset"] = Account.objects.filter(company=company_for_filtering).select_related(
                'account_group').order_by('account_type', 'account_name')

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        # Start with base readonly fields
        current_ro_fields = list(self.readonly_fields)
        if obj:  # Editing an existing object
            current_ro_fields.append('company')  # Company should not be changed after settings are created
        return tuple(current_ro_fields)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('company', 'updated_by')  # Added updated_by

        # Non-SU can only see settings for companies they are Owner/Admin of AND are effectively active
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

        # Check if user has admin rights to any effectively active company
        base_permission = CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).exists()

        if not base_permission: return False  # No admin rights to any relevant company

        if obj is not None:  # Viewing/changing specific CompanyAccountingSettings
            # Company of these settings must be effectively active
            if not (obj.company.is_active and not obj.company.is_suspended_by_admin):
                return False
            # User must be Owner/Admin of this specific company
            return CompanyMembership.objects.filter(
                user=request.user,
                company=obj.company,
                role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
                is_active_membership=True
            ).exists()
        return True  # For list view, if base_permission is True

    def has_add_permission(self, request):
        if request.user.is_superuser: return True
        # Non-SU can add if they are Owner/Admin of at least one effectively active company
        # (the form will then restrict which company they can choose)
        return CompanyMembership.objects.filter(
            user=request.user,
            role__in=[CompanyMembership.Role.OWNER.value, CompanyMembership.Role.ADMIN.value],
            is_active_membership=True,
            company__is_active=True,
            company__is_suspended_by_admin=False
        ).exists()

    def has_change_permission(self, request, obj=None):
        # Same logic as view permission for a specific object
        return self.has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # Typically, settings are not deleted but maybe deactivated or changed.
        # If deletion is allowed, same logic as change.
        return self.has_change_permission(request, obj)