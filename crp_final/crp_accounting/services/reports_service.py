# crp_accounting/services/reports_service.py

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation as DecimalInvalidOperation, ROUND_HALF_UP
from datetime import date
from typing import List, Dict, Tuple, Optional, Any, DefaultDict, TypedDict

from django.utils.translation import gettext_lazy as _
from django.db import models  # For specific ORM exceptions if needed
from django.db.models import Sum, Q, F
from django.db.models.functions import Coalesce
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned

logger = logging.getLogger(__name__)

# --- Model Imports ---
from ..models.coa import Account, AccountGroup, PLSection
from ..models.journal import VoucherLine, TransactionStatus, DrCrType

try:
    # Assuming ExchangeRate is in base.py or common.py within crp_accounting.models
    from ..models.base import ExchangeRate  # Or from ..models.common import ExchangeRate
except ImportError:
    logger.error(
        "Reports Service: CRITICAL - ExchangeRate model not found. Currency conversion will be non-functional.")
    ExchangeRate = None  # type: ignore

# --- Company Import ---
try:
    from company.models import Company
except ImportError:
    logger.error("Reports Service: CRITICAL - Company model not found. Multi-tenancy is fundamentally broken.")
    Company = None  # type: ignore

# --- Enum Imports ---
from crp_core.enums import AccountNature, AccountType


# --- Custom Exceptions ---
class ReportGenerationError(Exception):
    """Base exception for errors during report generation."""
    pass


class BalanceCalculationError(ReportGenerationError):
    """Exception for errors specifically during balance calculations."""
    pass


class CurrencyConversionError(ReportGenerationError):
    """Exception for errors during currency conversion attempts."""
    pass


class DataIntegrityWarning(Warning):  # Use Warning for non-critical issues
    """Warning for data inconsistencies that might affect report accuracy but don't halt generation."""
    pass


# --- Constants ---
ZERO_DECIMAL = Decimal('0.00')
RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY = _("Retained Earnings (Calculated)")
RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER = "RETAINED_EARNINGS_CALCULATED"  # Use a string constant
DEFAULT_FX_RATE_PRECISION = 8  # Precision for storing/using exchange rates
DEFAULT_AMOUNT_PRECISION = 2  # Standard precision for monetary amounts

# =============================================================================
# Type Definitions (Ensure PK_TYPE matches your Account/AccountGroup models)
# =============================================================================
PK_TYPE = Any  # Typically int or str (for UUID)


class ProfitLossAccountDetail(TypedDict):
    account_pk: PK_TYPE
    account_number: str
    account_name: str
    amount: Decimal  # This will be in the report_currency
    original_amount: Decimal
    original_currency: str


class ProfitLossLineItem(TypedDict):
    section_key: str
    title: str
    amount: Decimal  # In report_currency
    is_subtotal: bool
    accounts: Optional[List[ProfitLossAccountDetail]]


class BalanceSheetNode(TypedDict):
    id: PK_TYPE
    name: str
    type: str
    level: int
    balance: Decimal  # In report_currency
    currency: Optional[str]  # Original currency for leaf accounts, report_currency for RE
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
    converted_balance: Decimal  # Balance converted to the target_report_currency
    pl_section: str


