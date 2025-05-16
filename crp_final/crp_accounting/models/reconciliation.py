# crp_accounting/models/reconciliation.py

import logging
from decimal import Decimal
from datetime import date  # Use this directly
from typing import Optional, Dict, Any

from django.db import models, IntegrityError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.conf import settings  # For AUTH_USER_MODEL

from crp_core.enums import AccountType

# --- Base Model & Company Import ---
try:
    from .base import TenantScopedModel
    from company.models import Company
except ImportError:
    raise ImportError("Could not import TenantScopedModel or Company. Critical for reconciliation models.")

# --- Related Accounting Model Imports ---
try:
    from .coa import Account
    from .journal import Voucher, VoucherLine  # Needed for linking/matching
except ImportError as e:
    raise ImportError(f"Could not import related accounting models for reconciliation: {e}.")

# --- Enum Imports ---
# (No BRS-specific enums are strictly necessary for the models themselves initially,
# but status fields will use choices.)

logger = logging.getLogger("crp_accounting.models.reconciliation")
ZERO_DECIMAL = Decimal('0.00')


# =============================================================================
# BankStatementUpload Model
# =============================================================================
class BankStatementUpload(TenantScopedModel):
    """
    Tracks uploaded bank statement files for reconciliation.
    The 'company' field is inherited from TenantScopedModel.
    """

    class UploadStatus(models.TextChoices):
        PENDING = 'PENDING', _('Pending Processing')
        PROCESSING = 'PROCESSING', _('Processing')
        COMPLETED = 'COMPLETED', _('Completed - Transactions Imported')
        PARTIAL_IMPORT = 'PARTIAL_IMPORT', _('Partially Imported - Some Errors')
        FAILED = 'FAILED', _('Failed - No Transactions Imported')
        DUPLICATE = 'DUPLICATE', _('Duplicate File (Already Processed)')

    bank_account = models.ForeignKey(
        Account,
        verbose_name=_("Bank Account in CoA"),
        on_delete=models.PROTECT,  # Don't delete upload if account is deleted; might be needed for audit
        related_name='statement_uploads',
        help_text=_("The company's bank account in the Chart of Accounts this statement relates to.")
    )
    statement_file = models.FileField(
        _("Statement File"),
        upload_to='bank_statements/%Y/%m/%d/',  # Organizes uploads by date
        help_text=_("The uploaded bank statement file (e.g., CSV, OFX, QIF).")
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Uploaded By"),
        on_delete=models.SET_NULL,
        null=True, blank=True,  # Might be uploaded by system or anonymous process in some cases
        related_name='bank_statement_uploads'
    )
    uploaded_at = models.DateTimeField(_("Uploaded At"), default=timezone.now, editable=False)
    statement_period_start_date = models.DateField(_("Statement Period Start Date"), null=True, blank=True, help_text=_(
        "Optional: Start date covered by the statement, if known from file or user input."))
    statement_period_end_date = models.DateField(_("Statement Period End Date"), null=True, blank=True,
                                                 help_text=_("Optional: End date covered by the statement."))
    file_hash = models.CharField(_("File Hash (MD5)"), max_length=32, blank=True, null=True, editable=False,
                                 db_index=True, help_text=_("MD5 hash of the file to detect duplicates."))
    status = models.CharField(
        _("Upload Status"), max_length=20, choices=UploadStatus.choices,
        default=UploadStatus.PENDING.value, db_index=True
    )
    processing_notes = models.TextField(_("Processing Notes/Errors"), blank=True, null=True)
    transaction_count_in_file = models.PositiveIntegerField(_("Transactions in File"), null=True, blank=True,
                                                            editable=False, help_text=_(
            "Number of transactions detected by parser in the file, if known."))
    transactions_imported_count = models.PositiveIntegerField(_("Transactions Imported"), default=0, editable=False,
                                                              help_text=_(
                                                                  "Number of BankStatementTransaction records created from this file."))

    class Meta:
        verbose_name = _("Bank Statement Upload")
        verbose_name_plural = _("Bank Statement Uploads")
        ordering = ['company__name', '-uploaded_at']
        indexes = [
            models.Index(fields=['company', 'bank_account', 'uploaded_at'], name='bsupload_co_bank_ts_idx'),
            models.Index(fields=['company', 'status'], name='bsupload_co_status_idx'),
            # Unique constraint on file_hash per company might be too strict if same file re-uploaded for different purpose
            # Consider uniqueness on (company, bank_account, file_hash, statement_period_end_date) if needed.
        ]

    def __str__(self):
        bank_acc_name = self.bank_account.account_name if self.bank_account_id and hasattr(self,
                                                                                           '_bank_account_cache') else \
            (Account.objects.get(pk=self.bank_account_id).account_name if self.bank_account_id else _("N/A Bank Acct"))
        co_name = self.company.name if self.company_id and hasattr(self, '_company_cache') else \
            (Company.objects.get(pk=self.company_id).name if self.company_id else _("N/A Co"))

        file_name_part = str(self.statement_file).split('/')[-1] if self.statement_file else _("No File")
        return f"Statement for {bank_acc_name} ({co_name}) - File: {file_name_part} ({self.get_status_display()})"

    def clean(self):
        super().clean()  # From TenantScopedModel for company assignment
        errors = {}
        # Ensure company context is determined
        effective_company: Optional[Company] = self.company
        if not effective_company and self.company_id:
            try:
                effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company assigned.")

        if not effective_company and not self._state.adding and 'company' not in errors:  # Existing record must have company
            errors['company'] = _("Company association is missing for this statement upload.")

        if self.bank_account_id:
            try:
                bank_acc_to_check = Account.objects.select_related('company').get(pk=self.bank_account_id)
                if effective_company and bank_acc_to_check.company != effective_company:
                    errors['bank_account'] = _("Selected Bank Account must belong to the upload's Company.")
                if bank_acc_to_check.account_type != AccountType.ASSET.value:  # Assuming crp_core.enums.AccountType
                    errors['bank_account'] = _("Selected account must be an Asset (Bank/Cash) type account.")
            except Account.DoesNotExist:
                errors['bank_account'] = _("Selected Bank Account not found.")
        elif not self._state.adding:  # Existing record must have bank account
            errors['bank_account'] = _("Bank Account is required for this statement upload.")

        if self.statement_period_start_date and self.statement_period_end_date and \
                self.statement_period_start_date > self.statement_period_end_date:
            errors['statement_period_end_date'] = _("Statement period end date cannot be before start date.")

        if errors: raise DjangoValidationError(errors)

    # save() inherited from TenantScopedModel


