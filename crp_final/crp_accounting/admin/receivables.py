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
    CustomerInvoice, InvoiceLine, CustomerPayment, PaymentAllocation,
    InvoiceStatus, PaymentStatus  # Enums from receivables.py
)
from ..models.coa import Account
from ..models.party import Party
from ..models.journal import Voucher, VoucherType  # VoucherType for admin actions

from company.models import Company

# --- Enum Imports ---
from crp_core.enums import AccountType as CoreAccountType, PartyType as CorePartyType

# --- Service Imports ---
from ..services import receivables_service
from ..services.receivables_service import (  # Specific exceptions for better handling
    ReceivablesServiceError, InvoiceProcessingError, PaymentProcessingError,
    GLPostingError, SequenceGenerationError
)

logger = logging.getLogger("crp_accounting.admin.receivables")  # Consistent logger name
ZERO = Decimal('0.00')


# =============================================================================
# InvoiceLine Inline Admin
# =============================================================================
class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    fields = ('description', 'quantity', 'unit_price', 'revenue_account', 'tax_amount_on_line', 'line_total_display')
    readonly_fields = ('line_total_display',)
    extra = 0  # Prefer users to click "Add another"
    autocomplete_fields = ['revenue_account']
    verbose_name = _("Invoice Line Item")
    verbose_name_plural = _("Invoice Line Items")

    classes = ['collapse'] if not settings.DEBUG else []  # Keep collapsed if form is long, unless DEBUG

    @admin.display(description=_('Line Total'))
    def line_total_display(self, obj: InvoiceLine) -> str:
        """Displays the calculated line total with the invoice's currency."""
        currency_symbol = ""
        # Access parent invoice safely
        parent_invoice = getattr(obj, 'invoice', None)  # obj is InvoiceLine instance
        if parent_invoice and parent_invoice.currency:
            currency_symbol = parent_invoice.currency
        return f"{obj.line_total or ZERO:.2f} {currency_symbol}"

    def _get_parent_invoice_context(self, request: HttpRequest) -> Optional[CustomerInvoice]:
        """
        Retrieves the parent CustomerInvoice instance being edited/added.
        This is stored on the request by get_formset.
        """
        return getattr(request, '_current_parent_invoice_for_line_inline', None)

    def _get_company_for_inline_filtering(self, request: HttpRequest, parent_invoice: Optional[CustomerInvoice]) -> \
            Optional[Company]:
        """
        Determines the company context for filtering choices within this inline.
        Prioritizes parent invoice's company, then company from main form POST (for new invoices),
        then request.company.
        """
        log_prefix = f"[IL_Inline_GetCo][User:{request.user.name if request.user else 'Anonymous'}]"

        if parent_invoice and parent_invoice.pk and parent_invoice.company_id:
            if hasattr(parent_invoice, 'company') and parent_invoice.company:
                logger.debug(f"{log_prefix} Using parent_invoice.company: {parent_invoice.company.name}")
                return parent_invoice.company
            try:
                company_inst = Company.objects.get(pk=parent_invoice.company_id)
                logger.debug(f"{log_prefix} Fetched company from parent_invoice.company_id: {company_inst.name}")
                return company_inst
            except Company.DoesNotExist:
                logger.error(
                    f"{log_prefix} Parent Invoice {parent_invoice.pk} has invalid company_id {parent_invoice.company_id}.")
                return None

        is_parent_add_view = not (parent_invoice and parent_invoice.pk)
        if is_parent_add_view and request.method == 'POST':
            company_pk_from_main_form = request.POST.get('company')
            if company_pk_from_main_form:
                try:
                    company_inst = Company.objects.get(pk=company_pk_from_main_form)
                    logger.debug(f"{log_prefix} Using company from main form POST: {company_inst.name}")
                    return company_inst
                except (Company.DoesNotExist, ValueError, TypeError):
                    logger.warning(
                        f"{log_prefix} Invalid company PK '{company_pk_from_main_form}' from main form POST.")

        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company):
            logger.debug(f"{log_prefix} Using request.company: {request_company.name}")
            return request_company

        logger.warning(f"{log_prefix} Could not determine company context for inline filtering.")
        return None

    def get_formset(self, request: Any, obj: Optional[CustomerInvoice] = None, **kwargs: Any) -> Any:
        """Stores the parent CustomerInvoice object on the request for use in formfield_for_foreignkey."""
        request._current_parent_invoice_for_line_inline = obj
        logger.debug(
            f"[IL_Inline GetFormset][User:{request.user.name if request.user else 'Anonymous'}] Stored parent invoice (PK: {obj.pk if obj else 'None'}) on request.")
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_invoice = self._get_parent_invoice_context(request)
        company_for_filtering = self._get_company_for_inline_filtering(request, parent_invoice)
        log_prefix = f"[IL_Inline FFKey][User:{request.user.name if request.user else 'Anonymous'}][Fld:'{db_field.name}']"

        if db_field.name == "revenue_account":
            if company_for_filtering:
                kwargs["queryset"] = Account.objects.filter(
                    company=company_for_filtering,
                    account_type=CoreAccountType.INCOME.value,
                    is_active=True,
                    allow_direct_posting=True
                ).select_related('account_group').order_by('account_group__name', 'account_number')
                logger.debug(
                    f"{log_prefix} Filtered Revenue Account choices for Co '{company_for_filtering.name}'. Count: {kwargs['queryset'].count()}")
            else:
                kwargs["queryset"] = Account.objects.none()
                logger.warning(f"{log_prefix} No company context, Revenue Account queryset is None.")
                # Condition based on second code block's logic for showing message more accurately
                is_add_view_of_parent = not (parent_invoice and parent_invoice.pk)
                if is_add_view_of_parent and request.user.is_superuser and \
                        request.method == 'GET' and not company_for_filtering and \
                        not request.POST.get('company'):  # Ensure it's GET and company not in POST
                    messages.info(request,
                                  _("Select 'Company' on the main Invoice form to populate Revenue Accounts for lines."))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_invoice_editable(self, parent_invoice: Optional[CustomerInvoice]) -> bool:
        """Determines if the parent invoice allows its lines to be edited."""
        if parent_invoice is None: return True
        return parent_invoice.status == InvoiceStatus.DRAFT.value

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
        'amount_due_display',
        'status_colored', 'related_gl_voucher_link', 'created_at_short')
    list_filter_non_superuser = (
        'status', ('customer', admin.RelatedOnlyFieldListFilter), ('invoice_date', admin.DateFieldListFilter),
        ('due_date', admin.DateFieldListFilter), 'currency')
    search_fields = ('invoice_number', 'customer__name', 'company__name')
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

    add_fieldsets = ((None, {'fields': ('company', 'customer', 'invoice_date', 'due_date', 'currency')}),
                     (_('Details'), {'fields': ('invoice_number', 'terms', 'notes_to_customer', 'internal_notes')}))
    change_fieldsets_draft = (
        (None, {'fields': ('company', 'customer', 'invoice_number', 'invoice_date', 'due_date', 'currency', 'status')}),
        (_('Details'), {'fields': ('terms', 'notes_to_customer', 'internal_notes')}), (_('Financials (Calculated)'), {
            'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_non_draft = (
        (None, {'fields': ('company', 'customer', 'invoice_number', 'invoice_date', 'due_date', 'currency', 'status')}),
        (
        _('Details Read-Only'), {'fields': ('terms', 'notes_to_customer', 'internal_notes'), 'classes': ('collapse',)}),
        (
            _('Financials (Calculated)'),
            {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'), 'classes': ('collapse',)}))

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else (
            self.change_fieldsets_draft if obj.status == InvoiceStatus.DRAFT.value else self.change_fieldsets_non_draft)

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj:
            if obj.status != InvoiceStatus.DRAFT.value:
                ro.update(['customer', 'invoice_date', 'due_date', 'currency', 'terms', 'notes_to_customer',
                           'internal_notes'])
            if obj.invoice_number and obj.invoice_number.strip():  # Non-editable if number is set, regardless of DRAFT
                ro.add('invoice_number')
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context_for_main_form = self._get_company_from_request_obj_or_form(
            request,
            obj=self.get_object(request,
                                request.resolver_match.kwargs.get('object_id')) if request.resolver_match.kwargs.get(
                'object_id') else None,
            form_data_for_add_view_post=request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
                'object_id') else None
        )
        if db_field.name == "customer":
            if company_context_for_main_form:
                kwargs["queryset"] = Party.objects.filter(company=company_context_for_main_form,
                                                          party_type=CorePartyType.CUSTOMER.value,
                                                          is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: CustomerInvoice, form, change):
        log_prefix = f"[CIAdmin SaveModel][User:{request.user.name if request.user else 'Anonymous'}][Inv:{obj.pk or 'New'}]"
        is_new = not obj.pk
        if is_new: obj.created_by = request.user
        obj.updated_by = request.user

        is_finalizing_from_draft = change and form.initial.get(
            'status') == InvoiceStatus.DRAFT.value and obj.status != InvoiceStatus.DRAFT.value
        is_creating_as_non_draft = is_new and obj.status != InvoiceStatus.DRAFT.value

        if (is_finalizing_from_draft or is_creating_as_non_draft) and (
                not obj.invoice_number or not obj.invoice_number.strip()):
            if obj.company:
                try:
                    obj.invoice_number = receivables_service.generate_next_invoice_number_from_sequence(obj.company,
                                                                                                        obj.invoice_date)
                    logger.info(f"{log_prefix} Generated invoice number '{obj.invoice_number}'.")
                except SequenceGenerationError as e_seq:
                    logger.error(f"{log_prefix} Failed to generate invoice number: {e_seq}")
                    form.add_error(None, DjangoValidationError(
                        _("Failed to generate invoice number: %(err)s. Please check sequence setup or try again.") % {
                            'err': str(e_seq)},
                        code='inv_num_gen_fail'))
                    return
                except Exception as e_num_gen:
                    logger.exception(f"{log_prefix} Unexpected error generating invoice number.")
                    form.add_error(None, DjangoValidationError(
                        _("Unexpected error generating invoice number: %(err)s") % {'err': str(e_num_gen)},
                        code='inv_num_gen_unexpected_fail'))
                    return
            else:
                logger.error(f"{log_prefix} Cannot generate invoice number: Company not set on invoice instance.")
                form.add_error('company', _("Company must be set to finalize invoice and generate number."))
                return
        try:
            super().save_model(request, obj, form, change)
            logger.info(f"{log_prefix} Invoice header saved (PK: {obj.pk}). Status: {obj.status}")
        except DjangoValidationError as e:
            form._update_errors(e);
            return
        except Exception as e:
            messages.error(request, _("Failed to save invoice: %(err)s") % {'err': str(e)})
            logger.exception(f"{log_prefix} Error saving invoice header.")
            return

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        invoice_instance: CustomerInvoice = form.instance
        if invoice_instance and invoice_instance.pk:
            logger.debug(
                f"[CIAdmin SaveFormset] Recalculating totals for Invoice {invoice_instance.invoice_number or invoice_instance.pk} after line changes.")
            invoice_instance._recalculate_totals_and_due(
                perform_save=True)  # perform_save will also update status and save
            logger.debug(
                f"[CIAdmin SaveFormset] Invoice {invoice_instance.invoice_number} totals/status updated. Final status: {invoice_instance.get_status_display()}")

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
            return str(obj.customer_id or "Error linking customer")

    @admin.display(description=_('Total Amt'), ordering='total_amount')
    def total_amount_display(self, obj: CustomerInvoice):
        return f"{obj.total_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Amt Due'), ordering='amount_due')
    def amount_due_display(self, obj: CustomerInvoice):
        return f"{obj.amount_due or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: CustomerInvoice):
        color_map = {InvoiceStatus.DRAFT.value: "grey", InvoiceStatus.SENT.value: "#007bff",
                     InvoiceStatus.PARTIALLY_PAID.value: "orange", InvoiceStatus.PAID.value: "green",
                     InvoiceStatus.OVERDUE.value: "#dc3545", InvoiceStatus.VOID.value: "black",
                     InvoiceStatus.CANCELLED.value: "#6c757d"}
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

    def _call_receivables_service_action_single(self, request: HttpRequest, queryset: models.QuerySet,
                                                service_method_name: str, item_id_param_name: str,
                                                success_msg_template: str,
                                                eligibility_func: Optional[Callable[[Any], bool]] = None,
                                                action_kwargs_func: Optional[Callable[[Any], Dict]] = None):
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method:
            logger.error(f"Admin Action: Service method '{service_method_name}' not found.")
            messages.error(request, _("Action misconfigured."))
            return

        processed_count, error_count, skipped_count = 0, 0, 0
        for item in queryset:
            item_str = str(getattr(item, 'invoice_number', None) or getattr(item, 'reference_number', None) or item.pk)
            if eligibility_func and not eligibility_func(item):
                skipped_count += 1
                logger.debug(f"Skipped item {item_str} for action '{service_method_name}' due to eligibility.")
                continue

            try:
                if not item.company_id:
                    messages.error(request, _("Item %(id)s (%(type)s) has no company.") % {'id': item_str,
                                                                                           'type': item._meta.verbose_name})
                    error_count += 1
                    continue

                current_action_kwargs = action_kwargs_func(item) if action_kwargs_func else {}
                method_params = {'company_id': item.company_id, 'user': request.user, item_id_param_name: item.pk,
                                 **current_action_kwargs}

                logger.info(
                    f"Admin Action '{service_method_name}' called for {item._meta.verbose_name} PK {item.pk} with params: {method_params}")
                service_method(**method_params)
                processed_count += 1
            except (
            DjangoValidationError, ReceivablesServiceError, DjangoPermissionDenied, ObjectDoesNotExist) as e_serv:
                msg = getattr(e_serv, 'message_dict', None)
                msg = "; ".join([f"{k}: {v[0]}" for k, v in msg.items()]) if msg else str(e_serv)
                messages.error(request, f"{item._meta.verbose_name.capitalize()} '{item_str}': {msg}")
                error_count += 1
                logger.warning(f"Admin Action '{service_method_name}' for {item.pk} failed with known error: {msg}",
                               exc_info=settings.DEBUG)
            except Exception as e_unexp:
                logger.exception(f"Admin Action '{service_method_name}' unexpected error for {item.pk}")
                messages.error(request,
                               _("Unexpected error on %(it)s '%(is)s': %(e)s") % {'it': item._meta.verbose_name,
                                                                                  'is': item_str, 'e': str(e_unexp)})
                error_count += 1

        if processed_count > 0: messages.success(request, success_msg_template.format(count=processed_count))
        if error_count > 0:
            messages.warning(request,
                             _("Action completed with %(ec)d error(s). Processed: %(pc)d. Skipped: %(sc)d.") % {
                                 'ec': error_count, 'pc': processed_count, 'sc': skipped_count})
        elif skipped_count > 0 and processed_count == 0:
            messages.info(request, _("No items were eligible for this action."))
        elif not queryset.exists():
            messages.info(request, _("No items selected."))

    def _call_receivables_service_batch_ids(self, request: HttpRequest, queryset: models.QuerySet,
                                            service_method_name: str, item_ids_param_name: str,
                                            success_msg_template: str):
        if not queryset.exists():
            self.message_user(request, _("No items selected."), messages.INFO)
            return
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method:
            logger.error(f"Admin Batch: Service '{service_method_name}' missing.")
            messages.error(request, _("Action misconfigured."))
            return

        items_by_company: Dict[Any, List[Any]] = {}
        for item_pk, company_id_val in queryset.values_list('pk', 'company_id'):
            if not company_id_val:
                messages.error(request, _("Item PK %(pk)s has no company.") % {'pk': item_pk})
                continue
            items_by_company.setdefault(company_id_val, []).append(item_pk)

        overall_success_count, overall_error_count = 0, 0
        for co_id, pks_list in items_by_company.items():
            if not pks_list: continue
            try:
                logger.info(f"Admin Batch Action '{service_method_name}' for CoID {co_id} with PKs: {pks_list}")
                s_c, e_c, e_details = service_method(company_id=co_id, user=request.user,
                                                     **{item_ids_param_name: pks_list})
                overall_success_count += s_c
                overall_error_count += e_c
                for detail_msg in e_details: messages.error(request, f"CoID {co_id}: {detail_msg}")
            except Exception as e_batch:
                logger.exception(f"Admin Batch Action {service_method_name} CRITICAL error for Co {co_id}.")
                messages.error(request,
                               _("Critical batch error for CoID %(co)s: %(err)s") % {'co': co_id, 'err': str(e_batch)})
                overall_error_count += len(pks_list)

        if overall_success_count > 0: self.message_user(request,
                                                        success_msg_template.format(count=overall_success_count),
                                                        messages.SUCCESS)
        if overall_error_count > 0:
            self.message_user(request, _("Batch action completed with %(err_count)d error(s).") % {
                'err_count': overall_error_count},
                              messages.WARNING if overall_success_count > 0 else messages.ERROR)
        elif overall_success_count == 0 and overall_error_count == 0 and any(
                items_by_company.values()):  # Check if there were items to process
            messages.info(request, _("No items were eligible or processed by the batch action."))

    @admin.action(description=_('Mark selected invoices as SENT (if Draft)'))
    def admin_action_mark_invoices_sent(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_receivables_service_action_single(request, queryset,
                                                     service_method_name='mark_invoice_as_sent',
                                                     item_id_param_name='invoice_id',
                                                     success_msg_template=_("{count} invoice(s) marked as SENT."),
                                                     eligibility_func=lambda
                                                         inv: inv.status == InvoiceStatus.DRAFT.value,
                                                     action_kwargs_func=lambda inv: {'post_to_gl': False})

    @admin.action(description=_('Post selected DRAFT/SENT invoices to General Ledger'))
    def admin_action_post_invoices_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_receivables_service_batch_ids(request, queryset,
                                                 service_method_name='post_selected_invoices_to_gl',
                                                 item_ids_param_name='invoice_ids_list',
                                                 success_msg_template="{count} invoice(s) processed for GL posting.")

    @admin.action(description=_('VOID selected invoices'))
    def admin_action_void_invoices(self, request: HttpRequest, queryset: models.QuerySet):
        void_reason_default = _("Voided via admin bulk action by %(user)s on %(date)s.") % {
            'user': request.user.name if request.user else 'System', 'date': timezone.now().strftime('%Y-%m-%d')}
        self._call_receivables_service_action_single(request, queryset,
                                                     service_method_name='void_customer_invoice',
                                                     item_id_param_name='invoice_id',
                                                     success_msg_template=_("{count} invoice(s) VOIDED."),
                                                     eligibility_func=lambda inv: inv.status not in [
                                                         InvoiceStatus.VOID.value, InvoiceStatus.PAID.value],
                                                     action_kwargs_func=lambda inv: {'void_reason': void_reason_default,
                                                                                     'void_date': timezone.now().date()})


# =============================================================================
# PaymentAllocation Inline Admin
# =============================================================================
class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    fields = ('invoice_link_for_alloc', 'amount_applied', 'allocation_date')
    readonly_fields = ('invoice_link_for_alloc',)
    extra = 0
    autocomplete_fields = ['invoice']
    verbose_name = _("Invoice Allocation")
    verbose_name_plural = _("Invoice Allocations")

    @admin.display(description=_('Invoice (Current Due)'))
    def invoice_link_for_alloc(self, obj: PaymentAllocation) -> str:
        if obj.invoice_id:
            try:
                inv = obj.invoice if hasattr(obj,
                                             'invoice') and obj.invoice else CustomerInvoice.objects.select_related(
                    'company').get(pk=obj.invoice_id)
                link = reverse("admin:crp_accounting_customerinvoice_change", args=[inv.pk])
                return format_html('<a href="{url}" target="_blank">{num}</a> (Due: {due:.2f} {curr})', url=link,
                                   num=inv.invoice_number or f"Inv#{inv.pk}", due=inv.amount_due, curr=inv.currency)
            except (NoReverseMatch, CustomerInvoice.DoesNotExist):
                return f"Invoice ID: {obj.invoice_id} (Error)"
        return "—"  # Handles new unsaved lines or lines where invoice is not set

    def _get_parent_payment_context(self, request: HttpRequest) -> Optional[CustomerPayment]:
        return getattr(request, '_current_parent_payment_for_alloc_inline', None)

    def _get_company_customer_for_alloc_filter(self, request: HttpRequest, parent_payment: Optional[CustomerPayment]) -> \
            Tuple[Optional[Company], Optional[Party]]:
        log_prefix = f"[PA_Inline_GetCoCust][User:{request.user.name if request.user else 'Anonymous'}]"
        company_instance: Optional[Company] = None
        customer_instance: Optional[Party] = None

        if parent_payment and parent_payment.pk:  # Existing parent payment
            if hasattr(parent_payment, 'company') and parent_payment.company:
                company_instance = parent_payment.company
            elif parent_payment.company_id:
                try:
                    company_instance = Company.objects.get(pk=parent_payment.company_id)
                except Company.DoesNotExist:
                    logger.error(
                        f"{log_prefix} Parent Payment {parent_payment.pk} has invalid company_id {parent_payment.company_id}.")

            if hasattr(parent_payment, 'customer') and parent_payment.customer:
                customer_instance = parent_payment.customer
            elif parent_payment.customer_id:
                try:
                    customer_instance = Party.objects.get(pk=parent_payment.customer_id)
                except Party.DoesNotExist:
                    logger.error(
                        f"{log_prefix} Parent Payment {parent_payment.pk} has invalid customer_id {parent_payment.customer_id}.")

            if company_instance and customer_instance:
                logger.debug(
                    f"{log_prefix} From parent Pmt {parent_payment.pk}: Co='{company_instance.name}', Cust='{customer_instance.name}'")
                return company_instance, customer_instance
            else:
                logger.warning(
                    f"{log_prefix} Parent Pmt {parent_payment.pk} exists but failed to fully resolve company or customer.")
                return None, None

        is_add_view_of_parent = not (parent_payment and parent_payment.pk)
        if is_add_view_of_parent and request.method == 'POST':
            company_pk_from_post = request.POST.get('company')
            customer_pk_from_post = request.POST.get('customer')
            if company_pk_from_post and customer_pk_from_post:
                try:
                    company_instance = Company.objects.get(pk=company_pk_from_post)
                    customer_instance = Party.objects.get(pk=customer_pk_from_post, company=company_instance,
                                                          party_type=CorePartyType.CUSTOMER.value)
                    logger.debug(
                        f"{log_prefix} From main form POST: Co='{company_instance.name}', Cust='{customer_instance.name}'")
                    return company_instance, customer_instance
                except (ObjectDoesNotExist, ValueError, TypeError):
                    logger.warning(
                        f"{log_prefix} Invalid company/customer PK ('{company_pk_from_post}', '{customer_pk_from_post}') from main form POST.")
                    return None, None

        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company):
            logger.debug(
                f"{log_prefix} Fallback to request.company: {request_company.name}. Customer not determined yet for new payment on GET.")
            return request_company, None

        logger.warning(f"{log_prefix} Could not determine company/customer context for alloc filtering.")
        return None, None

    def get_formset(self, request: Any, obj: Optional[CustomerPayment] = None, **kwargs: Any) -> Any:
        request._current_parent_payment_for_alloc_inline = obj
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_payment = self._get_parent_payment_context(request)
        parent_company, parent_customer = self._get_company_customer_for_alloc_filter(request, parent_payment)

        if db_field.name == "invoice":
            if parent_company and parent_customer and parent_payment and parent_payment.currency:
                kwargs["queryset"] = CustomerInvoice.objects.filter(
                    company=parent_company, customer=parent_customer, currency=parent_payment.currency,
                    status__in=[InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value,
                                InvoiceStatus.OVERDUE.value]
                ).exclude(amount_due__lte=ZERO).select_related('company', 'customer').order_by('due_date',
                                                                                               'invoice_date')
            else:
                kwargs["queryset"] = CustomerInvoice.objects.none()
                is_add_view_of_parent = not (parent_payment and parent_payment.pk)
                # More precise condition for showing message, from second code block's logic
                if is_add_view_of_parent and request.user.is_superuser and not \
                        (request.method == 'POST' and request.POST.get('company') and \
                         request.POST.get('customer') and request.POST.get('currency')):
                    messages.info(request,
                                  _("Select Company, Customer & Currency on Payment form to populate Invoices for allocation."))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_payment_editable(self, parent_payment: Optional[CustomerPayment]) -> bool:
        if parent_payment is None: return True
        return parent_payment.status != PaymentStatus.VOID.value

    def has_add_permission(self, request, obj=None):
        parent = self._get_parent_payment_context(request) or obj
        return super().has_add_permission(request, parent) and self._is_parent_payment_editable(parent)

    def has_change_permission(self, request, obj=None):
        parent_ctx = obj.payment if isinstance(obj,
                                               PaymentAllocation) and obj.payment_id else self._get_parent_payment_context(
            request)
        if parent_ctx is None and isinstance(obj, CustomerPayment): parent_ctx = obj
        return super().has_change_permission(request, obj) and self._is_parent_payment_editable(parent_ctx)

    def has_delete_permission(self, request, obj=None):
        parent_ctx = obj.payment if isinstance(obj,
                                               PaymentAllocation) and obj.payment_id else self._get_parent_payment_context(
            request)
        if parent_ctx is None and isinstance(obj, CustomerPayment): parent_ctx = obj
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

    add_fieldsets = ((None, {'fields': (
    'company', 'customer', 'payment_date', 'amount_received', 'currency', 'payment_method', 'bank_account_credited')}),
                     (_('Details'), {'fields': ('reference_number', 'notes')}),)
    change_fieldsets = ((None, {'fields': (
    'company', 'customer', 'payment_date', 'amount_received', 'currency', 'status', 'payment_method',
    'bank_account_credited')}),
                        (_('Details'), {'fields': ('reference_number', 'notes')}),
                        (_('Financials (Calculated)'), {'fields': ('amount_applied', 'amount_unapplied')}),
                        (_('GL Link'), {'fields': ('related_gl_voucher_link',)}),
                        (_('Audit'), {'fields': ('created_by', 'created_at', 'updated_by', 'updated_at'),
                                      'classes': ('collapse',)}))

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.change_fieldsets

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj and obj.status == PaymentStatus.VOID.value:
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
                                                          party_type=CorePartyType.CUSTOMER.value,
                                                          is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        elif db_field.name == "bank_account_credited":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=CoreAccountType.ASSET.value, is_active=True,
                                                            allow_direct_posting=True).order_by('account_name')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: CustomerPayment, form, change):
        is_new = not obj.pk
        if is_new: obj.created_by = request.user
        # obj.updated_by is handled by TenantAccountingModelAdmin's save_model
        try:
            super().save_model(request, obj, form, change)
            logger.info(
                f"[CPAdmin SaveModel][User:{request.user.name if request.user else 'Anonymous'}] Payment header saved (PK: {obj.pk}). Status: {obj.status}")
        except DjangoValidationError as e:
            form._update_errors(e);
            return
        except Exception as e:
            messages.error(request, _("Failed to save payment: %(err)s") % {'err': str(e)})
            logger.exception(f"Error saving CPAdmin for Pmt {obj.pk or 'New'}")
            return

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        payment_instance: CustomerPayment = form.instance
        if payment_instance and payment_instance.pk:
            logger.debug(
                f"[CPAdmin SaveFormset] Recalculating applied amounts for Payment {payment_instance.pk} after allocation changes.")
            payment_instance._recalculate_applied_amounts_and_status(save_instance=True)
            logger.debug(
                f"[CPAdmin SaveFormset] Payment {payment_instance.pk} amounts/status updated. Final status: {payment_instance.get_status_display()}")

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
            cust_name = obj.customer.name if hasattr(obj,
                                                     '_customer_cache') and obj.customer._customer_cache else Party.objects.values_list(
                'name', flat=True).get(pk=obj.customer_id)
            link = reverse("admin:crp_accounting_party_change", args=[obj.customer_id])
            return format_html('<a href="{}">{}</a>', link, cust_name)
        except (NoReverseMatch, Party.DoesNotExist):
            return str(obj.customer_id or "Error")

    @admin.display(description=_('Bank A/C'), ordering='bank_account_credited__account_name')
    def bank_account_credited_short(self, obj: CustomerPayment):
        if obj.bank_account_credited:
            name = obj.bank_account_credited.account_name
            return (name[:20] + "...") if len(name) > 20 else name
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
        color_map = {PaymentStatus.UNAPPLIED.value: "orange", PaymentStatus.PARTIALLY_APPLIED.value: "#007bff",
                     PaymentStatus.FULLY_APPLIED.value: "green", PaymentStatus.VOID.value: "black", }
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

    # --- Admin Action Helper Methods (Copied and adapted from CustomerInvoiceAdmin for consistency) ---
    def _call_service_action_single_item(self, request: HttpRequest, queryset: models.QuerySet,
                                         service_method_name: str, item_id_param_name: str, success_msg_template: str,
                                         eligibility_func: Optional[Callable[[Any], bool]] = None,
                                         action_kwargs_func: Optional[Callable[[Any], Dict]] = None):
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method:
            logger.error(f"Admin Action (Payment): Service method '{service_method_name}' not found.")
            messages.error(request, _("Action misconfigured."))
            return

        processed_count, error_count, skipped_count = 0, 0, 0
        for item in queryset:
            item_str = str(getattr(item, 'reference_number', None) or item.pk)  # Use reference_number for payments
            if eligibility_func and not eligibility_func(item):
                skipped_count += 1
                logger.debug(f"Skipped item {item_str} for payment action '{service_method_name}' due to eligibility.")
                continue

            try:
                if not item.company_id:
                    messages.error(request, _("Payment %(id)s has no company.") % {'id': item_str})
                    error_count += 1;
                    continue
                current_action_kwargs = action_kwargs_func(item) if action_kwargs_func else {}
                method_params = {'company_id': item.company_id, 'user': request.user, item_id_param_name: item.pk,
                                 **current_action_kwargs}

                logger.info(
                    f"Admin Payment Action '{service_method_name}' called for {item._meta.verbose_name} PK {item.pk} with params: {method_params}")
                service_method(**method_params)
                processed_count += 1
            except (
            DjangoValidationError, ReceivablesServiceError, DjangoPermissionDenied, ObjectDoesNotExist) as e_serv:
                msg = getattr(e_serv, 'message_dict', None);
                msg = "; ".join([f"{k}: {v[0]}" for k, v in msg.items()]) if msg else str(e_serv)
                messages.error(request, f"{item._meta.verbose_name.capitalize()} '{item_str}': {msg}")
                error_count += 1;
                logger.warning(f"Admin Payment Action '{service_method_name}' for {item.pk} failed: {msg}",
                               exc_info=settings.DEBUG)
            except Exception as e_unexp:
                logger.exception(f"Admin Payment Action '{service_method_name}' unexpected error for {item.pk}")
                messages.error(request,
                               _("Unexpected error on payment '%(is)s': %(e)s") % {'is': item_str, 'e': str(e_unexp)})
                error_count += 1

        if processed_count > 0: messages.success(request, success_msg_template.format(count=processed_count))
        if error_count > 0:
            messages.warning(request,
                             _("Payment action completed with %(ec)d error(s). Processed: %(pc)d. Skipped: %(sc)d.") % {
                                 'ec': error_count, 'pc': processed_count, 'sc': skipped_count})
        elif skipped_count > 0 and processed_count == 0:
            messages.info(request, _("No payments were eligible for this action."))
        elif not queryset.exists():
            messages.info(request, _("No payments selected."))

    def _call_service_action_batch_ids(self, request: HttpRequest, queryset: models.QuerySet,
                                       service_method_name: str, item_ids_param_name: str,
                                       success_msg_template: str):
        if not queryset.exists():
            self.message_user(request, _("No payments selected."), messages.INFO);
            return
        service_method = getattr(receivables_service, service_method_name, None)
        if not service_method:
            logger.error(f"Admin Batch Payment Action: Service '{service_method_name}' missing.");
            messages.error(request, _("Action misconfigured."));
            return

        items_by_company: Dict[Any, List[Any]] = {}
        for item_pk, company_id_val in queryset.values_list('pk', 'company_id'):
            if not company_id_val: messages.error(request,
                                                  _("Payment PK %(pk)s has no company.") % {'pk': item_pk}); continue
            items_by_company.setdefault(company_id_val, []).append(item_pk)

        overall_s, overall_e = 0, 0
        for co_id, pks_list in items_by_company.items():
            if not pks_list: continue
            try:
                logger.info(f"Admin Batch Payment Action '{service_method_name}' for CoID {co_id} with PKs: {pks_list}")
                s_c, e_c, e_dets = service_method(company_id=co_id, user=request.user,
                                                  **{item_ids_param_name: pks_list})
                overall_s += s_c;
                overall_e += e_c
                for detail in e_dets: messages.error(request, f"CoID {co_id}: {detail}")
            except Exception as e_batch:
                logger.exception(f"Admin Batch Payment Action {service_method_name} CRITICAL error for Co {co_id}.")
                messages.error(request, _("Critical batch payment error for CoID %(co)s: %(err)s") % {'co': co_id,
                                                                                                      'err': str(
                                                                                                          e_batch)})
                overall_e += len(pks_list)

        if overall_s > 0: self.message_user(request, success_msg_template.format(count=overall_s), messages.SUCCESS)
        if overall_e > 0:
            self.message_user(request, _("Batch payment action completed with %(err_count)d error(s).") % {
                'err_count': overall_e}, messages.WARNING if overall_s > 0 else messages.ERROR)
        elif overall_s == 0 and overall_e == 0 and any(items_by_company.values()):
            messages.info(request, _("No payments were eligible or processed by the batch action."))

    @admin.action(description=_('Post selected payments to General Ledger'))
    def admin_action_post_payments_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        if hasattr(receivables_service, 'post_selected_payments_to_gl'):
            self._call_service_action_batch_ids(request, queryset,
                                                service_method_name='post_selected_payments_to_gl',
                                                item_ids_param_name='payment_ids_list',
                                                success_msg_template=_(
                                                    "{count} payment(s) successfully processed for GL posting."))
        else:
            messages.error(request, _("Batch GL posting service for payments is not yet implemented."))
            logger.error("Admin Action: Attempted to call non-existent service 'post_selected_payments_to_gl'.")

    @admin.action(description=_('VOID selected payments'))
    def admin_action_void_payments(self, request: HttpRequest, queryset: models.QuerySet):
        if hasattr(receivables_service, 'void_customer_payment'):
            void_reason_default = _("Voided via admin bulk action by %(user)s on %(date)s.") % {
                'user': request.user.name if request.user else 'System', 'date': timezone.now().strftime('%Y-%m-%d')}
            self._call_service_action_single_item(request, queryset,
                                                  service_method_name='void_customer_payment',
                                                  item_id_param_name='payment_id',
                                                  success_msg_template=_("{count} payment(s) successfully VOIDED."),
                                                  eligibility_func=lambda pmt: pmt.status != PaymentStatus.VOID.value,
                                                  action_kwargs_func=lambda pmt: {'void_reason': void_reason_default,
                                                                                  'void_date': timezone.now().date()})
        else:
            messages.error(request,
                           _("VOID Payment service method ('void_customer_payment') is not implemented in receivables_service.py."))
            logger.error("Admin Action: Attempted to call non-existent service 'void_customer_payment'.")