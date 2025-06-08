# crp_accounting/admin/coa.py

import logging
from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError

# --- Model Imports ---
from ..models.coa import AccountGroup, Account

# --- Base Admin Import ---
# Assuming admin_base.py is in the same directory (crp_accounting/admin/admin_base.py)
from .admin_base import TenantAccountingModelAdmin

logger = logging.getLogger("crp_accounting.admin.coa")


# =============================================================================
# Inline Admin: Manage Accounts within AccountGroup
# =============================================================================
class AccountInline(admin.TabularInline):
    model = Account
    fields = (
        'account_number', 'account_name',
        'get_account_type_display',
        'is_active', 'allow_direct_posting',
        'current_balance_display_inline',
    )
    readonly_fields = (
        'current_balance_display_inline',
        'get_account_type_display',
        'account_number',
    )
    extra = 0
    show_change_link = True
    ordering = ('account_number',)
    verbose_name = _("Account in this Group")
    verbose_name_plural = _("Accounts in this Group")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('account_group')

    @admin.display(description=_("Balance"))
    def current_balance_display_inline(self, obj: Account):
        return f"{obj.current_balance:,.2f} {obj.currency}" if obj.currency else f"{obj.current_balance:,.2f}"


# =============================================================================
# Admin: AccountGroup
# =============================================================================
@admin.register(AccountGroup)
class AccountGroupAdmin(TenantAccountingModelAdmin):
    list_display = (
        'name', 'parent_group_display',
        'get_full_path_display', 'description_short',
        'account_count_display'
        # 'get_record_company_display' is added by TenantAccountingModelAdmin for superusers list view
    )
    search_fields = ('name', 'description', 'parent_group__name', 'company__name')

    # These are MODEL fields that should be readonly only on the change form,
    # not affecting the 'company_admin_form_link' which is handled by base's get_readonly_fields
    readonly_model_fields_on_change = ('created_at', 'updated_at')

    autocomplete_fields = ['parent_group', 'company']
    inlines = [AccountInline]
    list_select_related = ('parent_group', 'company')

    list_filter_non_superuser = (('parent_group', admin.RelatedOnlyFieldListFilter),)
    list_filter_superuser = ('company', ('parent_group', admin.RelatedOnlyFieldListFilter),)

    actions = ['action_soft_delete_selected', 'action_undelete_selected']

    add_fieldsets = (
        (None, {'fields': ('company', 'name', 'parent_group', 'description')}),
        # If you want the link on ADD form, company must be selected first for link to show
        # (_('Company Details'), {'fields': ,)}),
    )
    change_fieldsets = (
        (None, {'fields': ('name', 'parent_group', 'description')}),
        (_('Audit Information'),
         {'fields': (
             'company',  # The actual FK, base admin handles its readonly state
             'created_at',
             'updated_at'
         ), 'classes': ('collapse',)}),
    )

    def get_fieldsets(self, request, obj=None):
        if obj: return self.change_fieldsets
        return self.add_fieldsets

    def get_readonly_fields(self, request, obj=None):
        # Get readonly fields from the parent class (TenantAccountingModelAdmin)
        # This now includes 'company_admin_form_link' if applicable and other base logic.
        ro_from_super = set(super().get_readonly_fields(request, obj) or [])

        if obj:  # Editing an existing object
            # Add model fields specific to this admin that should be readonly on change
            ro_from_super.update(self.readonly_model_fields_on_change or ())

        return tuple(ro_from_super)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_account_count_annotation=Count('accounts')).order_by('name')

    def get_list_filter(self, request):
        return self.list_filter_superuser if request.user.is_superuser else self.list_filter_non_superuser

    @admin.display(description=_('Parent Group'), ordering='parent_group__name')
    def parent_group_display(self, obj: AccountGroup) -> str:
        return obj.parent_group.name if obj.parent_group else "—"

    @admin.display(description=_('Full Path'))
    def get_full_path_display(self, obj: AccountGroup) -> str:
        return obj.get_full_path()

    @admin.display(description=_('No. of Accounts'), ordering='_account_count_annotation')
    def account_count_display(self, obj: AccountGroup) -> int:
        return getattr(obj, '_account_count_annotation', 0)

    @admin.display(description=_('Description'))
    def description_short(self, obj: AccountGroup) -> str:
        return (obj.description[:57] + "...") if obj.description and len(obj.description) > 60 else (
                obj.description or "—")


