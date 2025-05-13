# crp_accounting/admin_views.py

import logging
from datetime import date  # Removed unused datetime
from decimal import Decimal
from io import BytesIO
from typing import Union, Any, List, Dict, Optional

from django.contrib import messages
from django.shortcuts import render, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.admin import site as admin_site
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied
from django.http import HttpRequest, HttpResponse, Http404, HttpResponseBadRequest
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

# --- Model Imports ---
from .models.coa import Account
from .models.journal import VoucherType

# --- Company Model Import ---
try:
    from company.models import Company
except ImportError:
    Company = None
    logging.critical("Admin Views: CRITICAL - Could not import Company model. Tenant context will fail.")

# --- Enum Imports ---
from crp_core.enums import AccountNature

# --- PDF/Excel Imports ---
try:
    from xhtml2pdf import pisa

    XHTML2PDF_AVAILABLE = True
except ImportError:
    XHTML2PDF_AVAILABLE = False
    logging.warning("Admin Views: xhtml2pdf library not found. PDF export will not be available.")
try:
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    logging.warning("Admin Views: openpyxl library not found. Excel export will not be available.")

# --- Service Imports ---
from .services import reports_service, ledger_service
from .exceptions import ReportGenerationError

logger = logging.getLogger("crp_accounting.admin_views")
ZERO = Decimal('0.00')
PK_TYPE = Union[int, str, 'uuid.UUID']  # Not directly used in this version but good for type hints


# =========================================
# Helper Functions
# =========================================
def _get_admin_base_context(title: str, request: HttpRequest) -> Dict[str, Any]:
    resolver_match_opts = getattr(request.resolver_match, 'opts', None)
    current_app = getattr(request.resolver_match, 'app_name', admin_site.name)
    return {
        'title': title,  # Assumed to be str() before passing
        'site_header': admin_site.site_header,
        'site_title': admin_site.site_title,
        'opts': resolver_match_opts or Account._meta,
        'has_permission': True,
        'is_popup': request.GET.get('_popup') == '1',
        'is_nav_sidebar_enabled': not (request.GET.get('_popup') == '1'),
        'current_app': current_app,
        'user': request.user,
    }


def _get_company_for_report_or_raise(request: HttpRequest) -> Company:
    log_prefix = f"[GetCoForReport][User:{getattr(request.user, 'username', request.user.id)}]"
    if not Company:
        logger.critical(f"{log_prefix} Company model not available.")
        raise Http404(_("System configuration error: Company module not available."))

    company_from_middleware = getattr(request, 'company', None)

    if request.user.is_superuser:
        company_id_str = request.GET.get('company_id')
        if company_id_str:
            try:
                company_pk = Company._meta.pk.to_python(company_id_str)
                selected_company = Company.objects.get(pk=company_pk)
                logger.info(
                    f"{log_prefix} SU using company '{selected_company.name}' (PK:{selected_company.pk}) from GET 'company_id'.")
                return selected_company
            except (Company.DoesNotExist, ValueError, TypeError) as e:
                logger.warning(f"{log_prefix} SU provided invalid company_id '{company_id_str}'. Error: {e}")
                raise Http404(_("Company specified by ID '%(id)s' not found or invalid.") % {'id': company_id_str})
        if isinstance(company_from_middleware, Company):
            logger.info(
                f"{log_prefix} SU using company '{company_from_middleware.name}' (PK:{company_from_middleware.pk}) from request.company.")
            return company_from_middleware
        logger.info(f"{log_prefix} SU: No 'company_id' in GET and no company from middleware. Prompting.")
        raise ValueError(_("Superuser: 'Company ID' parameter required. Please select a company to view reports."))
    else:  # Non-superuser
        if not isinstance(company_from_middleware, Company):
            logger.warning(
                f"{log_prefix} Non-SU: No valid company context (request.company is '{type(company_from_middleware)}').")
            raise DjangoPermissionDenied(_("No active company associated with your session. Access denied."))
        logger.info(
            f"{log_prefix} Non-SU using company '{company_from_middleware.name}' (PK:{company_from_middleware.pk}) from request.company.")
        return company_from_middleware


