# crp_accounting/management/commands/seed_coa.py
import logging
from typing import Dict, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError

# --- Model Imports ---
try:
    from crp_accounting.models.coa import AccountGroup, Account, PLSection
    from company.models import Company  # Your tenant model
except ImportError as e:
    raise CommandError(
        f"Could not import necessary models (Account, AccountGroup, Company). Check paths and app setup: {e}")

# --- Core/Enum Imports ---
try:
    # ACCOUNT_ROLE_GROUPS structure:
    # { 'Group Display Name': [ ('code', 'name'), ('code', 'name', 'NATURE_OVERRIDE_STR'), ... ] }
    from crp_core.constants import ACCOUNT_ROLE_GROUPS
    from crp_core.enums import AccountType, AccountNature, PartyType, CurrencyType
except ImportError as e:
    raise CommandError(f"Could not import constants/enums from crp_core. Check paths and dependencies: {e}")
except AttributeError as e:  # Catch if enums or constants are not structured as expected
    raise CommandError(
        f"AttributeError during import/setup from crp_core. Ensure Enums and ACCOUNT_ROLE_GROUPS are correctly defined. Error: {e}")

logger = logging.getLogger(__name__)


# --- Helper Function ---
def get_primary_group_name(constant_key: str) -> str:
    """Extracts the primary concept (Assets, Liabilities, etc.) from the constant key."""
    return constant_key.split(' - ')[0].split(' (')[0].split(' / ')[0].strip()


# --- Derived Mappings (Ensure these align with your Enum definitions and constant keys) ---

# Maps the *primary concept name* (derived from ACCOUNT_ROLE_GROUPS keys) to AccountType Enum member
GROUP_CONCEPT_TO_ACCOUNT_TYPE_ENUM: Dict[str, AccountType] = {
    'Assets': AccountType.ASSET,
    'Liabilities': AccountType.LIABILITY,
    'Equity': AccountType.EQUITY,
    'Income': AccountType.INCOME,
    'Cost of Goods Sold': AccountType.COST_OF_GOODS_SOLD,
    'Expenses': AccountType.EXPENSE,
    # Add other top-level concepts if they exist in ACCOUNT_ROLE_GROUPS keys and map to a type
    # e.g., 'Non-Operational / Adjustments': AccountType.EXPENSE,
}

# Default PL Section based on AccountType Enum member
ACCOUNT_TYPE_TO_DEFAULT_PL_SECTION_ENUM: Dict[AccountType, PLSection] = {
    AccountType.ASSET: PLSection.NONE,
    AccountType.LIABILITY: PLSection.NONE,
    AccountType.EQUITY: PLSection.NONE,
    AccountType.INCOME: PLSection.REVENUE,
    AccountType.COST_OF_GOODS_SOLD: PLSection.COGS,
    AccountType.EXPENSE: PLSection.OPERATING_EXPENSE,
}

# Control accounts - map account *code* (from constants.py) to PartyType Enum VALUE
CONTROL_ACCOUNTS_MAP: Dict[str, str] = {
    '1030_accounts_receivable_trade': PartyType.CUSTOMER.value,
    '2000_accounts_payable_trade': PartyType.SUPPLIER.value,
    # Add other specific control account codes from your constants.py if needed
}


