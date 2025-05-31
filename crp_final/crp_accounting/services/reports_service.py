# crp_accounting/services/reports_service.py

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation as DecimalInvalidOperation, ROUND_HALF_UP
from datetime import date, timedelta
from typing import List, Dict, Tuple, Optional, Any, DefaultDict, TypedDict

from django.utils.translation import gettext_lazy as _
from django.db import models
from django.db.models import Sum, Q, F  # F was in second file's imports
from django.db.models.functions import Coalesce
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned  # From second file
from django.utils import timezone

# Logger from the second file
logger = logging.getLogger("crp_accounting.services.reports")

# --- Model Imports (Start with second file's, add AP models) ---
from ..models.coa import Account, AccountGroup, PLSection  # FiscalYear needed for BS from first file logic
from ..models.journal import VoucherLine, TransactionStatus, DrCrType
from ..models.receivables import CustomerInvoice, InvoiceStatus, CustomerPayment,PaymentAllocation  # as CustomerPaymentAllocation, PaymentStatus as CustomerPaymentStatus
from ..models.party import Party

# AP Models for new reports (from first file)
from ..models.payables import VendorBill, BillStatus as VendorBillStatus, VendorPayment, VendorPaymentAllocation
from crp_core.enums import PaymentStatus as VendorPaymentStatus

# FiscalYear for advanced BS retained earnings (from first file's BS logic, if we were using it fully)
# However, the BS in the second file is simpler, so FiscalYear might not be directly used by *that* BS version.
# For now, let's assume the second file's BS is the target and doesn't use FiscalYear/CompanyAccountingSettings explicitly.
# If a more advanced BS is desired later, these would be uncommented and integrated.
# from ..models.coa import FiscalYear
# try:
#     from company.models import CompanyAccountingSettings
# except ImportError:
#     logger.error("Reports Service: CompanyAccountingSettings model not found. Advanced BS features might be impacted.")
#     CompanyAccountingSettings = None # type: ignore


try:
    from ..models.base import ExchangeRate
except ImportError:
    logger.error(
        "Reports Service: CRITICAL - ExchangeRate model not found. Currency conversion will be non-functional.")
    ExchangeRate = None  # type: ignore

try:
    from company.models import Company
except ImportError:
    logger.error("Reports Service: CRITICAL - Company model not found. Multi-tenancy is fundamentally broken.")
    Company = None  # type: ignore

# --- Enum Imports (From second file) ---
from crp_core.enums import AccountNature, AccountType, PartyType as CorePartyType, \
    PaymentStatus as CorePaymentStatus  # Renamed for clarity


# --- Custom Exceptions (From second file) ---
class ReportGenerationError(Exception): pass


class BalanceCalculationError(ReportGenerationError): pass


class CurrencyConversionError(ReportGenerationError): pass


class DataIntegrityWarning(Warning): pass


# --- Constants (From second file, add AP/generic aging constants if needed) ---
ZERO_DECIMAL = Decimal('0.00')
RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY = _("Retained Earnings (Calculated)")
RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER = "RETAINED_EARNINGS_CALCULATED"  # From second file
DEFAULT_FX_RATE_PRECISION = 8
DEFAULT_AMOUNT_PRECISION = 2
DEFAULT_AR_AGING_BUCKETS_DAYS = [0, 30, 60, 90]  # From second file (AR specific)
# Generic aging buckets from first file for AP - can be the same if desired
DEFAULT_AP_AGING_BUCKETS_DAYS = [0, 30, 60, 90]  # For AP, can be same as AR or different

# Constants from first file, if needed for the parts of BS/P&L that are retained
# DRAWINGS_ACCOUNT_CODE = '3110_owners_drawings_withdrawals'
# DIVIDENDS_DECLARED_ACCOUNT_CODE = '3210_dividends_declared_paid'

# =============================================================================
# Type Definitions
# =============================================================================
PK_TYPE = Any


# --- TypedDicts from second file (TB, P&L, BS, AR) ---
class ProfitLossAccountDetail(TypedDict):  # From second file
    account_pk: PK_TYPE
    account_number: str
    account_name: str
    amount: Decimal
    original_amount: Decimal
    original_currency: str
    is_subtotal_in_note: Optional[bool]  # Added from second file's P&L structure


class ProfitLossLineItem(TypedDict):  # From second file
    section_key: str
    title: str
    amount: Decimal
    is_subtotal: bool
    is_main_section_title: bool
    accounts: Optional[List[ProfitLossAccountDetail]]
    level: int
    has_note: Optional[bool]
    note_ref: Optional[str]


class BalanceSheetNode(TypedDict):  # From second file
    id: PK_TYPE
    name: str
    type: str  # 'group' or 'account'
    level: int
    balance: Decimal
    currency: Optional[str]  # Original currency for accounts
    account_number: Optional[str]
    children: List['BalanceSheetNode']


class ProcessedAccountBalance(TypedDict):  # From second file
    account_pk: PK_TYPE
    account_number: str
    account_name: str
    account_type: str
    account_nature: str
    account_group_pk: Optional[PK_TYPE]
    original_currency: str
    original_balance: Decimal
    converted_balance: Decimal
    pl_section: Optional[str]


# --- Generic TypedDicts for Aging and Statements (inspired by first file, for AP and potential AR harmonization) ---
class AgingEntry(TypedDict):  # Generic for AR and AP Aging
    party_pk: PK_TYPE
    party_name: str
    currency: str  # Report currency
    buckets: Dict[str, Decimal]
    total_due: Decimal


class StatementLine(TypedDict):  # Generic for Customer and Vendor Statements
    date: date
    transaction_type: str
    reference: str
    debit: Optional[Decimal]
    credit: Optional[Decimal]
    balance: Decimal


class StatementData(TypedDict):  # Generic
    party_pk: PK_TYPE
    party_name: str
    statement_period_start: date
    statement_period_end: date
    report_currency: str
    opening_balance: Decimal
    lines: List[StatementLine]
    closing_balance: Decimal


# TypedDicts for AR from second file (kept for now to ensure no breakage to existing AR logic)
class ARAgingEntry(TypedDict):  # From second file (can be replaced by generic AgingEntry if compatible)
    customer_pk: PK_TYPE
    customer_name: str
    currency: str
    buckets: Dict[str, Decimal]
    total_due: Decimal


class CustomerStatementLine(TypedDict):  # From second file (can be replaced by generic StatementLine)
    date: date
    transaction_type: str
    reference: str
    debit: Optional[Decimal]
    credit: Optional[Decimal]
    balance: Decimal


class CustomerStatementData(TypedDict):  # From second file (can be replaced by generic StatementData)
    customer_pk: PK_TYPE
    customer_name: str
    statement_period_start: date
    statement_period_end: date
    report_currency: str
    opening_balance: Decimal
    lines: List[CustomerStatementLine]
    closing_balance: Decimal


