# crp_accounting/admin/reconciliation.py

import logging
from decimal import Decimal
from typing import Optional, Any, Tuple, Callable, Dict, List

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.admin.sites import AdminSite  # For type hinting self.admin_site
from django.db import models
from django.forms import FileInput
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html, escape
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied, \
    ObjectDoesNotExist
from django.http import HttpRequest, HttpResponseRedirect, HttpResponse  # For response_change
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.utils.http import urlencode  # For redirecting to Voucher add form with params

# --- Base Admin Class Import ---
from .admin_base import TenantAccountingModelAdmin

# --- Model Imports ---
from ..models.reconciliation import (
    BankStatementUpload, BankStatementTransaction, BankReconciliation, ReconciledItemPair
)
from ..models.coa import Account
from ..models.journal import VoucherLine, Voucher, VoucherType as AppVoucherType  # Alias if needed
from company.models import Company

# --- Enum Imports ---
from crp_core.enums import AccountType

# --- Service Imports ---
from ..services import brs_service
from ..services.brs_service import (
    BRSServiceError, StatementParsingError, ReconciliationError, MatchingError, GLPostingError
)

logger = logging.getLogger("crp_accounting.admin.reconciliation")
ZERO = Decimal('0.00')


# =============================================================================
# BankStatementUpload Admin (Updated for PDF information)
# =============================================================================
class BankStatementUploadForm(forms.ModelForm):  # Use forms.ModelForm
    class Meta:
        model = BankStatementUpload
        fields = '__all__'
        widgets = {
            'statement_file': FileInput(attrs={'accept': '.csv,.pdf'}),  # Explicitly accept PDF and CSV
        }


