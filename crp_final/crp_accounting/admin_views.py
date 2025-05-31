import logging
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Union, Any, List, Dict, Optional
import os  # For link_callback

from django.conf import settings  # For link_callback
from django.contrib import messages
from django.shortcuts import render, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.admin import site as admin_site
from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied as DjangoPermissionDenied
from django.http import HttpRequest, HttpResponse, Http404, HttpResponseBadRequest  # Ensure HttpRequest is imported
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

# --- Model Imports ---
from .models.coa import Account
from .models.journal import VoucherType
from .models.party import Party
from .services.voucher_service import PERMISSIONS_MAP, get_permissions_for_role

# --- Company Model Import ---
try:
    from company.models import Company, CompanyMembership
except ImportError:
    Company = None
    logging.critical("Admin Views: CRITICAL - Could not import Company model. Tenant context will fail.")

# --- Enum Imports ---
from crp_core.enums import AccountNature, \
    PartyType as CorePartyType

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
PK_TYPE = Union[int, str, 'uuid.UUID']


# =========================================
# Helper Functions
# =========================================
def _get_admin_base_context(title: str, request: HttpRequest) -> Dict[str, Any]:
    resolver_match_opts = getattr(request.resolver_match, 'opts', None)
    current_app = getattr(request.resolver_match, 'app_name', admin_site.name)
    return {
        'title': title,
        'site_header': admin_site.site_header,
        'site_title': admin_site.site_title,
        'opts': resolver_match_opts or Account._meta,
        'has_permission': True,
        'is_popup': request.GET.get('_popup') == '1',
        'is_nav_sidebar_enabled': not (request.GET.get('_popup') == '1'),
        'current_app': current_app,
        'user': request.user,
        # 'request': request, # No need to add request here if render() or render_to_string(..., request=request) is used
    }


def _get_company_for_report_or_raise(request: HttpRequest) -> Company:
    log_prefix = f"[GetCoForReport][User:{getattr(request.user, 'name', request.user.id)}]"
    if not Company:
        logger.critical(f"{log_prefix} Company model not available.")
        raise Http404(_("System configuration error: Company module not available."))

    company_from_middleware = getattr(request, 'company', None)

    if request.user.is_superuser:
        company_id_str = request.GET.get('company_id')
        if company_id_str:
            try:
                company_pk = Company._meta.pk.to_python(company_id_str)
                selected_company = Company.objects.get(pk=company_pk, is_active=True)
                logger.info(
                    f"{log_prefix} SU using company '{selected_company.name}' (PK:{selected_company.pk}) from GET 'company_id'.")
                return selected_company
            except (Company.DoesNotExist, ValueError, TypeError) as e:
                logger.warning(f"{log_prefix} SU provided invalid company_id '{company_id_str}'. Error: {e}")
                raise Http404(
                    _("Company specified by ID '%(id)s' not found, invalid, or inactive.") % {'id': company_id_str})
        if isinstance(company_from_middleware, Company) and company_from_middleware.is_active:
            logger.info(
                f"{log_prefix} SU using company '{company_from_middleware.name}' (PK:{company_from_middleware.pk}) from request.company.")
            return company_from_middleware
        logger.info(f"{log_prefix} SU: No 'company_id' in GET and no active company from middleware. Prompting.")
        raise ValueError(
            _("Superuser: 'Company ID' parameter required or selected company is inactive. Please select an active company to view reports."))
    else:
        if not isinstance(company_from_middleware, Company) or not company_from_middleware.is_active:
            logger.warning(
                f"{log_prefix} Non-SU: No valid active company context (request.company is '{type(company_from_middleware)}').")
            raise DjangoPermissionDenied(
                _("No active company associated with your session, or company is inactive. Access denied."))
        logger.info(
            f"{log_prefix} Non-SU using company '{company_from_middleware.name}' (PK:{company_from_middleware.pk}) from request.company.")
        return company_from_middleware


def _default_link_callback(uri: str, rel: str) -> Optional[str]:
    """
    Default link callback for xhtml2pdf.
    Resolves local static and media files to absolute file system paths.
    """
    # Try to prevent issues if settings are not fully defined (e.g., during tests or partial setups)
    media_url = getattr(settings, 'MEDIA_URL', None)
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    static_url = getattr(settings, 'STATIC_URL', None)
    static_root = getattr(settings, 'STATIC_ROOT', None)  # This is what collectstatic uses

    path = None
    if media_url and uri.startswith(media_url) and media_root:
        path = os.path.join(media_root, uri.replace(media_url, "", 1))
    elif static_url and uri.startswith(static_url) and static_root:  # Check static_root first
        path = os.path.join(static_root, uri.replace(static_url, "", 1))
    elif static_url and uri.startswith(
            static_url):  # Fallback for resolving against STATICFILES_DIRS if static_root not primary
        # This part is trickier as STATICFILES_DIRS can have multiple paths
        # For simplicity, we'll try finding it in the first one or rely on static_root
        # A more robust solution might iterate settings.STATICFILES_DIRS
        from django.contrib.staticfiles import finders
        # finders.find will search through STATICFILES_DIRS and app 'static' directories
        absolute_path = finders.find(uri.replace(static_url, "", 1))
        if absolute_path:
            path = absolute_path

    if not path:  # If not resolved yet, handle other cases
        if os.path.isabs(uri):  # If it's already an absolute path
            path = uri
        else:
            logger.debug(
                f"xhtml2pdf link_callback: URI '{uri}' is not a local static/media asset or absolute path, passing through.")
            return uri  # Let pisa try to handle it (e.g. http:// links)

    path = os.path.normpath(path)

    # Basic security check (ensure it's within known safe roots if resolved from URL)
    safe_roots = []
    if static_root: safe_roots.append(os.path.normpath(static_root))
    if media_root: safe_roots.append(os.path.normpath(media_root))
    # Also consider STATICFILES_DIRS if used above for resolution
    for staticfiles_dir_path in getattr(settings, 'STATICFILES_DIRS', []):
        safe_roots.append(os.path.normpath(staticfiles_dir_path))

    is_safe_path = False
    if any(path.startswith(safe_root) for safe_root in safe_roots):
        is_safe_path = True

    if not is_safe_path and not os.path.isabs(uri):  # If original uri was not absolute and path is not safe
        logger.warning(
            f"xhtml2pdf link_callback: URI '{uri}' resolved to potentially unsafe path '{path}'. Denying access.")
        return None

    if os.path.isfile(path):
        return path

    logger.warning(f"xhtml2pdf link_callback: file not found for URI '{uri}' (resolved to '{path}')")
    return None


