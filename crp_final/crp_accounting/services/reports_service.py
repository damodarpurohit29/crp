# crp_accounting/services/reports_service.py

import logging
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from datetime import date # timedelta wasn't used but can be kept if future use is planned
from typing import List, Dict, Tuple, Optional, Any, DefaultDict, TypedDict

from django.utils.translation import gettext_lazy as _
from django.db import models
from django.db.models import Sum, Q # F was not used, can be removed if not needed elsewhere
from django.db.models.functions import Coalesce
# from django.core.exceptions import ObjectDoesNotExist # Not directly used, but good for ORM interactions
# from django.utils import timezone # Not directly used, but good for date/time operations

logger = logging.getLogger("crp_accounting.services.reports")

# --- Model Imports ---
from ..models.coa import Account, AccountGroup, PLSection
from ..models.journal import VoucherLine, TransactionStatus, DrCrType
from ..models.receivables import CustomerInvoice, InvoiceStatus, CustomerPayment, PaymentAllocation, SMALL_TOLERANCE
from ..models.party import Party
from ..models.payables import (
    VendorBill,
    BillStatus as VendorBillStatus,
    VendorPayment,
    VendorPaymentAllocation # Used in AP Aging
)
from crp_core.enums import PaymentStatus as VendorPaymentStatus # For VendorPayment status checking

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

# --- Enum Imports ---
from crp_core.enums import AccountNature, AccountType, PartyType as CorePartyType, \
    PaymentStatus as CorePaymentStatus


# --- Custom Exceptions ---
class ReportGenerationError(Exception):
    """Base exception for report generation failures."""
    pass


class BalanceCalculationError(ReportGenerationError):
    """Exception for errors during account balance calculations."""
    pass


class CurrencyConversionError(ReportGenerationError):
    """Exception for errors during currency conversion."""
    pass


class DataIntegrityWarning(Warning):
    """Warning for potential data integrity issues found during report generation."""
    pass


# --- Constants ---
ZERO_DECIMAL = Decimal('0.00')
RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY = _("Retained Earnings (Calculated)")
RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER = "RETAINED_EARNINGS_CALCULATED"
DEFAULT_FX_RATE_PRECISION = 8
DEFAULT_AMOUNT_PRECISION = 2
DEFAULT_AR_AGING_BUCKETS_DAYS = [0, 30, 60, 90]
DEFAULT_AP_AGING_BUCKETS_DAYS = [0, 30, 60, 90]

# =============================================================================
# Type Definitions
# =============================================================================
PK_TYPE = Any  # Generic type for primary keys


class ProfitLossAccountDetail(TypedDict):
    account_pk: PK_TYPE
    account_number: str
    account_name: str
    amount: Decimal
    original_amount: Decimal
    original_currency: str
    is_subtotal_in_note: Optional[bool]


class ProfitLossLineItem(TypedDict):
    section_key: str
    title: str
    amount: Decimal
    is_subtotal: bool
    is_main_section_title: bool
    accounts: Optional[List[ProfitLossAccountDetail]]
    level: int
    has_note: Optional[bool]
    note_ref: Optional[str]


class BalanceSheetNode(TypedDict):
    id: PK_TYPE
    name: str
    type: str  # 'group' or 'account'
    level: int
    balance: Decimal
    currency: Optional[str]
    account_number: Optional[str]
    children: List['BalanceSheetNode']


class ProcessedAccountBalance(TypedDict):
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


class AgingEntry(TypedDict):
    party_pk: PK_TYPE
    party_name: str
    currency: str
    buckets: Dict[str, Decimal]
    total_due: Decimal


class StatementLine(TypedDict):
    date: date
    transaction_type: str
    reference: str
    debit: Optional[Decimal]
    credit: Optional[Decimal]
    balance: Decimal


class StatementData(TypedDict):
    party_pk: PK_TYPE
    party_name: str
    statement_period_start: date
    statement_period_end: date
    report_currency: str
    opening_balance: Decimal
    lines: List[StatementLine]
    closing_balance: Decimal


class ARAgingEntry(TypedDict):
    customer_pk: PK_TYPE
    customer_name: str
    currency: str
    buckets: Dict[str, Decimal]
    total_due: Decimal


class CustomerStatementLine(TypedDict):
    date: date
    transaction_type: str
    reference: str
    debit: Optional[Decimal]
    credit: Optional[Decimal]
    balance: Decimal


class CustomerStatementData(TypedDict):
    customer_pk: PK_TYPE
    customer_name: str
    statement_period_start: date
    statement_period_end: date
    report_currency: str
    opening_balance: Decimal
    lines: List[CustomerStatementLine]
    closing_balance: Decimal


