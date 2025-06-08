# crp_accounting/admin/receivables.py

import logging
from decimal import Decimal
from typing import Optional, Any, Tuple, Callable, Dict, List

from django.contrib import admin, messages
from django.db import models, transaction
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied, \
    ObjectDoesNotExist
from django.http import HttpRequest
from django.conf import settings
from django.utils import timezone

# --- Base Admin Class Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model Imports ---
from ..models.receivables import (
    InvoiceSequence,
    CustomerInvoice, InvoiceLine, CustomerPayment, PaymentAllocation,
    InvoiceStatus, PaymentStatus, CurrencyType, CorePartyType, CoreAccountType  # Enums
)
from ..models.coa import Account
from ..models.party import Party
from ..models.journal import Voucher  # Use Voucher.VoucherType if needed

from company.models import Company

# --- Service Imports ---
from ..services import receivables_service
from ..services.receivables_service import (
    ReceivablesServiceError, InvoiceProcessingError, PaymentProcessingError,
    GLPostingError, SequenceGenerationError
)

logger = logging.getLogger("crp_accounting.admin.receivables")
ZERO = Decimal('0.00')


# =============================================================================
# InvoiceSequence Admin
# =============================================================================
@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(TenantAccountingModelAdmin):
    list_display = (
        'company_name_display',
        'prefix',
        'period_format_for_reset_display',
        'current_period_key_display',
        'padding_digits',
        'last_number',
        'updated_at_short'
    )
    list_filter_non_superuser = ('prefix', 'period_format_for_reset')
    search_fields = ('company__name', 'prefix', 'current_period_key')
    ordering = ('company__name', 'prefix', '-current_period_key')

    fields = ('company', 'prefix', 'period_format_for_reset', 'padding_digits', 'current_period_key', 'last_number')
    readonly_fields_base = ('current_period_key', 'last_number', 'created_at', 'updated_at')

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if hasattr(self.model, 'created_by'): ro.add('created_by')
        if hasattr(self.model, 'updated_by'): ro.add('updated_by')
        return tuple(ro)

    @admin.display(description=_('Company'), ordering='company__name')
    def company_name_display(self, obj: InvoiceSequence) -> str:
        if obj.company:
            return obj.company.name
        elif obj.company_id:
            try:
                return Company.objects.get(pk=obj.company_id).name
            except Company.DoesNotExist:
                return f"Company ID: {obj.company_id} (Not Found)"
        return "—"


    @admin.display(description=_('Period Format'), ordering='period_format_for_reset')
    def period_format_for_reset_display(self, obj: InvoiceSequence) -> str:
        return obj.period_format_for_reset or _("Continuous (No Reset)")

    @admin.display(description=_('Current Period Key'), ordering='current_period_key')
    def current_period_key_display(self, obj: InvoiceSequence) -> str:
        return obj.current_period_key or "—"

    @admin.display(description=_('Last Updated'), ordering='updated_at')
    def updated_at_short(self, obj: InvoiceSequence) -> str:
        if obj.updated_at: return obj.updated_at.strftime('%Y-%m-%d %H:%M')
        return "—"


