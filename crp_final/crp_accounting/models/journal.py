# crp_accounting/models/journal.py

import logging
from decimal import Decimal
from typing import Optional, Any  # Added Any for PK type

from django.conf import settings
# from django.contrib.contenttypes.fields import GenericForeignKey # Uncomment if GFK is used
# from django.contrib.contenttypes.models import ContentType      # Uncomment if GFK is used
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.contrib.auth import get_user_model  # IMPORTED get_user_model

# --- Base Model Import ---
try:
    from .base import TenantScopedModel
except ImportError:
    raise ImportError("Could not import TenantScopedModel from .base. Critical dependency missing.")

# --- Enum/Choice Imports ---
try:
    from crp_core.enums import DrCrType, VoucherType, TransactionStatus, ApprovalActionType
except ImportError:
    raise ImportError("Could not import core enums from 'crp_core'. Critical dependency missing.")

# --- Related Model Imports ---
try:
    from .coa import Account
    from .party import Party
    from .period import AccountingPeriod
    from company.models import Company  # Explicit import
except ImportError as e:
    raise ImportError(f"Could not import related accounting/company models: {e}. Check definitions.")

logger = logging.getLogger("crp_accounting.models.journal")
ZERO_DECIMAL = Decimal('0.00')
User = get_user_model()  #  Get the User model class once


# =============================================================================
# VoucherSequence Model (Tenant Scoped)
# =============================================================================
class VoucherSequence(TenantScopedModel):
    voucher_type = models.CharField(
        _("Voucher Type"), max_length=30, choices=VoucherType.choices, db_index=True,
        help_text=_("The type of voucher this sequence applies to.")
    )
    accounting_period = models.ForeignKey(
        AccountingPeriod, verbose_name=_("Accounting Period"), on_delete=models.CASCADE,
        db_index=True, help_text=_("The accounting period this sequence is for (must belong to the same company).")
    )
    prefix = models.CharField(
        _("Prefix"), max_length=30, blank=True,
        help_text=_("Optional prefix for generated voucher numbers (e.g., 'JV-CO-24Q1-').")
    )
    padding_digits = models.PositiveSmallIntegerField(
        _("Number Padding Digits"), default=4, validators=[MinValueValidator(1)],
        help_text=_("Total digits for the numeric part, including leading zeros (e.g., 4 for '0001'). Minimum 1.")
    )
    last_number = models.PositiveIntegerField(
        _("Last Number Used"), default=0,
        help_text=_("The last sequential number issued for this specific sequence configuration.")
    )

    class Meta:
        verbose_name = _("Voucher Sequence Configuration")
        verbose_name_plural = _("Voucher Sequence Configurations")
        unique_together = (('company', 'voucher_type', 'accounting_period'),)
        ordering = ['company__name', 'accounting_period__start_date', 'voucher_type']
        indexes = [
            models.Index(fields=['company', 'voucher_type', 'accounting_period'], name='vouchseq_co_type_period_idx')]

    def __str__(self):
        vt_display = self.get_voucher_type_display()
        period_name, co_name = _('N/A Period'), _('N/A Co.')
        if self.accounting_period_id:
            try:
                # Prefer direct access if object is already loaded (e.g. by select_related)
                period_name = (self.accounting_period.name if hasattr(self,
                                                                      '_accounting_period_cache') and self._accounting_period_cache
                               else AccountingPeriod.objects.get(pk=self.accounting_period_id).name)
            except ObjectDoesNotExist:
                period_name = f"Period ID {self.accounting_period_id} (Not Found)"
        if self.company_id:
            try:
                co_name = (self.company.name if hasattr(self, '_company_cache') and self._company_cache
                           else Company.objects.get(pk=self.company_id).name)
            except ObjectDoesNotExist:
                co_name = f"Co ID {self.company_id} (Not Found)"
        return f"{vt_display} Sequence for '{period_name}' (Co: {co_name})"

    def format_number(self, number: int) -> str:
        padding = int(self.padding_digits) if self.padding_digits is not None and self.padding_digits >= 1 else 1
        num_str = str(max(0, number)).zfill(padding)
        return f"{self.prefix}{num_str}"

    def clean(self):
        super().clean()
        errors = {}
        effective_sequence_company: Optional[Company] = None
        if self.company_id:
            try:
                effective_sequence_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID for sequence.")
        elif self.accounting_period_id:
            try:
                period = AccountingPeriod.objects.select_related('company').get(pk=self.accounting_period_id)
                if period.company:
                    effective_sequence_company = period.company
                    if not self.company_id: self.company = effective_sequence_company
                else:
                    errors['accounting_period'] = _("Selected Accounting Period has no company.")
            except AccountingPeriod.DoesNotExist:
                errors['accounting_period'] = _("Selected Accounting Period not found.")

        if not effective_sequence_company and 'company' not in errors:
            errors['company'] = _("Company context is missing for this sequence.")

        if not self.accounting_period_id:
            errors['accounting_period'] = _("Accounting Period is required.")
        elif effective_sequence_company:
            try:
                period_to_check = AccountingPeriod.objects.select_related('company').get(pk=self.accounting_period_id)
                if period_to_check.company != effective_sequence_company:
                    errors['accounting_period'] = _(
                        "Accounting Period (Co: %(ap_co)s) must belong to sequence's Company (Co: %(seq_co)s).") % \
                                                  {'ap_co': period_to_check.company.name,
                                                   'seq_co': effective_sequence_company.name}
            except AccountingPeriod.DoesNotExist:
                if 'accounting_period' not in errors: errors['accounting_period'] = _(
                    "Selected Accounting Period not found for company check.")

        if self.padding_digits is not None and self.padding_digits < 1:
            errors['padding_digits'] = _("Padding digits must be at least 1.")
        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        if not kwargs.pop('skip_clean', False): self.full_clean()
        super().save(*args, **kwargs)


