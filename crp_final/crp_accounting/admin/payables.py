# crp_accounting/admin/payables.py
import logging
from decimal import Decimal
from typing import Optional, Any, Dict, Tuple, Callable  # List was unused, removed

from django import forms  # Import forms for custom inline form
from django.contrib import admin, messages
from django.db import models
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied, \
    ObjectDoesNotExist
from django.http import HttpRequest
from django.utils import timezone  # For void_date default

# --- Base Admin Class Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model Imports ---
from ..models.payables import (
    BillSequence, VendorBill, BillLine,
    VendorPayment, VendorPaymentAllocation, PaymentSequence
)
from ..models.coa import Account
from ..models.party import Party
from ..models.journal import Voucher  # VoucherType as JournalVoucherType unused, removed
from company.models import Company

# --- Enum Imports ---
from crp_core.enums import AccountType as CoreAccountType, PartyType as CorePartyType, PartyType

# --- Service Imports ---
from ..services import payables_service
from ..services.payables_service import (  # Unused service exceptions removed for brevity
    SequenceGenerationError
)

# --- CustomerInvoiceAdmin Import for reusing methods (ensure this path is correct) ---
try:
    from .receivables import CustomerInvoiceAdmin  # Adjust if CustomerInvoiceAdmin is elsewhere
except ImportError:
    # Create a dummy class or raise an error if CustomerInvoiceAdmin is critical and not found
    class CustomerInvoiceAdmin:  # Dummy, replace with actual import or handle absence
        @staticmethod
        def related_gl_voucher_link(obj): return "GL Link N/A"
        # @staticmethod # status_colored is defined specifically in VendorPaymentAdmin now
        # def status_colored(obj): return str(obj.get_status_display())


    logger_module_level = logging.getLogger(__name__)  # Use a distinct logger name
    logger_module_level.warning("CustomerInvoiceAdmin could not be imported from .receivables. Using dummy methods.")

logger = logging.getLogger("crp_accounting.admin.payables")
ZERO = Decimal('0.00')


# =============================================================================
# Custom Form for VendorPaymentAllocation Inline
# =============================================================================
class VendorPaymentAllocationInlineForm(forms.ModelForm):
    class Meta:
        model = VendorPaymentAllocation
        fields = '__all__'  # Or list them explicitly: ['vendor_bill', 'allocated_amount', 'allocation_date', ...]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'vendor_bill' in self.fields:
            self.fields['vendor_bill'].label = _("Vendor Bill (Current Due)")