# =============================================================================
# Currency Conversion (From second file - this logic will be used by all reports)
# =============================================================================
def _get_exchange_rate(company_id: Optional[PK_TYPE], from_currency: str, to_currency: str,
                       conversion_date: date) -> Decimal:
    if not ExchangeRate: raise CurrencyConversionError("ExchangeRate model not available.")
    if from_currency == to_currency: return Decimal('1.0')

    company_specific_query = Q(company_id=company_id)
    global_query = Q(company_id__isnull=True)
    direct_rate_conditions = Q(from_currency=from_currency, to_currency=to_currency, date__lte=conversion_date)
    inverse_rate_conditions = Q(from_currency=to_currency, to_currency=from_currency, date__lte=conversion_date)

    rate_obj = ExchangeRate.objects.filter(company_specific_query & direct_rate_conditions).order_by('-date').first()
    if rate_obj: return Decimal(rate_obj.rate)

    inverse_rate_obj = ExchangeRate.objects.filter(company_specific_query & inverse_rate_conditions).order_by(
        '-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(Decimal(f'1e-{DEFAULT_FX_RATE_PRECISION}'),
                                                                          rounding=ROUND_HALF_UP)

    rate_obj = ExchangeRate.objects.filter(global_query & direct_rate_conditions).order_by('-date').first()
    if rate_obj: return Decimal(rate_obj.rate)

    inverse_rate_obj = ExchangeRate.objects.filter(global_query & inverse_rate_conditions).order_by('-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(Decimal(f'1e-{DEFAULT_FX_RATE_PRECISION}'),
                                                                          rounding=ROUND_HALF_UP)
    logger.error(
        f"No exchange rate found for Co {company_id or 'Global'} from {from_currency} to {to_currency} on or before {conversion_date}.")
    raise CurrencyConversionError(
        f"Exchange rate missing: {from_currency} to {to_currency} for {conversion_date} (Co: {company_id or 'Global'}).")


def _convert_currency(company_id: Optional[PK_TYPE], amount: Decimal, from_currency: str, to_currency: str,
                      conversion_date: date, precision: int = DEFAULT_AMOUNT_PRECISION) -> Decimal:
    if from_currency == to_currency: return amount.quantize(Decimal(f'1e-{precision}'), rounding=ROUND_HALF_UP)
    if amount == ZERO_DECIMAL: return ZERO_DECIMAL
    try:
        rate = _get_exchange_rate(company_id, from_currency, to_currency, conversion_date)
        converted = (amount * rate).quantize(Decimal(f'1e-{precision}'), rounding=ROUND_HALF_UP)
        return converted
    except CurrencyConversionError:
        raise
    except Exception as e:
        logger.exception(
            f"Error during currency conversion from {from_currency} to {to_currency} for Co {company_id or 'Global'}")
        raise CurrencyConversionError(f"General conversion failure: {str(e)}") from e


# =============================================================================
# Core Balance Calculation Helper (From second file - this logic will be used by all reports)
# =============================================================================
def _calculate_account_balances(company_id: PK_TYPE, as_of_date: date, target_report_currency: str) -> Dict[
    PK_TYPE, ProcessedAccountBalance]:
    if not Company: raise ReportGenerationError("Company model not available.")
    if not company_id: raise ValueError("company_id must be provided.")

    logger.debug(
        f"Calculating balances for Co ID {company_id} as of {as_of_date}, target currency {target_report_currency}...")
    account_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    conversion_errors_logged = set()

    try:
        aggregation = VoucherLine.objects.filter(
            voucher__company_id=company_id, voucher__status=TransactionStatus.POSTED.value,
            voucher__date__lte=as_of_date, account__company_id=company_id, account__is_active=True
        ).values('account').annotate(
            total_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), ZERO_DECIMAL,
                                 output_field=models.DecimalField()),
            total_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), ZERO_DECIMAL,
                                  output_field=models.DecimalField())
        ).values(
            'account__id', 'account__account_number', 'account__account_name', 'account__account_type',
            'account__account_nature', 'account__account_group_id', 'account__currency', 'account__pl_section',
            'total_debit', 'total_credit'
        )

        for item in aggregation:
            pk = item['account__id']
            acc_currency = item['account__currency']
            nature = item['account__account_nature']

            original_balance = (item['total_debit'] - item['total_credit']) \
                if nature == AccountNature.DEBIT.value else (item['total_credit'] - item['total_debit'])

            converted_balance: Decimal
            try:
                converted_balance = _convert_currency(company_id, original_balance, acc_currency,
                                                      target_report_currency, as_of_date)
            except CurrencyConversionError as cce:
                rate_key = (acc_currency, target_report_currency)
                if rate_key not in conversion_errors_logged:
                    logger.warning(
                        f"Co {company_id}: {cce} for Account {pk} ({item['account__account_number']}). Using original balance for this account. Report may be mixed-currency.")
                    conversion_errors_logged.add(rate_key)
                converted_balance = original_balance

            account_balances[pk] = ProcessedAccountBalance(
                account_pk=pk, account_number=item['account__account_number'],
                account_name=str(item['account__account_name']),
                account_type=item['account__account_type'], account_nature=nature,
                account_group_pk=item['account__account_group_id'],
                original_currency=acc_currency, original_balance=original_balance, converted_balance=converted_balance,
                pl_section=item['account__pl_section']
            )

        all_company_active_accounts = Account.objects.filter(company_id=company_id, is_active=True).select_related(
            'account_group')
        for acc in all_company_active_accounts:
            if acc.pk not in account_balances:
                account_balances[acc.pk] = ProcessedAccountBalance(
                    account_pk=acc.pk, account_number=acc.account_number, account_name=str(acc.account_name),
                    account_type=acc.account_type, account_nature=acc.account_nature,
                    account_group_pk=acc.account_group_id,
                    original_currency=acc.currency, original_balance=ZERO_DECIMAL, converted_balance=ZERO_DECIMAL,
                    pl_section=acc.pl_section
                )
        logger.debug(f"Calculated balances for {len(account_balances)} accounts for Co ID {company_id}.")
        return account_balances
    except Exception as e:
        logger.exception(f"Unexpected error in _calculate_account_balances for Co ID {company_id}.")
        raise BalanceCalculationError(f"Failed to calculate account balances: {str(e)}") from e


