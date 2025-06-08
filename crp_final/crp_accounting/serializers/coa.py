# crp_accounting/serializers/coa.py
import logging
from rest_framework import serializers
from django.utils.translation import gettext_lazy as _
from decimal import Decimal  # Not directly used here but good for context

# --- Model Imports ---
from ..models.coa import Account, AccountGroup, PLSection

# --- Enum Imports ---
from crp_core.enums import AccountType, AccountNature, CurrencyType, PartyType

logger = logging.getLogger(__name__)


# =============================================================================
# Helper/Summary Serializers
# =============================================================================
class AccountGroupSummarySerializer(serializers.ModelSerializer):
    """Minimal representation of AccountGroup for nesting."""
    full_path = serializers.CharField(source='get_full_path', read_only=True)

    class Meta:
        model = AccountGroup
        fields = ('id', 'name', 'full_path')
        read_only_fields = fields  # All fields here are read-only by nature of summary


class AccountSummarySerializer(serializers.ModelSerializer):
    """Minimal representation of Account for nesting or summaries."""
    # Uses Django's get_<field_name>_display() for choice fields
    account_type_display = serializers.CharField(source='get_account_type_display', read_only=True)

    class Meta:
        model = Account
        fields = ('id', 'account_number', 'account_name', 'is_active', 'account_type', 'account_type_display')
        read_only_fields = fields


# =============================================================================
# AccountGroup Serializers
# =============================================================================
class AccountGroupReadSerializer(serializers.ModelSerializer):
    """Serializer for *reading* AccountGroup data."""
    parent_group = AccountGroupSummarySerializer(read_only=True)
    full_path = serializers.CharField(source='get_full_path', read_only=True)

    # Assuming account_count is annotated by the ViewSet or not strictly needed here
    # If you need it and it's not annotated, consider a SerializerMethodField or ensure annotation.
    # account_count = serializers.IntegerField(source='accounts.count', read_only=True) # Example

    class Meta:
        model = AccountGroup
        fields = (
            'id', 'name', 'description', 'parent_group', 'full_path',  # 'account_count',
            'created_at', 'updated_at',
            'deleted'  # Assuming 'deleted' is the soft-delete field from TenantScopedModel
        )
        read_only_fields = fields


class AccountGroupWriteSerializer(serializers.ModelSerializer):
    """
    Serializer for *creating/updating* AccountGroup data.
    Dynamically filters 'parent_group_id' queryset based on company context.
    """
    parent_group_id = serializers.PrimaryKeyRelatedField(
        queryset=AccountGroup.objects.none(),  # Start with none; populated by __init__ if context
        source='parent_group',
        allow_null=True,
        required=False,
        help_text=_("ID of the parent Account Group. Must belong to your company.")
    )

    class Meta:
        model = AccountGroup
        fields = ('id', 'name', 'description', 'parent_group_id')
        read_only_fields = ('id',)
        # 'company' field is set by the ViewSet via context.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        company_from_context = self.context.get('company_context')
        parent_group_field = self.fields.get('parent_group_id')

        if parent_group_field:  # Ensure field exists
            if company_from_context:
                # Real request with company context: filter the queryset.
                logger.debug(
                    f"AccountGroupWriteSerializer.__init__: Filtering parent_group_id for Co ID {company_from_context.id}")
                parent_queryset = AccountGroup.objects.filter(company=company_from_context)
                if self.instance:  # If updating, exclude self from choices
                    parent_queryset = parent_queryset.exclude(pk=self.instance.pk)
                parent_group_field.queryset = parent_queryset
            else:
                # No company_context (likely schema generation). Queryset remains AccountGroup.objects.none().
                # logger.debug(
                #    "AccountGroupWriteSerializer.__init__: No company_context. "
                #    "'parent_group_id' queryset remains AccountGroup.objects.none() (expected for schema generation)."
                # )
                pass

    def validate_name(self, value_name: str):
        """Ensure name is unique within the company context."""
        company = self.context.get('company_context')
        if not company:
            # Likely schema generation, skip company-scoped check. DB/model clean will catch it for real requests.
            # logger.debug("AccountGroupWriteSerializer.validate_name: company_context is None (expected for schema generation).")
            return value_name

        # Model's default manager (CompanyManager) should be tenant-scoped.
        queryset = AccountGroup.objects.filter(company=company, name=value_name)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                _("An Account Group with this name ('%(name)s') already exists in your company.") % {'name': value_name}
            )
        return value_name

    def validate_parent_group_id(self, parent_group_instance: AccountGroup):
        """Validate the selected parent group instance."""
        if parent_group_instance is None:
            return None  # Allow null if field has allow_null=True

        company_from_context = self.context.get('company_context')
        if not company_from_context:
            # This validation might be too strict for schema generation if it always requires context.
            # However, if an ID is provided, it should ideally be valid against some context.
            # For real requests, CompanyContextMixin should provide the context.
            logger.error("AccountGroupWriteSerializer.validate_parent_group_id: company_context is None.")
            raise serializers.ValidationError(
                _("Internal Error: Company context not available for parent group validation."))

        if parent_group_instance.company_id != company_from_context.id:
            raise serializers.ValidationError(
                _("Parent Account Group ('%(pg_name)s') must belong to your current company.") %
                {'pg_name': parent_group_instance.name}
            )

        # Model's clean() method handles deeper cycle detection for parent_group.
        # Serializer can check for immediate self-parenting if desired.
        if self.instance and self.instance.pk == parent_group_instance.pk:
            raise serializers.ValidationError(_("An account group cannot be its own parent."))

        return parent_group_instance

    def create(self, validated_data):
        company = self.context.get('company_context')
        if not company:
            logger.error("AccountGroupWriteSerializer.create: 'company_context' missing.")
            raise serializers.ValidationError(_("Internal Error: Company context is required for creation."))
        validated_data['company'] = company
        # Model's save() method (via TenantScopedModel) calls full_clean()
        return super().create(validated_data)

    def update(self, instance: AccountGroup, validated_data):
        company_from_context = self.context.get('company_context')
        if not company_from_context or instance.company_id != company_from_context.id:
            # This implies an attempt to update an object not belonging to the current user's company context
            # or context is missing. The ViewSet's get_object() should prevent this.
            logger.error(
                f"AccountGroupWriteSerializer.update: Company mismatch or missing context. Instance Co ID: {instance.company_id}, Context Co: {company_from_context.id if company_from_context else 'None'}")
            raise serializers.ValidationError(_("Operation not allowed due to company mismatch or missing context."))

        validated_data.pop('company', None)  # Ensure company cannot be changed via payload
        return super().update(instance, validated_data)