def _render_to_pdf(template_src: str, context_dict: Optional[Dict[str, Any]] = None) -> BytesIO:
    if not XHTML2PDF_AVAILABLE:
        logger.error("Attempted PDF generation but xhtml2pdf library is not installed.")
        raise ImportError("xhtml2pdf library is required for PDF generation. Please install it.")
    if context_dict is None: context_dict = {}
    html_content = render_to_string(template_src, context_dict)
    result_buffer = BytesIO()
    pdf_status = pisa.CreatePDF(BytesIO(html_content.encode("UTF-8")), dest=result_buffer, encoding='utf-8')
    if pdf_status.err:
        logger.error(f"PDF generation error (code {pdf_status.err}) for template {template_src}.")
        raise Exception(f"PDF generation failed (code {pdf_status.err}). Check server logs for details.")
    result_buffer.seek(0)
    return result_buffer


def _write_bs_hierarchy_excel(
        sheet: Any, nodes: List[Dict[str, Any]], start_row: int,
        level_offset: int = 0, currency_symbol: str = ""
) -> int:
    current_row = start_row
    bold_font = Font(bold=True)
    standard_number_format = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'
    for node in nodes:
        indentation = "    " * (node.get('level', 0) + level_offset)
        node_name = str(node.get('name', 'N/A'))  # Ensure string for openpyxl
        name_cell_value = f"{indentation}{node_name}"
        name_cell = sheet.cell(row=current_row, column=1, value=name_cell_value)

        balance_value = node.get('balance', ZERO)
        balance_cell = sheet.cell(row=current_row, column=2, value=balance_value)
        balance_cell.number_format = standard_number_format
        balance_cell.alignment = Alignment(horizontal='right')

        if node.get('type') == 'group' or node.get('is_total', False):
            name_cell.font = bold_font
            balance_cell.font = bold_font

        current_row += 1
        child_nodes = node.get('children')
        if child_nodes:
            current_row = _write_bs_hierarchy_excel(sheet, child_nodes, current_row, level_offset + 1, currency_symbol)
    return current_row