# =============================================================================
# Hierarchy Building Helpers (From second file - this logic will be used by TB & BS)
# =============================================================================
def _build_group_hierarchy_recursive(parent_group_id: Optional[PK_TYPE], all_groups: Dict[PK_TYPE, AccountGroup],
                                     account_data_map: Dict[PK_TYPE, ProcessedAccountBalance], level: int) -> Tuple[
    List[Dict[str, Any]], Decimal, Decimal]:  # This is for Trial Balance
    current_level_nodes: List[Dict[str, Any]] = []
    current_level_total_debit = ZERO_DECIMAL
    current_level_total_credit = ZERO_DECIMAL

    child_groups = [group for pk, group in all_groups.items() if group.parent_group_id == parent_group_id]
    for group in sorted(child_groups, key=lambda g: g.name):
        child_nodes, child_debit, child_credit = _build_group_hierarchy_recursive(
            group.pk, all_groups, account_data_map, level + 1
        )
        group_node = {
            'id': group.pk, 'name': str(group.name), 'type': 'group', 'level': level,
            'debit': child_debit, 'credit': child_credit, 'children': child_nodes
        }
        if group_node['children'] or group_node['debit'] != ZERO_DECIMAL or group_node['credit'] != ZERO_DECIMAL:
            current_level_nodes.append(group_node)
            current_level_total_debit += child_debit
            current_level_total_credit += child_credit

    direct_accounts_nodes = []
    for acc_pk, acc_data in account_data_map.items():
        if acc_data.get('account_group_pk') == parent_group_id:
            balance_in_report_curr = acc_data['converted_balance']
            nature = acc_data['account_nature']
            account_debit, account_credit = ZERO_DECIMAL, ZERO_DECIMAL

            if nature == AccountNature.DEBIT.value:
                account_debit = balance_in_report_curr if balance_in_report_curr >= ZERO_DECIMAL else ZERO_DECIMAL
                account_credit = -balance_in_report_curr if balance_in_report_curr < ZERO_DECIMAL else ZERO_DECIMAL
            elif nature == AccountNature.CREDIT.value:
                account_credit = balance_in_report_curr if balance_in_report_curr >= ZERO_DECIMAL else ZERO_DECIMAL
                account_debit = -balance_in_report_curr if balance_in_report_curr < ZERO_DECIMAL else ZERO_DECIMAL
            else:
                logger.warning(
                    f"Account {acc_pk} ({acc_data.get('account_number')}) has unknown nature '{nature}'. Balance signs might be incorrect.")

            if account_debit != ZERO_DECIMAL or account_credit != ZERO_DECIMAL:
                account_node = {
                    'id': acc_pk,
                    'name': f"{acc_data.get('account_number', 'N/A')} - {str(acc_data.get('account_name', 'N/A'))}",
                    'type': 'account', 'level': level,
                    'debit': account_debit, 'credit': account_credit, 'children': []
                }
                direct_accounts_nodes.append(account_node)
                current_level_total_debit += account_debit
                current_level_total_credit += account_credit
    direct_accounts_nodes.sort(key=lambda item: account_data_map.get(item['id'], {}).get('account_number', ''))
    current_level_nodes.extend(direct_accounts_nodes)
    return current_level_nodes, current_level_total_debit, current_level_total_credit


def _build_balance_sheet_hierarchy(parent_group_id: Optional[PK_TYPE], all_groups: Dict[PK_TYPE, AccountGroup],
                                   account_balances_bs: Dict[PK_TYPE, ProcessedAccountBalance], level: int) -> Tuple[
    List[BalanceSheetNode], Decimal]:  # This is for Balance Sheet
    current_level_nodes: List[BalanceSheetNode] = []
    current_level_total_balance = ZERO_DECIMAL

    child_groups = [group for pk, group in all_groups.items() if group.parent_group_id == parent_group_id]
    for group in sorted(child_groups, key=lambda g: g.name):
        child_hierarchy_nodes, child_total_balance = _build_balance_sheet_hierarchy(
            group.pk, all_groups, account_balances_bs, level + 1
        )
        group_node: BalanceSheetNode = {
            'id': group.pk, 'name': str(group.name), 'type': 'group', 'level': level,
            'balance': child_total_balance,
            'currency': None,
            'account_number': None,
            'children': child_hierarchy_nodes
        }
        if group_node['children'] or group_node['balance'] != ZERO_DECIMAL:
            current_level_nodes.append(group_node)
            current_level_total_balance += child_total_balance

    direct_accounts_nodes: List[BalanceSheetNode] = []
    for acc_pk, acc_data in account_balances_bs.items():
        if acc_data['account_group_pk'] == parent_group_id:
            account_balance_in_report_curr = acc_data['converted_balance']
            if account_balance_in_report_curr != ZERO_DECIMAL:
                account_node: BalanceSheetNode = {
                    'id': acc_pk,
                    'name': f"{acc_data['account_number']} - {str(acc_data['account_name'])}",
                    'type': 'account', 'level': level,
                    'balance': account_balance_in_report_curr,
                    'currency': acc_data['original_currency'],
                    'account_number': acc_data['account_number'],
                    'children': []
                }
                direct_accounts_nodes.append(account_node)
                current_level_total_balance += account_balance_in_report_curr
    direct_accounts_nodes.sort(key=lambda item: account_balances_bs.get(item['id'], {}).get('account_number', ''))
    current_level_nodes.extend(direct_accounts_nodes)
    return current_level_nodes, current_level_total_balance


# =============================================================================
# Public Report Generation Functions (TB, P&L, BS - FROM SECOND FILE, UNCHANGED)
# =============================================================================
def generate_trial_balance_structured(company_id: PK_TYPE, as_of_date: date, report_currency: Optional[str] = None) -> \
        Dict[str, Any]:  # FROM SECOND FILE
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"TB Gen: Co ID {company_id} ({effective_report_currency}) as of {as_of_date}")

    processed_balances_map = _calculate_account_balances(company_id, as_of_date, effective_report_currency)
    flat_entries_list: List[Dict[str, Any]] = []
    grand_total_debit, grand_total_credit = ZERO_DECIMAL, ZERO_DECIMAL

    for pk, data in processed_balances_map.items():
        balance_in_report_curr = data['converted_balance']
        nature = data['account_nature']
        debit_amount, credit_amount = ZERO_DECIMAL, ZERO_DECIMAL

        if nature == AccountNature.DEBIT.value:
            debit_amount = balance_in_report_curr if balance_in_report_curr >= ZERO_DECIMAL else ZERO_DECIMAL
            credit_amount = -balance_in_report_curr if balance_in_report_curr < ZERO_DECIMAL else ZERO_DECIMAL
        elif nature == AccountNature.CREDIT.value:
            credit_amount = balance_in_report_curr if balance_in_report_curr >= ZERO_DECIMAL else ZERO_DECIMAL
            debit_amount = -balance_in_report_curr if balance_in_report_curr < ZERO_DECIMAL else ZERO_DECIMAL

        if debit_amount != ZERO_DECIMAL or credit_amount != ZERO_DECIMAL:
            flat_entries_list.append({
                'account_pk': pk, 'account_number': data['account_number'], 'account_name': str(data['account_name']),
                'debit': debit_amount, 'credit': credit_amount, 'currency': effective_report_currency
                # Note: 'is_group' was in first file, not here in second file's TB flat entries
            })
        grand_total_debit += debit_amount
        grand_total_credit += credit_amount
    flat_entries_list.sort(key=lambda x: x['account_number'])

    company_groups_qs = AccountGroup.objects.filter(company_id=company_id).order_by('name')
    group_dict = {group.pk: group for group in company_groups_qs}
    hierarchy, _, _ = _build_group_hierarchy_recursive(None, group_dict, processed_balances_map, 0)

    is_balanced = abs(grand_total_debit - grand_total_credit) < Decimal('0.01')
    if not is_balanced:
        logger.error(
            f"TB Co ID {company_id}: OUT OF BALANCE! Debit Total: {grand_total_debit}, Credit Total: {grand_total_credit}, Difference: {grand_total_debit - grand_total_credit}")

    return {
        'company_id': company_id, 'as_of_date': as_of_date, 'report_currency': effective_report_currency,
        'hierarchy': hierarchy, 'flat_entries': flat_entries_list,
        'total_debit': grand_total_debit, 'total_credit': grand_total_credit, 'is_balanced': is_balanced,
    }