# =============================================================================
# Account Serializers
# =============================================================================
class AccountReadSerializer(serializers.ModelSerializer):
    """Serializer for *reading* Account data."""
    account_group = AccountGroupSummarySerializer(read_only=True)
    account_nature_display = serializers.CharField(source='get_account_nature_display', read_only=True)
    account_type_display = serializers.CharField(source='get_account_type_display', read_only=True)
    pl_section_display = serializers.CharField(source='get_pl_section_display', read_only=True)
    currency_display = serializers.CharField(source='get_currency_display', read_only=True)
    control_account_party_type_display = serializers.CharField(
        source='get_control_account_party_type_display', read_only=True, allow_null=True
    )

    class Meta:
        model = Account
        fields = (
            'id', 'account_number', 'account_name', 'description',
            'account_group', 'account_type', 'account_type_display',
            'account_nature', 'account_nature_display',
            'pl_section', 'pl_section_display',
            'currency', 'currency_display',
            'is_active', 'allow_direct_posting',
            'is_control_account', 'control_account_party_type', 'control_account_party_type_display',
            'current_balance', 'balance_last_updated',
            'created_at', 'updated_at',
            'deleted'  # Assuming 'deleted' is the soft-delete field
        )
        read_only_fields = fields


class AccountWriteSerializer(serializers.ModelSerializer):
    """
    Serializer for *creating/updating* Account data.
    Dynamically filters 'account_group_id' queryset based on company context.
    """
    account_group_id = serializers.PrimaryKeyRelatedField(
        queryset=AccountGroup.objects.none(),  # Start with none; populated by __init__ if context
        source='account_group',
        help_text=_("ID of the parent Account Group. Must belong to your company.")
    )
    account_type = serializers.ChoiceField(choices=AccountType.choices)
    pl_section = serializers.ChoiceField(
        choices=PLSection.choices, required=False, allow_blank=True,
        default=PLSection.NONE.value  # Ensure default matches model if not blank
    )
    currency = serializers.ChoiceField(
        choices=CurrencyType.choices, required=False, allow_blank=True
        # Default currency is handled by the Account model's _set_derived_fields method
    )
    control_account_party_type = serializers.ChoiceField(
        choices=PartyType.choices, allow_null=True, required=False
    )

    class Meta:
        model = Account
        fields = (
            'id', 'account_number', 'account_name', 'description',
            'account_group_id', 'account_type', 'pl_section', 'currency',
            'is_active', 'allow_direct_posting',
            'is_control_account', 'control_account_party_type',
        )
        read_only_fields = ('id',)
        # 'company', 'account_nature', 'current_balance', 'balance_last_updated' are excluded or readonly.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        company_from_context = self.context.get('company_context')
        account_group_field = self.fields.get('account_group_id')

        if account_group_field:  # Ensure field exists
            if company_from_context:
                logger.debug(
                    f"AccountWriteSerializer.__init__: Filtering account_group_id for Co ID {company_from_context.id}")
                account_group_field.queryset = AccountGroup.objects.filter(company=company_from_context)
            else:
                # logger.debug(
                #    "AccountWriteSerializer.__init__: No company_context. "
                #    "'account_group_id' queryset remains AccountGroup.objects.none() (expected for schema generation)."
                # )
                pass

    def validate_account_number(self, value_acc_num: str):
        company = self.context.get('company_context')
        if not company:
            # logger.debug("AccountWriteSerializer.validate_account_number: company_context is None (expected for schema generation).")
            return value_acc_num

        queryset = Account.objects.filter(company=company, account_number=value_acc_num)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                _("An Account with this number ('%(number)s') already exists in your company.") % {
                    'number': value_acc_num}
            )
        return value_acc_num

    def validate_account_group_id(self, account_group_instance: AccountGroup):
        if account_group_instance is None:  # PrimaryKeyRelatedField handles this if allow_null=False
            raise serializers.ValidationError(_("Account Group is required."))  # Should not be hit if field is required

        company_from_context = self.context.get('company_context')
        if not company_from_context:
            logger.error("AccountWriteSerializer.validate_account_group_id: company_context is None.")
            raise serializers.ValidationError(_("Internal Error: Company context not available for validation."))

        if account_group_instance.company_id != company_from_context.id:
            raise serializers.ValidationError(
                _("Account Group ('%(ag_name)s') must belong to your current company.") %
                {'ag_name': account_group_instance.name}
            )
        return account_group_instance

    def validate(self, data):
        """
        Object-level validation. Relies on model's full_clean() for ultimate integrity.
        """
        # Get values, falling back to instance values if not in `data` (for PATCH)
        acc_type = data.get('account_type', getattr(self.instance, 'account_type', None))
        pl_section_val = data.get('pl_section', getattr(self.instance, 'pl_section', PLSection.NONE.value))
        is_control = data.get('is_control_account', getattr(self.instance, 'is_control_account', False))
        party_type = data.get('control_account_party_type', getattr(self.instance, 'control_account_party_type', None))

        # P&L Section validation based on Account Type
        is_pl_account_type = acc_type in [
            AccountType.INCOME.value, AccountType.EXPENSE.value, AccountType.COST_OF_GOODS_SOLD.value
        ]
        if is_pl_account_type and (not pl_section_val or pl_section_val == PLSection.NONE.value):
            raise serializers.ValidationError(
                {'pl_section': _("P&L Section is required for Income, Expense, or COGS account types.")}
            )
        if not is_pl_account_type and pl_section_val and pl_section_val != PLSection.NONE.value:
            raise serializers.ValidationError(
                {'pl_section': _("P&L Section must be 'Not Applicable' for Asset, Liability, or Equity account types.")}
            )

        # If P&L section was not provided for a non-P&L account type, ensure it defaults correctly.
        # The model's default for pl_section is PLSection.NONE.value, so this should be fine.
        # If 'pl_section' is not in data and it's a non-P&L account, DRF will use existing instance value or model default.

        # Control Account validation
        if is_control and not party_type:
            raise serializers.ValidationError(
                {'control_account_party_type': _("Control accounts must specify a Party Type.")}
            )
        if not is_control and party_type:
            raise serializers.ValidationError(
                {'control_account_party_type': _("Party Type can only be set on Control Accounts.")}
            )

        # Model's `clean()` method (called by `full_clean()` during save) handles:
        # - Setting `account_nature`.
        # - Setting default `currency` from company.
        # - Validating `account_group.company` matches `account.company`.
        return data

    def create(self, validated_data):
        company = self.context.get('company_context')
        if not company:
            logger.error("AccountWriteSerializer.create: 'company_context' missing.")
            raise serializers.ValidationError(_("Internal Error: Company context is required for creation."))
        validated_data['company'] = company
        return super().create(validated_data)

    def update(self, instance: Account, validated_data):
        company_from_context = self.context.get('company_context')
        if not company_from_context or instance.company_id != company_from_context.id:
            logger.error(
                f"AccountWriteSerializer.update: Company mismatch or missing context. Instance Co ID: {instance.company_id}, Context Co: {company_from_context.id if company_from_context else 'None'}")
            raise serializers.ValidationError(_("Operation not allowed due to company mismatch or missing context."))

        validated_data.pop('company', None)
        return super().update(instance, validated_data)


