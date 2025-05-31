# crp_accounting/signals.py

import logging
from decimal import Decimal
from typing import Any, Optional, Dict

from django.db import transaction, OperationalError
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# --- Model Imports ---
try:
    from .models.journal import Voucher, VoucherLine, TransactionStatus, DrCrType
    from .models.coa import Account, AccountType, AccountNature  # Ensure AccountNature is imported
    from .models.period import AccountingPeriod
    from company.models import Company
except ImportError as e:
    # This is critical. If models can't import, the app won't work.
    logging.critical(f"CRP Signals: CRITICAL - Failed to import models: {e}", exc_info=True)
    raise

# Use a distinct logger name for easy filtering of these specific signal logs
logger = logging.getLogger("crp_accounting.signals_DEBUG")
# To see these debug logs, ensure your Django settings configure this logger
# and a handler for it with level DEBUG.

ZERO_DECIMAL = Decimal('0.00')


# --- Synchronous Balance Update Logic ---

def _get_account_for_sync_update(account_pk: Any, company_pk: Any, log_prefix: str) -> Optional[Account]:
    """
    Safely fetches an Account instance for update, scoped by company.
    Returns None if not found, logging an error.
    """
    try:
        # In a high-concurrency synchronous signal, select_for_update() might be considered
        # to prevent race conditions on the same account row if multiple signals acted on it
        # nearly simultaneously. However, Django's transaction handling around signals usually mitigates this.
        # For simplicity and common use cases, direct get is often sufficient.
        account = Account.objects.get(pk=account_pk, company_id=company_pk)
        logger.debug(f"{log_prefix} Fetched Account {account.account_number} (PK: {account_pk}) for update.")
        return account
    except Account.DoesNotExist:
        logger.error(f"{log_prefix} Account PK {account_pk} NOT FOUND in Company PK {company_pk}.")
        return None
    except Exception as e:  # Catch any other unexpected error
        logger.exception(
            f"{log_prefix} Unexpected error fetching Account PK {account_pk} for Company PK {company_pk}: {e}")
        return None


def _apply_sync_balance_adjustment(
        account: Account,
        line_amount: Decimal,
        line_is_debit_entry: bool,  # True if the ORIGINAL voucher line was a DEBIT
        is_reversal_operation: bool,  # True if this entire operation is a REVERSAL of the voucher
        log_prefix: str
):
    """
    Applies an adjustment to the account's current_balance synchronously.
    Handles account nature and reversal logic.
    """
    if account.current_balance is None:
        logger.warning(
            f"{log_prefix} Account {account.pk} ('{account.account_name}') had NULL balance. Initializing to 0.")
        account.current_balance = ZERO_DECIMAL

    original_balance = account.current_balance

    # Step 1: Determine the direct impact of the line as if all accounts were debit-nature
    # A debit line intends to increase a "debit-view" of balance.
    # A credit line intends to decrease a "debit-view" of balance.
    direct_impact_as_debit_effect: Decimal
    if line_is_debit_entry:
        direct_impact_as_debit_effect = line_amount
    else:  # line is a credit entry
        direct_impact_as_debit_effect = -line_amount

    # Step 2: If it's a reversal operation, flip this direct impact
    if is_reversal_operation:
        direct_impact_as_debit_effect = -direct_impact_as_debit_effect
        logger.debug(
            f"{log_prefix} Reversal operation: Flipped impact for Account {account.account_number} to {direct_impact_as_debit_effect}.")

    # Step 3: Apply the calculated "debit-view impact" based on the account's actual nature
    final_change_to_stored_balance: Decimal
    if account.account_nature == AccountNature.DEBIT.value:  # Asset, Expense, COGS
        final_change_to_stored_balance = direct_impact_as_debit_effect
    elif account.account_nature == AccountNature.CREDIT.value:  # Liability, Equity, Income
        # For credit nature accounts, a positive "debit-view impact" DECREASES their balance
        # (as their balance convention is that credits increase it).
        # So, we subtract the debit-view impact.
        final_change_to_stored_balance = -direct_impact_as_debit_effect
    else:
        logger.error(
            f"{log_prefix} Account {account.pk} ('{account.account_name}') has UNKNOWN nature '{account.account_nature}'. Cannot adjust balance.")
        return  # Do not modify if nature is unknown

    account.current_balance += final_change_to_stored_balance
    account.balance_last_updated = timezone.now()

    try:
        account.save(update_fields=['current_balance', 'balance_last_updated'])
        logger.info(
            f"{log_prefix} Synced balance Account {account.account_number} (PK: {account.pk}, Nature: {account.account_nature}): "
            f"Original: {original_balance}, LineAmt: {line_amount}, LineWasDebit: {line_is_debit_entry}, "
            f"ReversalOp: {is_reversal_operation}, DebitViewImpact: {direct_impact_as_debit_effect}, "
            f"FinalChangeToBalance: {final_change_to_stored_balance}, NewBalance: {account.current_balance}"
        )
    except Exception as e:
        logger.exception(
            f"{log_prefix} FAILED to save synced balance for Account {account.pk} ('{account.account_name}'). Original: {original_balance}. Error: {e}")
        # Revert in-memory change if save fails, although the transaction will likely roll back anyway.
        account.current_balance = original_balance
        raise  # Re-raise to ensure the calling transaction (e.g., voucher save/delete) rolls back


