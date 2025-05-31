# crp_accounting/serializers/party.py

import logging

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from django.utils.translation import gettext_lazy as _
from decimal import Decimal

# --- Model Imports ---
from ..models.party import Party
from ..models.coa import Account

# --- Enum Imports ---
from crp_core.enums import PartyType

# --- Shared Serializer Import ---
from .coa import AccountSummarySerializer

logger = logging.getLogger(__name__)

# =============================================================================
# Party Serializers (Tenant Aware)
# =============================================================================

class PartyReadSerializer(serializers.ModelSerializer):
    # ... (Your PartyReadSerializer is good as is) ...
    control_account = AccountSummarySerializer(read_only=True, allow_null=True)
    party_type_display = serializers.CharField(source='get_party_type_display', read_only=True)
    balance = serializers.SerializerMethodField(help_text=_("Dynamically calculated outstanding balance."))
    credit_status = serializers.SerializerMethodField(help_text=_("Credit limit status ('Within Limit', 'Over Credit Limit', 'N/A')."))

    class Meta:
        model = Party
        fields = [
            'id',
            'party_type',
            'party_type_display',
            'name',
            'contact_email',
            'contact_phone',
            'address',
            'control_account',
            'credit_limit',
            'is_active',
            'balance',
            'credit_status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_balance(self, obj: Party) -> Decimal | None:
        try:
            return obj.calculate_outstanding_balance()
        except (ValueError, AttributeError, ObjectDoesNotExist) as e:
            logger.warning(f"Could not calculate balance for Party {obj.pk} (Co {obj.company_id}): {e}")
            return None

    def get_credit_status(self, obj: Party) -> str:
        try:
            return obj.get_credit_status()
        except (ValueError, AttributeError, ObjectDoesNotExist) as e:
            logger.warning(f"Could not get credit status for Party {obj.pk} (Co {obj.company_id}): {e}")
            return 'Error'


class PartyWriteSerializer(serializers.ModelSerializer):
    control_account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.all(), # <<< VIEW MUST OVERRIDE THIS QUERYSET with company filter
        allow_null=True,
        required=False,
        help_text=_("ID of the Control Account (must belong to your company and be appropriate for the party type).")
    )

    class Meta:
        model = Party
        fields = [
            'id',
            'party_type',
            'name',
            'contact_email',
            'contact_phone',
            'address',
            'control_account',
            'credit_limit',
            'is_active',
        ]
        read_only_fields = ('id',)

    def validate_name(self, value):
        request = self.context.get('request')
        if not request or not hasattr(request, 'company') or not request.company:
            logger.warning("Party name validation skipped: Company context missing for request.")
            # Consider if this should be an error or rely on DB constraint
            # If company is always expected, raise serializers.ValidationError("Company context is missing.")
            return value

        company = request.company
        queryset = Party.objects.filter(company=company, name=value)
        instance = getattr(self, 'instance', None)
        if instance:
            queryset = queryset.exclude(pk=instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(_("A Party with this name already exists in your company."))
        return value

    def validate_control_account(self, value):
        # This validation is specifically for the 'control_account' field data passed in.
        # It ensures the chosen account (if any) is valid in its own right for company context.
        # The broader 'is this account suitable for THIS party_type' is in self.validate().
        if value is None: # No account selected, nothing to validate here.
            return value

        request = self.context.get('request')
        if not request or not hasattr(request, 'company') or not request.company:
            logger.warning("Control account company validation skipped: Company context missing for request.")
            # As above, consider if this should be an error.
            return value

        company_context = request.company
        if value.company != company_context:
            raise serializers.ValidationError(
                _("The selected Control Account does not belong to your company.")
            )
        return value


    def validate(self, data):
        instance = getattr(self, 'instance', None)

        # Determine company context for the party being created/updated
        company = None
        if instance:
            company = instance.company
        else: # Creating a new party
            request = self.context.get('request')
            if request and hasattr(request, 'company') and request.company:
                company = request.company
            else:
                # This should ideally be set in the view's perform_create by assigning request.company
                # If it's absolutely critical and might be missed by view logic:
                raise serializers.ValidationError(
                    {"non_field_errors": _("Internal Error: Company context could not be determined for the Party.")}
                )
        # If data contains 'company', ensure it matches context or handle appropriately
        # For now, assuming 'company' for the Party itself is set by the view and not in 'data'.

        party_type_value = data.get('party_type', getattr(instance, 'party_type', None))
        control_account_instance = data.get('control_account', getattr(instance, 'control_account', None))
        is_active_value = data.get('is_active', getattr(instance, 'is_active', True))

        if party_type_value is None: # party_type is a required field for Party model usually.
             raise serializers.ValidationError({'party_type': _("Party type is required.")})


        # --- Control Account Logic ---
        requires_ca_types_values = [PartyType.CUSTOMER.value, PartyType.SUPPLIER.value]

        # 1. Check if CA is required
        if is_active_value and party_type_value in requires_ca_types_values and not control_account_instance:
            party_type_label = PartyType(party_type_value).label if party_type_value else "selected type"
            raise serializers.ValidationError({
                'control_account': _("An active %(type)s must have a Control Account assigned.") % {'type': party_type_label}
            })

        # 2. Validate selected CA (if one is provided)
        if control_account_instance:
            # a. Ensure it belongs to the party's company (double-check, validate_control_account also does this for request context)
            # This check is crucial if the party's company could differ from request.company (e.g. admin editing any company's party)
            if control_account_instance.company != company:
                 raise serializers.ValidationError({'control_account': _("Control Account must belong to the same company as the Party.")})

            # b. Check if it IS a control account
            if not control_account_instance.is_control_account:
                raise serializers.ValidationError({'control_account': _("The selected account '%(name)s' is not designated as a control account.") % {'name': control_account_instance.account_name}})

            # c. Check if it's the CORRECT type of control account for the party_type
            #    (Allows generic control accounts, where account.control_account_party_type is None)
            account_specific_party_type = getattr(control_account_instance, 'control_account_party_type', None)

            is_valid_type_match = False
            if account_specific_party_type is None: # Generic control account, valid for any requiring type
                is_valid_type_match = True
            elif account_specific_party_type == party_type_value: # Specific type matches party's type
                is_valid_type_match = True

            if not is_valid_type_match:
                account_type_label = PartyType(account_specific_party_type).label if account_specific_party_type else _("Generic/Unspecified")
                party_type_label = PartyType(party_type_value).label

                raise serializers.ValidationError({
                    'control_account': _(
                        "The selected Control Account '%(account_name)s' is designated for '%(account_type)s' parties, "
                        "but this Party is of type '%(party_type)s'. Please select a compatible Control Account."
                    ) % {
                        'account_name': control_account_instance.account_name,
                        'account_type': account_type_label,
                        'party_type': party_type_label,
                    }
                })

        # --- Credit Limit Validation ---
        # If you have model-level constraints (e.g., CheckConstraint for credit_limit >= 0),
        # they will also apply. Explicit validation here is fine too.
        # credit_limit = data.get('credit_limit', getattr(instance, 'credit_limit', Decimal('0.00')))
        # if credit_limit < Decimal('0.00'):
        #     raise serializers.ValidationError({'credit_limit': _("Credit limit cannot be negative.")})

        return data