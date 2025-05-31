# crp_accounting/admin/journal.py

import logging
from decimal import Decimal
from typing import Optional, Any

from django.contrib import admin, messages
from django.db import models
from django.http import HttpRequest
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied
from django.conf import settings
from django.forms import ModelForm

# --- Base Admin Class Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model Imports ---
from ..models.journal import Voucher, VoucherLine, VoucherSequence, VoucherApproval
from ..models.period import AccountingPeriod, FiscalYear  # For choices and status checks
from ..models.coa import Account
from ..models.party import Party
from company.models import Company  # For type hinting and direct use

# --- Enum Imports & Constants ---
from ..models.journal import TransactionStatus, ApprovalActionType, VoucherType, DrCrType

# Define these based on your FiscalYear.status field string values or enum values
FISCAL_YEAR_STATUS_OPEN = "Open"
FISCAL_YEAR_STATUS_LOCKED = "Locked"

# --- Service Imports ---
from ..services import voucher_service

# --- Custom Exceptions ---
from ..exceptions import (
    VoucherWorkflowError, InvalidVoucherStatusError, PeriodLockedError,
    BalanceError
)

logger = logging.getLogger("crp_accounting.admin.journal")


# =============================================================================
# VoucherSequence Admin
# =============================================================================
@admin.register(VoucherSequence)
class VoucherSequenceAdmin(TenantAccountingModelAdmin):
    # Base class adds 'get_record_company_display' for SUs.
    list_display = ('__str__', 'prefix', 'last_number', 'padding_digits', 'updated_at')
    list_filter_non_superuser = ('voucher_type', ('accounting_period__fiscal_year', admin.RelatedOnlyFieldListFilter))
    search_fields = ('prefix', 'voucher_type', 'company__name', 'accounting_period__name')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('company', 'accounting_period', 'accounting_period__fiscal_year')
    ordering = ('company__name', 'accounting_period__start_date', 'voucher_type')
    fieldsets = (
        (None, {'fields': ('company', 'voucher_type', 'accounting_period')}),
        (_('Sequence Format'), {'fields': ('prefix', 'padding_digits', 'last_number')}),
        (_('Audit Information'), {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('company',) + self.list_filter_non_superuser
        return self.list_filter_non_superuser


# =============================================================================
# VoucherApproval Inline & Admin
# =============================================================================
class VoucherApprovalInline(admin.StackedInline):
    model = VoucherApproval
    fields = ('action_timestamp', 'user_display', 'action_type_display', 'from_status_display', 'to_status_display',
              'comments_short')
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False
    ordering = ('-action_timestamp',)
    verbose_name = _("Workflow Log Entry")
    verbose_name_plural = _("Workflow Log")
    classes = ['collapse']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('user', 'company')

    @admin.display(description=_('User'))
    def user_display(self, obj: VoucherApproval):
        return obj.user.get_full_name() or obj.user.get_name() if obj.user else _("System")

    @admin.display(description=_('Action'))
    def action_type_display(self, obj: VoucherApproval): return obj.get_action_type_display()

    @admin.display(description=_('From Status'))
    def from_status_display(self, obj: VoucherApproval):
        return obj.get_from_status_display() if obj.from_status else "N/A"

    @admin.display(description=_('To Status'))
    def to_status_display(self, obj: VoucherApproval):
        return obj.get_to_status_display() if obj.to_status else "N/A"

    @admin.display(description=_('Comments'))
    def comments_short(self, obj: VoucherApproval):
        return (obj.comments[:50] + '...') if obj.comments and len(obj.comments) > 50 else obj.comments or "—"


@admin.register(VoucherApproval)
class VoucherApprovalAdmin(TenantAccountingModelAdmin):
    # Base class adds 'get_record_company_display' for SUs.
    list_display = (
        'voucher_link', 'user_display', 'action_timestamp', 'action_type_display',
        'from_status_display', 'to_status_display', 'comments_short'
    )
    list_filter_non_superuser = (
        'action_type', ('user', admin.RelatedOnlyFieldListFilter), ('action_timestamp', admin.DateFieldListFilter)
    )
    search_fields = ('voucher__voucher_number', 'voucher__company__name', 'user__name', 'user__email', 'comments')
    readonly_fields = [f.name for f in VoucherApproval._meta.fields if f.name not in ('id',)]
    date_hierarchy = 'action_timestamp'
    list_select_related = ('voucher__company', 'user', 'company')
    list_per_page = 50
    ordering = ('-action_timestamp',)

    user_display = VoucherApprovalInline.user_display
    action_type_display = VoucherApprovalInline.action_type_display
    from_status_display = VoucherApprovalInline.from_status_display
    to_status_display = VoucherApprovalInline.to_status_display
    comments_short = VoucherApprovalInline.comments_short

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('company', 'voucher__company') + self.list_filter_non_superuser
        return self.list_filter_non_superuser

    @admin.display(description=_('Voucher'), ordering='voucher__voucher_number')
    def voucher_link(self, obj: VoucherApproval):
        if obj.voucher:
            try:
                link = reverse("admin:crp_accounting_voucher_change", args=[obj.voucher.pk])
                return format_html('<a href="{}" target="_blank">{}</a>', link,
                                   obj.voucher.voucher_number or f"Voucher ID:{obj.voucher.pk}")
            except NoReverseMatch:
                return obj.voucher.voucher_number or f"Voucher ID:{obj.voucher.pk} (Link Error)"
        return "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# =============================================================================
# VoucherLine Inline Admin (Refined for Robust Company Context)
# =============================================================================
class VoucherLineInline(admin.TabularInline):
    model = VoucherLine
    # form = VoucherLineInlineForm # Use this if you define a custom form (see advanced solution)
    fields = ('account', 'dr_cr', 'amount', 'narration')
    extra = 1
    autocomplete_fields = ['account'] # This triggers AJAX lookups
    verbose_name = _("Voucher Line")
    verbose_name_plural = _("Voucher Lines")
    classes = ['collapse'] if not settings.DEBUG else [] # settings.DEBUG for easy dev

    def _get_company_context_for_inline(self, request: HttpRequest, parent_voucher_from_formset: Optional[Voucher]) -> Optional[Company]:
        """
        Determines the Company context for filtering Account choices in the inline.
        Handles:
        1. Editing an existing Voucher (parent_voucher_from_formset is the Voucher).
        2. Adding a new Voucher (parent_voucher_from_formset is None).
           - During POST: from the 'company' field selected on the main Voucher form.
           - During GET: from request.company (middleware) if available.
        3. Autocomplete AJAX requests (attempts to infer from URL if possible).
        """
        log_prefix = f"VLI_CoCtx (User:{request.user.name}):"

        # --- Scenario 1: Editing an existing Voucher (parent_voucher_from_formset is populated) ---
        if parent_voucher_from_formset and parent_voucher_from_formset.pk: # Ensure it's a saved voucher
            if parent_voucher_from_formset.company_id:
                # Prefer preloaded company object if available (from select_related on parent admin)
                if hasattr(parent_voucher_from_formset, 'company') and parent_voucher_from_formset.company:
                    logger.debug(f"{log_prefix} Using parent_voucher_from_formset.company: {parent_voucher_from_formset.company.name} (PK:{parent_voucher_from_formset.company.pk})")
                    return parent_voucher_from_formset.company
                try:
                    # Fallback to fetching by ID if company object not loaded
                    company_instance = Company.objects.get(pk=parent_voucher_from_formset.company_id)
                    logger.debug(f"{log_prefix} Fetched company from parent_voucher_from_formset.company_id: {company_instance.name} (PK:{company_instance.pk})")
                    return company_instance
                except Company.DoesNotExist:
                    logger.error(f"{log_prefix} Parent Voucher {parent_voucher_from_formset.pk} has invalid company_id {parent_voucher_from_formset.company_id}. Critical data issue.")
                    return None # Cannot determine company
            else:
                logger.error(f"{log_prefix} Parent Voucher {parent_voucher_from_formset.pk} is missing company_id. Critical data issue.")
                return None

        # --- Scenario 2: Adding a new Voucher (parent_voucher_from_formset is None) ---
        # Or if parent_voucher_from_formset was not a saved instance
        if not (parent_voucher_from_formset and parent_voucher_from_formset.pk):
            logger.debug(f"{log_prefix} In 'Add New Voucher' or unsaved parent context.")
            # During POST of the "Add Voucher" form:
            if request.method == 'POST':
                company_pk_from_post = request.POST.get('company') # Name of company field on Voucher form
                if company_pk_from_post:
                    try:
                        company_instance = Company.objects.get(pk=company_pk_from_post)
                        logger.debug(f"{log_prefix} Using company from main form POST data: {company_instance.name} (PK:{company_instance.pk})")
                        return company_instance
                    except (Company.DoesNotExist, ValueError, TypeError): # Added TypeError
                        logger.warning(f"{log_prefix} Invalid company PK '{company_pk_from_post}' from main form POST data.")
                        # Fall through if POST data is invalid, try request.company
                else:
                    logger.debug(f"{log_prefix} No 'company' field in POST data for new voucher.")


            # Fallback for GET requests (initial "Add Voucher" page) or if POST didn't yield company:
            # Use request.company (set by middleware, primarily for non-SUs or SU "acting-as")
            request_company = getattr(request, 'company', None)
            if isinstance(request_company, Company):
                logger.debug(f"{log_prefix} Using company from request.company (middleware): {request_company.name} (PK:{request_company.pk})")
                return request_company
            else:
                logger.debug(f"{log_prefix} request.company is not a valid Company instance (Value: {request_company}).")

        # --- Scenario 3: Attempt to infer for Autocomplete AJAX requests (more heuristic) ---
        # This is harder because autocomplete requests don't always have full parent context easily.
        # Django's default autocomplete URLs might include the main object's PK in `request.resolver_match.kwargs['object_id']`
        # if the autocomplete is on a *change form*.
        parent_pk_from_url_for_autocomplete = request.resolver_match.kwargs.get('object_id')
        is_autocomplete_url_context = 'admin/autocomplete' in request.path and 'term' in request.GET

        if is_autocomplete_url_context and parent_pk_from_url_for_autocomplete:
            try:
                # This assumes object_id in the URL is the parent Voucher PK
                parent_v = Voucher.objects.select_related('company').get(pk=parent_pk_from_url_for_autocomplete)
                if parent_v.company:
                    logger.debug(f"{log_prefix} Autocomplete context: Inferred company from URL object_id {parent_pk_from_url_for_autocomplete}: {parent_v.company.name}")
                    return parent_v.company
            except (Voucher.DoesNotExist, ValueError, TypeError):
                logger.warning(f"{log_prefix} Autocomplete context: Could not find/use parent voucher from URL object_id {parent_pk_from_url_for_autocomplete}")

        logger.warning(f"{log_prefix} Could not determine company context. Parent from formset PK: {parent_voucher_from_formset.pk if parent_voucher_from_formset else 'None'}. POST 'company' field: {request.POST.get('company') if request.method == 'POST' else 'N/A (not POST)'}. request.company: {getattr(request, 'company', 'Not Set')}")
        return None

    def get_formset(self, request: Any, obj: Optional[Voucher] = None, **kwargs: Any) -> Any:
        """
        Passes the parent Voucher instance (obj) to formfield_for_foreignkey via the request.
        """
        # This attribute is used by formfield_for_foreignkey to get the parent voucher.
        request._current_parent_voucher_for_inline = obj
        logger.debug(f"VLI GetFormset: Storing parent voucher (PK: {obj.pk if obj else 'None'}) on request for user {request.user.name}")
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Filters ForeignKey choices, especially 'account', based on the determined company context.
        This method is called for rendering the form AND for providing choices to the autocomplete widget.
        """
        parent_voucher_from_formset_context = getattr(request, '_current_parent_voucher_for_inline', None)

        # Determine the company context using the refined helper
        company_context = self._get_company_context_for_inline(request, parent_voucher_from_formset_context)

        log_prefix_ffk = f"VLI_FFK (User:{request.user.name}, Field:'{db_field.name}'):"
        logger.debug(f"{log_prefix_ffk} Parent Voucher (from formset ctx): {parent_voucher_from_formset_context.pk if parent_voucher_from_formset_context else 'None'}. "
                     f"Determined Company Context: {company_context.name if company_context else 'None'}")

        if db_field.name == "account":
            if company_context:
                # This queryset will be used by the form field and the autocomplete widget.
                account_queryset = Account.objects.filter(
                    company=company_context,
                    is_active=True,
                    allow_direct_posting=True
                ).select_related('company', 'account_group').order_by('account_group__name', 'account_number')
                kwargs["queryset"] = account_queryset
                logger.info(f"{log_prefix_ffk} Filtered Account choices for Company '{company_context.name}'. Queryset count: {account_queryset.count()}.")
            else:
                kwargs["queryset"] = Account.objects.none() # No choices if no company context
                logger.warning(f"{log_prefix_ffk} No company context determined. Setting Account queryset to None.")
                # Display message only on the "add" page for superusers if no company is selected on the main form
                is_add_view = not (parent_voucher_from_formset_context and parent_voucher_from_formset_context.pk)
                if is_add_view and request.user.is_superuser:
                    # Check if company field on main form is empty
                    # This message appears if the company_context is None during initial GET of "add" page
                    if not (request.method == 'POST' and request.POST.get('company')):
                         messages.info(request, _("Select 'Company' on the main Voucher form to populate Account choices for lines."))
                elif not is_add_view: # Editing existing, but company_context is None - this is an issue
                    messages.error(request, _("System Error: Could not determine the company for this voucher to filter line accounts. Please check voucher data."))

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _is_parent_voucher_editable(self, parent_voucher: Optional[Voucher]) -> bool:
        if parent_voucher is None: return True  # Adding new voucher, lines are editable
        if hasattr(parent_voucher, 'is_editable'): # Assumes Voucher model has an is_editable property
            return parent_voucher.is_editable
        # Fallback: if no is_editable property, assume lines for existing vouchers are editable
        # unless explicitly restricted by status, etc. (This part might need more business logic)
        logger.warning(f"Parent voucher {parent_voucher.pk} missing 'is_editable' property; defaulting editability check.")
        return parent_voucher.status in [TransactionStatus.DRAFT.value, TransactionStatus.REJECTED.value] # Example


    # --- Permission handling for inline actions ---
    # These now rely on _is_parent_voucher_editable and parent voucher passed via request attribute
    def has_add_permission(self, request, obj=None): # obj is the parent Voucher
        parent_voucher = getattr(request, '_current_parent_voucher_for_inline', obj)
        return super().has_add_permission(request, parent_voucher) and self._is_parent_voucher_editable(parent_voucher)

    def has_change_permission(self, request, obj=None): # obj here is VoucherLine instance if called for existing line
        # For an existing line, obj is VoucherLine. Its parent is obj.voucher.
        # For the formset as a whole (e.g. can any lines be changed?), obj is the parent Voucher.
        parent_voucher_context = obj.voucher if isinstance(obj, VoucherLine) and obj.voucher else \
                                 getattr(request, '_current_parent_voucher_for_inline', obj if isinstance(obj, Voucher) else None)
        return super().has_change_permission(request, obj) and self._is_parent_voucher_editable(parent_voucher_context)

    def has_delete_permission(self, request, obj=None):
        parent_voucher_context = obj.voucher if isinstance(obj, VoucherLine) and obj.voucher else \
                                 getattr(request, '_current_parent_voucher_for_inline', obj if isinstance(obj, Voucher) else None)
        return super().has_delete_permission(request, obj) and self._is_parent_voucher_editable(parent_voucher_context)
# =============================================================================
# Voucher Admin
# =============================================================================
@admin.register(Voucher)
class VoucherAdmin(TenantAccountingModelAdmin):
    # Base class adds 'get_record_company_display' for SUs.
    list_display = (
        'voucher_number_display', 'date', 'voucher_type_display', 'narration_short',
        'party_display', 'total_debit_display', 'total_credit_display', 'is_balanced_display',
        'status_colored', 'created_at_short',
    )
    list_filter_non_superuser = (
        'status', 'voucher_type',
        ('accounting_period__fiscal_year', admin.RelatedOnlyFieldListFilter),
        ('date', admin.DateFieldListFilter), ('party', admin.RelatedOnlyFieldListFilter)
    )
    search_fields = ('voucher_number', 'narration', 'reference', 'party__name', 'company__name')
    readonly_fields_base = (
        'voucher_number', 'status', 'is_balanced_display', 'created_by', 'created_at', 'updated_at',
        'approved_by', 'approved_at', 'posted_by', 'posted_at', 'is_reversal_for', 'is_reversed'
    )
    list_select_related = ('company', 'party', 'accounting_period', 'accounting_period__fiscal_year', 'created_by')
    autocomplete_fields = ['company', 'party', 'accounting_period', 'created_by', 'approved_by', 'posted_by',
                           'is_reversal_for']
    inlines = [VoucherLineInline, VoucherApprovalInline]
    actions = ['admin_action_submit_vouchers', 'admin_action_approve_and_post_vouchers', 'admin_action_reject_vouchers']
    ordering = ('-date', '-created_at')
    date_hierarchy = 'date'

    add_fieldsets = (
        (None, {'fields': ('company', 'date', 'voucher_type', 'accounting_period', 'party', 'reference', 'narration')}),
        (_('Reversal Information (Optional)'), {'fields': ('is_reversal_for',), 'classes': ('collapse',)}),
    )
    change_fieldsets_editable_voucher = (
        (None, {'fields': ('company', 'date', 'voucher_type', 'accounting_period', 'party', 'reference', 'narration')}),
        (_('Reversal Information'), {'fields': ('is_reversal_for', 'is_reversed'), 'classes': ('collapse',)}),
        (_('Status & Audit (Read-Only)'), {
            'fields': ('voucher_number', 'status', 'is_balanced_display',
                       'created_by', 'created_at', 'updated_at',
                       'approved_by', 'approved_at', 'posted_by', 'posted_at'), 'classes': ('collapse',)
        }),
    )
    change_fieldsets_non_editable_voucher = (
        (None, {'fields': ('company', 'date', 'voucher_type', 'accounting_period', 'party', 'reference', 'narration')}),
        (_('Reversal Information'), {'fields': ('is_reversal_for', 'is_reversed'), 'classes': ('collapse',)}),
        (_('Status & Audit (Read-Only)'), {
            'fields': ('voucher_number', 'status', 'is_balanced_display',
                       'created_by', 'created_at', 'updated_at',
                       'approved_by', 'approved_at', 'posted_by', 'posted_at'), 'classes': ('collapse',)
        }),
    )

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('company',) + self.list_filter_non_superuser
        return self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        if obj is None: return self.add_fieldsets
        if hasattr(obj, 'is_editable') and obj.is_editable: return self.change_fieldsets_editable_voucher
        return self.change_fieldsets_non_editable_voucher

    def get_readonly_fields(self, request, obj=None):
        ro_fields = set(super().get_readonly_fields(request, obj) or [])
        ro_fields.update(self.readonly_fields_base)
        if obj and hasattr(obj, 'is_editable') and not obj.is_editable:
            ro_fields.update(['date', 'voucher_type', 'accounting_period', 'party', 'reference', 'narration'])
        if obj and obj.is_reversal_for_id: ro_fields.add('is_reversal_for')
        ro_fields.add('is_reversed')
        return tuple(ro_fields)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Uses self._get_company_from_request_obj_or_form from base admin
        parent_obj_instance = None
        object_id_str = request.resolver_match.kwargs.get('object_id')
        if object_id_str:
            try:
                parent_obj_instance = self.get_object(request, object_id_str)
            except (self.model.DoesNotExist, DjangoValidationError):
                pass

        form_data_for_context = request.POST if not parent_obj_instance and request.method == 'POST' and request.POST else None
        company_context = self._get_company_from_request_obj_or_form(request, parent_obj_instance,
                                                                     form_data_for_context)

        if db_field.name == "accounting_period":
            if company_context:
                kwargs["queryset"] = AccountingPeriod.objects.filter(
                    company=company_context, locked=False,
                    fiscal_year__status__in=[FISCAL_YEAR_STATUS_OPEN, FISCAL_YEAR_STATUS_LOCKED]
                ).select_related('fiscal_year').order_by('-fiscal_year__start_date', '-start_date')
            else:
                kwargs["queryset"] = AccountingPeriod.objects.none()
                if not request.resolver_match.kwargs.get('object_id') and request.user.is_superuser:
                    messages.info(request, _("Select 'Company' first to populate Accounting Period choices."))
        elif db_field.name == "party":
            if company_context:
                kwargs["queryset"] = Party.objects.filter(company=company_context, is_active=True).order_by('name')
            else:
                kwargs["queryset"] = Party.objects.none()
        elif db_field.name == "is_reversal_for":
            if company_context:
                current_pk = request.resolver_match.kwargs.get('object_id')
                qs = Voucher.objects.filter(company=company_context, status=TransactionStatus.POSTED.value,
                                            is_reversed=False).order_by('-date')
                if current_pk: qs = qs.exclude(pk=current_pk)
                kwargs["queryset"] = qs
            else:
                kwargs["queryset"] = Voucher.objects.none()

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: Voucher, form, change):
        try:
            # Base admin handles company, created/updated_by, and full_clean
            super().save_model(request, obj, form, change)
        except DjangoValidationError as e:
            form._update_errors(e)  # Show model validation errors on the form
        except Exception as e:
            logger.exception(f"Admin: Error saving Voucher {obj.pk or 'NEW'}")
            messages.error(request, _("An unexpected error occurred: %(error)s") % {'error': str(e)})

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        voucher_instance = form.instance
        if voucher_instance and voucher_instance.pk and hasattr(voucher_instance,
                                                                'is_editable') and voucher_instance.is_editable:
            try:
                voucher_service.validate_voucher_balance(voucher_instance)
            except BalanceError as be:
                messages.warning(request, _("Voucher '%(num)s' lines are imbalanced: %(message)s") % {
                    'num': self.voucher_number_display(voucher_instance), 'message': be.message})
            except Exception as e:
                logger.error(f"Admin: Error re-validating balance for Vch {voucher_instance.pk} post-formset: {e}")

    # --- Display Helpers ---
    @admin.display(description=_('Voucher No.'), ordering='voucher_number')
    def voucher_number_display(self, obj: Voucher):
        return obj.voucher_number or f"Draft (PK:{obj.pk})"

    @admin.display(description=_('Type'), ordering='voucher_type')
    def voucher_type_display(self, obj: Voucher):
        return obj.get_voucher_type_display()

    @admin.display(description=_('Narration'))
    def narration_short(self, obj: Voucher):
        return (obj.narration[:60] + '...') if obj.narration and len(obj.narration) > 60 else obj.narration or "—"

    @admin.display(description=_('Party'), ordering='party__name')
    def party_display(self, obj: Voucher):
        return obj.party.name if obj.party else "—"

    @admin.display(description=_('Debit'))
    def total_debit_display(self, obj: Voucher):
        return obj.total_debit

    @admin.display(description=_('Credit'))
    def total_credit_display(self, obj: Voucher):
        return obj.total_credit

    @admin.display(description=_('Balanced?'), boolean=True)
    def is_balanced_display(self, obj: Voucher):
        return obj.is_balanced

    @admin.display(description=_('Created At'), ordering='created_at')
    def created_at_short(self, obj: Voucher):
        return obj.created_at.strftime('%Y-%m-%d %H:%M') if obj.created_at else "N/A"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored(self, obj: Voucher):
        color_map = {
            TransactionStatus.DRAFT.value: "grey", TransactionStatus.PENDING_APPROVAL.value: "orange",
            TransactionStatus.POSTED.value: "green", TransactionStatus.REJECTED.value: "red",
            TransactionStatus.CANCELLED.value: "black", }
        color = color_map.get(obj.status, "blue")
        return format_html(f'<strong style="color:{color};">{obj.get_status_display()}</strong>')

    # --- Admin Actions (Corrected Calls) ---
    def _call_voucher_service_action(
            self, request, queryset, service_method_name: str,
            user_param_name: str,  # Specific user parameter name for the service function
            success_msg_template: str,
            action_params=None
    ):
        if action_params is None: action_params = {}
        updated_count, error_count = 0, 0
        processed_vouchers_info = []

        for voucher in queryset:
            try:
                if not voucher.company_id:
                    messages.error(request, _("Voucher '%(num)s' is missing company association. Cannot process.") % {
                        'num': self.voucher_number_display(voucher)})
                    error_count += 1;
                    continue

                service_method = getattr(voucher_service, service_method_name)

                # Prepare arguments for the service call, including the correctly named user parameter
                service_call_kwargs = {
                    'company_id': voucher.company_id,
                    'voucher_id': voucher.pk,
                    user_param_name: request.user,  # Pass request.user to the specific user parameter
                    **action_params
                }

                service_method(**service_call_kwargs)  # Call using keyword arguments

                updated_count += 1
                processed_vouchers_info.append(
                    f"{self.voucher_number_display(voucher)} ({voucher.company.name if voucher.company else 'N/A'})")
            except (DjangoValidationError, PermissionDenied, VoucherWorkflowError, BalanceError, PeriodLockedError,
                    InvalidVoucherStatusError) as e:
                msg = e.messages_joined if hasattr(e, 'messages_joined') else str(e)
                messages.error(request,
                               _("Voucher '%(num)s': %(error)s") % {'num': self.voucher_number_display(voucher),
                                                                    'error': msg});
                error_count += 1
            except TypeError as te:
                logger.exception(
                    f"Admin Action TypeError on '{service_method_name}' for Vch {voucher.pk} (Co {voucher.company_id}): {te}. Args: {service_call_kwargs}")
                messages.error(request,
                               _("Programming error calling service for voucher '%(num)s'. Check service function signature. Error: %(type_err)s") % {
                                   'num': self.voucher_number_display(voucher), 'type_err': str(te)});
                error_count += 1
            except Exception as e:
                logger.exception(
                    f"Admin Action '{service_method_name}' failed for Vch {voucher.pk}, Co {voucher.company_id}")
                messages.error(request, _("Unexpected error on voucher '%(num)s': %(error)s") % {
                    'num': self.voucher_number_display(voucher), 'error': str(e)});
                error_count += 1

        # ... (messaging logic as before) ...
        if updated_count > 0: self.message_user(request, success_msg_template % {'count': updated_count,
                                                                                 'details': ", ".join(
                                                                                     processed_vouchers_info)},
                                                messages.SUCCESS if not error_count else messages.WARNING)
        if error_count > 0 and updated_count == 0:
            self.message_user(request, _("No vouchers were processed due to errors."), messages.ERROR)
        elif error_count > 0:
            self.message_user(request,
                              _("Action partially completed with %(errors)d error(s).") % {'errors': error_count},
                              messages.WARNING)

    @admin.action(description=_('Submit selected DRAFT vouchers for approval'))
    def admin_action_submit_vouchers(self, request, queryset):
        eligible_qs = queryset.filter(status=TransactionStatus.DRAFT.value)
        if not eligible_qs.exists(): self.message_user(request, _("No DRAFT vouchers selected."), messages.INFO); return
        self._call_voucher_service_action(
            request, eligible_qs,
            service_method_name='submit_voucher_for_approval',
            user_param_name='submitted_by_user',  # Correct parameter name
            success_msg_template=_("%(count)d voucher(s) submitted: %(details)s")
        )

    @admin.action(description=_('Approve & POST selected PENDING/REJECTED vouchers'))
    def admin_action_approve_and_post_vouchers(self, request, queryset):
        eligible_qs = queryset.filter(
            status__in=[TransactionStatus.PENDING_APPROVAL.value, TransactionStatus.REJECTED.value])
        if not eligible_qs.exists(): self.message_user(request, _("No PENDING or REJECTED vouchers selected."),
                                                       messages.INFO); return
        self._call_voucher_service_action(
            request, eligible_qs,
            service_method_name='approve_and_post_voucher',
            user_param_name='approver_user',  # Correct parameter name
            success_msg_template=_("%(count)d voucher(s) approved and posted: %(details)s"),
            action_params={'comments': _("Approved via admin action.")}
        )

    @admin.action(description=_('Reject selected PENDING vouchers'))
    def admin_action_reject_vouchers(self, request, queryset):
        eligible_qs = queryset.filter(status=TransactionStatus.PENDING_APPROVAL.value)
        if not eligible_qs.exists(): self.message_user(request, _("No PENDING vouchers selected."),
                                                       messages.INFO); return
        self._call_voucher_service_action(
            request, eligible_qs,
            service_method_name='reject_voucher',
            user_param_name='rejecting_user',  # Correct parameter name
            success_msg_template=_("%(count)d voucher(s) rejected: %(details)s"),
            action_params={
                'comments': _("Rejected via admin bulk action by %(user)s.") % {'user': request.user.name}}
        )


# =============================================================================
# VoucherLine Admin (Standalone - For Superuser/Debugging)
# =============================================================================
@admin.register(VoucherLine)
class VoucherLineAdmin(TenantAccountingModelAdmin):
    # Base class 'dynamic_company_display_field_name' handles company column for SUs.
    # Do NOT add 'company' or 'voucher__company' directly to list_display here.
    list_display = (
        'id',
        'voucher_link_display',
        'account_display',
        'dr_cr_display',
        'amount',
        'line_narration_short',
        'created_at_display'  # Use display method for consistent formatting
    )
    search_fields = (
        'voucher__voucher_number',
        'voucher__company__name',  # For SU to search by parent voucher's company
        'account__account_name',
        'account__account_number',
        'narration'
    )
    list_filter_non_superuser = (
        ('voucher__status', admin.ChoicesFieldListFilter),  # Filter by parent voucher's status
        'account__account_type',  # Filter by the line's account type
        ('voucher__date', admin.DateFieldListFilter)  # Filter by parent voucher's date
    )
    raw_id_fields = ('voucher', 'account')  # Good for performance with many vouchers/accounts
    list_select_related = (
        'voucher__company',  # <<< CRUCIAL: For filtering queryset by company AND for SU company display
        'voucher',  # For voucher_link_display and other voucher fields
        'account',  # For account_display
        'account__company',
    # Optional: If you ever need to display/check account's own company (should match voucher's)
        # 'created_by', 'updated_by' # If these fields are on VoucherLine and needed for display/filter
    )
    date_hierarchy = 'voucher__date'  # Use parent voucher's date for navigation
    ordering = ('-voucher__date', '-voucher__pk', 'pk')  # Order by voucher date, then voucher, then line
    list_per_page = 50

    # --- Crucial: get_queryset for Tenant Scoping ---
    def get_queryset(self, request: HttpRequest) -> models.QuerySet:
        """
        Filters the queryset to show:
        - All VoucherLines for superusers (respecting any list_select_related optimizations).
        - Only VoucherLines belonging to vouchers of the non-superuser's current company.
        """
        # Start with the base queryset, which already includes list_select_related.
        # super().get_queryset(request) from ModelAdmin will give all objects.
        # TenantAccountingModelAdmin.get_queryset() MIGHT do some initial filtering IF VoucherLine
        # had a direct 'company' field and was configured for it, but it doesn't.
        # So, we start fresh from the base ModelAdmin's perspective.
        qs = super(TenantAccountingModelAdmin, self).get_queryset(request)  # Call ModelAdmin's get_queryset

        if not request.user.is_superuser:
            # Determine the company for the non-superuser using the helper from TenantAccountingModelAdmin
            # obj=None and form_data=None because we are in a list view context, not editing/adding.
            company_context = self._get_company_from_request_obj_or_form(request, obj=None,
                                                                         form_data_for_add_view_post=None)

            if company_context:
                # Filter lines based on the company of their parent voucher
                logger.debug(
                    f"[VoucherLineAdmin QS] Non-SU '{request.user.name}' viewing lines for Company '{company_context.name}' (PK: {company_context.pk}).")
                return qs.filter(voucher__company=company_context)
            else:
                # Non-superuser has no associated company, should see nothing.
                logger.warning(
                    f"[VoucherLineAdmin QS] Non-SU '{request.user.name}' has NO company context. Returning empty queryset for VoucherLines.")
                return qs.none()

        # Superuser sees all lines. The list_select_related for voucher__company is still useful
        # for the dynamic company display column added by TenantAccountingModelAdmin.
        logger.debug(f"[VoucherLineAdmin QS] Superuser '{request.user.name}' viewing all VoucherLines.")
        return qs

    def get_list_filter(self, request):
        """Adds company filters for superusers."""
        if request.user.is_superuser:
            # Allow SU to filter by the parent voucher's company
            # And optionally by the line's account's company (though they should always match voucher's company)
            return (
                ('voucher__company', admin.RelatedOnlyFieldListFilter),
                # ('account__company', admin.RelatedOnlyFieldListFilter), # Usually redundant if data is consistent
            ) + self.list_filter_non_superuser
        return self.list_filter_non_superuser

    # --- Permissions: Control direct manipulation of VoucherLines ---
    def has_add_permission(self, request: HttpRequest) -> bool:
        """VoucherLines are typically added via the VoucherInline, not directly."""
        return False  # Or set to `request.user.is_superuser` for SU debugging

    def has_change_permission(self, request: HttpRequest, obj: Optional[VoucherLine] = None) -> bool:
        """
        Allow SUs to change any line. Non-SUs typically shouldn't change standalone lines,
        as changes should happen via the parent Voucher (and its status).
        """
        if not super().has_change_permission(request, obj):  # Django's base permission check
            return False
        if request.user.is_superuser:
            return True
        # If you want to allow non-SUs to change lines of DRAFT vouchers (for THEIR company):
        # if obj and obj.voucher_id:
        #     user_company_context = self._get_company_from_request_obj_or_form(request)
        #     if user_company_context and obj.voucher.company_id == user_company_context.id:
        #         return obj.voucher.status == TransactionStatus.DRAFT.value
        return False  # Default: Non-SUs cannot change standalone lines

    def has_delete_permission(self, request: HttpRequest, obj: Optional[VoucherLine] = None) -> bool:
        """Similar logic to has_change_permission."""
        if not super().has_delete_permission(request, obj):
            return False
        if request.user.is_superuser:
            return True
        # if obj and obj.voucher_id:
        #     user_company_context = self._get_company_from_request_obj_or_form(request)
        #     if user_company_context and obj.voucher.company_id == user_company_context.id:
        #         return obj.voucher.status == TransactionStatus.DRAFT.value
        return False

    # --- Display Methods ---
    @admin.display(description=_('Voucher'), ordering='voucher__voucher_number')
    def voucher_link_display(self, obj: VoucherLine) -> str:
        """Displays a clickable link to the parent Voucher's admin change page."""
        # obj.voucher should be preloaded via list_select_related
        if obj.voucher_id and obj.voucher:
            try:
                link = reverse("admin:crp_accounting_voucher_change", args=[obj.voucher.pk])
                return format_html('<a href="{}" target="_blank">{}</a>', link,
                                   obj.voucher.voucher_number or f"Voucher #{obj.voucher.pk}")
            except NoReverseMatch:
                logger.warning(
                    f"NoReverseMatch for voucher change link from VoucherLine {obj.pk} (Voucher PK: {obj.voucher.pk})")
                return obj.voucher.voucher_number or f"Voucher #{obj.voucher.pk} (Link Error)"
        return "—"

    @admin.display(description=_('Account'), ordering='account__account_number')
    def account_display(self, obj: VoucherLine) -> str:
        """Displays the account name and number. Handles if account is not set (should not happen)."""
        # obj.account should be preloaded
        if obj.account_id and obj.account:
            return f"{obj.account.account_name} ({obj.account.account_number})"
        return _("Account Missing") if obj.account_id else "—"

    @admin.display(description=_('Dr/Cr'), ordering='dr_cr')
    def dr_cr_display(self, obj: VoucherLine) -> str:
        """Displays the human-readable Dr/Cr status."""
        return obj.get_dr_cr_display() if obj.dr_cr else "—"

    @admin.display(description=_('Narration'))
    def line_narration_short(self, obj: VoucherLine) -> str:
        """Displays a truncated version of the line narration."""
        narration = obj.narration or ""  # Ensure narration is a string
        return (narration[:57] + '...') if len(narration) > 60 else narration or "—"

    @admin.display(description=_('Line Created At'), ordering='created_at')
    def created_at_display(self, obj: VoucherLine) -> str:
        """Formats the created_at timestamp. Assumes VoucherLine has 'created_at'."""
        if hasattr(obj, 'created_at') and obj.created_at:
            return obj.created_at.strftime('%Y-%m-%d %H:%M')
        return "—"