def _synchronously_update_balances_for_voucher(voucher_pk: Any, company_pk: Any, is_reversal: bool = False):
    """
    Performs the actual synchronous balance update for a given voucher's lines.
    If is_reversal is True, it applies opposite impacts for each line.
    This function expects to be called within an appropriate transaction context if multiple
    account updates need to be atomic as a group (which it does via `with transaction.atomic()`).
    """
    log_prefix = f"[SYNC_BAL_UPDATE][Co:{company_pk}][Vch:{voucher_pk}]"
    logger.info(f"{log_prefix} --- TASK START --- Reversal Mode: {is_reversal}")

    try:
        # Fetch voucher with lines and related account data for efficiency if possible
        # (though account nature is the main thing needed from account for balance logic)
        voucher = Voucher.objects.prefetch_related(
            'lines__account'  # Ensure account is prefetched for lines
        ).get(pk=voucher_pk, company_id=company_pk)
        logger.debug(
            f"{log_prefix} Fetched Voucher: '{voucher.voucher_number or voucher.pk}', Status: '{voucher.status}'")

        current_time = timezone.now()  # Consistent timestamp for this operation run

        # This atomic block ensures all account updates for THIS voucher are a single unit.
        # The outer signal handler (e.g., pre_delete) is also within a transaction.
        with transaction.atomic():
            lines_to_process = list(voucher.lines.all())  # Get lines once after fetching voucher
            if not lines_to_process:
                logger.warning(f"{log_prefix} Voucher has no lines. No balance changes to apply.")
                # If not a reversal and it's a POSTED voucher, still mark balances_updated
                if hasattr(voucher,
                           'balances_updated') and not is_reversal and voucher.status == TransactionStatus.POSTED.value:
                    Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(balances_updated=True,
                                                                                        updated_at=current_time)
                    logger.info(
                        f"{log_prefix} Marked empty POSTED voucher '{voucher.voucher_number or voucher.pk}' as balances_updated=True.")
                return

            logger.info(f"{log_prefix} Processing {len(lines_to_process)} lines for balance update/reversal.")
            # Cache Account instances fetched within this loop to avoid redundant DB hits for the same account
            processed_accounts_cache: Dict[Any, Account] = {}

            for line_idx, line in enumerate(lines_to_process):
                line_log_prefix = f"{log_prefix}[LinePK:{line.pk},Idx:{line_idx + 1}]"
                logger.debug(
                    f"{line_log_prefix} Processing. AccID:{line.account_id}, Amt:{line.amount}, DrCr:{line.dr_cr}")

                if not line.account_id or line.amount is None or line.amount == ZERO_DECIMAL:
                    logger.warning(f"{line_log_prefix} Skipping invalid line (missing acc_id or zero/null amount).")
                    continue

                account = processed_accounts_cache.get(line.account_id)
                if not account:  # If not in cache, fetch it
                    account = _get_account_for_sync_update(line.account_id, company_pk, line_log_prefix)
                    if not account:  # If still not found after trying to fetch
                        logger.critical(
                            f"{line_log_prefix} CRITICAL: Account with ID {line.account_id} NOT FOUND in Company {company_pk}. Balance impact for this line MISSED!")
                        # Depending on business rules, you might want to raise an exception here
                        # to halt the entire voucher processing if a single account is missing.
                        # For now, it logs and skips this line.
                        continue
                    processed_accounts_cache[line.account_id] = account  # Cache it

                logger.debug(
                    f"{line_log_prefix} Account '{account.account_number}' (Nature: {account.account_nature}, CurrentBal: {account.current_balance}) fetched/retrieved from cache.")

                _apply_sync_balance_adjustment(
                    account=account,
                    line_amount=line.amount,
                    line_is_debit_entry=(line.dr_cr == DrCrType.DEBIT.value),  # Original nature of the line
                    is_reversal_operation=is_reversal,
                    log_prefix=line_log_prefix  # Pass line-specific prefix for detailed logging
                )

            # After all lines are processed successfully within the atomic block:
            # If this is a normal posting (not a reversal), mark the voucher's balances_updated flag.
            if hasattr(voucher,
                       'balances_updated') and not is_reversal and voucher.status == TransactionStatus.POSTED.value:
                Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
                    balances_updated=True,
                    updated_at=current_time  # Also update the voucher's timestamp
                )
                logger.info(
                    f"{log_prefix} Successfully marked Voucher '{voucher.voucher_number or voucher.pk}' balances as updated (for non-reversal POST).")
            elif is_reversal:
                logger.info(
                    f"{log_prefix} All line reversals applied within atomic block for (soon-to-be) deleted voucher '{voucher.voucher_number or voucher.pk}'.")

        logger.info(
            f"{log_prefix} --- TASK FINISHED --- Synchronous balance update/reversal process completed successfully for voucher '{voucher.voucher_number or voucher.pk}'.")

    except Voucher.DoesNotExist:
        logger.error(f"{log_prefix} Voucher not found. Cannot update/reverse balances.")
        # No re-raise here, as the voucher doesn't exist to be processed.
    except OperationalError as oe:  # Database connection issues, deadlocks etc.
        logger.error(
            f"{log_prefix} OperationalError during synchronous balance update: {oe}. This typically requires retry or investigation.",
            exc_info=True)
        raise  # Re-raise to ensure the calling transaction (e.g., voucher save/delete) rolls back
    except Exception as e:  # Catch any other unexpected error
        logger.exception(f"{log_prefix} Unexpected error during synchronous balance update for voucher: {e}")
        raise  # Re-raise to ensure visibility and potential rollback