# MODIFIED: Added 'request: Optional[HttpRequest] = None' parameter
def _render_to_pdf(template_src: str, context_dict: Optional[Dict[str, Any]] = None,
                   request: Optional[HttpRequest] = None) -> BytesIO:
    if not XHTML2PDF_AVAILABLE:
        logger.error("Attempted PDF generation but xhtml2pdf library is not installed.")
        raise ImportError(_("xhtml2pdf library is required for PDF generation. Please install it."))

    if context_dict is None:
        context_dict = {}

    # Diagnostic log for context_dict['request'] state if it exists from report_data
    if 'request' in context_dict:
        if not isinstance(context_dict['request'], HttpRequest):
            logger.warning(
                f"_render_to_pdf: context_dict already contains a 'request' key, but it's a "
                f"{type(context_dict['request']).__name__}, not HttpRequest. "
                f"This should be correctly handled by passing the 'request' parameter to render_to_string."
            )
        # If context_dict['request'] IS an HttpRequest, it's fine too.
        # render_to_string will prioritize the explicitly passed 'request' argument if different.

    current_link_callback = _default_link_callback
    required_settings_for_callback = ['STATIC_URL', 'MEDIA_URL']  # STATIC_ROOT/MEDIA_ROOT checked in callback
    if not all(hasattr(settings, s_name) and getattr(settings, s_name) for s_name in required_settings_for_callback):
        logger.warning(
            "xhtml2pdf: One or more of STATIC_URL, MEDIA_URL "  # STATIC_ROOT/MEDIA_ROOT are also important
            "are not configured in Django settings. Local file paths in PDF (images, CSS) via "
            "link_callback might not work as expected."
        )
        # Consider if link_callback should be disabled or simplified if settings are missing
        # current_link_callback = None # For example

    # MODIFIED: Pass the 'request' object to render_to_string
    html_content = render_to_string(template_src, context_dict, request=request)
    result_buffer = BytesIO()

    try:
        pdf_status = pisa.CreatePDF(
            BytesIO(html_content.encode("UTF-8")),
            dest=result_buffer,
            encoding='utf-8',
            link_callback=current_link_callback
        )
    except Exception as e:
        logger.exception(
            f"Exception during pisa.CreatePDF for template {template_src}. "
            f"HTML (first 500 chars): {html_content[:500]}"
        )
        raise Exception(
            _("PDF generation failed due to an internal error in the PDF library: %(error)s") % {'error': str(e)})

    if pdf_status.err:
        error_message = f"PDF generation error (code {pdf_status.err}) for template {template_src}."
        logger.error(f"{error_message} HTML (first 500 chars): {html_content[:500]}")
        # You can add more detailed pisa log messages here if needed
        # pisa_log = getattr(pdf_status, "log", []) # Check actual attribute name in pisa.document.Document
        # for msg_type, msg_val, msg_obj_id, msg_text in pisa_log:
        #     if msg_type >= logging.WARNING: # Example to log warnings and errors from pisa
        #         logger.error(f"PISA Log: Type={msg_type}, Value={msg_val}, Text={msg_text}")
        raise Exception(
            _("PDF generation failed (code %(code)s). Check server logs. "
              "This often indicates issues with HTML/CSS structure or unresolvable linked resources.") % {
                'code': pdf_status.err}
        )

    result_buffer.seek(0)
    return result_buffer


def _write_bs_hierarchy_excel(
        sheet: Any, nodes: List[Dict[str, Any]], start_row: int,
        level_offset: int = 0, currency_symbol: str = ""
) -> int:
    current_row = start_row
    bold_font = Font(bold=True)
    # Ensure currency_symbol is not None for formatting
    actual_currency_symbol = currency_symbol if currency_symbol is not None else ""
    standard_number_format = f'"{actual_currency_symbol}"#,##0.00;[Red]-"{actual_currency_symbol}"#,##0.00'
    for node in nodes:
        indentation = "    " * (node.get('level', 0) + level_offset)
        node_name = str(node.get('name', 'N/A'))
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
            current_row = _write_bs_hierarchy_excel(sheet, child_nodes, current_row, level_offset + 1,
                                                    currency_symbol)  # Pass original symbol
    return current_row


def _get_validated_date_params_for_download(request: HttpRequest, date_param_names: List[str]) -> Dict[str, date]:
    dates: Dict[str, date] = {}
    for param_name in date_param_names:
        date_str = request.GET.get(param_name)
        if not date_str:
            raise ValueError(
                _("Missing required date parameter '%(param)s' for download.") % {'param': param_name})
        try:
            dates[param_name] = date.fromisoformat(date_str)
        except ValueError:
            raise ValueError(
                _("Invalid date format for parameter '%(param)s': %(value)s.") % {'param': param_name,
                                                                                  'value': date_str})
    return dates