# =============================================================================
# Admin: Account
# =============================================================================
@admin.register(Account)
class AccountAdmin(TenantAccountingModelAdmin):
    list_display = (
        'account_number',
        'account_name',
        'account_group_display',
        'account_type',
        'pl_section',
        'account_nature',
        'current_balance_display',
        'is_active_display',
        'view_ledger_link'
        # 'get_record_company_display' is added by TenantAccountingModelAdmin for superusers list view
    )

    list_filter_base = (
        'is_active',
        'account_type',
        'pl_section',
        'account_nature',
        ('account_group', admin.RelatedOnlyFieldListFilter),
        'is_control_account',
        'currency',  # 'currency' filter defined once
        'allow_direct_posting'
    )

    search_fields = ('account_number', 'account_name', 'description', 'account_group__name', 'company__name')

    # These are MODEL fields that should be readonly only on the change form
    readonly_model_fields_on_change = (
        'account_nature', 'current_balance', 'balance_last_updated',
        'created_at', 'updated_at', 'account_number',
    )

    autocomplete_fields = ['account_group', 'company']
    list_select_related = ('account_group', 'company')
    actions = ['admin_action_make_accounts_active', 'admin_action_make_accounts_inactive',
               'action_soft_delete_selected', 'action_undelete_selected']
    list_per_page = 30
    ordering = ('account_group__name', 'account_number')

    fieldsets_add = (
        (None, {'fields': ('company', 'account_number', 'account_name', 'account_group', 'description')}),
        (_('Classification'), {'fields': ('account_type', 'pl_section', 'currency')}),
        (_('Settings'),
         {'fields': ('is_active', 'allow_direct_posting', 'is_control_account', 'control_account_party_type')}),
    )
    fieldsets_change = (
        (None, {'fields': ('account_name', 'account_group', 'description')}),
        (_('Classification'), {'fields': ('account_type', 'pl_section', 'account_nature', 'currency')}),
        (_('Settings'),
         {'fields': ('is_active', 'allow_direct_posting', 'is_control_account', 'control_account_party_type')}),
        (_('Balance Information (Read-Only)'), {'fields': ('current_balance', 'balance_last_updated')}),
        (_('Audit Information'),
         {'fields': (
             'company',  # The actual FK, base admin handles its readonly state
             'created_at',
             'updated_at'
         ), 'classes': ('collapse',)}),
    )

    def get_fieldsets(self, request, obj=None):
        if obj: return self.fieldsets_change
        return self.fieldsets_add

    def get_readonly_fields(self, request, obj=None):
        # Get readonly fields from the parent class (TenantAccountingModelAdmin)
        # This now includes 'company_admin_form_link' if applicable and other base logic.
        ro_from_super = set(super().get_readonly_fields(request, obj) or [])

        if obj:  # Editing an existing object
            # Add model fields specific to this admin that should be readonly on change
            ro_from_super.update(self.readonly_model_fields_on_change or ())

        return tuple(ro_from_super)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs

    def get_list_filter(self, request):
        if request.user.is_superuser:
            # Add 'company' filter for superusers
            # The self.list_filter_base already contains 'currency' once
            return ('company',) + self.list_filter_base
        return self.list_filter_base

    @admin.display(description=_('Account Group'), ordering='account_group__name')
    def account_group_display(self, obj: Account) -> str:
        return obj.account_group.name if obj.account_group else "—"

    @admin.display(description=_('Current Balance'), ordering='current_balance')
    def current_balance_display(self, obj: Account) -> str:
        cur_sym = obj.currency or ""
        return f"{obj.current_balance:,.2f} {cur_sym}".strip()

    @admin.display(description=_('Is Active'), boolean=True, ordering='is_active')
    def is_active_display(self, obj: Account) -> bool:
        return obj.is_active

    # crp_accounting/admin/coa.py (inside AccountAdmin)
    # Add at the top:
    # import logging
    # from django.urls import reverse, NoReverseMatch
    # from django.utils.html import format_html
    # from django.utils.translation import gettext_lazy as _
    # logger = logging.getLogger(__name__) # Or your app-specific logger

    @admin.display(description=_('Ledger'))
    def view_ledger_link(self, obj: Account) -> str:
        if not obj.pk:  # Good check to ensure obj is saved
            logger.debug(f"Account object {obj} has no PK, cannot generate ledger link.")
            return "— (Unsaved)"  # Or "— (No PK)"
        if not obj.company_id:  # Account must belong to a company
            logger.warning(f"Account {obj.pk} has no company_id. Ledger link cannot be reliably generated.")
            return "— (No Company)"  # Or make link disabled

        ledger_url_name = 'crp_accounting_api:admin-view-account-ledger'  # YOUR DESIRED URL NAME
        try:
            # This URL will be /api/accounting/admin-reports/account/<account_pk>/ledger/
            # if your app_name is 'crp_accounting_api' and path name is 'admin-view-account-ledger'
            url = reverse(ledger_url_name, args=[obj.pk])  # Only account_pk is passed in args
            return format_html(
                '<a href="{}" target="_blank" title="{}"><i class="fas fa-file-invoice-dollar"></i> {}</a>',
                url, _("View Account Ledger for {}").format(obj.account_name), _("View"))
        except NoReverseMatch:
            logger.error(
                f"Admin COA: NoReverseMatch for ledger URL '{ledger_url_name}' with args '[{obj.pk}]'. "
                f"Ensure URL pattern exists in 'crp_accounting.urls_api' (with app_name='crp_accounting_api') "
                f"and the path name is 'admin-view-account-ledger'."
            )
            return "— (Link Setup Error)"
        except Exception as e:
            logger.error(f"Admin COA: Error generating ledger link for account {obj.pk}: {e}", exc_info=True)
            return _("Link Error")

    @admin.action(description=_('Mark selected accounts as ACTIVE'))
    def admin_action_make_accounts_active(self, request, queryset):
        updated_count = 0;
        skipped_count = 0
        for account in queryset:
            try:
                if not account.is_active:
                    account.is_active = True
                    account.save(update_fields=['is_active', 'updated_at'])
                    updated_count += 1
            except DjangoValidationError as e:
                skipped_count += 1;
                self.message_user(request,
                                  _("Could not activate '%(name)s': %(error)s") % {'name': account.account_name,
                                                                                   'error': e.message_dict or e.messages_joined},
                                  messages.ERROR)
            except Exception as e:
                skipped_count += 1;
                logger.error(f"Error activating account {account.pk} via admin: {e}", exc_info=True);
                self.message_user(request,
                                  _("Unexpected error activating '%(name)s'.") % {'name': account.account_name},
                                  messages.ERROR)
        if updated_count: self.message_user(request,
                                            _("%(count)d account(s) marked as active.") % {'count': updated_count},
                                            messages.SUCCESS)
        if skipped_count: self.message_user(request,
                                            _("%(count)d account(s) skipped due to errors.") % {'count': skipped_count},
                                            messages.WARNING)

    @admin.action(description=_('Mark selected accounts as INACTIVE'))
    def admin_action_make_accounts_inactive(self, request, queryset):
        updated_count = 0;
        skipped_count = 0
        for account in queryset:
            try:
                if account.is_active:
                    account.is_active = False
                    account.save(update_fields=['is_active', 'updated_at'])
                    updated_count += 1
            except DjangoValidationError as e:
                skipped_count += 1;
                self.message_user(request,
                                  _("Could not deactivate '%(name)s': %(error)s") % {'name': account.account_name,
                                                                                     'error': e.message_dict or e.messages_joined},
                                  messages.ERROR)
            except Exception as e:
                skipped_count += 1;
                logger.error(f"Error deactivating account {account.pk} via admin: {e}", exc_info=True);
                self.message_user(request,
                                  _("Unexpected error deactivating '%(name)s'.") % {'name': account.account_name},
                                  messages.ERROR)
        if updated_count: self.message_user(request,
                                            _("%(count)d account(s) marked as inactive.") % {'count': updated_count},
                                            messages.SUCCESS)
        if skipped_count: self.message_user(request,
                                            _("%(count)d account(s) skipped due to errors.") % {'count': skipped_count},
                                            messages.WARNING)