# =============================================================================
# InvoiceLine Inline Admin
# =============================================================================
class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    fields = ('description', 'quantity', 'unit_price', 'revenue_account', 'tax_amount_on_line', 'line_total_display')
    readonly_fields = ('line_total_display',)
    extra = 0
    autocomplete_fields = ['revenue_account']
    verbose_name = _("Invoice Line Item")
    verbose_name_plural = _("Invoice Line Items")
    classes = ['collapse'] if not settings.DEBUG else []

    @admin.display(description=_('Line Total'))
    def line_total_display(self, obj: InvoiceLine) -> str:
        currency_symbol = ""
        parent_invoice = getattr(obj, 'invoice', None)
        if parent_invoice and parent_invoice.currency: currency_symbol = parent_invoice.currency
        return f"{obj.line_total or ZERO:.2f} {currency_symbol}"

    def _get_parent_invoice_context(self, request: HttpRequest) -> Optional[CustomerInvoice]:
        return getattr(request, '_current_parent_invoice_for_line_inline', None)

    def _get_company_for_inline_filtering(self, request: HttpRequest, parent_invoice: Optional[CustomerInvoice]) -> \
            Optional[Company]:
        if parent_invoice and parent_invoice.pk and parent_invoice.company_id:
            if hasattr(parent_invoice, 'company') and parent_invoice.company: return parent_invoice.company
            try:
                return Company.objects.get(pk=parent_invoice.company_id)
            except Company.DoesNotExist:
                return None
        is_parent_add_view = not (parent_invoice and parent_invoice.pk)
        if is_parent_add_view and request.method == 'POST':
            company_pk = request.POST.get('company')
            if company_pk:
                try:
                    return Company.objects.get(pk=company_pk)
                except (Company.DoesNotExist, ValueError, TypeError):
                    pass
        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company): return request_company
        return None

    def get_formset(self, request: Any, obj: Optional[CustomerInvoice] = None, **kwargs: Any) -> Any:
        request._current_parent_invoice_for_line_inline = obj
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_invoice = self._get_parent_invoice_context(request)
        company_for_filtering = self._get_company_for_inline_filtering(request, parent_invoice)

        if db_field.name == "revenue_account":
            if company_for_filtering:
                kwargs["queryset"] = Account.objects.filter(
                    company=company_for_filtering,
                    account_type=getattr(CoreAccountType, 'INCOME', 'INCOME'),
                    is_active=True, allow_direct_posting=True
                ).select_related('account_group').order_by('account_group__name', 'account_number')
            else:
                kwargs["queryset"] = Account.objects.none()
                is_add_view_of_parent = not (parent_invoice and parent_invoice.pk)
                if is_add_view_of_parent and request.method == 'GET' and not company_for_filtering and not request.POST.get(
                        'company'):
                    messages.info(request, _("Select 'Company' on main form to populate Revenue Accounts."))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_invoice_editable(self, parent_invoice: Optional[CustomerInvoice]) -> bool:
        if parent_invoice is None: return True
        return parent_invoice.status == getattr(InvoiceStatus, 'DRAFT', 'DRAFT')

    def has_add_permission(self, request, obj=None):
        parent_invoice = self._get_parent_invoice_context(request) or obj
        return super().has_add_permission(request, parent_invoice) and self._is_parent_invoice_editable(parent_invoice)

    def has_change_permission(self, request, obj=None):
        parent_invoice = obj.invoice if isinstance(obj,
                                                   InvoiceLine) and obj.invoice_id else self._get_parent_invoice_context(
            request)
        if parent_invoice is None and isinstance(obj, CustomerInvoice): parent_invoice = obj
        return super().has_change_permission(request, obj) and self._is_parent_invoice_editable(parent_invoice)

    def has_delete_permission(self, request, obj=None):
        parent_invoice = obj.invoice if isinstance(obj,
                                                   InvoiceLine) and obj.invoice_id else self._get_parent_invoice_context(
            request)
        if parent_invoice is None and isinstance(obj, CustomerInvoice): parent_invoice = obj
        return super().has_delete_permission(request, obj) and self._is_parent_invoice_editable(parent_invoice)