# =========================================
# HTML Report Views (No changes here needed for this specific error)
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
                messages.error(request, _("Invalid date format for 'As of Date'."))
                return render(request, 'admin/crp_accounting/reports/trial_balance_report.html', context)

            report_data = reports_service.generate_trial_balance_structured(
                company_id=target_company.id, as_of_date=as_of_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True
            if not report_data.get('is_balanced'):
                messages.warning(request, _("The Trial Balance is out of balance!"))
        elif 'company_id' in request.GET:
            messages.info(request, _("Please select an 'As of Date' to generate the Trial Balance."))

    except ValueError as ve:
        messages.error(request, str(ve))
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
            context['title'] = str(_('Trial Balance - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}")
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin TB for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, _("An unexpected error occurred."))
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
                messages.error(request, _("Invalid date format for period."))
                return render(request, 'admin/crp_accounting/reports/profit_loss_report.html', context)

            if start_date_val > end_date_val:
                messages.error(request, _("Start date cannot be after end date."))
                return render(request, 'admin/crp_accounting/reports/profit_loss_report.html', context)

            report_data = reports_service.generate_profit_loss(
                company_id=target_company.id, start_date=start_date_val, end_date=end_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True
        elif 'company_id' in request.GET:
            messages.info(request, _("Please select a 'From' and 'To' date to generate the P&L."))

    except ValueError as ve:
        messages.error(request, str(ve))
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
            context['title'] = str(_('Profit & Loss - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}")
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin P&L for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, _("An unexpected error occurred."))
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
                messages.error(request, _("Invalid date format for 'As of Date'."))
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
            messages.info(request, _("Please select an 'As of Date' to generate the Balance Sheet."))

    except ValueError as ve:
        messages.error(request, str(ve))
        context['report_error'] = str(ve)
        if request.user.is_superuser and ("Company ID" in str(ve) or "select a company" in str(ve).lower()):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
            context['title'] = str(_('Balance Sheet - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating report'))}: {rge}")
        context['report_error'] = f"{str(_('Error generating report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin BS for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, _("An unexpected error occurred."))
        context['report_error'] = str(_("An unexpected error occurred."))

    return render(request, 'admin/crp_accounting/reports/balance_sheet_report.html', context)


@staff_member_required
def admin_account_ledger_view(request: HttpRequest, account_pk: PK_TYPE) -> HttpResponse:
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
            company_id_from_get = request.GET.get('company_id')
            if company_id_from_get:
                target_company = get_object_or_404(Company, pk=company_id_from_get, is_active=True)
                target_account = get_object_or_404(Account.objects.select_related('company', 'account_group'),
                                                   pk=account_pk, company=target_company)
            else:
                target_account = get_object_or_404(Account.objects.select_related('company', 'account_group'),
                                                   pk=account_pk)
                if not target_account.company or not target_account.company.is_active:
                    raise Http404(_("Account is not associated with an active company, or company context is missing."))
                target_company = target_account.company
        else:
            company_from_request = getattr(request, 'company', None)
            if not isinstance(company_from_request, Company) or not company_from_request.is_active:
                raise DjangoPermissionDenied(
                    _("No active company associated with your session or company is inactive."))
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
                messages.error(request, _("Invalid date format for ledger period."))
                return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)

            if start_date_val > end_date_val:
                messages.error(request, _("Start date cannot be after end date."))
                return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)

            ledger_data = ledger_service.get_account_ledger_data(
                company_id=target_company.id, account_pk=target_account.pk,
                start_date=start_date_val, end_date=end_date_val
            )
            is_debit_nature = target_account.account_nature == AccountNature.DEBIT.value

            def _format_balance_display(bal: Decimal) -> Dict[str, Any]:
                amt = abs(bal)
                dr_cr_indicator = ""
                if bal != ZERO:
                    dr_cr_indicator = ("Dr" if bal > ZERO else "Cr") if is_debit_nature else \
                        ("Cr" if bal > ZERO else "Dr")
                return {'amount': amt, 'dr_cr': dr_cr_indicator}

            context['opening_balance_display'] = _format_balance_display(ledger_data.get('opening_balance', ZERO))
            context['closing_balance_display'] = _format_balance_display(ledger_data.get('closing_balance', ZERO))
            processed_entries = []
            running_bal_for_display = ledger_data.get('opening_balance', ZERO)
            for entry in ledger_data.get('entries', []):
                debit, credit = entry.get('debit', ZERO), entry.get('credit', ZERO)
                if is_debit_nature:
                    running_bal_for_display += (debit - credit)
                else:
                    running_bal_for_display += (credit - debit)

                vt_label = str(VoucherType(entry['voucher_type']).label) if entry.get('voucher_type') and entry[
                    'voucher_type'] in VoucherType._value2member_map_ else ""
                processed_entries.append(
                    {**entry, 'running_balance_display': _format_balance_display(running_bal_for_display),
                     'voucher_type_display': vt_label})
            context['entries'] = processed_entries
            context['total_debit'] = ledger_data.get('total_debit', ZERO)
            context['total_credit'] = ledger_data.get('total_credit', ZERO)
            context['report_currency'] = target_account.currency
            context['report_data_available'] = True
        elif context.get('company') and context.get('account'):
            messages.info(request, _("Please select a 'From' and 'To' date to view the ledger details."))

    except DjangoPermissionDenied as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except Http404:
        messages.error(request,
                       _("Account or Company not found for ledger. Ensure company context is correct if superuser."))
        context['report_error'] = _("Account not found for ledger.")
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Ledger error'))}: {rge}")
        context[
            'report_error'] = f"{str(_('Ledger error'))}: {rge}"
    except Exception as e:
        logger.exception(f"Unexpected error generating Account Ledger for AccPK {account_pk}")
        messages.error(request, _("An unexpected error occurred."))
        context['report_error'] = _("An unexpected error occurred.")
    return render(request, 'admin/crp_accounting/reports/account_ledger_report.html', context)


@staff_member_required
def admin_ar_aging_report_view(request: HttpRequest) -> HttpResponse:
    context = _get_admin_base_context(str(_("AR Aging Report")), request)
    target_company: Optional[Company] = None
    today = date.today()

    context['as_of_date_param'] = request.GET.get('as_of_date', today.isoformat())
    context['aging_bucket_days_config_str'] = ",".join(
        map(str, reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS))
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{str(_('AR Aging Report'))} - {target_company.name}"

        as_of_date_val = date.fromisoformat(context['as_of_date_param'])

        buckets_str_from_get = request.GET.get('aging_buckets')
        if buckets_str_from_get:
            try:
                aging_buckets_to_use = [int(b.strip()) for b in buckets_str_from_get.split(',') if b.strip()]
                context['aging_bucket_days_config_str'] = buckets_str_from_get
            except ValueError:
                messages.error(request, _("Invalid format for aging buckets. Using default."))
                aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS
        else:
            aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS

        report_data = reports_service.generate_ar_aging_report(
            company_id=target_company.id,
            as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )
        context.update(report_data)
        context['report_data_available'] = True

    except ValueError as ve:
        messages.error(request, str(ve))
        context['report_error'] = str(ve)
        if "Company ID" in str(ve) and request.user.is_superuser:
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
            context['title'] = str(_('AR Aging Report - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating AR Aging report'))}: {rge}")
        context['report_error'] = f"{str(_('Error generating AR Aging report'))}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin AR Aging Report for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, _("An unexpected error occurred while generating the AR Aging report."))
        context['report_error'] = str(_("An unexpected error occurred."))

    if 'opts' not in context: context['opts'] = Company._meta if Company else Account._meta

    return render(request, 'admin/crp_accounting/reports/ar_aging_report.html', context)


@staff_member_required
def admin_customer_statement_view(request: HttpRequest, customer_pk: Optional[PK_TYPE] = None) -> HttpResponse:
    context = _get_admin_base_context(str(_("Customer Statement")), request)
    target_company: Optional[Company] = None
    target_customer: Optional[Party] = None
    today = date.today()
    default_start_date = today.replace(day=1)
    next_month = default_start_date.replace(day=28) + timedelta(days=4)
    default_end_date = next_month - timedelta(days=next_month.day)

    context['start_date_param'] = request.GET.get('start_date', default_start_date.isoformat())
    context['end_date_param'] = request.GET.get('end_date', default_end_date.isoformat())
    customer_pk_from_get = request.GET.get('customer_id')
    effective_customer_pk = customer_pk or customer_pk_from_get
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{str(_('Customer Statement'))} - {target_company.name}"

        if target_company:
            context['customers_for_selection'] = Party.objects.filter(
                company=target_company,
                party_type=CorePartyType.CUSTOMER.value,
                is_active=True
            ).order_by('name')

        if not effective_customer_pk:
            if 'company_id' in request.GET and not customer_pk_from_get:
                messages.info(request, _("Please select a customer to view the statement."))
                context['show_customer_selector'] = True
        else:
            target_customer = get_object_or_404(
                Party.objects.select_related('company'),
                pk=effective_customer_pk,
                company=target_company,
                party_type=CorePartyType.CUSTOMER.value
            )
            context['customer'] = target_customer
            context['title'] = f"{str(_('Statement'))}: {target_customer.name} - {target_company.name}"

            start_date_val = date.fromisoformat(context['start_date_param'])
            end_date_val = date.fromisoformat(context['end_date_param'])
            if start_date_val > end_date_val:
                raise DjangoValidationError(_("Start date cannot be after end date for the statement."))
            context['start_date_to_display'] = start_date_val
            context['end_date_to_display'] = end_date_val

            report_data = reports_service.generate_customer_statement(
                company_id=target_company.id,
                customer_id=target_customer.pk,
                start_date=start_date_val,
                end_date=end_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True

    except ValueError as ve:
        messages.error(request, str(ve))
        context['report_error'] = str(ve)
        if "Company ID" in str(ve) and request.user.is_superuser and not target_company:
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
            context['title'] = str(_('Customer Statement - Select Company'))
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e))
        context['report_error'] = str(e)
    except DjangoValidationError as dve:
        error_message = "; ".join(dve.messages) if hasattr(dve, 'messages') and isinstance(dve.messages, list) else str(
            dve)
        messages.error(request, error_message)
        context['report_error'] = error_message
    except ReportGenerationError as rge:
        messages.error(request, f"{str(_('Error generating statement'))}: {rge}")
        context['report_error'] = f"{str(_('Error generating statement'))}: {rge}"
    except Exception as e:
        logger.exception(
            f"Error generating Customer Statement for Co '{getattr(target_company, 'name', 'N/A')}', Cust '{getattr(target_customer, 'name', effective_customer_pk)}'")
        messages.error(request, _("An unexpected error occurred generating the statement."))
        context['report_error'] = str(_("An unexpected error occurred."))

    if 'opts' not in context: context['opts'] = Party._meta

    return render(request, 'admin/crp_accounting/reports/customer_statement_report.html', context)