# =============================================================================
# Currency Conversion
# =============================================================================
def _get_exchange_rate(
        company_id: Optional[PK_TYPE],
        from_currency: str,
        to_currency: str,
        conversion_date: date
) -> Decimal:
    if not ExchangeRate:
        raise CurrencyConversionError("ExchangeRate model is not available for currency conversion.")
    if from_currency == to_currency:
        return Decimal('1.0')

    # Try company-specific rate first, then global rate (company_id is None)
    # Order by date descending to get the latest rate on or before the conversion_date.
    company_specific_query = Q(company_id=company_id)
    global_query = Q(company_id__isnull=True)

    direct_rate_conditions = Q(from_currency=from_currency, to_currency=to_currency, date__lte=conversion_date)
    inverse_rate_conditions = Q(from_currency=to_currency, to_currency=from_currency, date__lte=conversion_date)

    # Attempt 1: Company-specific direct rate
    rate_obj = ExchangeRate.objects.filter(company_specific_query & direct_rate_conditions).order_by('-date').first()
    if rate_obj: return Decimal(rate_obj.rate)

    # Attempt 2: Company-specific inverse rate
    inverse_rate_obj = ExchangeRate.objects.filter(company_specific_query & inverse_rate_conditions).order_by(
        '-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(Decimal('0.00000001'),
                                                                          rounding=ROUND_HALF_UP)  # High precision for inverse

    # Attempt 3: Global direct rate
    rate_obj = ExchangeRate.objects.filter(global_query & direct_rate_conditions).order_by('-date').first()
    if rate_obj: return Decimal(rate_obj.rate)

    # Attempt 4: Global inverse rate
    inverse_rate_obj = ExchangeRate.objects.filter(global_query & inverse_rate_conditions).order_by('-date').first()
    if inverse_rate_obj and inverse_rate_obj.rate != ZERO_DECIMAL:
        return (Decimal('1.0') / Decimal(inverse_rate_obj.rate)).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)

    logger.error(
        f"No exchange rate found for Co {company_id or 'Global'} from {from_currency} to {to_currency} on or before {conversion_date}.")
    raise CurrencyConversionError(
        f"Exchange rate missing: {from_currency} to {to_currency} for {conversion_date} (Company: {company_id or 'Global'})."
    )


def _convert_currency(
        company_id: Optional[PK_TYPE],
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        conversion_date: date,
        precision: int = DEFAULT_AMOUNT_PRECISION
) -> Decimal:
    if from_currency == to_currency:
        return amount.quantize(Decimal('1e-' + str(precision)), rounding=ROUND_HALF_UP)
    if amount == ZERO_DECIMAL:  # Optimization
        return ZERO_DECIMAL
    try:
        rate = _get_exchange_rate(company_id, from_currency, to_currency, conversion_date)
        converted_amount = (amount * rate).quantize(Decimal('1e-' + str(precision)), rounding=ROUND_HALF_UP)
        logger.debug(
            f"Converted {amount} {from_currency} to {converted_amount} {to_currency} via rate {rate:.{DEFAULT_FX_RATE_PRECISION}f} for date {conversion_date} (Co: {company_id or 'Global'})")
        return converted_amount
    except CurrencyConversionError:
        raise  # Re-raise the specific error from _get_exchange_rate
    except Exception as e:
        logger.exception(
            f"Error during currency conversion from {from_currency} to {to_currency} for Co {company_id or 'Global'}")
        raise CurrencyConversionError(f"General conversion failure: {str(e)}") from e


