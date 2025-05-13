# crp_accounting/admin/period.py

import logging
from io import BytesIO
from urllib.parse import urlencode
from decimal import Decimal

from django.contrib import admin, messages
# Import AdminDateWidget
from django.contrib.admin.widgets import AdminDateWidget
# Import models for formfield_overrides
from django.db import models as django_db_models # Use an alias if 'models' is ambiguous, or just 'models'
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.http import HttpResponse
from django.core.exceptions import ValidationError as DjangoValidationError

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Font

    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    logging.warning("Admin Period: 'openpyxl' library not found. Excel exports will be disabled.")

from ..models.period import FiscalYear, AccountingPeriod


# Assuming FiscalYearStatus is an enum defined in your models.period or models.enums
# from ..models.enums import FiscalYearStatus # Or wherever it's defined
class FiscalYearStatus:  # Placeholder if not imported
    OPEN = "Open"
    LOCKED = "Locked"
    CLOSED = "Closed"


from ..services import reports_service
from .admin_base import TenantAccountingModelAdmin

logger = logging.getLogger(__name__)
ZERO = Decimal('0.00')


# =============================================================================
# Fiscal Year Admin
# =============================================================================
@admin.register(FiscalYear)
class FiscalYearAdmin(TenantAccountingModelAdmin):
    list_display = (
    'name', 'start_date', 'end_date', 'status_display', 'is_active_display', 'closed_by_user_display', 'updated_at')

    list_filter_non_superuser = ('status', 'is_active', ('start_date', admin.DateFieldListFilter))
    list_filter_superuser = ('company', 'status', 'is_active', ('start_date', admin.DateFieldListFilter))

    search_fields = ('name', 'company__name')
    readonly_fields = ('status', 'closed_by', 'closed_at', 'created_at', 'updated_at')
    list_select_related = ('company', 'closed_by')
    actions = ["admin_action_activate_year", "admin_action_lock_year", "admin_action_close_year",
               "admin_action_reopen_year"]

    # --- ADD THIS SECTION ---
    formfield_overrides = {
        django_db_models.DateField: {'widget': AdminDateWidget},
        # If you also had DateTimeFields you want to style, you'd add:
        # django_db_models.DateTimeField: {'widget': AdminSplitDateTime},
    }
    # --- END ADDED SECTION ---

    def get_fieldsets(self, request, obj=None):
        base_fields = ('name', 'start_date', 'end_date')
        company_fieldset_fields = ('company',)

        main_fieldset_fields = base_fields
        if request.user.is_superuser:
            if not obj:
                main_fieldset_fields = company_fieldset_fields + base_fields

        main_fieldset_definition = (None, {'fields': main_fieldset_fields})

        return (
            main_fieldset_definition,
            (_('Status Information'), {'fields': ('status', 'is_active', 'closed_by', 'closed_at')}),
            (_('Audit Information'), {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
        )

    @admin.display(description=_('Status'), ordering='status')
    def status_display(self, obj: FiscalYear):
        color = "green"
        if obj.status == FiscalYearStatus.LOCKED:
            color = "orange"
        elif obj.status == FiscalYearStatus.CLOSED:
            color = "red"
        return format_html(f'<span style="color:{color}; font-weight:bold;">{obj.get_status_display()}</span>')

    @admin.display(description=_('Active'), boolean=True, ordering='is_active')
    def is_active_display(self, obj: FiscalYear):
        return obj.is_active

    @admin.display(description=_('Closed By'), ordering='closed_by__username')
    def closed_by_user_display(self, obj: FiscalYear):
        return obj.closed_by.get_username() if obj.closed_by else "N/A"

    @admin.action(description=_('Activate selected Fiscal Year (deactivates others in same company)'))
    def admin_action_activate_year(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, _("Please select exactly one Fiscal Year to activate."), messages.WARNING)
            return
        year = queryset.first()
        try:
            year.activate()
            self.message_user(request, _("Fiscal Year '%(name)s' for Company '%(company)s' activated successfully.") % {
                'name': year.name, 'company': year.company.name}, messages.SUCCESS)
        except DjangoValidationError as e:
            self.message_user(request, f"Error activating '{year.name}': {e.messages_joined}", messages.ERROR)
        except Exception as e:
            logger.error(f"Admin error activating FY {year.pk} (Co: {year.company_id}): {e}", exc_info=True)
            self.message_user(request, _("An unexpected error occurred: %(error)s") % {'error': str(e)}, messages.ERROR)

    @admin.action(description=_('Lock selected OPEN Fiscal Years'))
    def admin_action_lock_year(self, request, queryset):
        updated_count = 0
        for year in queryset.filter(status=FiscalYearStatus.OPEN):
            try:
                if hasattr(year, 'lock_year'):
                    year.lock_year()
                else:
                    year.status = FiscalYearStatus.LOCKED; year.save(update_fields=['status', 'updated_at'])
                updated_count += 1
            except DjangoValidationError as e:
                self.message_user(request, _("Could not lock Fiscal Year '%(name)s': %(error)s") % {'name': year.name,
                                                                                                    'error': e.messages_joined},
                                  messages.ERROR)
        if updated_count > 0: self.message_user(request,
                                                _("%(count)d fiscal year(s) locked.") % {'count': updated_count},
                                                messages.SUCCESS)

    @admin.action(description=_('Close selected LOCKED Fiscal Years'))
    def admin_action_close_year(self, request, queryset):
        updated_count = 0
        for year in queryset.filter(status=FiscalYearStatus.LOCKED):
            try:
                year.close_year(user=request.user)
                updated_count += 1
            except DjangoValidationError as e:
                self.message_user(request, _("Could not close Fiscal Year '%(name)s': %(error)s") % {'name': year.name,
                                                                                                     'error': e.messages_joined},
                                  messages.ERROR)
        if updated_count > 0: self.message_user(request,
                                                _("%(count)d fiscal year(s) closed.") % {'count': updated_count},
                                                messages.SUCCESS)

    @admin.action(description=_('Reopen selected Fiscal Years (LOCKED/CLOSED to OPEN) - Use Caution!'))
    def admin_action_reopen_year(self, request, queryset):
        updated_count = 0
        for year in queryset.exclude(status=FiscalYearStatus.OPEN):
            try:
                if hasattr(year, 'reopen_year'):
                    year.reopen_year()
                else:
                    year.status = FiscalYearStatus.OPEN
                    year.closed_by = None
                    year.closed_at = None
                    year.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])
                updated_count += 1
            except DjangoValidationError as e:
                self.message_user(request, _("Could not reopen Fiscal Year '%(name)s': %(error)s") % {'name': year.name,
                                                                                                      'error': e.messages_joined},
                                  messages.ERROR)
        if updated_count > 0: self.message_user(request, _("%(count)d fiscal year(s) reopened to OPEN status.") % {
            'count': updated_count}, messages.WARNING)


