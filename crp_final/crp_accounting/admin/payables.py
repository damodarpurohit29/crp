# crp_accounting/admin/payables.py
import logging
from decimal import Decimal
from typing import Optional, Any, Dict, List, Tuple, Callable

from django.contrib import admin, messages
from django.db import models
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied, \
    ObjectDoesNotExist
from django.http import HttpRequest
from django.utils import timezone  # For void_date default

from . import CustomerInvoiceAdmin
# --- Base Admin Class Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model Imports ---
from ..models.payables import (
    BillSequence, VendorBill, BillLine,
    VendorPayment, VendorPaymentAllocation, PaymentSequence
)
from ..models.coa import Account
from ..models.party import Party
from ..models.journal import Voucher, VoucherType as JournalVoucherType  # Use a distinct alias if needed
from company.models import Company

# --- Enum Imports ---
from crp_core.enums import AccountType as CoreAccountType, PartyType as CorePartyType, PartyType

# --- Service Imports ---
from ..services import payables_service
from ..services.payables_service import (
    PayablesServiceError, BillProcessingError, PaymentProcessingError,
    AllocationError, GLPostingError, SequenceGenerationError
)

logger = logging.getLogger("crp_accounting.admin.payables")
ZERO = Decimal('0.00')


# =============================================================================
# BillSequence Admin
# =============================================================================
@admin.register(BillSequence)
class BillSequenceAdmin(TenantAccountingModelAdmin):  # Inherit base
    list_display = (
    'prefix', 'current_period_key_display', 'current_number', 'padding_digits', 'period_format_for_reset', 'updated_at')
    # Company column handled by base for SU
    list_filter_non_superuser = ('prefix', 'period_format_for_reset')
    search_fields = ('prefix', 'company__name')  # company__name for SU
    readonly_fields = ('current_number', 'current_period_key')  # Base handles created/updated audit fields
    fieldsets = (
        (None, {'fields': ('company', 'prefix')}),  # company field for SU on add
        (_('Numbering Format'), {'fields': ('padding_digits', 'period_format_for_reset')}),
        (_('Current State (System Managed)'), {'fields': ('current_period_key', 'current_number')}),
        (_('Audit Info'),
         {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    @admin.display(description=_("Current Period Key"), ordering='current_period_key')
    def current_period_key_display(self, obj: BillSequence) -> str:
        return obj.current_period_key or _("(Continuous/Not Yet Used)")

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser


@admin.register(PaymentSequence)  # Similar Admin for PaymentSequence
class PaymentSequenceAdmin(TenantAccountingModelAdmin):
    list_display = (
    'prefix', 'current_period_key_display', 'current_number', 'padding_digits', 'period_format_for_reset', 'updated_at')
    list_filter_non_superuser = ('prefix', 'period_format_for_reset')
    search_fields = ('prefix', 'company__name')
    readonly_fields = ('current_number', 'current_period_key')
    fieldsets = (
        (None, {'fields': ('company', 'prefix')}),
        (_('Numbering Format'), {'fields': ('padding_digits', 'period_format_for_reset')}),
        (_('Current State (System Managed)'), {'fields': ('current_period_key', 'current_number')}),
        (_('Audit Info'),
         {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    current_period_key_display = BillSequenceAdmin.current_period_key_display  # Reuse display method
    get_list_filter = BillSequenceAdmin.get_list_filter  # Reuse filter logic


# =============================================================================
# BillLine Inline Admin
# =============================================================================
class BillLineInline(admin.TabularInline):
    model = BillLine
    fields = (
    'sequence', 'expense_account', 'description', 'quantity', 'unit_price', 'tax_amount_on_line', 'amount_display',
    'line_total_inclusive_tax_display')
    readonly_fields = ('amount_display', 'line_total_inclusive_tax_display')
    autocomplete_fields = ['expense_account']
    extra = 0  # Prefer "Add another"
    verbose_name = _("Bill Line Item")
    verbose_name_plural = _("Bill Line Items")
    fk_name = 'vendor_bill'  # Explicitly define if model has multiple FKs to parent (not here, but good habit)

    @admin.display(description=_('Amount (Excl. Tax)'))
    def amount_display(self, obj: BillLine) -> Decimal:
        return obj.amount or ZERO

    @admin.display(description=_('Line Total (Incl. Tax)'))
    def line_total_inclusive_tax_display(self, obj: BillLine) -> Decimal:
        return obj.line_total_inclusive_tax or ZERO

    def _get_parent_bill_context(self, request: HttpRequest) -> Optional[VendorBill]:
        return getattr(request, '_current_parent_bill_for_line_inline', None)

    def _get_company_for_inline_filtering(self, request: HttpRequest, parent_bill: Optional[VendorBill]) -> Optional[
        Company]:
        # (Logic similar to InvoiceLineInline._get_company_for_inline_filtering, adapted for VendorBill)
        log_prefix = f"[BL_Inline_GetCo][User:{request.user.name}]"
        if parent_bill and parent_bill.pk and parent_bill.company_id:
            if hasattr(parent_bill, 'company') and parent_bill.company: return parent_bill.company
            try:
                return Company.objects.get(pk=parent_bill.company_id)
            except Company.DoesNotExist:
                logger.error(f"{log_prefix} Parent Bill {parent_bill.pk} invalid company_id."); return None
        is_parent_add_view = not (parent_bill and parent_bill.pk)
        if is_parent_add_view and request.method == 'POST':
            company_pk = request.POST.get('company')
            if company_pk:
                try:
                    return Company.objects.get(pk=company_pk)
                except (Company.DoesNotExist, ValueError, TypeError):
                    logger.warning(f"{log_prefix} Invalid company PK '{company_pk}' from POST.")
        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company): return request_company
        logger.warning(f"{log_prefix} Could not determine company context for BillLine filtering.")
        return None

    def get_formset(self, request: Any, obj: Optional[VendorBill] = None, **kwargs: Any) -> Any:
        request._current_parent_bill_for_line_inline = obj
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_bill = self._get_parent_bill_context(request)
        company_for_filtering = self._get_company_for_inline_filtering(request, parent_bill)

        if db_field.name == "expense_account":
            if company_for_filtering:
                # Exclude Revenue, Equity, and AR/AP control accounts
                kwargs["queryset"] = Account.objects.filter(company=company_for_filtering, is_active=True,
                                                            allow_direct_posting=True) \
                    .exclude(account_type__in=[CoreAccountType.INCOME.value, CoreAccountType.EQUITY.value]) \
                    .exclude(is_control_account=True,
                             control_account_party_type__in=[PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]) \
                    .select_related('account_group').order_by('account_group__name', 'account_number')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_bill_editable(self, parent_bill: Optional[VendorBill]) -> bool:
        if parent_bill is None: return True
        return parent_bill.status in [VendorBill.BillStatus.DRAFT.value]  # Only DRAFT bills allow line changes

    def has_add_permission(self, request, obj=None):
        parent = self._get_parent_bill_context(request) or obj; return super().has_add_permission(request,
                                                                                                  parent) and self._is_parent_bill_editable(
            parent)

    def has_change_permission(self, request, obj=None):
        parent_ctx = obj.vendor_bill if isinstance(obj, BillLine) else self._get_parent_bill_context(request) or (
            obj if isinstance(obj, VendorBill) else None); return super().has_change_permission(request,
                                                                                                obj) and self._is_parent_bill_editable(
            parent_ctx)

    def has_delete_permission(self, request, obj=None):
        parent_ctx = obj.vendor_bill if isinstance(obj, BillLine) else self._get_parent_bill_context(request) or (
            obj if isinstance(obj, VendorBill) else None); return super().has_delete_permission(request,
                                                                                                obj) and self._is_parent_bill_editable(
            parent_ctx)


# =============================================================================
# VendorPaymentAllocation Inline (For VendorBillAdmin)
# =============================================================================
class VendorPaymentAllocationInlineForBill(admin.TabularInline):
    model = VendorPaymentAllocation
    extra = 0
    fields = ('vendor_payment_link', 'allocated_amount_display', 'allocation_date_display')
    readonly_fields = ('vendor_payment_link', 'allocated_amount_display', 'allocation_date_display')
    can_delete = False  # Allocations are usually managed from the Payment side or voided
    show_change_link = True  # Link to VendorPaymentAllocationAdmin if registered (optional)
    verbose_name = _("Payment Allocation on this Bill")
    verbose_name_plural = _("Payment Allocations on this Bill")

    @admin.display(description=_("Payment"), ordering='vendor_payment__payment_number')
    def vendor_payment_link(self, obj: VendorPaymentAllocation):
        if obj.vendor_payment_id:
            try:
                vp = obj.vendor_payment if hasattr(obj,
                                                   'vendor_payment') and obj.vendor_payment else VendorPayment.objects.get(
                    pk=obj.vendor_payment_id)
                url = reverse("admin:crp_accounting_vendorpayment_change", args=[vp.pk])
                return format_html('<a href="{}" target="_blank">{}</a>', url, vp.payment_number or f"Pmt#{vp.pk}")
            except (NoReverseMatch, VendorPayment.DoesNotExist):
                return f"Payment ID {obj.vendor_payment_id} (Link Error)"
        return "—"

    @admin.display(description=_("Allocated Amt"))
    def allocated_amount_display(self, obj: VendorPaymentAllocation):
        return f"{obj.allocated_amount or ZERO:.2f}"  # Add currency from payment

    @admin.display(description=_("Alloc. Date"))
    def allocation_date_display(self, obj: VendorPaymentAllocation):
        return obj.allocation_date.strftime('%Y-%m-%d') if obj.allocation_date else ""

    def has_add_permission(self, request, obj=None):
        return False  # Allocations are added via Payment form


# =============================================================================
# VendorBill Admin
# =============================================================================
@admin.register(VendorBill)
class VendorBillAdmin(TenantAccountingModelAdmin):
    list_display = ('bill_number_display', 'supplier_link', 'issue_date', 'due_date_display', 'total_amount_display',
                    'amount_due_display', 'status_colored', 'related_gl_voucher_link')
    list_filter_non_superuser = (
    'status', ('supplier', admin.RelatedOnlyFieldListFilter), ('issue_date', admin.DateFieldListFilter), 'currency')
    search_fields = ('bill_number', 'supplier__name', 'supplier_bill_reference', 'company__name')
    readonly_fields_base = (
    'amount_paid', 'amount_due', 'related_gl_voucher_link', 'approved_by', 'approved_at', 'subtotal_amount',
    'tax_amount', 'total_amount')  # Audit fields handled by base
    autocomplete_fields = ['company', 'supplier', 'related_gl_voucher', 'approved_by']
    inlines = [BillLineInline, VendorPaymentAllocationInlineForBill]
    actions = ['admin_action_submit_bills_for_approval', 'admin_action_approve_bills', 'admin_action_post_bills_to_gl',
               'admin_action_void_bills', 'action_soft_delete_selected', 'action_undelete_selected']
    ordering = ('-issue_date', '-created_at')
    date_hierarchy = 'issue_date'
    list_select_related = ('company', 'supplier', 'created_by', 'approved_by', 'related_gl_voucher__company')

    # Fieldsets
    add_fieldsets = ((None, {'fields': ('company', 'supplier', 'issue_date', 'due_date', 'currency')}),
                     (_('Bill Identifiers & Notes'), {'fields': ('bill_number', 'supplier_bill_reference', 'notes')}),)
    change_fieldsets_draft = (
    (None, {'fields': ('company', 'supplier', 'status', 'issue_date', 'due_date', 'currency')}),
    (_('Bill Identifiers & Notes'), {'fields': ('bill_number', 'supplier_bill_reference', 'notes')}), (
    _('Amounts (Calculated)'),
    {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
    (_('GL & Approval Info'), {'fields': ('related_gl_voucher_link', 'approved_by', 'approved_at')}),
    (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_submitted = (
    (None, {'fields': ('company', 'supplier', 'status', 'issue_date', 'due_date', 'currency')}),
    (_('Bill Identifiers & Notes'), {'fields': ('bill_number', 'supplier_bill_reference', 'notes')}), (
    _('Amounts (Calculated)'),
    {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
    (_('GL & Approval Info'), {'fields': ('related_gl_voucher_link', 'approved_by', 'approved_at')}),
    (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_approved_or_paid = (
    (None, {'fields': ('company', 'supplier', 'status', 'issue_date', 'due_date', 'currency')}), (
    _('Bill Identifiers & Notes (Read-Only)'),
    {'fields': ('bill_number', 'supplier_bill_reference', 'notes'), 'classes': ('collapse',)}), (
    _('Amounts (Calculated)'),
    {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
    (_('GL & Approval Info'), {'fields': ('related_gl_voucher_link', 'approved_by', 'approved_at')}),
    (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_void = change_fieldsets_approved_or_paid  # Similar readonly nature for void

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        if obj is None: return self.add_fieldsets
        if obj.status == VendorBill.BillStatus.DRAFT.value: return self.change_fieldsets_draft
        if obj.status == VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value: return self.change_fieldsets_submitted
        return self.change_fieldsets_approved_or_paid  # Covers APPROVED, PAID, PARTIALLY_PAID, VOID

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj:
            if obj.status != VendorBill.BillStatus.DRAFT.value: ro.update(
                ['supplier', 'issue_date', 'due_date', 'currency', 'supplier_bill_reference', 'notes'])
            if obj.bill_number and obj.bill_number.strip(): ro.add('bill_number')
            if obj.status == VendorBill.BillStatus.VOID.value: ro.add('status')  # Cannot change from VOID
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request,
                                                                                              request.resolver_match.kwargs.get(
                                                                                                  'object_id')) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)
        if db_field.name == "supplier":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(company=company_context,
                                                          party_type=CorePartyType.SUPPLIER.value,
                                                          is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorBill, form, change):
        is_new = not obj.pk
        if is_new: obj.created_by = request.user
        # updated_by handled by TenantAccountingModelAdmin

        original_status = form.initial.get('status') if change else None
        finalizing_from_draft_or_submitted = (change and original_status in [VendorBill.BillStatus.DRAFT.value,
                                                                             VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value] and \
                                              obj.status not in [VendorBill.BillStatus.DRAFT.value,
                                                                 VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value]) or \
                                             (is_new and obj.status not in [VendorBill.BillStatus.DRAFT.value,
                                                                            VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value])

        if finalizing_from_draft_or_submitted and (not obj.bill_number or not obj.bill_number.strip()):
            if not obj.company_id:  # Should be set by base admin before this
                form.add_error('company', _("Company must be set to generate a bill number."));
                return
            try:
                obj.bill_number = payables_service.get_next_bill_number(obj.company, obj.issue_date)
            except SequenceGenerationError as e:
                form.add_error(None, DjangoValidationError(str(e), code='bill_num_gen_fail')); return

        super().save_model(request, obj, form, change)  # Saves header, calls obj.full_clean()

    def save_formset(self, request, form, formset, change):  # For BillLineInline
        super().save_formset(request, form, formset, change)  # Saves lines
        bill_instance: VendorBill = form.instance
        if bill_instance and bill_instance.pk:  # BillLine.save/delete now triggers this
            logger.debug(
                f"[VBAdmin SaveFormset] Bill {bill_instance.bill_number or bill_instance.pk} lines changed. Model's save should have handled recalc.")
            # No explicit call to _recalculate_derived_fields here if BillLine.save/delete handles it.
            # If there's a scenario where lines are changed but parent doesn't get updated, add:
            # bill_instance._recalculate_derived_fields(perform_save=True)

    # Display Helpers
    @admin.display(description=_('Bill No.'), ordering='bill_number')
    def bill_number_display(self, obj: VendorBill):
        return obj.bill_number or (_("Draft (PK:%(pk)s)") % {'pk': obj.pk})

    @admin.display(description=_('Supplier'), ordering='supplier__name')
    def supplier_link(self, obj: VendorBill):
        if not obj.supplier_id: return "—"
        try:
            name = obj.supplier.name if hasattr(obj, 'supplier') and obj.supplier else Party.objects.values_list('name',
                                                                                                                 flat=True).get(
                pk=obj.supplier_id); link = reverse("admin:crp_accounting_party_change",
                                                    args=[obj.supplier_id]); return format_html('<a href="{}">{}</a>',
                                                                                                link, name)
        except (NoReverseMatch, Party.DoesNotExist):
            return str(obj.supplier_id)

    @admin.display(description=_('Due Date'), ordering='due_date')
    def due_date_display(self, obj: VendorBill):
        return obj.due_date.strftime('%Y-%m-%d') if obj.due_date else "—"

    @admin.display(description=_('Total Amt'), ordering='total_amount')
    def total_amount_display(self, obj: VendorBill):
        return f"{obj.total_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Amt Due'), ordering='amount_due')
    def amount_due_display(self, obj: VendorBill):
        return f"{obj.amount_due or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: VendorBill):
        # (Color map similar to CustomerInvoiceAdmin.status_colored, adapted for BillStatus)
        cs = {VendorBill.BillStatus.DRAFT.value: "grey", VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value: "#ffc107",
              VendorBill.BillStatus.APPROVED.value: "#28a745", VendorBill.BillStatus.PARTIALLY_PAID.value: "#17a2b8",
              VendorBill.BillStatus.PAID.value: "#007bff", VendorBill.BillStatus.VOID.value: "black"}
        return format_html(f'<strong style="color:{cs.get(obj.status, "black")};">{obj.get_status_display()}</strong>')

    related_gl_voucher_link = CustomerInvoiceAdmin.related_gl_voucher_link  # Reuse from CustomerInvoiceAdmin

    # Admin Actions (Using a generic helper for single item service calls)
    def _call_payables_service_single(self, request: HttpRequest, queryset: models.QuerySet, service_method_name: str,
                                      item_id_param_name: str, success_msg_template: str,
                                      eligibility_func: Optional[Callable[[Any], bool]] = None,
                                      action_kwargs_func: Optional[Callable[[Any], Dict]] = None):
        # (This helper is similar to the one in CustomerInvoiceAdmin, ensure it's robust)
        # For brevity, not repeating the full helper, but it should iterate, call service, handle messages.
        # Key is to pass correct params to service_method.
        processed_count, error_count, skipped_count = 0, 0, 0
        for item in queryset:
            item_str = str(getattr(item, 'bill_number', None) or getattr(item, 'payment_number', None) or item.pk)
            if eligibility_func and not eligibility_func(item): skipped_count += 1; continue
            try:
                if not item.company_id: messages.error(request, _("Item missing company.")); error_count += 1; continue
                service_method = getattr(payables_service, service_method_name)
                current_action_kwargs = action_kwargs_func(item) if action_kwargs_func else {}
                # Dynamic parameter naming for item ID based on service function needs
                method_params = {'company_id': item.company_id, 'user': request.user, item_id_param_name: item.pk,
                                 **current_action_kwargs}
                if service_method_name == 'post_vendor_bill_to_gl':  # Specific params for this one
                    method_params = {'bill_id': item.pk, 'company_id': item.company_id, 'posting_user': request.user}
                elif service_method_name == 'post_vendor_payment_to_gl':
                    method_params = {'payment_id': item.pk, 'company_id': item.company_id, 'posting_user': request.user}

                service_method(**method_params)
                processed_count += 1
            except (DjangoValidationError, PayablesServiceError, DjangoPermissionDenied, ObjectDoesNotExist) as e_serv:
                msg = getattr(e_serv, 'message_dict', None);
                msg = "; ".join([f"{k}: {v[0]}" for k, v in msg.items()]) if msg else str(e_serv)
                messages.error(request, f"{item._meta.verbose_name.capitalize()} '{item_str}': {msg}");
                error_count += 1
            except Exception as e_unexp:
                logger.exception(f"Admin Action '{service_method_name}' error for {item.pk}"); messages.error(request,
                                                                                                              _("Unexpected error on item '%(is)s': %(e)s") % {
                                                                                                                  'is': item_str,
                                                                                                                  'e': str(
                                                                                                                      e_unexp)}); error_count += 1
        if processed_count: messages.success(request,
                                             success_msg_template.format(count=processed_count))  # Use .format()
        if error_count:
            messages.warning(request,
                             _("Action completed with %(ec)d error(s). Processed: %(pc)d. Skipped: %(sc)d.") % {
                                 'ec': error_count, 'pc': processed_count, 'sc': skipped_count})
        elif skipped_count and not processed_count:
            messages.info(request, _("No items eligible."))
        elif not queryset.exists():
            messages.info(request, _("No items selected."))

    @admin.action(description=_('Submit selected DRAFT bills for approval'))
    def admin_action_submit_bills_for_approval(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset,
                                           service_method_name='submit_vendor_bill_for_approval',
                                           item_id_param_name='bill_id',
                                           success_msg_template=_("{count} bill(s) submitted for approval."),
                                           eligibility_func=lambda
                                               bill: bill.status == VendorBill.BillStatus.DRAFT.value
                                           )

    @admin.action(description=_('Approve selected SUBMITTED bills'))
    def admin_action_approve_bills(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset,
                                           service_method_name='approve_vendor_bill', item_id_param_name='bill_id',
                                           success_msg_template=_("{count} bill(s) approved."),
                                           eligibility_func=lambda
                                               bill: bill.status == VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value,
                                           action_kwargs_func=lambda bill: {
                                               'approval_notes': _("Approved via admin bulk action.")}
                                           )

    @admin.action(description=_("Post selected APPROVED bills to GL"))
    def admin_action_post_bills_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset,
                                           service_method_name='post_vendor_bill_to_gl', item_id_param_name='bill_id',
                                           # item_id_param_name is for the helper's dynamic param. The actual call uses specific names.
                                           success_msg_template=_("{count} bill(s) posted to GL."),
                                           eligibility_func=lambda
                                               bill: bill.status == VendorBill.BillStatus.APPROVED.value and not bill.related_gl_voucher_id
                                           )

    @admin.action(description=_("Void selected bills"))
    def action_void_bills(self, request: HttpRequest, queryset: models.QuerySet):
        # Voiding often requires a reason. For bulk, use a default or an intermediate form.
        default_reason = _("Voided via admin action by %(user)s") % {'user': request.user.name}
        self._call_payables_service_single(request, queryset,
                                           service_method_name='void_vendor_bill', item_id_param_name='bill_id',
                                           # Pass 'vendor_bill' instance if service expects that name
                                           success_msg_template=_("{count} bill(s) voided."),
                                           eligibility_func=lambda
                                               bill: bill.status != VendorBill.BillStatus.VOID.value,
                                           action_kwargs_func=lambda bill: {'void_reason': default_reason,
                                                                            'void_date': timezone.now().date()}
                                           )


# =============================================================================
# VendorPaymentAllocation Inline (For VendorPaymentAdmin)
# =============================================================================
class VendorPaymentAllocationInlineForPayment(admin.TabularInline):
    model = VendorPaymentAllocation
    fields = ('vendor_bill_link', 'allocated_amount', 'allocation_date')
    readonly_fields = ('vendor_bill_link',)  # Bill selected via autocomplete on add
    extra = 0
    autocomplete_fields = ['vendor_bill']
    verbose_name = _("Bill Allocation");
    verbose_name_plural = _("Bill Allocations")
    fk_name = 'vendor_payment'  # Explicitly define if model has multiple FKs to parent

    @admin.display(description=_('Vendor Bill (Current Due)'))
    def vendor_bill_link(self, obj: VendorPaymentAllocation) -> str:
        if obj.vendor_bill_id:
            try:
                # Prefer preloaded related object if available
                bill = obj.vendor_bill if hasattr(obj, 'vendor_bill') and obj.vendor_bill else \
                    VendorBill.objects.select_related('company').get(pk=obj.vendor_bill_id)
                link = reverse("admin:crp_accounting_vendorbill_change", args=[bill.pk])
                due_display = f"{bill.amount_due or ZERO:.2f} {bill.currency}"
                return format_html('<a href="{url}" target="_blank">{num}</a> (Due: {due})',
                                   url=link,
                                   num=bill.bill_number or bill.supplier_bill_reference or f"Bill#{bill.pk}",
                                   due=due_display)
            except (NoReverseMatch, VendorBill.DoesNotExist):
                return f"Bill ID: {obj.vendor_bill_id} (Link/Data Error)"
            except Exception as e:  # Catch any other error during formatting
                logger.error(f"Error rendering vendor_bill_link for alloc {obj.pk}: {e}")
                return f"Bill ID: {obj.vendor_bill_id} (Display Error)"
        return "—"

    def _get_parent_payment_context(self, request: HttpRequest) -> Optional[VendorPayment]:
        """Retrieves the parent VendorPayment instance being edited/added from the request."""
        return getattr(request, '_current_parent_payment_for_alloc_inline', None)

    def _get_company_supplier_for_alloc_filter(self, request: HttpRequest, parent_payment: Optional[VendorPayment]) -> \
    Tuple[Optional[Company], Optional[Party]]:
        """
        Determines the Company and Supplier context for filtering VendorBill choices.
        1. Uses parent_payment's company and supplier if it's an existing, saved payment.
        2. If adding a new payment (parent_payment is None or not saved):
           - On POST: Tries to get Company and Supplier from the main VendorPayment form's POST data.
           - On GET (or if POST data invalid): Uses request.company (for non-SU or SU "acting as").
             Supplier context might be unavailable on initial GET for a new payment, leading to no bill choices.
        """
        log_prefix = f"[VPA_Inline_GetCoSupp][User:{request.user.name}]"

        # --- Case 1: Editing an existing, saved VendorPayment ---
        if parent_payment and parent_payment.pk:  # parent_payment is a saved instance
            company_instance: Optional[Company] = None
            supplier_instance: Optional[Party] = None

            # Get Company
            if parent_payment.company_id:
                company_instance = parent_payment.company if hasattr(parent_payment,
                                                                     'company') and parent_payment.company else None
                if not company_instance:
                    try:
                        company_instance = Company.objects.get(pk=parent_payment.company_id)
                    except Company.DoesNotExist:
                        logger.error(
                            f"{log_prefix} Parent Payment {parent_payment.pk} has invalid company_id."); return None, None
            else:  # Should not happen for a saved TenantScopedModel
                logger.error(f"{log_prefix} Parent Payment {parent_payment.pk} missing company_id.");
                return None, None

            # Get Supplier
            if parent_payment.supplier_id:
                supplier_instance = parent_payment.supplier if hasattr(parent_payment,
                                                                       'supplier') and parent_payment.supplier else None
                if not supplier_instance:
                    try:
                        supplier_instance = Party.objects.get(pk=parent_payment.supplier_id, company=company_instance)
                    except Party.DoesNotExist:
                        logger.error(
                            f"{log_prefix} Parent Payment {parent_payment.pk} has invalid supplier_id {parent_payment.supplier_id} for company {company_instance.name if company_instance else 'N/A'}."); return company_instance, None
            else:  # Supplier is mandatory on VendorPayment
                logger.error(f"{log_prefix} Parent Payment {parent_payment.pk} missing supplier_id.");
                return company_instance, None

            if company_instance and supplier_instance:
                logger.debug(
                    f"{log_prefix} Using Co '{company_instance.name}' and Supp '{supplier_instance.name}' from existing ParentPmt {parent_payment.pk}.")
                return company_instance, supplier_instance
            return None, None  # Should ideally not be reached if parent_payment is valid

        # --- Case 2: Adding a new VendorPayment (parent_payment is None or not yet saved with PK) ---
        is_parent_add_view = not (parent_payment and parent_payment.pk)
        if is_parent_add_view:
            logger.debug(f"{log_prefix} In 'Add New Payment' or unsaved parent context.")
            if request.method == 'POST':
                company_pk_from_main_form = request.POST.get(
                    'company')  # Name of company field on VendorPaymentAdmin form
                supplier_pk_from_main_form = request.POST.get('supplier')  # Name of supplier field

                if company_pk_from_main_form and supplier_pk_from_main_form:
                    try:
                        company_instance = Company.objects.get(pk=company_pk_from_main_form)
                        # Ensure supplier is fetched within the context of the company from form
                        supplier_instance = Party.objects.get(pk=supplier_pk_from_main_form, company=company_instance,
                                                              party_type=PartyType.SUPPLIER.value)
                        logger.debug(
                            f"{log_prefix} Using Co '{company_instance.name}' and Supp '{supplier_instance.name}' from main form POST data.")
                        return company_instance, supplier_instance
                    except (Company.DoesNotExist, Party.DoesNotExist, ValueError, TypeError):
                        logger.warning(
                            f"{log_prefix} Invalid company_pk ('{company_pk_from_main_form}') or supplier_pk ('{supplier_pk_from_main_form}') from main form POST data.")
                        # Fall through if POST data is invalid, try request.company
                else:
                    logger.debug(
                        f"{log_prefix} Missing 'company' or 'supplier' field in POST data for new payment context.")

            # Fallback for GET requests (initial "Add Payment" page) or if POST didn't yield company/supplier:
            request_company = getattr(request, 'company', None)
            if isinstance(request_company, Company):
                # For a new payment, we can get the company from request context for filtering,
                # but supplier is usually not known until selected on the main form.
                # So, bill choices might be empty until supplier is also chosen.
                logger.debug(
                    f"{log_prefix} Using Co '{request_company.name}' from request.company. Supplier context is TBD from main form.")
                return request_company, None  # Supplier is None here, bill choices will be empty until supplier is picked on main form
            else:
                logger.debug(f"{log_prefix} request.company is not a valid Company instance for new payment.")

        logger.warning(f"{log_prefix} Could not determine company AND supplier context for allocation filtering.")
        return None, None

    def get_formset(self, request: Any, obj: Optional[VendorPayment] = None, **kwargs: Any) -> Any:
        request._current_parent_payment_for_alloc_inline = obj
        logger.debug(
            f"[VPA_Inline GetFormset][User:{request.user.name}] Stored parent payment (PK: {obj.pk if obj else 'None'}) on request.")
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_payment = self._get_parent_payment_context(request)
        # This helper now returns both company and supplier derived from the parent payment or form context.
        company_for_filtering, supplier_for_filtering = self._get_company_supplier_for_alloc_filter(request,
                                                                                                    parent_payment)

        log_prefix = f"[VPA_Inline FFKey][User:{request.user.name}][Fld:'{db_field.name}']"
        logger.debug(f"{log_prefix} ParentPmt (ctx): {parent_payment.pk if parent_payment else 'None'}. "
                     f"Effective Co: {company_for_filtering.name if company_for_filtering else 'None'}. "
                     f"Effective Supp: {supplier_for_filtering.name if supplier_for_filtering else 'None'}")

        if db_field.name == "vendor_bill":
            if company_for_filtering and supplier_for_filtering and parent_payment and parent_payment.currency:
                # Filter bills by the determined company, supplier, and payment's currency.
                # Also, only show bills that are not fully paid or void.
                kwargs["queryset"] = VendorBill.objects.filter(
                    company=company_for_filtering,
                    supplier=supplier_for_filtering,
                    currency=parent_payment.currency,  # Match currency
                    status__in=[VendorBill.BillStatus.APPROVED.value, VendorBill.BillStatus.PARTIALLY_PAID.value]
                ).exclude(amount_due__lte=ZERO).select_related('company', 'supplier').order_by('due_date', 'issue_date')
                logger.info(
                    f"{log_prefix} Filtered VendorBill choices for Co '{company_for_filtering.name}', Supp '{supplier_for_filtering.name}'. Count: {kwargs['queryset'].count()}.")
            else:
                kwargs["queryset"] = VendorBill.objects.none()
                logger.warning(
                    f"{log_prefix} Insufficient context (company, supplier, or payment currency missing). Setting VendorBill queryset to None.")

                is_add_view_parent = not (parent_payment and parent_payment.pk)
                # Message for SU on initial GET of "Add Payment" form
                if is_add_view_parent and request.user.is_superuser and request.method == 'GET':
                    if not (
                            company_for_filtering and supplier_for_filtering and parent_payment and parent_payment.currency):
                        messages.info(request,
                                      _("Select Company, Supplier & Currency on the main Payment form to populate Vendor Bill choices for allocation."))

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_payment_editable(self, parent_payment: Optional[VendorPayment]) -> bool:
        """Determines if allocations can be added/changed for the parent payment."""
        if parent_payment is None: return True  # Adding new payment, allocations are part of it
        # Allocations can usually be made/changed if the payment is not VOID
        # and perhaps only for DRAFT or APPROVED_FOR_PAYMENT statuses before it's fully COMPLETED/Posted,
        # depending on workflow. For now, allowing for non-VOID.
        return parent_payment.status != VendorPayment.PaymentStatus.VOID.value

    # Permissions for inline actions
    def has_add_permission(self, request, obj=None):  # obj is parent VendorPayment
        parent_payment = self._get_parent_payment_context(request) or obj
        return super().has_add_permission(request, parent_payment) and self._is_parent_payment_editable(parent_payment)

    def has_change_permission(self, request, obj=None):  # obj here is VendorPaymentAllocation or parent VendorPayment
        parent_payment_context = obj.vendor_payment if isinstance(obj,
                                                                  VendorPaymentAllocation) and obj.vendor_payment_id else \
            self._get_parent_payment_context(request) or (obj if isinstance(obj, VendorPayment) else None)
        return super().has_change_permission(request, obj) and self._is_parent_payment_editable(parent_payment_context)

    def has_delete_permission(self, request, obj=None):
        parent_payment_context = obj.vendor_payment if isinstance(obj,
                                                                  VendorPaymentAllocation) and obj.vendor_payment_id else \
            self._get_parent_payment_context(request) or (obj if isinstance(obj, VendorPayment) else None)
        return super().has_delete_permission(request, obj) and self._is_parent_payment_editable(parent_payment_context)

# =============================================================================
# VendorPayment Admin
# =============================================================================
@admin.register(VendorPayment)
class VendorPaymentAdmin(TenantAccountingModelAdmin):
    list_display = (
    'payment_number_display', 'supplier_link', 'payment_date', 'payment_method_display', 'payment_amount_display',
    'unallocated_amount_display', 'status_colored', 'related_gl_voucher_link')
    list_filter_non_superuser = (
    'status', ('supplier', admin.RelatedOnlyFieldListFilter), ('payment_date', admin.DateFieldListFilter),
    'payment_method', 'currency')
    search_fields = ('payment_number', 'supplier__name', 'reference_details', 'company__name', 'id')
    readonly_fields_base = (
    'allocated_amount', 'unallocated_amount', 'related_gl_voucher_link')  # Audit fields from base
    autocomplete_fields = ['company', 'supplier', 'payment_account', 'related_gl_voucher']
    inlines = [VendorPaymentAllocationInlineForPayment]
    actions = ['admin_action_approve_payments', 'admin_action_post_payments_to_gl', 'admin_action_void_payments',
               'action_soft_delete_selected', 'action_undelete_selected']
    ordering = ('-payment_date', '-created_at')
    date_hierarchy = 'payment_date'
    list_select_related = ('company', 'supplier', 'payment_account', 'created_by', 'related_gl_voucher__company')

    add_fieldsets = ((None, {'fields': (
    'company', 'supplier', 'payment_date', 'payment_method', 'payment_account', 'currency', 'payment_amount')}),
                     (_('Details'), {'fields': ('payment_number', 'reference_details', 'notes')}),)
    change_fieldsets_editable = ((None, {'fields': (
    'company', 'supplier', 'status', 'payment_date', 'payment_method', 'payment_account', 'currency',
    'payment_amount')}), (_('Details'), {'fields': ('payment_number', 'reference_details', 'notes')}),
                                 (_('Allocation Status'), {'fields': ('allocated_amount', 'unallocated_amount')}),
                                 (_('GL Info'), {'fields': ('related_gl_voucher_link',)}), (_('Audit'), {
        'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))

    # Add change_fieldsets_void if void status makes more fields readonly

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.change_fieldsets_editable

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj:
            if obj.status not in [VendorPayment.PaymentStatus.DRAFT.value]: ro.update(
                ['supplier', 'payment_date', 'payment_method', 'payment_account', 'currency', 'payment_amount',
                 'reference_details', 'notes'])
            if obj.payment_number and obj.payment_number.strip(): ro.add('payment_number')
            if obj.status == VendorPayment.PaymentStatus.VOID.value: ro.add('status')
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request,
                                                                                              request.resolver_match.kwargs.get(
                                                                                                  'object_id')) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)
        if db_field.name == "supplier":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(company=company_context,
                                                          party_type=CorePartyType.SUPPLIER.value,
                                                          is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        elif db_field.name == "payment_account":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=CoreAccountType.ASSET.value, is_active=True,
                                                            allow_direct_posting=True).order_by('account_name')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorPayment, form, change):
        is_new = not obj.pk
        if is_new: obj.created_by = request.user

        finalizing_from_draft = (change and form.initial.get(
            'status') == VendorPayment.PaymentStatus.DRAFT.value and obj.status != VendorPayment.PaymentStatus.DRAFT.value) or \
                                (is_new and obj.status != VendorPayment.PaymentStatus.DRAFT.value)
        if finalizing_from_draft and (not obj.payment_number or not obj.payment_number.strip()):
            if not obj.company_id: form.add_error('company', _("Company needed for payment number.")); return
            try:
                obj.payment_number = payables_service.get_next_payment_number(obj.company, obj.payment_date)
            except SequenceGenerationError as e:
                form.add_error(None, DjangoValidationError(str(e), code='pmt_num_gen_fail')); return

        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):  # For VendorPaymentAllocationInlineForPayment
        super().save_formset(request, form, formset, change)  # Saves allocations
        payment_instance: VendorPayment = form.instance
        if payment_instance and payment_instance.pk:  # PaymentAllocation.save/delete now triggers this
            logger.debug(
                f"[VPAdmin SaveFormset] Payment {payment_instance.payment_number or payment_instance.pk} allocations changed. Model's save should handle recalc.")
            # No explicit call if PaymentAllocation.save/delete handles it.
            # If needed: payment_instance._recalculate_derived_fields(perform_save=True)

    # Display Helpers
    @admin.display(description=_('Pmt No.'), ordering='payment_number')
    def payment_number_display(self, obj: VendorPayment):
        return obj.payment_number or (_("Draft (PK:%(pk)s)") % {'pk': obj.pk})

    supplier_link = VendorBillAdmin.supplier_link  # Reuse

    @admin.display(description=_('Pmt Method'), ordering='payment_method')
    def payment_method_display(self, obj: VendorPayment):
        return obj.get_payment_method_display() if obj.payment_method else "—"

    @admin.display(description=_('Pmt Amt'), ordering='payment_amount')
    def payment_amount_display(self, obj: VendorPayment):
        return f"{obj.payment_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Unallocated'), ordering='unallocated_amount')
    def unallocated_amount_display(self, obj: VendorPayment):
        return f"{obj.unallocated_amount or ZERO:.2f} {obj.currency}"

    status_colored = CustomerInvoiceAdmin.status_colored  # Reuse, but adapt color map if PaymentStatus differs significantly
    related_gl_voucher_link = CustomerInvoiceAdmin.related_gl_voucher_link  # Reuse

    _call_payables_service_single = VendorBillAdmin._call_payables_service_single  # Reuse helper

    # Admin Actions
    @admin.action(description=_('Approve selected DRAFT payments'))
    def admin_action_approve_payments(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset,
                                           service_method_name='approve_vendor_payment',
                                           item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) approved."),
                                           eligibility_func=lambda
                                               pmt: pmt.status == VendorPayment.PaymentStatus.DRAFT.value,
                                           action_kwargs_func=lambda pmt: {
                                               'approval_notes': _("Approved via admin bulk action.")}
                                           )

    @admin.action(description=_("Post selected APPROVED payments to GL"))
    def admin_action_post_payments_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset,
                                           service_method_name='post_vendor_payment_to_gl',
                                           item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) posted to GL."),
                                           eligibility_func=lambda
                                               pmt: pmt.status == VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value and not pmt.related_gl_voucher_id
                                           )

    @admin.action(description=_("Void selected payments"))
    def action_void_payments(self, request: HttpRequest, queryset: models.QuerySet):
        default_reason = _("Voided via admin action by %(user)s") % {'user': request.user.name}
        self._call_payables_service_single(request, queryset,
                                           service_method_name='void_vendor_payment', item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) voided."),
                                           eligibility_func=lambda
                                               pmt: pmt.status != VendorPayment.PaymentStatus.VOID.value,
                                           action_kwargs_func=lambda pmt: {'void_reason': default_reason,
                                                                           'void_date': timezone.now().date()}
                                           )


@admin.register(VendorPaymentAllocation)  # Standalone admin for allocations (optional)
class VendorPaymentAllocationAdmin(TenantAccountingModelAdmin):
    list_display = (
    'id', 'vendor_payment_link', 'vendor_bill_link', 'allocated_amount_display', 'allocation_date_display')
    # Company column handled by base for SU
    list_filter_non_superuser = (
    ('allocation_date', admin.DateFieldListFilter), 'vendor_payment__supplier', 'vendor_bill__supplier_bill_reference')
    search_fields = (
    'vendor_payment__payment_number', 'vendor_bill__bill_number', 'vendor_bill__supplier_bill_reference',
    'company__name')
    readonly_fields = ('company',)  # Company derived from payment, should not be directly editable here
    autocomplete_fields = ['vendor_payment', 'vendor_bill']  # No 'company' here, derived
    list_select_related = (
    'company', 'vendor_payment__company', 'vendor_payment__supplier', 'vendor_bill__company', 'vendor_bill__supplier')

    vendor_payment_link = VendorPaymentAllocationInlineForBill.vendor_payment_link  # Reuse
    vendor_bill_link = VendorPaymentAllocationInlineForPayment.vendor_bill_link  # Reuse

    @admin.display(description=_("Allocated Amt"))
    def allocated_amount_display(self, obj: VendorPaymentAllocation):
        return f"{obj.allocated_amount or ZERO:.2f}"  # Add currency from payment/bill

    @admin.display(description=_("Alloc. Date"))
    def allocation_date_display(self, obj: VendorPaymentAllocation):
        return obj.allocation_date.strftime('%Y-%m-%d') if obj.allocation_date else ""

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        # For standalone allocation admin, company context comes from request if non-SU, or can be selected if SU.
        # Then filter payment/bill by that company.
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request,
                                                                                              request.resolver_match.kwargs.get(
                                                                                                  'object_id')) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)

        if db_field.name == "vendor_payment":
            if company_context:
                kwargs["queryset"] = VendorPayment.objects.filter(company=company_context, status__in=[
                    VendorPayment.PaymentStatus.PAID_COMPLETED.value,
                    VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value]).exclude(
                    unallocated_amount__lte=ZERO)  # Only payments with unallocated amount
            else:
                kwargs["queryset"] = VendorPayment.objects.none()
        elif db_field.name == "vendor_bill":
            if company_context:  # Further filter by supplier if vendor_payment is selected in form
                payment_pk = request.POST.get('vendor_payment') if request.method == 'POST' else (
                    self.get_object(request, request.resolver_match.kwargs.get(
                        'object_id')).vendor_payment_id if request.resolver_match.kwargs.get(
                        'object_id') and self.get_object(request,
                                                         request.resolver_match.kwargs.get('object_id')) else None)
                supplier_id_for_filter = None
                if payment_pk:
                    try: supplier_id_for_filter = VendorPayment.objects.get(pk=payment_pk).supplier_id
                    except: pass

                qs = VendorBill.objects.filter(company=company_context,
                                               status__in=[VendorBill.BillStatus.APPROVED.value,
                                                           VendorBill.BillStatus.PARTIALLY_PAID.value]).exclude(
                    amount_due__lte=ZERO)
                if supplier_id_for_filter: qs = qs.filter(supplier_id=supplier_id_for_filter)
                kwargs["queryset"] = qs
            else:
                kwargs["queryset"] = VendorBill.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorPaymentAllocation, form, change):
        # Company should be derived from vendor_payment
        if obj.vendor_payment and obj.vendor_payment.company_id:
            obj.company = obj.vendor_payment.company
        super().save_model(request, obj, form,
                           change)  # This calls full_clean via base, and model save triggers parent updates