def _reset_balances_updated_flag_on_commit(voucher_pk: Any, company_pk: Any):
    """Sets the balances_updated flag to False for a given voucher in a specific company.
       Designed to be called via transaction.on_commit for safety."""
    if not voucher_pk or not company_pk:
        logger.warning(f"ResetFlagCommit: Called with missing voucher_pk ({voucher_pk}) or company_pk ({company_pk}).")
        return

    log_prefix = f"[ResetFlagCommit][Co:{company_pk}][Vch:{voucher_pk}]"
    try:
        if not hasattr(Voucher, 'balances_updated'):  # Defensive check
            logger.error(f"{log_prefix} Voucher model is missing 'balances_updated' field. Cannot reset.")
            return

        # Ensure this update targets the specific company's voucher
        rows_affected = Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
            balances_updated=False,
            updated_at=timezone.now()  # Also update the voucher's own timestamp
        )
        if rows_affected > 0:
            logger.debug(f"{log_prefix} Balances_updated flag reset successfully.")
        else:
            logger.warning(
                f"{log_prefix} Could not reset balances_updated flag (voucher PK {voucher_pk} not found for company PK {company_pk}, or flag was already false).")
    except Exception as e:
        logger.error(f"{log_prefix} Error occurred while resetting balances_updated flag: {e}", exc_info=True)


# --- Signal Handlers (Synchronous Version) ---

