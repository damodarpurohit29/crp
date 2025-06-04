# crp_accounting/signals.py

import logging
from decimal import Decimal
from typing import Any, Optional, Dict, Set

from django.db import transaction, OperationalError
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
# from django.conf import settings # Not used directly in this snippet
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# --- Model Imports ---
try:
    from .models.journal import Voucher, VoucherLine, TransactionStatus, DrCrType
    from .models.coa import Account, AccountType, AccountNature
    # from .models.period import AccountingPeriod # Not used directly in this signals file
    # from company.models import Company # Not used directly in this signals file
except ImportError as e:
    logging.critical(f"CRP Signals: CRITICAL - Failed to import models: {e}", exc_info=True)
    raise

logger = logging.getLogger("crp_accounting.signals_DEBUG")  # Your chosen logger name

ZERO_DECIMAL = Decimal('0.00')


# --- Synchronous Balance Update Logic ---

def _get_account_for_update_with_lock(account_pk: Any, company_pk: Any, log_prefix: str) -> Optional[Account]:
    """
    Safely fetches an Account instance for update, scoped by company, and locks the row.
    Returns None if not found, logging an error.
    This function MUST be called within an atomic transaction block.
    """
    try:
        # Lock the account row to prevent race conditions during balance updates.
        account = Account.objects.select_for_update().get(pk=account_pk, company_id=company_pk)
        logger.debug(f"{log_prefix} Fetched & LOCKED Account {account.account_number} (PK: {account_pk}) for update.")
        return account
    except Account.DoesNotExist:
        logger.error(
            f"{log_prefix} Account PK {account_pk} NOT FOUND in Company PK {company_pk} while attempting to lock.")
        return None
    except OperationalError as oe:  # Could be due to lock contention or DB issues
        logger.error(f"{log_prefix} OperationalError fetching/locking Account PK {account_pk}: {oe}", exc_info=True)
        raise  # Re-raise to ensure transaction rollback
    except Exception as e:
        logger.exception(
            f"{log_prefix} Unexpected error fetching/locking Account PK {account_pk} for Company PK {company_pk}: {e}")
        raise  # Re-raise to ensure transaction rollback


def _apply_balance_adjustment_to_account(
        account: Account,
        line_amount: Decimal,
        line_was_debit_entry: bool,
        is_reversal_of_original_entry: bool,  # Renamed for clarity
        log_prefix: str
):
    """
    Applies an adjustment to the account's current_balance synchronously.
    Handles account nature and reversal logic.
    Assumes 'account' was fetched with select_for_update() if called within a transaction.
    """
    if account.current_balance is None:
        logger.warning(
            f"{log_prefix} Account {account.pk} ('{account.account_name}') had NULL balance. Initializing to 0 before adjustment.")
        account.current_balance = ZERO_DECIMAL

    original_balance = account.current_balance

    # Determine the effect of the line amount based on its Dr/Cr type.
    # A debit entry aims to increase a debit-natured balance or decrease a credit-natured balance.
    # A credit entry aims to decrease a debit-natured balance or increase a credit-natured balance.
    change_amount = line_amount

    # If the operation is a reversal of the original line entry, flip the sign of the change.
    if is_reversal_of_original_entry:
        change_amount = -change_amount
        logger.debug(f"{log_prefix} Reversal operation: Effective change amount for line is {change_amount}.")

    # Apply the change based on the account's nature.
    if account.account_nature == AccountNature.DEBIT.value:  # Asset, Expense, COGS
        account.current_balance += change_amount if line_was_debit_entry else -change_amount
    elif account.account_nature == AccountNature.CREDIT.value:  # Liability, Equity, Income
        account.current_balance += change_amount if not line_was_debit_entry else -change_amount
    else:
        logger.error(
            f"{log_prefix} Account {account.pk} ('{account.account_name}') has UNKNOWN nature '{account.account_nature}'. Cannot adjust balance.")
        return  # Do not modify if nature is unknown

    account.balance_last_updated = timezone.now()

    try:
        # We only update these two fields. The Account instance might have other changes
        # from elsewhere if not careful, but select_for_update helps.
        account.save(update_fields=['current_balance', 'balance_last_updated'])
        logger.info(
            f"{log_prefix} Synced balance Account {account.account_number} (PK: {account.pk}, Nature: {account.account_nature}): "
            f"OriginalBal: {original_balance}, LineAmt: {line_amount}, LineWasDr: {line_was_debit_entry}, "
            f"IsReversal: {is_reversal_of_original_entry}, EffectiveChange: {change_amount}, NewBal: {account.current_balance}"
        )
    except Exception as e:
        logger.exception(
            f"{log_prefix} FAILED to save synced balance for Account {account.pk} ('{account.account_name}'). OriginalBal: {original_balance}. Error: {e}")
        # Important: If save fails, the in-memory 'account.current_balance' is changed,
        # but DB is not. The transaction should roll back.
        raise  # Re-raise to ensure the calling transaction rolls back.


