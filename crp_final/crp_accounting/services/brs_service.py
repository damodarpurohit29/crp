import csv
import hashlib
import io
import logging
import re  # For more complex string cleaning
from datetime import date, datetime
from decimal import Decimal, InvalidOperation as DecimalInvalidOperation, ROUND_HALF_UP
from typing import List, Dict, Any, Optional, Tuple, Union

from django.core.files.base import ContentFile
from django.db import transaction, IntegrityError
from django.shortcuts import get_object_or_404  # For convenience
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.conf import settings
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

# --- Model Imports ---
from company.models import Company

try:
    from company.models_settings import CompanyAccountingSettings
except ImportError:
    CompanyAccountingSettings = None  # type: ignore
    logging.critical(
        "BRS Service: CRITICAL - CompanyAccountingSettings model not found. Default accounts for adjustments will fail.")

from ..models.reconciliation import (
    BankStatementUpload, BankStatementTransaction, BankReconciliation, ReconciledItemPair
)
from ..models.coa import Account
from ..models.journal import Voucher, VoucherLine, DrCrType, TransactionStatus, VoucherType
from ..models.period import AccountingPeriod

# --- Enum Imports ---
from crp_core.enums import AccountNature, AccountType

# --- Service Imports ---
from . import voucher_service
from . import ledger_service

# --- PDF Parsing Library ---
PDFPLUMBER_AVAILABLE = False
try:
    import pdfplumber

    PDFPLUMBER_AVAILABLE = True
except ImportError:
    logging.warning("BRS Service: pdfplumber library not found. PDF statement parsing will be disabled.")

logger = logging.getLogger("crp_accounting.services.brs")
User = settings.AUTH_USER_MODEL
ZERO = Decimal('0.00')


# --- Custom Service Exceptions ---
class BRSServiceError(Exception): """Base BRS service error."""


class StatementParsingError(BRSServiceError): """Error during statement file parsing."""


class ReconciliationError(BRSServiceError): """Error during the reconciliation process itself."""


class MatchingError(BRSServiceError): """Error when trying to match bank and GL items."""


class GLPostingError(BRSServiceError): """Error during GL posting for BRS adjustments."""


# =============================================================================
# File Hashing Utility
# =============================================================================
def _calculate_file_hash(file_obj_bytes: bytes) -> str:
    """Calculates MD5 hash from file bytes."""
    hash_md5 = hashlib.md5()
    hash_md5.update(file_obj_bytes)
    return hash_md5.hexdigest()


# =============================================================================
# Bank Statement Upload Record Creation
# =============================================================================
@transaction.atomic
def create_statement_upload_record(
        company_id: Any, bank_account_id: Any, uploaded_by_user: User,
        statement_file: Any, file_name: str,  # InMemoryUploadedFile
        statement_period_start: Optional[date] = None, statement_period_end: Optional[date] = None
) -> BankStatementUpload:
    log_prefix = f"[CreateUpload][Co:{company_id}][User:{uploaded_by_user.name if uploaded_by_user else 'System'}]"
    logger.info(f"{log_prefix} Creating upload record for file '{file_name}'.")
    try:
        company = Company.objects.get(pk=company_id)
        bank_account = Account.objects.get(pk=bank_account_id, company=company, account_type=AccountType.ASSET.value)
    except Company.DoesNotExist:
        raise BRSServiceError(_("Invalid company for statement upload."))
    except Account.DoesNotExist:
        raise BRSServiceError(_("Bank account not found or not an Asset type for company."))

    # Read file content for hashing and ContentFile creation
    statement_file.seek(0);
    file_content_bytes = statement_file.read();
    statement_file.seek(0)
    file_hash_val = _calculate_file_hash(file_content_bytes)

    # Robust duplicate check: same company, bank account, file hash, and overlapping/same period end date
    # This helps prevent re-processing identical statements for the same period end.
    duplicate_qs = BankStatementUpload.objects.filter(company=company, bank_account=bank_account,
                                                      file_hash=file_hash_val)
    if statement_period_end: duplicate_qs = duplicate_qs.filter(statement_period_end_date=statement_period_end)

    # Check against already COMPLETED uploads more strictly
    if duplicate_qs.filter(status=BankStatementUpload.UploadStatus.COMPLETED.value).exists():
        logger.error(
            f"{log_prefix} Duplicate file upload detected (Hash: {file_hash_val}, Bank Acct: {bank_account.id}, Stmt End: {statement_period_end}). Matches existing COMPLETED upload.")
        raise BRSServiceError(
            _("This statement file appears to have been already uploaded and processed successfully for this bank account and period end date."))

    upload = BankStatementUpload(
        company=company, bank_account=bank_account, uploaded_by=uploaded_by_user,
        statement_file=ContentFile(file_content_bytes, name=file_name),  # Use original name for storage
        file_hash=file_hash_val, status=BankStatementUpload.UploadStatus.PENDING.value,
        statement_period_start_date=statement_period_start, statement_period_end_date=statement_period_end
    )
    try:
        upload.full_clean(exclude=['transaction_count_in_file', 'transactions_imported_count'])
    except DjangoValidationError as e:
        raise BRSServiceError(e.message_dict)
    upload.save()
    logger.info(
        f"{log_prefix} Created BankStatementUpload record PK {upload.pk} for file '{upload.statement_file.name}'.")
    return upload