# =========================================
# EXCEL Download Views (CORRECTIONS APPLIED HERE)
# =========================================
@staff_member_required
def download_trial_balance_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE:
        return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']

        report_data = reports_service.generate_trial_balance_structured(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = str(_("Trial Balance"))

        # CORRECTED currency_symbol logic
        currency_symbol_candidate = getattr(target_company, 'default_currency_symbol', None)
        currency_symbol = currency_symbol_candidate if currency_symbol_candidate is not None else ''

        sheet['A1'] = f"{str(_('Trial Balance Report'))} - {target_company.name}"
        sheet.merge_cells('A1:D1')
        sheet['A1'].font = Font(bold=True, size=14)
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells('A2:D2')
        sheet['A2'].alignment = Alignment(horizontal='center')
        sheet.append([])
        headers = [str(_("Account Number")), str(_("Account Name")), str(_("Debit")), str(_("Credit"))]
        sheet.append(headers)
        header_row_num = sheet.max_row
        for col_idx, cell_value in enumerate(headers, 1):
            cell = sheet.cell(row=header_row_num, column=col_idx)
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style='thin'))
            cell.alignment = Alignment(horizontal='center' if col_idx != 2 else 'left')
            sheet.column_dimensions[get_column_letter(col_idx)].width = (
                20 if col_idx == 1 else 35 if col_idx == 2 else 18)
        sheet.cell(row=header_row_num, column=3).alignment = Alignment(horizontal='right')
        sheet.cell(row=header_row_num, column=4).alignment = Alignment(horizontal='right')
        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'

        for entry in report_data.get('flat_entries', []):
            if entry.get('debit', ZERO) == ZERO and entry.get('credit', ZERO) == ZERO and not entry.get('is_group'):
                continue
            row_values = [entry.get('account_number', ''), str(entry.get('account_name', 'N/A')),
                          entry.get('debit', ZERO), entry.get('credit', ZERO)]
            sheet.append(row_values)
            current_row = sheet.max_row
            sheet.cell(row=current_row, column=3).number_format = number_format_currency
            sheet.cell(row=current_row, column=4).number_format = number_format_currency
            if entry.get('is_group'):
                for col_idx_group in range(1, 5):
                    sheet.cell(row=current_row, column=col_idx_group).font = Font(bold=True)

        sheet.append([])
        sheet.append(
            [None, str(_("Totals")), report_data.get('total_debit', ZERO), report_data.get('total_credit', ZERO)])
        total_row_num = sheet.max_row
        for col_idx_total in [2, 3, 4]:
            sheet.cell(row=total_row_num, column=col_idx_total).font = Font(bold=True)
        sheet.cell(row=total_row_num, column=3).number_format = number_format_currency
        sheet.cell(row=total_row_num, column=4).number_format = number_format_currency
        sheet.cell(row=total_row_num, column=3).alignment = Alignment(horizontal='right')
        sheet.cell(row=total_row_num, column=4).alignment = Alignment(horizontal='right')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Trial_Balance_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        workbook.save(response)
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(f"Error exporting Trial Balance Excel for Co '{getattr(target_company, 'name', 'N/A')}'")
        return HttpResponse(str(_("An unexpected error occurred during Excel generation.")), status=500)


@staff_member_required
def download_profit_loss_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE:
        return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['start_date', 'end_date'])
        start_date_val, end_date_val = date_params['start_date'], date_params['end_date']
        if start_date_val > end_date_val: raise ValueError(str(_("Start date cannot be after end date.")))

        report_data = reports_service.generate_profit_loss(
            company_id=target_company.id, start_date=start_date_val, end_date=end_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = str(_("Profit & Loss"))

        # CORRECTED currency_symbol logic
        currency_symbol_candidate = getattr(target_company, 'default_currency_symbol', None)
        currency_symbol = currency_symbol_candidate if currency_symbol_candidate is not None else ''

        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'
        sheet['A1'] = f"{str(_('Profit & Loss Statement'))} - {target_company.name}"
        sheet.merge_cells('A1:C1')
        sheet['A1'].font = Font(bold=True, size=14)
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet[
            'A2'] = f"{str(_('For the period:'))} {start_date_val.strftime('%B %d, %Y')} {str(_('to'))} {end_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells('A2:C2')
        sheet['A2'].alignment = Alignment(horizontal='center')
        sheet.append([])
        headers = [str(_("Description")), str(_("Amount")), str(_("Details"))]
        sheet.append(headers)
        header_row_num = sheet.max_row
        for col_idx, cell_val_pl in enumerate(headers, 1):
            cell = sheet.cell(row=header_row_num, column=col_idx)
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style='thin'))
            cell.alignment = Alignment(horizontal='left' if col_idx == 1 else 'right')
        sheet.column_dimensions[get_column_letter(1)].width = 45
        sheet.column_dimensions[get_column_letter(2)].width = 20
        sheet.column_dimensions[get_column_letter(3)].width = 30

        for item in report_data.get('report_lines', []):
            is_subtotal_or_net = item.get('is_subtotal', False) or item.get('section_key') == 'NET_INCOME'
            row_data = [str(item.get('title', 'N/A')), item.get('amount', ZERO)]
            account_details_str = ""
            if not item.get('is_subtotal', False) and item.get('accounts'):
                if len(item['accounts']) <= 3:
                    account_details_str = "; ".join(
                        [
                            f"{acc['account_number']} - {acc['account_name']}: {currency_symbol}{acc.get('amount', ZERO):,.2f}"
                            for acc in
                            item['accounts']])
                elif item.get('has_note') and item.get('note_ref'):
                    account_details_str = f"See Note: {item['note_ref']}"
            row_data.append(account_details_str if account_details_str else None)
            sheet.append(row_data)

            current_row_num = sheet.max_row
            desc_cell = sheet.cell(row=current_row_num, column=1)
            amt_cell = sheet.cell(row=current_row_num, column=2)
            desc_cell.font = Font(bold=is_subtotal_or_net)
            amt_cell.font = Font(bold=is_subtotal_or_net)
            amt_cell.number_format = number_format_currency

        if report_data.get('financial_notes_data'):
            sheet.append([])
            sheet.append([str(_("Financial Notes"))])
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, size=12)
            for note_ref, note_content in report_data['financial_notes_data'].items():
                sheet.append([f"{str(note_content.get('title', note_ref))} ({note_ref})"])
                sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
                for detail_line in note_content.get('details', []):
                    note_desc = detail_line.get('account_name', detail_line.get('title', ''))
                    note_amt = detail_line.get('amount', ZERO)
                    sheet.append([f"    {str(note_desc)}", note_amt])
                    sheet.cell(row=sheet.max_row, column=2).number_format = number_format_currency
                sheet.append([str(_("Total for Note")) + f" {note_ref}", note_content.get('total_amount', ZERO)])
                total_note_cell = sheet.cell(row=sheet.max_row, column=1)
                total_note_amt_cell = sheet.cell(row=sheet.max_row, column=2)
                total_note_cell.font = Font(italic=True)
                total_note_amt_cell.font = Font(italic=True)
                total_note_amt_cell.number_format = number_format_currency

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Profit_Loss_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        workbook.save(response)
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(f"Error exporting P&L Excel for Co '{getattr(target_company, 'name', 'N/A')}'")
        return HttpResponse(str(_("Excel generation error.")), status=500)


