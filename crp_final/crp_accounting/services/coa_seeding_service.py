# crp_accounting/services/coa_seeding_service.py
import logging
from typing import Dict, Optional

from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError

# --- Model Imports ---
# Ensure these paths are correct for your project structure
from crp_accounting.models.coa import AccountGroup, Account, PLSection
from company.models import Company  # Your tenant model
logger = logging.getLogger(__name__)
# --- Core/Enum Imports ---
# Ensure these paths are correct for your project structure
try:
    from crp_core.constants import ACCOUNT_ROLE_GROUPS
    from crp_core.enums import AccountType, AccountNature, PartyType, CurrencyType
except ImportError as e:
    # This is a critical failure for the service
    logger.critical(
        f"COA Seeding Service: Could not import constants/enums from crp_core. Service will not function. Error: {e}")
    # Depending on how your app starts, you might want to raise the ImportError
    # to prevent the app from starting if these are missing.
    # For now, we'll let it load but log a critical error.
    ACCOUNT_ROLE_GROUPS = {}  # Define as empty to prevent further errors if called


    # Define dummy enums or raise specific error if an attempt is made to use them
    class DummyEnum:
        value = None; label = None


    AccountType, AccountNature, PartyType, CurrencyType, PLSection = (DummyEnum,) * 5

logger = logging.getLogger(__name__)


# --- Helper Function ---
def get_primary_group_name(constant_key: str) -> str:
    """Extracts the primary concept (Assets, Liabilities, etc.) from the constant key."""
    return constant_key.split(' - ')[0].split(' (')[0].split(' / ')[0].strip()


# --- Derived Mappings (Ensure these align with your Enum definitions and constant keys) ---
GROUP_CONCEPT_TO_ACCOUNT_TYPE_ENUM: Dict[str, AccountType] = {
    'Assets': AccountType.ASSET,
    'Liabilities': AccountType.LIABILITY,
    'Equity': AccountType.EQUITY,
    'Income': AccountType.INCOME,
    'Cost of Goods Sold': AccountType.COST_OF_GOODS_SOLD,
    'Expenses': AccountType.EXPENSE,
}

ACCOUNT_TYPE_TO_DEFAULT_PL_SECTION_ENUM: Dict[AccountType, PLSection] = {
    AccountType.ASSET: PLSection.NONE,
    AccountType.LIABILITY: PLSection.NONE,
    AccountType.EQUITY: PLSection.NONE,
    AccountType.INCOME: PLSection.REVENUE,
    AccountType.COST_OF_GOODS_SOLD: PLSection.COGS,
    AccountType.EXPENSE: PLSection.OPERATING_EXPENSE,
}

CONTROL_ACCOUNTS_MAP: Dict[str, str] = {
    '1030_accounts_receivable_trade': PartyType.CUSTOMER.value,
    '2000_accounts_payable_trade': PartyType.SUPPLIER.value,
}


class COASeedingError(Exception):
    """Custom exception for critical errors during COA seeding for a company."""
    pass