# =============================================================================
# Statement Parsing Logic (CSV and PDF - HDFC Example)
# =============================================================================
def _clean_amount_string(amount_str: str) -> str:
    """Removes common currency symbols and commas for Decimal conversion."""
    if not amount_str: return "0"
    return amount_str.replace(',', '').replace('₹', '').replace('$', '').replace('€', '').strip()


def _parse_generic_csv_statement(file_stream: io.TextIOWrapper, log_prefix: str) -> Tuple[
    List[Dict[str, Any]], List[str]]:
    """
    Parses a generic CSV statement.
    Assumed order: Date, Description, Debit, Credit, [Balance], [Reference]
    Date format: YYYY-MM-DD (needs to be made configurable or auto-detected)
    """
    transactions_data = []
    parsing_errors: List[str] = []
    reader = csv.reader(file_stream)

    try:
        header = next(reader, None)  # Attempt to skip header
    except StopIteration:
        logger.info(f"{log_prefix} CSV file is empty."); return [], []
    if header: logger.debug(f"{log_prefix} CSV Header: {header}")

    line_num = 1  # After header
    for row in reader:
        line_num += 1
        if not any(field.strip() for field in row) or len(row) < 4:  # Min Date, Desc, Debit, Credit
            parsing_errors.append(_("L%(n)s: Skipped - row is empty or has too few columns.") % {'n': line_num})
            continue
        try:
            date_str, desc_str, debit_str, credit_str = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            balance_str = row[4].strip() if len(row) > 4 else None
            ref_str = row[5].strip() if len(row) > 5 else None

            if not date_str or not desc_str:
                parsing_errors.append(_("L%(n)s: Date or Description missing.") % {'n': line_num});
                continue

            try:
                txn_date = datetime.strptime(date_str, '%Y-%m-%d').date()  # TODO: Make date format configurable
            except ValueError:
                parsing_errors.append(_("L%(n)s: Invalid date format '%(d)s'. Expected YYYY-MM-DD.") % {'n': line_num,
                                                                                                        'd': date_str}); continue

            debit_val = Decimal(_clean_amount_string(debit_str)) if debit_str else ZERO
            credit_val = Decimal(_clean_amount_string(credit_str)) if credit_str else ZERO

            amount, txn_type = ZERO, None
            if debit_val > ZERO and credit_val == ZERO:
                amount = debit_val; txn_type = BankStatementTransaction.TransactionType.DEBIT.value
            elif credit_val > ZERO and debit_val == ZERO:
                amount = credit_val; txn_type = BankStatementTransaction.TransactionType.CREDIT.value
            elif debit_val == ZERO and credit_val == ZERO:
                parsing_errors.append(_("L%(n)s: Both debit and credit are zero.") % {'n': line_num}); continue
            else:
                parsing_errors.append(
                    _("L%(n)s: Ambiguous debit/credit (both have values or invalid).") % {'n': line_num}); continue

            balance_after = Decimal(_clean_amount_string(balance_str)) if balance_str else None
            transactions_data.append({
                'transaction_date': txn_date, 'posting_date': txn_date,  # Assume posting date = transaction date
                'description': desc_str, 'reference_number': ref_str,
                'transaction_type': txn_type, 'amount': amount,
                'balance_after_transaction': balance_after
            })
        except (IndexError, ValueError, DecimalInvalidOperation, TypeError) as e_row:
            parsing_errors.append(
                _("L%(n)s: Error parsing row - %(e)s. Data: %(data)s") % {'n': line_num, 'e': str(e_row),
                                                                          'data': str(row)[:150]})
        except Exception as e_unexp_csv_row:
            parsing_errors.append(
                _("L%(n)s: Unexpected error processing row - %(e)s.") % {'n': line_num, 'e': str(e_unexp_csv_row)})
            logger.error(f"{log_prefix} Unexpected CSV row error: {e_unexp_csv_row}", exc_info=True)
    return transactions_data, parsing_errors