# =============================================================================
# Command Class
# =============================================================================
class Command(BaseCommand):
    help = 'Seeds or updates the Chart of Accounts for a specific company, correctly applying nature overrides.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--company-subdomain', type=str, required=True,
            help='The unique subdomain prefix of the Company to seed the COA for.',
        )
        parser.add_argument(
            '--force-update-nature', action='store_true',
            help='Force update the nature of existing accounts based on constants, even if model usually sets it on save.'
        )

    def handle(self, *args, **options):
        company_subdomain = options['company_subdomain']
        force_update_nature = options['force_update_nature']
        try:
            target_company = Company.objects.get(subdomain_prefix=company_subdomain)
        except Company.DoesNotExist:
            raise CommandError(f"Company with subdomain prefix '{company_subdomain}' not found.")
        except Exception as e:  # Catch other potential errors during Company fetch
            raise CommandError(f"Error fetching company '{company_subdomain}': {e}")

        self._seed_company_coa(target_company, force_update_nature)

    @transaction.atomic
    def _seed_company_coa(self, company: Company, force_update_nature: bool):
        self.stdout.write(
            self.style.SUCCESS(f"\n--- Processing COA for Company: '{company.name}' ({company.subdomain_prefix}) ---"))

        # Counters for this run
        stats = {
            'groups_created': 0, 'groups_skipped_or_updated': 0,
            'accounts_created': 0, 'accounts_updated': 0, 'accounts_failed': 0
        }
        group_objects_map: Dict[str, AccountGroup] = {}  # Cache for group objects

        for group_display_name_from_constant, account_tuples in ACCOUNT_ROLE_GROUPS.items():
            # self.stdout.write(f"\nProcessing Group Definition: '{group_display_name_from_constant}'")

            primary_concept_name = get_primary_group_name(group_display_name_from_constant)
            parent_group_obj = group_objects_map.get(primary_concept_name)

            # 1. Ensure Primary Group (e.g., "Assets") for the company
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
                        stats['groups_skipped_or_updated'] += 1
                except Exception as e:
                    self.stderr.write(self.style.ERROR(
                        f"  [ERROR] Creating/updating primary group '{primary_concept_name}': {e}. Skipping this group definition."))
                    continue  # Skip this entire group definition block from constants.py

            # 2. Determine Target AccountGroup for accounts (either the primary group or a sub-group)
            target_account_group_obj: AccountGroup
            if group_display_name_from_constant == primary_concept_name:
                target_account_group_obj = parent_group_obj  # Accounts go directly under "Assets", "Equity", etc.
            else:  # It's a sub-group like "Assets - Current Assets"
                sub_group_defaults = {
                    'parent_group': parent_group_obj,  # Link to the primary group object
                    'description': f"Sub-group: {group_display_name_from_constant}"
                }
                try:
                    target_account_group_obj, created = AccountGroup.objects.update_or_create(
                        company=company, name=group_display_name_from_constant, defaults=sub_group_defaults
                    )
                    group_objects_map[group_display_name_from_constant] = target_account_group_obj
                    if created:
                        stats['groups_created'] += 1
                    else:
                        stats['groups_skipped_or_updated'] += 1
                except Exception as e:
                    self.stderr.write(self.style.ERROR(
                        f"  [ERROR] Creating/updating sub-group '{group_display_name_from_constant}': {e}. Skipping its accounts."))
                    continue  # Skip accounts for this sub-group

            # 3. Determine AccountType for all accounts in this definition block
            account_type_enum_member = GROUP_CONCEPT_TO_ACCOUNT_TYPE_ENUM.get(primary_concept_name)
            if not account_type_enum_member:
                self.stderr.write(self.style.WARNING(
                    f"  [WARN] No AccountType mapping for primary concept '{primary_concept_name}'. Skipping accounts in '{group_display_name_from_constant}'."))
                continue  # Skip accounts if type cannot be determined

            default_pl_section_enum_member = ACCOUNT_TYPE_TO_DEFAULT_PL_SECTION_ENUM.get(account_type_enum_member,
                                                                                         PLSection.NONE)

            # 4. Create/Update Accounts
            for acc_info_tuple in account_tuples:
                account_code = acc_info_tuple[0].strip()
                account_name = acc_info_tuple[1].strip()

                explicit_nature_override_str: Optional[str] = None
                if len(acc_info_tuple) == 3:
                    explicit_nature_override_str = acc_info_tuple[2]  # e.g., 'CREDIT'

                is_control = account_code in CONTROL_ACCOUNTS_MAP
                control_party_type_val = CONTROL_ACCOUNTS_MAP.get(account_code)

                final_pl_section_val = default_pl_section_enum_member.value
                # Specific PL Section Overrides can be added here if needed based on account_name/code
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
                    # account_nature will be set below if overridden, otherwise model's save() handles default
                    'pl_section': final_pl_section_val,
                    'currency': company.default_currency_code or CurrencyType.USD.value,  # Use company default
                    'description': f"Seeded account: {account_name}",
                    'allow_direct_posting': True,  # Can be refined if 'Summary?' comments used
                    'is_active': True,
                    'is_control_account': is_control,
                    'control_account_party_type': control_party_type_val,
                }

                # Determine final nature: Use override if present, otherwise it will be None
                # and the model's save() method will derive it from account_type.
                final_nature_val: Optional[str] = None
                if explicit_nature_override_str:
                    try:
                        final_nature_val = AccountNature[explicit_nature_override_str.upper()].value
                        account_defaults['account_nature'] = final_nature_val  # Add to defaults if overridden
                    except KeyError:
                        self.stderr.write(self.style.ERROR(
                            f"  [WARN] Invalid nature override '{explicit_nature_override_str}' for account '{account_name}'. Model default will apply."))

                try:
                    account_obj, created = Account.objects.update_or_create(
                        company=company,
                        account_number=account_code,
                        defaults=account_defaults
                    )

                    # If '--force-update-nature' and an override exists, ensure it's set even if account wasn't "created"
                    if not created and force_update_nature and final_nature_val and account_obj.account_nature != final_nature_val:
                        account_obj.account_nature = final_nature_val
                        account_obj.save(update_fields=['account_nature'])  # Save only this field
                        self.stdout.write(
                            f"    [ACC] Force updated nature for: {account_name} ({account_code}) to {account_obj.get_account_nature_display()}")

                    if created:
                        stats['accounts_created'] += 1
                        # Log the nature that was actually saved (either override or model default)
                        log_nature = account_obj.get_account_nature_display() if hasattr(account_obj,
                                                                                         'get_account_nature_display') else account_obj.account_nature
                        self.stdout.write(
                            f"    [ACC] Created: {account_name} ({account_code}) - Type: {account_type_enum_member.label}, Nature: {log_nature}")
                    else:
                        stats['accounts_updated'] += 1

                except (IntegrityError, ValidationError) as e:
                    self.stderr.write(self.style.ERROR(f"  [ERROR] Account {account_code} ('{account_name}'): {e}"))
                    stats['accounts_failed'] += 1
                except Exception as e:
                    self.stderr.write(
                        self.style.ERROR(f"  [ERROR][General] Account {account_code} ('{account_name}'): {e}"))
                    stats['accounts_failed'] += 1

        self.stdout.write(self.style.SUCCESS(f"--- Finished COA for Company: '{company.name}' ---"))
        self.stdout.write(
            f"  Groups Created: {stats['groups_created']}, Groups Found/Updated: {stats['groups_skipped_or_updated']}")
        self.stdout.write(
            f"  Accounts Created: {stats['accounts_created']}, Accounts Found/Updated: {stats['accounts_updated']}")
        if stats['accounts_failed'] > 0:
            self.stderr.write(self.style.ERROR(f"  Accounts Failed: {stats['accounts_failed']}"))
        self.stdout.write(self.style.SUCCESS('---------------------------------------------------'))