# =============================================================================
# Ledger Serializers
# =============================================================================
class AccountLedgerEntrySerializer(serializers.Serializer):
    """Serializer for a single line item in an Account Ledger report."""
    # Fields should match the dictionary keys returned by your ledger_service
    line_pk = serializers.UUIDField(read_only=True)  # Assuming VoucherLine PK is UUID
    date = serializers.DateField(read_only=True)
    voucher_pk = serializers.UUIDField(read_only=True)  # Assuming Voucher PK is UUID
    voucher_number = serializers.CharField(read_only=True, allow_null=True)
    voucher_type = serializers.CharField(read_only=True)  # Display name of voucher type
    narration = serializers.CharField(read_only=True, allow_blank=True)
    reference = serializers.CharField(read_only=True, allow_blank=True, allow_null=True)
    debit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    credit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    running_balance = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)


class AccountLedgerResponseSerializer(serializers.Serializer):
    """Serializer for the overall response of the Account Ledger endpoint."""
    account = AccountSummarySerializer(read_only=True)  # Use the summary for account details
    start_date = serializers.DateField(required=False, allow_null=True, read_only=True)
    end_date = serializers.DateField(required=False, allow_null=True, read_only=True)
    opening_balance = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    total_debit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    total_credit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    closing_balance = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    entries = AccountLedgerEntrySerializer(many=True, read_only=True)  # List of ledger entries