@receiver(pre_delete, sender=Voucher, dispatch_uid="crp_accounting_voucher_pre_delete_sync_reversal")
def handle_posted_voucher_deletion_sync(sender, instance: Voucher, **kwargs):
    """
    Synchronously reverses account balance impacts when a POSTED voucher is deleted.
    Runs BEFORE the voucher and its lines are actually deleted from the DB.
    """
    voucher_to_delete = instance
    # Use a specific prefix for this signal handler's logs for easier identification
    log_prefix = f"[PRE_DELETE_VCH_SIGNAL][Co:{voucher_to_delete.company_id}][Vch:{voucher_to_delete.pk}]"
    logger.info(
        f"{log_prefix} --- SIGNAL START --- Intercepted pre_delete for Voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'.")

    if not voucher_to_delete.company_id:
        logger.error(
            f"{log_prefix} Voucher is missing company_id! Cannot process balance reversal. ABORTING REVERSAL logic for this signal.")
        # Decide if deletion should proceed. If company_id is fundamental, you might raise error here.
        # For now, allowing deletion to proceed but logging error.
        return

    logger.debug(
        f"{log_prefix} Current Voucher Status: '{voucher_to_delete.status}'. Required for reversal: '{TransactionStatus.POSTED.value}'.")

    if hasattr(voucher_to_delete, 'status') and voucher_to_delete.status == TransactionStatus.POSTED.value:
        logger.info(
            f"{log_prefix} Voucher IS POSTED. Proceeding with synchronous balance reversal for "
            f"'{voucher_to_delete.voucher_number or voucher_to_delete.pk}'."
        )
        try:
            # This call MUST raise an exception if reversal fails, to stop deletion.
            _synchronously_update_balances_for_voucher(
                voucher_pk=voucher_to_delete.pk,
                company_pk=voucher_to_delete.company_id,
                is_reversal=True  # CRITICAL: This tells the function to reverse impacts
            )
            logger.info(
                f"{log_prefix} Synchronous balance reversal call completed successfully for voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'.")
        except Exception as e:
            # Log the CRITICAL failure to reverse balances.
            logger.critical(
                f"{log_prefix} --- CRITICAL FAILURE --- during synchronous balance reversal for Voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'. "
                f"Error: {e}. Voucher deletion will be ABORTED to maintain data integrity.", exc_info=True
            )
            # Re-raise as DjangoValidationError to try and stop the deletion via admin message.
            # The actual rollback of deletion depends on how Django handles exceptions from pre_delete.
            raise DjangoValidationError(  # This should prevent the deletion if properly handled by Django admin
                _("CRITICAL ERROR: Failed to reverse account balances for the voucher being deleted. "
                  "The deletion has been aborted to maintain data integrity. "
                  "Please check server logs for details (Voucher PK: %(voucher_pk)s). Error: %(error_detail)s") %
                {'voucher_pk': voucher_to_delete.pk, 'error_detail': str(e)}
            ) from e  # Chain the original exception for full traceback
    else:
        status_val = getattr(voucher_to_delete, 'status', 'AttributeMissing')
        logger.info(
            f"{log_prefix} Voucher status is '{status_val}' (not POSTED). No synchronous balance reversal needed.")
    logger.info(f"{log_prefix} --- SIGNAL END ---")


@receiver(post_save, sender=Voucher, dispatch_uid="crp_accounting_voucher_post_save_sync_update")
def handle_voucher_post_save_sync_balances(sender, instance: Voucher, created, update_fields=None, **kwargs):
    """
    Handles Voucher post_save:
    - If status becomes POSTED and balances_updated is False, perform synchronous balance update.
    - If status changes FROM POSTED to something else and balances_updated was True, reset the flag.
    """
    log_prefix = f"[POST_SAVE_VCH_SIGNAL][Co:{instance.company_id}][Vch:{instance.pk}]"
    logger.debug(f"{log_prefix} --- SIGNAL START --- Created: {created}, Update_fields: {update_fields}")

    if not instance.company_id:
        logger.error(f"{log_prefix} Voucher saved without company_id! Cannot process sync balance update.")
        return
    if not hasattr(instance, 'status') or not hasattr(instance, 'balances_updated'):
        logger.error(
            f"{log_prefix} Voucher instance missing 'status' or 'balances_updated' attributes. Signal aborted.")
        return

    # Check if 'status' was actually part of the fields being updated, or if it's a new instance.
    # This helps avoid unnecessary processing if only other fields were saved.
    status_field_was_effectively_updated = created or update_fields is None or 'status' in update_fields

    if instance.status == TransactionStatus.POSTED.value:
        # Only proceed if status is POSTED AND (it's a new POSTED voucher OR status was just changed to POSTED)
        # AND balances are not already marked as updated.
        if not instance.balances_updated:
            if status_field_was_effectively_updated or created:  # Ensure this is a fresh post or status change to post
                logger.info(
                    f"{log_prefix} Voucher status is POSTED & balances_updated=False. "
                    f"Performing synchronous balance update for '{instance.voucher_number or instance.pk}'."
                )
                try:
                    # Perform update after the main transaction commits to ensure voucher and lines are fully saved.
                    transaction.on_commit(
                        lambda: _synchronously_update_balances_for_voucher(
                            voucher_pk=instance.pk,
                            company_pk=instance.company_id,
                            is_reversal=False  # This is a normal posting
                        )
                    )
                except Exception as e:  # Catch errors from trying to schedule on_commit (should be rare)
                    logger.critical(
                        f"{log_prefix} CRITICAL: Error scheduling on_commit balance update for POSTED voucher: {e}",
                        exc_info=True)
            else:
                logger.debug(
                    f"{log_prefix} Voucher is POSTED & balances_updated=False, but status field was not in update_fields. Assuming no change to posting status that requires immediate balance update.")
        else:  # Is POSTED and balances_updated is True
            logger.debug(
                f"{log_prefix} Voucher is POSTED & balances_updated=True. Assuming balances are current. No sync update performed.")

    # If status changed *away from* POSTED (or was never POSTED but flag is somehow true)
    elif instance.balances_updated:  # This means (status != POSTED) AND (balances_updated == True)
        if status_field_was_effectively_updated or created:  # Ensure status change is relevant
            logger.warning(
                f"{log_prefix} Voucher status is '{instance.get_status_display()}' (not POSTED) "
                f"but balances_updated=True. Resetting flag for '{instance.voucher_number or instance.pk}'."
            )
            transaction.on_commit(
                lambda: _reset_balances_updated_flag_on_commit(
                    voucher_pk=instance.pk,
                    company_pk=instance.company_id
                )
            )
    logger.debug(f"{log_prefix} --- SIGNAL END ---")