# --- P&L Structure Definition (FROM SECOND FILE) ---
DEFAULT_PL_STRUCTURE_DEFINITION = [
    {'key': PLSection.REVENUE.value, 'title': _('Revenue'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': PLSection.COGS.value, 'title': _('Cost of Goods Sold / Services (Direct)'), 'level': 0,
     'is_subtotal': False, 'calculation_method': 'sum_section'},
    {'key': 'GROSS_PROFIT', 'title': _('Gross Profit'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [
         (PLSection.REVENUE.value, True),
         (PLSection.COGS.value, False)
     ]},
    {'key': PLSection.OPERATING_EXPENSE.value, 'title': _('Operating Expenses'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section', 'has_note': True, 'note_ref': 'Note_OpEx',
     # Note related fields from 2nd file
     'note_title': _('Details of Operating Expenses')},
    {'key': PLSection.DEPRECIATION_AMORTIZATION.value, 'title': _('Depreciation & Amortization'), 'level': 0,
     'is_subtotal': False, 'calculation_method': 'sum_section'},
    {'key': 'OPERATING_PROFIT', 'title': _('Operating Profit / (Loss)'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [
         ('GROSS_PROFIT', True),
         (PLSection.OPERATING_EXPENSE.value, False),
         (PLSection.DEPRECIATION_AMORTIZATION.value, False)
     ]},
    {'key': PLSection.OTHER_INCOME.value, 'title': _('Other Income'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': PLSection.OTHER_EXPENSE.value, 'title': _('Other Expenses'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': 'PROFIT_BEFORE_TAX', 'title': _('Profit / (Loss) Before Tax'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [
         ('OPERATING_PROFIT', True),
         (PLSection.OTHER_INCOME.value, True),
         (PLSection.OTHER_EXPENSE.value, False)
     ]},
    {'key': PLSection.TAX_EXPENSE.value, 'title': _('Tax Expense'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': 'NET_INCOME', 'title': _('Net Income / (Loss)'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [
         ('PROFIT_BEFORE_TAX', True),
         (PLSection.TAX_EXPENSE.value, False)
     ]},
]


def generate_profit_loss(company_id: PK_TYPE, start_date: date, end_date: date,
                         report_currency: Optional[str] = None) -> Dict[str, Any]:  # FROM SECOND FILE
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"P&L Gen: Co ID {company_id} ({effective_report_currency}) from {start_date} to {end_date}")
    if start_date > end_date: raise ValueError("Start date cannot be after end date for P&L.")

    section_totals: DefaultDict[str, Decimal] = defaultdict(Decimal)
    section_details: DefaultDict[str, List[ProfitLossAccountDetail]] = defaultdict(list)
    calculated_values: Dict[str, Decimal] = {}
    conversion_errors_logged_pl = set()
    financial_notes_data: Dict[str, Dict[str, Any]] = {}  # From second file

    pl_account_types = [AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value]

    account_movements = VoucherLine.objects.filter(
        voucher__company_id=company_id, voucher__status=TransactionStatus.POSTED.value,
        voucher__date__gte=start_date, voucher__date__lte=end_date,
        account__company_id=company_id, account__account_type__in=pl_account_types, account__is_active=True
    ).values('account').annotate(
        period_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), ZERO_DECIMAL,
                              output_field=models.DecimalField()),
        period_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), ZERO_DECIMAL,
                               output_field=models.DecimalField())
    ).values(
        'account__id', 'account__account_number', 'account__account_name',
        'account__account_nature', 'account__pl_section', 'account__currency',
        'period_debit', 'period_credit'
    )

    for item in account_movements:
        acc_pk = item['account__id']
        acc_currency = item['account__currency']
        nature = item['account__account_nature']
        pl_section_str = item['account__pl_section']

        if not pl_section_str or pl_section_str == PLSection.NONE.value:
            logger.debug(
                f"P&L Co {company_id}: Account {acc_pk} ({item['account__account_number']}) has no PLSection, skipping from P&L aggregation.")
            continue

        movement_in_acc_currency = (item['period_credit'] - item['period_debit']) \
            if nature == AccountNature.CREDIT.value else (item['period_debit'] - item['period_credit'])

        if movement_in_acc_currency == ZERO_DECIMAL: continue

        movement_in_report_currency: Decimal
        try:
            movement_in_report_currency = _convert_currency(company_id, movement_in_acc_currency, acc_currency,
                                                            effective_report_currency, end_date)
        except CurrencyConversionError as cce:
            rate_key = (acc_currency, effective_report_currency)
            if rate_key not in conversion_errors_logged_pl:
                logger.warning(
                    f"P&L Co {company_id}: {cce} for Acc {acc_pk} ({item['account__account_number']}). Using original amount for this account.")
                conversion_errors_logged_pl.add(rate_key)
            movement_in_report_currency = movement_in_acc_currency

        section_totals[pl_section_str] += movement_in_report_currency
        section_details[pl_section_str].append(ProfitLossAccountDetail(  # ProfitLossAccountDetail from 2nd file
            account_pk=acc_pk, account_number=item['account__account_number'],
            account_name=str(item['account__account_name']),
            amount=movement_in_report_currency, original_amount=movement_in_acc_currency,
            original_currency=acc_currency,
            is_subtotal_in_note=False  # Defaulting this, adjust if specific logic needed
        ))

    report_lines: List[ProfitLossLineItem] = []  # ProfitLossLineItem from 2nd file
    for section_def in DEFAULT_PL_STRUCTURE_DEFINITION:  # Using 2nd file's PL structure
        key = section_def['key']
        title = str(section_def['title'])
        is_subtotal = section_def['is_subtotal']
        level = section_def.get('level', 0)
        calculation_method = section_def.get('calculation_method')
        has_note = section_def.get('has_note', False)  # From second file structure
        note_ref = section_def.get('note_ref')  # From second file structure
        note_title_str = str(section_def.get('note_title', title))  # From second file structure

        accounts_for_line: Optional[List[ProfitLossAccountDetail]] = None
        current_line_amount: Decimal = ZERO_DECIMAL

        if calculation_method == 'sum_section':
            current_line_amount = section_totals.get(key, ZERO_DECIMAL)
            accounts_for_line = sorted(section_details.get(key, []), key=lambda x: x['account_number'])

            if has_note and note_ref and accounts_for_line:  # Logic from 2nd file
                financial_notes_data[note_ref] = {
                    'title': note_title_str,
                    'details': accounts_for_line,
                    'total_amount': current_line_amount
                }
                # accounts_for_line = None # Per 2nd file logic, if note exists, details are there

        elif calculation_method == 'periodic_cogs_formula':  # This was in 1st file, not 2nd P&L.
            # If this is truly desired, the PLSection enum and DEFAULT_PL_STRUCTURE need to be aligned from 1st file.
            # For now, keeping P&L as per second file.
            logger.warning(
                f"P&L Co {company_id}: 'periodic_cogs_formula' is defined in 1st file logic but not active in 2nd file's P&L structure. Skipping.")
            pass

        elif is_subtotal:
            for basis_key, is_positive_impact in section_def.get('calculation_basis', []):
                amount_from_basis = calculated_values.get(basis_key, ZERO_DECIMAL)
                current_line_amount += amount_from_basis * (1 if is_positive_impact else -1)

        calculated_values[key] = current_line_amount

        show_line = is_subtotal or \
                    (calculation_method == 'sum_section' and (current_line_amount != ZERO_DECIMAL or (
                            accounts_for_line and len(accounts_for_line) > 0))) or \
                    (calculation_method == 'periodic_cogs_formula' and current_line_amount != ZERO_DECIMAL)

        if show_line:
            report_lines.append(ProfitLossLineItem(
                section_key=key, title=title, amount=current_line_amount,
                is_subtotal=is_subtotal,
                is_main_section_title=bool(calculation_method and not is_subtotal),
                accounts=accounts_for_line, level=level,
                has_note=has_note, note_ref=note_ref
            ))

    net_income = calculated_values.get('NET_INCOME', ZERO_DECIMAL)
    return {
        'company_id': company_id, 'start_date': start_date, 'end_date': end_date,
        'report_currency': effective_report_currency,
        'report_lines': report_lines,
        'net_income': net_income,
        'financial_notes_data': financial_notes_data  # From 2nd file
    }


def generate_balance_sheet(company_id: PK_TYPE, as_of_date: date, report_currency: Optional[str] = None) -> Dict[
    str, Any]:  # FROM SECOND FILE
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"BS Gen: Co ID {company_id} ({effective_report_currency}) as of {as_of_date}")

    all_balances = _calculate_account_balances(company_id, as_of_date, effective_report_currency)

    retained_earnings_calculated = ZERO_DECIMAL
    pl_account_types_for_re = {AccountType.INCOME.value, AccountType.EXPENSE.value,
                               AccountType.COST_OF_GOODS_SOLD.value}
    for acc_data in all_balances.values():
        if acc_data['account_type'] in pl_account_types_for_re:
            balance = acc_data['converted_balance']
            if acc_data['account_nature'] == AccountNature.CREDIT.value:
                retained_earnings_calculated += balance
            elif acc_data['account_nature'] == AccountNature.DEBIT.value:
                retained_earnings_calculated -= balance

    asset_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    liability_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    equity_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}

    for pk, data in all_balances.items():
        if data['account_type'] == AccountType.ASSET.value:
            asset_balances[pk] = data
        elif data['account_type'] == AccountType.LIABILITY.value:
            liability_balances[pk] = data
        elif data['account_type'] == AccountType.EQUITY.value:
            equity_balances[pk] = data

    company_groups_qs = AccountGroup.objects.filter(company_id=company_id).order_by('name')
    group_dict = {group.pk: group for group in company_groups_qs}

    asset_hierarchy, total_assets = _build_balance_sheet_hierarchy(None, group_dict, asset_balances, 0)
    liability_hierarchy, total_liabilities = _build_balance_sheet_hierarchy(None, group_dict, liability_balances, 0)
    equity_hierarchy, total_explicit_equity = _build_balance_sheet_hierarchy(None, group_dict, equity_balances, 0)

    retained_earnings_node: BalanceSheetNode = {  # BalanceSheetNode from 2nd file
        'id': RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER,  # From 2nd file
        'name': str(RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY),
        'type': 'account', 'level': 0,
        'balance': retained_earnings_calculated,
        'currency': effective_report_currency,
        'account_number': None,
        'children': []
    }

    equity_hierarchy.append(retained_earnings_node)  # Simple append from 2nd file
    total_equity = total_explicit_equity + retained_earnings_calculated

    balance_difference = total_assets - (total_liabilities + total_equity)
    is_balanced = abs(balance_difference) < Decimal('0.01')
    if not is_balanced:
        logger.error(
            f"BS Co ID {company_id}: OUT OF BALANCE! Assets: {total_assets}, "
            f"Liabilities: {total_liabilities}, Equity (Explicit + RE): {total_equity}, "
            f"Total L+E: {total_liabilities + total_equity}, Difference: {balance_difference}"
        )

    return {
        'company_id': company_id, 'as_of_date': as_of_date, 'report_currency': effective_report_currency,
        'is_balanced': is_balanced, 'balance_difference': balance_difference,
        'assets': {'hierarchy': asset_hierarchy, 'total': total_assets},
        'liabilities': {'hierarchy': liability_hierarchy, 'total': total_liabilities},
        'equity': {'hierarchy': equity_hierarchy, 'total': total_equity}
    }