# =============================================================================
# Currency Conversion Utility
# =============================================================================
def _get_exchange_rate(company_id: Optional[PK_TYPE], from_currency: str, to_currency: str,
                       conversion_date: date) -> Decimal:
    if not ExchangeRate:
        raise CurrencyConversionError("ExchangeRate model is not available. Cannot perform currency conversion.")
    if from_currency == to_currency:
        return Decimal('1.0')

    rate_obj = ExchangeRate.objects.filter(
        company_id=company_id, from_currency=from_currency, to_currency=to_currency, date__lte=conversion_date
    ).order_by('-date').first()
    if rate_obj:
        return Decimal(rate_obj.rate)

    inverse_rate_obj = ExchangeRate.objects.filter(
        company_id=company_id, from_currency=to_currency, to_currency=from_currency, date__lte=conversion_date
    ).order_by('-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(
            Decimal(f'1e-{DEFAULT_FX_RATE_PRECISION}'), rounding=ROUND_HALF_UP
        )

    rate_obj = ExchangeRate.objects.filter(
        company_id__isnull=True, from_currency=from_currency, to_currency=to_currency, date__lte=conversion_date
    ).order_by('-date').first()
    if rate_obj:
        return Decimal(rate_obj.rate)

    inverse_rate_obj = ExchangeRate.objects.filter(
        company_id__isnull=True, from_currency=to_currency, to_currency=from_currency, date__lte=conversion_date
    ).order_by('-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(
            Decimal(f'1e-{DEFAULT_FX_RATE_PRECISION}'), rounding=ROUND_HALF_UP
        )

    logger.error(
        f"No exchange rate found for Co {company_id or 'Global'} from {from_currency} to {to_currency} on or before {conversion_date}.")
    raise CurrencyConversionError(
        f"Exchange rate missing: {from_currency} to {to_currency} for {conversion_date} (Co: {company_id or 'Global'}).")


def _convert_currency(company_id: Optional[PK_TYPE], amount: Decimal, from_currency: str, to_currency: str,
                      conversion_date: date, precision: int = DEFAULT_AMOUNT_PRECISION) -> Decimal:
    if from_currency == to_currency:
        return amount.quantize(Decimal(f'1e-{precision}'), rounding=ROUND_HALF_UP)
    if amount == ZERO_DECIMAL:
        return ZERO_DECIMAL
    try:
        rate = _get_exchange_rate(company_id, from_currency, to_currency, conversion_date)
        converted = (amount * rate).quantize(Decimal(f'1e-{precision}'), rounding=ROUND_HALF_UP)
        return converted
    except CurrencyConversionError:
        raise
    except Exception as e:
        logger.exception(
            f"Error during currency conversion from {from_currency} to {to_currency} for Co {company_id or 'Global'}")
        raise CurrencyConversionError(f"General currency conversion failure: {str(e)}") from e


# =============================================================================
# Core Balance Calculation Helper
# =============================================================================
def _calculate_account_balances(company_id: PK_TYPE, as_of_date: date, target_report_currency: str) -> \
        Dict[PK_TYPE, ProcessedAccountBalance]:
    if not Company: raise ReportGenerationError("Company model not available.")
    if not company_id: raise ValueError("company_id must be provided for balance calculation.")

    logger.debug(
        f"Calculating account balances for Company ID {company_id} as of {as_of_date}, target currency {target_report_currency}...")
    account_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    conversion_errors_logged = set()

    try:
        aggregation = VoucherLine.objects.filter(
            voucher__company_id=company_id,
            voucher__status=TransactionStatus.POSTED.value,
            voucher__date__lte=as_of_date,
            account__company_id=company_id,
            account__is_active=True
        ).values(
            'account'
        ).annotate(
            total_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), ZERO_DECIMAL,
                                 output_field=models.DecimalField()),
            total_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), ZERO_DECIMAL,
                                  output_field=models.DecimalField())
        ).values(
            'account__id', 'account__account_number', 'account__account_name',
            'account__account_type', 'account__account_nature', 'account__account_group_id',
            'account__currency', 'account__pl_section',
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
                        f"Co {company_id}: Balance calc currency conversion error: {cce} for Account {pk} ({item['account__account_number']}). "
                        "Using original balance. Report may be mixed-currency.")
                    conversion_errors_logged.add(rate_key)
                converted_balance = original_balance

            account_balances[pk] = ProcessedAccountBalance(
                account_pk=pk,
                account_number=item['account__account_number'],
                account_name=str(item['account__account_name']),
                account_type=item['account__account_type'],
                account_nature=nature,
                account_group_pk=item['account__account_group_id'],
                original_currency=acc_currency,
                original_balance=original_balance,
                converted_balance=converted_balance,
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
        logger.debug(
            f"Successfully calculated balances for {len(account_balances)} accounts for Company ID {company_id}.")
        return account_balances
    except Exception as e:
        logger.exception(f"Unexpected error in _calculate_account_balances for Company ID {company_id}.")
        raise BalanceCalculationError(f"Failed to calculate account balances: {str(e)}") from e


# =============================================================================
# Hierarchy Building Helpers
# =============================================================================
def _build_group_hierarchy_recursive(
        parent_group_id: Optional[PK_TYPE],
        all_groups: Dict[PK_TYPE, AccountGroup],
        account_data_map: Dict[PK_TYPE, ProcessedAccountBalance],
        level: int
) -> Tuple[List[Dict[str, Any]], Decimal, Decimal]:
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
                    f"Account {acc_pk} ({acc_data.get('account_number')}) has unknown nature '{nature}'. Balance signs might be incorrect on Trial Balance.")

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