@admin.register(BankStatementUpload)
class BankStatementUploadAdmin(TenantAccountingModelAdmin):
    form = BankStatementUploadForm  # Use the custom form

    list_display = ('id_link', 'bank_account_display', 'statement_file_name_list', 'uploaded_at_short_list',
                    'uploaded_by_user_display_list', 'status_colored_list', 'transaction_count_in_file',
                    'transactions_imported_count',)
    list_filter_non_superuser = (
    'status', ('bank_account', admin.RelatedOnlyFieldListFilter), ('uploaded_at', admin.DateFieldListFilter))
    search_fields = ('bank_account__account_name', 'bank_account__account_number', 'statement_file', 'company__name',
                     'uploaded_by__name')
    readonly_fields_on_change = (
    'uploaded_by_user_display_form', 'uploaded_at_short_form', 'file_hash', 'status_colored_form',
    'processing_notes_display', 'transaction_count_in_file', 'transactions_imported_count',
    'statement_file_name_form_display',)
    autocomplete_fields = ['company', 'bank_account']
    date_hierarchy = 'uploaded_at';
    actions = ['admin_action_process_statement_uploads'];
    ordering = ('-uploaded_at',);
    list_select_related = ('company', 'bank_account', 'uploaded_by')

    add_fieldsets = (
        (None, {'fields': ('company', 'bank_account', 'statement_file')}),
        (_('Optional Period Info'), {
            'fields': ('statement_period_start_date', 'statement_period_end_date'),
            'classes': ('collapse',),
            'description': _(
                "Supported file types: CSV, PDF. Ensure PDF is text-based (not scanned image without OCR).")
            # Help text
        }),
    )
    change_fieldsets = (
        (None, {'fields': ('company', 'bank_account', 'statement_file_name_form_display')}),
        # Original file cannot be changed directly here for safety
        (_('Statement Period'), {'fields': ('statement_period_start_date', 'statement_period_end_date')}),
        (_('Upload Details'),
         {'fields': ('uploaded_by_user_display_form', 'uploaded_at_short_form', 'file_hash', 'status_colored_form')}),
        (_('Processing Results'),
         {'fields': ('transaction_count_in_file', 'transactions_imported_count', 'processing_notes_display'),
          'classes': ('collapse',)}),
        (_('Audit Info'),
         {'fields': ('created_at', 'updated_at', 'created_by', 'updated_by'), 'classes': ('collapse',)}),
    )

    # Note: To re-upload a file for an existing record, it's usually better to delete and create new,
    # or have a specific "Replace File" action if status allows.
    # The 'statement_file' field is typically not shown in change_fieldsets if it's complex to re-process.
    # If you want to allow re-uploading via the change form, add 'statement_file' to change_fieldsets carefully.

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.change_fieldsets

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        if obj: ro.update(self.readonly_fields_on_change)
        # Prevent changing key details if already processed or processing
        if obj and obj.status not in [BankStatementUpload.UploadStatus.PENDING.value,
                                      BankStatementUpload.UploadStatus.FAILED.value]:
            ro.update(['bank_account', 'statement_period_start_date', 'statement_period_end_date'])
            # statement_file re-upload for an existing record is tricky due to hash/processing.
            # Usually, you'd create a new upload if the file is different or needs reprocessing.
            # For simplicity, let's make it readonly after initial upload if not PENDING/FAILED.
            ro.add('statement_file')
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        # (Logic for filtering bank_account as before)
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request, unquote(
            request.resolver_match.kwargs.get('object_id', ''))) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)
        if db_field.name == "bank_account":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=AccountType.ASSET.value,
                                                            is_active=True).order_by('account_name')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj: BankStatementUpload, form, change):
        log_prefix = f"[BSUAdmin SaveModel][User:{request.user.name}][Upload:{obj.pk or 'New'}]";
        is_new = not obj.pk
        if is_new: obj.uploaded_by = request.user

        # Handle file hash calculation if a new file is uploaded (on add or change if allowed)
        if 'statement_file' in form.changed_data and obj.statement_file and hasattr(obj.statement_file.file, 'read'):
            try:
                obj.statement_file.file.seek(0)
                file_content_bytes = obj.statement_file.file.read()
                obj.statement_file.file.seek(0)
                obj.file_hash = brs_service._calculate_file_hash(file_content_bytes)
                logger.info(f"{log_prefix} Calculated/Updated file_hash: {obj.file_hash}")
                # If file changed, reset processing status to PENDING to allow re-processing
                if not is_new and obj.status != BankStatementUpload.UploadStatus.PENDING.value:
                    logger.info(f"{log_prefix} File changed. Resetting status to PENDING for re-processing.")
                    obj.status = BankStatementUpload.UploadStatus.PENDING.value
                    obj.processing_notes = f"File updated by {request.user.name}. Awaiting re-processing.\n" + (
                                obj.processing_notes or "")
                    obj.transactions_imported_count = 0  # Reset counts
                    obj.transaction_count_in_file = None
            except Exception as e_hash:
                logger.error(f"{log_prefix} Error calculating file hash on save: {e_hash}");
                messages.error(request, _("Error processing uploaded file (hash calculation failed)."))
                # Do not save if hash calculation fails for a new/changed file, as it's critical for idempotency.
                return

        super().save_model(request, obj, form, change)  # Calls full_clean from base
        if is_new and obj.pk: messages.info(request,
                                            _("Bank statement (ID: %(id)s) for '%(bank)s' uploaded. Process it via the admin action to import transactions.") % {
                                                'id': obj.pk, 'bank': obj.bank_account})

    # --- Display methods (as before, confirmed correct) ---
    @admin.display(description=_('ID'), ordering='id')
    def id_link(self, obj: BankStatementUpload):
        return format_html('<a href="{}">{}</a>',
                           reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_change", args=[obj.pk]), obj.pk)

    @admin.display(description=_('Bank Account'), ordering='bank_account__account_name')
    def bank_account_display(self, obj: BankStatementUpload):
        return str(obj.bank_account) if obj.bank_account_id else "—"

    @admin.display(description=_('File'))
    def statement_file_name_list(self, obj: BankStatementUpload):
        return obj.statement_file.name.split('/')[-1] if obj.statement_file else "N/A"

    @admin.display(description=_('File Name'))
    def statement_file_name_form_display(self, obj: BankStatementUpload):
        return self.statement_file_name_list(obj)

    @admin.display(description=_('Uploaded At'), ordering='uploaded_at')
    def uploaded_at_short_list(self, obj: BankStatementUpload):
        return obj.uploaded_at.strftime('%Y-%m-%d %H:%M') if obj.uploaded_at else ''

    @admin.display(description=_('Uploaded At'))
    def uploaded_at_short_form(self, obj: BankStatementUpload):
        return self.uploaded_at_short_list(obj)

    @admin.display(description=_('Uploaded By'), ordering='uploaded_by__name')
    def uploaded_by_user_display_list(self, obj: BankStatementUpload):
        return obj.uploaded_by.name if obj.uploaded_by_id else (_("System") if obj.pk else "—")

    @admin.display(description=_('Uploaded By'))
    def uploaded_by_user_display_form(self, obj: BankStatementUpload):
        return self.uploaded_by_user_display_list(obj)

    @admin.display(description=_('Status'), ordering='status')
    def status_colored_list(self, obj: BankStatementUpload):
        colors = {BankStatementUpload.UploadStatus.PENDING.value: 'grey',
                  BankStatementUpload.UploadStatus.PROCESSING.value: 'orange',
                  BankStatementUpload.UploadStatus.COMPLETED.value: 'green',
                  BankStatementUpload.UploadStatus.PARTIAL_IMPORT.value: 'purple',
                  BankStatementUpload.UploadStatus.FAILED.value: 'red',
                  BankStatementUpload.UploadStatus.DUPLICATE.value: 'blue'}; return format_html(
            f'<strong style="color:{colors.get(obj.status, "black")};">{obj.get_status_display()}</strong>')

    @admin.display(description=_('Status'))
    def status_colored_form(self, obj: BankStatementUpload):
        return self.status_colored_list(obj)

    @admin.display(description=_('Processing Notes'))
    def processing_notes_display(self, obj: BankStatementUpload):
        return format_html(
            "<pre style='white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto; border: 1px solid #eee; padding: 5px;'>{}</pre>",
            escape(obj.processing_notes or _("No processing notes.")))

    # --- Admin Action (as before, confirmed correct) ---
    @admin.action(description=_('Process selected PENDING/FAILED/PARTIAL statement uploads'))
    def admin_action_process_statement_uploads(self, request: HttpRequest, queryset: models.QuerySet):
        eligible_uploads = queryset.filter(
            status__in=[BankStatementUpload.UploadStatus.PENDING.value, BankStatementUpload.UploadStatus.FAILED.value,
                        BankStatementUpload.UploadStatus.PARTIAL_IMPORT.value])
        if not eligible_uploads.exists(): messages.info(request,
                                                        _("No uploads in processable status selected.")); return
        processed_count, error_count_uploads = 0, 0  # Renamed error_count to avoid conflict
        for upload in eligible_uploads:
            log_prefix_action = f"[AdminActionProcessUpload][User:{request.user.name}][Upload:{upload.pk}]"
            try:
                # Ensure company context is passed if service expects it, though process_statement_upload uses upload.company_id
                imported_txns, errors_list = brs_service.process_statement_upload(statement_upload_id=upload.pk,
                                                                                  user_performing_process=request.user)
                if errors_list:
                    error_count_uploads += 1; messages.warning(request,
                                                               _("Stmt '%(f)s' (ID:%(id)s) processed with %(ec)d errors/warnings. %(ic)d txns imported. Check processing notes.") % {
                                                                   'f': upload.statement_file.name.split('/')[-1],
                                                                   'id': upload.pk, 'ec': len(errors_list),
                                                                   'ic': imported_txns}); [
                        messages.error(request, f"Upload ID {upload.pk} error: {err[:150]}") for err in
                        errors_list[:3]]  # Show first few errors
                elif imported_txns > 0:
                    processed_count += 1; messages.success(request,
                                                           _("Stmt '%(f)s' (ID:%(id)s) processed. %(c)d txns imported.") % {
                                                               'f': upload.statement_file.name.split('/')[-1],
                                                               'id': upload.pk, 'c': imported_txns})
                else:
                    messages.info(request,
                                  _("Stmt '%(f)s' (ID:%(id)s) processed. No new txns imported (e.g., empty, all duplicates, or unparseable).") % {
                                      'f': upload.statement_file.name.split('/')[-1], 'id': upload.pk})
            except BRSServiceError as e_serv:
                error_count_uploads += 1; messages.error(request,
                                                         _("Error processing stmt '%(f)s' (ID:%(id)s): %(err)s") % {
                                                             'f': upload.statement_file.name.split('/')[-1],
                                                             'id': upload.pk, 'err': str(e_serv)}); logger.error(
                    f"{log_prefix_action} Service error: {e_serv}", exc_info=True)
            except Exception as e_unexp:
                error_count_uploads += 1; messages.error(request,
                                                         _("Unexpected error processing stmt '%(f)s' (ID:%(id)s): %(err)s") % {
                                                             'f': upload.statement_file.name.split('/')[-1],
                                                             'id': upload.pk, 'err': str(e_unexp)}); logger.exception(
                    f"{log_prefix_action} Unexpected error.")
        if processed_count > 0: self.message_user(request,
                                                  _("%(c)d statement(s) had transactions successfully imported/updated.") % {
                                                      'c': processed_count}, messages.SUCCESS)
        if error_count_uploads > 0: self.message_user(request,
                                                      _("%(c)d statement(s) encountered errors during processing.") % {
                                                          'c': error_count_uploads}, messages.WARNING)

# =============================================================================
# BankStatementTransaction Admin
# =============================================================================
# (BankStatementTransactionAdmin as previously provided - confirmed correct)
@admin.register(BankStatementTransaction)
class BankStatementTransactionAdmin(TenantAccountingModelAdmin):
    list_display = ('transaction_date_display', 'description_short', 'transaction_type_colored', 'amount_display',
                    'bank_account_name_list', 'is_reconciled_display', 'upload_source_link')
    list_filter_non_superuser = (
    ('bank_account', admin.RelatedOnlyFieldListFilter), ('transaction_date', admin.DateFieldListFilter),
    'transaction_type', 'is_reconciled')
    search_fields = (
    'description', 'transaction_id', 'reference_number', 'bank_account__account_name', 'upload_source__statement_file',
    'company__name')
    readonly_fields = [f.name for f in BankStatementTransaction._meta.fields if f.name not in ('id',)]
    date_hierarchy = 'transaction_date';
    list_select_related = ('company', 'upload_source', 'bank_account', 'reconciled_by');
    ordering = ('-transaction_date', '-pk')

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description=_('Txn Date'), ordering='transaction_date')
    def transaction_date_display(self, obj: BankStatementTransaction):
        return obj.transaction_date.strftime('%Y-%m-%d') if obj.transaction_date else ''

    @admin.display(description=_('Description'))
    def description_short(self, obj: BankStatementTransaction):
        return (obj.description[:60] + '...') if obj.description and len(obj.description) > 60 else obj.description

    @admin.display(description=_('Type'), ordering='transaction_type')
    def transaction_type_colored(self, obj: BankStatementTransaction):
        color = 'green' if obj.transaction_type == BankStatementTransaction.TransactionType.CREDIT.value else (
            'red' if obj.transaction_type == BankStatementTransaction.TransactionType.DEBIT.value else 'grey'); return format_html(
            f'<strong style="color:{color};">{obj.get_transaction_type_display()}</strong>')

    @admin.display(description=_('Amount'), ordering='amount')
    def amount_display(self, obj: BankStatementTransaction):
        curr = obj.bank_account.currency if obj.bank_account_id and obj.bank_account else ""; return f"{obj.amount:.2f} {curr}"

    @admin.display(description=_('Bank Account'), ordering='bank_account__account_name')
    def bank_account_name_list(self, obj: BankStatementTransaction):
        return str(obj.bank_account) if obj.bank_account_id else "—"

    @admin.display(description=_('Reconciled?'), boolean=True, ordering='is_reconciled')
    def is_reconciled_display(self, obj: BankStatementTransaction):
        return obj.is_reconciled

    @admin.display(description=_('Upload Source'), ordering='upload_source__id')
    def upload_source_link(self, obj: BankStatementTransaction):
        if obj.upload_source_id:
            try:
                link = reverse("admin:crp_accounting_bankstatementupload_change",
                               args=[obj.upload_source_id]); return format_html('<a href="{}">Upload #{}</a>', link,
                                                                                obj.upload_source_id)
            except NoReverseMatch:
                return f"Upload #{obj.upload_source_id}"
        return "—"