# =============================================================================
# BankStatementTransaction Model
# =============================================================================
class BankStatementTransaction(TenantScopedModel):  # Company from parent BankStatementUpload
    """
    An individual transaction line parsed from an uploaded bank statement.
    Company context is derived from the 'upload_source'.
    """

    class TransactionType(models.TextChoices):
        DEBIT = 'DEBIT', _('Debit (Withdrawal/Payment)')  # Money out from bank's perspective
        CREDIT = 'CREDIT', _('Credit (Deposit/Receipt)')  # Money in from bank's perspective
        OTHER = 'OTHER', _('Other/Info')

    upload_source = models.ForeignKey(
        BankStatementUpload,
        verbose_name=_("Upload Source"),
        on_delete=models.CASCADE,  # If upload record is deleted, its transactions are also deleted
        related_name='statement_transactions',
        help_text=_("The uploaded statement file this transaction came from.")
    )
    bank_account = models.ForeignKey(  # Denormalized for easier querying, must match upload_source.bank_account
        Account, verbose_name=_("Bank Account"), on_delete=models.PROTECT,
        related_name='bank_statement_lines',
        help_text=_("The company's bank account this transaction belongs to (denormalized from upload source).")
    )
    transaction_id = models.CharField(_("Bank Transaction ID"), max_length=100, blank=True, null=True, db_index=True,
                                      help_text=_("Unique ID from the bank statement, if available."))
    transaction_date = models.DateField(_("Transaction Date"), db_index=True)
    posting_date = models.DateField(_("Posting Date"), null=True, blank=True, db_index=True, help_text=_(
        "Date the transaction was posted by the bank, if different from transaction date."))
    description = models.TextField(_("Description/Payee"))
    amount = models.DecimalField(_("Amount"), max_digits=20, decimal_places=2)
    transaction_type = models.CharField(  # Debit or Credit as per bank statement convention
        _("Transaction Type (Bank's View)"), max_length=10, choices=TransactionType.choices,
        help_text=_("Debit if money out of account, Credit if money into account, from bank's perspective.")
    )
    balance_after_transaction = models.DecimalField(_("Balance After Txn"), max_digits=20, decimal_places=2, null=True,
                                                    blank=True, help_text=_(
            "Running balance on the statement after this transaction, if provided."))
    reference_number = models.CharField(_("Reference Number"), max_length=100, blank=True, null=True,
                                        help_text=_("Check number, payment reference, etc."))

    # --- Reconciliation Fields ---
    is_reconciled = models.BooleanField(_("Is Reconciled"), default=False, db_index=True)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Reconciled By"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_txns_reconciled'
    )
    reconciliation_date = models.DateTimeField(_("Reconciliation Date"), null=True, blank=True)

    # Link to a VoucherLine if directly matched (can be ManyToMany via ReconciledItemPair)
    # matched_gl_line = models.ForeignKey(VoucherLine, ...)

    class Meta:
        verbose_name = _("Bank Statement Transaction")
        verbose_name_plural = _("Bank Statement Transactions")
        ordering = ['upload_source__company__name', 'bank_account__account_number', 'transaction_date',
                    'pk']  # Ensure consistent order
        indexes = [
            models.Index(fields=['upload_source', 'transaction_date'], name='bstxn_upload_date_idx'),
            models.Index(fields=['company', 'bank_account', 'transaction_date', 'is_reconciled'],
                         name='bstxn_co_bank_date_rec_idx'),
            models.Index(fields=['company', 'transaction_id', 'transaction_date'], name='bstxn_co_txnid_date_idx'),
            # For finding by bank's ID
        ]
        # Consider unique_together for (bank_account, transaction_id, transaction_date, amount, description_hash) if high chance of duplicate imports
        # For now, duplicate detection is primarily at the BankStatementUpload (file_hash) level.

    def __str__(self):
        return f"{self.transaction_date} - {self.description[:50]} - {self.get_transaction_type_display()} {self.amount} (Bank Acct: {self.bank_account_id})"

    def clean(self):
        # Company is set from upload_source in save()
        super().clean()
        errors = {}
        if not self.upload_source_id: errors['upload_source'] = _("Must be linked to an upload source.")
        if not self.bank_account_id: errors['bank_account'] = _("Bank Account is required (denormalized).")

        if self.upload_source_id and self.bank_account_id:
            # Ensure bank_account on transaction matches bank_account on upload_source
            # and company context is derived correctly from upload_source
            if not hasattr(self, '_upload_source_cache') or not self._upload_source_cache:
                try:
                    self.upload_source = BankStatementUpload.objects.select_related('company', 'bank_account').get(
                        pk=self.upload_source_id)
                except BankStatementUpload.DoesNotExist:
                    errors['upload_source'] = _("Upload source not found."); raise DjangoValidationError(
                        errors)  # Fail early

            # Set company from parent upload source before TenantScopedModel's clean runs via full_clean in save
            if not self.company_id and self.upload_source.company_id:
                self.company = self.upload_source.company

            if self.bank_account_id != self.upload_source.bank_account_id:
                errors['bank_account'] = _("Transaction's bank account must match its upload source's bank account.")
            if self.company_id != self.upload_source.company_id:  # Should be set by now
                errors['company'] = _("Transaction's company must match its upload source's company.")

        if self.amount <= ZERO_DECIMAL: errors['amount'] = _("Transaction amount must be positive (type indicates direction).")
        if not self.transaction_type: errors['transaction_type'] = _("Transaction type (Debit/Credit) is required.")
        if self.is_reconciled and not self.reconciliation_date: self.reconciliation_date = timezone.now()
        if self.is_reconciled and not self.reconciled_by_id:
            errors['reconciled_by'] = _("Reconciled By user is required if marked reconciled.")
        elif not self.is_reconciled and (self.reconciled_by_id or self.reconciliation_date):
            errors['is_reconciled'] = _("Must be marked 'Is Reconciled' if reconciliation date/user is set.")

        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        # Ensure company and bank_account are set from the upload_source before TenantScopedModel's save (which calls full_clean)
        if self.upload_source_id and (not self.company_id or not self.bank_account_id):
            if not hasattr(self, '_upload_source_cache') or not self._upload_source_cache:
                try:
                    self.upload_source = BankStatementUpload.objects.select_related('company', 'bank_account').get(
                        pk=self.upload_source_id)
                except BankStatementUpload.DoesNotExist:
                    raise ValueError("Cannot save BankStatementTransaction: Upload Source not found.")

            if self.upload_source.company_id: self.company_id = self.upload_source.company_id
            if self.upload_source.bank_account_id: self.bank_account_id = self.upload_source.bank_account_id

        if not self.company_id: raise IntegrityError(
            "BankStatementTransaction cannot be saved without a company context derived from its upload source.")
        if not self.bank_account_id: raise IntegrityError(
            "BankStatementTransaction cannot be saved without a bank account derived from its upload source.")

        super().save(*args, **kwargs)  # TenantScopedModel.save() will call full_clean()


