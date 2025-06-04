# crp_accounting/serializers/journal.py

import logging
from datetime import date
from decimal import Decimal
from typing import Dict, Optional, List

from rest_framework import serializers
from django.db import transaction  # Keep for create/update methods
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.models import ContentType  # Keep if GFK is used
from django.core.exceptions import ValidationError as DjangoValidationError, \
    ObjectDoesNotExist  # Keep for specific catches

# --- Model Imports (Assume tenant-scoped) ---
from ..models.journal import Voucher, VoucherLine, TransactionStatus, DrCrType, VoucherType
from ..models.coa import Account
from ..models.party import Party
from ..models.period import AccountingPeriod
from company.models import Company  # Import Company for type checking and explicit use

# --- Enum/Constant Imports ---
from crp_core.enums import PartyType  # Assuming PartyType enum imported

logger = logging.getLogger("crp_accounting.serializers.journal")  # More specific logger name


# =============================================================================
# Voucher Line Serializer
# =============================================================================
class VoucherLineSerializer(serializers.ModelSerializer):
    account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.all(),  # ViewSet MUST override this with a company-filtered queryset
        help_text=_("PK of the Account (must be active, postable, and belong to the voucher's company).")
    )
    # For read operations, these provide convenient display
    # Ensure your Account model has 'account_number' and 'account_name'
    account_number = serializers.CharField(source='account.account_number', read_only=True, allow_null=True)
    account_name = serializers.CharField(source='account.account_name', read_only=True, allow_null=True)

    class Meta:
        model = VoucherLine
        fields = [
            'id',
            'account',  # Writable: Expects PK of an Account
            'account_number',  # Read-only
            'account_name',  # Read-only
            'dr_cr',
            'amount',
            'narration',
        ]
        read_only_fields = ['id']

    def validate_amount(self, value: Decimal) -> Decimal:
        if value is None:  # Should be caught by field's allow_null=False if applicable
            raise serializers.ValidationError(_("Amount cannot be null."))
        if value <= Decimal('0.00'):  # Allow exactly 0.00 if that's a business rule, else >
            raise serializers.ValidationError(_("Amount must be a positive value greater than zero."))
        return value

    def validate_dr_cr(self, value: str) -> str:
        if not value:  # Should be caught by field's allow_blank=False if applicable
            raise serializers.ValidationError(_("Debit/Credit type (dr_cr) is required."))
        # Optional: Further validate against DrCrType.choices if not handled by ChoiceField
        # if value not in [choice[0] for choice in DrCrType.choices]:
        #     raise serializers.ValidationError(_("Invalid Debit/Credit type selected."))
        return value

    def validate_account(self, account: Account) -> Account:
        """
        Validates the selected account. Assumes 'company' is in self.context.
        The queryset for this field should already be filtered by company in the ViewSet.
        This validation provides an additional layer of security/integrity.
        """
        # company_from_voucher_context is the company of the Voucher being created/updated.
        company_from_voucher_context = self.context.get(
            'company_from_voucher_context')  # Expect this from VoucherSerializer

        if not account.is_active:
            raise serializers.ValidationError(
                _("Selected account '%(name)s' (%(number)s) is inactive.") %
                {'name': account.account_name, 'number': account.account_number}
            )
        if not account.allow_direct_posting:
            raise serializers.ValidationError(
                _("Direct posting is not allowed to account '%(name)s' (%(number)s).") %
                {'name': account.account_name, 'number': account.account_number}
            )

        # Company Check: Ensure the account's company matches the voucher's company.
        if company_from_voucher_context:
            if account.company_id != company_from_voucher_context.id:  # Compare IDs for efficiency
                logger.warning(
                    f"VoucherLine Validation: Account {account.pk} (Co: {account.company.name if account.company else account.company_id}) "
                    f"does not match expected Voucher's Company {company_from_voucher_context.name} (Co PK: {company_from_voucher_context.id})."
                )
                raise serializers.ValidationError(
                    _("Selected account '%(acc_name)s' belongs to Company '%(acc_co)s', but the voucher is for Company '%(vch_co)s'.") %
                    {'acc_name': account.account_name, 'acc_co': account.company.name if account.company else 'N/A',
                     'vch_co': company_from_voucher_context.name}
                )
        else:
            # This indicates a programming error if context isn't passed correctly.
            # For a line, company context should always be derived from its parent voucher.
            logger.error(
                "VoucherLineSerializer.validate_account: 'company_from_voucher_context' missing from context. Cannot perform full validation.")
            # Depending on strictness, you might raise an error here.
            # However, the ViewSet's queryset filtering for 'account' field is the primary defense.

        return account