# =============================================================================
# BankReconciliation Admin (Workspace)
# =============================================================================
@admin.register(BankReconciliation)
class BankReconciliationAdmin(TenantAccountingModelAdmin):
    add_form_template = 'admin/change_form.html'
    change_form_template = 'admin/crp_accounting/reconciliation/brs_workspace.html'

    list_display = (
    'statement_date', 'bank_account_name_list_br', 'status_colored_list_br', 'difference_display_list_br',
    'reconciliation_completed_at_short_list_br', 'reconciled_by_user_display_list_br')
    list_filter_non_superuser = (
    ('bank_account', admin.RelatedOnlyFieldListFilter), 'status', ('statement_date', admin.DateFieldListFilter))
    search_fields = ('bank_account__account_name', 'bank_account__account_number', 'notes', 'company__name')
    readonly_fields_for_change_form = (
    'difference_display_form_br', 'adjusted_book_balance', 'reconciliation_completed_at_short_form_br',
    'reconciled_by_user_display_form_br', 'company_display_for_form_br', 'bank_account_display_for_form_br',
    'status_colored_for_form_br',)
    autocomplete_fields = ['company', 'bank_account']
    ordering = ('-statement_date', 'bank_account__account_name');
    date_hierarchy = 'statement_date'
    actions = ['admin_action_finalize_reconciliations'];
    list_select_related = ('company', 'bank_account', 'reconciled_by')
    add_fieldsets = ((None, {'fields': ('company', 'bank_account', 'statement_date', 'statement_ending_balance')}),)
    change_fieldsets_in_progress = ((None, {'fields': (
    'company_display_for_form_br', 'bank_account_display_for_form_br', 'statement_date', 'statement_ending_balance')}),
                                    (_('Calculated Balances'), {'fields': (
                                    'book_balance_before_adjustments', 'adjusted_book_balance',
                                    'difference_display_for_form_br')}), (
                                    _('Reconciling Items Summary (Manually update or from Service)'), {'fields': (
                                    'outstanding_payments_total', 'deposits_in_transit_total',
                                    'bank_charges_or_interest_total', 'other_adjustments_total')}), (
                                    _('Status & Completion'), {'fields': (
                                    'status_colored_for_form_br', 'reconciled_by_user_display_for_form_br',
                                    'reconciliation_completed_at_short_for_form_br')}),
                                    (_('Notes'), {'fields': ('notes',)}), (_('Audit Info'), {
        'fields': ('created_at', 'updated_at', 'created_by', 'updated_by'), 'classes': ('collapse',)}),)
    change_fieldsets_reconciled = ((None, {'fields': (
    'company_display_for_form_br', 'bank_account_display_for_form_br', 'statement_date', 'statement_ending_balance')}),
                                   (_('Calculated Balances'), {'fields': (
                                   'book_balance_before_adjustments', 'adjusted_book_balance',
                                   'difference_display_for_form_br')}), (_('Reconciling Items Summary'), {'fields': (
    'outstanding_payments_total', 'deposits_in_transit_total', 'bank_charges_or_interest_total',
    'other_adjustments_total')}), (_('Status & Completion'), {'fields': (
    'status_colored_for_form_br', 'reconciled_by_user_display_for_form_br',
    'reconciliation_completed_at_short_for_form_br')}), (_('Notes'), {'fields': ('notes',)}), (_('Audit Info'), {
        'fields': ('created_at', 'updated_at', 'created_by', 'updated_by'), 'classes': ('collapse',)}),)

    def get_list_filter(self, request):
        return (
               'company',) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else (
            self.change_fieldsets_reconciled if obj.status == BankReconciliation.ReconciliationStatus.RECONCILED.value else self.change_fieldsets_in_progress)

    def get_readonly_fields(self, request, obj=None):
        ro = set(super().get_readonly_fields(request, obj) or [])
        if obj: ro.update(self.readonly_fields_for_change_form); ro.add('statement_date')
        if obj and obj.status == BankReconciliation.ReconciliationStatus.RECONCILED.value: ro.update(
            ['statement_ending_balance', 'book_balance_before_adjustments', 'outstanding_payments_total',
             'deposits_in_transit_total', 'bank_charges_or_interest_total', 'other_adjustments_total', 'notes'])
        return tuple(ro)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        company_context = self._get_company_from_request_obj_or_form(request, self.get_object(request, unquote(
            request.resolver_match.kwargs.get('object_id', ''))) if request.resolver_match.kwargs.get(
            'object_id') else None, request.POST if request.method == 'POST' and not request.resolver_match.kwargs.get(
            'object_id') else None)
        if db_field.name == "bank_account":
            if company_context:
                kwargs["queryset"] = Account.objects.filter(company=company_context,
                                                            account_type=AccountType.ASSET.value,
                                                            is_active=True).order_by('account_name')
            else:
                kwargs["queryset"] = Account.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def add_view(self, request, form_url='', extra_context=None):
        context = self.get_changeform_initial_data(request);
        context.update(extra_context or {});
        context.update(self.admin_site.each_context(request) or {});  # Use self.admin_site
        context['opts'] = self.opts;
        context['add'] = True;
        context['change'] = False;
        context['has_view_permission'] = self.has_view_permission(request);
        logger.debug(f"[BRSAdmin AddView] Using template '{self.add_form_template}'.")
        return super().add_view(request, form_url, extra_context=context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {};
        log_prefix = f"[BRSAdmin ChangeView][User:{request.user.name}][RecID:{object_id}]"
        extra_context.update(self.admin_site.each_context(request) or {});
        extra_context['opts'] = self.opts
        try:
            reconciliation_obj = self.get_object(request, unquote(object_id))
            if reconciliation_obj:
                logger.info(
                    f"{log_prefix} Loading workspace for Rec {reconciliation_obj.pk} (Co: {reconciliation_obj.company_id})")
                workspace_data = brs_service.get_reconciliation_workspace_data(company_id=reconciliation_obj.company_id,
                                                                               bank_account_id=reconciliation_obj.bank_account_id,
                                                                               statement_end_date=reconciliation_obj.statement_date,
                                                                               reconciliation_id=reconciliation_obj.pk)
                extra_context.update(workspace_data);
                extra_context['original'] = reconciliation_obj
                logger.debug(
                    f"{log_prefix} Workspace data loaded. Unrec BankTx: {len(workspace_data.get('unreconciled_bank_transactions', []))}, Unrec GL: {len(workspace_data.get('unreconciled_gl_lines', []))}")
            else:
                logger.warning(f"{log_prefix} Could not get object.")
        except Exception as e:
            logger.exception(f"{log_prefix} Error preparing extra_context."); messages.error(request,
                                                                                             _("Error loading workspace: %(err)s") % {
                                                                                                 'err': str(e)})
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def save_model(self, request, obj: BankReconciliation, form, change):
        is_new = not obj.pk
        if is_new: obj.created_by = request.user
        # updated_by will be set by TenantScopedModel
        if change and form.has_changed():
            if any(f in form.changed_data for f in
                   ['statement_ending_balance', 'book_balance_before_adjustments', 'outstanding_payments_total',
                    'deposits_in_transit_total', 'bank_charges_or_interest_total', 'other_adjustments_total']):
                obj.calculate_adjusted_balances(perform_save=False)
        super().save_model(request, obj, form, change)  # This calls obj.full_clean()

    # --- response_change: Handles custom submit buttons from brs_workspace.html ---
    def response_change(self, request: HttpRequest, obj: BankReconciliation) -> HttpResponse:
        opts = self.model._meta
        log_prefix = f"[BRSAdmin RespChange][User:{request.user.name}][Rec:{obj.pk}]"

        if "_match_selected_items_action" in request.POST:
            selected_bank_txn_pk = request.POST.get('selected_bank_transaction_pk')
            selected_gl_line_pks = request.POST.getlist('selected_gl_line_pks')
            match_notes = request.POST.get('match_notes', "")
            if not selected_bank_txn_pk or not selected_gl_line_pks:
                self.message_user(request, _("Select one bank txn and at least one GL line to match."), messages.ERROR)
            else:
                try:
                    logger.info(
                        f"{log_prefix} Manual match via form. BankTx: {selected_bank_txn_pk}, GLLines: {selected_gl_line_pks}")
                    brs_service.match_reconciliation_items(company_id=obj.company_id, reconciliation_id=obj.pk,
                                                           bank_transaction_pk=selected_bank_txn_pk,
                                                           gl_line_pks=selected_gl_line_pks,
                                                           reconciled_by_user=request.user, notes=match_notes)
                    self.message_user(request, _("Selected items matched successfully."), messages.SUCCESS)
                except (BRSServiceError, MatchingError, DjangoValidationError) as e:
                    self.message_user(request, f"{_('Error matching items')}: {str(e)}", messages.ERROR)
                except Exception as e:
                    logger.exception(f"{log_prefix} Unexpected error during match."); self.message_user(request,
                                                                                                        _("Unexpected error during matching."),
                                                                                                        messages.ERROR)
            return HttpResponseRedirect(request.path)  # Refresh workspace

        elif "_create_adjustment_bank_fee" in request.POST or "_create_adjustment_interest_earned" in request.POST:
            selected_bank_txn_pk = request.POST.get('selected_bank_transaction_pk')
            if not selected_bank_txn_pk: self.message_user(request, _("Select a bank transaction for adjustment."),
                                                           messages.ERROR); return HttpResponseRedirect(request.path)
            try:
                bank_txn = BankStatementTransaction.objects.get(pk=selected_bank_txn_pk, company=obj.company,
                                                                bank_account=obj.bank_account)
                is_debit_adjustment = "_create_adjustment_bank_fee" in request.POST  # Bank fee is a debit to expense

                # Redirect to Voucher add form with pre-filled data
                # Requires CompanyAccountingSettings to have default_bank_fee_account / default_interest_income_account
                # This part needs robust fetching of default accounts from settings.
                default_other_side_account_pk = None
                narration_prefix = ""
                try:
                    company_settings = obj.company.accounting_settings
                    if is_debit_adjustment:
                        default_other_side_account_pk = company_settings.default_bank_fee_expense_account_id
                        narration_prefix = _("Bank Fee Adjustment for:")
                    else:  # Interest earned
                        default_other_side_account_pk = company_settings.default_interest_income_account_id
                        narration_prefix = _("Interest Earned Adjustment for:")
                except (Company.accounting_settings.RelatedObjectDoesNotExist, AttributeError):
                    messages.error(request, _("Default accounts for adjustments not configured in Company Settings."))
                    return HttpResponseRedirect(request.path)

                if not default_other_side_account_pk:
                    messages.error(request, _("Default account for this adjustment type not set in Company Settings."))
                    return HttpResponseRedirect(request.path)

                query_params = {
                    'company': obj.company_id,  # Auto-select company on Voucher form if SU
                    'voucher_type': AppVoucherType.BANK_RECONCILIATION_ADJUSTMENT.value,
                    'date': bank_txn.transaction_date.isoformat(),
                    'narration': f"{narration_prefix} {bank_txn.description[:100]} (Bank Txn ID: {bank_txn.pk})",
                    'brs_bank_txn_pk_for_adj': bank_txn.pk,  # Custom param to link back
                    'brs_reconciliation_pk': obj.pk,  # Custom param
                    # Pre-fill lines (example):
                    # line_1_account = obj.bank_account_id
                    # line_1_dr_cr = DrCrType.CREDIT.value if is_debit_adjustment else DrCrType.DEBIT.value
                    # line_1_amount = str(bank_txn.amount)
                    # line_2_account = default_other_side_account_pk
                    # line_2_dr_cr = DrCrType.DEBIT.value if is_debit_adjustment else DrCrType.CREDIT.value
                    # line_2_amount = str(bank_txn.amount)
                    # ... add these to query_params ...
                }
                # This is a simplified redirect. Ideally, VoucherAdmin.add_view handles these GET params to pre-fill form.
                # A hidden field on Voucher form `source_brs_bank_txn_pk` might be useful.
                # After saving voucher, a signal on Voucher could link it back to BankStatementTransaction.
                add_voucher_url = reverse("admin:crp_accounting_voucher_add") + "?" + urlencode(query_params)
                messages.info(request,
                              _("Redirecting to create adjustment voucher. Please complete and save the voucher. It will be linked automatically if possible."))
                return HttpResponseRedirect(add_voucher_url)

            except BankStatementTransaction.DoesNotExist:
                self.message_user(request, _("Selected bank txn for adjustment not found."), messages.ERROR)
            except Exception as e:
                logger.exception(f"{log_prefix} Error preparing adjustment."); self.message_user(request,
                                                                                                 _("Error preparing for adjustment."),
                                                                                                 messages.ERROR)
            return HttpResponseRedirect(request.path)

        elif "_finalize_reconciliation_action" in request.POST:  # From submit row button
            obj.refresh_from_db(fields=['statement_ending_balance'])  # Ensure we use potentially saved balance
            if obj.statement_ending_balance is None: self.message_user(request,
                                                                       _("Statement Ending Balance must be set."),
                                                                       messages.ERROR); return HttpResponseRedirect(
                request.path)
            try:
                brs_service.finalize_bank_reconciliation(company_id=obj.company_id, reconciliation_id=obj.pk,
                                                         finalized_by_user=request.user,
                                                         statement_actual_ending_balance=obj.statement_ending_balance)
                self.message_user(request, _("Bank Reconciliation finalized successfully."), messages.SUCCESS)
            except (ReconciliationError, DjangoValidationError) as e:
                self.message_user(request, f"{_('Error finalizing')}: {str(e)}", messages.ERROR)
            except Exception as e:
                logger.exception(f"{log_prefix} Unexpected error during finalize."); self.message_user(request,
                                                                                                       _("Unexpected error finalizing."),
                                                                                                       messages.ERROR)
            return HttpResponseRedirect(
                reverse('admin:crp_accounting_bankreconciliation_changelist'))  # Go to list view after finalize

        return super().response_change(request, obj)

    # Display helpers for BankReconciliationAdmin (List and Form, with unique names)
    @admin.display(description=_('Bank Acc.'), ordering='bank_account__account_name')  # Shortened for list
    def bank_account_name_list_br(self, obj: BankReconciliation):
        return str(obj.bank_account.account_number) if obj.bank_account_id and obj.bank_account else "—"

    @admin.display(description=_('Bank Account'))  # For form
    def bank_account_display_for_form_br(self, obj: Optional[BankReconciliation]):
        return str(obj.bank_account) if obj and obj.bank_account_id else "—"

    @admin.display(description=_('Status'), ordering='status')
    def status_colored_list_br(self, obj: BankReconciliation):
        colors = {BankReconciliation.ReconciliationStatus.IN_PROGRESS.value: 'orange',
                  BankReconciliation.ReconciliationStatus.RECONCILED.value: 'green'}; return format_html(
            f'<strong style="color:{colors.get(obj.status, "grey")};">{obj.get_status_display()}</strong>')

    @admin.display(description=_('Status'))  # For form
    def status_colored_for_form_br(self, obj: Optional[BankReconciliation]):
        return self.status_colored_list_br(obj) if obj else BankReconciliation.ReconciliationStatus.IN_PROGRESS.label

    @admin.display(description=_('Difference'), ordering='difference')
    def difference_display_list_br(self, obj: BankReconciliation):
        diff_val = obj.difference or ZERO; color = 'green' if abs(diff_val) < Decimal(
            '0.01') else 'red'; curr = obj.bank_account.currency if obj.bank_account_id and obj.bank_account else ""; return format_html(
            f'<strong style="color:{color};">{diff_val:.2f} {curr}</strong>')

    @admin.display(description=_('Difference'))  # For form
    def difference_display_for_form_br(self, obj: Optional[BankReconciliation]):
        return self.difference_display_list_br(obj) if obj else f"{ZERO:.2f}"

    @admin.display(description=_('Completed'), ordering='reconciliation_completed_at')  # Shortened for list
    def reconciliation_completed_at_short_list_br(self, obj: BankReconciliation):
        return obj.reconciliation_completed_at.strftime('%Y-%m-%d %H:%M') if obj.reconciliation_completed_at else "—"

    @admin.display(description=_('Completed At'))  # For form
    def reconciliation_completed_at_short_for_form_br(self, obj: Optional[BankReconciliation]):
        return self.reconciliation_completed_at_short_list_br(obj) if obj else "—"

    @admin.display(description=_('By'), ordering='reconciled_by__name')  # Shortened for list
    def reconciled_by_user_display_list_br(self, obj: BankReconciliation):
        return obj.reconciled_by.name if obj.reconciled_by_id else "—"

    @admin.display(description=_('Reconciled By'))  # For form
    def reconciled_by_user_display_for_form_br(self, obj: Optional[BankReconciliation]):
        return self.reconciled_by_user_display_list_br(obj) if obj else "—"

    @admin.display(description=_('Company'))  # For form
    def company_display_for_form_br(self, obj: Optional[BankReconciliation]):
        return obj.company.name if obj and obj.company_id and obj.company else "—"

    @admin.action(description=_('Finalize selected RECONCILIABLE reconciliations (List Action)'))
    def admin_action_finalize_reconciliations(self, request: HttpRequest, queryset: models.QuerySet):
        # (Logic as previously provided - seems robust)
        eligible_recs = queryset.filter(status=BankReconciliation.ReconciliationStatus.IN_PROGRESS.value)
        if not eligible_recs.exists(): messages.info(request, _("No 'In Progress' reconciliations selected.")); return
        processed_count, error_count = 0, 0
        for rec in eligible_recs:
            log_prefix_action = f"[AdminActionFinalizeBRS][User:{request.user.name}][Rec:{rec.pk}]"
            try:
                statement_bal = rec.statement_ending_balance
                if statement_bal is None: messages.error(request, _("Rec %(id)s: Stmt Ending Balance not set.") % {
                    'id': rec.pk}); error_count += 1; continue
                brs_service.finalize_bank_reconciliation(company_id=rec.company_id, reconciliation_id=rec.pk,
                                                         finalized_by_user=request.user,
                                                         statement_actual_ending_balance=statement_bal)
                processed_count += 1;
                messages.success(request, _("BRS for '%(b)s' @ %(d)s finalized.") % {'b': rec.bank_account,
                                                                                     'd': rec.statement_date})
            except ReconciliationError as e_serv:
                error_count += 1; messages.error(request, _("Error finalizing Rec ID %(id)s: %(err)s") % {'id': rec.pk,
                                                                                                          'err': str(
                                                                                                              e_serv)}); logger.warning(
                    f"{log_prefix_action} Service error: {e_serv}")
            except Exception as e_unexp:
                error_count += 1; messages.error(request, _("Unexpected error finalizing Rec ID %(id)s: %(err)s") % {
                    'id': rec.pk, 'err': str(e_unexp)}); logger.exception(f"{log_prefix_action} Unexpected error.")
        if processed_count > 0: self.message_user(request,
                                                  _("%(c)d reconciliation(s) finalized.") % {'c': processed_count},
                                                  messages.SUCCESS)
        if error_count > 0: self.message_user(request,
                                              _("%(c)d reconciliation(s) failed to finalize.") % {'c': error_count},
                                              messages.WARNING)


# =============================================================================
# ReconciledItemPair Admin
# =============================================================================
# (ReconciledItemPairAdmin as previously provided - confirmed correct)
@admin.register(ReconciledItemPair)
class ReconciledItemPairAdmin(TenantAccountingModelAdmin):
    list_display = (
    'reconciliation_link', 'bank_transaction_link', 'matched_gl_line_link', 'reconciled_at_short_display_rip',
    'reconciled_by_user_display_rip')
    list_filter_non_superuser = (('reconciliation__bank_account', admin.RelatedOnlyFieldListFilter),
                                 ('reconciliation__statement_date', admin.DateFieldListFilter),
                                 ('reconciled_at', admin.DateFieldListFilter))
    search_fields = (
    'reconciliation__bank_account__account_name', 'bank_transaction__description', 'matched_gl_line__narration',
    'company__name', 'notes')
    readonly_fields = [f.name for f in ReconciledItemPair._meta.fields if f.name not in ('id',)]
    list_select_related = (
    'company', 'reconciliation__company', 'reconciliation__bank_account', 'bank_transaction__company',
    'bank_transaction__bank_account', 'matched_gl_line__voucher__company', 'matched_gl_line__account__company',
    'reconciled_by')
    ordering = ('-reconciled_at',)

    def get_list_filter(self, request):
        return ('company', ('reconciliation__company',
                            admin.RelatedOnlyFieldListFilter)) + self.list_filter_non_superuser if request.user.is_superuser else self.list_filter_non_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description=_('Reconciliation'), ordering='reconciliation__statement_date')
    def reconciliation_link(self, obj: ReconciledItemPair):
        if obj.reconciliation_id and obj.reconciliation:
            try:
                r = obj.reconciliation; link = reverse("admin:crp_accounting_bankreconciliation_change", args=[
                    r.pk]); ba_num = r.bank_account.account_number if r.bank_account_id and r.bank_account else 'N/A'; return format_html(
                    '<a href="{}" target="_blank">BRS {} @ {}</a>', link, ba_num, r.statement_date)
            except (NoReverseMatch, AttributeError, ObjectDoesNotExist):
                return f"Rec ID {obj.reconciliation_id}"
        return "—"

    @admin.display(description=_('Bank Txn'), ordering='bank_transaction__transaction_date')
    def bank_transaction_link(self, obj: ReconciledItemPair):
        if obj.bank_transaction_id and obj.bank_transaction:
            try:
                bt = obj.bank_transaction; link = reverse("admin:crp_accounting_bankstatementtransaction_change", args=[
                    bt.pk]); d = bt.description or f"ID {bt.pk}"; return format_html('<a href="{}">{}</a>', link,
                                                                                     d[:30] + "..." if d and len(
                                                                                         d) > 30 else d)
            except (NoReverseMatch, AttributeError, ObjectDoesNotExist):
                return f"BankTx ID {obj.bank_transaction_id}"
        return "—"

    @admin.display(description=_('GL Line'), ordering='matched_gl_line__voucher__date')
    def matched_gl_line_link(self, obj: ReconciledItemPair):
        if obj.matched_gl_line_id and obj.matched_gl_line:
            try:
                gl_line = obj.matched_gl_line;
                link = reverse("admin:crp_accounting_voucherline_change", args=[gl_line.pk])
                desc_parts = [];
                if gl_line.narration: desc_parts.append(gl_line.narration)
                if gl_line.voucher_id and gl_line.voucher and gl_line.voucher.narration and gl_line.voucher.narration not in desc_parts: desc_parts.append(
                    gl_line.voucher.narration)
                desc = " | ".join(desc_parts) or f"GLLine ID {gl_line.pk}"
                return format_html('<a href="{}">{}</a>', link, desc[:30] + "..." if len(desc) > 30 else desc)
            except (NoReverseMatch, AttributeError, ObjectDoesNotExist):
                return f"GLLine ID {obj.matched_gl_line_id} (Link Err)"
        return _("N/A (Adjustment)")

    @admin.display(description=_('Reconciled At'), ordering='reconciled_at')
    def reconciled_at_short_display_rip(self, obj: ReconciledItemPair):
        return obj.reconciled_at.strftime('%Y-%m-%d %H:%M') if obj.reconciled_at else ''

    @admin.display(description=_('Reconciled By'), ordering='reconciled_by__name')
    def reconciled_by_user_display_rip(self, obj: ReconciledItemPair):
        return obj.reconciled_by.name if obj.reconciled_by_id else (_("System") if obj.reconciled_at else "—")