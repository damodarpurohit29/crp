# crp_accounting/serializers/period.py

import logging
from rest_framework import serializers
from django.utils.translation import gettext_lazy as _

# --- Model Imports ---
from ..models.period import FiscalYear, AccountingPeriod

# --- Enum/Constants Import (CRITICAL) ---
# Assuming FiscalYearStatus is an enum defined in your models.period or a shared enums file.
# If it's a simple choices tuple on the model, you might need to access it differently
# or define constants here. For robustness, an Enum is preferred.
try:
    # Example: from ..models.enums import FiscalYearStatus
    # Or from ..models.period import FiscalYearStatus (if defined in period.py models)
    # For now, using the placeholder:
    class FiscalYearStatus:
        OPEN = "Open"
        LOCKED = "Locked"
        CLOSED = "Closed"
        # Ensure these values EXACTLY match what's stored in your FiscalYear.status field
except ImportError:
    # Fallback if the import path is different or enum not yet created
    logging.warning("serializers.period: FiscalYearStatus enum/constants not found. Using placeholders.")


    class FiscalYearStatus:
        OPEN = "Open"
        LOCKED = "Locked"
        CLOSED = "Closed"

logger = logging.getLogger(__name__)


# =============================================================================
# FiscalYear Serializers
# =============================================================================

class FiscalYearReadSerializer(serializers.ModelSerializer):
    """Serializer for *reading* FiscalYear data."""
    closed_by_email = serializers.EmailField(source='closed_by.email', read_only=True, allow_null=True)
    status_display = serializers.CharField(source='get_status_display',
                                           read_only=True)  # Assumes FiscalYear has this method

    class Meta:
        model = FiscalYear
        fields = [
            'id', 'name', 'start_date', 'end_date',
            'status', 'status_display', 'is_active',
            'closed_by_email', 'closed_at',
            'created_at', 'updated_at',
            'deleted'  # Assuming 'deleted' is the field from django-safedelete
        ]
        read_only_fields = fields


class FiscalYearWriteSerializer(serializers.ModelSerializer):
    """Serializer for *creating/updating* FiscalYear data."""

    class Meta:
        model = FiscalYear
        fields = ['id', 'name', 'start_date', 'end_date']
        read_only_fields = ('id',)
        # 'company' is set by the ViewSet's perform_create via context.
        # 'status', 'is_active', 'closed_by', 'closed_at' are managed by model methods or admin actions.

    def validate_name(self, value):
        company = self.context.get('company_context')
        if not company:
            # This might happen during schema generation.
            # For actual requests, the view's CompanyContextMixin should ensure company exists.
            # If it's critical for validation even at schema time (e.g. if name needs company prefix),
            # this would need to be handled, but usually not.
            # logger.debug("FiscalYearWriteSerializer.validate_name: company_context is None (expected for schema generation).")
            return value  # Skip company-scoped uniqueness check if no company context (e.g. schema generation)

        queryset = FiscalYear.objects.filter(company=company, name=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                _("A Fiscal Year with this name ('%(name)s') already exists in your company.") % {'name': value}
            )
        return value

    def validate(self, data):
        # Get current values or instance values if not provided (for PATCH)
        start_date = data.get('start_date', getattr(self.instance, 'start_date', None))
        end_date = data.get('end_date', getattr(self.instance, 'end_date', None))

        if start_date and end_date and end_date <= start_date:
            raise serializers.ValidationError({"end_date": _("End date must be after start date.")})

        # Complex validations like overlapping fiscal years within the company
        # are best handled by the model's `clean()` method, which is called
        # by `full_clean()` during `serializer.save()`.
        return data

    def create(self, validated_data):
        company = self.context.get('company_context')
        if not company:
            # This should be caught by the View/Mixin for actual requests.
            # If it happens, it's an internal error.
            logger.error("FiscalYearWriteSerializer.create: 'company_context' is missing from serializer context.")
            raise serializers.ValidationError(_("Internal Error: Company context is required for creation."))
        validated_data['company'] = company
        # The model's save method (via TenantScopedModel) will call full_clean()
        return super().create(validated_data)

    def update(self, instance: FiscalYear, validated_data):
        # Compare with the actual value used in your FiscalYear.status field
        if instance.status == FiscalYearStatus.CLOSED:
            raise serializers.ValidationError(
                _("Cannot update a Fiscal Year that is already CLOSED. It must be reopened first."))

        # Prevent changing the company of an existing Fiscal Year
        if 'company' in validated_data and validated_data['company'] != instance.company:
            raise serializers.ValidationError(_("The company assignment of an existing Fiscal Year cannot be changed."))
        validated_data.pop('company', None)  # Ensure 'company' isn't passed to super if present

        return super().update(instance, validated_data)