# =============================================================================
# CustomerInvoice Admin
# =============================================================================
@admin.register(CustomerInvoice)
class CustomerInvoiceAdmin(TenantAccountingModelAdmin):
    list_display = (
        'invoice_number_display', 'customer_link', 'invoice_date', 'due_date', 'total_amount_display',
        'amount_due_display', 'status_colored', 'related_gl_voucher_link', 'created_at_short')
    list_filter_non_superuser = (
        'status', ('customer', admin.RelatedOnlyFieldListFilter), ('invoice_date', admin.DateFieldListFilter),
        ('due_date', admin.DateFieldListFilter), 'currency')
    search_fields = (
    'invoice_number', 'customer__name', 'company__name')  # ESSENTIAL for autocomplete in PaymentAllocationInline
    readonly_fields_base = (
        'subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due', 'related_gl_voucher_link',
        'created_by', 'created_at', 'updated_by', 'updated_at')
    autocomplete_fields = ['company', 'customer']
    inlines = [InvoiceLineInline]
    actions = ['admin_action_mark_invoices_sent', 'admin_action_post_invoices_to_gl', 'admin_action_void_invoices',
               'action_soft_delete_selected', 'action_undelete_selected']
    ordering = ('-invoice_date', '-created_at')
    date_hierarchy = 'invoice_date'
    list_select_related = ('company', 'customer', 'created_by', 'related_gl_voucher__company')

    add_fieldsets = (
        (None, {'fields': ('company', 'customer', 'invoice_date', 'due_date', 'currency')}),
        (_('Details'), {'fields': ('invoice_number', 'terms', 'notes_to_customer', 'internal_notes')})
    )
    change_fieldsets_draft = (
        (None, {'fields': ('company', 'customer', 'invoice_number', 'invoice_date', 'due_date', 'currency', 'status')}),
        (_('Details'), {'fields': ('terms', 'notes_to_customer', 'internal_notes')}),
        (_('Financials (Calculated)'),
         {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'), 'classes': ('collapse',)})
    )
    change_fieldsets_non_draft = (
        (None, {'fields': ('company', 'customer', 'invoice_number', 'invoice_date', 'due_date', 'currency', 'status')}),
        (
        _('Details Read-Only'), {'fields': ('terms', 'notes_to_customer', 'internal_notes'), 'classes': ('collapse',)}),
        (_('Financials (Calculated)'),
         {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'), 'classes': ('collapse',)})
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None: return self.add_fieldsets
        draft_status_value = getattr(InvoiceStatus, 'DRAFT', 'DRAFT')
        return self.change_fieldsets_draft if obj.status == draft_status_value else self.change_fieldsets_non_draft

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        draft_status_value = getattr(InvoiceStatus, 'DRAFT', 'DRAFT')
        if obj:
            if obj.status != draft_status_value:
                ro.update(['customer', 'invoice_date', 'due_date', 'currency', 'terms', 'notes_to_customer',
                           'internal_notes'])
            if obj.invoice_number and obj.invoice_number.strip(): ro.add('invoice_number')
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context = self._get_company_from_request_obj_or_form(
            request,
            obj=self.get_object(request,
                                request.resolver_match.kwargs.get('object_id')) if request.resolver_match.kwargs.get(
                'object_id') else None,
            form_data_for_add_view_post=request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
                'object_id') else None
        )
        if db_field.name == "customer":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(
                    company=company_context, party_type=getattr(CorePartyType, 'CUSTOMER', 'CUSTOMER'), is_active=True
                ).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request: HttpRequest, obj: CustomerInvoice, form: Any, change: bool):
        log_prefix = f"[CIAdmin SaveM][User:{request.user.name if request.user else 'Anon'}][Inv:{obj.pk or 'New'}]"
        # Invoice number generation logic is handled within CustomerInvoice.save()
        try:
            super().save_model(request, obj, form, change)  # Calls obj.save()
            logger.info(
                f"{log_prefix} Invoice header saved (PK: {obj.pk}, Num: {obj.invoice_number}). Status: {obj.status}")
        except DjangoValidationError as e_val:
            logger.warning(
                f"{log_prefix} Validation error during save: {e_val.message_dict if hasattr(e_val, 'message_dict') else e_val}")
            form._update_errors(e_val);
            return
        except SequenceGenerationError as e_seq:
            logger.error(f"{log_prefix} Failed to generate invoice number: {e_seq}")
            form.add_error('invoice_number', DjangoValidationError(
                _("Failed to generate invoice number: %(err)s. Check sequence setup or manually enter.") % {
                    'err': str(e_seq)},
                code='inv_num_gen_fail'
            ));
            return
        except Exception as e_save:
            logger.exception(f"{log_prefix} Unexpected error saving invoice header.")
            messages.error(request, _("Unexpected error saving invoice: %(err)s") % {'err': str(e_save)});
            return

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        invoice_instance: CustomerInvoice = form.instance
        if invoice_instance and invoice_instance.pk:
            logger.debug(
                f"[CIAdmin SaveFS] Recalc totals for Inv {invoice_instance.invoice_number or invoice_instance.pk}")
            invoice_instance._recalculate_totals_and_due(perform_save=True)  # Assuming this method exists and works
            logger.debug(
                f"[CIAdmin SaveFS] Inv {invoice_instance.invoice_number} totals/status updated. Final status: {invoice_instance.get_status_display()}")

    @admin.display(description=_('Inv. No.'), ordering='invoice_number')
    def invoice_number_display(self, obj: CustomerInvoice):
        return obj.invoice_number or f"{_('Draft')} (ID:{obj.pk})"

    @admin.display(description=_('Customer'), ordering='customer__name')
    def customer_link(self, obj: CustomerInvoice):
        if not obj.customer_id: return "—"
        try:
            cust_name = obj.customer.name if hasattr(obj, 'customer') and obj.customer else Party.objects.values_list(
                'name', flat=True).get(pk=obj.customer_id)
            link = reverse("admin:crp_accounting_party_change", args=[obj.customer_id])
            return format_html('<a href="{}">{}</a>', link, cust_name)
        except (NoReverseMatch, Party.DoesNotExist):
            return str(obj.customer_id or "Error")

    @admin.display(description=_('Total Amt'), ordering='total_amount')
    def total_amount_display(self, obj: CustomerInvoice):
        return f"{obj.total_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Amt Due'), ordering='amount_due')
    def amount_due_display(self, obj: CustomerInvoice):
        return f"{obj.amount_due or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: CustomerInvoice):
        color_map = {
            getattr(InvoiceStatus, 'DRAFT', 'DRAFT'): "grey", getattr(InvoiceStatus, 'SENT', 'SENT'): "#007bff",
            getattr(InvoiceStatus, 'PARTIALLY_PAID', 'PARTIALLY_PAID'): "orange",
            getattr(InvoiceStatus, 'PAID', 'PAID'): "green",
            getattr(InvoiceStatus, 'OVERDUE', 'OVERDUE'): "#dc3545", getattr(InvoiceStatus, 'VOID', 'VOID'): "black",
            getattr(InvoiceStatus, 'CANCELLED', 'CANCELLED'): "#6c757d"
        }
        return format_html(
            f'<strong style="color:{color_map.get(obj.status, "black")};">{obj.get_status_display()}</strong>')

    @admin.display(description=_('Created'), ordering='created_at')
    def created_at_short(self, obj: CustomerInvoice):
        return obj.created_at.strftime('%Y-%m-%d %H:%M') if obj.created_at else ''

    @admin.display(description=_('GL Voucher'))
    def related_gl_voucher_link(self, obj: Optional[CustomerInvoice]) -> str:
        if obj and obj.related_gl_voucher_id:
            try:
                gl_voucher = obj.related_gl_voucher if hasattr(obj,
                                                               'related_gl_voucher') and obj.related_gl_voucher else Voucher.objects.get(
                    pk=obj.related_gl_voucher_id)
                url = reverse("admin:crp_accounting_voucher_change", args=[gl_voucher.pk])
                return format_html('<a href="{}" target="_blank">{}</a>', url,
                                   gl_voucher.voucher_number or f"Vch#{gl_voucher.pk}")
            except (NoReverseMatch, Voucher.DoesNotExist):
                return f"Vch ID {obj.related_gl_voucher_id} (Link Error)"
        return "—"

    # Admin Actions (using helper methods)
    def _call_receivables_service_action_single(self, request: HttpRequest, queryset: models.QuerySet,
                                                service_method_name: str, item_id_param_name: str,
                                                success_msg_template: str,
                                                eligibility_func: Optional[Callable[[Any], bool]] = None,
                                                action_kwargs_func: Optional[Callable[[Any], Dict]] = None):
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method: messages.error(request, _("Action misconfigured.")); return
        processed, errors, skipped = 0, 0, 0
        for item in queryset:
            item_str = str(getattr(item, 'invoice_number', None) or getattr(item, 'reference_number', None) or item.pk)
            if eligibility_func and not eligibility_func(item): skipped += 1; continue
            try:
                if not item.company_id: messages.error(request, _("Item %(id)s missing company.") % {
                    'id': item_str}); errors += 1; continue
                kwargs = action_kwargs_func(item) if action_kwargs_func else {}
                service_method(company_id=item.company_id, user=request.user, **{item_id_param_name: item.pk}, **kwargs)
                processed += 1
            except (
            DjangoValidationError, ReceivablesServiceError, DjangoPermissionDenied, ObjectDoesNotExist) as e_serv:
                msg = getattr(e_serv, 'message_dict', None);
                msg = "; ".join([f"{k}: {v[0]}" for k, v in msg.items()]) if msg else str(e_serv)
                messages.error(request, f"{item._meta.verbose_name.capitalize()} '{item_str}': {msg}");
                errors += 1
            except Exception as e_unexp:
                messages.error(request,
                               _("Unexpected error on %(it)s '%(is)s': %(e)s") % {'it': item._meta.verbose_name,
                                                                                  'is': item_str, 'e': str(e_unexp)});
                errors += 1
        if processed > 0: messages.success(request, success_msg_template.format(count=processed))
        if errors > 0:
            messages.warning(request,
                             _("Action completed with %(ec)d error(s). Processed: %(pc)d. Skipped: %(sc)d.") % {
                                 'ec': errors, 'pc': processed, 'sc': skipped})
        elif skipped > 0 and processed == 0:
            messages.info(request, _("No items eligible."))

    def _call_receivables_service_batch_ids(self, request: HttpRequest, queryset: models.QuerySet,
                                            service_method_name: str, item_ids_param_name: str,
                                            success_msg_template: str):
        if not queryset.exists(): self.message_user(request, _("No items selected."), messages.INFO); return
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method: messages.error(request, _("Action misconfigured.")); return
        items_by_co: Dict[Any, List[Any]] = {}
        for pk, co_id in queryset.values_list('pk', 'company_id'):
            if not co_id: messages.error(request, _("Item PK %(pk)s missing company.") % {'pk': pk}); continue
            items_by_co.setdefault(co_id, []).append(pk)
        s_all, e_all = 0, 0
        for co_id, pks in items_by_co.items():
            if not pks: continue
            try:
                s_c, e_c, e_details = service_method(company_id=co_id, user=request.user, **{item_ids_param_name: pks})
                s_all += s_c;
                e_all += e_c
                for detail in e_details: messages.error(request, f"CoID {co_id}: {detail}")
            except Exception as e_batch:
                messages.error(request,
                               _("Critical batch error for CoID %(co)s: %(err)s") % {'co': co_id, 'err': str(e_batch)});
                e_all += len(pks)
        if s_all > 0: self.message_user(request, success_msg_template.format(count=s_all), messages.SUCCESS)
        if e_all > 0:
            self.message_user(request, _("Batch action with %(err)d error(s).") % {'err': e_all},
                              messages.WARNING if s_all > 0 else messages.ERROR)
        elif s_all == 0 and e_all == 0 and any(items_by_co.values()):
            messages.info(request, _("No items eligible or processed."))

    @admin.action(description=_('Mark selected invoices as SENT (if Draft)'))
    def admin_action_mark_invoices_sent(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_receivables_service_action_single(request, queryset, service_method_name='mark_invoice_as_sent',
                                                     item_id_param_name='invoice_id',
                                                     success_msg_template=_("{count} invoice(s) marked as SENT."),
                                                     eligibility_func=lambda inv: inv.status == getattr(InvoiceStatus,
                                                                                                        'DRAFT',
                                                                                                        'DRAFT'),
                                                     action_kwargs_func=lambda inv: {'post_to_gl': False})

    @admin.action(description=_('Post selected DRAFT/SENT invoices to General Ledger'))
    def admin_action_post_invoices_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_receivables_service_batch_ids(request, queryset, service_method_name='post_selected_invoices_to_gl',
                                                 item_ids_param_name='invoice_ids_list',
                                                 success_msg_template="{count} invoice(s) processed for GL posting.")

    @admin.action(description=_('VOID selected invoices'))
    def admin_action_void_invoices(self, request: HttpRequest, queryset: models.QuerySet):
        void_reason = _("Voided via admin by %(user)s on %(date)s.") % {'user': request.user.name or 'System',
                                                                        'date': timezone.now().strftime('%Y-%m-%d')}
        self._call_receivables_service_action_single(request, queryset, service_method_name='void_customer_invoice',
                                                     item_id_param_name='invoice_id',
                                                     success_msg_template=_("{count} invoice(s) VOIDED."),
                                                     eligibility_func=lambda inv: inv.status not in [
                                                         getattr(InvoiceStatus, 'VOID', 'VOID'),
                                                         getattr(InvoiceStatus, 'PAID', 'PAID')],
                                                     action_kwargs_func=lambda inv: {'void_reason': void_reason,
                                                                                     'void_date': timezone.now().date()})


# =============================================================================
# PaymentAllocation Inline Admin
# =============================================================================
class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    fields = ('invoice', 'amount_applied', 'allocation_date')
    readonly_fields = ()
    extra = 1
    autocomplete_fields = ['invoice']
    verbose_name = _("Invoice Allocation")
    verbose_name_plural = _("Invoice Allocations")
    classes = ['collapse'] if not settings.DEBUG else []

    def get_formset(self, request: Any, obj: Optional[CustomerPayment] = None, **kwargs: Any) -> Any:
        logger.debug("=" * 50)
        logger.debug(f"[PaymentAllocationInline.get_formset] Called. Request method: {request.method}")

        formset = super().get_formset(request, obj, **kwargs)

        company, customer, currency = None, None, None

        if obj and obj.pk:  # CHANGE VIEW - Easy case, use the existing object
            logger.debug("Context source: Existing object (change_view)")
            company = obj.company
            customer = obj.customer
            currency = obj.currency
        else:  # ADD VIEW - This is the tricky case
            logger.debug("Context source: POST or GET data (add_view)")
            customer_pk = request.POST.get('customer') if request.method == 'POST' else None
            currency_val = request.POST.get('currency') if request.method == 'POST' else None

            logger.debug(f"Attempting to find context. Customer PK from POST: {customer_pk}")

            # *** THE KEY FIX IS HERE ***
            # Instead of relying on POST['company'], we find the company via the submitted customer.
            if customer_pk:
                try:
                    # Find the customer that was submitted in the main form
                    customer = Party.objects.select_related('company').get(pk=customer_pk)
                    # Get the company from that customer object
                    company = customer.company
                    logger.debug(f"Found Customer '{customer.name}' and derived Company '{company.name}' from it.")
                except Party.DoesNotExist:
                    logger.error(f"CRITICAL: Submitted customer PK {customer_pk} does not exist!")
                    customer = None
                    company = None

            # Get currency from POST data
            if currency_val:
                currency = currency_val

        logger.debug(f"Final Derived Context -> Company: {company}, Customer: {customer}, Currency: {currency}")

        # The rest of the logic remains the same
        invoice_queryset = CustomerInvoice.objects.none()
        if company and customer and currency:
            logger.debug("SUCCESS: All context found. Building queryset.")
            sent_val = getattr(InvoiceStatus, 'SENT', 'SENT')
            partial_val = getattr(InvoiceStatus, 'PARTIALLY_PAID', 'PARTIALLY_PAID')
            overdue_val = getattr(InvoiceStatus, 'OVERDUE', 'OVERDUE')

            invoice_queryset = CustomerInvoice.objects.filter(
                company=company,
                customer=customer,
                currency=currency,
                status__in=[sent_val, partial_val, overdue_val]
            ).exclude(amount_due__lte=ZERO)
        else:
            logger.debug("FAILURE: One or more context variables are missing. Queryset will be empty.")

        formset.form.base_fields['invoice'].queryset = invoice_queryset
        formset.form.base_fields['invoice'].label = _('Invoice (Current Due)')

        logger.debug("get_formset finished.")
        logger.debug("=" * 50)

        return formset

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    # --- KEEP ALL YOUR PERMISSION METHODS (has_add_permission, etc.) EXACTLY AS THEY WERE ---
    def _is_parent_payment_editable(self, parent_payment: Optional[CustomerPayment]) -> bool:
        if parent_payment is None: return True
        if hasattr(self, '_parent_obj_for_perms'): parent_payment = self._parent_obj_for_perms
        if parent_payment: return parent_payment.status != getattr(PaymentStatus, 'VOID', 'VOID')
        return True

    def has_add_permission(self, request, obj=None):
        self._parent_obj_for_perms = obj
        return super().has_add_permission(request, obj) and self._is_parent_payment_editable(obj)

    def has_change_permission(self, request, obj=None):
        parent_ctx = obj.payment if isinstance(obj, PaymentAllocation) and obj.payment_id else obj
        self._parent_obj_for_perms = parent_ctx
        return super().has_change_permission(request, obj) and self._is_parent_payment_editable(parent_ctx)

    def has_delete_permission(self, request, obj=None):
        parent_ctx = obj.payment if isinstance(obj, PaymentAllocation) and obj.payment_id else obj
        self._parent_obj_for_perms = parent_ctx
        return super().has_delete_permission(request, obj) and self._is_parent_payment_editable(parent_ctx)
# =============================================================================
# CustomerPayment Admin
# =============================================================================
@admin.register(CustomerPayment)
class CustomerPaymentAdmin(TenantAccountingModelAdmin):
    list_display = ('id_link', 'customer_link', 'payment_date', 'amount_received_display', 'amount_applied_display',
                    'amount_unapplied_display', 'status_colored', 'bank_account_credited_short',
                    'related_gl_voucher_link')
    list_filter_non_superuser = (
    'status', ('customer', admin.RelatedOnlyFieldListFilter), ('payment_date', admin.DateFieldListFilter),
    'payment_method', 'currency')
    search_fields = ('reference_number', 'customer__name', 'notes', 'company__name', 'id')
    readonly_fields_base = (
    'amount_applied', 'amount_unapplied', 'related_gl_voucher_link', 'created_by', 'created_at', 'updated_by',
    'updated_at')
    autocomplete_fields = ['company', 'customer', 'bank_account_credited']
    inlines = [PaymentAllocationInline]
    actions = ['admin_action_post_payments_to_gl', 'admin_action_void_payments', 'action_soft_delete_selected',
               'action_undelete_selected']
    ordering = ('-payment_date', '-created_at')
    date_hierarchy = 'payment_date'
    list_select_related = ('company', 'customer', 'bank_account_credited', 'created_by', 'related_gl_voucher__company')

    add_fieldsets = (
        (None, {'fields': ('company', 'customer', 'payment_date', 'amount_received', 'currency', 'payment_method',
                           'bank_account_credited')}),
        (_('Details'), {'fields': ('reference_number', 'notes')}),
    )
    change_fieldsets = (
        (None, {'fields': (
        'company', 'customer', 'payment_date', 'amount_received', 'currency', 'status', 'payment_method',
        'bank_account_credited')}),
        (_('Details'), {'fields': ('reference_number', 'notes')}),
        (_('Financials (Calculated)'), {'fields': ('amount_applied', 'amount_unapplied')}),
        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'), 'classes': ('collapse',)})
    )

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.change_fieldsets

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj and obj.status == getattr(PaymentStatus, 'VOID', 'VOID'):
            ro.update(
                ['customer', 'payment_date', 'amount_received', 'currency', 'payment_method', 'bank_account_credited',
                 'reference_number', 'notes'])
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context = self._get_company_from_request_obj_or_form(
            request,
            obj=self.get_object(request,
                                request.resolver_match.kwargs.get('object_id')) if request.resolver_match.kwargs.get(
                'object_id') else None,
            form_data_for_add_view_post=request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
                'object_id') else None
        )
        if db_field.name == "customer":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(company=company_context,
                                                          party_type=getattr(CorePartyType, 'CUSTOMER', 'CUSTOMER'),
                                                          is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        elif db_field.name == "bank_account_credited":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=getattr(CoreAccountType, 'ASSET', 'ASSET'),
                                                            is_active=True, allow_direct_posting=True).order_by(
                    'account_name')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: CustomerPayment, form, change):
        try:
            super().save_model(request, obj, form, change)
            logger.info(f"[CPAdmin SaveM] Pmt header saved (PK: {obj.pk}). Status: {obj.status}")
        except DjangoValidationError as e_val:
            form._update_errors(e_val); return
        except Exception as e_save:
            messages.error(request, _("Failed to save payment: %(err)s") % {'err': str(e_save)}); return

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        payment_instance: CustomerPayment = form.instance
        if payment_instance and payment_instance.pk:
            logger.debug(f"[CPAdmin SaveFS] Recalc applied amounts for Pmt {payment_instance.pk}")
            payment_instance._recalculate_applied_amounts_and_status(save_instance=True)
            logger.debug(
                f"[CPAdmin SaveFS] Pmt {payment_instance.pk} amounts/status updated. Final status: {payment_instance.get_status_display()}")

    @admin.display(description=_('Payment ID'), ordering='id')
    def id_link(self, obj: CustomerPayment):
        try:
            link = reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_change", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', link, obj.pk)
        except NoReverseMatch:
            return str(obj.pk)

    @admin.display(description=_('Customer'), ordering='customer__name')
    def customer_link(self, obj: CustomerPayment):
        if not obj.customer_id: return "—"
        try:
            cust_name = obj.customer.name if hasattr(obj, 'customer') and obj.customer else Party.objects.values_list(
                'name', flat=True).get(pk=obj.customer_id)
            link = reverse("admin:crp_accounting_party_change", args=[obj.customer_id])
            return format_html('<a href="{}">{}</a>', link, cust_name)
        except (NoReverseMatch, Party.DoesNotExist):
            return str(obj.customer_id or "Error")

    @admin.display(description=_('Bank A/C'), ordering='bank_account_credited__account_name')
    def bank_account_credited_short(self, obj: CustomerPayment):
        if obj.bank_account_credited: name = obj.bank_account_credited.account_name; return (name[:20] + "...") if len(
            name) > 23 else name
        return "—"

    @admin.display(description=_('Amt Rcvd'), ordering='amount_received')
    def amount_received_display(self, obj: CustomerPayment):
        return f"{obj.amount_received or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Amt Applied'), ordering='amount_applied')
    def amount_applied_display(self, obj: CustomerPayment):
        return f"{obj.amount_applied or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Amt Unapplied'), ordering='amount_unapplied')
    def amount_unapplied_display(self, obj: CustomerPayment):
        return f"{obj.amount_unapplied or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: CustomerPayment):
        color_map = {
            getattr(PaymentStatus, 'UNAPPLIED', 'UNAPPLIED'): "orange",
            getattr(PaymentStatus, 'PARTIALLY_APPLIED', 'PARTIALLY_APPLIED'): "#007bff",
            getattr(PaymentStatus, 'APPLIED', 'APPLIED'): "green",
            getattr(PaymentStatus, 'VOID', 'VOID'): "black"
        }
        return format_html(
            f'<strong style="color:{color_map.get(obj.status, "grey")};">{obj.get_status_display()}</strong>')

    @admin.display(description=_('GL Voucher'))
    def related_gl_voucher_link(self, obj: Optional[CustomerPayment]) -> str:
        if obj and obj.related_gl_voucher_id:
            try:
                gl_voucher = obj.related_gl_voucher if hasattr(obj,
                                                               'related_gl_voucher') and obj.related_gl_voucher else Voucher.objects.get(
                    pk=obj.related_gl_voucher_id)
                url = reverse("admin:crp_accounting_voucher_change", args=[gl_voucher.pk])
                return format_html('<a href="{}" target="_blank">{}</a>', url,
                                   gl_voucher.voucher_number or f"Voucher #{gl_voucher.pk}")
            except (NoReverseMatch, Voucher.DoesNotExist):
                return f"Vch ID {obj.related_gl_voucher_id} (Link Error)"
        return "—"

    _call_service_action_single_item = CustomerInvoiceAdmin._call_receivables_service_action_single
    _call_service_action_batch_ids = CustomerInvoiceAdmin._call_receivables_service_batch_ids

    @admin.action(description=_('Post selected payments to General Ledger'))
    def admin_action_post_payments_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        if hasattr(receivables_service, 'post_selected_payments_to_gl'):
            self._call_service_action_batch_ids(request, queryset, service_method_name='post_selected_payments_to_gl',
                                                item_ids_param_name='payment_ids_list',
                                                success_msg_template=_("{count} payment(s) processed for GL posting."))
        else:
            messages.error(request, _("Batch GL posting for payments not implemented."))

    @admin.action(description=_('VOID selected payments'))
    def admin_action_void_payments(self, request: HttpRequest, queryset: models.QuerySet):
        if hasattr(receivables_service, 'void_customer_payment'):
            void_reason = _("Voided via admin by %(user)s on %(date)s.") % {'user': request.user.name or 'System',
                                                                            'date': timezone.now().strftime('%Y-%m-%d')}
            self._call_service_action_single_item(request, queryset, service_method_name='void_customer_payment',
                                                  item_id_param_name='payment_id',
                                                  success_msg_template=_("{count} payment(s) VOIDED."),
                                                  eligibility_func=lambda p: p.status != getattr(PaymentStatus, 'VOID',
                                                                                                 'VOID'),
                                                  action_kwargs_func=lambda p: {'void_reason': void_reason,
                                                                                'void_date': timezone.now().date()})
        else:
            messages.error(request, _("VOID Payment service not implemented."))