# =============================================================================
# Core Voucher Model (Tenant Scoped)
# =============================================================================
class Voucher(TenantScopedModel):
    date = models.DateField(_("Transaction Date"), default=timezone.now, db_index=True)
    effective_date = models.DateField(_("Effective Date"), null=True, blank=True, db_index=True,
                                      help_text=_("Defaults to transaction date."))
    voucher_number = models.CharField(_("Voucher Number"), max_length=50, db_index=True, editable=False, blank=True,
                                      null=True)
    reference = models.CharField(_("Reference"), max_length=100, blank=True, null=True)
    narration = models.TextField(_("Narration"))
    voucher_type = models.CharField(_("Voucher Type"), max_length=30, choices=VoucherType.choices,
                                    default=VoucherType.GENERAL.value, db_index=True)
    status = models.CharField(_("Status"), max_length=20, choices=TransactionStatus.choices,
                              default=TransactionStatus.DRAFT.value, db_index=True)
    party = models.ForeignKey(Party, verbose_name=_("Party"), on_delete=models.PROTECT, null=True, blank=True,
                              db_index=True, related_name="vouchers", help_text=_("Must belong to the same company."))
    accounting_period = models.ForeignKey(AccountingPeriod, verbose_name=_("Accounting Period"),
                                          on_delete=models.PROTECT, db_index=True, related_name="vouchers",
                                          help_text=_("Must belong to the same company."))
    created_by = models.ForeignKey(User, verbose_name=_("Created By"), on_delete=models.SET_NULL,
                                   related_name='created_vouchers', null=True, blank=True, editable=False)  # ✅ User
    updated_by = models.ForeignKey(User, verbose_name=_("Last Updated By"), on_delete=models.SET_NULL,
                                   related_name='updated_vouchers', null=True, blank=True, editable=False)  # ✅ User
    approved_by = models.ForeignKey(User, verbose_name=_("Approved By"), on_delete=models.SET_NULL,
                                    related_name='approved_vouchers', null=True, blank=True, editable=False)  # ✅ User
    approved_at = models.DateTimeField(_("Approved At"), null=True, blank=True, editable=False)
    posted_by = models.ForeignKey(User, verbose_name=_("Posted By"), on_delete=models.SET_NULL,
                                  related_name='posted_vouchers', null=True, blank=True, editable=False)  # ✅ User
    posted_at = models.DateTimeField(_("Posted At"), null=True, blank=True, editable=False)
    is_reversal_for = models.OneToOneField('self', verbose_name=_("Is Reversal For"), on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name='reversed_by_voucher')
    is_reversed = models.BooleanField(_("Is Reversed"), default=False, editable=False, db_index=True)
    balances_updated = models.BooleanField(_("Balances Updated Flag"), default=False, db_index=True, editable=False,
                                           help_text=_("Internal flag for task idempotency."))

    class Meta:  # Meta for Voucher
        verbose_name = _("Voucher")
        verbose_name_plural = _("Vouchers")
        unique_together = (('company', 'voucher_number'),)
        ordering = ['company__name', '-date', '-created_at']
        indexes = [
            models.Index(fields=['company', 'status', 'accounting_period'], name='vouch_co_stat_period_idx'),
            models.Index(fields=['company', 'date', 'voucher_type'], name='voucher_co_date_type_idx'),
            models.Index(fields=['company', 'party'], name='voucher_co_party_idx'),
            models.Index(fields=['is_reversal_for'], name='voucher_is_reversal_idx',
                         condition=Q(is_reversal_for__isnull=False)),
            models.Index(fields=['company', 'is_reversed', 'status'], name='voucher_co_rev_stat_idx'),
            models.Index(fields=['company', 'balances_updated', 'status'], name='voucher_co_balupd_stat_idx'),
        ]
        permissions = [("submit_voucher", "Can submit voucher"), ("approve_voucher", "Can approve voucher"),
                       ("reject_voucher", "Can reject voucher"), ("post_voucher", "Can post voucher"),
                       ("create_reversal_voucher", "Can create reversal voucher"),
                       ("delete_draft_voucher", "Can delete draft voucher"),
                       ("view_all_company_vouchers", "Can view all company vouchers"), ]

    @property
    def total_debit(self) -> Decimal:
        if hasattr(self, '_prefetched_lines_total_debit'): return self._prefetched_lines_total_debit
        if not self.pk: return ZERO_DECIMAL
        return self.lines.filter(dr_cr=DrCrType.DEBIT.value).aggregate(total=Sum('amount', default=ZERO_DECIMAL))[
            'total']

    @property
    def total_credit(self) -> Decimal:
        if hasattr(self, '_prefetched_lines_total_credit'): return self._prefetched_lines_total_credit
        if not self.pk: return ZERO_DECIMAL
        return self.lines.filter(dr_cr=DrCrType.CREDIT.value).aggregate(total=Sum('amount', default=ZERO_DECIMAL))[
            'total']

    @property
    def is_balanced(self) -> bool:
        if not self.pk or not self.lines.exists():
            return True if self.status == TransactionStatus.DRAFT.value else False
        return abs(self.total_debit - self.total_credit) < Decimal('0.005')

    @property
    def is_editable(self) -> bool:
        return self.status in [TransactionStatus.DRAFT.value, TransactionStatus.REJECTED.value]

    def __str__(self):
        co_name = _("N/A Co.")
        if self.company_id:
            try:
                co_name = (self.company.name if hasattr(self, '_company_cache') and self._company_cache
                           else Company.objects.get(pk=self.company_id).name)
            except ObjectDoesNotExist:
                co_name = f"Co ID {self.company_id} (Not Found)"
        v_num = self.voucher_number or (_('Draft (PK:%(pk)s)') % {'pk': (self.pk or "New")})
        dt = self.date or _("No Date")
        return f"{v_num} - {dt} ({co_name})"

    def clean(self):
        super().clean()
        errors = {}
        if self.date and not self.effective_date: self.effective_date = self.date

        voucher_effective_company: Optional[Company] = None
        if self.company_id:
            try:
                voucher_effective_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID (%(id)s) for voucher.") % {'id': self.company_id}
        elif self.accounting_period_id:
            try:
                period = AccountingPeriod.objects.select_related('company').get(pk=self.accounting_period_id)
                if period.company:
                    voucher_effective_company = period.company
                    if not self.company_id: self.company = voucher_effective_company
                else:
                    errors['accounting_period'] = _("Selected Accounting Period has no company.")
            except AccountingPeriod.DoesNotExist:
                errors['accounting_period'] = _("Invalid Accounting Period selected.")

        if not voucher_effective_company:
            if not self._state.adding: errors['company'] = _("Voucher's company association is missing.")
            if self.narration and 'company' not in errors: errors['company'] = _(
                "Company must be selected if narration provided.")
            if self.date and 'company' not in errors: errors['company'] = _(
                "Company must be selected if date provided.")
            if errors: raise DjangoValidationError(errors)
            return

        if self.accounting_period_id:
            try:
                period = AccountingPeriod.objects.select_related('company').get(pk=self.accounting_period_id)
                if period.company != voucher_effective_company:
                    errors['accounting_period'] = _(
                        "Period (Co: %(ap_co)s) must belong to Voucher's Co (Co: %(v_co)s).") % \
                                                  {'ap_co': period.company.name, 'v_co': voucher_effective_company.name}
                elif self.date:
                    if period.locked and (self._state.adding or getattr(Voucher.objects.filter(pk=self.pk).first(),
                                                                        'accounting_period_id', None) != period.pk):
                        errors['accounting_period'] = _("Period '%(name)s' is locked.") % {'name': period.name}
                    if not (period.start_date <= self.date <= period.end_date):
                        errors['date'] = _("Date %(v_date)s outside period '%(p_name)s' (%(p_s)s-%(p_e)s).") % \
                                         {'v_date': self.date, 'p_name': period.name, 'p_s': period.start_date,
                                          'p_e': period.end_date}
            except AccountingPeriod.DoesNotExist:
                if 'accounting_period' not in errors: errors['accounting_period'] = _("Selected Period not found.")
        elif self.date:
            errors['accounting_period'] = _("Period required if Date set.")

        if self.party_id:
            try:
                party = Party.objects.select_related('company').get(pk=self.party_id)
                if party.company != voucher_effective_company:
                    errors['party'] = _("Party not of Voucher's Company.")
                elif not party.is_active:
                    errors['party'] = _("Party inactive.")
            except Party.DoesNotExist:
                errors['party'] = _("Party not found.")

        if self.is_reversal_for_id:
            try:
                original = Voucher.objects.select_related('company').get(pk=self.is_reversal_for_id)
                if original.pk == self.pk and self.pk:
                    errors['is_reversal_for'] = _("Cannot reverse self.")
                elif original.company != voucher_effective_company:
                    errors['is_reversal_for'] = _("Original not of Voucher's Company.")
                elif original.status != TransactionStatus.POSTED.value:
                    errors['is_reversal_for'] = _("Can only reverse Posted vouchers.")
                elif original.is_reversed and (
                        self._state.adding or getattr(original.reversed_by_voucher, 'pk', None) != self.pk):
                    errors['is_reversal_for'] = _("Original '%(num)s' already reversed.") % {
                        'num': original.voucher_number or original.pk}
            except Voucher.DoesNotExist:
                errors['is_reversal_for'] = _("Original voucher to reverse not found.")

        if self.voucher_number and self.voucher_number.strip():
            qs = Voucher.objects.filter(company=voucher_effective_company, voucher_number=self.voucher_number)
            if self.pk: qs = qs.exclude(pk=self.pk)
            if qs.exists(): errors['voucher_number'] = _("Voucher number in use for your company.")

        if errors: raise DjangoValidationError(errors)