# =============================================================================
# Accounts Receivable (AR) Specific Report Functions (FROM SECOND FILE, UNCHANGED)
# =============================================================================
def generate_ar_aging_report(
        company_id: PK_TYPE,
        as_of_date: date,
        report_currency: Optional[str] = None,
        aging_buckets_days: Optional[List[int]] = None
) -> Dict[str, Any]:  # FROM SECOND FILE
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"AR Aging Gen: Co ID {company_id} ({effective_report_currency}) as of {as_of_date}")

    current_buckets_def = aging_buckets_days if aging_buckets_days is not None else DEFAULT_AR_AGING_BUCKETS_DAYS
    if not current_buckets_def or 0 not in current_buckets_def:
        effective_buckets_def = sorted(list(set([0] + (current_buckets_def or []))))
    else:
        effective_buckets_def = sorted(list(set(current_buckets_def)))

    bucket_labels: List[str] = []
    if not effective_buckets_def:
        bucket_labels.append(str(_("Total Due")))
    elif len(effective_buckets_def) == 1 and effective_buckets_def[0] == 0:
        bucket_labels.append(str(_("Current/Not Due")))
    else:
        bucket_labels.append(str(_("Current")))
        for i in range(len(effective_buckets_def) - 1):
            lower_bound = effective_buckets_def[i] + 1
            upper_bound = effective_buckets_def[i + 1]
            if lower_bound <= upper_bound:
                bucket_labels.append(f"{lower_bound}-{upper_bound} {str(_('Days'))}")
        last_defined_threshold = effective_buckets_def[-1]
        bucket_labels.append(f"{last_defined_threshold + 1}+ {str(_('Days'))}")

    outstanding_invoices_qs = CustomerInvoice.objects.filter(
        company_id=company_id,
        amount_due__gt=ZERO_DECIMAL,
        status__in=[InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value, InvoiceStatus.OVERDUE.value]
    ).select_related('customer')

    # Using ARAgingEntry from second file to avoid breaking its specific AR logic
    aging_data: DefaultDict[PK_TYPE, ARAgingEntry] = defaultdict(
        lambda: ARAgingEntry(customer_pk=None, customer_name="", currency=effective_report_currency,  # type: ignore
                             buckets={label: ZERO_DECIMAL for label in bucket_labels}, total_due=ZERO_DECIMAL)
    )
    grand_totals_by_bucket: Dict[str, Decimal] = {label: ZERO_DECIMAL for label in bucket_labels}
    grand_total_due_all_customers = ZERO_DECIMAL
    conversion_errors_logged_ar = set()

    for invoice in outstanding_invoices_qs:
        if not invoice.customer:
            logger.warning(
                f"AR Aging Co {company_id}: Invoice {invoice.invoice_number or invoice.pk} has no customer. Skipping.")
            continue

        age_days = (as_of_date - invoice.due_date).days
        bucket_idx_to_use = 0
        if age_days > 0:
            found_bucket = False
            # This bucket logic is from the second file (AR aging)
            for i in range(len(effective_buckets_def)):
                threshold = effective_buckets_def[i]
                if age_days <= threshold:
                    bucket_idx_to_use = i + (1 if threshold > 0 else 0)
                    if threshold == 0 and age_days > 0:
                        bucket_idx_to_use = 1
                    elif threshold > 0:
                        bucket_idx_to_use = i + 1  # This logic differs slightly from 1st file, keeping 2nd file's
                    else:  # age_days <= 0, threshold = 0
                        bucket_idx_to_use = 0
                    found_bucket = True
                    break
            if not found_bucket:
                bucket_idx_to_use = len(bucket_labels) - 1

        if bucket_idx_to_use < 0: bucket_idx_to_use = 0
        if bucket_idx_to_use >= len(bucket_labels): bucket_idx_to_use = len(bucket_labels) - 1
        chosen_bucket_label = bucket_labels[bucket_idx_to_use]

        invoice_due_in_report_currency: Decimal
        try:
            invoice_due_in_report_currency = _convert_currency(
                company_id, invoice.amount_due, invoice.currency, effective_report_currency, as_of_date
            )
        except CurrencyConversionError as cce:
            rate_key = (invoice.currency, effective_report_currency)
            if rate_key not in conversion_errors_logged_ar:
                logger.warning(
                    f"AR Aging Co {company_id}: {cce} for Inv {invoice.invoice_number or invoice.pk}. Using original amount_due.")
                conversion_errors_logged_ar.add(rate_key)
            invoice_due_in_report_currency = invoice.amount_due

        customer_entry = aging_data[invoice.customer.pk]  # ARAgingEntry
        if not customer_entry['customer_pk']:
            customer_entry['customer_pk'] = invoice.customer.pk
            customer_entry['customer_name'] = invoice.customer.name
            # ARAgingEntry 'currency' is already set to effective_report_currency by defaultdict

        customer_entry['buckets'][chosen_bucket_label] = customer_entry['buckets'].get(chosen_bucket_label,
                                                                                       ZERO_DECIMAL) + invoice_due_in_report_currency
        customer_entry['total_due'] += invoice_due_in_report_currency

        grand_totals_by_bucket[chosen_bucket_label] = grand_totals_by_bucket.get(chosen_bucket_label,
                                                                                 ZERO_DECIMAL) + invoice_due_in_report_currency
        grand_total_due_all_customers += invoice_due_in_report_currency

    sorted_aging_data = sorted(list(aging_data.values()), key=lambda x: x['customer_name'])

    return {
        'company_id': company_id,
        'as_of_date': as_of_date,
        'report_currency': effective_report_currency,
        'bucket_labels': bucket_labels,
        'aging_data': sorted_aging_data,
        'grand_totals_by_bucket': grand_totals_by_bucket,
        'grand_total_due_all_customers': grand_total_due_all_customers,
        'effective_buckets_definition_for_debug': effective_buckets_def
    }