@staff_member_required
def download_balance_sheet_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE:
        return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']
        report_data = reports_service.generate_balance_sheet(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = str(_("Balance Sheet"))

        # CORRECTED currency_symbol logic
        currency_symbol_candidate = getattr(target_company, 'default_currency_symbol', None)
        currency_symbol = currency_symbol_candidate if currency_symbol_candidate is not None else ''

        sheet['A1'] = f"{str(_('Balance Sheet'))} - {target_company.name}"
        sheet.merge_cells('A1:B1')
        sheet['A1'].font = Font(bold=True, size=14)
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells('A2:B2')
        sheet['A2'].alignment = Alignment(horizontal='center')
        sheet.column_dimensions['A'].width = 50
        sheet.column_dimensions['B'].width = 20
        current_row = 3

        number_format_currency_bs = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'

        current_row += 1
        sheet.cell(row=current_row, column=1, value=str(_("Assets"))).font = Font(bold=True, size=12)
        current_row += 1
        assets_data = report_data.get('assets', {})
        current_row = _write_bs_hierarchy_excel(sheet, assets_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)  # currency_symbol is now corrected
        sheet.cell(row=current_row, column=1, value=str(_("Total Assets"))).font = Font(bold=True)
        total_assets_cell = sheet.cell(row=current_row, column=2, value=assets_data.get('total', ZERO))
        total_assets_cell.font = Font(bold=True)
        total_assets_cell.number_format = number_format_currency_bs
        total_assets_cell.alignment = Alignment(horizontal='right')
        current_row += 2

        sheet.cell(row=current_row, column=1, value=str(_("Liabilities"))).font = Font(bold=True, size=12)
        current_row += 1
        liabilities_data = report_data.get('liabilities', {})
        current_row = _write_bs_hierarchy_excel(sheet, liabilities_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)  # currency_symbol is now corrected
        sheet.cell(row=current_row, column=1, value=str(_("Total Liabilities"))).font = Font(bold=True)
        total_liab_cell = sheet.cell(row=current_row, column=2, value=liabilities_data.get('total', ZERO))
        total_liab_cell.font = Font(bold=True)
        total_liab_cell.number_format = number_format_currency_bs
        total_liab_cell.alignment = Alignment(horizontal='right')
        current_row += 2

        sheet.cell(row=current_row, column=1, value=str(_("Equity"))).font = Font(bold=True, size=12)
        current_row += 1
        equity_data = report_data.get('equity', {})
        current_row = _write_bs_hierarchy_excel(sheet, equity_data.get('hierarchy', []), current_row, 0,
                                                currency_symbol)  # currency_symbol is now corrected
        sheet.cell(row=current_row, column=1, value=str(_("Total Equity"))).font = Font(bold=True)
        total_equity_cell = sheet.cell(row=current_row, column=2, value=equity_data.get('total', ZERO))
        total_equity_cell.font = Font(bold=True)
        total_equity_cell.number_format = number_format_currency_bs
        total_equity_cell.alignment = Alignment(horizontal='right')
        current_row += 2

        total_liab_equity = liabilities_data.get('total', ZERO) + equity_data.get('total', ZERO)
        sheet.cell(row=current_row, column=1, value=str(_("Total Liabilities and Equity"))).font = Font(bold=True)
        total_liab_equity_cell = sheet.cell(row=current_row, column=2, value=total_liab_equity)
        total_liab_equity_cell.font = Font(bold=True)
        total_liab_equity_cell.number_format = number_format_currency_bs
        total_liab_equity_cell.alignment = Alignment(horizontal='right')

        if not report_data.get('is_balanced', True):
            current_row += 2
            warning_cell = sheet.cell(row=current_row, column=1, value=str(_("Note: Balance Sheet is Out of Balance!")))
            warning_cell.font = Font(color="FF0000", bold=True)
            sheet.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=2)
            warning_cell.alignment = Alignment(horizontal='center')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_Balance_Sheet_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        workbook.save(response)
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(f"Error exporting BS Excel for Co '{getattr(target_company, 'name', 'N/A')}'")
        return HttpResponse(str(_("Excel generation error.")), status=500)


# =========================================
# PDF Download Views - MODIFIED
# =========================================
@staff_member_required
def download_trial_balance_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE:
        return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")), status=501)
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
            'company': target_company, 'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(), 'report_data_available': True,
            'is_pdf': True, **report_data
        }
        # MODIFIED: Pass 'request' to _render_to_pdf
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/trial_balance_report.html', context, request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Trial_Balance_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
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
        logger.exception(f"Error exporting TB PDF for Co '{getattr(target_company, 'name', 'N/A')}'")
        if "PDF generation failed" in str(e):
            return HttpResponse(str(e), status=500)
        return HttpResponse(str(_("An unexpected PDF generation error occurred.")), status=500)


@staff_member_required
def download_profit_loss_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE:
        return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")), status=501)
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
            'company': target_company, 'start_date_to_display': start_date_val, 'end_date_to_display': end_date_val,
            'start_date_param': start_date_val.isoformat(), 'end_date_param': end_date_val.isoformat(),
            'report_data_available': True, 'is_pdf': True, **report_data
        }
        # MODIFIED: Pass 'request' to _render_to_pdf
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/profit_loss_report.html', context, request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Profit_Loss_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
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
        logger.exception(f"Error exporting P&L PDF for Co '{getattr(target_company, 'name', 'N/A')}'")
        if "PDF generation failed" in str(e):
            return HttpResponse(str(e), status=500)
        return HttpResponse(str(_("An unexpected PDF generation error occurred.")), status=500)


@staff_member_required
def download_balance_sheet_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE:
        return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")), status=501)
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
            'company': target_company, 'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(), 'report_data_available': True,
            'layout_preference': request.GET.get('layout', 'horizontal'), 'is_pdf': True, **report_data
        }
        if 'balance_difference' not in context and 'assets' in report_data and 'liabilities' in report_data and 'equity' in report_data:
            context['balance_difference'] = report_data['assets'].get('total', ZERO) - \
                                            (report_data['liabilities'].get('total', ZERO) + report_data['equity'].get(
                                                'total', ZERO))
        # MODIFIED: Pass 'request' to _render_to_pdf
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/balance_sheet_report.html', context, request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Balance_Sheet_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
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
        logger.exception(f"Error exporting BS PDF for Co '{getattr(target_company, 'name', 'N/A')}'")
        if "PDF generation failed" in str(e):
            return HttpResponse(str(e), status=500)
        return HttpResponse(str(_("An unexpected PDF generation error occurred.")), status=500)