# =============================================================================
# VoucherApproval Log Model (Tenant Scoped)
# =============================================================================
class VoucherApproval(TenantScopedModel):
    voucher = models.ForeignKey(Voucher, verbose_name=_("Voucher"), related_name='approvals', on_delete=models.CASCADE)
    user = models.ForeignKey(User, verbose_name=_("User"), on_delete=models.PROTECT,
                             related_name='voucher_approval_actions')  # User
    action_timestamp = models.DateTimeField(_("Action Timestamp"), default=timezone.now, editable=False)
    action_type = models.CharField(_("Action Type"), max_length=20, choices=ApprovalActionType.choices, db_index=True)
    from_status = models.CharField(_("From Status"), max_length=20, choices=TransactionStatus.choices, null=True,
                                   blank=True)
    to_status = models.CharField(_("To Status"), max_length=20, choices=TransactionStatus.choices, null=True,
                                 blank=True)
    comments = models.TextField(_("Comments"), blank=True)

    class Meta:  # Meta for VoucherApproval
        verbose_name = _("Voucher Approval Log")
        verbose_name_plural = _("Voucher Approval Logs")
        ordering = ['company__name', 'voucher__date', '-action_timestamp']
        indexes = [
            models.Index(fields=['company', 'voucher', 'action_timestamp'], name='vouchappr_co_vch_ts_idx'),
            models.Index(fields=['company', 'user', 'action_timestamp'], name='vouchappr_co_user_ts_idx'),
        ]

    def __str__(self):
        user_str, vch_str = _('System Action'), f"Vch PK:{self.voucher_id or 'N/A'}"
        if self.user_id:
            try:
                #  Use User (which is get_user_model() result)
                user_instance = (self.user if hasattr(self, '_user_cache') and self._user_cache
                                 else User.objects.get(pk=self.user_id))
                user_str = user_instance.get_full_name() or user_instance.username
            except ObjectDoesNotExist:
                user_str = f"User ID {self.user_id} (Not Found)"
        if self.voucher_id:
            try:
                vch_obj = (self.voucher if hasattr(self, '_voucher_cache') and self._voucher_cache
                           else Voucher.objects.get(pk=self.voucher_id))
                vch_str = vch_obj.voucher_number or f"Vch PK:{self.voucher_id}"
            except ObjectDoesNotExist:
                pass  # Default vch_str is fine
        ts_str = self.action_timestamp.strftime('%Y-%m-%d %H:%M') if self.action_timestamp else 'N/A'
        return f"{vch_str}: {self.get_action_type_display()} by {user_str} at {ts_str}"

    def clean(self):
        super().clean()
        errors = {}
        if not self.voucher_id: errors['voucher'] = _("Voucher required for approval log.")

        effective_log_company: Optional[Company] = None
        if self.company_id:
            try:
                effective_log_company = Company.objects.get(pk=self.company_id)
            except Company.DoesNotExist:
                errors['company'] = _("Invalid Company ID for log.")
        elif self.voucher_id:
            try:
                voucher_parent = Voucher.objects.select_related('company').get(pk=self.voucher_id)
                if voucher_parent.company:
                    effective_log_company = voucher_parent.company
                    if not self.company_id: self.company = effective_log_company
                else:
                    errors['voucher'] = _("Parent Voucher has no company.")
            except Voucher.DoesNotExist:
                errors['voucher'] = _("Associated Voucher not found.")

        if not effective_log_company and 'company' not in errors: errors['company'] = _(
            "Company context missing for log.")

        if effective_log_company and self.voucher_id:
            try:
                voucher_to_check = Voucher.objects.select_related('company').get(pk=self.voucher_id)
                if voucher_to_check.company != effective_log_company:
                    errors['voucher'] = _("Voucher's Co (%(vch_co)s) must match Log's Co (%(log_co)s).") % \
                                        {'vch_co': voucher_to_check.company.name, 'log_co': effective_log_company.name}
            except Voucher.DoesNotExist:
                if 'voucher' not in errors: errors['voucher'] = _("Associated Voucher not found for company check.")
        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        if not kwargs.pop('skip_clean', False): self.full_clean()
        super().save(*args, **kwargs)