def generate_customer_statement(
        company_id: PK_TYPE,
        customer_id: PK_TYPE,
        start_date: date,
        end_date: date,
        report_currency: Optional[str] = None
) -> CustomerStatementData:  # FROM SECOND FILE, uses CustomerStatementData/Line
    if not Company or not Party: raise ReportGenerationError("Company or Party model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
        customer_instance = Party.objects.get(pk=customer_id, company_id=company_id,
                                              party_type=CorePartyType.CUSTOMER.value)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")
    except Party.DoesNotExist:
        raise ReportGenerationError(f"Customer ID {customer_id} (Type: Customer) not found for company {company_id}.")
    except AttributeError:
        logger.error("Party model or CorePartyType enum might be missing 'party_type' or 'CUSTOMER' value.")
        raise ReportGenerationError("Misconfiguration in Party model or CorePartyType enum.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(
        f"Cust Stmt Gen: Co ID {company_id}, Cust {customer_id} ({effective_report_currency}) from {start_date} to {end_date}")

    statement_lines_raw: List[Dict[str, Any]] = []
    conversion_errors_logged_stmt = set()

    invoices_qs = CustomerInvoice.objects.filter(
        company_id=company_id, customer_id=customer_id,
        status__in=[
            InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value,
            InvoiceStatus.PAID.value, InvoiceStatus.OVERDUE.value
        ]
    ).order_by('invoice_date', 'pk')

    for inv in invoices_qs:
        statement_lines_raw.append({
            'date': inv.invoice_date,
            'type': 'Invoice',  # Using string directly as in 2nd file
            'ref': inv.invoice_number or f"Inv#{inv.pk}",
            'orig_amount': inv.total_amount,
            'orig_currency': inv.currency,
            'debit_credit_factor': 1
        })

    payments_qs = CustomerPayment.objects.filter(
        company_id=company_id, customer_id=customer_id,
        # Using CorePaymentStatus from 2nd file's imports (crp_core.enums.PaymentStatus)
        status__in=[
            CorePaymentStatus.COMPLETED.value, CorePaymentStatus.PARTIALLY_PAID.value, CorePaymentStatus.APPLIED.value
        ]
    ).order_by('payment_date', 'pk')

    for pmt in payments_qs:
        statement_lines_raw.append({
            'date': pmt.payment_date,
            'type': 'Payment',  # Using string directly
            'ref': pmt.reference_number or f"Pmt#{pmt.pk}",
            'orig_amount': pmt.amount_received,
            'orig_currency': pmt.currency,
            'debit_credit_factor': -1
        })

    statement_lines_raw.sort(
        key=lambda x: (x['date'], 0 if x['type'] == 'Invoice' else (1 if x['type'] == 'Payment' else 2)))

    opening_balance_statement: Decimal = ZERO_DECIMAL
    # Using CustomerStatementLine from 2nd file
    processed_statement_lines: List[CustomerStatementLine] = []

    for item in statement_lines_raw:
        if item['date'] < start_date:
            amount_to_convert = item['orig_amount'] * item['debit_credit_factor']  # Factor applied here in 2nd file
            converted_amount: Decimal
            try:
                converted_amount = _convert_currency(company_id, amount_to_convert, item['orig_currency'],
                                                     effective_report_currency, item['date'])
            except CurrencyConversionError as cce:  # Using cce from 2nd file
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt:
                    logger.warning(
                        f"Cust Stmt Co {company_id} Cust {customer_id}: OB conversion error for {item['ref']} ({item['orig_currency']} to {effective_report_currency} on {item['date']}). Using unconverted amount. Error: {cce}")
                    conversion_errors_logged_stmt.add(rate_key)
                converted_amount = amount_to_convert
            opening_balance_statement += converted_amount
        # else: break # Optimization from 2nd file, commented out for safety

    running_balance_statement = opening_balance_statement
    for item in statement_lines_raw:
        if item['date'] >= start_date and item['date'] <= end_date:
            amount_in_item_currency = item['orig_amount']
            converted_amount: Decimal
            try:
                converted_amount = _convert_currency(company_id, amount_in_item_currency, item['orig_currency'],
                                                     effective_report_currency,
                                                     item['date'])
            except CurrencyConversionError as cce:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt:
                    logger.warning(
                        f"Cust Stmt Co {company_id} Cust {customer_id}: Line item conversion error for {item['ref']} ({item['orig_currency']} to {effective_report_currency} on {item['date']}). Using unconverted amount. Error: {cce}")
                    conversion_errors_logged_stmt.add(rate_key)
                converted_amount = amount_in_item_currency

            debit_val, credit_val = None, None
            if item['debit_credit_factor'] == 1:
                debit_val = converted_amount
                running_balance_statement += converted_amount
            elif item['debit_credit_factor'] == -1:
                credit_val = converted_amount
                running_balance_statement -= converted_amount

            processed_statement_lines.append(CustomerStatementLine(
                date=item['date'], transaction_type=item['type'], reference=item['ref'],
                debit=debit_val, credit=credit_val, balance=running_balance_statement
            ))
    # Using CustomerStatementData from 2nd file
    return CustomerStatementData(
        customer_pk=customer_id, customer_name=customer_instance.name,
        statement_period_start=start_date, statement_period_end=end_date,
        report_currency=effective_report_currency,
        opening_balance=opening_balance_statement.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'),
                                                           rounding=ROUND_HALF_UP),
        lines=processed_statement_lines,
        closing_balance=running_balance_statement.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'),
                                                           rounding=ROUND_HALF_UP)
    )