# =============================================================================
# BankReconciliation Model
# =============================================================================
class BankReconciliation(TenantScopedModel):  # Company from TenantScopedModel
    """
    Represents a completed or in-progress bank reconciliation effort for a
    specific bank account and statement period.
    """

    class ReconciliationStatus(models.TextChoices):
        IN_PROGRESS = 'IN_PROGRESS', _('In Progress')
        RECONCILED = 'RECONCILED', _('Reconciled')
        # PENDING_REVIEW = 'PENDING_REVIEW', _('Pending Review') # Optional further status

    bank_account = models.ForeignKey(Account, verbose_name=_("Bank Account"), on_delete=models.PROTECT,
                                     related_name='reconciliations')
    statement_date = models.DateField(_("Statement End Date"), db_index=True,
                                      help_text=_("The end date of the bank statement being reconciled."))
    statement_ending_balance = models.DecimalField(_("Statement Ending Balance"), max_digits=20, decimal_places=2,
                                                   help_text=_("Ending balance as per the bank statement."))

    # --- Calculated values at time of reconciliation ---
    book_balance_before_adjustments = models.DecimalField(_("Book Balance (Before Adjustments)"), max_digits=20,
                                                          decimal_places=2, help_text=_(
            "GL balance of the bank account as of statement date, before reconciliation adjustments."))
    outstanding_payments_total = models.DecimalField(_("Total Outstanding Payments (-)"), max_digits=20,
                                                     decimal_places=2, default=ZERO_DECIMAL, help_text=_(
            "Sum of checks/payments issued but not cleared by bank."))
    deposits_in_transit_total = models.DecimalField(_("Total Deposits in Transit (+)"), max_digits=20, decimal_places=2,
                                                    default=ZERO_DECIMAL,
                                                    help_text=_("Sum of deposits made but not yet reflected by bank."))
    bank_charges_or_interest_total = models.DecimalField(_("Bank Charges/Interest (+/-)"), max_digits=20,
                                                         decimal_places=2, default=ZERO_DECIMAL, help_text=_(
            "Net of bank fees not in GL and interest earned not in GL. Negative for net fees."))
    other_adjustments_total = models.DecimalField(_("Other Adjustments (+/-)"), max_digits=20, decimal_places=2,
                                                  default=ZERO_DECIMAL, help_text=_("Net of other reconciling items."))
    adjusted_book_balance = models.DecimalField(_("Adjusted Book Balance"), max_digits=20, decimal_places=2,
                                                editable=False)  # book_balance - outstanding_payments + deposits_in_transit + bank_charges/interest + other_adjustments
    difference = models.DecimalField(_("Difference (Must be ZERO_DECIMAL)"), max_digits=20, decimal_places=2,
                                     editable=False)  # statement_ending_balance - adjusted_book_balance

    status = models.CharField(_("Reconciliation Status"), max_length=20, choices=ReconciliationStatus.choices,
                              default=ReconciliationStatus.IN_PROGRESS.value, db_index=True)
    reconciled_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Reconciled By"),
                                      on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='bank_reconciliations_done')
    reconciliation_completed_at = models.DateTimeField(_("Reconciliation Completed At"), null=True, blank=True,
                                                       editable=False)
    notes = models.TextField(_("Reconciliation Notes"), blank=True, null=True)

    class Meta:
        verbose_name = _("Bank Reconciliation")
        verbose_name_plural = _("Bank Reconciliations")
        # One reconciliation per bank account per statement end date for a company
        unique_together = (('company', 'bank_account', 'statement_date'),)
        ordering = ['company__name', 'bank_account__account_number', '-statement_date']
        indexes = [models.Index(fields=['company', 'bank_account', 'status', 'statement_date'],
                                name='br_co_bank_stat_sdate_idx')]

    def __str__(self):
        bank_acc_name = self.bank_account.account_name if self.bank_account_id and hasattr(self,
                                                                                           '_bank_account_cache') else \
            (Account.objects.get(pk=self.bank_account_id).account_name if self.bank_account_id else "N/A Bank")
        co_name = self.company.name if self.company_id and hasattr(self, '_company_cache') else \
            (Company.objects.get(pk=self.company_id).name if self.company_id else "N/A Co")
        return f"Reconciliation for {bank_acc_name} ({co_name}) as of {self.statement_date} (Status: {self.get_status_display()})"

    def calculate_adjusted_balances(self, perform_save: bool = False):
        """Calculates adjusted_book_balance and difference. Optionally saves."""
        # Book balance would be fetched by service using ledger_service.calculate_account_balance_upto()
        # and set on instance before calling this, or passed in.
        # For now, assume book_balance_before_adjustments is populated.

        self.adjusted_book_balance = (
                (self.book_balance_before_adjustments or ZERO_DECIMAL) -
                (self.outstanding_payments_total or ZERO_DECIMAL) +
                (self.deposits_in_transit_total or ZERO_DECIMAL) +
                (self.bank_charges_or_interest_total or ZERO_DECIMAL) +
                (self.other_adjustments_total or ZERO_DECIMAL)
        )
        self.difference = (self.statement_ending_balance or ZERO_DECIMAL) - self.adjusted_book_balance

        changed = False
        # Check if significant change before marking as changed (to avoid save if only float precision diff)
        if abs(self.adjusted_book_balance - getattr(self, '_original_adjusted_book_balance',
                                                    self.adjusted_book_balance + 1)) > Decimal('0.001'): changed = True
        if abs(self.difference - getattr(self, '_original_difference', self.difference + 1)) > Decimal(
            '0.001'): changed = True

        if self.status == self.ReconciliationStatus.IN_PROGRESS.value and abs(self.difference) < Decimal(
                '0.01'):  # Threshold for reconciled
            # Optionally auto-set to RECONCILED, or require user to click "Finalize"
            # For now, service layer will handle status transition to RECONCILED.
            pass

        if perform_save and changed:
            self.save(update_fields=['adjusted_book_balance', 'difference', 'updated_at'])

    def clean(self):
        super().clean()
        errors = {}
        # Basic company and bank account validation
        effective_company: Optional[Company] = self.company
        if not effective_company and self.company_id:
            try: effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist: errors['company'] = _("Invalid Company.")
        if not effective_company and not self._state.adding and 'company' not in errors:
            errors['company'] = _("Company association missing.")
        else:
            if self.bank_account_id:
                try:
                    bank_acc = Account.objects.select_related('company').get(pk=self.bank_account_id)
                    if bank_acc.company != effective_company: errors['bank_account'] = _(
                        "Bank Account must belong to Reconciliation's Company.")
                    if bank_acc.account_type != AccountType.ASSET.value: errors['bank_account'] = _(
                        "Account must be Asset type.")
                except Account.DoesNotExist:
                    errors['bank_account'] = _("Bank Account not found.")
            elif not self._state.adding:
                errors['bank_account'] = _("Bank Account is required.")

        if self.status == self.ReconciliationStatus.RECONCILED.value:
            if abs(self.difference or ZERO_DECIMAL) >= Decimal('0.01'):  # Allow for tiny rounding
                errors['status'] = _("Cannot mark as 'Reconciled' when there is a non-ZERO_DECIMAL difference (%(diff)s).") % {
                    'diff': self.difference}
            if not self.reconciliation_completed_at: self.reconciliation_completed_at = timezone.now()
            if not self.reconciled_by_id: errors['reconciled_by'] = _(
                "Reconciled By user is required for 'Reconciled' status.")
        elif self.status == self.ReconciliationStatus.IN_PROGRESS.value:
            # When moving back to IN_PROGRESS, clear completed fields
            self.reconciliation_completed_at = None
            # self.reconciled_by = None # Keep user who last worked on it? Or clear? Business decision.
            pass

        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        # Store original values for comparison in calculate_adjusted_balances if called before save
        self._original_adjusted_book_balance = self.adjusted_book_balance
        self._original_difference = self.difference

        # Recalculate before full_clean if main financial figures are set (usually by service)
        if self.statement_ending_balance is not None and self.book_balance_before_adjustments is not None:
            self.calculate_adjusted_balances(perform_save=False)  # Recalc in memory

        super().save(*args, **kwargs)  # Calls full_clean via TenantScopedModel