# =========================================
# HTML Report Views
# =========================================
@staff_member_required
def admin_trial_balance_view(request: HttpRequest) -> HttpResponse:
    context = _get_admin_base_context(str(_("Trial Balance Report")), request)
    target_company: Optional[Company] = None
    as_of_date_str = request.GET.get('as_of_date')

    context['as_of_date_param'] = as_of_date_str or date.today().isoformat()
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{str(_('Trial Balance'))} - {target_company.name}"

        if as_of_date_str:
            try:
                as_of_date_val = date.fromisoformat(as_of_date_str)
                context['as_of_date_to_display'] = as_of_date_val
            except ValueError:
                messages.error(request, str(_("Invalid date format for 'As of Date'.")))
                return render(request, 'admin/crp_accounting/reports/trial_balance_report.html', context)

            report_data = reports_service.generate_trial_balance_structured(
                company_id=target_company.id, as_of_date=as_of_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True
            if not report_data.get('is_balanced'):
                messages.warning(request, _("The Trial Balance is out of balance!"))
        elif 'company_id' in request.GET:  # Company selected, but no date
            messages.info(request, str(_("Please select an 'As of Date' to generate the Trial Balance.")))

    except ValueError as ve:  # Catches _get_company_for_report_or_raise or date.fromisoformat (if date_str was invalid and not caught above)
        messages.error(request, str(ve));
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True;
            context['title'] = str(_('Trial Balance - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e));
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}");
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin TB for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, str(_("An unexpected error occurred.")));
        context['report_error'] = str(_("An unexpected error occurred."))

    return render(request, 'admin/crp_accounting/reports/trial_balance_report.html', context)


@staff_member_required
def admin_profit_loss_view(request: HttpRequest) -> HttpResponse:
    context = _get_admin_base_context(str(_("Profit & Loss Statement")), request)
    target_company: Optional[Company] = None
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    today = date.today()
    context['start_date_param'] = start_date_str or today.replace(day=1).isoformat()
    context['end_date_param'] = end_date_str or today.isoformat()
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{str(_('Profit & Loss Statement'))} - {target_company.name}"

        if start_date_str and end_date_str:
            try:
                start_date_val = date.fromisoformat(start_date_str)
                end_date_val = date.fromisoformat(end_date_str)
                context['start_date_to_display'] = start_date_val
                context['end_date_to_display'] = end_date_val
            except ValueError:
                messages.error(request, str(_("Invalid date format for period.")))
                return render(request, 'admin/crp_accounting/reports/profit_loss_report.html', context)

            if start_date_val > end_date_val:
                messages.error(request, str(_("Start date cannot be after end date.")))
                return render(request, 'admin/crp_accounting/reports/profit_loss_report.html',
                              context)  # Render with error

            report_data = reports_service.generate_profit_loss(
                company_id=target_company.id, start_date=start_date_val, end_date=end_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True
        elif 'company_id' in request.GET:
            messages.info(request, str(_("Please select a 'From' and 'To' date to generate the P&L.")))

    except ValueError as ve:
        messages.error(request, str(ve));
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True;
            context['title'] = str(_('Profit & Loss - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e));
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}");
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin P&L for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, str(_("An unexpected error occurred.")));
        context['report_error'] = str(_("An unexpected error occurred."))

    return render(request, 'admin/crp_accounting/reports/profit_loss_report.html', context)


@staff_member_required
def admin_balance_sheet_view(request: HttpRequest) -> HttpResponse:
    context = _get_admin_base_context(str(_("Balance Sheet")), request)
    target_company: Optional[Company] = None
    as_of_date_str = request.GET.get('as_of_date')
    context['as_of_date_param'] = as_of_date_str or date.today().isoformat()
    context['report_data_available'] = False
    context['layout_preference'] = request.GET.get('layout', 'horizontal')
    if context['layout_preference'] not in ['horizontal', 'vertical']: context['layout_preference'] = 'horizontal'

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{str(_('Balance Sheet'))} - {target_company.name}"

        if as_of_date_str:
            try:
                as_of_date_val = date.fromisoformat(as_of_date_str)
                context['as_of_date_to_display'] = as_of_date_val
            except ValueError:
                messages.error(request, str(_("Invalid date format for 'As of Date'.")))
                return render(request, 'admin/crp_accounting/reports/balance_sheet_report.html', context)

            report_data = reports_service.generate_balance_sheet(
                company_id=target_company.id, as_of_date=as_of_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True
            if not report_data.get('is_balanced'):
                messages.warning(request, _("The Balance Sheet is out of balance!"))
            if 'assets' in report_data and 'liabilities' in report_data and 'equity' in report_data:
                context['balance_difference'] = report_data['assets'].get('total', ZERO) - \
                                                (report_data['liabilities'].get('total', ZERO) + report_data[
                                                    'equity'].get('total', ZERO))
        elif 'company_id' in request.GET:
            messages.info(request, str(_("Please select an 'As of Date' to generate the Balance Sheet.")))

    except ValueError as ve:
        messages.error(request, str(ve));
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True;
            context['title'] = str(_('Balance Sheet - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e));
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}");
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin BS for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, str(_("An unexpected error occurred.")));
        context['report_error'] = str(_("An unexpected error occurred."))

    return render(request, 'admin/crp_accounting/reports/balance_sheet_report.html', context)


@staff_member_required
def admin_account_ledger_view(request: HttpRequest, account_pk: Any) -> HttpResponse:
    context = _get_admin_base_context(str(_("Account Ledger")), request)
    target_company: Optional[Company] = None
    target_account: Optional[Account] = None
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    today = date.today()
    context['start_date_param'] = start_date_str or today.replace(day=1).isoformat()
    context['end_date_param'] = end_date_str or today.isoformat()
    context['report_data_available'] = False

    try:
        if request.user.is_superuser:
            account_manager = Account.global_objects if hasattr(Account, 'global_objects') else Account.objects
            target_account = get_object_or_404(account_manager.select_related('company', 'account_group'),
                                               pk=account_pk)
            if not target_account.company: raise Http404(_("Account is not properly associated with a company."))
            target_company = target_account.company
        else:
            company_from_request = getattr(request, 'company', None)
            if not isinstance(company_from_request, Company): raise DjangoPermissionDenied(
                _("No active company associated with your session."))
            target_company = company_from_request
            target_account = get_object_or_404(Account.objects.select_related('company', 'account_group'),
                                               pk=account_pk, company=target_company)

        context['company'] = target_company
        context['account'] = target_account
        context[
            'title'] = f"{str(_('Ledger'))}: {target_account.account_name} ({target_account.account_number}) - {target_company.name}"

        if start_date_str and end_date_str:
            try:
                start_date_val = date.fromisoformat(start_date_str)
                end_date_val = date.fromisoformat(end_date_str)
                context['start_date_to_display'] = start_date_val
                context['end_date_to_display'] = end_date_val
            except ValueError:
                messages.error(request, str(_("Invalid date format for ledger period.")))
                return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)

            if start_date_val > end_date_val:
                messages.error(request, str(_("Start date cannot be after end date.")))
                return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)

            ledger_data = ledger_service.get_account_ledger_data(
                company_id=target_company.id, account_pk=target_account.pk,
                start_date=start_date_val, end_date=end_date_val
            )
            is_debit_nature = target_account.account_nature == AccountNature.DEBIT.value

            def _format_balance_display(bal: Decimal) -> Dict[str, Any]:
                amt = abs(bal);
                dr_cr = ""
                if bal != ZERO: dr_cr = ("Dr" if bal > ZERO else "Cr") if is_debit_nature else (
                    "Cr" if bal > ZERO else "Dr")
                return {'amount': amt, 'dr_cr': dr_cr}

            context['opening_balance_display'] = _format_balance_display(ledger_data.get('opening_balance', ZERO))
            context['closing_balance_display'] = _format_balance_display(ledger_data.get('closing_balance', ZERO))
            processed_entries = []
            running_bal_for_display = ledger_data.get('opening_balance', ZERO)
            for entry in ledger_data.get('entries', []):
                debit, credit = entry.get('debit', ZERO), entry.get('credit', ZERO)
                running_bal_for_display += (debit - credit) if is_debit_nature else (credit - debit)
                vt_label = str(VoucherType(entry['voucher_type']).label) if entry.get('voucher_type') and entry[
                    'voucher_type'] in VoucherType.values else ""
                processed_entries.append(
                    {**entry, 'running_balance_display': _format_balance_display(running_bal_for_display),
                     'voucher_type_display': vt_label})
            context['entries'] = processed_entries
            context['total_debit'] = ledger_data.get('total_debit', ZERO);
            context['total_credit'] = ledger_data.get('total_credit', ZERO)
            context['report_currency'] = target_account.currency
            context['report_data_available'] = True
        elif context.get('company') and context.get('account'):  # Only if company and account are resolved
            messages.info(request, str(_("Please select a 'From' and 'To' date to view the ledger details.")))

    except DjangoPermissionDenied as e:
        messages.error(request, str(e)); context['report_error'] = str(e)
    except Http404:
        messages.error(request, _("Account or Company not found for ledger.")); context['report_error'] = _(
            "Account not found for ledger.")
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Ledger error'))}: {rge}"); context[
            'report_error'] = f"{str(_('Ledger error'))}: {rge}"
    except Exception as e:
        logger.exception(f"Unexpected error generating Account Ledger for AccPK {account_pk}")
        messages.error(request, _("An unexpected error occurred."));
        context['report_error'] = _("An unexpected error occurred.")
    return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)