def _synchronously_update_balances_for_voucher_transaction(voucher_pk: Any, company_pk: Any, is_reversal: bool):
    """
    Core logic for updating/reversing balances for all lines of a voucher.
    This function is wrapped by `_handle_voucher_balance_update_on_commit` which runs it in `on_commit`.
    It itself runs within its own atomic block to ensure all account updates for the voucher are one unit.
    """
    log_prefix = f"[SYNC_BAL_CORE][Co:{company_pk}][Vch:{voucher_pk}]"
    logger.info(f"{log_prefix} --- CORE LOGIC START --- Reversal Mode: {is_reversal}")

    try:
        voucher = Voucher.objects.prefetch_related(
            'lines__account'  # Account object needed for its nature
        ).get(pk=voucher_pk, company_id=company_pk)
        logger.debug(
            f"{log_prefix} Fetched Voucher: '{voucher.voucher_number or voucher.pk}', Status: '{voucher.status}'")

        # This atomic block ensures all account updates for THIS voucher are a single unit.
        with transaction.atomic():
            lines_to_process = list(voucher.lines.all())
            if not lines_to_process:
                logger.warning(f"{log_prefix} Voucher has no lines. No balance changes to apply.")
                # If not a reversal and it's a POSTED voucher, still mark balances_updated
                # This part is tricky if balances_updated is managed here vs. in the signal handler.
                # Let's assume the flag update happens outside this core logic, closer to the signal.
                return

            logger.info(
                f"{log_prefix} Processing {len(lines_to_process)} lines. Mode: {'Reversal' if is_reversal else 'Update'}.")

            # Keep track of accounts already fetched and locked in this operation to avoid re-locking
            locked_accounts_in_operation: Dict[Any, Account] = {}

            for line_idx, line in enumerate(lines_to_process):
                line_log_prefix = f"{log_prefix}[LinePK:{line.pk},Idx:{line_idx + 1}]"
                logger.debug(f"{line_log_prefix} AccID:{line.account_id}, Amt:{line.amount}, DrCr:{line.dr_cr}")

                if not line.account_id or line.amount is None or line.amount == ZERO_DECIMAL:
                    logger.warning(f"{line_log_prefix} Skipping line (missing account_id or zero/null amount).")
                    continue

                account = locked_accounts_in_operation.get(line.account_id)
                if not account:
                    account = _get_account_for_update_with_lock(line.account_id, company_pk, line_log_prefix)
                    if not account:
                        logger.critical(
                            f"{line_log_prefix} CRITICAL: Account ID {line.account_id} NOT FOUND in Co {company_pk}. Balance impact MISSED for this line!")
                        # FAIL LOUDLY: If an account for a line is missing, it's a severe data integrity issue.
                        raise ObjectDoesNotExist(
                            f"Account ID {line.account_id} referenced by VoucherLine PK {line.pk} not found in Company PK {company_pk}.")
                    locked_accounts_in_operation[line.account_id] = account

                if account.account_nature not in [AccountNature.DEBIT.value, AccountNature.CREDIT.value]:
                    logger.error(
                        f"{line_log_prefix} Account {account.pk} ('{account.account_name}') has invalid nature '{account.account_nature}'. Skipping balance adjustment.")
                    continue

                _apply_balance_adjustment_to_account(
                    account=account,
                    line_amount=line.amount,
                    line_was_debit_entry=(line.dr_cr == DrCrType.DEBIT.value),
                    is_reversal_of_original_entry=is_reversal,
                    log_prefix=line_log_prefix
                )
            # End of for loop (all lines processed for this voucher)
        # End of with transaction.atomic() for this voucher's lines
        logger.info(f"{log_prefix} --- CORE LOGIC FINISHED SUCCESSFULLY ---")

    except Voucher.DoesNotExist:
        logger.error(f"{log_prefix} Voucher PK {voucher_pk} not found for Co {company_pk}. Cannot update balances.")
    except ObjectDoesNotExist as odne:  # Specifically from _get_account_for_update_with_lock or line check
        logger.critical(f"{log_prefix} Data integrity error: {odne}", exc_info=True)
        raise  # Re-raise to ensure outer transaction (if any) rolls back
    except OperationalError as oe:
        logger.error(f"{log_prefix} OperationalError during balance update: {oe}. This often requires retry.",
                     exc_info=True)
        raise
    except Exception as e:
        logger.exception(f"{log_prefix} Unexpected error during balance update for voucher: {e}")
        raise


def _handle_voucher_balance_update_on_commit(voucher_pk: Any, company_pk: Any, is_reversal: bool):
    """
    Wrapper to be called via transaction.on_commit.
    This ensures the main DB operation (voucher save/delete) is complete before trying to update balances.
    """
    log_prefix = f"[ON_COMMIT_BAL_HANDLER][Co:{company_pk}][Vch:{voucher_pk}]"
    logger.info(f"{log_prefix} Triggered. Reversal Mode: {is_reversal}")
    try:
        _synchronously_update_balances_for_voucher_transaction(voucher_pk, company_pk, is_reversal)

        # If successful and NOT a reversal (i.e., a posting), mark balances_updated=True
        if not is_reversal:
            if hasattr(Voucher, 'balances_updated'):
                rows = Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
                    balances_updated=True,
                    updated_at=timezone.now()
                )
                if rows > 0:
                    logger.info(f"{log_prefix} Marked Voucher balances_updated=True.")
                else:
                    logger.warning(
                        f"{log_prefix} Failed to mark Voucher balances_updated=True (voucher not found or already True).")
    except Exception as e:
        # CRITICAL: Balance update failed AFTER parent transaction committed.
        # Data might be inconsistent. This requires manual intervention or a reconciliation process.
        logger.critical(
            f"{log_prefix} --- CRITICAL FAILURE IN ON_COMMIT --- Balance update/reversal for Voucher PK {voucher_pk} FAILED. "
            f"Main transaction was already committed. Error: {e}", exc_info=True
        )
        # What to do here? Re-raising won't roll back the outer commit.
        # Options: Log to Sentry, send admin email, flag voucher for review.


def _reset_balances_updated_flag_on_commit_transaction(voucher_pk: Any, company_pk: Any):
    """Sets balances_updated=False. Called via on_commit."""
    if not voucher_pk or not company_pk:
        logger.warning(f"[RESET_FLAG_ON_COMMIT] Missing voucher_pk ({voucher_pk}) or company_pk ({company_pk}).")
        return
    log_prefix = f"[RESET_FLAG_ON_COMMIT][Co:{company_pk}][Vch:{voucher_pk}]"
    try:
        if not hasattr(Voucher, 'balances_updated'):
            logger.error(f"{log_prefix} Voucher model missing 'balances_updated' field.")
            return
        rows = Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
            balances_updated=False, updated_at=timezone.now()
        )
        if rows > 0:
            logger.info(f"{log_prefix} Balances_updated flag reset to False.")
        else:
            logger.warning(f"{log_prefix} Could not reset flag (voucher not found or flag already False).")
    except Exception as e:
        logger.error(f"{log_prefix} Error resetting balances_updated flag: {e}", exc_info=True)