# =============================================================================
# NEW: Accounts Payable (AP) Specific Report Functions (FROM FIRST FILE)
# These will use the generic AgingEntry, StatementLine, StatementData TypedDicts
# =============================================================================
def generate_ap_aging_report(company_id: PK_TYPE, as_of_date: date, report_currency: Optional[str] = None,
                             aging_buckets_days: Optional[List[int]] = None) -> Dict[str, Any]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"AP Aging Gen: Co ID {company_id} ({effective_report_currency}) as of {as_of_date}")

    # Using DEFAULT_AP_AGING_BUCKETS_DAYS for AP
    current_buckets_def = aging_buckets_days if aging_buckets_days is not None else DEFAULT_AP_AGING_BUCKETS_DAYS
    effective_buckets_def = sorted(list(set([0] + (current_buckets_def or []))))

    bucket_labels: List[str] = [str(_("Current"))]
    for i in range(len(effective_buckets_def) - 1):
        lower = effective_buckets_def[i] + 1
        upper = effective_buckets_def[i + 1]
        bucket_labels.append(f"{lower}-{upper} {str(_('Days'))}")
    bucket_labels.append(f"{effective_buckets_def[-1] + 1}+ {str(_('Days'))}")

    outstanding_bills = VendorBill.objects.filter(
        company_id=company_id, amount_due__gt=ZERO_DECIMAL,
        status__in=[VendorBillStatus.APPROVED.value, VendorBillStatus.PARTIALLY_PAID.value]
    ).select_related('supplier')

    # Using generic AgingEntry
    aging_data: DefaultDict[PK_TYPE, AgingEntry] = defaultdict(
        lambda: AgingEntry(party_pk=None, party_name="", currency=effective_report_currency,  # type: ignore
                           buckets={lbl: ZERO_DECIMAL for lbl in bucket_labels}, total_due=ZERO_DECIMAL)
    )
    grand_t_bucket = {lbl: ZERO_DECIMAL for lbl in bucket_labels}
    grand_t_due = ZERO_DECIMAL
    conversion_errors_logged_ap = set()

    for bill in outstanding_bills:
        if not bill.supplier:
            logger.warning(f"AP Aging Co {company_id}: Bill {bill.bill_number or bill.pk} missing supplier. Skipping.")
            continue
        due_date = bill.due_date or bill.issue_date
        age_days = (as_of_date - due_date).days

        # Bucket logic from first file for AP
        label_idx = 0
        if age_days > 0:
            found = False
            for i in range(1, len(effective_buckets_def)):
                if age_days <= effective_buckets_def[i]:
                    label_idx = i
                    found = True
                    break
            if not found:
                label_idx = len(bucket_labels) - 1
        chosen_lbl = bucket_labels[label_idx]

        try:
            bill_due_rep = _convert_currency(company_id, bill.amount_due, bill.currency, effective_report_currency,
                                             as_of_date)
        except CurrencyConversionError as cce:
            rate_key = (bill.currency, effective_report_currency)  # Simplified key
            if rate_key not in conversion_errors_logged_ap:
                logger.warning(
                    f"AP Aging Co {company_id}: {cce} for Bill {bill.bill_number or bill.pk}. Using orig amt.")
                conversion_errors_logged_ap.add(rate_key)
            bill_due_rep = bill.amount_due

        supp_entry = aging_data[bill.supplier.pk]  # Uses generic AgingEntry
        if not supp_entry['party_pk']:
            supp_entry['party_pk'] = bill.supplier.pk
            supp_entry['party_name'] = bill.supplier.name
        supp_entry['buckets'][chosen_lbl] = supp_entry['buckets'].get(chosen_lbl, ZERO_DECIMAL) + bill_due_rep
        supp_entry['total_due'] += bill_due_rep
        grand_t_bucket[chosen_lbl] = grand_t_bucket.get(chosen_lbl, ZERO_DECIMAL) + bill_due_rep
        grand_t_due += bill_due_rep

    return {
        'company_id': company_id, 'as_of_date': as_of_date, 'report_currency': effective_report_currency,
        'bucket_labels': bucket_labels, 'aging_data': sorted(list(aging_data.values()), key=lambda x: x['party_name']),
        'grand_totals_by_bucket': grand_t_bucket,
        'grand_total_due_all_suppliers': grand_t_due,  # Key from first file
        'effective_buckets_definition_for_debug': effective_buckets_def
    }