# =============================================================================
# Core Balance Calculation Helper (Tenant Aware, with Currency Conversion)
# =============================================================================
def _calculate_account_balances(company_id: PK_TYPE, as_of_date: date,
                                target_report_currency: str) -> Dict[PK_TYPE, ProcessedAccountBalance]:
    if not Company:
        raise ReportGenerationError("Reports Service cannot function: Company model is not available.")
    if not company_id:
        raise ValueError("company_id must be provided to calculate account balances.")

    logger.debug(
        f"Calculating balances for Co ID {company_id} as of {as_of_date}, target currency {target_report_currency}...")
    account_balances: Dict[PK_TYPE, ProcessedAccountBalance] = {}
    conversion_errors_logged = set()  # To log each missing rate only once per calculation run

    try:
        # This query structure is generally efficient for fetching aggregated data.
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
            pk, acc_currency, nature = item['account__id'], item['account__currency'], item['account__account_nature']
            original_balance_in_acc_currency = (
                        item['total_debit'] - item['total_credit']) if nature == AccountNature.DEBIT.value \
                else (item['total_credit'] - item['total_debit'])

            converted_balance_in_report_currency: Decimal
            try:
                converted_balance_in_report_currency = _convert_currency(
                    company_id, original_balance_in_acc_currency, acc_currency, target_report_currency, as_of_date
                )
            except CurrencyConversionError as cce:
                rate_key = (acc_currency, target_report_currency)  # Log once per currency pair, not per date
                if rate_key not in conversion_errors_logged:
                    logger.error(
                        f"Co {company_id}: {cce} for Account {pk}. Using original balance for this account in report. Report will be mixed-currency if other accounts convert successfully.")
                    conversion_errors_logged.add(rate_key)
                converted_balance_in_report_currency = original_balance_in_acc_currency
                # If a specific conversion fails, the report for that account line effectively remains in original currency.
                # This means totals might be mixed-currency sums if multiple such failures occur.
                # An alternative is to raise ReportGenerationError here to halt the report.

            account_balances[pk] = ProcessedAccountBalance(
                account_pk=pk, account_number=item['account__account_number'],
                account_name=item['account__account_name'],
                account_type=item['account__account_type'], account_nature=nature,
                account_group_pk=item['account__account_group_id'],
                original_currency=acc_currency, original_balance=original_balance_in_acc_currency,
                converted_balance=converted_balance_in_report_currency,
                pl_section=item['account__pl_section']
            )
        # Add active accounts with no movements (zero balance)
        all_company_active_accounts_qs = Account.objects.filter(company_id=company_id, is_active=True).select_related(
            'account_group')
        for acc in all_company_active_accounts_qs:
            if acc.pk not in account_balances:
                account_balances[acc.pk] = ProcessedAccountBalance(
                    account_pk=acc.pk, account_number=acc.account_number, account_name=acc.account_name,
                    account_type=acc.account_type, account_nature=acc.account_nature,
                    account_group_pk=acc.account_group_id, original_currency=acc.currency,
                    original_balance=ZERO_DECIMAL, converted_balance=ZERO_DECIMAL,
                    pl_section=acc.pl_section
                )
        logger.debug(
            f"Calculated and converted balances for {len(account_balances)} active accounts for Co ID {company_id}.")
        return account_balances
    except (ObjectDoesNotExist, MultipleObjectsReturned) as e:
        logger.error(f"ORM error calculating balances for Co ID {company_id}: {e}")
        raise BalanceCalculationError(f"Database error during balance calculation: {str(e)}") from e
    except DecimalInvalidOperation as e:
        logger.error(f"Decimal operation error calculating balances for Co ID {company_id}: {e}")
        raise BalanceCalculationError(f"Numeric error during balance calculation: {str(e)}") from e
    except Exception as e:
        logger.exception(f"Unexpected error calculating account balances for Co ID {company_id} as of {as_of_date}.")
        raise BalanceCalculationError(f"Unexpected error during balance calculation: {str(e)}") from e