# --- Signal Handlers ---

@receiver(pre_delete, sender=Voucher, dispatch_uid="crp_accounting_voucher_pre_delete_sync_reversal_v2")
def handle_posted_voucher_deletion_sync_reversal(sender, instance: Voucher, **kwargs):
    """
    Synchronously reverses account balance impacts when a POSTED voucher is about to be deleted.
    This runs *within the same transaction* as the delete operation. If this signal handler
    raises an unhandled exception, the entire transaction (including the delete) should roll back.
    """
    voucher_to_delete = instance
    log_prefix = f"[PRE_DELETE_VCH_SIGNAL][Co:{voucher_to_delete.company_id}][Vch:{voucher_to_delete.pk}]"
    logger.info(
        f"{log_prefix} --- PRE_DELETE SIGNAL START --- Voucher: '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'")

    if not voucher_to_delete.company_id:
        logger.error(
            f"{log_prefix} Voucher missing company_id! Balance reversal ABORTED. Deletion may proceed without balance adjustment if not halted.")
        # Consider raising an error if company_id is critical for all operations
        # raise DjangoValidationError("Cannot process voucher deletion: Company ID is missing.")
        return

    if hasattr(voucher_to_delete, 'status') and voucher_to_delete.status == TransactionStatus.POSTED.value:
        logger.info(f"{log_prefix} Voucher IS POSTED. Attempting synchronous balance reversal.")
        try:
            # This is called directly (not on_commit) because pre_delete runs before commit.
            # If this fails, the exception will propagate and should roll back the delete.
            _synchronously_update_balances_for_voucher_transaction(
                voucher_pk=voucher_to_delete.pk,
                company_pk=voucher_to_delete.company_id,
                is_reversal=True  # CRITICAL: Reverse the impacts
            )
            logger.info(f"{log_prefix} Synchronous balance reversal call completed successfully.")
        except Exception as e:
            logger.critical(
                f"{log_prefix} --- CRITICAL FAILURE --- during synchronous balance reversal. "
                f"Error: {e}. Deletion transaction SHOULD ROLL BACK.", exc_info=True
            )
            # Re-raise to ensure the delete transaction is rolled back.
            # DjangoValidationError might be caught by admin and displayed.
            # A more generic Exception will also halt and roll back.
            raise DjangoValidationError(
                _("CRITICAL: Failed to reverse account balances for Voucher PK %(voucher_pk)s. Deletion aborted. Error: %(error_detail)s") %
                {'voucher_pk': voucher_to_delete.pk, 'error_detail': str(e)}
            ) from e
    else:
        status_val = getattr(voucher_to_delete, 'status', 'AttributeMissing')
        logger.info(f"{log_prefix} Voucher status is '{status_val}' (not POSTED). No balance reversal needed.")
    logger.info(f"{log_prefix} --- PRE_DELETE SIGNAL END ---")


@receiver(post_save, sender=Voucher, dispatch_uid="crp_accounting_voucher_post_save_sync_update_v2")
def handle_voucher_post_save_sync_balances_update(sender, instance: Voucher, created: bool,
                                                  update_fields: Optional[Set[str]] = None, **kwargs):
    """
    Handles Voucher post_save:
    - If status becomes POSTED and balances_updated is False: schedule balance update on_commit.
    - If status changes FROM POSTED and balances_updated was True: schedule flag reset on_commit.
    """
    log_prefix = f"[POST_SAVE_VCH_SIGNAL][Co:{instance.company_id}][Vch:{instance.pk}]"
    logger.info(f"{log_prefix} --- POST_SAVE SIGNAL START --- Created: {created}, UpdateFields: {update_fields}")

    if not instance.company_id:
        logger.error(f"{log_prefix} Voucher saved without company_id! Cannot process balance updates.")
        return
    if not hasattr(instance, 'status') or not hasattr(Voucher, 'balances_updated'):  # Check model attribute presence
        logger.error(
            f"{log_prefix} Voucher instance/model missing 'status' or 'balances_updated' attributes. Signal aborted.")
        return

    # Determine if the 'status' field was part of this save operation.
    # 'update_fields' is None if model.save() was called without it (e.g. on creation, or full save)
    # 'update_fields' is a frozenset of field names if model.save(update_fields=[...]) was used.
    status_was_potentially_changed = created or (update_fields is None) or ('status' in update_fields)

    if instance.status == TransactionStatus.POSTED.value:
        if not instance.balances_updated:  # Balances need update
            if status_was_potentially_changed:  # And status is newly POSTED or save implies it could be
                logger.info(
                    f"{log_prefix} Voucher '{instance.voucher_number or instance.pk}' is POSTED & balances_updated=False. Scheduling balance update on_commit.")
                try:
                    transaction.on_commit(
                        lambda: _handle_voucher_balance_update_on_commit(
                            voucher_pk=instance.pk,
                            company_pk=instance.company_id,
                            is_reversal=False
                        )
                    )
                except Exception as e:
                    logger.critical(f"{log_prefix} CRITICAL: Error scheduling on_commit balance update: {e}",
                                    exc_info=True)
            else:  # Status is POSTED, balances_updated=False, but 'status' field was not in update_fields.
                # This case implies an update to other fields of an already POSTED (but not balance-updated) voucher.
                # We might still want to trigger balance update if something critical changed.
                # For now, assuming only explicit status changes to POSTED trigger the update.
                logger.debug(
                    f"{log_prefix} Voucher POSTED, balances_updated=False, but 'status' not in update_fields. No immediate update scheduled by this save.")
        else:  # Voucher is POSTED and balances_updated is True.
            logger.debug(f"{log_prefix} Voucher POSTED & balances_updated=True. Balances assumed current.")

    # Case: Status changed AWAY FROM POSTED or was never POSTED, but balances_updated is True.
    # This indicates balances might be stale or flag needs reset.
    elif instance.balances_updated:  # (implies instance.status != TransactionStatus.POSTED.value here)
        if status_was_potentially_changed:  # Only if status field was part of the save.
            logger.warning(
                f"{log_prefix} Voucher status '{instance.get_status_display()}' (not POSTED) but balances_updated=True. "
                f"Scheduling flag reset for '{instance.voucher_number or instance.pk}' on_commit."
            )
            try:
                transaction.on_commit(
                    lambda: _reset_balances_updated_flag_on_commit_transaction(
                        voucher_pk=instance.pk,
                        company_pk=instance.company_id
                    )
                )
            except Exception as e:
                logger.critical(f"{log_prefix} CRITICAL: Error scheduling on_commit flag reset: {e}", exc_info=True)
        else:
            logger.debug(
                f"{log_prefix} Voucher status not POSTED, balances_updated=True, but 'status' not in update_fields. No flag reset by this save.")
    logger.info(f"{log_prefix} --- POST_SAVE SIGNAL END ---")