# =============================================================================
# Accounting Period Admin
# =============================================================================
@admin.register(AccountingPeriod)
class AccountingPeriodAdmin(TenantAccountingModelAdmin):
    list_display = (
    'name', 'fiscal_year_display', 'start_date', 'end_date', 'lock_status_display', 'view_reports_links')

    list_filter_non_superuser = (
    'locked', ('fiscal_year', admin.RelatedOnlyFieldListFilter), ('start_date', admin.DateFieldListFilter))
    list_filter_superuser = (
    'company', 'locked', ('fiscal_year', admin.RelatedOnlyFieldListFilter), ('start_date', admin.DateFieldListFilter))

    search_fields = ('name', 'fiscal_year__name', 'company__name')
    readonly_fields = ('locked', 'created_at', 'updated_at')
    list_select_related = ('company', 'fiscal_year__company')
    autocomplete_fields = ['fiscal_year']
    actions = ["admin_action_lock_periods", "admin_action_unlock_periods", "admin_action_export_trial_balance"]
    ordering = ('-fiscal_year__start_date', '-start_date',)

    # --- ADD THIS SECTION ---
    formfield_overrides = {
        django_db_models.DateField: {'widget': AdminDateWidget},
    }
    # --- END ADDED SECTION ---

    def get_fieldsets(self, request, obj=None):
        base_fields = ('name', 'fiscal_year', 'start_date', 'end_date')
        company_fieldset_fields = ('company',)

        main_fieldset_fields = base_fields
        if request.user.is_superuser:
            if not obj:
                main_fieldset_fields = company_fieldset_fields + base_fields

        main_fieldset_definition = (None, {'fields': main_fieldset_fields})

        return (
            main_fieldset_definition,
            (_('Status Information'), {'fields': ('locked',)}),
            (_('Audit Information'), {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
        )

    @admin.display(description=_('Fiscal Year'), ordering='fiscal_year__name')
    def fiscal_year_display(self, obj: AccountingPeriod):
        return obj.fiscal_year.name if obj.fiscal_year else "N/A"

    @admin.display(description=_("Status"), ordering='locked')
    def lock_status_display(self, obj: AccountingPeriod):
        if obj.locked: return format_html('<span style="color:red; font-weight:bold;">🔒 Locked</span>')
        return format_html('<span style="color:green; font-weight:bold;">🔓 Open</span>')

    @admin.action(description=_('Lock selected OPEN periods'))
    def admin_action_lock_periods(self, request, queryset):
        updated_count = 0
        for period in queryset.filter(locked=False):
            try:
                period.lock_period()
                updated_count += 1
            except DjangoValidationError as e:
                self.message_user(request, _("Could not lock period '%(name)s': %(error)s") % {'name': period.name,
                                                                                               'error': e.messages_joined},
                                  messages.ERROR)
        if updated_count > 0: self.message_user(request, _("%(count)d period(s) locked.") % {'count': updated_count},
                                                messages.SUCCESS)

    @admin.action(description=_('Unlock selected LOCKED periods'))
    def admin_action_unlock_periods(self, request, queryset):
        updated_count = 0
        for period in queryset.filter(locked=True):
            try:
                period.unlock_period()
                updated_count += 1
            except DjangoValidationError as e:
                self.message_user(request, _("Could not unlock period '%(name)s': %(error)s") % {'name': period.name,
                                                                                                 'error': e.messages_joined},
                                  messages.ERROR)
        if updated_count > 0: self.message_user(request, _("%(count)d period(s) unlocked.") % {'count': updated_count},
                                                messages.SUCCESS)

    @admin.display(description=_('View Reports'))
    def view_reports_links(self, obj: AccountingPeriod):  # obj is an AccountingPeriod instance
        if not obj.company_id or not obj.fiscal_year_id:  # company_id comes from the AccountingPeriod model
            return "N/A (Missing Company/FY Info)"

        report_links = []
        namespace = "crp_accounting_api"  # This is your app_name from urls_api.py

        # --- MODIFICATION START ---
        # Add company_id to the base parameters for all report links
        # Assuming your AccountingPeriod model has a `company_id` field or `company.pk`
        # If AccountingPeriod has a direct ForeignKey to Company named 'company':
        # company_pk_to_use = obj.company.pk if obj.company else None
        # If AccountingPeriod just has 'company_id':
        company_pk_to_use = obj.company_id

        if not company_pk_to_use:
            logger.warning(f"AccountingPeriod {obj.pk} is missing company information for report links.")
            return "N/A (Period missing company link)"

        base_link_params = {'company_id': company_pk_to_use}

        # --- MODIFICATION END ---

        def build_url(view_name_suffix, params):
            try:
                # Construct the URL name, e.g., "crp_accounting_api:admin-view-balance-sheet"
                url_name = f"{namespace}:admin-view-{view_name_suffix.replace('_', '-')}"
                base_url = reverse(url_name)  # Reverse without args if your report URLs don't have path params

                # Append query parameters
                return f"{base_url}?{urlencode(params)}"
            except NoReverseMatch:
                logger.warning(
                    f"Admin Period: URL not found via reverse for '{url_name}'. Check urls.py and namespace.")
                return None

        # Trial Balance
        # Merge base_link_params with report-specific params
        tb_params = {**base_link_params, 'as_of_date': obj.end_date.isoformat()}
        tb_url = build_url('trial-balance', tb_params)
        if tb_url:
            report_links.append(format_html('<a href="{}" target="_blank">TB</a>', tb_url))

        # Profit & Loss
        pl_params = {
            **base_link_params,
            'start_date': obj.start_date.isoformat(),
            'end_date': obj.end_date.isoformat()
        }
        pl_url = build_url('profit-loss', pl_params)
        if pl_url:
            report_links.append(format_html('<a href="{}" target="_blank">P&L</a>', pl_url))

        # Balance Sheet
        bs_params = {**base_link_params, 'as_of_date': obj.end_date.isoformat()}
        bs_url = build_url('balance-sheet', bs_params)
        if bs_url:
            report_links.append(format_html('<a href="{}" target="_blank">BS</a>', bs_url))

        return format_html(' | '.join(report_links)) if report_links else "No Reports Configured"
    @admin.action(description=_('Export Trial Balance to Excel for selected period(s)'))
    def admin_action_export_trial_balance(self, request, queryset):
        if not OPENPYXL_AVAILABLE:
            self.message_user(request, _("Excel export requires 'openpyxl' library to be installed."), messages.ERROR)
            return
        if queryset.count() != 1:
            self.message_user(request, _("Please select exactly one accounting period for this export action."),
                              messages.WARNING)
            return
        period = queryset.select_related('company').first()
        if not period or not period.company_id:
            self.message_user(request, _("Cannot export: Valid Company association not found for the period."),
                              messages.ERROR)
            return

        company_id_for_report = period.company_id
        as_of_date_for_report = period.end_date
        company_name_for_filename = period.company.name if period.company else f"Company_{company_id_for_report}"
        try:
            report_data = reports_service.generate_trial_balance_structured(
                company_id=company_id_for_report, as_of_date=as_of_date_for_report
            )
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = f"TB {as_of_date_for_report.strftime('%Y-%m-%d')}"
            bold_font = Font(bold=True, size=12)
            sheet['A1'] = f"Trial Balance - {company_name_for_filename}";
            sheet['A1'].font = Font(bold=True, size=14)
            sheet.merge_cells('A1:D1')
            sheet['A2'] = f"As of: {as_of_date_for_report.strftime('%B %d, %Y')}"
            sheet['A3'] = f"Currency: {report_data.get('report_currency', 'N/A')}"
            headers = ["Acc. No.", "Account Name", "Debit", "Credit"]
            sheet.append([]);
            header_row_idx = sheet.max_row + 1
            for col_num, header_text in enumerate(headers, 1):
                cell = sheet.cell(row=header_row_idx, column=col_num, value=header_text);
                cell.font = bold_font
                sheet.column_dimensions[get_column_letter(col_num)].width = 18 if col_num in [3, 4] else (
                    40 if col_num == 2 else 15)
            for entry in report_data.get('flat_entries', []):
                sheet.append([entry.get('account_number', ''), entry.get('account_name', ''), entry.get('debit', ZERO),
                              entry.get('credit', ZERO)])
                for col_idx in [3, 4]: sheet.cell(row=sheet.max_row, column=col_idx).number_format = '#,##0.00'
            sheet.append([]);
            total_row_idx = sheet.max_row + 1
            sheet.cell(row=total_row_idx, column=2, value="TOTALS:").font = bold_font
            sheet.cell(row=total_row_idx, column=3, value=report_data.get('total_debit', ZERO)).font = bold_font
            sheet.cell(row=total_row_idx, column=3).number_format = '#,##0.00'
            sheet.cell(row=total_row_idx, column=4, value=report_data.get('total_credit', ZERO)).font = bold_font
            sheet.cell(row=total_row_idx, column=4).number_format = '#,##0.00'
            if not report_data.get('is_balanced', True):
                sheet.cell(row=total_row_idx + 1, column=1,
                           value="WARNING: Trial Balance is NOT balanced!").font = Font(color="FF0000", bold=True)
            response_stream = BytesIO()
            workbook.save(response_stream);
            response_stream.seek(0)
            response = HttpResponse(response_stream.read(),
                                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            filename = f"{company_name_for_filename}_Trial_Balance_{period.name.replace(' ', '_')}_{as_of_date_for_report.strftime('%Y%m%d')}.xlsx"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response

        except DjangoValidationError as ve:
            self.message_user(request, f"Export failed due to validation: {ve.messages_joined}", messages.ERROR)
        except Exception as e:
            logger.exception(f"Error exporting Trial Balance for Co {company_id_for_report}, Period {period.pk}: {e}")
            self.message_user(request, f"An unexpected error occurred during export: {str(e)}", messages.ERROR)