# =============================================================================
# Hierarchy Building Helpers (Operate on CONVERTED balances)
# =============================================================================
def _build_group_hierarchy_recursive(
        parent_group_id: Optional[PK_TYPE], all_groups: Dict[PK_TYPE, AccountGroup],
        # Expects dict of ProcessedAccountBalance where 'converted_balance' is in report currency
        account_data_map: Dict[PK_TYPE, ProcessedAccountBalance], level: int
) -> Tuple[
    List[Dict[str, Any]], Decimal, Decimal]:  # Returns (nodes, total_debit_report_curr, total_credit_report_curr)
    current_level_nodes: List[Dict[str, Any]] = []
    current_level_total_debit = ZERO_DECIMAL
    current_level_total_credit = ZERO_DECIMAL

    child_groups = [group for pk, group in all_groups.items() if group.parent_group_id == parent_group_id]
    for group in sorted(child_groups, key=lambda g: g.name):
        child_nodes, child_debit, child_credit = _build_group_hierarchy_recursive(
            group.pk, all_groups, account_data_map, level + 1
        )
        group_node = {
            'id': group.pk, 'name': group.name, 'type': 'group', 'level': level,
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

            if account_debit != ZERO_DECIMAL or account_credit != ZERO_DECIMAL:
                account_node = {
                    'id': acc_pk,
                    'name': f"{acc_data.get('account_number', 'N/A')} - {acc_data.get('account_name', 'N/A')}",
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
        parent_group_id: Optional[PK_TYPE], all_groups: Dict[PK_TYPE, AccountGroup],
        account_balances_bs: Dict[PK_TYPE, ProcessedAccountBalance], level: int
) -> Tuple[List[BalanceSheetNode], Decimal]:  # Returns (nodes, total_balance_report_curr)
    current_level_nodes: List[BalanceSheetNode] = []
    current_level_total_balance = ZERO_DECIMAL

    child_groups = [group for pk, group in all_groups.items() if group.parent_group_id == parent_group_id]
    for group in sorted(child_groups, key=lambda g: g.name):
        child_hierarchy_nodes, child_total_balance = _build_balance_sheet_hierarchy(
            group.pk, all_groups, account_balances_bs, level + 1
        )
        group_node: BalanceSheetNode = {
            'id': group.pk, 'name': group.name, 'type': 'group', 'level': level,
            'balance': child_total_balance,  # Sum of converted balances
            'currency': None,  # Group currency is implicitly the report currency
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
                    'name': f"{acc_data['account_number']} - {acc_data['account_name']}",
                    'type': 'account', 'level': level,
                    'balance': account_balance_in_report_curr,
                    'currency': acc_data['original_currency'],  # Original account currency for reference
                    'children': []
                }
                direct_accounts_nodes.append(account_node)
                current_level_total_balance += account_balance_in_report_curr
    direct_accounts_nodes.sort(key=lambda item: account_balances_bs.get(item['id'], {}).get('account_number', ''))
    current_level_nodes.extend(direct_accounts_nodes)
    return current_level_nodes, current_level_total_balance


# =============================================================================
# Public Report Generation Functions (Utilize converted balances)
# =============================================================================
def generate_trial_balance_structured(company_id: PK_TYPE, as_of_date: date,
                                      report_currency: Optional[str] = None) -> Dict[str, Any]:
    if not Company: raise ReportGenerationError("Reports Service cannot function: Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except ObjectDoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.") from None
    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"Generating TB for Co ID {company_id} (Currency: {effective_report_currency}) as of {as_of_date}...")

    # Balances are calculated and CONVERTED to effective_report_currency
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
                'account_pk': pk, 'account_number': data['account_number'], 'account_name': data['account_name'],
                'debit': debit_amount, 'credit': credit_amount, 'currency': effective_report_currency
            })
        grand_total_debit += debit_amount
        grand_total_credit += credit_amount
    flat_entries_list.sort(key=lambda x: x['account_number'])

    company_groups_qs = AccountGroup.objects.filter(company_id=company_id).order_by('name')
    group_dict = {group.pk: group for group in company_groups_qs}
    # Pass processed_balances_map to hierarchy builder
    hierarchy, _, _ = _build_group_hierarchy_recursive(None, group_dict, processed_balances_map, 0)

    is_balanced = abs(grand_total_debit - grand_total_credit) < Decimal('0.01')  # For potential rounding
    if not is_balanced:
        diff = grand_total_debit - grand_total_credit
        logger.error(
            f"DataIntegrityError: Co ID {company_id} TB OUT OF BALANCE! Currency: {effective_report_currency}, Date: {as_of_date}, D:{grand_total_debit}, C:{grand_total_credit}, Diff:{diff}")
    return {
        'company_id': company_id, 'as_of_date': as_of_date, 'report_currency': effective_report_currency,
        'hierarchy': hierarchy, 'flat_entries': flat_entries_list,
        'total_debit': grand_total_debit, 'total_credit': grand_total_credit, 'is_balanced': is_balanced,
    }


# --- P&L Definition Constants (Consider making configurable) ---
DEFAULT_PL_STRUCTURE_DEFINITION = [
    {'key': PLSection.REVENUE.value, 'title': _('Revenue'), 'is_subtotal': False},
    {'key': PLSection.COGS.value, 'title': _('Cost of Goods Sold'), 'is_subtotal': False},
    {'key': 'GROSS_PROFIT', 'title': _('Gross Profit'), 'is_subtotal': True,
     'calculation_basis': [PLSection.REVENUE.value, PLSection.COGS.value]},
    {'key': PLSection.OPERATING_EXPENSE.value, 'title': _('Operating Expenses'), 'is_subtotal': False},
    {'key': PLSection.DEPRECIATION_AMORTIZATION.value, 'title': _('Depreciation & Amortization'), 'is_subtotal': False},
    {'key': 'OPERATING_PROFIT', 'title': _('Operating Profit / (Loss)'), 'is_subtotal': True,
     'calculation_basis': ['GROSS_PROFIT', PLSection.OPERATING_EXPENSE.value,
                           PLSection.DEPRECIATION_AMORTIZATION.value]},
    {'key': PLSection.OTHER_INCOME.value, 'title': _('Other Income'), 'is_subtotal': False},
    {'key': PLSection.OTHER_EXPENSE.value, 'title': _('Other Expenses'), 'is_subtotal': False},
    {'key': 'PROFIT_BEFORE_TAX', 'title': _('Profit / (Loss) Before Tax'), 'is_subtotal': True,
     'calculation_basis': ['OPERATING_PROFIT', PLSection.OTHER_INCOME.value, PLSection.OTHER_EXPENSE.value]},
    {'key': PLSection.TAX_EXPENSE.value, 'title': _('Tax Expense'), 'is_subtotal': False},
    {'key': 'NET_INCOME', 'title': _('Net Income / (Loss)'), 'is_subtotal': True,
     'calculation_basis': ['PROFIT_BEFORE_TAX', PLSection.TAX_EXPENSE.value]},
]
PL_SECTION_PROFIT_IMPACT = {
    PLSection.REVENUE.value: True, PLSection.COGS.value: False,
    PLSection.OPERATING_EXPENSE.value: False, PLSection.DEPRECIATION_AMORTIZATION.value: False,
    PLSection.OTHER_INCOME.value: True, PLSection.OTHER_EXPENSE.value: False,
    PLSection.TAX_EXPENSE.value: False,
}