# =============================================================================
# BillSequence Admin
# =============================================================================
@admin.register(BillSequence)
class BillSequenceAdmin(TenantAccountingModelAdmin):  # Inherit base
    list_display = (
        'prefix', 'current_period_key_display', 'current_number', 'padding_digits', 'period_format_for_reset',
        'updated_at')
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

    @admin.display(description=_("Current Period Key"), ordering='current_period_key')
    def current_period_key_display(self, obj: BillSequence) -> str:
        return obj.current_period_key or _("(Continuous/Not Yet Used)")

    def get_list_filter(self, request):
        return (
                   'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser


@admin.register(PaymentSequence)
class PaymentSequenceAdmin(TenantAccountingModelAdmin):
    list_display = (
        'prefix', 'current_period_key_display', 'current_number', 'padding_digits', 'period_format_for_reset',
        'updated_at')
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
    current_period_key_display = BillSequenceAdmin.current_period_key_display
    get_list_filter = BillSequenceAdmin.get_list_filter


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
    extra = 0
    verbose_name = _("Bill Line Item")
    verbose_name_plural = _("Bill Line Items")
    fk_name = 'vendor_bill'

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
        return parent_bill.status in [VendorBill.BillStatus.DRAFT.value]

    def has_add_permission(self, request, obj=None):
        parent = self._get_parent_bill_context(request) or obj;
        return super().has_add_permission(request, parent) and self._is_parent_bill_editable(parent)

    def has_change_permission(self, request, obj=None):
        parent_ctx = obj.vendor_bill if isinstance(obj, BillLine) else self._get_parent_bill_context(request) or (
            obj if isinstance(obj, VendorBill) else None)
        return super().has_change_permission(request, obj) and self._is_parent_bill_editable(parent_ctx)

    def has_delete_permission(self, request, obj=None):
        parent_ctx = obj.vendor_bill if isinstance(obj, BillLine) else self._get_parent_bill_context(request) or (
            obj if isinstance(obj, VendorBill) else None)
        return super().has_delete_permission(request, obj) and self._is_parent_bill_editable(parent_ctx)


# =============================================================================
# VendorPaymentAllocation Inline (For VendorBillAdmin - ReadOnly View of Allocations)
# =============================================================================
class VendorPaymentAllocationInlineForBill(admin.TabularInline):
    model = VendorPaymentAllocation
    extra = 0
    fields = ('vendor_payment_link', 'allocated_amount_display', 'allocation_date_display')
    readonly_fields = ('vendor_payment_link', 'allocated_amount_display', 'allocation_date_display')
    can_delete = False
    show_change_link = True
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
        return f"{obj.allocated_amount or ZERO:.2f}"

    @admin.display(description=_("Alloc. Date"))
    def allocation_date_display(self, obj: VendorPaymentAllocation):
        return obj.allocation_date.strftime('%Y-%m-%d') if obj.allocation_date else ""

    def has_add_permission(self, request, obj=None):
        return False


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
        'tax_amount', 'total_amount')
    autocomplete_fields = ['company', 'supplier', 'related_gl_voucher', 'approved_by']
    inlines = [BillLineInline, VendorPaymentAllocationInlineForBill]
    actions = ['admin_action_submit_bills_for_approval', 'admin_action_approve_bills', 'admin_action_post_bills_to_gl',
               'action_void_bills', 'action_soft_delete_selected', 'action_undelete_selected']
    ordering = ('-issue_date', '-created_at')
    date_hierarchy = 'issue_date'
    list_select_related = ('company', 'supplier', 'created_by', 'approved_by', 'related_gl_voucher__company')

    add_fieldsets = ((None, {'fields': ('company', 'supplier', 'issue_date', 'due_date', 'currency')}),
                     (_('Bill Identifiers & Notes'), {'fields': ('bill_number', 'supplier_bill_reference', 'notes')}),)
    change_fieldsets_draft = (
        (None, {'fields': ('company', 'supplier', 'status', 'issue_date', 'due_date', 'currency')}),
        (_('Bill Identifiers & Notes'), {'fields': ('bill_number', 'supplier_bill_reference', 'notes')}),
        (_('Amounts (Calculated)'),
         {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL & Approval Info'), {'fields': ('related_gl_voucher_link', 'approved_by', 'approved_at')}),
        (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_submitted = change_fieldsets_draft
    change_fieldsets_approved_or_paid = (
        (None, {'fields': ('company', 'supplier', 'status', 'issue_date', 'due_date', 'currency')}),
        (_('Bill Identifiers & Notes (Read-Only)'),
         {'fields': ('bill_number', 'supplier_bill_reference', 'notes'), 'classes': ('collapse',)}),
        # Consider making notes editable if needed
        (_('Amounts (Calculated)'),
         {'fields': ('subtotal_amount', 'tax_amount', 'total_amount', 'amount_paid', 'amount_due')}),
        (_('GL & Approval Info'), {'fields': ('related_gl_voucher_link', 'approved_by', 'approved_at')}),
        (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}))
    change_fieldsets_void = change_fieldsets_approved_or_paid

    def get_list_filter(self, request):
        return (
                   'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        if obj is None: return self.add_fieldsets
        if obj.status == VendorBill.BillStatus.DRAFT.value: return self.change_fieldsets_draft
        if obj.status == VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value: return self.change_fieldsets_submitted
        # For APPROVED, PAID, PARTIALLY_PAID, VOID
        return self.change_fieldsets_approved_or_paid  # Merged approved/paid/void for simplicity

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        ro.update(self.readonly_fields_base)
        if obj:
            if obj.status != VendorBill.BillStatus.DRAFT.value:
                ro.update(['company', 'supplier', 'issue_date', 'due_date', 'currency', 'supplier_bill_reference'])
                # 'notes' could still be editable if desired, remove from here if so
            if obj.bill_number and obj.bill_number.strip():
                ro.add('bill_number')
            if obj.status == VendorBill.BillStatus.VOID.value:
                ro.update(['company', 'supplier', 'status', 'issue_date', 'due_date', 'currency',
                           'supplier_bill_reference', 'notes',
                           'bill_number'])  # Make almost everything readonly for VOID
        else:  # Add form
            if not request.user.is_superuser:
                ro.add('company')  # Non-superusers might have company pre-filled and readonly

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
        # Add similar filtering for 'approved_by' if needed, e.g., filter by users of the company_context
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorBill, form, change):
        is_new = not obj.pk
        if is_new:
            obj.created_by = request.user
            if not obj.company_id and hasattr(request, 'company') and isinstance(request.company,
                                                                                 Company):  # Pre-fill company for non-SU
                obj.company = request.company

        obj.updated_by = request.user  # Always set updated_by

        original_status = form.initial.get('status') if change else None

        finalizing_from_draft_or_submitted = (
                change and
                original_status in [VendorBill.BillStatus.DRAFT.value,
                                    VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value] and
                obj.status not in [VendorBill.BillStatus.DRAFT.value,
                                   VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value]
        )
        finalizing_on_create = (
                is_new and
                obj.status not in [VendorBill.BillStatus.DRAFT.value,
                                   VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value]
        )

        if (finalizing_from_draft_or_submitted or finalizing_on_create) and \
                (not obj.bill_number or not obj.bill_number.strip()):
            if not obj.company_id:
                # Attempt to get company from request if not set, for non-superusers
                if hasattr(request, 'company') and isinstance(request.company, Company):
                    obj.company = request.company
                else:
                    form.add_error('company', _("Company must be set to generate a bill number."))
                    return  # Stop save_model
            if obj.company:  # Ensure company is now set
                try:
                    obj.bill_number = payables_service.get_next_bill_number(obj.company, obj.issue_date)
                except SequenceGenerationError as e:
                    form.add_error(None, DjangoValidationError(str(e), code='bill_num_gen_fail'))
                    return  # Stop save_model
            else:  # Should have been caught by company check, but defensive
                form.add_error('company', _("Company is still missing, cannot generate bill number."))
                return

        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        bill_instance: VendorBill = form.instance
        if bill_instance and bill_instance.pk:
            logger.debug(
                f"[VBAdmin SaveFormset] Bill {bill_instance.bill_number or bill_instance.pk} lines changed. "
                f"Attempting to recalculate derived fields if status allows."
            )
            # Recalculate if draft or submitted, otherwise it's generally locked
            if bill_instance.status in [VendorBill.BillStatus.DRAFT.value,
                                        VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value]:
                try:
                    bill_instance._recalculate_derived_fields(perform_save=True)  # Save is important after line changes
                    logger.info(f"Recalculated derived fields for bill {bill_instance.pk} after formset save.")
                except Exception as e:
                    logger.error(f"Error recalculating bill {bill_instance.pk} after formset save: {e}")
                    messages.error(request,
                                   _("Could not update bill totals after line changes: %(error)s") % {'error': str(e)})

    @admin.display(description=_('Bill No.'), ordering='bill_number')
    def bill_number_display(self, obj: VendorBill):
        return obj.bill_number or (_("Draft (PK:%(pk)s)") % {'pk': obj.pk})

    @admin.display(description=_('Supplier'), ordering='supplier__name')
    def supplier_link(self, obj: VendorBill):
        if not obj.supplier_id: return "—"
        try:
            name = obj.supplier.name if hasattr(obj, 'supplier') and obj.supplier else Party.objects.values_list('name',
                                                                                                                 flat=True).get(
                pk=obj.supplier_id)
            link = reverse("admin:crp_accounting_party_change", args=[obj.supplier_id])
            return format_html('<a href="{}">{}</a>', link, name)
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
        cs = {VendorBill.BillStatus.DRAFT.value: "grey", VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value: "#ffc107",
              # Amber
              VendorBill.BillStatus.APPROVED.value: "#28a745",  # Green
              VendorBill.BillStatus.PARTIALLY_PAID.value: "#17a2b8",  # Teal
              VendorBill.BillStatus.PAID.value: "#007bff",  # Blue
              VendorBill.BillStatus.VOID.value: "black"}
        return format_html(f'<strong style="color:{cs.get(obj.status, "black")};">{obj.get_status_display()}</strong>')

    related_gl_voucher_link = CustomerInvoiceAdmin.related_gl_voucher_link  # Ensure CustomerInvoiceAdmin is correctly imported or handled

    # --- THIS IS THE CORRECTED HELPER METHOD ---
    def _call_payables_service_single(self, request: HttpRequest, queryset: models.QuerySet, service_method_name: str,
                                      item_id_param_name: str, success_msg_template: str,
                                      eligibility_func: Optional[Callable[[Any], bool]] = None,
                                      action_kwargs_func: Optional[Callable[[Any], Dict]] = None):
        processed_count, error_count, skipped_count = 0, 0, 0
        for item in queryset:
            item_str = str(getattr(item, 'bill_number', None) or getattr(item, 'payment_number', None) or item.pk)
            if eligibility_func and not eligibility_func(item):
                skipped_count += 1
                logger.info(
                    f"Admin Action '{service_method_name}': Item '{item_str}' (PK: {item.pk}) skipped due to eligibility.")
                continue
            try:
                if not item.company_id:
                    messages.error(request,
                                   _("Item '%(item_str)s' is missing company information.") % {'item_str': item_str})
                    error_count += 1
                    continue

                service_method = getattr(payables_service, service_method_name)
                current_action_kwargs = action_kwargs_func(item) if action_kwargs_func else {}

                method_params = {
                    'company_id': item.company_id,
                    item_id_param_name: item.pk,  # e.g., 'bill_id': item.pk or 'payment_id': item.pk
                    **current_action_kwargs
                }

                # Set the correct user parameter based on the service method's signature
                if service_method_name in ['post_vendor_bill_to_gl', 'post_vendor_payment_to_gl']:
                    method_params['posting_user'] = request.user
                elif service_method_name in ['void_vendor_bill', 'void_vendor_payment']:
                    method_params['voiding_user'] = request.user
                else:  # Default for submit_vendor_bill_for_approval, approve_vendor_bill, approve_vendor_payment etc.
                    method_params['user'] = request.user

                logger.debug(f"Calling service {service_method_name} with params: {method_params} for item {item_str}")
                service_method(**method_params)
                processed_count += 1
            except (payables_service.PayablesServiceError,
                    DjangoValidationError, DjangoPermissionDenied, ObjectDoesNotExist) as e_serv:
                msg_dict = getattr(e_serv, 'message_dict', None)
                if msg_dict:
                    error_messages_list = []
                    for field, messages_list_inner in msg_dict.items():
                        field_name_display = field if field != '__all__' else 'General'
                        error_messages_list.append(f"{field_name_display}: {'; '.join(messages_list_inner)}")
                    msg = "; ".join(error_messages_list)
                elif hasattr(e_serv, 'messages') and isinstance(e_serv.messages, list):
                    msg = "; ".join(e_serv.messages)
                else:
                    msg = str(e_serv)

                logger.warning(
                    f"Admin Action '{service_method_name}' on item '{item_str}' (PK: {item.pk}) failed: {msg}",
                    exc_info=False)  # exc_info=False for known errors
                messages.error(request, f"{item._meta.verbose_name.capitalize()} '{item_str}': {msg}")
                error_count += 1
            except Exception as e_unexp:
                logger.exception(
                    f"Admin Action '{service_method_name}' unexpected error for item {item_str} (PK: {item.pk})")
                messages.error(request,
                               _("Unexpected error on item '%(is)s': %(e)s") % {'is': item_str, 'e': str(e_unexp)})
                error_count += 1

        if processed_count:
            messages.success(request, success_msg_template.format(count=processed_count))

        summary_parts = []
        if error_count: summary_parts.append(_("%(count)d error(s)") % {'count': error_count})
        if skipped_count: summary_parts.append(_("%(count)d skipped") % {'count': skipped_count})

        if summary_parts:
            processed_msg = _("Processed: %(count)d.") % {'count': processed_count} if processed_count else ""
            final_summary_msg = _("Action completed with ") + ", ".join(summary_parts) + ". " + processed_msg
            if error_count > 0:
                messages.warning(request, final_summary_msg.strip())
            else:  # Only skipped
                messages.info(request, final_summary_msg.strip())

        elif not processed_count and not error_count and not skipped_count and not queryset.exists():
            messages.info(request, _("No items selected for action."))
        elif not processed_count and not error_count and skipped_count:  # Only skipped, no errors, no processed
            messages.info(request,
                          _("All selected items were skipped (%(count)d item(s)) as they were not eligible.") % {
                              'count': skipped_count})

    @admin.action(description=_('Submit selected DRAFT bills for approval'))
    def admin_action_submit_bills_for_approval(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(
            request, queryset,
            service_method_name='submit_vendor_bill_for_approval',
            item_id_param_name='bill_id',
            success_msg_template=_("{count} bill(s) submitted for approval."),
            eligibility_func=lambda bill: bill.status == VendorBill.BillStatus.DRAFT.value
        )

    @admin.action(description=_('Approve selected SUBMITTED bills'))
    def admin_action_approve_bills(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(
            request, queryset,
            service_method_name='approve_vendor_bill',
            item_id_param_name='bill_id',
            success_msg_template=_("{count} bill(s) approved."),
            eligibility_func=lambda bill: bill.status == VendorBill.BillStatus.SUBMITTED_FOR_APPROVAL.value,
            action_kwargs_func=lambda bill: {'approval_notes': _("Approved via admin bulk action.")}
        )

    @admin.action(description=_("Post selected APPROVED bills to GL"))
    def admin_action_post_bills_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(
            request, queryset,
            service_method_name='post_vendor_bill_to_gl',
            item_id_param_name='bill_id',
            # This should align with what post_vendor_bill_to_gl expects for the bill's ID
            success_msg_template=_("{count} bill(s) posted to GL."),
            eligibility_func=lambda bill: bill.status == VendorBill.BillStatus.APPROVED.value and not (
                        bill.related_gl_voucher_id and bill.related_gl_voucher and bill.related_gl_voucher.status == 'POSTED')
        )

    @admin.action(description=_("Void selected bills"))
    def action_void_bills(self, request: HttpRequest, queryset: models.QuerySet):
        default_reason = _("Voided via admin action by %(user)s") % {
            'user': request.user.name}  # request.user.name uses your custom User model's 'name' field.
        self._call_payables_service_single(
            request, queryset,
            service_method_name='void_vendor_bill',
            item_id_param_name='bill_id',  # This should align with what void_vendor_bill expects for the bill's ID
            success_msg_template=_("{count} bill(s) voided."),
            eligibility_func=lambda bill: bill.status != VendorBill.BillStatus.VOID.value,
            action_kwargs_func=lambda bill: {
                'void_reason': default_reason,
                'void_date': timezone.now().date()  # Uses Django's timezone
            }
        )

# =============================================================================
# VendorPaymentAllocation Inline (For VendorPaymentAdmin - Editable)
# =============================================================================
class VendorPaymentAllocationInlineForPayment(admin.TabularInline):
    model = VendorPaymentAllocation
    form = VendorPaymentAllocationInlineForm  # Use custom form for label
    fields = ('vendor_bill', 'allocated_amount', 'allocation_date')
    readonly_fields = ()
    extra = 0
    autocomplete_fields = ['vendor_bill']
    verbose_name = _("Bill Allocation")
    verbose_name_plural = _("Bill Allocations")
    fk_name = 'vendor_payment'

    @admin.display(description=_('Selected Vendor Bill Details'))
    def vendor_bill_link(self, obj: VendorPaymentAllocation) -> str:
        if obj.vendor_bill_id:
            try:
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
            except Exception as e:
                logger.error(
                    f"Error rendering vendor_bill_link for alloc {obj.pk}: {e}"); return f"Bill ID: {obj.vendor_bill_id} (Display Error)"
        return "—"

    def _get_parent_payment_context(self, request: HttpRequest) -> Optional[VendorPayment]:
        return getattr(request, '_current_parent_payment_for_alloc_inline', None)

    def _get_company_supplier_currency_for_alloc_filter(self, request: HttpRequest,
                                                        parent_payment_obj_from_context: Optional[VendorPayment]) -> \
            Tuple[Optional[Company], Optional[Party], Optional[str]]:
        log_prefix = f"[VPA_Inline_GetCSC][User:{request.user.name}][SU:{request.user.is_superuser}]"
        company_instance: Optional[Company] = None
        supplier_instance: Optional[Party] = None
        payment_currency: Optional[str] = None  # Can be None or an empty string

        # 1. Try to get from request.POST (most current for form submissions)
        if request.method == 'POST':
            logger.debug(f"{log_prefix} Method is POST. Processing POST data.")
            # Assuming main form field names are 'company', 'supplier', 'currency'
            company_pk_from_post = request.POST.get('company')
            supplier_pk_from_post = request.POST.get('supplier')
            # request.POST.get('currency') will return None if key not present, or the value (which can be empty string)
            currency_from_post = request.POST.get('currency')

            logger.debug(
                f"{log_prefix} POST values - Co_PK: {company_pk_from_post}, Supp_PK: {supplier_pk_from_post}, Curr: '{currency_from_post}'")

            if company_pk_from_post:
                try:
                    company_instance = Company.objects.get(pk=company_pk_from_post)
                    logger.debug(f"{log_prefix} Company set from POST: {company_instance.name}")
                except (Company.DoesNotExist, ValueError, TypeError) as e:
                    logger.warning(f"{log_prefix} Invalid company_pk '{company_pk_from_post}' from POST: {e}")
                    company_instance = None  # Crucial: ensure it's None if lookup fails

            if supplier_pk_from_post:
                if company_instance:  # Supplier lookup needs a valid company from POST
                    try:
                        supplier_instance = Party.objects.get(pk=supplier_pk_from_post, company=company_instance,
                                                              party_type=PartyType.SUPPLIER.value)
                        logger.debug(f"{log_prefix} Supplier set from POST: {supplier_instance.name}")
                    except (Party.DoesNotExist, ValueError, TypeError) as e:
                        logger.warning(
                            f"{log_prefix} Invalid supplier_pk '{supplier_pk_from_post}' for company '{company_instance.name}' from POST: {e}")
                        supplier_instance = None  # Crucial: ensure it's None
                else:
                    logger.warning(
                        f"{log_prefix} Cannot lookup supplier from POST PK '{supplier_pk_from_post}' as company_instance from POST is None.")
                    supplier_instance = None

            if currency_from_post is not None:  # Distinguishes from key not being in POST
                payment_currency = currency_from_post
                logger.debug(f"{log_prefix} Currency set from POST: '{payment_currency}'")

        # 2. If values are still missing (or it's a GET request), try parent object (if available)
        # This is for "change" page GETs, or if POST data was incomplete/invalid.
        if parent_payment_obj_from_context and parent_payment_obj_from_context.pk:
            logger.debug(
                f"{log_prefix} Parent obj (PK: {parent_payment_obj_from_context.pk}) available. Using for potentially missing values.")
            if not company_instance and parent_payment_obj_from_context.company_id:
                try:
                    # Attempt to use prefetched related object if available, otherwise query
                    company_instance = parent_payment_obj_from_context.company
                    if not isinstance(company_instance, Company):  # Check if it's a valid Company instance
                        company_instance = Company.objects.get(pk=parent_payment_obj_from_context.company_id)
                    logger.debug(f"{log_prefix} Company set from parent_obj: {company_instance.name}")
                except Company.DoesNotExist:
                    logger.warning(
                        f"{log_prefix} Company ID {parent_payment_obj_from_context.company_id} on parent obj not found.")
                    company_instance = None

            # Supplier from parent_obj needs a valid company_instance (either from POST or now from parent's company)
            if not supplier_instance and parent_payment_obj_from_context.supplier_id:
                if company_instance:  # Only proceed if we have a company context
                    try:
                        supplier_instance = parent_payment_obj_from_context.supplier
                        if not isinstance(supplier_instance, Party):  # Check if it's a valid Party instance
                            supplier_instance = Party.objects.get(pk=parent_payment_obj_from_context.supplier_id)

                        # Validate that this supplier actually belongs to the determined company_instance
                        if supplier_instance.company_id != company_instance.id:
                            logger.warning(
                                f"{log_prefix} Supplier {supplier_instance.name} (from parent) company_id {supplier_instance.company_id} "
                                f"does not match current company_instance_id {company_instance.id}. Invalidating supplier.")
                            supplier_instance = None
                        else:
                            logger.debug(f"{log_prefix} Supplier set from parent_obj: {supplier_instance.name}")
                    except Party.DoesNotExist:
                        logger.warning(
                            f"{log_prefix} Supplier ID {parent_payment_obj_from_context.supplier_id} on parent obj not found.")
                        supplier_instance = None
                else:
                    logger.warning(
                        f"{log_prefix} Cannot lookup supplier from parent_obj as current company_instance is None.")

            if payment_currency is None and parent_payment_obj_from_context.currency is not None:
                # Only update if not set by POST (even if POST was an empty string, payment_currency would not be None)
                payment_currency = parent_payment_obj_from_context.currency
                logger.debug(f"{log_prefix} Currency set from parent_obj: '{payment_currency}'")

        # 3. For GET requests on "add" page (no parent_obj.pk yet)
        is_add_page_get = request.method == 'GET' and not (
                    parent_payment_obj_from_context and parent_payment_obj_from_context.pk)
        if is_add_page_get:
            logger.debug(f"{log_prefix} GET request on Add page.")
            # If company is still not set AND user is not superuser, try request.company (from TenantAccountingModelAdmin)
            if not company_instance and not request.user.is_superuser:
                logger.debug(f"{log_prefix} Company not yet set. Non-SU: trying request.company.")
                # request.company should be set by your TenantAccountingModelAdmin for non-superusers
                request_company_attr = getattr(request, 'company', None)
                if isinstance(request_company_attr, Company):
                    company_instance = request_company_attr
                    logger.debug(f"{log_prefix} Company set from request.company: {company_instance.name}")
                else:
                    logger.warning(f"{log_prefix} Non-SU: request.company not available or not a Company instance.")
            elif not company_instance and request.user.is_superuser:
                logger.debug(
                    f"{log_prefix} Superuser on Add Page (GET), company not yet selected on main form (or derived from POST/parent).")
                # For a superuser on a GET request for an "add" page, company_instance will likely be None
                # until they select a company on the main form. This is expected.

        # Final consistency check: if supplier is set, it must belong to the final company_instance
        if supplier_instance and company_instance and supplier_instance.company_id != company_instance.id:
            logger.warning(
                f"{log_prefix} Final check: Supplier {supplier_instance.name} (Company ID: {supplier_instance.company_id}) "
                f"does not belong to final Company {company_instance.name} (ID: {company_instance.id}). Invalidating supplier.")
            supplier_instance = None
        elif supplier_instance and not company_instance:  # If supplier is somehow set but company is not
            logger.warning(
                f"{log_prefix} Final check: Supplier {supplier_instance.name} is set, but no valid company_instance. Invalidating supplier.")
            supplier_instance = None

        logger.info(f"{log_prefix} FINAL Context Derived - "
                    f"Company: {company_instance.name if company_instance else 'None'}, "
                    f"Supplier: {supplier_instance.name if supplier_instance else 'None'}, "
                    f"Currency: '{payment_currency if payment_currency is not None else 'None (not set)'}'")

        return company_instance, supplier_instance, payment_currency

    def get_formset(self, request: Any, obj: Optional[VendorPayment] = None, **kwargs: Any) -> Any:
        request._current_parent_payment_for_alloc_inline = obj
        logger.debug(
            f"[VPA_Inline GetFormset][User:{request.user.name}] Stored parent payment (PK: {obj.pk if obj else 'None'}) on request.")
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        parent_payment_context_obj = self._get_parent_payment_context(request)

        company_for_filtering, supplier_for_filtering, payment_currency_for_filtering = \
            self._get_company_supplier_currency_for_alloc_filter(request, parent_payment_context_obj)

        log_prefix = f"[VPA_Inline FFKey][User:{request.user.name}][Fld:'{db_field.name}']"
        logger.debug(
            f"{log_prefix} Filtering with Co: {company_for_filtering.name if company_for_filtering else 'None'}, "
            f"Supp: {supplier_for_filtering.name if supplier_for_filtering else 'None'}, "
            f"Curr: {payment_currency_for_filtering or 'None'}")

        if db_field.name == "vendor_bill":
            if company_for_filtering and supplier_for_filtering and payment_currency_for_filtering:
                kwargs["queryset"] = VendorBill.objects.filter(
                    company=company_for_filtering,
                    supplier=supplier_for_filtering,
                    currency=payment_currency_for_filtering,
                    status__in=[VendorBill.BillStatus.APPROVED.value, VendorBill.BillStatus.PARTIALLY_PAID.value]
                ).exclude(amount_due__lte=ZERO).select_related('company', 'supplier').order_by('due_date', 'issue_date')
                logger.info(f"{log_prefix} Filtered VendorBill choices. Count: {kwargs['queryset'].count()}.")
            else:
                kwargs["queryset"] = VendorBill.objects.none()
                logger.warning(f"{log_prefix} Insufficient context for VendorBill. Setting queryset to None.")
                if not (parent_payment_context_obj and parent_payment_context_obj.pk) and request.method == 'GET':
                    if not (
                            company_for_filtering and supplier_for_filtering and payment_currency_for_filtering):  # Check again if still no context
                        messages.info(request,
                                      _("Select 'Company', 'Supplier', and 'Currency' on the main Payment form to populate choices for 'Vendor Bill (Current Due)'."))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_payment_editable(self, parent_payment: Optional[VendorPayment]) -> bool:
        if parent_payment is None: return True
        return parent_payment.status != VendorPayment.PaymentStatus.VOID.value

    def has_add_permission(self, request, obj=None):
        parent_payment = self._get_parent_payment_context(request) or obj
        return super().has_add_permission(request, parent_payment) and self._is_parent_payment_editable(parent_payment)

    def has_change_permission(self, request, obj=None):
        parent_payment_context = obj.vendor_payment if isinstance(obj,
                                                                  VendorPaymentAllocation) and obj.vendor_payment_id else self._get_parent_payment_context(
            request) or (obj if isinstance(obj, VendorPayment) else None)
        return super().has_change_permission(request, obj) and self._is_parent_payment_editable(parent_payment_context)

    def has_delete_permission(self, request, obj=None):
        parent_payment_context = obj.vendor_payment if isinstance(obj,
                                                                  VendorPaymentAllocation) and obj.vendor_payment_id else self._get_parent_payment_context(
            request) or (obj if isinstance(obj, VendorPayment) else None)
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
    readonly_fields_base = ('allocated_amount', 'unallocated_amount', 'related_gl_voucher_link')
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
    'payment_amount')}),
                                 (_('Details'), {'fields': ('payment_number', 'reference_details', 'notes')}),
                                 (_('Allocation Status'), {'fields': ('allocated_amount', 'unallocated_amount')}),
                                 (_('GL Info'), {'fields': ('related_gl_voucher_link',)}),
                                 (_('Audit'), {'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'),
                                               'classes': ('collapse',)}))

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
        # This method in VendorPaymentAdmin itself determines querysets for main form fields.
        # It also uses _get_company_from_request_obj_or_form from TenantAccountingModelAdmin.
        company_context = self._get_company_from_request_obj_or_form(
            request,
            self.get_object(request,
                            request.resolver_match.kwargs.get('object_id')) if request.resolver_match.kwargs.get(
                'object_id') else None,
            request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get('object_id') else None
        )
        log_prefix_main = f"[VPAdmin FFKey][User:{request.user.name}][Fld:'{db_field.name}']"
        logger.debug(
            f"{log_prefix_main} Main form company context: {company_context.name if company_context else 'None'}")

        if db_field.name == "supplier":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(company=company_context,
                                                          party_type=CorePartyType.SUPPLIER.value,
                                                          is_active=True).order_by('name')
                logger.debug(
                    f"{log_prefix_main} Supplier queryset filtered for company: {company_context.name}. Count: {kwargs['queryset'].count()}")
            else:
                kwargs["queryset"] = Party.objects.none()
                logger.warning(f"{log_prefix_main} No company context for supplier field. Queryset set to None.")
        elif db_field.name == "payment_account":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=CoreAccountType.ASSET.value, is_active=True,
                                                            allow_direct_posting=True).order_by('account_name')
                logger.debug(
                    f"{log_prefix_main} Payment Account queryset filtered for company: {company_context.name}. Count: {kwargs['queryset'].count()}")
            else:
                kwargs["queryset"] = Account.objects.none()
                logger.warning(f"{log_prefix_main} No company context for payment_account field. Queryset set to None.")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorPayment, form, change):
        is_new = not obj.pk
        if is_new: obj.created_by = request.user
        finalizing = (change and form.initial.get(
            'status') == VendorPayment.PaymentStatus.DRAFT.value and obj.status != VendorPayment.PaymentStatus.DRAFT.value) or \
                     (is_new and obj.status != VendorPayment.PaymentStatus.DRAFT.value)
        if finalizing and (not obj.payment_number or not obj.payment_number.strip()):
            if not obj.company_id: form.add_error('company', _("Company needed for payment number.")); return
            try:
                obj.payment_number = payables_service.get_next_payment_number(obj.company, obj.payment_date)
            except SequenceGenerationError as e:
                form.add_error(None, DjangoValidationError(str(e), code='pmt_num_gen_fail')); return
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        payment_instance: VendorPayment = form.instance
        if payment_instance and payment_instance.pk: logger.debug(
            f"[VPAdmin SaveFormset] Payment {payment_instance.payment_number or payment_instance.pk} allocations changed. Model's save should handle recalc.")

    @admin.display(description=_('Pmt No.'), ordering='payment_number')
    def payment_number_display(self, obj: VendorPayment):
        return obj.payment_number or (_("Draft (PK:%(pk)s)") % {'pk': obj.pk})

    supplier_link = VendorBillAdmin.supplier_link

    @admin.display(description=_('Pmt Method'), ordering='payment_method')
    def payment_method_display(self, obj: VendorPayment):
        return obj.get_payment_method_display() if obj.payment_method else "—"

    @admin.display(description=_('Pmt Amt'), ordering='payment_amount')
    def payment_amount_display(self, obj: VendorPayment):
        return f"{obj.payment_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Unallocated'), ordering='unallocated_amount')
    def unallocated_amount_display(self, obj: VendorPayment):
        return f"{obj.unallocated_amount or ZERO:.2f} {obj.currency}"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: VendorPayment):
        color_map = {VendorPayment.PaymentStatus.DRAFT.value: "grey",
                     VendorPayment.PaymentStatus.PENDING_APPROVAL.value: "#ffc107",
                     VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value: "#28a745",
                     VendorPayment.PaymentStatus.PAID_COMPLETED.value: "#007bff",
                     VendorPayment.PaymentStatus.VOID.value: "black"}
        return format_html(
            f'<strong style="color:{color_map.get(obj.status, "black")};">{obj.get_status_display()}</strong>')

    related_gl_voucher_link = CustomerInvoiceAdmin.related_gl_voucher_link
    _call_payables_service_single = VendorBillAdmin._call_payables_service_single

    @admin.action(description=_('Approve selected DRAFT payments'))
    def admin_action_approve_payments(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset, service_method_name='approve_vendor_payment',
                                           item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) approved."),
                                           eligibility_func=lambda
                                               pmt: pmt.status == VendorPayment.PaymentStatus.DRAFT.value,
                                           action_kwargs_func=lambda pmt: {
                                               'approval_notes': _("Approved via admin bulk action.")})

    @admin.action(description=_("Post selected APPROVED payments to GL"))
    def admin_action_post_payments_to_gl(self, request: HttpRequest, queryset: models.QuerySet):
        self._call_payables_service_single(request, queryset, service_method_name='post_vendor_payment_to_gl',
                                           item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) posted to GL."),
                                           eligibility_func=lambda
                                               pmt: pmt.status == VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value and not pmt.related_gl_voucher_id)

    @admin.action(description=_("Void selected payments"))
    def action_void_payments(self, request: HttpRequest, queryset: models.QuerySet):
        default_reason = _("Voided via admin action by %(user)s") % {'user': request.user.name}
        self._call_payables_service_single(request, queryset, service_method_name='void_vendor_payment',
                                           item_id_param_name='payment_id',
                                           success_msg_template=_("{count} payment(s) voided."), eligibility_func=lambda
                pmt: pmt.status != VendorPayment.PaymentStatus.VOID.value,
                                           action_kwargs_func=lambda pmt: {'void_reason': default_reason,
                                                                           'void_date': timezone.now().date()})