@staff_member_required
def download_ar_aging_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE:
        return HttpResponse(str(_("Excel export library (openpyxl) is not installed.")), status=501)

    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']

        buckets_str_from_get = request.GET.get('aging_buckets')
        if buckets_str_from_get:
            try:
                aging_buckets_to_use = [int(b.strip()) for b in buckets_str_from_get.split(',') if b.strip()]
            except ValueError:
                aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS
                logger.warning(
                    f"Invalid aging_buckets GET param '{buckets_str_from_get}'. Using default for Excel export.")
        else:
            aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS

        report_data = reports_service.generate_ar_aging_report(
            company_id=target_company.id,
            as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = str(_("AR Aging"))

        # CORRECTED currency_symbol logic
        company_sym = getattr(target_company, 'default_currency_symbol', None)
        report_sym = report_data.get('report_currency', None)
        if company_sym is not None:
            currency_symbol = company_sym
        elif report_sym is not None:
            currency_symbol = report_sym
        else:
            currency_symbol = ''

        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'

        sheet['A1'] = f"{str(_('AR Aging Report'))} - {target_company.name}"
        sheet.merge_cells(start_row=1, start_column=1, end_row=1,
                          end_column=len(report_data.get('bucket_labels', [])) + 2)
        sheet['A1'].font = Font(bold=True, size=14)
        sheet['A1'].alignment = Alignment(horizontal='center')

        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells(start_row=2, start_column=1, end_row=2,
                          end_column=len(report_data.get('bucket_labels', [])) + 2)
        sheet['A2'].alignment = Alignment(horizontal='center')
        sheet.append([])

        excel_headers = [str(_("Customer Name"))] + [str(label) for label in report_data.get('bucket_labels', [])] + [
            str(_("Total Due"))]
        sheet.append(excel_headers)
        header_row_num = sheet.max_row
        for col_idx, header_title in enumerate(excel_headers, 1):
            cell = sheet.cell(row=header_row_num, column=col_idx, value=header_title)
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style='thin'))
            cell.alignment = Alignment(horizontal='left' if col_idx == 1 else 'right')
            sheet.column_dimensions[get_column_letter(col_idx)].width = 30 if col_idx == 1 else 18

        for entry in report_data.get('aging_data', []):
            row_values = [str(entry.get('customer_name', 'N/A'))]
            for bucket_label in report_data.get('bucket_labels', []):
                row_values.append(entry.get('buckets', {}).get(bucket_label, ZERO))
            row_values.append(entry.get('total_due', ZERO))
            sheet.append(row_values)
            current_data_row = sheet.max_row
            for col_idx in range(2, len(excel_headers) + 1):
                sheet.cell(row=current_data_row, column=col_idx).number_format = number_format_currency
                sheet.cell(row=current_data_row, column=col_idx).alignment = Alignment(horizontal='right')

        sheet.append([])

        grand_total_row_values = [str(_("Grand Totals"))]
        for bucket_label in report_data.get('bucket_labels', []):
            grand_total_row_values.append(report_data.get('grand_totals_by_bucket', {}).get(bucket_label, ZERO))
        grand_total_row_values.append(report_data.get('grand_total_due_all_customers', ZERO))
        sheet.append(grand_total_row_values)
        total_row_num = sheet.max_row
        for col_idx in range(1, len(grand_total_row_values) + 1):
            cell = sheet.cell(row=total_row_num, column=col_idx)
            cell.font = Font(bold=True)
            if col_idx > 1:
                cell.number_format = number_format_currency
                cell.alignment = Alignment(horizontal='right')
            else:
                cell.alignment = Alignment(horizontal='left')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_AR_Aging_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        workbook.save(response)
        return response

    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report generation error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(f"Error exporting AR Aging Excel for Co '{getattr(target_company, 'name', 'N/A')}'")
        return HttpResponse(str(_("An unexpected error occurred during AR Aging Excel generation.")), status=500)


@staff_member_required
def download_ar_aging_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE:
        return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")), status=501)

    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']

        buckets_str_from_get = request.GET.get('aging_buckets')
        if buckets_str_from_get:
            try:
                aging_buckets_to_use = [int(b.strip()) for b in buckets_str_from_get.split(',') if b.strip()]
            except ValueError:
                aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS
                logger.warning(
                    f"Invalid aging_buckets GET param '{buckets_str_from_get}'. Using default for PDF export.")
        else:
            aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS

        report_data = reports_service.generate_ar_aging_report(
            company_id=target_company.id,
            as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )

        context = {
            **_get_admin_base_context(f"{str(_('AR Aging Report'))} - {target_company.name}", request),
            'company': target_company,
            'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(),
            'aging_bucket_days_config_str': ",".join(map(str, aging_buckets_to_use)),
            'report_data_available': True,
            'is_pdf': True,
            **report_data
        }
        # MODIFIED: Pass 'request' to _render_to_pdf
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/ar_aging_report.html', context, request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_AR_Aging_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
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
        logger.exception(f"Error exporting AR Aging PDF for Co '{getattr(target_company, 'name', 'N/A')}'")
        if "PDF generation failed" in str(e):
            return HttpResponse(str(e), status=500)
        return HttpResponse(str(_("An unexpected error occurred during AR Aging PDF generation.")), status=500)


@staff_member_required
def download_customer_statement_pdf(request: HttpRequest, customer_pk: PK_TYPE) -> HttpResponse:
    target_company: Optional[Company] = None
    target_customer: Optional[Party] = None
    if not XHTML2PDF_AVAILABLE:
        return HttpResponse(str(_("PDF export library (xhtml2pdf) is not installed.")), status=501)

    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['start_date', 'end_date'])
        start_date_val, end_date_val = date_params['start_date'], date_params['end_date']

        if start_date_val > end_date_val:
            raise ValueError(str(_("Start date cannot be after end date for the statement.")))

        target_customer = get_object_or_404(
            Party.objects.select_related('company'),
            pk=customer_pk,
            company=target_company,
            party_type=CorePartyType.CUSTOMER.value
        )

        report_data = reports_service.generate_customer_statement(
            company_id=target_company.id,
            customer_id=target_customer.pk,
            start_date=start_date_val,
            end_date=end_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )

        context = {
            **_get_admin_base_context(f"{str(_('Statement'))}: {target_customer.name} - {target_company.name}",
                                      request),
            'company': target_company,
            'customer': target_customer,
            'start_date_to_display': start_date_val,
            'end_date_to_display': end_date_val,
            'start_date_param': start_date_val.isoformat(),
            'end_date_param': end_date_val.isoformat(),
            'report_data_available': True,
            'is_pdf': True,
            **report_data
        }
        context['opts'] = Party._meta

        # MODIFIED: Pass 'request' to _render_to_pdf
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/customer_statement_report.html', context,
                                    request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Statement_{target_customer.name.replace(' ', '_')}_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
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
            f"Error exporting Customer Statement PDF for Co '{getattr(target_company, 'name', 'N/A')}', Cust '{getattr(target_customer, 'name', customer_pk)}'")
        if "PDF generation failed" in str(e):
            return HttpResponse(str(e), status=500)
        return HttpResponse(str(_("An unexpected error occurred during Customer Statement PDF generation.")),
                            status=500)


# =============================================================================
# NEW: Accounts Payable (AP) HTML Report Views (Tenant Aware)
# =============================================================================

@staff_member_required
def admin_ap_aging_report_view(request: HttpRequest) -> HttpResponse:
    """Displays the Accounts Payable Aging Report."""
    context = _get_admin_base_context(_("AP Aging Report"), request)
    target_company: Optional[Company] = None
    today = date.today()

    context['as_of_date_param'] = request.GET.get('as_of_date', today.isoformat())
    context['aging_bucket_days_config_str'] = ",".join(
        map(str, reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS))  # Reuse same bucket config for now
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{_('AP Aging Report')} - {target_company.name}"

        as_of_date_val = date.fromisoformat(context['as_of_date_param'])

        buckets_str_from_get = request.GET.get('aging_buckets')
        if buckets_str_from_get:
            try:
                aging_buckets_to_use = [int(b.strip()) for b in buckets_str_from_get.split(',') if b.strip()]
            except ValueError:
                aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS;
                messages.error(request,
                               _("Invalid aging buckets format."))
        else:
            aging_buckets_to_use = reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS
        context['aging_bucket_days_config_str'] = ",".join(map(str, aging_buckets_to_use))

        report_data = reports_service.generate_ap_aging_report(  # Call AP specific service
            company_id=target_company.id,
            as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )
        context.update(report_data)
        context['report_data_available'] = True

    except ValueError as ve:
        messages.error(request, str(ve));
        context['report_error'] = str(ve)
        if request.user.is_superuser and "Company ID" in str(ve):
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e));
        context['report_error'] = str(e)
    except ReportGenerationError as rge:
        messages.error(request, f"{_('Error generating AP Aging')}: {rge}");
        context[
            'report_error'] = f"{_('Error generating AP Aging')}: {rge}"
    except Exception as e:
        logger.exception(f"Error generating admin AP Aging for Co '{getattr(target_company, 'name', 'N/A')}'")
        messages.error(request, _("Unexpected error generating AP Aging."));
        context['report_error'] = _("Unexpected error.")

    if 'opts' not in context: context['opts'] = Company._meta if Company else Account._meta
    return render(request, 'admin/crp_accounting/reports/ap_aging_report.html', context)