def generate_profit_loss(company_id: PK_TYPE, start_date: date, end_date: date,
                         report_currency: Optional[str] = None) -> Dict[str, Any]:
    if not Company: raise ReportGenerationError("Reports Service: Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except ObjectDoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.") from None
    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(
        f"Generating P&L for Co ID {company_id} (Currency: {effective_report_currency}): {start_date} to {end_date}")
    if start_date > end_date: raise ValueError("Start date cannot be after end date for P&L.")

    section_totals: DefaultDict[str, Decimal] = defaultdict(Decimal)
    section_details: DefaultDict[str, List[ProfitLossAccountDetail]] = defaultdict(list)
    conversion_errors_logged_pl = set()

    try:
        pl_account_types = [AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value]
        account_movements_data = VoucherLine.objects.filter(
            voucher__company_id=company_id, voucher__status=TransactionStatus.POSTED.value,
            voucher__date__gte=start_date, voucher__date__lte=end_date,
            account__company_id=company_id, account__account_type__in=pl_account_types, account__is_active=True
        ).values('account').annotate(
            period_debit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.DEBIT.value)), ZERO_DECIMAL,
                                  output_field=models.DecimalField()),
            period_credit=Coalesce(Sum('amount', filter=Q(dr_cr=DrCrType.CREDIT.value)), ZERO_DECIMAL,
                                   output_field=models.DecimalField())
        ).values(
            'account__id', 'account__account_number', 'account__account_name', 'account__account_nature',
            'account__pl_section', 'account__currency', 'period_debit', 'period_credit'
        )

        for item in account_movements_data:
            acc_currency, nature, pl_section = item['account__currency'], item['account__account_nature'], item[
                'account__pl_section']
            movement_in_acc_currency = (
                        item['period_credit'] - item['period_debit']) if nature == AccountNature.CREDIT.value \
                else (item['period_debit'] - item['period_credit'])
            if not pl_section or pl_section == PLSection.NONE.value:
                logger.warning(DataIntegrityWarning(
                    f"Co ID {company_id} P&L: Account '{item['account__account_name']}' (PK: {item['account__id']}) P&L type with missing/NONE PL section. Excluded."))
                continue
            if movement_in_acc_currency == ZERO_DECIMAL: continue

            movement_in_report_currency: Decimal
            try:
                movement_in_report_currency = _convert_currency(
                    company_id, movement_in_acc_currency, acc_currency, effective_report_currency, end_date
                )
            except CurrencyConversionError as cce:
                rate_key = (acc_currency, effective_report_currency)
                if rate_key not in conversion_errors_logged_pl:
                    logger.error(
                        f"Co {company_id} P&L: {cce} for Account {item['account__id']}. Original amount used for this account.")
                    conversion_errors_logged_pl.add(rate_key)
                movement_in_report_currency = movement_in_acc_currency

            section_totals[pl_section] += movement_in_report_currency
            section_details[pl_section].append(ProfitLossAccountDetail(
                account_pk=item['account__id'], account_number=item['account__account_number'],
                account_name=item['account__account_name'], amount=movement_in_report_currency,
                original_amount=movement_in_acc_currency, original_currency=acc_currency
            ))
        # ... (rest of P&L report_lines and calculated_values logic remains same) ...
        report_lines: List[ProfitLossLineItem] = []
        calculated_values: Dict[str, Decimal] = {}
        for section_def in DEFAULT_PL_STRUCTURE_DEFINITION:
            key, title, is_subtotal = section_def['key'], str(section_def['title']), section_def['is_subtotal']
            accounts_detail = None
            line_amount: Decimal
            if not is_subtotal:
                line_amount = section_totals.get(key, ZERO_DECIMAL)
                accounts_detail = sorted(section_details.get(key, []), key=lambda x: x['account_number'])
            else:
                line_amount = ZERO_DECIMAL
                for basis_key in section_def.get('calculation_basis', []):
                    basis_amount = calculated_values.get(basis_key, ZERO_DECIMAL)
                    impact_positive = PL_SECTION_PROFIT_IMPACT.get(basis_key)
                    if impact_positive is True:
                        line_amount += basis_amount
                    elif impact_positive is False:
                        line_amount -= basis_amount
                    else:
                        line_amount += basis_amount
            calculated_values[key] = line_amount
            report_lines.append({'section_key': key, 'title': title, 'amount': line_amount, 'is_subtotal': is_subtotal,
                                 'accounts': accounts_detail})

        net_income = calculated_values.get('NET_INCOME', ZERO_DECIMAL)
        return {
            'company_id': company_id, 'start_date': start_date, 'end_date': end_date,
            'report_currency': effective_report_currency,
            'report_lines': report_lines, 'net_income': net_income,
        }
    except Exception as e:
        logger.exception(f"Error during P&L generation for Co ID {company_id} ({start_date} to {end_date}).")
        raise ReportGenerationError(f"Failed to generate Profit & Loss: {str(e)}") from e