@admin.register(VendorPaymentAllocation)
class VendorPaymentAllocationAdmin(TenantAccountingModelAdmin):
    list_display = (
    'id', 'vendor_payment_link', 'vendor_bill_link', 'allocated_amount_display', 'allocation_date_display')
    list_filter_non_superuser = (
    ('allocation_date', admin.DateFieldListFilter), 'vendor_payment__supplier', 'vendor_bill__supplier_bill_reference')
    search_fields = (
    'vendor_payment__payment_number', 'vendor_bill__bill_number', 'vendor_bill__supplier_bill_reference',
    'company__name')
    readonly_fields = ('company',)
    autocomplete_fields = ['vendor_payment', 'vendor_bill']
    list_select_related = (
    'company', 'vendor_payment__company', 'vendor_payment__supplier', 'vendor_bill__company', 'vendor_bill__supplier')

    vendor_payment_link = VendorPaymentAllocationInlineForBill.vendor_payment_link
    vendor_bill_link = VendorPaymentAllocationInlineForPayment.vendor_bill_link

    @admin.display(description=_("Allocated Amt"))
    def allocated_amount_display(self, obj: VendorPaymentAllocation):
        return f"{obj.allocated_amount or ZERO:.2f}"

    @admin.display(description=_("Alloc. Date"))
    def allocation_date_display(self, obj: VendorPaymentAllocation):
        return obj.allocation_date.strftime('%Y-%m-%d') if obj.allocation_date else ""

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        # For standalone admin, context fetching is slightly different
        # (This logic seems mostly okay, but ensure TenantAccountingModelAdmin sets request.company for non-SU correctly for this admin too)
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request,
                                                                                              request.resolver_match.kwargs.get(
                                                                                                  'object_id')) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)

        if db_field.name == "vendor_payment":
            if company_context:
                kwargs["queryset"] = VendorPayment.objects.filter(company=company_context, status__in=[
                    VendorPayment.PaymentStatus.PAID_COMPLETED.value,
                    VendorPayment.PaymentStatus.APPROVED_FOR_PAYMENT.value]).exclude(unallocated_amount__lte=ZERO)
            else:
                kwargs["queryset"] = VendorPayment.objects.none()
        elif db_field.name == "vendor_bill":
            # In standalone, we need to be more careful about how we get supplier/currency
            # as the "parent" payment isn't directly on this form in the same way as an inline.
            # We might need to make `vendor_payment` a required selection first, then filter `vendor_bill`.
            # Or, if `vendor_payment` is already selected (e.g., in POST or existing instance), use its context.
            selected_payment_id = None
            if request.method == 'POST':
                selected_payment_id = request.POST.get('vendor_payment')
            elif request.resolver_match.kwargs.get('object_id'):
                instance = self.get_object(request, request.resolver_match.kwargs.get('object_id'))
                if instance: selected_payment_id = instance.vendor_payment_id

            if company_context and selected_payment_id:
                try:
                    selected_payment = VendorPayment.objects.get(pk=selected_payment_id, company=company_context)
                    kwargs["queryset"] = VendorBill.objects.filter(
                        company=company_context,
                        supplier=selected_payment.supplier,
                        currency=selected_payment.currency,
                        status__in=[VendorBill.BillStatus.APPROVED.value, VendorBill.BillStatus.PARTIALLY_PAID.value]
                    ).exclude(amount_due__lte=ZERO)
                except VendorPayment.DoesNotExist:
                    kwargs["queryset"] = VendorBill.objects.none()
            elif company_context:  # Show bills for the company if no payment is selected yet (less ideal)
                kwargs["queryset"] = VendorBill.objects.filter(company=company_context,
                                                               status__in=[VendorBill.BillStatus.APPROVED.value,
                                                                           VendorBill.BillStatus.PARTIALLY_PAID.value]).exclude(
                    amount_due__lte=ZERO)
            else:
                kwargs["queryset"] = VendorBill.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: VendorPaymentAllocation, form, change):
        if obj.vendor_payment and obj.vendor_payment.company_id: obj.company = obj.vendor_payment.company
        super().save_model(request, obj, form, change)