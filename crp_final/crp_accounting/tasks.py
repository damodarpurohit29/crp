# crp_accounting/tasks.py
import logging
from decimal import Decimal
from typing import Optional, Any  # For type hinting voucher_id and company_id

from celery import shared_task, exceptions as celery_exceptions
from django.db import transaction, OperationalError
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.conf import settings  # For settings.AUTH_USER_MODEL if needed for logging context

# --- Model Imports ---
# Ensure these paths are correct for your project structure
try:
    from .models.journal import Voucher, VoucherLine, DrCrType, TransactionStatus
    from .models.coa import Account, AccountType
    from company.models import Company  # Import Company for type checking and explicit use
except ImportError as e:
    # This is a critical failure at startup if models can't be imported.
    logging.critical(f"CRP Accounting Tasks: CRITICAL - Could not import necessary models. Tasks will fail. Error: {e}")
    # To prevent Celery worker from repeatedly trying to load a broken task,
    # you might reraise or define dummy classes if strictly necessary for import.
    # However, fixing the import is the real solution.
    raise ImportError(f"Could not import necessary models for tasks.py. Check paths and dependencies: {e}")

logger = logging.getLogger("crp_accounting.tasks")  # Specific logger for tasks

# --- Constants ---
MAX_RETRIES_BAL_UPDATE = getattr(settings, 'CELERY_TASK_BALANCE_UPDATE_MAX_RETRIES', 3)
RETRY_DELAY_BAL_UPDATE = getattr(settings, 'CELERY_TASK_BALANCE_UPDATE_RETRY_DELAY', 60)  # seconds
ZERO_DECIMAL = Decimal('0.00')


# --- Helper Methods for Account Type Logic (Values from Enums) ---
def _account_affects_balance_positively_on_credit(account_type_value: str) -> bool:
    return account_type_value in [
        AccountType.LIABILITY.value, AccountType.EQUITY.value, AccountType.INCOME.value
    ]


def _account_affects_balance_positively_on_debit(account_type_value: str) -> bool:
    return account_type_value in [
        AccountType.ASSET.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value
    ]


# --- Asynchronous Task (Tenant Aware) ---

