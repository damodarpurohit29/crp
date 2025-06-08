# crp_accounting/admin/party.py

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.urls import reverse
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from django.utils import timezone

# --- Base Admin Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model & Enum Imports ---
from ..models.party import Party
from ..models.coa import Account
from crp_core.enums import PartyType

logger = logging.getLogger(__name__)


@admin.register(Party)
class PartyAdmin(TenantAccountingModelAdmin):
    tenant_parent_fk_fields = ['control_account']

    list_display = (
        'name',
        'party_type',
        'control_account_link',
        'is_active',
        'contact_phone',
        'credit_limit',
        'updated_at',
    )
    list_filter = (
        ('company', admin.RelatedOnlyFieldListFilter),
        'party_type',
        'is_active',
        ('control_account', admin.RelatedOnlyFieldListFilter),
        ('created_at', admin.DateFieldListFilter),
    )
    search_fields = (
        'name',
        'contact_email',
        'contact_phone',
        'company__name',
        'control_account__account_name',
        'control_account__account_number',
    )
    readonly_fields = (
        'display_balance_in_form',
        'display_credit_status_in_form',
        # 'created_at', 'updated_at' are handled by base if needed as readonly,
        # but also explicitly listed in fieldsets for collapsed section
    )
    fieldsets = (
        (None, {
            'fields': ('company', 'party_type', 'name', 'is_active')
        }),
        ('Contact Information', {
            'fields': ('contact_email', 'contact_phone', 'address')
        }),
        ('Accounting & Credit Settings', {
            'fields': (
                'control_account',
                'credit_limit',
                'display_balance_in_form',
                'display_credit_status_in_form',
            )
        }),
        ('Audit Information', {
            # Corrected: Removed 'created_by' and 'updated_by'
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    autocomplete_fields = ['control_account']
    list_per_page = 25
    actions = ['make_active', 'make_inactive_with_check']

    def get_queryset(self, request: HttpRequest):
        qs = super().get_queryset(request)
        return qs.select_related('control_account')

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)

        if db_field.name == "control_account" and field and hasattr(field, 'queryset'):
            current_qs = field.queryset
            filtered_qs = current_qs.filter(is_control_account=True)

            party_instance_type_value = None
            object_id = request.resolver_match.kwargs.get('object_id')

            if object_id:
                instance = self.get_object(request, object_id)
                if instance:
                    party_instance_type_value = instance.party_type
            else:
                if request.method == 'POST':
                    party_instance_type_value = request.POST.get('party_type')
                elif request.method == 'GET':
                    party_instance_type_value = request.GET.get('party_type')

            if party_instance_type_value:
                if isinstance(party_instance_type_value, str):
                    try:
                        enum_member = getattr(PartyType, party_instance_type_value.upper(), None)
                        if enum_member:
                            party_instance_type_value = enum_member.value
                    except AttributeError:
                        logger.debug(
                            f"PartyAdmin: Could not convert party_type string '{party_instance_type_value}' to enum value.")
                        pass

                if party_instance_type_value == PartyType.CUSTOMER.value:
                    filtered_qs = filtered_qs.filter(control_account_party_type=PartyType.CUSTOMER.value)
                elif party_instance_type_value == PartyType.SUPPLIER.value:
                    filtered_qs = filtered_qs.filter(control_account_party_type=PartyType.SUPPLIER.value)

            field.queryset = filtered_qs.order_by('account_number', 'account_name')
        return field

    @admin.display(description=_('Control Account'), ordering='control_account__account_name')
    def control_account_link(self, obj: Party):
        if obj.control_account:
            try:
                url = reverse("admin:crp_accounting_account_change", args=[obj.control_account.pk])
                return format_html('<a href="{}">{} ({})</a>', url, obj.control_account.account_name,
                                   obj.control_account.account_number)
            except Exception as e:
                logger.warning(f"PartyAdmin: Could not reverse URL for Account PK {obj.control_account.pk}: {e}")
                return str(obj.control_account)
        return _("N/A")

    @admin.display(description=_('Current Balance'))
    def display_balance_in_form(self, obj: Party):
        if not obj.pk or not obj.control_account: return _("N/A")
        try:
            balance = obj.calculate_outstanding_balance()
            return f"{balance:,.2f}"
        except Exception as e:
            logger.warning(
                f"PartyAdmin: Error calculating balance in form for Party {obj.pk} (Co: {obj.company_id}): {e}")
            return _("Error")

    @admin.display(description=_('Credit Status'))
    def display_credit_status_in_form(self, obj: Party):
        if not obj.pk or not obj.control_account: return _("N/A")
        try:
            status = obj.get_credit_status()
            color = "red" if status == "Over Credit Limit" else "green" if status == "Within Limit" else "darkgrey"
            return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, status)
        except Exception as e:
            logger.warning(
                f"PartyAdmin: Error getting credit status in form for Party {obj.pk} (Co: {obj.company_id}): {e}")
            return _("Error")

    @admin.action(description=_('Mark selected parties as active'))
    def make_active(self, request: HttpRequest, queryset):
        updated_count = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, _("%(count)d parties marked as active.") % {'count': updated_count})

    @admin.action(description=_('Mark selected parties as inactive (Check Balance)'))
    def make_inactive_with_check(self, request: HttpRequest, queryset):
        active_parties_to_check = queryset.filter(is_active=True)
        updated_count = 0
        skipped_count = 0
        check_balance_setting = getattr(settings, 'ACCOUNTING_CHECK_BALANCE_BEFORE_PARTY_DEACTIVATION', True)

        for party in active_parties_to_check:
            can_deactivate = True
            if check_balance_setting and party.control_account:
                try:
                    balance = party.calculate_outstanding_balance()
                    if balance != Decimal('0.00'):
                        can_deactivate = False
                        messages.warning(request,
                                         _("Cannot deactivate party '%(name)s' (Company: %(co_name)s) due to non-zero balance: %(balance).2f") % {
                                             'name': party.name,
                                             'co_name': party.company.name if party.company else 'N/A',
                                             'balance': balance
                                         })
                        skipped_count += 1
                except Exception as e:
                    can_deactivate = False
                    logger.error(
                        f"PartyAdmin: Error checking balance for party '{party.name}' (PK: {party.pk}, Co: {party.company_id}) before deactivation: {e}")
                    messages.error(request, _("Error checking balance for party '%(name)s'. Skipped deactivation.") % {
                        'name': party.name})
                    skipped_count += 1

            if can_deactivate:
                party.is_active = False
                party.updated_at = timezone.now()
                party.save(update_fields=['is_active', 'updated_at'])
                updated_count += 1

        if updated_count > 0:
            messages.success(request, _("%(count)d parties marked as inactive.") % {'count': updated_count})
        if skipped_count > 0:
            messages.warning(request, _("%(count)d parties were not deactivated (see other messages for details).") % {
                'count': skipped_count})