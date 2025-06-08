# Filename: crp_accounting/management/commands/recalculate_account_balances.py
# ****** WARNING: DESPITE THE FILENAME, THIS COMMAND ZEROS OUT BALANCES ******
# ******          IT INCLUDES A DANGEROUS --all FLAG                   ******

import logging
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# --- Adjust these imports based on your actual project structure ---
from crp_accounting.models import Account # Only need Account now
try:
    # ***** REPLACE THIS WITH YOUR ACTUAL COMPANY MODEL IMPORT *****
    from company.models import Company # e.g., from crp_core.models import Company
except ImportError:
    raise ImportError("Please adjust the import path for your Company model in recalculate_account_balances.py")
# --- End Adjustments ---

logger = logging.getLogger(__name__)
ZERO_DECIMAL = Decimal('0.00')

class Command(BaseCommand):
    # !!! HELP TEXT REFLECTS ACTUAL ACTION, NOT FILENAME !!!
    help = '!!! DANGEROUS !!! Forces the current_balance of active accounts to ZERO. Use --companies IDs or --all. Ignores history.'

    def add_arguments(self, parser):
        # Group for mutual exclusion (either companies or all)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--companies',
            nargs='+', # One or more
            type=str,
            help='List of specific Company IDs to process.',
        )
        group.add_argument(
            '--all',
            action='store_true',
            help='Process ALL ACTIVE companies (USE WITH EXTREME CAUTION!).',
        )
        # Confirmation flag is separate and always needed
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm that you understand this command OVERWRITES balances with ZERO.',
        )
        # Optional flag to include inactive companies when using --all
        parser.add_argument(
            '--include-inactive',
            action='store_true',
            help='Include inactive companies when using --all flag.'
        )


    def handle(self, *args, **options):
        self.stdout.write(self.style.ERROR("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"))
        self.stdout.write(self.style.ERROR("!!! WARNING: THIS COMMAND FORCES ACCOUNT BALANCES TO ZERO    !!!"))
        self.stdout.write(self.style.ERROR("!!!          (Filename 'recalculate...' is MISLEADING)         !!!"))
        self.stdout.write(self.style.ERROR("!!!          THIS IS DESTRUCTIVE AND IRREVERSIBLE            !!!"))
        self.stdout.write(self.style.ERROR("!!!          IT IGNORES ALL TRANSACTION HISTORY FOR BALANCE  !!!"))
        self.stdout.write(self.style.ERROR("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"))

        use_all = options['all']
        company_ids_str = options['companies'] # Will be None if --all is used
        confirmed = options['confirm']
        include_inactive = options['include_inactive']

        if not confirmed:
            self.stdout.write(self.style.ERROR("Operation aborted. You must add the --confirm flag to proceed."))
            self.stdout.write(self.style.WARNING("Example: python manage.py recalculate_account_balances --companies 1 2 --confirm"))
            self.stdout.write(self.style.WARNING("   OR: python manage.py recalculate_account_balances --all --confirm"))
            return

        # Determine companies to process
        company_queryset = None
        if use_all:
            self.stdout.write(self.style.WARNING("Processing companies based on --all flag..."))
            company_queryset = Company.objects.all()
            if not include_inactive:
                if hasattr(Company, 'is_active'):
                    company_queryset = company_queryset.filter(is_active=True)
                    self.stdout.write(self.style.NOTICE("(--all specified: Targeting only ACTIVE companies. Use --include-inactive to override.)"))
                else:
                    self.stdout.write(self.style.WARNING("(--all specified: Company model has no 'is_active' field, processing all found.)"))
            else:
                 self.stdout.write(self.style.NOTICE("(--all --include-inactive specified: Targeting ALL companies, active or not.)"))


        elif company_ids_str:
             # Validate company IDs
            valid_company_ids = []
            invalid_entries = []
            for cid in company_ids_str:
                try:
                    valid_company_ids.append(int(cid))
                except ValueError:
                    invalid_entries.append(cid)
            if invalid_entries:
                 raise CommandError(f"Invalid non-numeric company IDs provided: {', '.join(invalid_entries)}")

            # Fetch specified companies
            company_queryset = Company.objects.filter(pk__in=valid_company_ids)
            found_ids = list(company_queryset.values_list('pk', flat=True))
            missing_ids = list(set(valid_company_ids) - set(found_ids))

            if missing_ids:
                 self.stdout.write(self.style.WARNING(f"Could not find companies with IDs: {', '.join(map(str, missing_ids))}"))
        else:
             # Should not happen due to mutually exclusive group, but good practice
             raise CommandError("You must specify either --companies or --all.")


        if not company_queryset or not company_queryset.exists():
             self.stdout.write(self.style.WARNING("No companies found matching the criteria. Exiting."))
             return

        # Confirmation prompt - Make it stronger if --all is used
        self.stdout.write(self.style.WARNING("="*60))
        num_targeted = company_queryset.count()
        if use_all:
             self.stdout.write(self.style.ERROR(f"You are about to force balances to ZERO for ALL {num_targeted} targeted companies!"))
        else:
            self.stdout.write(f"You are about to force balances to ZERO for {num_targeted} specified companies:")

        # List companies for clarity before confirmation
        for company in company_queryset.order_by('name'): # Order for clarity
             self.stdout.write(f"  - {company.name} (ID: {company.pk})")
        self.stdout.write(self.style.WARNING("="*60))

        prompt_text = "Type 'ZERO BALANCES' to confirm this irreversible action: "
        required_confirmation = "ZERO BALANCES"
        if use_all:
             prompt_text = self.style.ERROR("Type 'ZERO ALL BALANCES' to confirm affecting ALL targeted companies: ")
             required_confirmation = "ZERO ALL BALANCES"

        confirmation_input = input(prompt_text)

        if confirmation_input != required_confirmation:
            self.stdout.write(self.style.ERROR("Confirmation failed. Aborting operation."))
            return

        self.stdout.write("Confirmation received. Proceeding with zeroing out balances...")

        total_zeroed_count = 0
        processed_companies = 0
        failed_companies = []

        # --- Loop through each targeted company ---
        for company in company_queryset:
            self.stdout.write(f"\nProcessing Company: {company.name} (ID: {company.pk}) - FORCING BALANCES TO ZERO")
            company_zeroed_count = 0
            company_error = False
            try:
                with transaction.atomic():
                    # Find active accounts for the company that don't already have a zero balance
                    accounts_to_update = Account.objects.filter(
                        company=company,
                        is_active=True
                    ).exclude(current_balance=ZERO_DECIMAL)

                    # Find accounts that are already zero (for reporting)
                    accounts_already_zero = Account.objects.filter(
                        company=company,
                        is_active=True,
                        current_balance=ZERO_DECIMAL
                    )
                    num_already_zero = accounts_already_zero.count()

                    if not accounts_to_update.exists():
                        self.stdout.write(f"  No active accounts with non-zero balances found for this company.")
                        if num_already_zero > 0:
                             self.stdout.write(f"  ({num_already_zero} active accounts were already zero.)")
                        continue # Skip to next company

                    # Perform the bulk update to zero
                    num_updated = accounts_to_update.update(
                         current_balance=ZERO_DECIMAL,
                         # Optionally update timestamp if it exists and makes sense for zeroing
                         # balance_last_updated=timezone.now()
                     )
                    company_zeroed_count = num_updated

                    if num_already_zero > 0:
                         self.stdout.write(f"  ({num_already_zero} active accounts were already zero.)")


            except Exception as e:
                # Log the error and mark the company as failed
                logger.error(f"Failed to zero balances for Company {company.name} (ID: {company.pk}) due to error: {e}", exc_info=True) # Add traceback
                self.stdout.write(self.style.ERROR(f"  Failed zeroing balances for Company: {company.name}. Rolled back changes for this company. See logs for details."))
                company_error = True
                failed_companies.append(f"{company.name} (ID: {company.pk})")

            # --- Update totals and report company status ---
            if not company_error:
                self.stdout.write(self.style.SUCCESS(f"  Finished Company {company.name}. Balances forced to ZERO: {company_zeroed_count}"))
                total_zeroed_count += company_zeroed_count
                processed_companies += 1

        # --- Final Summary ---
        self.stdout.write("\n" + "="*60)
        self.stdout.write(self.style.WARNING("ZERO Balance Operation Summary (Command: recalculate_account_balances)"))
        self.stdout.write(f"Successfully processed companies: {processed_companies}")
        self.stdout.write(f"Total accounts forced to zero: {total_zeroed_count}")
        if failed_companies:
            self.stdout.write(self.style.ERROR(f"Failed companies (rolled back): {len(failed_companies)}"))
            for fc in failed_companies:
                 self.stdout.write(self.style.ERROR(f"  - {fc}"))
        self.stdout.write(self.style.SUCCESS("Account balance zeroing finished."))
        self.stdout.write(self.style.ERROR("Reminder: This command ZEROED balances, it did not recalculate them based on history."))
        self.stdout.write("="*60)