@shared_task(
    bind=True,  # Allows access to self (the task instance) for retries, etc.
    max_retries=MAX_RETRIES_BAL_UPDATE,
    default_retry_delay=RETRY_DELAY_BAL_UPDATE,
    name="crp_accounting.tasks.update_account_balances_for_voucher",  # Explicit task name
    autoretry_for=(OperationalError,),  # Automatically retry for OperationalError
    retry_backoff=True,  # Use exponential backoff for retries
    acks_late=True  # Acknowledge task only after it completes (or fails permanently)
)
def update_account_balances_task(self, voucher_id: Any, company_id: Any):
    """
    Asynchronous Celery task to update account balances based on a POSTED voucher
    for a specific COMPANY.

    Args:
        voucher_id: The PK of the Voucher.
        company_id: The PK of the Company to which the Voucher and Accounts belong.

    Uses a `balances_updated` flag on the Voucher model for idempotency.
    Handles retries for operational database errors.
    """
    task_id = self.request.id or "sync_run"
    log_prefix = f"[Task:{task_id}][Co:{company_id}][Vch:{voucher_id}]"

    if not company_id:
        logger.error(f"{log_prefix} CRITICAL: Task called without company_id. Aborting.")
        # This is a programming error, do not retry.
        return  # Or raise a non-retryable exception

    logger.info(f"{log_prefix} Starting balance update check.")

    # --- Idempotency Check (Tenant-Aware) ---
    # CRITICAL: Assumes Voucher model has a BooleanField named `balances_updated`.
    if not hasattr(Voucher, 'balances_updated'):
        logger.critical(f"{log_prefix} CRITICAL ERROR: Voucher model is missing 'balances_updated' field. Aborting.")
        return

    try:
        # Check the flag within the specific company context
        if Voucher.objects.filter(pk=voucher_id, company_id=company_id, balances_updated=True).exists():
            logger.info(f"{log_prefix} Skipping: Balances already marked as updated.")
            return
    except OperationalError as oe_check:
        logger.warning(
            f"{log_prefix} DB operational error during idempotency check: {oe_check}. Retrying as per task config...")
        raise  # Celery will catch this due to autoretry_for=(OperationalError,)
    except Exception as e_check:
        logger.error(f"{log_prefix} Unexpected error during idempotency check: {e_check}. Failing task.", exc_info=True)
        return  # Fail if check fails unexpectedly (not an OperationalError)

    # --- Fetch Voucher and Process Lines (Tenant-Aware) ---
    try:
        voucher = Voucher.objects.prefetch_related(
            'lines__account__company'  # Ensure company of account is available if needed
        ).get(pk=voucher_id, company_id=company_id)

        if voucher.status != TransactionStatus.POSTED.value:
            logger.warning(
                f"{log_prefix} Voucher is not POSTED (Status: {voucher.get_status_display()}). Skipping balance update.")
            if voucher.balances_updated:  # Should not happen if logic is correct, but defensive
                logger.warning(f"{log_prefix} Resetting balances_updated flag for non-POSTED voucher.")
                Voucher.objects.filter(pk=voucher_id, company_id=company_id).update(balances_updated=False,
                                                                                    updated_at=timezone.now())
            return

        logger.info(f"{log_prefix} Processing POSTED voucher '{voucher.voucher_number or voucher_id}'.")
        current_time = timezone.now()  # Use a consistent timestamp for all updates in this run

        with transaction.atomic():
            processed_accounts_pks = set()
            for line in voucher.lines.all():  # lines are already related to this voucher
                if not line.account_id or line.amount is None or line.amount == ZERO_DECIMAL:
                    logger.warning(
                        f"{log_prefix} Skipping invalid VoucherLine {line.pk} (Account: {line.account_id}, Amount: {line.amount})")
                    continue

                account_pk_to_update = line.account_id
                try:
                    # Lock the account row for update within the company
                    acc_to_update = Account.objects.select_for_update().get(pk=account_pk_to_update,
                                                                            company_id=company_id)

                    if acc_to_update.current_balance is None:
                        logger.warning(
                            f"{log_prefix} Account {account_pk_to_update} had NULL balance. Initializing to 0.")
                        acc_to_update.current_balance = ZERO_DECIMAL

                    original_balance = acc_to_update.current_balance
                    adjustment = line.amount

                    if line.dr_cr == DrCrType.DEBIT.value:
                        acc_to_update.current_balance += adjustment if _account_affects_balance_positively_on_debit(
                            acc_to_update.account_type) else -adjustment
                    elif line.dr_cr == DrCrType.CREDIT.value:
                        acc_to_update.current_balance += adjustment if _account_affects_balance_positively_on_credit(
                            acc_to_update.account_type) else -adjustment
                    else:
                        logger.error(
                            f"{log_prefix} Invalid DrCrType '{line.dr_cr}' on VoucherLine {line.pk}. Skipping line.")
                        continue

                    acc_to_update.balance_last_updated = current_time
                    acc_to_update.save(update_fields=['current_balance', 'balance_last_updated'])

                    processed_accounts_pks.add(account_pk_to_update)
                    logger.debug(
                        f"{log_prefix} Updated balance Account {account_pk_to_update}: {original_balance} -> {acc_to_update.current_balance} (Line {line.pk})")

                except Account.DoesNotExist:
                    logger.error(
                        f"{log_prefix} Account {account_pk_to_update} (from Line {line.pk}) not found in Company {company_id}!")
                    # This is a data integrity issue. Decide if the whole task should fail or just log.
                    # For now, log and continue with other lines, but the voucher update at the end might be partial.
                except OperationalError as oe_acct:
                    logger.warning(
                        f"{log_prefix} DB lock/operational error updating Account {account_pk_to_update}: {oe_acct}. Retrying task...")
                    raise  # Let Celery handle retry based on autoretry_for
                except Exception as e_acct:
                    logger.exception(
                        f"{log_prefix} Unexpected error updating Account {account_pk_to_update} (Line {line.pk}): {e_acct}")
                    raise  # Reraise to roll back the atomic transaction and potentially retry task

            # If all lines processed successfully within the atomic block:
            logger.debug(
                f"{log_prefix} Atomic balance update transaction committed for {len(processed_accounts_pks)} accounts.")

        # Mark Voucher as Updated (AFTER successful transaction commit for lines)
        # This is outside the atomic block for lines, as it's a separate concern.
        try:
            rows_updated = Voucher.objects.filter(pk=voucher_id, company_id=company_id).update(
                balances_updated=True,
                updated_at=current_time  # Update voucher's own updated_at
            )
            if rows_updated > 0:
                logger.info(f"{log_prefix} Successfully marked Voucher balances as updated.")
            else:
                # Could happen if voucher was deleted/changed concurrently after line processing.
                logger.warning(
                    f"{log_prefix} Could not mark Voucher as updated (affected 0 rows). State might be inconsistent.")
        except OperationalError as oe_mark:
            logger.error(
                f"{log_prefix} ALERT: DB error marking Voucher as updated: {oe_mark}. Balances WERE updated. Manual check needed for flag.")
            # Do not retry this part if the core logic succeeded, to avoid re-processing balances. Alert instead.
        except Exception as e_mark:
            logger.critical(
                f"{log_prefix} ALERT: Balances updated, BUT MARKING VOUCHER FAILED: {e_mark}. Manual check needed for flag.",
                exc_info=True)

        logger.info(
            f"{log_prefix} Finished balance update task successfully. Accounts processed: {len(processed_accounts_pks)}")

    except Voucher.DoesNotExist:
        logger.error(f"{log_prefix} Voucher not found. Cannot update balances.")
        # No retry if voucher doesn't exist in the specified company.
    except celery_exceptions.Retry:
        logger.warning(f"{log_prefix} Task explicitly retrying due to caught exception.")
        raise  # Re-raise to let Celery handle the retry as configured
    except OperationalError as oe_task:  # Handles errors like initial voucher fetch
        logger.warning(
            f"{log_prefix} Database operational error during task execution: {oe_task}. Retrying task as per config...")
        raise  # Celery will catch this due to autoretry_for=(OperationalError,)
    except Exception as e_task:
        logger.exception(f"{log_prefix} Unhandled error processing balance update: {e_task}")
        try:
            # Attempt Celery's retry for other unexpected errors based on task's default policy
            # This might lead to retries even if it's a non-transient error.
            # Consider more specific error handling if certain errors should not be retried.
            raise self.retry(exc=e_task, countdown=RETRY_DELAY_BAL_UPDATE * (self.request.retries + 1))
        except celery_exceptions.MaxRetriesExceededError:
            logger.critical(
                f"{log_prefix} Max retries exceeded. Balance update failed permanently. ALERTING SYSTEM NEEDED.")
        except Exception as retry_e:
            logger.error(f"{log_prefix} Error attempting to retry task: {retry_e}")
            # If retry itself fails, nothing more can be done automatically here.