# =========================================
# EXCEL/PDF Download Views
# =========================================
def _get_validated_date_params_for_download(request: HttpRequest, date_param_names: List[str]) -> Dict[str, date]:
    dates: Dict[str, date] = {}
    for param_name in date_param_names:
        date_str = request.GET.get(param_name)
        if not date_str:
            raise ValueError(
                str(_("Missing required date parameter '%(param)s' for download.")) % {'param': param_name})
        try:
            dates[param_name] = date.fromisoformat(date_str)
        except ValueError:
            raise ValueError(str(_("Invalid date format for parameter '%(param)s': %(value)s.")) % {'param': param_name,
                                                                                                    'value': date_str})
    return dates


@staff_member_required
def download_trial_balance_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE: return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")),
                                                   status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']

        report_data = reports_service.generate_trial_balance_structured(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook();
        sheet = workbook.active
        sheet.title = str(_("Trial Balance"))
        currency_symbol = getattr(target_company, 'default_currency_symbol', '')

        sheet['A1'] = f"{str(_('Trial Balance Report'))} - {target_company.name}"
        sheet.merge_cells('A1:D1');
        sheet['A1'].font = Font(bold=True, size=14);
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells('A2:D2');
        sheet['A2'].alignment = Alignment(horizontal='center');
        sheet.append([])
        headers = [str(_("Account Number")), str(_("Account Name")), str(_("Debit")), str(_("Credit"))]
        sheet.append(headers);
        header_row_num = sheet.max_row
        for col_idx, cell_value in enumerate(headers, 1):
            cell = sheet.cell(row=header_row_num, column=col_idx);
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style='thin'));
            cell.alignment = Alignment(horizontal='center' if col_idx != 2 else 'left')
            sheet.column_dimensions[get_column_letter(col_idx)].width = (
                20 if col_idx == 1 else 35 if col_idx == 2 else 18)
        sheet.cell(row=header_row_num, column=3).alignment = Alignment(horizontal='right');
        sheet.cell(row=header_row_num, column=4).alignment = Alignment(horizontal='right')
        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'
        for entry in report_data.get('flat_entries', []):
            if entry.get('debit', ZERO) == ZERO and entry.get('credit', ZERO) == ZERO and not entry.get(
                'is_group'): continue
            row_values = [entry.get('account_number', ''), str(entry.get('account_name', 'N/A')),
                          entry.get('debit', ZERO), entry.get('credit', ZERO)]
            sheet.append(row_values);
            current_row = sheet.max_row
            sheet.cell(row=current_row, column=3).number_format = number_format_currency;
            sheet.cell(row=current_row, column=4).number_format = number_format_currency
            if entry.get('is_group'):
                for col_idx_group in range(1, 5): sheet.cell(row=current_row, column=col_idx_group).font = Font(
                    bold=True)  # Corrected variable name
        sheet.append([]);
        sheet.append(
            [None, str(_("Totals")), report_data.get('total_debit', ZERO), report_data.get('total_credit', ZERO)])
        total_row_num = sheet.max_row
        for col_idx_total in [2, 3, 4]: sheet.cell(row=total_row_num, column=col_idx_total).font = Font(
            bold=True)  # Corrected variable name
        sheet.cell(row=total_row_num, column=3).number_format = number_format_currency;
        sheet.cell(row=total_row_num, column=4).number_format = number_format_currency
        sheet.cell(row=total_row_num, column=3).alignment = Alignment(horizontal='right');
        sheet.cell(row=total_row_num, column=4).alignment = Alignment(horizontal='right')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Trial_Balance_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"';
        workbook.save(response);
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(
            f"Error exporting Trial Balance Excel for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("An unexpected error occurred during Excel generation.")), status=500)


@staff_member_required
def download_profit_loss_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE: return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")),
                                                   status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['start_date', 'end_date'])
        start_date_val, end_date_val = date_params['start_date'], date_params['end_date']
        if start_date_val > end_date_val: raise ValueError(str(_("Start date cannot be after end date.")))

        report_data = reports_service.generate_profit_loss(
            company_id=target_company.id, start_date=start_date_val, end_date=end_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook();
        sheet = workbook.active
        sheet.title = str(_("Profit & Loss"))
        currency_symbol = getattr(target_company, 'default_currency_symbol', '');
        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'
        sheet['A1'] = f"{str(_('Profit & Loss Statement'))} - {target_company.name}"
        sheet.merge_cells('A1:C1');
        sheet['A1'].font = Font(bold=True, size=14);
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet[
            'A2'] = f"{str(_('For the period:'))} {start_date_val.strftime('%B %d, %Y')} {str(_('to'))} {end_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells('A2:C2');
        sheet['A2'].alignment = Alignment(horizontal='center');
        sheet.append([])
        headers = [str(_("Description")), str(_("Amount")), str(_("Details"))]
        sheet.append(headers);
        header_row_num = sheet.max_row
        for col_idx, cell_val_pl in enumerate(headers, 1):  # Corrected variable name
            cell = sheet.cell(row=header_row_num, column=col_idx);
            cell.font = Font(bold=True);
            cell.border = Border(bottom=Side(style='thin'))
            cell.alignment = Alignment(horizontal='left' if col_idx == 1 else 'right')
        sheet.column_dimensions[get_column_letter(1)].width = 45;
        sheet.column_dimensions[get_column_letter(2)].width = 20;
        sheet.column_dimensions[get_column_letter(3)].width = 30
        for item in report_data.get('report_lines', []):
            is_subtotal_or_net = item.get('is_subtotal', False) or item.get('section_key') == 'NET_INCOME'
            sheet.append([str(item.get('title', 'N/A')), item.get('amount', ZERO), None]);
            current_row_num = sheet.max_row
            desc_cell = sheet.cell(row=current_row_num, column=1);
            amt_cell = sheet.cell(row=current_row_num, column=2)
            desc_cell.font = Font(bold=is_subtotal_or_net);
            amt_cell.font = Font(bold=is_subtotal_or_net);
            amt_cell.number_format = number_format_currency
            if not item.get('is_subtotal', False) and item.get('accounts'):
                for acc_detail in item.get('accounts', []):
                    acc_name = f"    {acc_detail.get('account_number', '')} - {str(acc_detail.get('account_name', 'N/A'))}"
                    sheet.append([acc_name, None, acc_detail.get('amount', ZERO)])
                    sheet.cell(row=sheet.max_row, column=3).number_format = number_format_currency
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Profit_Loss_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"';
        workbook.save(response);
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(
            f"Error exporting P&L Excel for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("Excel generation error.")), status=500)


@staff_member_required
def download_balance_sheet_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE: return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")),
                                                   status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']
        report_data = reports_service.generate_balance_sheet(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook();
        sheet = workbook.active
        sheet.title = str(_("Balance Sheet"))
        currency_symbol = getattr(target_company, 'default_currency_symbol', '');
        number_format_currency_bold = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'
        sheet['A1'] = f"{str(_('Balance Sheet'))} - {target_company.name}";
        sheet.merge_cells('A1:B1');
        sheet['A1'].font = Font(bold=True, size=14);
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}";
        sheet.merge_cells('A2:B2');
        sheet['A2'].alignment = Alignment(horizontal='center')
        sheet.column_dimensions['A'].width = 50;
        sheet.column_dimensions['B'].width = 20
        current_row = 3  # Start after main headers

        current_row += 1;
        sheet.cell(row=current_row, column=1, value=str(_("Assets"))).font = Font(bold=True, size=12)
        current_row += 1;
        assets_data = report_data.get('assets', {});
        current_row = _write_bs_hierarchy_excel(sheet, assets_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)
        sheet.cell(row=current_row, column=1, value=str(_("Total Assets"))).font = Font(bold=True);
        total_assets_cell = sheet.cell(row=current_row, column=2, value=assets_data.get('total', ZERO));
        total_assets_cell.font = Font(bold=True);
        total_assets_cell.number_format = number_format_currency_bold;
        total_assets_cell.alignment = Alignment(horizontal='right');
        current_row += 2

        sheet.cell(row=current_row, column=1, value=str(_("Liabilities"))).font = Font(bold=True, size=12)
        current_row += 1;
        liabilities_data = report_data.get('liabilities', {});
        current_row = _write_bs_hierarchy_excel(sheet, liabilities_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)
        sheet.cell(row=current_row, column=1, value=str(_("Total Liabilities"))).font = Font(bold=True);
        total_liab_cell = sheet.cell(row=current_row, column=2, value=liabilities_data.get('total', ZERO));
        total_liab_cell.font = Font(bold=True);
        total_liab_cell.number_format = number_format_currency_bold;
        total_liab_cell.alignment = Alignment(horizontal='right');
        current_row += 2

        sheet.cell(row=current_row, column=1, value=str(_("Equity"))).font = Font(bold=True, size=12)
        current_row += 1;
        equity_data = report_data.get('equity', {});
        current_row = _write_bs_hierarchy_excel(sheet, equity_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)
        sheet.cell(row=current_row, column=1, value=str(_("Total Equity"))).font = Font(bold=True);
        total_equity_cell = sheet.cell(row=current_row, column=2, value=equity_data.get('total', ZERO));
        total_equity_cell.font = Font(bold=True);
        total_equity_cell.number_format = number_format_currency_bold;
        total_equity_cell.alignment = Alignment(horizontal='right');
        current_row += 2

        total_liab_equity = liabilities_data.get('total', ZERO) + equity_data.get('total', ZERO)
        sheet.cell(row=current_row, column=1, value=str(_("Total Liabilities and Equity"))).font = Font(bold=True);
        total_liab_equity_cell = sheet.cell(row=current_row, column=2, value=total_liab_equity);
        total_liab_equity_cell.font = Font(bold=True);
        total_liab_equity_cell.number_format = number_format_currency_bold;
        total_liab_equity_cell.alignment = Alignment(horizontal='right')
        if not report_data.get('is_balanced', True): current_row += 2; warning_cell = sheet.cell(row=current_row,
                                                                                                 column=1, value=str(
                _("Note: Balance Sheet is Out of Balance!"))); warning_cell.font = Font(color="FF0000",
                                                                                        bold=True); sheet.merge_cells(
            start_row=current_row, start_column=1, end_row=current_row,
            end_column=2); warning_cell.alignment = Alignment(horizontal='center')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Balance_Sheet_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"';
        workbook.save(response);
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(
            f"Error exporting BS Excel for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("Excel generation error.")), status=500)


# PDF Download views
@staff_member_required
def download_trial_balance_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE: return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")),
                                                    status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']
        report_data = reports_service.generate_trial_balance_structured(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        context = {
            **_get_admin_base_context(f"{str(_('Trial Balance Report'))} - {target_company.name}", request),
            'company': target_company,
            'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(),  # For form prefill if template reuses
            'report_data_available': True,  # Assume data is available for PDF
            'is_pdf': True,
            **report_data
        }
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/trial_balance_report.html', context)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Trial_Balance_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"';
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except ImportError as ie:
        return HttpResponse(str(ie), status=501)
    except Exception as e:
        logger.exception(
            f"Error exporting TB PDF for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("PDF generation error.")), status=500)


@staff_member_required
def download_profit_loss_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE: return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")),
                                                    status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['start_date', 'end_date'])
        start_date_val, end_date_val = date_params['start_date'], date_params['end_date']
        if start_date_val > end_date_val: raise ValueError(str(_("Start date cannot be after end date.")))
        report_data = reports_service.generate_profit_loss(
            company_id=target_company.id, start_date=start_date_val, end_date=end_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        context = {
            **_get_admin_base_context(f"{str(_('Profit & Loss Statement'))} - {target_company.name}", request),
            'company': target_company,
            'start_date_to_display': start_date_val,
            'end_date_to_display': end_date_val,
            'start_date_param': start_date_val.isoformat(),
            'end_date_param': end_date_val.isoformat(),
            'report_data_available': True,
            'is_pdf': True,
            **report_data
        }
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/profit_loss_report.html', context)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Profit_Loss_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"';
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except ImportError as ie:
        return HttpResponse(str(ie), status=501)
    except Exception as e:
        logger.exception(
            f"Error exporting P&L PDF for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("PDF generation error.")), status=500)


@staff_member_required
def download_balance_sheet_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE: return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")),
                                                    status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']
        report_data = reports_service.generate_balance_sheet(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        context = {
            **_get_admin_base_context(f"{str(_('Balance Sheet'))} - {target_company.name}", request),
            'company': target_company,
            'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(),
            'report_data_available': True,
            'layout_preference': request.GET.get('layout', 'horizontal'),  # Pass layout preference
            'is_pdf': True,
            **report_data
        }
        # Ensure balance_difference is calculated for PDF context if needed by template
        if 'balance_difference' not in context and 'assets' in report_data and 'liabilities' in report_data and 'equity' in report_data:
            context['balance_difference'] = report_data['assets'].get('total', ZERO) - \
                                            (report_data['liabilities'].get('total', ZERO) + report_data['equity'].get(
                                                'total', ZERO))

        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/balance_sheet_report.html', context)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Balance_Sheet_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"';
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except ImportError as ie:
        return HttpResponse(str(ie), status=501)
    except Exception as e:
        logger.exception(
            f"Error exporting BS PDF for Co '{getattr(target_company, 'name', 'N/A')}'"); return HttpResponse(
            str(_("PDF generation error.")), status=500)