@receiver(post_save, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_post_save_invalidate_v2")
@receiver(pre_delete, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_pre_delete_invalidate_v2")
def handle_voucher_line_change_for_posted_voucher(sender, instance: VoucherLine, **kwargs):
    """
    If a VoucherLine is saved (created/updated) or deleted for an already POSTED Voucher,
    reset the parent voucher's `balances_updated` flag to False.
    This indicates that the stored balances might be stale.
    The actual recalculation/update will happen if the voucher's status changes again or via other means.
    """
    try:
        # Need to fetch the voucher with select_for_update if we intend to modify it within this transaction.
        # However, here we are scheduling an on_commit task to modify it, so direct access is okay for reading.
        voucher = instance.voucher
        if not voucher or not voucher.pk:
            logger.debug(f"[VCHLINE_CHANGE_SIGNAL][Line:{instance.pk}] Line has no valid parent voucher. Skipping.")
            return
    except ObjectDoesNotExist:
        logger.warning(f"[VCHLINE_CHANGE_SIGNAL][Line:{instance.pk}] Parent voucher for Line does not exist. Skipping.")
        return

    log_prefix = f"[VCHLINE_CHANGE_SIGNAL][Co:{voucher.company_id}][Vch:{voucher.pk}][Line:{instance.pk}]"
    logger.info(
        f"{log_prefix} --- VOUCHER_LINE CHANGE SIGNAL START --- Action: {'pre_delete' if kwargs.get('signal') == pre_delete else 'post_save'}")

    if not voucher.company_id:
        logger.error(f"{log_prefix} Parent voucher missing company_id. Cannot process.")
        return
    if not hasattr(Voucher, 'balances_updated'):  # Check on the model class
        logger.error(f"{log_prefix} Voucher model missing 'balances_updated' attribute. Signal aborted.")
        return

    if hasattr(voucher, 'status') and voucher.status == TransactionStatus.POSTED.value:
        # Fetch the voucher again to check its current balances_updated status from DB,
        # as 'voucher' instance might be stale if multiple signals/operations are chained.
        try:
            live_voucher_data = Voucher.objects.values('balances_updated').get(pk=voucher.pk,
                                                                               company_id=voucher.company_id)
            if live_voucher_data['balances_updated']:  # Only reset if it was True
                logger.info(
                    f"{log_prefix} Line changed/deleted for POSTED voucher '{voucher.voucher_number or voucher.pk}' whose balances_updated=True. "
                    f"Scheduling reset of parent voucher's balances_updated flag on_commit."
                )
                try:
                    transaction.on_commit(
                        lambda: _reset_balances_updated_flag_on_commit_transaction(
                            voucher_pk=voucher.pk,
                            company_pk=voucher.company_id
                        )
                    )
                except Exception as e:
                    logger.critical(
                        f"{log_prefix} CRITICAL: Error scheduling on_commit flag reset due to line change: {e}",
                        exc_info=True)
            else:
                logger.debug(
                    f"{log_prefix} Line changed for POSTED voucher, but its balances_updated was already False. No flag reset needed.")
        except Voucher.DoesNotExist:
            logger.error(f"{log_prefix} Parent voucher PK {voucher.pk} not found when re-checking for flag reset. Odd.")
    else:
        status_display = voucher.get_status_display() if hasattr(voucher, 'get_status_display') else getattr(voucher,
                                                                                                             'status',
                                                                                                             'Unknown')
        logger.debug(
            f"{log_prefix} Line changed, but parent voucher status is '{status_display}' (not POSTED). No action by this signal.")
    logger.info(f"{log_prefix} --- VOUCHER_LINE CHANGE SIGNAL END ---")
# # crp_accounting/signals.py
#
# import logging
# from decimal import Decimal
# from typing import Any, Optional, Dict
#
# from django.db import transaction, OperationalError
# from django.db.models.signals import post_save, pre_delete
# from django.dispatch import receiver
# from django.conf import settings
# from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
# from django.utils import timezone
# from django.utils.translation import gettext_lazy as _
#
# # --- Model Imports ---
# try:
#     from .models.journal import Voucher, VoucherLine, TransactionStatus, DrCrType
#     from .models.coa import Account, AccountType, AccountNature  # Ensure AccountNature is imported
#     from .models.period import AccountingPeriod
#     from company.models import Company
# except ImportError as e:
#     # This is critical. If models can't import, the app won't work.
#     logging.critical(f"CRP Signals: CRITICAL - Failed to import models: {e}", exc_info=True)
#     raise
#
# # Use a distinct logger name for easy filtering of these specific signal logs
# logger = logging.getLogger("crp_accounting.signals_DEBUG")
# # To see these debug logs, ensure your Django settings configure this logger
# # and a handler for it with level DEBUG.
#
# ZERO_DECIMAL = Decimal('0.00')
#
#
# # --- Synchronous Balance Update Logic ---
#
# def _get_account_for_sync_update(account_pk: Any, company_pk: Any, log_prefix: str) -> Optional[Account]:
#     """
#     Safely fetches an Account instance for update, scoped by company.
#     Returns None if not found, logging an error.
#     """
#     try:
#         # In a high-concurrency synchronous signal, select_for_update() might be considered
#         # to prevent race conditions on the same account row if multiple signals acted on it
#         # nearly simultaneously. However, Django's transaction handling around signals usually mitigates this.
#         # For simplicity and common use cases, direct get is often sufficient.
#         account = Account.objects.get(pk=account_pk, company_id=company_pk)
#         logger.debug(f"{log_prefix} Fetched Account {account.account_number} (PK: {account_pk}) for update.")
#         return account
#     except Account.DoesNotExist:
#         logger.error(f"{log_prefix} Account PK {account_pk} NOT FOUND in Company PK {company_pk}.")
#         return None
#     except Exception as e:  # Catch any other unexpected error
#         logger.exception(
#             f"{log_prefix} Unexpected error fetching Account PK {account_pk} for Company PK {company_pk}: {e}")
#         return None
#
#
# def _apply_sync_balance_adjustment(
#         account: Account,
#         line_amount: Decimal,
#         line_is_debit_entry: bool,  # True if the ORIGINAL voucher line was a DEBIT
#         is_reversal_operation: bool,  # True if this entire operation is a REVERSAL of the voucher
#         log_prefix: str
# ):
#     """
#     Applies an adjustment to the account's current_balance synchronously.
#     Handles account nature and reversal logic.
#     """
#     if account.current_balance is None:
#         logger.warning(
#             f"{log_prefix} Account {account.pk} ('{account.account_name}') had NULL balance. Initializing to 0.")
#         account.current_balance = ZERO_DECIMAL
#
#     original_balance = account.current_balance
#
#     # Step 1: Determine the direct impact of the line as if all accounts were debit-nature
#     # A debit line intends to increase a "debit-view" of balance.
#     # A credit line intends to decrease a "debit-view" of balance.
#     direct_impact_as_debit_effect: Decimal
#     if line_is_debit_entry:
#         direct_impact_as_debit_effect = line_amount
#     else:  # line is a credit entry
#         direct_impact_as_debit_effect = -line_amount
#
#     # Step 2: If it's a reversal operation, flip this direct impact
#     if is_reversal_operation:
#         direct_impact_as_debit_effect = -direct_impact_as_debit_effect
#         logger.debug(
#             f"{log_prefix} Reversal operation: Flipped impact for Account {account.account_number} to {direct_impact_as_debit_effect}.")
#
#     # Step 3: Apply the calculated "debit-view impact" based on the account's actual nature
#     final_change_to_stored_balance: Decimal
#     if account.account_nature == AccountNature.DEBIT.value:  # Asset, Expense, COGS
#         final_change_to_stored_balance = direct_impact_as_debit_effect
#     elif account.account_nature == AccountNature.CREDIT.value:  # Liability, Equity, Income
#         # For credit nature accounts, a positive "debit-view impact" DECREASES their balance
#         # (as their balance convention is that credits increase it).
#         # So, we subtract the debit-view impact.
#         final_change_to_stored_balance = -direct_impact_as_debit_effect
#     else:
#         logger.error(
#             f"{log_prefix} Account {account.pk} ('{account.account_name}') has UNKNOWN nature '{account.account_nature}'. Cannot adjust balance.")
#         return  # Do not modify if nature is unknown
#
#     account.current_balance += final_change_to_stored_balance
#     account.balance_last_updated = timezone.now()
#
#     try:
#         account.save(update_fields=['current_balance', 'balance_last_updated'])
#         logger.info(
#             f"{log_prefix} Synced balance Account {account.account_number} (PK: {account.pk}, Nature: {account.account_nature}): "
#             f"Original: {original_balance}, LineAmt: {line_amount}, LineWasDebit: {line_is_debit_entry}, "
#             f"ReversalOp: {is_reversal_operation}, DebitViewImpact: {direct_impact_as_debit_effect}, "
#             f"FinalChangeToBalance: {final_change_to_stored_balance}, NewBalance: {account.current_balance}"
#         )
#     except Exception as e:
#         logger.exception(
#             f"{log_prefix} FAILED to save synced balance for Account {account.pk} ('{account.account_name}'). Original: {original_balance}. Error: {e}")
#         # Revert in-memory change if save fails, although the transaction will likely roll back anyway.
#         account.current_balance = original_balance
#         raise  # Re-raise to ensure the calling transaction (e.g., voucher save/delete) rolls back
#
#
# def _synchronously_update_balances_for_voucher(voucher_pk: Any, company_pk: Any, is_reversal: bool = False):
#     """
#     Performs the actual synchronous balance update for a given voucher's lines.
#     If is_reversal is True, it applies opposite impacts for each line.
#     This function expects to be called within an appropriate transaction context if multiple
#     account updates need to be atomic as a group (which it does via `with transaction.atomic()`).
#     """
#     log_prefix = f"[SYNC_BAL_UPDATE][Co:{company_pk}][Vch:{voucher_pk}]"
#     logger.info(f"{log_prefix} --- TASK START --- Reversal Mode: {is_reversal}")
#
#     try:
#         # Fetch voucher with lines and related account data for efficiency if possible
#         # (though account nature is the main thing needed from account for balance logic)
#         voucher = Voucher.objects.prefetch_related(
#             'lines__account'  # Ensure account is prefetched for lines
#         ).get(pk=voucher_pk, company_id=company_pk)
#         logger.debug(
#             f"{log_prefix} Fetched Voucher: '{voucher.voucher_number or voucher.pk}', Status: '{voucher.status}'")
#
#         current_time = timezone.now()  # Consistent timestamp for this operation run
#
#         # This atomic block ensures all account updates for THIS voucher are a single unit.
#         # The outer signal handler (e.g., pre_delete) is also within a transaction.
#         with transaction.atomic():
#             lines_to_process = list(voucher.lines.all())  # Get lines once after fetching voucher
#             if not lines_to_process:
#                 logger.warning(f"{log_prefix} Voucher has no lines. No balance changes to apply.")
#                 # If not a reversal and it's a POSTED voucher, still mark balances_updated
#                 if hasattr(voucher,
#                            'balances_updated') and not is_reversal and voucher.status == TransactionStatus.POSTED.value:
#                     Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(balances_updated=True,
#                                                                                         updated_at=current_time)
#                     logger.info(
#                         f"{log_prefix} Marked empty POSTED voucher '{voucher.voucher_number or voucher.pk}' as balances_updated=True.")
#                 return
#
#             logger.info(f"{log_prefix} Processing {len(lines_to_process)} lines for balance update/reversal.")
#             # Cache Account instances fetched within this loop to avoid redundant DB hits for the same account
#             processed_accounts_cache: Dict[Any, Account] = {}
#
#             for line_idx, line in enumerate(lines_to_process):
#                 line_log_prefix = f"{log_prefix}[LinePK:{line.pk},Idx:{line_idx + 1}]"
#                 logger.debug(
#                     f"{line_log_prefix} Processing. AccID:{line.account_id}, Amt:{line.amount}, DrCr:{line.dr_cr}")
#
#                 if not line.account_id or line.amount is None or line.amount == ZERO_DECIMAL:
#                     logger.warning(f"{line_log_prefix} Skipping invalid line (missing acc_id or zero/null amount).")
#                     continue
#
#                 account = processed_accounts_cache.get(line.account_id)
#                 if not account:  # If not in cache, fetch it
#                     account = _get_account_for_sync_update(line.account_id, company_pk, line_log_prefix)
#                     if not account:  # If still not found after trying to fetch
#                         logger.critical(
#                             f"{line_log_prefix} CRITICAL: Account with ID {line.account_id} NOT FOUND in Company {company_pk}. Balance impact for this line MISSED!")
#                         # Depending on business rules, you might want to raise an exception here
#                         # to halt the entire voucher processing if a single account is missing.
#                         # For now, it logs and skips this line.
#                         continue
#                     processed_accounts_cache[line.account_id] = account  # Cache it
#
#                 logger.debug(
#                     f"{line_log_prefix} Account '{account.account_number}' (Nature: {account.account_nature}, CurrentBal: {account.current_balance}) fetched/retrieved from cache.")
#
#                 _apply_sync_balance_adjustment(
#                     account=account,
#                     line_amount=line.amount,
#                     line_is_debit_entry=(line.dr_cr == DrCrType.DEBIT.value),  # Original nature of the line
#                     is_reversal_operation=is_reversal,
#                     log_prefix=line_log_prefix  # Pass line-specific prefix for detailed logging
#                 )
#
#             # After all lines are processed successfully within the atomic block:
#             # If this is a normal posting (not a reversal), mark the voucher's balances_updated flag.
#             if hasattr(voucher,
#                        'balances_updated') and not is_reversal and voucher.status == TransactionStatus.POSTED.value:
#                 Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
#                     balances_updated=True,
#                     updated_at=current_time  # Also update the voucher's timestamp
#                 )
#                 logger.info(
#                     f"{log_prefix} Successfully marked Voucher '{voucher.voucher_number or voucher.pk}' balances as updated (for non-reversal POST).")
#             elif is_reversal:
#                 logger.info(
#                     f"{log_prefix} All line reversals applied within atomic block for (soon-to-be) deleted voucher '{voucher.voucher_number or voucher.pk}'.")
#
#         logger.info(
#             f"{log_prefix} --- TASK FINISHED --- Synchronous balance update/reversal process completed successfully for voucher '{voucher.voucher_number or voucher.pk}'.")
#
#     except Voucher.DoesNotExist:
#         logger.error(f"{log_prefix} Voucher not found. Cannot update/reverse balances.")
#         # No re-raise here, as the voucher doesn't exist to be processed.
#     except OperationalError as oe:  # Database connection issues, deadlocks etc.
#         logger.error(
#             f"{log_prefix} OperationalError during synchronous balance update: {oe}. This typically requires retry or investigation.",
#             exc_info=True)
#         raise  # Re-raise to ensure the calling transaction (e.g., voucher save/delete) rolls back
#     except Exception as e:  # Catch any other unexpected error
#         logger.exception(f"{log_prefix} Unexpected error during synchronous balance update for voucher: {e}")
#         raise  # Re-raise to ensure visibility and potential rollback
#
#
# def _reset_balances_updated_flag_on_commit(voucher_pk: Any, company_pk: Any):
#     """Sets the balances_updated flag to False for a given voucher in a specific company.
#        Designed to be called via transaction.on_commit for safety."""
#     if not voucher_pk or not company_pk:
#         logger.warning(f"ResetFlagCommit: Called with missing voucher_pk ({voucher_pk}) or company_pk ({company_pk}).")
#         return
#
#     log_prefix = f"[ResetFlagCommit][Co:{company_pk}][Vch:{voucher_pk}]"
#     try:
#         if not hasattr(Voucher, 'balances_updated'):  # Defensive check
#             logger.error(f"{log_prefix} Voucher model is missing 'balances_updated' field. Cannot reset.")
#             return
#
#         # Ensure this update targets the specific company's voucher
#         rows_affected = Voucher.objects.filter(pk=voucher_pk, company_id=company_pk).update(
#             balances_updated=False,
#             updated_at=timezone.now()  # Also update the voucher's own timestamp
#         )
#         if rows_affected > 0:
#             logger.debug(f"{log_prefix} Balances_updated flag reset successfully.")
#         else:
#             logger.warning(
#                 f"{log_prefix} Could not reset balances_updated flag (voucher PK {voucher_pk} not found for company PK {company_pk}, or flag was already false).")
#     except Exception as e:
#         logger.error(f"{log_prefix} Error occurred while resetting balances_updated flag: {e}", exc_info=True)
#
#
# # --- Signal Handlers (Synchronous Version) ---
#
# @receiver(pre_delete, sender=Voucher, dispatch_uid="crp_accounting_voucher_pre_delete_sync_reversal")
# def handle_posted_voucher_deletion_sync(sender, instance: Voucher, **kwargs):
#     """
#     Synchronously reverses account balance impacts when a POSTED voucher is deleted.
#     Runs BEFORE the voucher and its lines are actually deleted from the DB.
#     """
#     voucher_to_delete = instance
#     # Use a specific prefix for this signal handler's logs for easier identification
#     log_prefix = f"[PRE_DELETE_VCH_SIGNAL][Co:{voucher_to_delete.company_id}][Vch:{voucher_to_delete.pk}]"
#     logger.info(
#         f"{log_prefix} --- SIGNAL START --- Intercepted pre_delete for Voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'.")
#
#     if not voucher_to_delete.company_id:
#         logger.error(
#             f"{log_prefix} Voucher is missing company_id! Cannot process balance reversal. ABORTING REVERSAL logic for this signal.")
#         # Decide if deletion should proceed. If company_id is fundamental, you might raise error here.
#         # For now, allowing deletion to proceed but logging error.
#         return
#
#     logger.debug(
#         f"{log_prefix} Current Voucher Status: '{voucher_to_delete.status}'. Required for reversal: '{TransactionStatus.POSTED.value}'.")
#
#     if hasattr(voucher_to_delete, 'status') and voucher_to_delete.status == TransactionStatus.POSTED.value:
#         logger.info(
#             f"{log_prefix} Voucher IS POSTED. Proceeding with synchronous balance reversal for "
#             f"'{voucher_to_delete.voucher_number or voucher_to_delete.pk}'."
#         )
#         try:
#             # This call MUST raise an exception if reversal fails, to stop deletion.
#             _synchronously_update_balances_for_voucher(
#                 voucher_pk=voucher_to_delete.pk,
#                 company_pk=voucher_to_delete.company_id,
#                 is_reversal=True  # CRITICAL: This tells the function to reverse impacts
#             )
#             logger.info(
#                 f"{log_prefix} Synchronous balance reversal call completed successfully for voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'.")
#         except Exception as e:
#             # Log the CRITICAL failure to reverse balances.
#             logger.critical(
#                 f"{log_prefix} --- CRITICAL FAILURE --- during synchronous balance reversal for Voucher '{voucher_to_delete.voucher_number or voucher_to_delete.pk}'. "
#                 f"Error: {e}. Voucher deletion will be ABORTED to maintain data integrity.", exc_info=True
#             )
#             # Re-raise as DjangoValidationError to try and stop the deletion via admin message.
#             # The actual rollback of deletion depends on how Django handles exceptions from pre_delete.
#             raise DjangoValidationError(  # This should prevent the deletion if properly handled by Django admin
#                 _("CRITICAL ERROR: Failed to reverse account balances for the voucher being deleted. "
#                   "The deletion has been aborted to maintain data integrity. "
#                   "Please check server logs for details (Voucher PK: %(voucher_pk)s). Error: %(error_detail)s") %
#                 {'voucher_pk': voucher_to_delete.pk, 'error_detail': str(e)}
#             ) from e  # Chain the original exception for full traceback
#     else:
#         status_val = getattr(voucher_to_delete, 'status', 'AttributeMissing')
#         logger.info(
#             f"{log_prefix} Voucher status is '{status_val}' (not POSTED). No synchronous balance reversal needed.")
#     logger.info(f"{log_prefix} --- SIGNAL END ---")
#
#
# @receiver(post_save, sender=Voucher, dispatch_uid="crp_accounting_voucher_post_save_sync_update")
# def handle_voucher_post_save_sync_balances(sender, instance: Voucher, created, update_fields=None, **kwargs):
#     """
#     Handles Voucher post_save:
#     - If status becomes POSTED and balances_updated is False, perform synchronous balance update.
#     - If status changes FROM POSTED to something else and balances_updated was True, reset the flag.
#     """
#     log_prefix = f"[POST_SAVE_VCH_SIGNAL][Co:{instance.company_id}][Vch:{instance.pk}]"
#     logger.debug(f"{log_prefix} --- SIGNAL START --- Created: {created}, Update_fields: {update_fields}")
#
#     if not instance.company_id:
#         logger.error(f"{log_prefix} Voucher saved without company_id! Cannot process sync balance update.")
#         return
#     if not hasattr(instance, 'status') or not hasattr(instance, 'balances_updated'):
#         logger.error(
#             f"{log_prefix} Voucher instance missing 'status' or 'balances_updated' attributes. Signal aborted.")
#         return
#
#     # Check if 'status' was actually part of the fields being updated, or if it's a new instance.
#     # This helps avoid unnecessary processing if only other fields were saved.
#     status_field_was_effectively_updated = created or update_fields is None or 'status' in update_fields
#
#     if instance.status == TransactionStatus.POSTED.value:
#         # Only proceed if status is POSTED AND (it's a new POSTED voucher OR status was just changed to POSTED)
#         # AND balances are not already marked as updated.
#         if not instance.balances_updated:
#             if status_field_was_effectively_updated or created:  # Ensure this is a fresh post or status change to post
#                 logger.info(
#                     f"{log_prefix} Voucher status is POSTED & balances_updated=False. "
#                     f"Performing synchronous balance update for '{instance.voucher_number or instance.pk}'."
#                 )
#                 try:
#                     # Perform update after the main transaction commits to ensure voucher and lines are fully saved.
#                     transaction.on_commit(
#                         lambda: _synchronously_update_balances_for_voucher(
#                             voucher_pk=instance.pk,
#                             company_pk=instance.company_id,
#                             is_reversal=False  # This is a normal posting
#                         )
#                     )
#                 except Exception as e:  # Catch errors from trying to schedule on_commit (should be rare)
#                     logger.critical(
#                         f"{log_prefix} CRITICAL: Error scheduling on_commit balance update for POSTED voucher: {e}",
#                         exc_info=True)
#             else:
#                 logger.debug(
#                     f"{log_prefix} Voucher is POSTED & balances_updated=False, but status field was not in update_fields. Assuming no change to posting status that requires immediate balance update.")
#         else:  # Is POSTED and balances_updated is True
#             logger.debug(
#                 f"{log_prefix} Voucher is POSTED & balances_updated=True. Assuming balances are current. No sync update performed.")
#
#     # If status changed *away from* POSTED (or was never POSTED but flag is somehow true)
#     elif instance.balances_updated:  # This means (status != POSTED) AND (balances_updated == True)
#         if status_field_was_effectively_updated or created:  # Ensure status change is relevant
#             logger.warning(
#                 f"{log_prefix} Voucher status is '{instance.get_status_display()}' (not POSTED) "
#                 f"but balances_updated=True. Resetting flag for '{instance.voucher_number or instance.pk}'."
#             )
#             transaction.on_commit(
#                 lambda: _reset_balances_updated_flag_on_commit(
#                     voucher_pk=instance.pk,
#                     company_pk=instance.company_id
#                 )
#             )
#     logger.debug(f"{log_prefix} --- SIGNAL END ---")
#
#
# @receiver(post_save, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_post_save_invalidate")
# @receiver(pre_delete, sender=VoucherLine, dispatch_uid="crp_accounting_voucherline_pre_delete_invalidate")
# def handle_voucher_line_change_for_posted_voucher_flag_reset(sender, instance: VoucherLine, **kwargs):
#     """
#     If a VoucherLine is saved or deleted for an already POSTED Voucher,
#     this invalidates the parent voucher's calculated balances.
#     This signal will reset the parent voucher's `balances_updated` flag.
#     Actual recalculation should be triggered by re-posting the voucher or a manual process.
#     """
#     try:
#         voucher = instance.voucher  # Access parent voucher
#         if not voucher or not voucher.pk:  # Ensure voucher and its PK are valid
#             logger.debug(f"VoucherLine signal: Line {instance.pk} has no valid parent voucher. Skipping.")
#             return
#     except ObjectDoesNotExist:  # If voucher was already deleted or relation is broken
#         logger.warning(f"VoucherLine signal: Parent voucher for Line {instance.pk} does not exist. Skipping.")
#         return
#
#     log_prefix = f"[VCHLINE_CHANGE_SIGNAL][Co:{voucher.company_id}][Vch:{voucher.pk}][Line:{instance.pk}]"
#
#     if not voucher.company_id:
#         logger.error(f"{log_prefix} Parent voucher is missing company_id. Cannot process flag reset.")
#         return
#
#     # Only act if the parent voucher is currently POSTED
#     if hasattr(voucher, 'status') and voucher.status == TransactionStatus.POSTED.value:
#         # And only if its balances were previously considered updated
#         if hasattr(Voucher, 'balances_updated') and voucher.balances_updated:
#             logger.info(
#                 f"{log_prefix} Line changed/deleted for POSTED voucher '{voucher.voucher_number or voucher.pk}'. "
#                 f"Resetting parent voucher's balances_updated flag."
#             )
#             transaction.on_commit(
#                 lambda: _reset_balances_updated_flag_on_commit(
#                     voucher_pk=voucher.pk,
#                     company_pk=voucher.company_id
#                 )
#             )
#             # DECISION POINT: Do you want to *immediately* try to recalculate balances here?
#             # Doing so can be resource-intensive if many lines are changed rapidly or via bulk operations.
#             # Option: Uncomment to trigger immediate recalculation (synchronous).
#             # logger.info(f"{log_prefix} Additionally triggering immediate synchronous balance re-calculation for parent voucher.")
#             # transaction.on_commit(
#             #    lambda: _synchronously_update_balances_for_voucher(
#             #        voucher_pk=voucher.pk,
#             #        company_pk=voucher.company_id,
#             #        is_reversal=False # False means re-calculate based on current lines
#             #    )
#             # )
#         else:
#             logger.debug(
#                 f"{log_prefix} Line changed for POSTED voucher, but balances_updated was already False or not applicable. No flag reset needed.")
#     else:
#         status_display = voucher.get_status_display() if hasattr(voucher, 'get_status_display') else voucher.status
#         logger.debug(
#             f"{log_prefix} Line changed, but parent voucher status is '{status_display}' (not POSTED). No flag reset or recalculation triggered by this line change.")
#     logger.debug(f"{log_prefix} --- SIGNAL END ---")