def _build_balance_sheet_hierarchy(
        parent_group_id: Optional[PK_TYPE],
        all_groups: Dict[PK_TYPE, AccountGroup],
        account_balances_bs: Dict[PK_TYPE, ProcessedAccountBalance],
        level: int
) -> Tuple[List[BalanceSheetNode], Decimal]:
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
# Public Report Generation Functions (Trial Balance, P&L, Balance Sheet)
# =============================================================================
def generate_trial_balance_structured(company_id: PK_TYPE, as_of_date: date, report_currency: Optional[str] = None) -> \
        Dict[str, Any]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code
    if not effective_report_currency:
        effective_report_currency = 'USD'
        logger.warning(
            f"TB Gen for Co ID {company_id}: No report_currency provided and company has no default. Defaulting to USD.")

    logger.info(
        f"Generating Trial Balance for Company ID {company_id} (Currency: {effective_report_currency}) as of {as_of_date}")

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
                'account_pk': pk,
                'account_number': data['account_number'],
                'account_name': str(data['account_name']),
                'debit': debit_amount,
                'credit': credit_amount,
                'currency': effective_report_currency
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
            f"Trial Balance for Co ID {company_id} is OUT OF BALANCE! "
            f"Debit Total: {grand_total_debit}, Credit Total: {grand_total_credit}, "
            f"Difference: {grand_total_debit - grand_total_credit}")

    return {
        'company_id': company_id,
        'company_name': company_instance.name,
        'as_of_date': as_of_date,
        'report_currency': effective_report_currency,
        'hierarchy': hierarchy,
        'flat_entries': flat_entries_list,
        'total_debit': grand_total_debit,
        'total_credit': grand_total_credit,
        'is_balanced': is_balanced,
    }