@receiver(post_save, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_post_save_invalidate")
@receiver(pre_delete, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_pre_delete_invalidate")
def handle_voucher_line_change_for_posted_voucher_flag_reset(sender, instance: VoucherLine, **kwargs):
    """
    If a VoucherLine is saved or deleted for an already POSTED Voucher,
    this invalidates the parent voucher's calculated balances.
    This signal will reset the parent voucher's `balances_updated` flag.
    Actual recalculation should be triggered by re-posting the voucher or a manual process.
    """
    try:
        voucher = instance.voucher  # Access parent voucher
        if not voucher or not voucher.pk:  # Ensure voucher and its PK are valid
            logger.debug(f"VoucherLine signal: Line {instance.pk} has no valid parent voucher. Skipping.")
            return
    except ObjectDoesNotExist:  # If voucher was already deleted or relation is broken
        logger.warning(f"VoucherLine signal: Parent voucher for Line {instance.pk} does not exist. Skipping.")
        return

    log_prefix = f"[VCHLINE_CHANGE_SIGNAL][Co:{voucher.company_id}][Vch:{voucher.pk}][Line:{instance.pk}]"

    if not voucher.company_id:
        logger.error(f"{log_prefix} Parent voucher is missing company_id. Cannot process flag reset.")
        return

    # Only act if the parent voucher is currently POSTED
    if hasattr(voucher, 'status') and voucher.status == TransactionStatus.POSTED.value:
        # And only if its balances were previously considered updated
        if hasattr(Voucher, 'balances_updated') and voucher.balances_updated:
            logger.info(
                f"{log_prefix} Line changed/deleted for POSTED voucher '{voucher.voucher_number or voucher.pk}'. "
                f"Resetting parent voucher's balances_updated flag."
            )
            transaction.on_commit(
                lambda: _reset_balances_updated_flag_on_commit(
                    voucher_pk=voucher.pk,
                    company_pk=voucher.company_id
                )
            )
            # DECISION POINT: Do you want to *immediately* try to recalculate balances here?
            # Doing so can be resource-intensive if many lines are changed rapidly or via bulk operations.
            # Option: Uncomment to trigger immediate recalculation (synchronous).
            # logger.info(f"{log_prefix} Additionally triggering immediate synchronous balance re-calculation for parent voucher.")
            # transaction.on_commit(
            #    lambda: _synchronously_update_balances_for_voucher(
            #        voucher_pk=voucher.pk,
            #        company_pk=voucher.company_id,
            #        is_reversal=False # False means re-calculate based on current lines
            #    )
            # )
        else:
            logger.debug(
                f"{log_prefix} Line changed for POSTED voucher, but balances_updated was already False or not applicable. No flag reset needed.")
    else:
        status_display = voucher.get_status_display() if hasattr(voucher, 'get_status_display') else voucher.status
        logger.debug(
            f"{log_prefix} Line changed, but parent voucher status is '{status_display}' (not POSTED). No flag reset or recalculation triggered by this line change.")
    logger.debug(f"{log_prefix} --- SIGNAL END ---")