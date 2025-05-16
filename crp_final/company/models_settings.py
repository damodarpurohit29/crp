# company/models_settings.py
from typing import Optional

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist

# --- NO direct import of Company from .models for FK definition ---

ACCOUNT_MODEL_PATH = 'crp_accounting.Account'  # For ForeignKeys to Account model


class CompanyAccountingSettings(models.Model):
    company = models.OneToOneField(
        'company.Company',  # <<<< STRING REFERENCE
        on_delete=models.CASCADE,
        primary_key=True,
        related_name='accounting_settings',
        verbose_name=_("Company")
    )

    default_retained_earnings_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Retained Earnings Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Equity account for net income. Must be 'Equity' type.")
    )
    default_accounts_receivable_control = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default A/R Control Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Primary A/R control. Asset type, control for Customers.")
    )
    default_sales_revenue_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Sales Revenue Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Default revenue for sales. Income type.")
    )
    default_sales_tax_payable_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Sales Tax Payable Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Sales tax accrual. Liability type.")
    )
    default_unapplied_customer_cash_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Unapplied Customer Cash Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Liability for unapplied customer payments.")
    )
    default_accounts_payable_control = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default A/P Control Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Primary A/P control. Liability type, control for Suppliers.")
    )
    default_purchase_expense_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Purchase/Expense Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Default expense/asset for purchases. Expense or Asset type.")
    )
    default_purchase_tax_asset_account = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Purchase Tax Asset Account"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Asset for recoverable purchase tax.")
    )
    default_bank_account_for_payments_made = models.ForeignKey(
        ACCOUNT_MODEL_PATH, verbose_name=_("Default Bank Account for Payments Made"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text=_("Default bank/cash for AP payments.")
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Last Updated By"),
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='updated_company_accounting_settings'
    )

    def __str__(self):
        # self.company will be resolved by Django when needed
        try:
            return _("Accounting Settings for %(company_name)s") % {'company_name': self.company.name}
        except (AttributeError, ObjectDoesNotExist):  # Catch if company not loaded or link is broken
            return _("Accounting Settings (Company ID: %(company_id)s)") % {'company_id': self.company_id or 'N/A'}

    def clean(self):
        super().clean()
        # Local imports are fine inside methods
        from crp_accounting.models.coa import Account
        from crp_core.enums import AccountType, PartyType

        errors = {}
        # Ensure self.company is resolved for validation, self.company_id should be set.
        # This clean method assumes self.company_id (and thus self.company by relation) is valid.
        # The OneToOneField(primary_key=True) ensures company_id is set on instance creation.

        if not self.company_id:  # Should not happen with primary_key=True on OneToOneField
            errors['company'] = _("Company association is missing.")
            raise DjangoValidationError(errors)

        # Helper to validate an account field
        def validate_account_field(field_name: str, expected_account_type: str,
                                   expected_party_type_for_control: Optional[str] = None,
                                   is_control_flag_expected: Optional[bool] = None):
            account_instance = getattr(self, field_name)
            if account_instance:
                # Fetch the related company for the account for comparison
                # This assumes Account model has a 'company' FK.
                if account_instance.company_id != self.company_id:
                    errors[field_name] = _(
                        "Selected account (%(acc_name)s) must belong to the same company as these settings ('%(co_name)s').") % {
                                             'acc_name': account_instance, 'co_name': self.company.name}
                elif account_instance.account_type != expected_account_type:
                    errors[field_name] = _("Account '%(acc_name)s' must be of type '%(type)s'.") % {
                        'acc_name': account_instance, 'type': AccountType(expected_account_type).label}

                if is_control_flag_expected is not None and account_instance.is_control_account != is_control_flag_expected:
                    errors[field_name] = _(
                        "Account '%(acc_name)s' 'is_control_account' flag is not set as expected (Expected: %(exp)s).") % {
                                             'acc_name': account_instance, 'exp': is_control_flag_expected}

                if expected_party_type_for_control and account_instance.control_account_party_type != expected_party_type_for_control:
                    errors[field_name] = _(
                        "Control account '%(acc_name)s' is not configured for the correct party type (Expected: %(exp_pt)s, Got: %(got_pt)s).") % {
                                             'acc_name': account_instance,
                                             'exp_pt': PartyType(expected_party_type_for_control).label,
                                             'got_pt': account_instance.get_control_account_party_type_display()
                                         }

        # Validate each account field
        if self.default_retained_earnings_account: validate_account_field('default_retained_earnings_account',
                                                                          AccountType.EQUITY.value)
        if self.default_accounts_receivable_control: validate_account_field('default_accounts_receivable_control',
                                                                            AccountType.ASSET.value,
                                                                            PartyType.CUSTOMER.value, True)
        if self.default_sales_revenue_account: validate_account_field('default_sales_revenue_account',
                                                                      AccountType.INCOME.value)
        if self.default_sales_tax_payable_account: validate_account_field('default_sales_tax_payable_account',
                                                                          AccountType.LIABILITY.value)
        if self.default_unapplied_customer_cash_account: validate_account_field(
            'default_unapplied_customer_cash_account', AccountType.LIABILITY.value)
        if self.default_accounts_payable_control: validate_account_field('default_accounts_payable_control',
                                                                         AccountType.LIABILITY.value,
                                                                         PartyType.SUPPLIER.value, True)
        if self.default_purchase_expense_account:
            acc = self.default_purchase_expense_account
            if acc.company_id != self.company_id:
                errors['default_purchase_expense_account'] = _("Account must belong to company.")
            elif acc.account_type not in [AccountType.EXPENSE.value, AccountType.ASSET.value]:
                errors['default_purchase_expense_account'] = _("Account must be 'Expense' or 'Asset'.")
        if self.default_purchase_tax_asset_account: validate_account_field('default_purchase_tax_asset_account',
                                                                           AccountType.ASSET.value)
        if self.default_bank_account_for_payments_made: validate_account_field('default_bank_account_for_payments_made',
                                                                               AccountType.ASSET.value)

        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = _("Company Accounting Settings")
        verbose_name_plural = _("Company Accounting Settings")