# =============================================================================
# Voucher Line Model (Implicitly Tenant Scoped via Voucher)
# =============================================================================
class VoucherLine(models.Model):
    voucher = models.ForeignKey(Voucher, verbose_name=_("Voucher"), on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, verbose_name=_("Account"), on_delete=models.PROTECT,
                                related_name='voucher_lines', db_index=True,
                                help_text=_("Must belong to the same company as the voucher."))
    dr_cr = models.CharField(_("Dr/Cr"), max_length=6, choices=DrCrType.choices)
    amount = models.DecimalField(_("Amount"), max_digits=20, decimal_places=2,
                                 validators=[MinValueValidator(Decimal('0.000001'))])
    narration = models.TextField(_("Line Narration"), blank=True)
    created_at = models.DateTimeField(_("Line Created At"), auto_now_add=True, editable=False)

    class Meta:  # Meta for VoucherLine
        verbose_name = _("Voucher Line")
        verbose_name_plural = _("Voucher Lines")
        ordering = ['pk']
        indexes = [
            models.Index(fields=['voucher', 'account'], name='vouchline_vouch_acct_idx'),
            models.Index(fields=['voucher', 'dr_cr'], name='vouchline_vouch_drcr_idx'),
            models.Index(fields=['account', 'voucher'], name='vouchline_acct_vouch_id_idx'),
        ]

    def __str__(self):
        acc_str, vch_pk_str = _("N/A Acct"), _("N/A")
        if self.account_id:
            try:
                acc_obj = (self.account if hasattr(self, '_account_cache') and self._account_cache
                           else Account.objects.get(pk=self.account_id))
                acc_str = acc_obj.account_number
            except ObjectDoesNotExist:
                acc_str = f"Acct ID {self.account_id} (Not Found)"
        if self.voucher_id: vch_pk_str = str(self.voucher_id)
        amt_str = str(self.amount) if self.amount is not None else _("N/A")
        drcr_str = self.get_dr_cr_display() if self.dr_cr else _("N/A")
        return f"{drcr_str} {acc_str} - {amt_str} (Vch PK: {vch_pk_str})"

    def clean(self):
        super().clean()
        errors = {}
        if self.amount is not None and self.amount <= ZERO_DECIMAL: errors['amount'] = _(
            "Line amount must be positive.")
        if not self.dr_cr: errors['dr_cr'] = _("Debit/Credit indicator is required.")
        if not self.account_id: errors['account'] = _("An Account must be selected.")

        if errors: raise DjangoValidationError(errors)

        if not self.voucher_id:
            logger.debug(f"VoucherLine Clean: voucher_id not set (Acc ID: {self.account_id}). Skipping company check.")
            return

        try:
            parent_voucher = Voucher.objects.select_related('company').get(pk=self.voucher_id)
            line_account = Account.objects.select_related('company').get(pk=self.account_id)

            if not parent_voucher.company: errors['voucher'] = _("Parent Voucher's company missing.")
            if not line_account.company: errors['account'] = _("Selected Account's company missing.")

            if parent_voucher.company and line_account.company and parent_voucher.company != line_account.company:
                errors['account'] = _("Account (Co: %(acc_co)s) must belong to Voucher's Co (Co: %(vch_co)s).") % \
                                    {'acc_co': line_account.company.name, 'vch_co': parent_voucher.company.name}
        except Voucher.DoesNotExist:
            errors['voucher'] = _("Associated Voucher for line not found.")
        except Account.DoesNotExist:
            errors['account'] = _("Selected Account for line not found.")
        except Exception as e:
            logger.error(f"VoucherLine Clean Error (VchID: {self.voucher_id}, AccID: {self.account_id}): {e}",
                         exc_info=True)
            errors['__all__'] = _("Unexpected error validating line associations.")

        if errors: raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        if not kwargs.pop('skip_clean', False): self.full_clean()
        super().save(*args, **kwargs)
        logger.debug(f"VoucherLine {self.pk} for Voucher {self.voucher_id} saved.")