@transaction.atomic  # Ensure all operations for the company succeed or fail together
def seed_coa_for_company(company: Company):
    """
    Seeds the Chart of Accounts for the given Company instance.
    This function is designed to be called when a new company is created (e.g., via a signal).
    It's idempotent using update_or_create.

    Args:
        company: The Company instance to seed the COA for.

    Raises:
        COASeedingError: If a critical error occurs that should prevent company setup.
    """
    if not ACCOUNT_ROLE_GROUPS:  # Check if constants failed to load
        logger.error(
            f"COA Seeding for '{company.name}': ACCOUNT_ROLE_GROUPS is empty. Cannot seed. Constants might not have loaded.")
        raise COASeedingError("ACCOUNT_ROLE_GROUPS constant is missing or empty.")

    logger.info(f"Starting COA Seeding for Company: '{company.name}' (ID: {company.pk})")

    stats = {
        'groups_created': 0, 'groups_found_or_updated': 0,
        'accounts_created': 0, 'accounts_found_or_updated': 0, 'accounts_failed': 0
    }
    group_objects_map: Dict[str, AccountGroup] = {}

    for group_display_name_from_constant, account_tuples in ACCOUNT_ROLE_GROUPS.items():
        primary_concept_name = get_primary_group_name(group_display_name_from_constant)
        parent_group_obj = group_objects_map.get(primary_concept_name)

        # 1. Ensure Primary Group
        if not parent_group_obj:
            group_defaults = {'parent_group': None, 'description': f"Primary group: {primary_concept_name}"}
            try:
                parent_group_obj, created = AccountGroup.objects.update_or_create(
                    company=company, name=primary_concept_name, defaults=group_defaults
                )
                group_objects_map[primary_concept_name] = parent_group_obj
                if created:
                    stats['groups_created'] += 1
                else:
                    stats['groups_found_or_updated'] += 1
            except Exception as e:
                logger.error(
                    f"COA Seeding for '{company.name}': ERROR creating primary group '{primary_concept_name}': {e}. Aborting seed for this company.",
                    exc_info=True)
                raise COASeedingError(f"Failed on primary group '{primary_concept_name}': {e}") from e

        # 2. Determine Target AccountGroup
        target_account_group_obj: AccountGroup
        if group_display_name_from_constant == primary_concept_name:
            target_account_group_obj = parent_group_obj
        else:
            sub_group_defaults = {'parent_group': parent_group_obj,
                                  'description': f"Sub-group: {group_display_name_from_constant}"}
            try:
                target_account_group_obj, created = AccountGroup.objects.update_or_create(
                    company=company, name=group_display_name_from_constant, defaults=sub_group_defaults
                )
                group_objects_map[group_display_name_from_constant] = target_account_group_obj
                if created:
                    stats['groups_created'] += 1
                else:
                    stats['groups_found_or_updated'] += 1
            except Exception as e:
                logger.error(
                    f"COA Seeding for '{company.name}': ERROR creating sub-group '{group_display_name_from_constant}': {e}. Skipping its accounts.",
                    exc_info=True)
                continue  # Skip accounts for this sub-group, but continue with other group definitions

        # 3. Determine AccountType
        account_type_enum_member = GROUP_CONCEPT_TO_ACCOUNT_TYPE_ENUM.get(primary_concept_name)
        if not account_type_enum_member:
            logger.warning(
                f"COA Seeding for '{company.name}': No AccountType mapping for primary concept '{primary_concept_name}'. Skipping accounts in '{group_display_name_from_constant}'.")
            continue

        default_pl_section_enum_member = ACCOUNT_TYPE_TO_DEFAULT_PL_SECTION_ENUM.get(account_type_enum_member,
                                                                                     PLSection.NONE)

        # 4. Create/Update Accounts
        for acc_info_tuple in account_tuples:
            try:
                account_code = acc_info_tuple[0].strip()
                account_name = acc_info_tuple[1].strip()

                explicit_nature_override_str: Optional[str] = None
                if len(acc_info_tuple) == 3:
                    explicit_nature_override_str = acc_info_tuple[2]

                is_control = account_code in CONTROL_ACCOUNTS_MAP
                control_party_type_val = CONTROL_ACCOUNTS_MAP.get(account_code)

                final_pl_section_val = default_pl_section_enum_member.value
                if account_type_enum_member == AccountType.EXPENSE:
                    if 'tax' in account_name.lower():
                        final_pl_section_val = PLSection.TAX_EXPENSE.value
                    elif 'interest expense' in account_name.lower():
                        final_pl_section_val = PLSection.OTHER_EXPENSE.value
                elif account_type_enum_member == AccountType.INCOME:
                    if 'interest income' in account_name.lower(): final_pl_section_val = PLSection.OTHER_INCOME.value

                account_defaults = {
                    'account_name': account_name,
                    'account_group': target_account_group_obj,
                    'account_type': account_type_enum_member.value,
                    'pl_section': final_pl_section_val,
                    'currency': company.default_currency_code or CurrencyType.USD.value,
                    'description': f"Seeded account: {account_name}",
                    'allow_direct_posting': True,
                    'is_active': True,
                    'is_control_account': is_control,
                    'control_account_party_type': control_party_type_val,
                    # account_nature is intentionally omitted here if no override.
                    # The Account model's save() method will derive it.
                }

                if explicit_nature_override_str:
                    try:
                        account_defaults['account_nature'] = AccountNature[explicit_nature_override_str.upper()].value
                    except KeyError:
                        logger.error(
                            f"COA Seeding for '{company.name}': Invalid nature override '{explicit_nature_override_str}' for account '{account_name}'. Model default will apply.")

                _account_obj, created = Account.objects.update_or_create(
                    company=company,
                    account_number=account_code,
                    defaults=account_defaults
                )
                if created:
                    stats['accounts_created'] += 1
                else:
                    stats['accounts_found_or_updated'] += 1

            except (IntegrityError, ValidationError) as e:
                logger.error(
                    f"COA Seeding for '{company.name}': VALIDATION/INTEGRITY ERROR for account {account_code} ('{account_name}'): {e}",
                    exc_info=False)  # exc_info=False for cleaner logs for these
                stats['accounts_failed'] += 1
            except Exception as e:  # Catch any other unexpected error for a specific account
                logger.error(
                    f"COA Seeding for '{company.name}': UNEXPECTED ERROR for account {account_code} ('{account_name}'): {e}",
                    exc_info=True)
                stats['accounts_failed'] += 1

    logger.info(f"Finished COA Seeding for Company: '{company.name}' (ID: {company.pk}). "
                f"Groups Created: {stats['groups_created']}, Groups Found/Updated: {stats['groups_found_or_updated']}. "
                f"Accounts Created: {stats['accounts_created']}, Accounts Found/Updated: {stats['accounts_found_or_updated']}. "
                f"Accounts Failed: {stats['accounts_failed']}.")

    if stats['accounts_failed'] > 0:
        # Depending on policy, you might want to raise an error here to indicate partial failure
        # raise COASeedingError(f"COA seeding for company '{company.name}' completed with {stats['accounts_failed']} account creation/update failures.")
        pass  # For now, just log it.