def generate_balance_sheet(company_id: PK_TYPE, as_of_date: date,
                           report_currency: Optional[str] = None) -> Dict[str, Any]:
    # ... (Setup and fetching company_instance and effective_report_currency remains same) ...
    if not Company: raise ReportGenerationError("Reports Service cannot function: Company model not available.")
    try:
        company_instance = Company.objects.get(pk=company_id)
    except ObjectDoesNotExist:
        raise ReportGenerationError(f"Company with ID {company_id} not found.") from None
    effective_report_currency = report_currency or company_instance.default_currency_code or 'USD'
    logger.info(f"Generating BS for Co ID {company_id} (Currency: {effective_report_currency}) as of {as_of_date}")

    all_balances = _calculate_account_balances(company_id, as_of_date, effective_report_currency)
    retained_earnings = ZERO_DECIMAL
    pl_types = {AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value}
    for data in all_balances.values():
        if data['account_type'] in pl_types:
            retained_earnings += data['converted_balance'] if data['account_nature'] == AccountNature.CREDIT.value \
                else -data['converted_balance']
    logger.debug(
        f"Co {company_id}: Calculated Retained Earnings for BS: {retained_earnings} {effective_report_currency}")

    asset_balances, liability_balances, equity_balances = {}, {}, {}
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
    equity_hierarchy, total_equity_accounts = _build_balance_sheet_hierarchy(None, group_dict, equity_balances, 0)

    retained_earnings_node: BalanceSheetNode = {
        'id': RETAINED_EARNINGS_ACCOUNT_ID_PLACEHOLDER,
        'name': str(RETAINED_EARNINGS_ACCOUNT_NAME_DISPLAY),
        'type': 'account', 'level': 0, 'balance': retained_earnings,
        'currency': effective_report_currency, 'children': []
    }
    equity_hierarchy.append(retained_earnings_node)
    total_equity = total_equity_accounts + retained_earnings
    is_balanced = abs(total_assets - (total_liabilities + total_equity)) < Decimal('0.01')
    if not is_balanced:
        diff = total_assets - (total_liabilities + total_equity)
        logger.error(
            f"DataIntegrityError: Co ID {company_id} BS OUT OF BALANCE! Currency: {effective_report_currency}, Date: {as_of_date}, Assets:{total_assets}, Liab+Eq:{total_liabilities + total_equity}, Diff:{diff}")
    return {
        'company_id': company_id, 'as_of_date': as_of_date,
        'report_currency': effective_report_currency, 'is_balanced': is_balanced,
        'assets': {'hierarchy': asset_hierarchy, 'total': total_assets},
        'liabilities': {'hierarchy': liability_hierarchy, 'total': total_liabilities},
        'equity': {'hierarchy': equity_hierarchy, 'total': total_equity}
    }