def _parse_hdfc_bank_pdf_statement(pdf_file_obj: io.BytesIO, log_prefix: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parses a PDF statement with a structure similar to the HDFC example provided.
    Uses pdfplumber. This is a specific parser and will need adjustment for different PDF layouts.
    """
    if not PDFPLUMBER_AVAILABLE:
        return [], [_("PDF parsing library (pdfplumber) is not installed. Cannot process PDF file.")]

    transactions_data: List[Dict[str, Any]] = []
    parsing_errors: List[str] = []
    logger.info(f"{log_prefix} Starting PDF parsing (HDFC-like format).")

    try:
        with pdfplumber.open(pdf_file_obj) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                logger.debug(f"{log_prefix} Processing PDF page {page_num}")
                # pdfplumber's default table extraction settings. May need fine-tuning.
                # table_settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                # tables = page.extract_tables(table_settings)
                tables = page.extract_tables()  # Simpler default for now

                if not tables:
                    logger.debug(f"{log_prefix} Page {page_num}: No tables found by pdfplumber.");
                    continue

                for table_idx, table_rows in enumerate(tables, 1):
                    logger.debug(f"{log_prefix} Page {page_num}, Table {table_idx}: Found {len(table_rows)} rows.")
                    if not table_rows: continue

                    # Attempt to identify a transaction table based on expected number of columns or keywords in header
                    # This is highly heuristic. Example: HDFC format had ~7 main columns for transactions.
                    # Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit Amt., Closing Balance
                    #  0      1           2             3          4               5               6

                    # Simple heuristic: a transaction row should have date and some description, and either debit or credit
                    # We might need to skip header rows if they are part of table_rows
                    header_candidates = ["date", "transaction date", "narration", "particulars", "description",
                                         "withdrawal", "deposit"]

                    data_row_start_index = 0
                    if table_rows[0]:  # Check if first row could be a header
                        first_row_text = " ".join(filter(None, table_rows[0])).lower()
                        if any(hc in first_row_text for hc in header_candidates):
                            logger.debug(
                                f"{log_prefix} Page {page_num}, Table {table_idx}: Detected potential header row, skipping first row for data.")
                            data_row_start_index = 1

                    for row_num_in_table, row_cells in enumerate(table_rows[data_row_start_index:],
                                                                 data_row_start_index + 1):
                        if not any(cell and str(cell).strip() for cell in row_cells) or len(
                                row_cells) < 6:  # Need at least Date, Desc, Ref, ValDate, Dr, Cr
                            parsing_errors.append(
                                _("P%(pn)s T%(tn)s R%(rn)s: Skipped - empty or too few columns (%(cc)s).") % {
                                    'pn': page_num, 'tn': table_idx, 'rn': row_num_in_table, 'cc': len(row_cells)})
                            continue

                        try:
                            # Column mapping specific to HDFC screenshot
                            date_str = (str(row_cells[0]) if row_cells[0] else "").strip()
                            desc_str = (str(row_cells[1]) if row_cells[1] else "").strip()
                            ref_str = (str(row_cells[2]) if row_cells[2] else "").strip()
                            val_date_str = (str(row_cells[3]) if row_cells[3] else "").strip()
                            debit_str = (str(row_cells[4]) if row_cells[4] else "").strip()
                            credit_str = (str(row_cells[5]) if row_cells[5] else "").strip()
                            bal_str = (str(row_cells[6]) if len(row_cells) > 6 and row_cells[6] else "").strip()

                            if not date_str or not desc_str:  # Basic validation
                                parsing_errors.append(
                                    _("P%(pn)s T%(tn)s R%(rn)s: Date or Description missing.") % {'pn': page_num,
                                                                                                  'tn': table_idx,
                                                                                                  'rn': row_num_in_table});
                                continue

                            try:
                                # Handle dates like "21/05/24" or "21/05/2024"
                                txn_date = datetime.strptime(date_str, '%d/%m/%y').date() if len(
                                    date_str.split('/')[-1]) == 2 else datetime.strptime(date_str, '%d/%m/%Y').date()
                                post_date = (datetime.strptime(val_date_str, '%d/%m/%y').date() if len(
                                    val_date_str.split('/')[-1]) == 2 else datetime.strptime(val_date_str,
                                                                                             '%d/%m/%Y').date()) if val_date_str else txn_date
                            except ValueError:
                                parsing_errors.append(
                                    _("P%(pn)s T%(tn)s R%(rn)s: Invalid date format: D='%(d)s', VD='%(vd)s'. Expected DD/MM/YY or DD/MM/YYYY.") % {
                                        'pn': page_num, 'tn': table_idx, 'rn': row_num_in_table, 'd': date_str,
                                        'vd': val_date_str});
                                continue

                            debit_val = Decimal(_clean_amount_string(debit_str)) if debit_str else ZERO
                            credit_val = Decimal(_clean_amount_string(credit_str)) if credit_str else ZERO

                            amount, txn_type = ZERO, None
                            if debit_val > ZERO and credit_val == ZERO:
                                amount = debit_val; txn_type = BankStatementTransaction.TransactionType.DEBIT.value
                            elif credit_val > ZERO and debit_val == ZERO:
                                amount = credit_val; txn_type = BankStatementTransaction.TransactionType.CREDIT.value
                            elif debit_val == ZERO and credit_val == ZERO and desc_str:  # Some PDFs have info lines with no amounts
                                logger.debug(
                                    f"{log_prefix} P{page_num} T{table_idx} R{row_num_in_table}: Informational line with no amount, skipping financial entry: {desc_str[:50]}")
                                continue  # Skip, not a financial transaction
                            else:
                                parsing_errors.append(
                                    _("P%(pn)s T%(tn)s R%(rn)s: Ambiguous debit/credit or zero value.") % {
                                        'pn': page_num, 'tn': table_idx, 'rn': row_num_in_table}); continue

                            balance_after = Decimal(_clean_amount_string(bal_str)) if bal_str else None

                            transactions_data.append({
                                'transaction_date': txn_date, 'posting_date': post_date,
                                'description': desc_str, 'reference_number': ref_str,
                                'transaction_type': txn_type, 'amount': amount,
                                'balance_after_transaction': balance_after,
                                'raw_pdf_row_data': " | ".join(map(str, row_cells))  # Optional: for debugging
                            })
                        except (IndexError, ValueError, DecimalInvalidOperation, TypeError) as e_row:
                            parsing_errors.append(
                                _("P%(pn)s T%(tn)s R%(rn)s: Error parsing - %(e)s. Data: %(d)s") % {'pn': page_num,
                                                                                                    'tn': table_idx,
                                                                                                    'rn': row_num_in_table,
                                                                                                    'e': str(e_row),
                                                                                                    'd': str(row_cells)[
                                                                                                         :150]})
                        except Exception as e_unexp_pdf_row:
                            parsing_errors.append(
                                _("P%(pn)s T%(tn)s R%(rn)s: Unexpected row error - %(e)s.") % {'pn': page_num,
                                                                                               'tn': table_idx,
                                                                                               'rn': row_num_in_table,
                                                                                               'e': str(
                                                                                                   e_unexp_pdf_row)})
                            logger.error(f"{log_prefix} Unexpected PDF row error: {e_unexp_pdf_row}", exc_info=True)
    except Exception as e_pdf_main:
        logger.exception(f"{log_prefix} Major failure processing PDF file '{getattr(pdf_file_obj, 'name', 'N/A')}'.")
        parsing_errors.append(
            _("Overall PDF processing error: %(err)s. Check logs for details.") % {'err': str(e_pdf_main)})

    return transactions_data, parsing_errors


@transaction.atomic
def process_statement_upload(statement_upload_id: Any, user_performing_process: Optional[User] = None) -> Tuple[
    int, List[str]]:
    upload = get_object_or_404(BankStatementUpload.objects.select_for_update(), pk=statement_upload_id)
    log_prefix = f"[ProcessUpload][Co:{upload.company_id}][Upload:{upload.pk}]"
    logger.info(f"{log_prefix} Starting processing for file '{upload.statement_file.name}'.")

    if upload.status not in [BankStatementUpload.UploadStatus.PENDING.value,
                             BankStatementUpload.UploadStatus.FAILED.value,
                             BankStatementUpload.UploadStatus.PARTIAL_IMPORT.value]:
        logger.warning(f"{log_prefix} Upload status '{upload.get_status_display()}' not processable.");
        return 0, [_("Statement already processed or currently processing.")]

    upload.status = BankStatementUpload.UploadStatus.PROCESSING.value
    upload.processing_notes = f"Processing by {user_performing_process.name if user_performing_process else 'System'} at {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    upload.save(update_fields=['status', 'processing_notes', 'updated_at'])

    imported_count = 0;
    errors: List[str] = [];
    parsed_transactions_data: List[Dict[str, Any]] = []
    file_name_lower = upload.statement_file.name.lower()

    try:
        with upload.statement_file.open('rb') as file_bytes_io:  # Always open as bytes for consistency
            file_content_bytes = file_bytes_io.read()  # Read all bytes

        temp_file_like_object = io.BytesIO(file_content_bytes)  # Use BytesIO for pdfplumber

        if file_name_lower.endswith('.csv'):
            logger.info(f"{log_prefix} Detected CSV. Using generic CSV parser.")
            # Wrap bytes in TextIOWrapper for csv.reader
            text_stream = io.TextIOWrapper(io.BytesIO(file_content_bytes), encoding='utf-8-sig', newline='')
            parsed_transactions_data, csv_errors = _parse_generic_csv_statement(text_stream, log_prefix)
            errors.extend(csv_errors)
        elif file_name_lower.endswith('.pdf'):
            if not PDFPLUMBER_AVAILABLE: raise StatementParsingError(_("PDFPlumber library not installed."))
            logger.info(f"{log_prefix} Detected PDF. Using HDFC-like PDF parser.")
            parsed_transactions_data, pdf_errors = _parse_hdfc_bank_pdf_statement(temp_file_like_object, log_prefix)
            errors.extend(pdf_errors)
        else:
            raise StatementParsingError(_("Unsupported file type: %(f)s.") % {'f': upload.statement_file.name})

        upload.transaction_count_in_file = len(parsed_transactions_data) + sum(
            1 for e in errors if "L" in e or "Row" in e)  # Rough count

        for txn_data in parsed_transactions_data:
            # Idempotency: Prevent exact duplicates from the same file IF transaction_id is not unique/available
            # A more robust check would use a bank-provided unique transaction ID if available
            if BankStatementTransaction.objects.filter(
                    upload_source=upload, transaction_date=txn_data['transaction_date'],
                    amount=txn_data['amount'], transaction_type=txn_data['transaction_type'],
                    description__iexact=txn_data['description']  # Simple check, can be fooled
            ).exists():
                logger.info(
                    f"{log_prefix} Skipping potentially duplicate parsed transaction: {txn_data['transaction_date']}, {txn_data['description'][:30]}");
                continue

            BankStatementTransaction.objects.create(
                company=upload.company, upload_source=upload, bank_account=upload.bank_account,
                **txn_data  # Unpack parsed data
            )
            imported_count += 1

        if errors:
            upload.status = BankStatementUpload.UploadStatus.PARTIAL_IMPORT.value if imported_count > 0 else BankStatementUpload.UploadStatus.FAILED.value
            upload.processing_notes += "\n--- PARSING ERRORS/WARNINGS ---\n" + "\n".join(errors)
            logger.warning(f"{log_prefix} Processed with {len(errors)} errors/warnings. Imported: {imported_count}.")
        else:
            upload.status = BankStatementUpload.UploadStatus.COMPLETED.value
            upload.processing_notes += "\nProcessing completed successfully."
            logger.info(f"{log_prefix} Processing completed successfully. Imported: {imported_count}.")

    except StatementParsingError as spe:
        upload.status = BankStatementUpload.UploadStatus.FAILED.value; upload.processing_notes += f"\nError: {str(spe)}"; errors.append(
            str(spe)); logger.error(f"{log_prefix} Parse error: {spe}")
    except Exception as e_main:
        upload.status = BankStatementUpload.UploadStatus.FAILED.value; proc_notes = upload.processing_notes or ""; upload.processing_notes = proc_notes + f"\nFATAL Error: {str(e_main)}"; errors.append(
            _("Fatal error: %(e)s") % {'e': str(e_main)}); logger.exception(f"{log_prefix} Fatal error.")
    finally:
        upload.transactions_imported_count = imported_count  # Persist count even on failure
        upload.save(
            update_fields=['status', 'processing_notes', 'transaction_count_in_file', 'transactions_imported_count',
                           'updated_at'])
    return imported_count, errors