DEFAULT_PL_STRUCTURE_DEFINITION = [
    {'key': PLSection.REVENUE.value, 'title': _('Revenue'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': PLSection.COGS.value, 'title': _('Cost of Goods Sold / Services'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': 'GROSS_PROFIT', 'title': _('Gross Profit'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [(PLSection.REVENUE.value, True), (PLSection.COGS.value, False)]},
    {'key': PLSection.OPERATING_EXPENSE.value, 'title': _('Operating Expenses'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section', 'has_note': True, 'note_ref': 'Note_OpEx',
     'note_title': _('Details of Operating Expenses')},
    {'key': PLSection.DEPRECIATION_AMORTIZATION.value, 'title': _('Depreciation & Amortization'), 'level': 0,
     'is_subtotal': False, 'calculation_method': 'sum_section'},
    {'key': 'OPERATING_PROFIT', 'title': _('Operating Profit / (Loss)'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [('GROSS_PROFIT', True), (PLSection.OPERATING_EXPENSE.value, False),
                           (PLSection.DEPRECIATION_AMORTIZATION.value, False)]},
    {'key': PLSection.OTHER_INCOME.value, 'title': _('Other Income'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': PLSection.OTHER_EXPENSE.value, 'title': _('Other Expenses'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': 'PROFIT_BEFORE_TAX', 'title': _('Profit / (Loss) Before Tax'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [('OPERATING_PROFIT', True), (PLSection.OTHER_INCOME.value, True),
                           (PLSection.OTHER_EXPENSE.value, False)]},
    {'key': PLSection.TAX_EXPENSE.value, 'title': _('Tax Expense'), 'level': 0, 'is_subtotal': False,
     'calculation_method': 'sum_section'},
    {'key': 'NET_INCOME', 'title': _('Net Income / (Loss)'), 'level': 0, 'is_subtotal': True,
     'calculation_basis': [('PROFIT_BEFORE_TAX', True), (PLSection.TAX_EXPENSE.value, False)]},
]


def generate_profit_loss(company_id: PK_TYPE, start_date: date, end_date: date,
                         report_currency: Optional[str] = None) -> Dict[str, Any]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code
    if not effective_report_currency:
        effective_report_currency = 'USD'
        logger.warning(f"P&L Gen for Co ID {company_id}: No report_currency and no company default. Defaulting to USD.")

    logger.info(
        f"Generating P&L for Company ID {company_id} (Currency: {effective_report_currency}) from {start_date} to {end_date}")
    if start_date > end_date:
        raise ValueError("Start date cannot be after end date for Profit & Loss report.")

    section_totals: DefaultDict[str, Decimal] = defaultdict(Decimal)
    section_details: DefaultDict[str, List[ProfitLossAccountDetail]] = defaultdict(list)
    calculated_values: Dict[str, Decimal] = {}
    conversion_errors_logged_pl = set()
    financial_notes_data: Dict[str, Dict[str, Any]] = {}

    pl_account_types = [AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value]

    account_movements = VoucherLine.objects.filter(
        voucher__company_id=company_id,
        voucher__status=TransactionStatus.POSTED.value,
        voucher__date__gte=start_date,
        voucher__date__lte=end_date,
        account__company_id=company_id,
        account__account_type__in=pl_account_types,
        account__is_active=True
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
                f"P&L Co {company_id}: Account {acc_pk} ({item['account__account_number']}) has no PLSection or is 'NONE'. Skipping.")
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
                    f"P&L Co {company_id}: Currency conversion error: {cce} for Account {acc_pk} ({item['account__account_number']}). Using original amount.")
                conversion_errors_logged_pl.add(rate_key)
            movement_in_report_currency = movement_in_acc_currency

        section_totals[pl_section_str] += movement_in_report_currency
        section_details[pl_section_str].append(ProfitLossAccountDetail(
            account_pk=acc_pk, account_number=item['account__account_number'],
            account_name=str(item['account__account_name']),
            amount=movement_in_report_currency, original_amount=movement_in_acc_currency,
            original_currency=acc_currency,
            is_subtotal_in_note=False
        ))

    report_lines: List[ProfitLossLineItem] = []
    for section_def in DEFAULT_PL_STRUCTURE_DEFINITION:
        key = section_def['key']
        title = str(section_def['title'])
        is_subtotal = section_def['is_subtotal']
        level = section_def.get('level', 0)
        calculation_method = section_def.get('calculation_method')
        has_note_flag = section_def.get('has_note', False)
        note_reference = section_def.get('note_ref')
        note_title_text = str(section_def.get('note_title', title))

        accounts_for_this_line: Optional[List[ProfitLossAccountDetail]] = None
        current_line_amount_val: Decimal = ZERO_DECIMAL

        if calculation_method == 'sum_section':
            current_line_amount_val = section_totals.get(key, ZERO_DECIMAL)
            accounts_for_this_line = sorted(section_details.get(key, []), key=lambda x: x['account_number'])

            if has_note_flag and note_reference and accounts_for_this_line:
                financial_notes_data[note_reference] = {
                    'title': note_title_text,
                    'details': accounts_for_this_line,
                    'total_amount': current_line_amount_val
                }
        elif is_subtotal:
            for basis_key, is_positive_impact in section_def.get('calculation_basis', []):
                amount_from_basis = calculated_values.get(basis_key, ZERO_DECIMAL)
                current_line_amount_val += amount_from_basis * (
                    Decimal('1.0') if is_positive_impact else Decimal('-1.0'))

        calculated_values[key] = current_line_amount_val

        show_this_line = is_subtotal or \
                         (calculation_method == 'sum_section' and (current_line_amount_val != ZERO_DECIMAL or
                                                                   (accounts_for_this_line and len(
                                                                       accounts_for_this_line) > 0)))

        if show_this_line:
            report_lines.append(ProfitLossLineItem(
                section_key=key, title=title, amount=current_line_amount_val,
                is_subtotal=is_subtotal,
                is_main_section_title=bool(calculation_method and not is_subtotal),
                accounts=accounts_for_this_line if not has_note_flag else None,
                level=level,
                has_note=has_note_flag, note_ref=note_reference
            ))

    net_income_calculated = calculated_values.get('NET_INCOME', ZERO_DECIMAL)
    return {
        'company_id': company_id,
        'company_name': company_instance.name,
        'start_date': start_date,
        'end_date': end_date,
        'report_currency': effective_report_currency,
        'report_lines': report_lines,
        'net_income': net_income_calculated,
        'financial_notes_data': financial_notes_data
    }


def generate_balance_sheet(company_id: PK_TYPE, as_of_date: date, report_currency: Optional[str] = None) -> Dict[
    str, Any]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code
    if not effective_report_currency:
        effective_report_currency = 'USD'
        logger.warning(f"BS Gen for Co ID {company_id}: No report_currency and no company default. Defaulting to USD.")

    logger.info(
        f"Generating Balance Sheet for Company ID {company_id} (Currency: {effective_report_currency}) as of {as_of_date}")

    all_account_balances = _calculate_account_balances(company_id, as_of_date, effective_report_currency)

    retained_earnings_from_pl_accounts = ZERO_DECIMAL
    pl_types_for_retained_earnings = {AccountType.INCOME.value, AccountType.EXPENSE.value,
                                      AccountType.COST_OF_GOODS_SOLD.value}
    for acc_data in all_account_balances.values():
        if acc_data['account_type'] in pl_types_for_retained_earnings:
            balance = acc_data['converted_balance']
            if acc_data['account_nature'] == AccountNature.CREDIT.value:
                retained_earnings_from_pl_accounts += balance
            elif acc_data['account_nature'] == AccountNature.DEBIT.value:
                retained_earnings_from_pl_accounts -= balance

    asset_balances_map: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    liability_balances_map: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    equity_balances_map: Dict[PK_TYPE, ProcessedAccountBalance] = {}

    for pk, data in all_account_balances.items():
        if data['account_type'] == AccountType.ASSET.value:
            asset_balances_map[pk] = data
        elif data['account_type'] == AccountType.LIABILITY.value:
            liability_balances_map[pk] = data
        elif data['account_type'] == AccountType.EQUITY.value:
            equity_balances_map[pk] = data

    company_groups = {group.pk: group for group in AccountGroup.objects.filter(company_id=company_id)}

    asset_hierarchy_nodes, total_assets_val = _build_balance_sheet_hierarchy(None, company_groups, asset_balances_map, 0)
    liability_hierarchy_nodes, total_liabilities_val = _build_balance_sheet_hierarchy(None, company_groups, liability_balances_map, 0)
    equity_hierarchy_nodes, total_explicit_equity_val = _build_balance_sheet_hierarchy(None, company_groups, equity_balances_map, 0)

    retained_earnings_node_data: BalanceSheetNode = {
        'id': RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER,
        'name': str(RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY),
        'type': 'account', 'level': 0,
        'balance': retained_earnings_from_pl_accounts,
        'currency': effective_report_currency,
        'account_number': None,
        'children': []
    }
    equity_hierarchy_nodes.append(retained_earnings_node_data)
    total_equity_val = total_explicit_equity_val + retained_earnings_from_pl_accounts

    balance_difference_val = total_assets_val - (total_liabilities_val + total_equity_val)
    is_bs_balanced = abs(balance_difference_val) < Decimal('0.01')
    if not is_bs_balanced:
        logger.error(
            f"Balance Sheet for Co ID {company_id} is OUT OF BALANCE! "
            f"Assets: {total_assets_val}, Liabilities: {total_liabilities_val}, Equity: {total_equity_val}, "
            f"Total L+E: {total_liabilities_val + total_equity_val}, Difference: {balance_difference_val}"
        )

    return {
        'company_id': company_id,
        'company_name': company_instance.name,
        'as_of_date': as_of_date,
        'report_currency': effective_report_currency,
        'is_balanced': is_bs_balanced,
        'balance_difference': balance_difference_val,
        'assets': {'hierarchy': asset_hierarchy_nodes, 'total': total_assets_val},
        'liabilities': {'hierarchy': liability_hierarchy_nodes, 'total': total_liabilities_val},
        'equity': {'hierarchy': equity_hierarchy_nodes, 'total': total_equity_val}
    }


# =============================================================================
# Accounts Receivable (AR) Specific Report Functions
# =============================================================================
def generate_ar_aging_report(
        company_id: PK_TYPE,
        as_of_date: date,
        report_currency: Optional[str] = None,
        aging_buckets_days: Optional[List[int]] = None
) -> Dict[str, Any]:
    if not Company: raise ReportGenerationError("Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.")

    effective_report_currency = report_currency or company_instance.default_currency_code
    if not effective_report_currency:
        effective_report_currency = 'USD'
        logger.warning(
            f"AR Aging Gen for Co ID {company_id}: No report_currency and no company default. Defaulting to USD.")

    logger.info(
        f"Generating AR Aging for Company ID {company_id} (Currency: {effective_report_currency}) as of {as_of_date}")

    current_buckets_definition = aging_buckets_days if aging_buckets_days is not None else DEFAULT_AR_AGING_BUCKETS_DAYS
    effective_buckets_definition = sorted(list(set([0] + current_buckets_definition)))

    bucket_labels_list: List[str] = []
    if not effective_buckets_definition:
        bucket_labels_list.append(str(_("Total Due")))
    else:
        bucket_labels_list.append(str(_("Current")))
        for i in range(len(effective_buckets_definition) - 1):
            lower_b = effective_buckets_definition[i] + 1
            upper_b = effective_buckets_definition[i + 1]
            bucket_labels_list.append(f"{lower_b}-{upper_b} {str(_('Days'))}")
        bucket_labels_list.append(f"{effective_buckets_definition[-1] + 1}+ {str(_('Days'))}")

    potentially_outstanding_invoices = CustomerInvoice.objects.filter(
        company_id=company_id,
        invoice_date__lte=as_of_date,
        status__in=[
            InvoiceStatus.DRAFT.value, InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value,
            InvoiceStatus.PAID.value, InvoiceStatus.OVERDUE.value
        ]
    ).select_related('customer')

    ar_aging_data_map: DefaultDict[PK_TYPE, ARAgingEntry] = defaultdict(
        lambda: ARAgingEntry(customer_pk=None, customer_name="", currency=effective_report_currency, # type: ignore
                             buckets={label: ZERO_DECIMAL for label in bucket_labels_list}, total_due=ZERO_DECIMAL)
    )
    grand_totals_per_bucket: Dict[str, Decimal] = {label: ZERO_DECIMAL for label in bucket_labels_list}
    grand_total_due_overall = ZERO_DECIMAL
    conversion_errors_logged_ar_aging = set()

    for invoice in potentially_outstanding_invoices:
        if not invoice.customer:
            logger.warning(
                f"AR Aging Co {company_id}: Invoice {invoice.invoice_number or invoice.pk} is missing a customer. Skipping.")
            continue

        amount_paid_for_invoice_as_of_report_date = PaymentAllocation.objects.filter(
            invoice=invoice,
            payment__payment_date__lte=as_of_date,
            payment__status__in=[
                CorePaymentStatus.APPLIED.value,
                CorePaymentStatus.COMPLETED.value,
            ]
        ).aggregate(
            total_applied_specific_date=Coalesce(Sum('amount_applied'), ZERO_DECIMAL)
        )['total_applied_specific_date']

        invoice_outstanding_for_report = invoice.total_amount - amount_paid_for_invoice_as_of_report_date

        if invoice_outstanding_for_report <= SMALL_TOLERANCE:
            continue

        invoice_due_date = invoice.due_date or invoice.invoice_date

        days_overdue = (as_of_date - invoice_due_date).days

        chosen_bucket_label_for_invoice: str
        if days_overdue <= effective_buckets_definition[0]:
            chosen_bucket_label_for_invoice = bucket_labels_list[0]
        else:
            assigned = False
            for i in range(len(effective_buckets_definition) - 1):
                if days_overdue > effective_buckets_definition[i] and days_overdue <= effective_buckets_definition[
                    i + 1]:
                    chosen_bucket_label_for_invoice = bucket_labels_list[i + 1]
                    assigned = True
                    break
            if not assigned:
                chosen_bucket_label_for_invoice = bucket_labels_list[-1]

        outstanding_in_report_currency: Decimal
        try:
            outstanding_in_report_currency = _convert_currency(
                company_id, invoice_outstanding_for_report, invoice.currency, effective_report_currency, as_of_date
            )
        except CurrencyConversionError as cce:
            rate_key = (invoice.currency, effective_report_currency)
            if rate_key not in conversion_errors_logged_ar_aging:
                logger.warning(
                    f"AR Aging Co {company_id}: Currency conversion error: {cce} for Invoice {invoice.invoice_number or invoice.pk}. "
                    "Using original calculated outstanding amount.")
                conversion_errors_logged_ar_aging.add(rate_key)
            outstanding_in_report_currency = invoice_outstanding_for_report

        customer_aging_entry = ar_aging_data_map[invoice.customer.pk]
        if not customer_aging_entry['customer_pk']:
            customer_aging_entry['customer_pk'] = invoice.customer.pk
            customer_aging_entry['customer_name'] = invoice.customer.name

        customer_aging_entry['buckets'][chosen_bucket_label_for_invoice] = \
            customer_aging_entry['buckets'].get(chosen_bucket_label_for_invoice,
                                                ZERO_DECIMAL) + outstanding_in_report_currency
        customer_aging_entry['total_due'] += outstanding_in_report_currency

        grand_totals_per_bucket[chosen_bucket_label_for_invoice] = \
            grand_totals_per_bucket.get(chosen_bucket_label_for_invoice, ZERO_DECIMAL) + outstanding_in_report_currency
        grand_total_due_overall += outstanding_in_report_currency

    sorted_ar_aging_data = sorted(list(ar_aging_data_map.values()), key=lambda x: x['customer_name'])

    return {
        'company_id': company_id,
        'company_name': company_instance.name,
        'as_of_date': as_of_date,
        'report_currency': effective_report_currency,
        'bucket_labels': bucket_labels_list,
        'aging_data': sorted_ar_aging_data,
        'grand_totals_by_bucket': grand_totals_per_bucket,
        'grand_total_due_all_customers': grand_total_due_overall,
        'effective_buckets_definition_for_debug': effective_buckets_definition
    }


def generate_customer_statement(
        company_id: PK_TYPE,
        customer_id: PK_TYPE,
        start_date: date,
        end_date: date,
        report_currency: Optional[str] = None
) -> CustomerStatementData:
    if not (Company and Party): raise ReportGenerationError("Company or Party model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
        customer_instance = Party.objects.get(pk=customer_id, company_id=company_id,
                                              party_type=CorePartyType.CUSTOMER.value)
    except Company.DoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.")
    except Party.DoesNotExist:
        raise ReportGenerationError(
            f"Customer with ID {customer_id} (Type: Customer) not found for company {company_id}.")
    except AttributeError:
        logger.error(
            "Customer Statement: Party model or CorePartyType enum might be missing 'party_type' or 'CUSTOMER' value.")
        raise ReportGenerationError("Misconfiguration in Party model or CorePartyType enum for customer statement.")

    effective_report_currency = report_currency or company_instance.default_currency_code
    if not effective_report_currency:
        effective_report_currency = 'USD'
        logger.warning(
            f"Customer Stmt Gen for Co ID {company_id}, Cust {customer_id}: No report_currency and no company default. Defaulting to USD.")

    logger.info(
        f"Generating Customer Statement for Co ID {company_id}, Customer {customer_id} (Currency: {effective_report_currency}) from {start_date} to {end_date}")

    statement_transactions_raw: List[Dict[str, Any]] = []
    conversion_errors_logged_cust_stmt = set()

    customer_invoices = CustomerInvoice.objects.filter(
        company_id=company_id, customer_id=customer_id,
        status__in=[
            InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value,
            InvoiceStatus.PAID.value, InvoiceStatus.OVERDUE.value
        ]
    ).order_by('invoice_date', 'pk')

    for inv in customer_invoices:
        statement_transactions_raw.append({
            'date': inv.invoice_date,
            'type': str(_('Invoice')),
            'ref': inv.invoice_number or f"Inv#{inv.pk}",
            'orig_amount': inv.total_amount,
            'orig_currency': inv.currency,
            'debit_credit_factor': Decimal('1.0')
        })

    customer_payments_received = CustomerPayment.objects.filter(
        company_id=company_id, customer_id=customer_id,
        status__in=[
            CorePaymentStatus.APPLIED.value,
            CorePaymentStatus.PARTIALLY_APPLIED.value,
            CorePaymentStatus.COMPLETED.value
        ]
    ).order_by('payment_date', 'pk')

    for pmt in customer_payments_received:
        statement_transactions_raw.append({
            'date': pmt.payment_date,
            'type': str(_('Payment')),
            'ref': pmt.reference_number or f"Pmt#{pmt.pk}",
            'orig_amount': pmt.amount_received,
            'orig_currency': pmt.currency,
            'debit_credit_factor': Decimal('-1.0')
        })

    statement_transactions_raw.sort(
        key=lambda x: (x['date'], 0 if x['type'] == str(_('Invoice')) else 1))

    opening_balance_val: Decimal = ZERO_DECIMAL
    for item in statement_transactions_raw:
        if item['date'] < start_date:
            amount_for_ob = item['orig_amount'] * item['debit_credit_factor']
            converted_ob_amount: Decimal
            try:
                converted_ob_amount = _convert_currency(company_id, amount_for_ob, item['orig_currency'],
                                                        effective_report_currency, item['date'])
            except CurrencyConversionError as cce:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_cust_stmt:
                    logger.warning(
                        f"Cust Stmt Co {company_id} Cust {customer_id}: OB conversion error for {item['ref']} on {item['date']}. Using unconverted. Error: {cce}")
                    conversion_errors_logged_cust_stmt.add(rate_key)
                converted_ob_amount = amount_for_ob
            opening_balance_val += converted_ob_amount

    processed_statement_lines_list: List[CustomerStatementLine] = []
    current_running_balance = opening_balance_val

    for item in statement_transactions_raw:
        if item['date'] >= start_date and item['date'] <= end_date:
            item_amount_in_orig_currency = item['orig_amount']
            converted_item_amount: Decimal
            try:
                converted_item_amount = _convert_currency(company_id, item_amount_in_orig_currency,
                                                          item['orig_currency'],
                                                          effective_report_currency, item['date'])
            except CurrencyConversionError as cce:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_cust_stmt:
                    logger.warning(
                        f"Cust Stmt Co {company_id} Cust {customer_id}: Line item conversion error for {item['ref']} on {item['date']}. Using unconverted. Error: {cce}")
                    conversion_errors_logged_cust_stmt.add(rate_key)
                converted_item_amount = item_amount_in_orig_currency

            debit_value, credit_value = None, None
            if item['debit_credit_factor'] > 0:
                debit_value = converted_item_amount
                current_running_balance += converted_item_amount
            else:
                credit_value = converted_item_amount
                current_running_balance -= converted_item_amount

            processed_statement_lines_list.append(CustomerStatementLine(
                date=item['date'], transaction_type=item['type'], reference=item['ref'],
                debit=debit_value, credit=credit_value, balance=current_running_balance
            ))

    return CustomerStatementData(
        customer_pk=customer_id, customer_name=customer_instance.name,
        statement_period_start=start_date, statement_period_end=end_date,
        report_currency=effective_report_currency,
        opening_balance=opening_balance_val.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'), rounding=ROUND_HALF_UP),
        lines=processed_statement_lines_list,
        closing_balance=current_running_balance.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'),
                                                         rounding=ROUND_HALF_UP)
    )


# =============================================================================
# Accounts Payable (AP) Specific Report Functions
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
                              report_currency: Optional[str] = None) -> Dict[str, Any]:
    """
    Generates a complete and robust data structure for a vendor statement.
    This version includes all fixes for data fetching, currency conversion, and number formatting.
    """
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

    # --- Fetching Bill Data ---
    for bill in VendorBill.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status__in=[VendorBillStatus.APPROVED.value, VendorBillStatus.PARTIALLY_PAID.value,
                        VendorBillStatus.PAID.value]
    ).order_by('issue_date', 'pk'):
        raw_lines.append({
            'date': bill.issue_date, 'type': str(_('Bill')),
            'ref': bill.bill_number or bill.supplier_bill_reference or f"Bill#{bill.pk}",
            'orig_amount': bill.total_amount, 'orig_currency': bill.currency,
            'factor': 1
        })

    # --- FIX: Fetching CORRECT Payment Data ---
    # Based on the AttributeError, we know only COMPLETED exists, not PAID.
    # This now correctly reflects the available statuses in your enum.
    for pmt in VendorPayment.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status__in=[VendorPaymentStatus.COMPLETED.value]
    ).order_by('payment_date', 'pk'):
        raw_lines.append({
            'date': pmt.payment_date, 'type': str(_('Payment')),
            'ref': pmt.payment_number or f"Pmt#{pmt.pk}",
            'orig_amount': pmt.payment_amount, 'orig_currency': pmt.currency,
            'factor': -1
        })

    raw_lines.sort(
        key=lambda x: (x['date'], 0 if x['type'] == str(_('Bill')) else (1 if x['type'] == str(_('Payment')) else 2)))

    opening_balance = ZERO_DECIMAL
    processed_lines = []

    # Calculate Opening Balance with robust error handling
    for item in raw_lines:
        if item['date'] < start_date:
            conv_amt = ZERO_DECIMAL
            # FIX: Gracefully handle missing currency rates
            try:
                conv_amt = _convert_currency(company_id, item['orig_amount'], item['orig_currency'],
                                             effective_report_currency, item['date'])
            except CurrencyConversionError as e:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt_ven:
                    logger.warning(f"Vendor Stmt OB Calc - Co {company_id}: {e}. Defaulting to original amount.")
                    conversion_errors_logged_stmt_ven.add(rate_key)
                conv_amt = item['orig_amount']
            opening_balance += conv_amt * item['factor']

    # Process lines for the statement period with all fixes
    running_balance = opening_balance
    for item in raw_lines:
        if start_date <= item['date'] <= end_date:
            conv_amt = ZERO_DECIMAL
            # FIX: Gracefully handle missing currency rates
            try:
                conv_amt = _convert_currency(company_id, item['orig_amount'], item['orig_currency'],
                                             effective_report_currency, item['date'])
            except CurrencyConversionError as e:
                rate_key = (item['orig_currency'], effective_report_currency, item['date'])
                if rate_key not in conversion_errors_logged_stmt_ven:
                    logger.warning(f"Vendor Stmt Line Item - Co {company_id}: {e}. Defaulting to original amount.")
                    conversion_errors_logged_stmt_ven.add(rate_key)
                conv_amt = item['orig_amount']

            # FIX: Populate the correct debit/credit columns
            payment_or_debit = conv_amt if item['factor'] == -1 else None
            bill_or_credit = conv_amt if item['factor'] == 1 else None

            running_balance += conv_amt * item['factor']

            # Create the dictionary for the template with all fixes
            processed_lines.append({
                'date': item['date'],
                'transaction_type': item['type'],
                'reference': item['ref'],
                # FIX: Correctly format all numbers to prevent long decimals
                'payment_or_debit': payment_or_debit.quantize(
                    Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}')) if payment_or_debit is not None else None,
                'bill_or_credit': bill_or_credit.quantize(
                    Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}')) if bill_or_credit is not None else None,
                'balance': running_balance.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}'))
            })

    # Return a simple, flexible dictionary that the view can easily use
    return {
        'supplier': supplier_instance,
        'opening_balance': opening_balance.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}')),
        'lines': processed_lines,
        'closing_balance': running_balance.quantize(Decimal(f'1e-{DEFAULT_AMOUNT_PRECISION}')),
        'statement_period_start': start_date,
        'statement_period_end': end_date,
        'report_currency': effective_report_currency,
        'report_currency_symbol': company_instance.default_currency_symbol or '$'
    }
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