@staff_member_required
def admin_vendor_statement_view(request: HttpRequest,
                                supplier_pk: Optional[PK_TYPE] = None) -> HttpResponse:  # Changed param name
    """Displays a Vendor/Supplier Statement."""
    context = _get_admin_base_context(_("Vendor Statement"), request)
    target_company: Optional[Company] = None
    target_supplier: Optional[Party] = None  # Changed from target_customer
    today = date.today()
    default_start_date = today.replace(day=1)
    next_month = default_start_date.replace(day=28) + timedelta(days=4)  # Robust way to get next month
    default_end_date = next_month - timedelta(days=next_month.day)  # End of current month

    context['start_date_param'] = request.GET.get('start_date', default_start_date.isoformat())
    context['end_date_param'] = request.GET.get('end_date', default_end_date.isoformat())
    supplier_pk_from_get = request.GET.get('supplier_id')  # Changed from customer_id
    effective_supplier_pk = supplier_pk or supplier_pk_from_get
    context['report_data_available'] = False

    try:
        target_company = _get_company_for_report_or_raise(request)
        context['company'] = target_company
        context['title'] = f"{_('Vendor Statement')} - {target_company.name}"

        if target_company:  # Only populate selector if company is known
            context['suppliers_for_selection'] = Party.objects.filter(  # Changed from customers
                company=target_company,
                party_type=CorePartyType.SUPPLIER.value,  # Filter for SUPPLIER
                is_active=True
            ).order_by('name')

        if not effective_supplier_pk:
            if 'company_id' in request.GET and not supplier_pk_from_get:  # If company was selected but not supplier
                messages.info(request, _("Please select a supplier to view the statement."))
                context['show_supplier_selector'] = True  # Changed from show_customer_selector
        else:
            target_supplier = get_object_or_404(
                Party.objects.select_related('company'),
                pk=effective_supplier_pk,
                company=target_company,
                party_type=CorePartyType.SUPPLIER.value  # Ensure it's a supplier
            )
            context['supplier'] = target_supplier  # Changed from customer
            context['title'] = f"{_('Statement')}: {target_supplier.name} - {target_company.name}"

            start_date_val = date.fromisoformat(context['start_date_param'])
            end_date_val = date.fromisoformat(context['end_date_param'])
            if start_date_val > end_date_val: raise DjangoValidationError(_("Start date cannot be after end date."))
            context['start_date_to_display'] = start_date_val
            context['end_date_to_display'] = end_date_val

            report_data = reports_service.generate_vendor_statement(  # Call AP specific service
                company_id=target_company.id,
                supplier_id=target_supplier.pk,  # Pass supplier_id
                start_date=start_date_val,
                end_date=end_date_val,
                report_currency=getattr(target_company, 'default_currency_code', 'USD')
            )
            context.update(report_data)
            context['report_data_available'] = True

    except ValueError as ve:
        messages.error(request, str(ve));
        context['report_error'] = str(ve)
        if request.user.is_superuser and "Company ID" in str(ve) and not target_company:
            if Company: context['all_companies'] = Company.objects.filter(is_active=True).order_by('name')
            context['show_company_selector'] = True
        elif "Please select a supplier" in str(ve) and target_company and 'suppliers_for_selection' not in context:
            context['suppliers_for_selection'] = Party.objects.filter(company=target_company,
                                                                      party_type=CorePartyType.SUPPLIER.value,
                                                                      is_active=True).order_by('name')
            context['show_supplier_selector'] = True
    except (Http404, DjangoPermissionDenied) as e:
        messages.error(request, str(e));
        context['report_error'] = str(e)
    except DjangoValidationError as dve:
        messages.error(request, "; ".join(dve.messages) if hasattr(dve, 'messages') else str(dve));
        context[
            'report_error'] = str(dve)
    except ReportGenerationError as rge:
        messages.error(request, f"{_('Error generating statement')}: {rge}");
        context[
            'report_error'] = f"{_('Error generating statement')}: {rge}"
    except Exception as e:
        logger.exception(
            f"Error generating Vendor Stmt for Co '{getattr(target_company, 'name', 'N/A')}', Supp '{getattr(target_supplier, 'name', effective_supplier_pk)}'")
        messages.error(request, _("Unexpected error generating statement."));
        context['report_error'] = _("Unexpected error.")

    if 'opts' not in context: context['opts'] = Party._meta
    return render(request, 'admin/crp_accounting/reports/vendor_statement_report.html', context)


# =============================================================================
# NEW: Accounts Payable (AP) Download Views (Tenant Aware)
# =============================================================================