# =============================================================================
# ReconciledItemPair Model (Explicit Linking for Reconciliation)
# =============================================================================
class ReconciledItemPair(TenantScopedModel):
    """
    Explicitly links a BankStatementTransaction to a VoucherLine that represents
    the same financial event, as part of a specific BankReconciliation.
    This provides a clear audit trail of what was matched to what.
    The 'company' field is inherited and should match the parent BankReconciliation's company.
    """
    reconciliation = models.ForeignKey(
        'BankReconciliation',  # Use string if BankReconciliation is defined later in the same file
        verbose_name=_("Bank Reconciliation"),
        on_delete=models.CASCADE,
        related_name='item_pairs',
        help_text=_("The bank reconciliation effort this pairing belongs to.")
    )
    bank_transaction = models.OneToOneField(
        BankStatementTransaction,
        verbose_name=_("Bank Statement Transaction"),
        on_delete=models.CASCADE,  # If the bank transaction is deleted, this specific pairing is no longer valid.
        related_name='reconciliation_pair',
        help_text=_("The bank statement transaction being reconciled by this pair.")
    )
    matched_gl_line = models.ForeignKey(
        VoucherLine,
        verbose_name=_("Matched GL Voucher Line"),
        on_delete=models.SET_NULL,  # If the GL line is deleted, the link is broken but pair might be kept for audit.
        null=True, blank=True,
        # Allows bank_tx to be reconciled by creating a NEW adjustment voucher instead of matching an existing line.
        related_name='bank_reconciliation_matches',
        help_text=_(
            "The GL voucher line that this bank transaction was matched against. Can be null if reconciled via a new adjustment.")
    )
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Reconciled By User"),
        on_delete=models.SET_NULL,
        null=True, blank=True  # System or specific user
    )
    reconciled_at = models.DateTimeField(_("Reconciled At"), default=timezone.now)
    notes = models.CharField(_("Matching Notes"), max_length=255, blank=True, null=True)

    class Meta:
        verbose_name = _("Reconciled Item Pair")
        verbose_name_plural = _("Reconciled Item Pairs")
        # A bank transaction can only be paired once within a specific reconciliation.
        unique_together = (('reconciliation', 'bank_transaction'),)
        # A GL line could potentially be part of multiple small bank transactions (less common).
        # If a GL line should only be matched once per reconciliation:
        # unique_together = (('reconciliation', 'bank_transaction'), ('reconciliation', 'matched_gl_line'))
        # However, 'matched_gl_line' can be null, so the second unique_together might need a condition or
        # careful handling if you want to allow multiple pairs for the same (null) matched_gl_line.
        # For now, focusing on unique bank_transaction per reconciliation.
        ordering = ['reconciliation__statement_date', 'reconciliation__bank_account__account_number', '-reconciled_at']
        indexes = [
            models.Index(fields=['company', 'reconciliation', 'bank_transaction'], name='recitem_co_rec_btx_idx'),
            models.Index(fields=['company', 'reconciliation', 'matched_gl_line'], name='recitem_co_rec_glline_idx',
                         condition=models.Q(matched_gl_line__isnull=False)),
        ]

    def __str__(self):
        rec_id = self.reconciliation_id or 'N/A'
        bank_tx_id = self.bank_transaction_id or 'N/A'
        gl_line_id_str = str(self.matched_gl_line_id) if self.matched_gl_line_id else 'N/A (Adjustment)'
        return f"Pair for Rec {rec_id}: BankTx {bank_tx_id} <> GL Line {gl_line_id_str}"

    def clean(self):
        """
        Validates the ReconciledItemPair, ensuring company consistency and valid references.
        """
        # Call super().clean() first. TenantScopedModel might set self.company if it's an add
        # operation and company can be derived from request context (though less likely for this model
        # if created via nested inlines or services).
        super().clean()
        errors: Dict[str, Any] = {}

        # --- Step 1: Determine the effective_company for this ReconciledItemPair ---
        effective_company_instance: Optional[Company] = None

        # Priority 1: If self.company is already set (e.g., by TenantScopedModel or explicitly)
        if self.company_id:
            try:
                effective_company_instance = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID explicitly set on this reconciled pair.")

        # Priority 2: If self.company not set, try to derive from parent self.reconciliation
        elif self.reconciliation_id:
            try:
                parent_reconciliation = BankReconciliation.objects.select_related('company').get(
                    pk=self.reconciliation_id)
                if parent_reconciliation.company:
                    effective_company_instance = parent_reconciliation.company
                    # Assign to self.company if derived, so TenantScopedModel.save() uses it
                    self.company = effective_company_instance
                else:
                    errors['reconciliation'] = _(
                        "Parent Bank Reconciliation record is missing its company association.")
            except BankReconciliation.DoesNotExist:
                errors['reconciliation'] = _("Parent Bank Reconciliation record (ID: %(id)s) not found.") % {
                    'id': self.reconciliation_id}

        # If no company context can be established, it's a critical failure.
        if not effective_company_instance:
            if not self._state.adding and 'company' not in errors:  # Existing record MUST have company
                errors['company'] = _("Company context is critically missing for this reconciled pair.")
            elif self._state.adding and 'company' not in errors:  # New record, context not derived
                errors['company'] = _(
                    "Company context could not be determined for this new reconciled pair. Ensure parent reconciliation is set and has a company.")
            if errors: raise DjangoValidationError(errors)  # Raise immediately if no company context
            return  # Should not happen if above errors are raised

        # --- Step 2: Validate related objects against the effective_company_instance ---
        # Reconciliation (already fetched if company was derived from it)
        if self.reconciliation_id:
            if not hasattr(self,
                           '_reconciliation_cache') or not self._reconciliation_cache or self.reconciliation.pk != self.reconciliation_id:
                try:
                    self.reconciliation = BankReconciliation.objects.get(
                        pk=self.reconciliation_id)  # Load if not already loaded
                except BankReconciliation.DoesNotExist:
                    errors['reconciliation'] = _(
                        "Parent reconciliation record not found.");  # Already checked if derived company from it

            if hasattr(self,
                       '_reconciliation_cache') and self._reconciliation_cache and self._reconciliation_cache.company_id != effective_company_instance.id:
                errors['reconciliation'] = _(
                    "Parent Reconciliation's company (%(rec_co)s) must match the pair's company (%(pair_co)s).") % \
                                           {
                                               'rec_co': self._reconciliation_cache.company.name if self._reconciliation_cache.company else 'N/A',
                                               'pair_co': effective_company_instance.name}
        elif not self._state.adding:  # Reconciliation is mandatory for existing pairs
            errors['reconciliation'] = _("Parent Bank Reconciliation is required for this item pair.")

        # Bank Statement Transaction
        if self.bank_transaction_id:
            try:
                bank_trans_obj = BankStatementTransaction.objects.select_related('company').get(
                    pk=self.bank_transaction_id)
                if bank_trans_obj.company_id != effective_company_instance.id:
                    errors['bank_transaction'] = _(
                        "Bank Statement Transaction's company (%(bt_co)s) must match the pair's company (%(pair_co)s).") % \
                                                 {
                                                     'bt_co': bank_trans_obj.company.name if bank_trans_obj.company else 'N/A',
                                                     'pair_co': effective_company_instance.name}
            except BankStatementTransaction.DoesNotExist:
                errors['bank_transaction'] = _("Associated Bank Statement Transaction (ID: %(id)s) not found.") % {
                    'id': self.bank_transaction_id}
        elif not self._state.adding:  # Mandatory for existing pairs
            errors['bank_transaction'] = _("Bank Statement Transaction is required for this item pair.")

        # Matched GL Voucher Line
        if self.matched_gl_line_id:
            try:
                gl_line_obj = VoucherLine.objects.select_related('voucher__company').get(pk=self.matched_gl_line_id)
                if not gl_line_obj.voucher:
                    errors['matched_gl_line'] = _("Matched GL Line is not associated with a Voucher.")
                elif gl_line_obj.voucher.company_id != effective_company_instance.id:
                    errors['matched_gl_line'] = _(
                        "Matched GL Line's company (via Voucher: %(v_co)s) must match the pair's company (%(pair_co)s).") % \
                                                {
                                                    'v_co': gl_line_obj.voucher.company.name if gl_line_obj.voucher.company else 'N/A',
                                                    'pair_co': effective_company_instance.name}
            except VoucherLine.DoesNotExist:
                errors['matched_gl_line'] = _("Associated Matched GL Line (ID: %(id)s) not found.") % {
                    'id': self.matched_gl_line_id}

        # Business logic: If reconciled, must have a reconciled_by user
        if self.bank_transaction_id and self.bank_transaction.is_reconciled and not self.reconciled_by_id:
            # This validation might be better on BankStatementTransaction itself or handled by service
            pass  # For now, assume service sets reconciled_by when creating pair for reconciled bank_tx

        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Overrides save to ensure company is set from parent reconciliation if not already present.
        This is crucial before TenantScopedModel's save calls full_clean().
        """
        # Ensure company is set, preferably from parent reconciliation for consistency.
        if not self.company_id and self.reconciliation_id:
            try:
                # Efficiently get parent's company_id
                parent_reconciliation_company_id = BankReconciliation.objects.filter(
                    pk=self.reconciliation_id
                ).values_list('company_id', flat=True).first()

                if parent_reconciliation_company_id:
                    self.company_id = parent_reconciliation_company_id
                else:  # Should be caught by clean() if reconciliation_id is invalid
                    logger.error(
                        f"ReconciledItemPair Save: Parent BankReconciliation {self.reconciliation_id} not found or has no company. Cannot set company on pair.")
                    # Depending on strictness, could raise IntegrityError here
            except Exception as e:  # Catch any unexpected issue during lookup
                logger.error(
                    f"ReconciledItemPair Save: Error fetching parent reconciliation company for pair {self.pk or 'New'}: {e}",
                    exc_info=True)
                # Raise or handle, for now, let super().save() catch if company_id remains None and is required

        # If after all attempts self.company_id is still None and it's a new object,
        # TenantScopedModel's save might try to set it from request context IF available (e.g. direct admin add)
        # or raise an error if company is mandatory.
        if not self.company_id and (
                not self._state.adding or not self.pk):  # If existing or somehow new without company
            # This indicates a problem if company is mandatory and couldn't be derived.
            # TenantScopedModel's save will call full_clean, which should catch this based on our clean method.
            pass

        super().save(*args, **kwargs)  # TenantScopedModel's save will call self.full_clean()