def generate_vendor_statement(company_id: PK_TYPE, supplier_id: PK_TYPE, start_date: date, end_date: date,
                              report_currency: Optional[str] = None) -> StatementData:
    if not (Company and Party): raise ReportGenerationError("Models missing.")
    try:
        company_instance = Company.objects.get(pk=company_id)
        supplier_instance = Party.objects.get(pk=supplier_id, company_id=company_id,
                                              party_type=CorePartyType.SUPPLIER.value)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")
    except Party.DoesNotExist:
        raise ReportGenerationError(f"Supplier ID {supplier_id} (Type: Supplier) not found for company {company_id}.")

    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(
        f"Vendor Stmt Gen: Co ID {company_id}, Supp {supplier_id} ({effective_report_currency}) from {start_date} to {end_date}")

    raw_lines: List[Dict[str, Any]] = []
    conversion_errors_logged_stmt_ven = set()

    for bill in VendorBill.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status__in=[VendorBillStatus.APPROVED.value, VendorBillStatus.PARTIALLY_PAID.value,
                        VendorBillStatus.PAID.value]
    ).order_by('issue_date', 'pk'):
        raw_lines.append({
            'date': bill.issue_date, 'type': str(_('Bill')),
            'ref': bill.bill_number or bill.supplier_bill_reference or f"Bill#{bill.pk}",
            'orig_amount': bill.total_amount, 'orig_currency': bill.currency,
            'factor': 1  # Bill increases amount owed TO supplier
        })

    for pmt in VendorPayment.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status__in=[VendorPaymentStatus.COMPLETED.value]
    ).order_by('payment_date', 'pk'):
        raw_lines.append({
            'date': pmt.payment_date, 'type': str(_('Payment')),
            'ref': pmt.payment_number or f"Pmt#{pmt.pk}",
            'orig_amount': pmt.payment_amount, 'orig_currency': pmt.currency,
            'factor': -1  # Payment decreases amount owed TO supplier
        })

    # PLACEHOLDER: Add VendorDebitNote (Purchase Return) fetching and append to raw_lines with factor -1

    raw_lines.sort(
        key=lambda x: (x['date'], 0 if x['type'] == str(_('Bill')) else (1 if x['type'] == str(_('Payment')) else 2)))
    ob, proc_lines_stmt = ZERO_DECIMAL, []  # proc_lines_stmt to avoid clash with CustomerStatementLine type above

    for item in raw_lines:
        if item['date'] < start_date:
            conv_amt = ZERO_DECIMAL
            try:
                conv_amt = _convert_currency(company_id, item['orig_amount'], item['orig_currency'],
                                             effective_report_currency, item['date'])
            except CurrencyConversionError as cce:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt_ven:  # More specific key from second file
                    logger.warning(
                        f"Vendor Stmt Co {company_id} Supp {supplier_id}: OB conv error for {item['ref']}. Using orig amt. Error: {cce}")
                    conversion_errors_logged_stmt_ven.add(rate_key)
                conv_amt = item['orig_amount']
            ob += conv_amt * item['factor']

    run_bal = ob
    for item in raw_lines:
        if item['date'] >= start_date and item['date'] <= end_date:
            conv_amt = ZERO_DECIMAL
            try:
                conv_amt = _convert_currency(company_id, item['orig_amount'], item['orig_currency'],
                                             effective_report_currency, item['date'])
            except CurrencyConversionError as cce:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt_ven:
                    logger.warning(
                        f"Vendor Stmt Co {company_id} Supp {supplier_id}: Line item conv error for {item['ref']}. Using orig amt. Error: {cce}")
                    conversion_errors_logged_stmt_ven.add(rate_key)
                conv_amt = item['orig_amount']

            dr, cr = (conv_amt, None) if item['factor'] == -1 else (None, conv_amt)
            run_bal += conv_amt * item['factor']
            # Using generic StatementLine
            proc_lines_stmt.append(StatementLine(
                date=item['date'], transaction_type=item['type'], reference=item['ref'],
                debit=dr, credit=cr, balance=run_bal
            ))
    # Using generic StatementData
    return StatementData(
        party_pk=supplier_id, party_name=supplier_instance.name,
        statement_period_start=start_date, statement_period_end=end_date,
        report_currency=effective_report_currency,
        opening_balance=ob.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'), rounding=ROUND_HALF_UP),
        lines=proc_lines_stmt,
        closing_balance=run_bal.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'), rounding=ROUND_HALF_UP)
    )


# =============================================================================
# Placeholder function from the second file (UNCHANGED)
# =============================================================================
def list_customer_refunds(
        company_id: PK_TYPE,
        customer_id: Optional[PK_TYPE] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        report_currency: Optional[str] = None
) -> List[Dict[str, Any]]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company ID {company_id} not found.")

    # effective_report_currency = report_currency or company_instance.default_currency_code or 'USD' # Not used here

    logger.warning(f"list_customer_refunds Co {company_id}, Cust {customer_id or 'All'}: This is a placeholder. "
                   "Proper refund listing requires specific models/logic for identifying refund transactions.")
    return []