@staff_member_required
def download_ap_aging_excel(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not OPENPYXL_AVAILABLE: return HttpResponse(str(_("Excel export library is missing.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']

        buckets_str = request.GET.get('aging_buckets')
        aging_buckets_to_use = [int(b.strip()) for b in buckets_str.split(
            ',')] if buckets_str else reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS  # Reusing AR default for now

        report_data = reports_service.generate_ap_aging_report(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )

        workbook = openpyxl.Workbook();
        sheet = workbook.active;
        sheet.title = str(_("AP Aging"))

        # CORRECTED currency_symbol logic
        company_sym = getattr(target_company, 'default_currency_symbol', None)
        report_sym = report_data.get('report_currency', None)
        if company_sym is not None:
            currency_symbol = company_sym
        elif report_sym is not None:
            currency_symbol = report_sym
        else:
            currency_symbol = ''

        number_format_currency = f'"{currency_symbol}"#,##0.00;[Red]-"{currency_symbol}"#,##0.00'

        # Example Header
        sheet['A1'] = f"{str(_('AP Aging Report'))} - {target_company.name}"
        sheet.merge_cells(start_row=1, start_column=1, end_row=1,
                          end_column=len(report_data.get('bucket_labels', [])) + 2)
        sheet['A1'].font = Font(bold=True, size=14);
        sheet['A1'].alignment = Alignment(horizontal='center')
        sheet['A2'] = f"{str(_('As of Date:'))} {as_of_date_val.strftime('%B %d, %Y')}"
        sheet.merge_cells(start_row=2, start_column=1, end_row=2,
                          end_column=len(report_data.get('bucket_labels', [])) + 2)
        sheet['A2'].alignment = Alignment(horizontal='center');
        sheet.append([])

        excel_headers = [str(_("Supplier Name"))] + [str(label) for label in report_data.get('bucket_labels', [])] + [
            str(_("Total Due"))]
        sheet.append(excel_headers)
        header_row_num = sheet.max_row
        for col_idx, header_title in enumerate(excel_headers, 1):
            cell = sheet.cell(row=header_row_num, column=col_idx, value=header_title)
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style='thin'))
            cell.alignment = Alignment(horizontal='left' if col_idx == 1 else 'right')
            sheet.column_dimensions[get_column_letter(col_idx)].width = 30 if col_idx == 1 else 18

        for entry in report_data.get('aging_data', []):  # aging_data from AP report will have 'supplier_name'
            row_values = [str(entry.get('party_name', 'N/A'))]  # Use 'party_name' from AgingEntry
            for bucket_label in report_data.get('bucket_labels', []): row_values.append(
                entry.get('buckets', {}).get(bucket_label, ZERO))
            row_values.append(entry.get('total_due', ZERO))
            sheet.append(row_values)
            current_data_row = sheet.max_row
            for col_idx in range(2, len(excel_headers) + 1):  # Start from 2nd col for amounts
                sheet.cell(row=current_data_row, column=col_idx).number_format = number_format_currency
                sheet.cell(row=current_data_row, column=col_idx).alignment = Alignment(horizontal='right')

        sheet.append([])  # Empty row before totals

        grand_total_row_values = [str(_("Grand Totals"))]
        for bucket_label in report_data.get('bucket_labels', []):
            grand_total_row_values.append(report_data.get('grand_totals_by_bucket', {}).get(bucket_label, ZERO))
        grand_total_row_values.append(
            report_data.get('grand_total_due_all_customers', ZERO))  # Field name might be different for AP
        sheet.append(grand_total_row_values)
        total_row_num = sheet.max_row
        for col_idx in range(1, len(grand_total_row_values) + 1):
            cell = sheet.cell(row=total_row_num, column=col_idx)
            cell.font = Font(bold=True)
            if col_idx > 1:
                cell.number_format = number_format_currency
                cell.alignment = Alignment(horizontal='right')
            else:
                cell.alignment = Alignment(horizontal='left')

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"{target_company.name}_AP_Aging_{as_of_date_val.strftime('%Y%m%d')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"';
        workbook.save(response);
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report error'))}: {rge}", status=500)
    except Exception as e:
        logger.exception(
            f"Error exporting AP Aging Excel for Co '{getattr(target_company, 'name', 'N/A')}'");
        return HttpResponse(
            str(_("Excel generation error.")), status=500)


@staff_member_required
def download_ap_aging_pdf(request: HttpRequest) -> HttpResponse:
    target_company: Optional[Company] = None
    if not XHTML2PDF_AVAILABLE: return HttpResponse(str(_("PDF export library is missing.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['as_of_date'])
        as_of_date_val = date_params['as_of_date']
        buckets_str = request.GET.get('aging_buckets');
        aging_buckets_to_use = [int(b.strip()) for b in buckets_str.split(
            ',')] if buckets_str else reports_service.DEFAULT_AR_AGING_BUCKETS_DAYS

        report_data = reports_service.generate_ap_aging_report(
            company_id=target_company.id, as_of_date=as_of_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD'),
            aging_buckets_days=aging_buckets_to_use
        )
        context = {
            **_get_admin_base_context(f"{str(_('AP Aging Report'))} - {target_company.name}", request),
            'company': target_company, 'as_of_date_to_display': as_of_date_val,
            'as_of_date_param': as_of_date_val.isoformat(),
            'aging_bucket_days_config_str': ",".join(map(str, aging_buckets_to_use)),
            'report_data_available': True, 'is_pdf': True, **report_data
        }
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/ap_aging_report.html', context, request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_AP_Aging_{as_of_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"';
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report error'))}: {rge}", status=500)
    except ImportError as ie:
        return HttpResponse(str(ie), status=501)
    except Exception as e:
        logger.exception(
            f"Error exporting AP Aging PDF for Co '{getattr(target_company, 'name', 'N/A')}'");
        return HttpResponse(
            str(_("PDF generation error.")), status=500)


@staff_member_required
def download_vendor_statement_pdf(request: HttpRequest, supplier_pk: PK_TYPE) -> HttpResponse:
    target_company: Optional[Company] = None;
    target_supplier: Optional[Party] = None
    if not XHTML2PDF_AVAILABLE: return HttpResponse(str(_("PDF export library is missing.")), status=501)
    try:
        target_company = _get_company_for_report_or_raise(request)
        date_params = _get_validated_date_params_for_download(request, ['start_date', 'end_date'])
        start_date_val, end_date_val = date_params['start_date'], date_params['end_date']
        if start_date_val > end_date_val: raise ValueError(str(_("Start date cannot be after end date.")))

        target_supplier = get_object_or_404(Party.objects.select_related('company'), pk=supplier_pk,
                                            company=target_company, party_type=CorePartyType.SUPPLIER.value)
        report_data = reports_service.generate_vendor_statement(
            company_id=target_company.id, supplier_id=target_supplier.pk,
            start_date=start_date_val, end_date=end_date_val,
            report_currency=getattr(target_company, 'default_currency_code', 'USD')
        )
        context = {
            **_get_admin_base_context(f"{str(_('Statement'))}: {target_supplier.name} - {target_company.name}",
                                      request),
            'company': target_company, 'supplier': target_supplier,  # Changed from customer
            'start_date_to_display': start_date_val, 'end_date_to_display': end_date_val,
            'start_date_param': start_date_val.isoformat(), 'end_date_param': end_date_val.isoformat(),
            'report_data_available': True, 'is_pdf': True, **report_data
        }
        context['opts'] = Party._meta
        pdf_buffer = _render_to_pdf('admin/crp_accounting/reports/vendor_statement_report.html', context,
                                    request=request)
        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        filename = f"{target_company.name}_Statement_Vendor_{target_supplier.name.replace(' ', '_')}_{start_date_val.strftime('%Y%m%d')}_{end_date_val.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"';
        return response
    except ValueError as ve:
        return HttpResponseBadRequest(str(ve))
    except (Http404, DjangoPermissionDenied) as e:
        return HttpResponse(str(e), status=403 if isinstance(e, DjangoPermissionDenied) else 404)
    except ReportGenerationError as rge:
        return HttpResponse(f"{str(_('Report error'))}: {rge}", status=500)
    except ImportError as ie:
        return HttpResponse(str(ie), status=501)
    except Exception as e:
        logger.exception(
            f"Error exporting Vendor Stmt PDF for Co '{getattr(target_company, 'name', 'N/A')}', Supp '{getattr(target_supplier, 'name', supplier_pk)}'");
        return HttpResponse(
            str(_("PDF generation error.")), status=500)


@staff_member_required
def admin_reports_hub_view(request: HttpRequest) -> HttpResponse:
    context = _get_admin_base_context(_("Accounting Reports Hub"), request)
    user_name_attr = getattr(request.user, 'name', None)  # Try 'name' attribute first
    user_name = user_name_attr if user_name_attr else getattr(request.user, 'username', str(request.user.id))

    logger.debug(f"[ReportsHubView] User: {user_name}, Is Superuser: {request.user.is_superuser}")
    if request.user.is_superuser:
        if Company:
            companies_qs = Company.objects.filter(is_active=True).order_by('name')
            context['all_companies'] = companies_qs
            logger.debug(f"[ReportsHubView] Found companies for SU: {[c.name for c in companies_qs]}")
        else:
            logger.warning("[ReportsHubView] Company model not available, cannot list companies for SU.")
            context['all_companies'] = []
    else:
        logger.debug(f"[ReportsHubView] User {user_name} is not SU, not populating all_companies.")
    return render(request, 'admin/crp_accounting/reports_hub.html', context)