# =============================================================================
# Main Voucher Serializer
# =============================================================================
class VoucherSerializer(serializers.ModelSerializer):
    lines = VoucherLineSerializer(many=True, min_length=2)  # Require at least 2 lines for balance

    # Read-Only / Display Fields (calculated by model properties or service)
    voucher_number = serializers.CharField(read_only=True, allow_null=True, required=False)
    status = serializers.ChoiceField(choices=TransactionStatus.choices, read_only=True,
                                     required=False)  # Status is managed by service
    status_display = serializers.CharField(source='get_status_display', read_only=True, required=False)
    total_debit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True, required=False)
    total_credit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True, required=False)
    is_balanced = serializers.BooleanField(read_only=True, required=False)
    is_editable = serializers.BooleanField(read_only=True, required=False)  # From model property
    created_at = serializers.DateTimeField(read_only=True, format="%Y-%m-%d %H:%M:%S", required=False)
    updated_at = serializers.DateTimeField(read_only=True, format="%Y-%m-%d %H:%M:%S", required=False)
    # created_by_display = serializers.CharField(source='created_by.get_full_name', read_only=True, allow_null=True, required=False) # Example

    # Writeable Relations (Querysets for these MUST be filtered by company in the ViewSet using this serializer)
    party = serializers.PrimaryKeyRelatedField(
        queryset=Party.objects.all(),  # ViewSet MUST override
        allow_null=True, required=False,
        help_text=_("PK of the associated Party (must be active and belong to the voucher's company).")
    )
    accounting_period = serializers.PrimaryKeyRelatedField(
        queryset=AccountingPeriod.objects.all(),  # ViewSet MUST override
        help_text=_("PK of the Accounting Period (must be open/unlocked and belong to the voucher's company).")
    )

    # Generic Foreign Key for source_document (if used)
    # content_type = serializers.PrimaryKeyRelatedField(queryset=ContentType.objects.all(), allow_null=True, required=False)
    # object_id = serializers.CharField(allow_null=True, required=False, max_length=36) # Assuming UUID or int PK as char

    class Meta:
        model = Voucher
        fields = [
            'id', 'voucher_number',  # voucher_number is read-only but good to include
            'date', 'effective_date', 'reference', 'narration', 'voucher_type',
            'party', 'accounting_period',
            'status', 'status_display',  # status is read-only, managed by service
            # 'content_type', 'object_id', # Uncomment if using GFK
            'lines',
            # Read-only totals and flags
            'total_debit', 'total_credit', 'is_balanced', 'is_editable',
            'created_at', 'updated_at',  # 'created_by_display', # Example
            # Fields managed by system/service or TenantScopedModel:
            # 'company', 'created_by', 'updated_by', 'approved_by', 'approved_at',
            # 'posted_by', 'posted_at', 'is_reversal_for', 'is_reversed', 'balances_updated'
        ]
        # read_only_fields are fields that can NEVER be written via this serializer,
        # even if not explicitly listed in `fields` but present on the model.
        read_only_fields = [
            'id', 'voucher_number', 'status', 'status_display',
            'total_debit', 'total_credit', 'is_balanced', 'is_editable',
            'created_at', 'updated_at'
            # 'created_by_display',
            # Add system-managed FKs here if they should never be set by API user
            # 'company', 'created_by', 'updated_by', 'approved_by', 'posted_by'
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Retrieve the company context passed by the ViewSet
        # This company is the one the new/updated Voucher will belong to.
        self.company_from_voucher_context = self.context.get(
            'company_from_voucher_context')  # Ensure ViewSet passes this
        if not self.company_from_voucher_context and (self.instance is None or not self.instance.company_id):
            # This could happen if context not passed for a new voucher.
            # For updates, instance.company should be reliable.
            logger.warning(
                "VoucherSerializer __init__: 'company_from_voucher_context' not found in context. Validation of related objects might be incomplete if creating new.")
        elif self.instance and self.instance.company_id:
            # For updates, ensure company_from_voucher_context matches instance's company (or set it if missing)
            if not self.company_from_voucher_context:
                self.company_from_voucher_context = self.instance.company
            elif self.company_from_voucher_context.id != self.instance.company_id:
                logger.error(
                    f"VoucherSerializer __init__: Context company {self.company_from_voucher_context.id} MISMATCHES instance company {self.instance.company_id} for Voucher {self.instance.pk}. This is a critical issue.")
                # This situation should ideally be caught by the ViewSet logic.
                # Consider raising an error or falling back to instance.company.
                # For now, let's trust instance.company if there's a mismatch for an existing voucher.
                self.company_from_voucher_context = self.instance.company

        # Pass the determined company context down to the nested VoucherLineSerializer
        # This is crucial for VoucherLineSerializer.validate_account
        self.fields['lines'].context['company_from_voucher_context'] = self.company_from_voucher_context

        # If updating an existing voucher, make fields read-only if not editable
        if self.instance and not self.instance.is_editable:
            immutable_fields = [
                'date', 'effective_date', 'reference', 'narration', 'voucher_type',
                'party', 'accounting_period',
                # 'content_type', 'object_id' # If GFK used
            ]
            for field_name in immutable_fields:
                if field_name in self.fields:
                    self.fields[field_name].read_only = True
            self.fields['lines'].read_only = True  # Prevent line modifications for non-editable vouchers

    def validate_accounting_period(self, period: AccountingPeriod) -> AccountingPeriod:
        if not self.company_from_voucher_context:
            logger.warning(
                "VoucherSerializer.validate_accounting_period: Company context missing. Skipping company match validation.")
            # Cannot validate company match without context.
            # The ViewSet's queryset filtering for this field is the primary defense.
            # Model's clean() will be the final gatekeeper.
            return period

        if period.company_id != self.company_from_voucher_context.id:
            raise serializers.ValidationError(
                _("Selected Accounting Period (Co: %(period_co)s) does not belong to the Voucher's Company (Co: %(voucher_co)s).") %
                {'period_co': period.company.name if period.company else 'N/A',
                 'voucher_co': self.company_from_voucher_context.name}
            )

        # Check period lock only when creating a new voucher through API.
        # Model's clean() method is more robust for handling updates to existing vouchers
        # concerning locked periods (e.g., allowing minor edits vs. period changes).
        if self.instance is None and period.locked:  # self.instance is None for create
            raise serializers.ValidationError(
                _("Cannot create voucher in locked Accounting Period '%(period_name)s'.") %
                {'period_name': str(period)}, code='period_locked'
            )
        return period

    def validate_party(self, party: Optional[Party]) -> Optional[Party]:
        if party and self.company_from_voucher_context:  # Only validate if party and context exist
            if party.company_id != self.company_from_voucher_context.id:
                raise serializers.ValidationError(
                    _("Selected Party (Co: %(party_co)s) does not belong to the Voucher's Company (Co: %(voucher_co)s).") %
                    {'party_co': party.company.name if party.company else 'N/A',
                     'voucher_co': self.company_from_voucher_context.name}
                )
            if not party.is_active:
                raise serializers.ValidationError(_("Selected Party '%(name)s' is inactive.") % {'name': party.name})
        return party

    def validate_date(self, value: date) -> date:
        if value is None:  # Should be caught by ModelField null=False
            raise serializers.ValidationError(_("Transaction Date is required."))
        # Optional: Add future/past date restrictions if needed by business logic
        # if value > timezone.now().date():
        #     raise serializers.ValidationError(_("Transaction Date cannot be in the future."))
        return value

    def validate(self, data: Dict) -> Dict:
        """
        Object-level validation.
        - Voucher line balance.
        - Date within accounting period.
        - Party type vs Voucher type.
        """
        # Effective data for validation (combining instance data with new data for updates)
        # `data` here contains validated data from individual field validations.
        # `self.instance` is the existing model instance if updating.

        # --- Company Context ---
        # company_from_voucher_context is already set in self.__init__
        # If it's still None here for a 'create' operation, it's a problem with ViewSet context passing.
        if not self.company_from_voucher_context and self.instance is None:
            logger.error(
                "VoucherSerializer.validate: 'company_from_voucher_context' is missing in context for a new voucher. Cannot perform full validation.")
            raise serializers.ValidationError("Internal Server Error: Company context missing for voucher validation.")

        # Determine the effective company for validation.
        # For 'create', it's company_from_voucher_context.
        # For 'update', it's self.instance.company (which should match company_from_voucher_context).
        effective_company = self.company_from_voucher_context if self.instance is None else self.instance.company
        if not effective_company:  # Should not happen if logic above is correct
            raise serializers.ValidationError(
                "Internal Error: Effective company could not be determined for validation.")

        # --- Immutability Check (double-check, __init__ should handle UI) ---
        if self.instance and not self.instance.is_editable:
            # Check if any fields that *should* be immutable were changed.
            # `data` contains only fields present in the request payload.
            # `self.initial_data` contains the raw request payload.
            changed_immutable_fields = []
            immutable_header_fields = ['date', 'effective_date', 'reference', 'narration', 'voucher_type', 'party',
                                       'accounting_period']
            for field_name in immutable_header_fields:
                if field_name in self.initial_data and field_name in self.fields and not self.fields[
                    field_name].read_only:
                    # This means a writable field (according to current state) received data,
                    # but __init__ should have made it read_only if instance.is_editable is False.
                    # This check is more about data tampering if __init__ logic was bypassed.
                    changed_immutable_fields.append(field_name)

            if changed_immutable_fields:
                logger.warning(
                    f"Attempt to update immutable fields {changed_immutable_fields} for locked Voucher {self.instance.pk} (Co: {effective_company.name}).")
                raise serializers.ValidationError(
                    _("Cannot update core fields of a non-editable voucher (Status: '%(status)s').") %
                    {'status': self.instance.get_status_display()}, code='voucher_locked_fields'
                )
            if 'lines' in self.initial_data and not self.fields[
                'lines'].read_only:  # lines submitted for non-editable voucher
                logger.warning(
                    f"Attempt to update lines for locked Voucher {self.instance.pk} (Co: {effective_company.name}).")
                raise serializers.ValidationError(
                    _("Cannot update lines for a non-editable voucher (Status: '%(status)s').") %
                    {'status': self.instance.get_status_display()}, code='voucher_locked_lines'
                )

        # --- Line Validations (balance, count) ---
        # `self.initial_data.get('lines')` is used because `data.get('lines')` would be the *validated* lines data,
        # but we need to validate based on what was submitted.
        submitted_lines_payload = self.initial_data.get('lines')

        if submitted_lines_payload is not None:  # Lines were part of the submission
            if not isinstance(submitted_lines_payload, list):
                raise serializers.ValidationError({"lines": _("Lines data must be a list.")})
            if len(submitted_lines_payload) < 2:
                raise serializers.ValidationError({"lines": _("Voucher requires at least two lines for balancing.")})
            self._validate_lines_balance_from_payload(submitted_lines_payload)
        elif self.instance is None:  # Creating a new voucher, lines are mandatory
            raise serializers.ValidationError({"lines": _("Voucher lines are required for new vouchers.")})
        # If updating and no 'lines' in payload, existing lines are kept (or handled by partial update logic).

        # --- Date within Accounting Period ---
        # `data` contains validated instances for FKs like accounting_period, or new values for date fields.
        voucher_date = data.get('date', getattr(self.instance, 'date', None))
        accounting_period_instance = data.get('accounting_period', getattr(self.instance, 'accounting_period', None))

        if voucher_date and accounting_period_instance:
            # Ensure accounting_period_instance is for the correct company
            if accounting_period_instance.company_id != effective_company.id:
                raise serializers.ValidationError(
                    {'accounting_period': _("Accounting Period does not belong to the voucher's company.")})

            if not (accounting_period_instance.start_date <= voucher_date <= accounting_period_instance.end_date):
                raise serializers.ValidationError({
                    'date': _(
                        "Voucher date %(v_date)s is outside the selected period '%(p_name)s' (%(p_start)s - %(p_end)s).") %
                            {'v_date': voucher_date, 'p_name': str(accounting_period_instance),
                             'p_start': accounting_period_instance.start_date,
                             'p_end': accounting_period_instance.end_date}
                })
        elif voucher_date and not accounting_period_instance:  # Date set but no period
            raise serializers.ValidationError(
                {'accounting_period': _("Accounting Period is required if Transaction Date is set.")})

        # --- Party Type vs Voucher Type ---
        voucher_type_value = data.get('voucher_type', getattr(self.instance, 'voucher_type', None))
        party_instance_for_check = data.get('party',
                                            getattr(self.instance, 'party', None))  # This is the Party instance

        if party_instance_for_check and party_instance_for_check.company_id != effective_company.id:
            raise serializers.ValidationError({'party': _("Selected Party does not belong to the voucher's company.")})

        self._validate_party_type_vs_voucher_type(voucher_type_value, party_instance_for_check)

        # The model's clean() method will perform final, comprehensive validations including uniqueness.
        return data

    def _validate_lines_balance_from_payload(self, lines_payload: List[Dict]):
        """Validates balance from the raw lines payload."""
        total_debit = sum(Decimal(line.get('amount', '0') or '0') for line in lines_payload if
                          line.get('dr_cr') == DrCrType.DEBIT.value)
        total_credit = sum(Decimal(line.get('amount', '0') or '0') for line in lines_payload if
                           line.get('dr_cr') == DrCrType.CREDIT.value)

        if total_debit.quantize(Decimal("0.01")) != total_credit.quantize(Decimal("0.01")):
            raise serializers.ValidationError(
                {"lines_balance": _("Submitted lines do not balance. Debits: %(dr)s, Credits: %(cr)s") %
                                  {'dr': total_debit, 'cr': total_credit}}, code='unbalanced_lines'
            )
        if total_debit <= Decimal('0.00'):  # Assuming total amount must be positive
            raise serializers.ValidationError({"lines_total": _("Voucher total amount must be greater than zero.")},
                                              code='zero_total_amount')

    def _validate_party_type_vs_voucher_type(self, voucher_type_value: Optional[str], party_instance: Optional[Party]):
        if not voucher_type_value or not party_instance:  # If either is not set, no specific rule applies here
            # If a voucher_type *requires* a party, that should be a field-level `required=True`
            # or a check in `validate()` that party_instance is not None for those types.
            return

        party_type_value = party_instance.party_type

        required_party_map = {
            VoucherType.PURCHASE.value: PartyType.SUPPLIER.value,
            VoucherType.PAYMENT.value: PartyType.SUPPLIER.value,
            VoucherType.SALES.value: PartyType.CUSTOMER.value,
            VoucherType.RECEIPT.value: PartyType.CUSTOMER.value,
        }
        # Contra vouchers should not have a party.
        if voucher_type_value == VoucherType.CONTRA.value:
            raise serializers.ValidationError({'party': _("Contra vouchers should not have an associated party.")})

        expected_party_type = required_party_map.get(voucher_type_value)
        if expected_party_type and party_type_value != expected_party_type:
            # Get display names for better error messages
            current_party_type_display = party_instance.get_party_type_display()
            # Find the label for the expected_party_type
            expected_party_type_label = "Unknown Type"
            for val, label in PartyType.choices:
                if val == expected_party_type:
                    expected_party_type_label = str(label)  # Use str() for lazy proxy
                    break
            raise serializers.ValidationError({
                'party': _(
                    "Voucher type '%(vch_type)s' requires a Party of type '%(exp_party)s', but Party '%(party_name)s' is type '%(curr_party)s'.") %
                         {'vch_type': VoucherType(voucher_type_value).label, 'exp_party': expected_party_type_label,
                          'party_name': party_instance.name, 'curr_party': current_party_type_display}
            })

    @transaction.atomic
    def create(self, validated_data: Dict) -> Voucher:
        """
        Creates a Voucher header and its lines.
        The 'company' for the Voucher is expected to be set by the ViewSet
        in perform_create: serializer.save(company=self.request.company)
        """
        # Retrieve the company from context (set in __init__, passed by ViewSet)
        # This company is what the new Voucher will belong to.
        voucher_company = self.company_from_voucher_context
        if not voucher_company:
            # This should ideally be caught earlier (e.g., in ViewSet or serializer __init__)
            raise serializers.ValidationError("Critical: Company context not available for creating voucher.")

        # `self.initial_data` contains the raw submitted payload.
        lines_payload = self.initial_data.get('lines', [])
        if not lines_payload:  # Double check, validate() should catch this
            raise serializers.ValidationError({"lines": _("Cannot create voucher without lines payload.")})

        # Separate lines data; it's not a direct field on Voucher model.
        # `validated_data` here contains only header fields.
        validated_data.pop('lines', None)  # Ensure 'lines' isn't passed to Voucher.objects.create

        # Explicitly set the company for the new Voucher.
        validated_data['company'] = voucher_company
        # `created_by` could also be set here if user is in context:
        # validated_data['created_by'] = self.context['request'].user

        # Create the Voucher header instance
        voucher = Voucher.objects.create(**validated_data)
        logger.info(f"Voucher header {voucher.pk} created for Company {voucher_company.name}.")

        # Create VoucherLine instances
        # The VoucherLineSerializer's context already includes `company_from_voucher_context`
        line_serializer = VoucherLineSerializer(data=lines_payload, many=True, context=self.context)
        if line_serializer.is_valid(raise_exception=True):
            line_serializer.save(voucher=voucher)  # Associate lines with the newly created voucher
            logger.info(f"Saved {len(line_serializer.instance)} lines for Voucher {voucher.pk}.")

        # It's good practice to refresh_from_db after related objects are created/modified,
        # especially if model properties depend on them (like total_debit).
        voucher.refresh_from_db()
        return voucher

    @transaction.atomic
    def update(self, instance: Voucher, validated_data: Dict) -> Voucher:
        """
        Updates a Voucher header and manages its lines (create, update, delete).
        The 'company' of the Voucher instance is not changed.
        """
        # `company_from_voucher_context` is set in __init__ and should match `instance.company`
        if self.company_from_voucher_context and instance.company_id != self.company_from_voucher_context.id:
            logger.error(f"CRITICAL: Attempt to update Voucher {instance.pk} with mismatched company context! "
                         f"Instance Co: {instance.company_id}, Context Co: {self.company_from_voucher_context.id}")
            raise serializers.ValidationError("Company context mismatch during voucher update. Operation aborted.")

        # `self.initial_data` contains the raw submitted payload.
        lines_payload = self.initial_data.get('lines')  # May be None if lines not being updated

        # Remove 'lines' from validated_data as it's not a direct field to update on Voucher model
        validated_data.pop('lines', None)

        # Update header fields on the instance
        # `updated_by` could be set here: instance.updated_by = self.context['request'].user
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Save header changes first. Model's full_clean is called by super().save() if TenantScopedModel does it.
        # Or call instance.full_clean() here before save if base model doesn't.
        instance.save()
        logger.info(f"Voucher header {instance.pk} (Co: {instance.company.name}) updated.")

        # Handle Line Updates only if 'lines' was part of the submitted payload
        if lines_payload is not None:
            existing_lines_map = {line.id: line for line in instance.lines.all()}
            submitted_line_ids_with_pk = set()

            for line_data in lines_payload:
                line_id = line_data.get('id')
                # Context is already set on self.fields['lines'] which includes company_from_voucher_context
                line_serializer_context = self.fields['lines'].context

                if line_id:  # Potential update or an ID for a line that doesn't exist
                    line_instance = existing_lines_map.get(line_id)
                    if line_instance:  # Update existing line
                        line_serializer = VoucherLineSerializer(line_instance, data=line_data, partial=True,
                                                                context=line_serializer_context)
                        submitted_line_ids_with_pk.add(line_id)
                    else:  # ID provided but not found for this voucher - treat as error or new if allowed
                        logger.warning(
                            f"Line ID {line_id} provided in payload for Voucher {instance.pk} but not found among existing lines. Skipping.")
                        # If you want to allow creation with provided ID (not typical for DB-generated PKs):
                        # line_serializer = VoucherLineSerializer(data=line_data, context=line_serializer_context)
                        continue
                else:  # No ID, create new line
                    line_serializer = VoucherLineSerializer(data=line_data, context=line_serializer_context)

                if line_serializer.is_valid(raise_exception=True):
                    line_serializer.save(voucher=instance)  # Ensure voucher FK is set for new/updated lines

            # Delete lines that were existing but not in the submitted payload's IDs
            ids_to_delete = set(existing_lines_map.keys()) - submitted_line_ids_with_pk
            if ids_to_delete:
                logger.info(
                    f"Deleting VoucherLines with IDs {ids_to_delete} for Voucher {instance.pk} (Co: {instance.company.name})")
                VoucherLine.objects.filter(voucher=instance, id__in=ids_to_delete).delete()

        instance.refresh_from_db()  # Get latest state including line changes
        return instance