# =============================================================================
# AccountingPeriod Serializers
# =============================================================================

class AccountingPeriodReadSerializer(serializers.ModelSerializer):
    """Serializer for *reading* AccountingPeriod data."""
    fiscal_year_id = serializers.PrimaryKeyRelatedField(source='fiscal_year', read_only=True)
    fiscal_year_name = serializers.CharField(source='fiscal_year.name', read_only=True)
    lock_status_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AccountingPeriod
        fields = [
            'id', 'fiscal_year_id', 'fiscal_year_name',
            'name', 'start_date', 'end_date', 'locked', 'lock_status_display',
            'created_at', 'updated_at',
            'deleted'  # Assuming 'deleted' is the field from django-safedelete
        ]
        read_only_fields = fields

    def get_lock_status_display(self, obj: AccountingPeriod) -> str:
        return _("Locked") if obj.locked else _("Open")


class AccountingPeriodWriteSerializer(serializers.ModelSerializer):
    """
    Serializer for *creating/updating* AccountingPeriod data.
    Company is set by context. FiscalYear choices are dynamically filtered.
    """
    fiscal_year_id = serializers.PrimaryKeyRelatedField(
        queryset=FiscalYear.objects.none(),  # Start with none; populated by __init__ if context available
        source='fiscal_year',  # This links the 'fiscal_year_id' field to the 'fiscal_year' model attribute
        help_text=_("ID of the parent Fiscal Year. Must belong to your company and not be CLOSED.")
    )

    class Meta:
        model = AccountingPeriod
        fields = ['id', 'fiscal_year_id', 'name', 'start_date', 'end_date']
        read_only_fields = ('id',)
        # 'company' is set by ViewSet's perform_create via context.
        # 'locked' status is managed by model methods/admin actions, not directly via API create/update.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # `self.context` is an empty dict `{}` during DRF Spectacular schema generation if not explicitly passed.
        # So, `get('company_context')` will return None.
        company_from_context = self.context.get('company_context')

        fiscal_year_field = self.fields.get('fiscal_year_id')
        if fiscal_year_field:  # Ensure the field actually exists
            if company_from_context:
                # This is a real request with company context. Filter the queryset.
                logger.debug(
                    f"AccountingPeriodWriteSerializer.__init__: Filtering fiscal_year_id for Company ID {company_from_context.id}")
                fiscal_year_field.queryset = FiscalYear.objects.filter(
                    company=company_from_context
                ).exclude(status=FiscalYearStatus.CLOSED).order_by('-start_date')
            else:
                # No company_context (likely schema generation).
                # The queryset remains FiscalYear.objects.none().
                # This is fine for schema generation as it only needs field type info.
                # The logger message here is for debugging, can be removed in prod.
                # logger.debug(
                #    "AccountingPeriodWriteSerializer.__init__: No company_context. "
                #    "'fiscal_year_id' queryset remains FiscalYear.objects.none() (expected for schema generation)."
                # )
                pass

    def validate_fiscal_year_id(self, fiscal_year_instance: FiscalYear):  # Receives resolved FiscalYear instance
        """Ensure the selected FiscalYear is valid for the current company context."""
        company_from_context = self.context.get('company_context')

        if not company_from_context:
            # This check is more relevant for actual data validation than schema generation.
            # If schema generation reaches here without context, it's unusual but we should allow it to pass
            # if the goal is just to validate field types for the schema.
            # However, for actual data saving, company_context is vital.
            # Assuming this method is called during real request data validation:
            logger.error("AccountingPeriodWriteSerializer.validate_fiscal_year_id: Company context missing.")
            raise serializers.ValidationError(_("Internal Error: Company context not available for validation."))

        if fiscal_year_instance.company_id != company_from_context.id:
            raise serializers.ValidationError(
                _("The selected Fiscal Year ('%(fy_name)s') does not belong to your current company.") %
                {'fy_name': fiscal_year_instance.name}
            )

        # Compare with the actual value used in your FiscalYear.status field
        if fiscal_year_instance.status == FiscalYearStatus.CLOSED:
            raise serializers.ValidationError(
                _("Cannot add or modify periods for a Fiscal Year ('%(fy_name)s') that is already CLOSED.") %
                {'fy_name': fiscal_year_instance.name}
            )
        return fiscal_year_instance

    def validate_name(self, value_name: str):
        """Basic validation for the period name."""
        if not value_name.strip():
            raise serializers.ValidationError(_("Period name cannot be blank."))
        # Uniqueness within the fiscal_year ('fiscal_year', 'name') is best enforced by
        # the model's `unique_together` constraint in `Meta` and `full_clean()`.
        # Trying to perfectly replicate it here is complex due to when fiscal_year instance is available.
        return value_name

    def validate(self, data):
        """Object-level validation for date consistency and range within fiscal year."""
        start_date = data.get('start_date', getattr(self.instance, 'start_date', None))
        end_date = data.get('end_date', getattr(self.instance, 'end_date', None))

        # `data.get('fiscal_year')` will be the FiscalYear instance if 'fiscal_year_id' was provided and valid
        fiscal_year_instance = data.get('fiscal_year', getattr(self.instance, 'fiscal_year', None))

        if start_date and end_date and end_date <= start_date:
            raise serializers.ValidationError({"end_date": _("Period end date must be after start date.")})

        if not fiscal_year_instance:
            # If creating, 'fiscal_year' (resolved from fiscal_year_id) must be in 'data'.
            # If 'fiscal_year' isn't in 'data' and not self.instance (i.e., creating), it's an issue.
            if not self.instance and 'fiscal_year' not in data:
                # This state implies fiscal_year_id might have been invalid or not provided.
                # The PrimaryKeyRelatedField should have caught missing fiscal_year_id if required.
                # If it's optional and not provided, this validation depends on business logic.
                # For now, assume fiscal_year is effectively required if creating.
                # This might be redundant if the field `fiscal_year_id` is not `required=False`.
                pass  # Let the model's clean method or field requirement handle this.
        else:  # fiscal_year_instance is available
            if start_date and start_date < fiscal_year_instance.start_date:
                raise serializers.ValidationError(
                    {"start_date": _(
                        "Period start date (%(p_start)s) cannot be before its Fiscal Year's start date (%(fy_start)s).") %
                                   {'p_start': start_date, 'fy_start': fiscal_year_instance.start_date}})
            if end_date and end_date > fiscal_year_instance.end_date:
                raise serializers.ValidationError(
                    {"end_date": _(
                        "Period end date (%(p_end)s) cannot be after its Fiscal Year's end date (%(fy_end)s).") %
                                 {'p_end': end_date, 'fy_end': fiscal_year_instance.end_date}})

        # Model's `clean()` method will handle date overlaps with other AccountingPeriods
        # within the same FiscalYear.
        return data

    def create(self, validated_data):
        company = self.context.get('company_context')
        if not company:
            logger.error("AccountingPeriodWriteSerializer.create: 'company_context' is missing.")
            raise serializers.ValidationError(_("Internal Error: Company context is required for creation."))

        fiscal_year_instance = validated_data.get('fiscal_year')  # This is the resolved FY instance
        if fiscal_year_instance and fiscal_year_instance.company_id != company.id:
            # This should have been caught by validate_fiscal_year_id
            raise serializers.ValidationError(_("Fiscal Year assignment error: Company mismatch with current context."))

        validated_data['company'] = company  # Assign the company from context
        return super().create(validated_data)

    def update(self, instance: AccountingPeriod, validated_data):
        if instance.locked:
            # Check if any fields that affect financials or period boundaries are being changed
            disallowed_changes = {'start_date', 'end_date', 'fiscal_year'}
            if any(field in validated_data for field in disallowed_changes):
                raise serializers.ValidationError(
                    _("Cannot change dates or Fiscal Year of a locked accounting period. Unlock it first."))

        if 'company' in validated_data and validated_data['company'] != instance.company:
            raise serializers.ValidationError(_("The company of an existing Accounting Period cannot be changed."))
        validated_data.pop('company', None)

        new_fiscal_year_instance = validated_data.get('fiscal_year')
        if new_fiscal_year_instance and new_fiscal_year_instance.company_id != instance.company_id:
            # `validate_fiscal_year_id` should catch this if `fiscal_year_id` is in `validated_data`.
            # This is an extra check for direct `fiscal_year` instance manipulation (less common).
            raise serializers.ValidationError(_("Cannot reassign period to a Fiscal Year of a different company."))

        return super().update(instance, validated_data)