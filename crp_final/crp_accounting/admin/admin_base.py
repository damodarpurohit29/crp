import logging
from typing import Optional, Tuple, Any, Type, List, Dict

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError, ImproperlyConfigured, FieldDoesNotExist
from django.db import models, transaction
from django.db.models.query import QuerySet
from django.forms import BaseForm  # Keep BaseForm for type hinting
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from django.utils.text import get_text_list

from crp_accounting.forms import TenantAdminBaseModelForm

# --- Critical Company App Dependencies ---
try:
    from company.utils import get_current_company
    from company.models import Company
except ImportError as e:
    raise ImproperlyConfigured(
        "TenantAccountingModelAdmin: Critical dependency 'company' app (models/utils) "
        "could not be imported. Ensure it's correctly installed and in INSTALLED_APPS. "
        f"Original error: {e}"
    ) from e

# --- Import for the custom base form ---
try:
    from crp_accounting.forms import TenantAdminBaseModelForm
except ImportError:
    # Fallback if forms.py or TenantAdminBaseModelForm isn't created yet,
    # but it's essential for the fix.
    from crp_accounting.forms import TenantAdminBaseModelForm  # type: ignore

    logging.getLogger("crp_accounting.admin_base").warning(
        "TenantAdminBaseModelForm not found, falling back to django.forms.ModelForm. "
        "The ValueError fix for readonly 'company' field errors might not work as intended. "
        "Please create crp_accounting/forms.py with TenantAdminBaseModelForm."
    )

logger = logging.getLogger("crp_accounting.admin_base")


class TenantAccountingModelAdmin(admin.ModelAdmin):
    form = TenantAdminBaseModelForm  # Use the custom form as default

    dynamic_company_display_field_name = 'get_record_company_display'
    tenant_parent_fk_fields: List[str] = []

    def _get_company_from_request_obj_or_form(
            self, request: HttpRequest, obj: Optional[models.Model] = None,
            form_data_for_add_view_post: Optional[Dict[str, Any]] = None
    ) -> Optional[Company]:
        # 1. From object instance (most definitive if obj exists)
        if obj and hasattr(obj, 'company_id') and obj.company_id:
            if hasattr(obj, 'company') and isinstance(obj.company, Company):
                # logger.debug(f"AdminBaseCTX: Company '{obj.company.name}' derived from obj.company for '{obj._meta.object_name}'.")
                return obj.company
            try:
                company_instance = Company.objects.get(pk=obj.company_id)
                # logger.debug(f"AdminBaseCTX: Company '{company_instance.name}' fetched for obj '{obj._meta.object_name}'.")
                return company_instance
            except Company.DoesNotExist:
                logger.error(
                    f"AdminBaseCTX: Object '{obj._meta.object_name}' (PK: {obj.pk}) has company_id {obj.company_id} but Company not found.")

        # 2. From form_data (for superuser adding new record, during POST after company selection)
        if not obj and request.user.is_superuser and form_data_for_add_view_post and 'company' in form_data_for_add_view_post:
            company_pk_from_form = form_data_for_add_view_post.get('company')
            if isinstance(company_pk_from_form, Company):
                # logger.debug(f"AdminBaseCTX: Company '{company_pk_from_form.name}' derived from form_data (instance) for new obj by SU.")
                return company_pk_from_form
            if company_pk_from_form:
                try:
                    company_instance = Company.objects.get(pk=company_pk_from_form)
                    # logger.debug(f"AdminBaseCTX: Company '{company_instance.name}' derived from form_data (PK) for new obj by SU.")
                    return company_instance
                except (Company.DoesNotExist, ValueError, TypeError):  # Added TypeError for invalid PK type
                    logger.warning(f"AdminBaseCTX: Invalid company PK '{company_pk_from_form}' in form_data by SU.")

        # 3. From request.company (set by middleware)
        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company):
            # logger.debug(f"AdminBaseCTX: Company '{request_company.name}' derived from request.company.")
            return request_company

        # 4. From thread-local (set by middleware)
        thread_local_company = get_current_company()
        if isinstance(thread_local_company, Company):
            # logger.debug(f"AdminBaseCTX: Company '{thread_local_company.name}' derived from thread-local.")
            return thread_local_company

        # logger.log(logging.WARNING if not request.user.is_superuser else logging.DEBUG, "AdminBaseCTX: Could not determine company context.")
        return None

    def _has_standard_company_field(self, model_or_instance: Optional[Any] = None) -> bool:
        target_model = model_or_instance._meta.model if model_or_instance and hasattr(model_or_instance,
                                                                                      '_meta') else self.model
        try:
            field = target_model._meta.get_field('company')
            return field.is_relation and field.remote_field.model == Company
        except FieldDoesNotExist:
            return False

    def _user_has_company_object_permission(self, request: HttpRequest, obj: Optional[models.Model]) -> bool:
        if request.user.is_superuser: return True
        request_user_company_context = self._get_company_from_request_obj_or_form(request, None)
        if not request_user_company_context: return False
        if obj is None: return True
        if not self._has_standard_company_field(obj): return True
        obj_company_id = getattr(obj, 'company_id', None)
        if obj_company_id != request_user_company_context.id:
            logger.warning(
                f"AdminBasePerm Denied: User '{request.user.name}' (Context: '{request_user_company_context.name}') "
                f"accessing {obj._meta.verbose_name} (PK: {obj.pk}) of different Company (ID: {obj_company_id})."
            )
            return False
        return True

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        qs = super().get_queryset(request)
        if not self._has_standard_company_field(): return qs

        if not request.user.is_superuser:
            company_context_for_qs = self._get_company_from_request_obj_or_form(request)
            if company_context_for_qs:
                qs = qs.filter(company=company_context_for_qs)
            else:
                logger.warning(
                    f"AdminBase GetQueryset: Non-SU '{request.user.name}' has NO company context for {self.model.__name__}. Returning empty queryset.")
                return qs.none()

        # Always try to select_related company if the field exists for efficiency
        if self._has_standard_company_field():
            qs = qs.select_related('company')
        return qs

    def get_form(self, request: HttpRequest, obj: Optional[models.Model] = None, change: bool = False, **kwargs: Any) -> \
    Type[BaseForm]:
        # Get the base form class, which should be TenantAdminBaseModelForm or its subclass from self.form
        BaseFormClass = super().get_form(request, obj, change=change, **kwargs)

        is_add_view = obj is None
        if is_add_view and \
                not request.user.is_superuser and \
                self._has_standard_company_field(self.model):

            user_company = self._get_company_from_request_obj_or_form(request, None)
            if user_company:
                # Dynamically create a subclass of the current BaseFormClass
                # to ensure its instance.company is set correctly for non-SU add view.
                class FormWithPresetCompany(BaseFormClass):  # type: ignore
                    def __init__(self, *form_args: Any, **form_kwargs: Any):
                        instance_from_kwargs = form_kwargs.get('instance')
                        if instance_from_kwargs is None:
                            # If no instance is passed (typical for add view),
                            # create a new model instance and set its company.
                            # This instance will be used by the ModelForm.
                            model_class = self._meta.model  # Access model from FormClass.Meta
                            current_instance = model_class()
                            setattr(current_instance, 'company', user_company)
                            form_kwargs['instance'] = current_instance
                        super().__init__(*form_args, **form_kwargs)

                return FormWithPresetCompany  # type: ignore

        return BaseFormClass  # type: ignore

    def save_model(self, request: HttpRequest, obj: models.Model, form: BaseForm, change: bool) -> None:
        is_new = not obj.pk
        is_standard_tenant_model = self._has_standard_company_field(obj)
        company_assigned_to_obj: Optional[Company] = None

        # 1. Determine/confirm the object's company
        if is_standard_tenant_model:
            if hasattr(obj, 'company') and obj.company:  # Company might be pre-set on form.instance
                company_assigned_to_obj = obj.company
            elif is_new:  # New object, company not pre-set, try to derive it
                # For SU, form_data will contain their selection.
                # For non-SU, if get_form didn't set it, this is a fallback (should be rare).
                form_data_dict = form.cleaned_data if form and hasattr(form, 'cleaned_data') and form.is_bound else None
                derived_company = self._get_company_from_request_obj_or_form(request, None, form_data_dict)

                if derived_company:
                    obj.company = derived_company
                    company_assigned_to_obj = derived_company
                elif not request.user.is_superuser:  # Non-SU, new object, company still not found
                    msg = _("Cannot save: Company could not be determined for this new record and your user session.")
                    messages.error(request, msg)
                    logger.error(
                        f"AdminBase SaveModel(New): Non-SU '{request.user.name}' for {obj._meta.verbose_name}: company context missing. Denying save.")
                    raise PermissionDenied(msg)  # This will stop before full_clean

            # If company still not set, and it's a standard model, it might be an issue for full_clean
            if not company_assigned_to_obj and hasattr(obj, 'company_id') and obj.company_id:
                try:  # Should have been caught by obj.company check, but as a safeguard
                    company_assigned_to_obj = Company.objects.get(pk=obj.company_id)
                except Company.DoesNotExist:
                    pass

        # 2. Set audit fields
        user = request.user if request.user.is_authenticated else None
        if user:
            if is_new and hasattr(obj, 'created_by_id') and not obj.created_by_id: setattr(obj, 'created_by', user)
            if hasattr(obj, 'updated_by_id'): setattr(obj, 'updated_by', user)

        # 3. Infer company from parent FK if still not set (and obj is standard tenant model)
        # This is more for cases where 'company' might not be directly on the form but implied
        parent_fk_fields = getattr(self, 'tenant_parent_fk_fields', [])
        if is_standard_tenant_model and not getattr(obj, 'company_id', None) and parent_fk_fields:
            for field_name in parent_fk_fields:
                parent_obj = getattr(obj, field_name, None)
                if parent_obj and hasattr(parent_obj, 'company') and parent_obj.company:
                    obj.company = parent_obj.company
                    company_assigned_to_obj = obj.company
                    logger.info(
                        f"AdminBase SaveModel: Inferred Company '{obj.company.name}' for new {obj._meta.verbose_name} from parent '{field_name}'.")
                    break

        # 4. Validate parent FK integrity (if obj.company is now set)
        if company_assigned_to_obj and parent_fk_fields:
            for field_name in parent_fk_fields:
                parent_obj = getattr(obj, field_name, None)
                parent_obj_company_id = getattr(parent_obj, 'company_id', None) if parent_obj else None
                if parent_obj and parent_obj_company_id != company_assigned_to_obj.id:
                    parent_company_name = getattr(parent_obj.company, 'name', f"ID {parent_obj_company_id}") if hasattr(
                        parent_obj, 'company') and parent_obj.company else f"ID {parent_obj_company_id or 'Unknown'}"
                    msg = _(
                        "Integrity Error: The selected '%(parent_field)s' (%(parent_val)s from Company '%(parent_co)s') does not match the record's Company '%(obj_co)s'.") % {
                              'parent_field': obj._meta.get_field(field_name).verbose_name,
                              'parent_val': str(parent_obj), 'parent_co': parent_company_name,
                              'obj_co': company_assigned_to_obj.name}
                    if form and field_name in form.fields:
                        form.add_error(field_name, msg)  # This will use TenantAdminBaseModelForm's add_error
                    else:  # If field_name not in form, add as non-field error
                        form.add_error(None, msg)
                    logger.error(
                        f"AdminBase SaveModel: Integrity fail on {obj._meta.verbose_name} ID {obj.pk or 'NEW'}. Field '{field_name}': {msg}")
                    # Do not raise ValidationError here directly if form can handle it, let form validation flow
                    # However, if form.add_error leads to is_valid() being false, Django won't save.
                    # If form processing doesn't catch it, a direct raise might be needed but let form try first.
                    # For now, rely on form.add_error making form invalid.

        # 5. Perform model's full_clean
        try:
            # Ensure obj.company is set if it's required and still missing (should be rare now)
            if is_standard_tenant_model and not getattr(obj, 'company_id', None) and \
                    not obj._meta.get_field('company').blank and not obj._meta.get_field('company').null:
                # This is a fallback, should have been set or error raised earlier
                logger.error(
                    f"AdminBase SaveModel: Critical - company not set on '{obj._meta.verbose_name}' before full_clean, and it's required.")
                # Trigger a validation error that form can pick up
                raise ValidationError({'company': _("Company is required and could not be determined.")})

            obj.full_clean()
        except ValidationError as e:
            logger.warning(
                f"AdminBase SaveModel: Model validation failed for {obj._meta.verbose_name} PK '{obj.pk or 'NEW'}' "
                f"(Co: {getattr(company_assigned_to_obj, 'name', 'Unset')}): {e.message_dict if hasattr(e, 'message_dict') else e.messages}"
            )
            if form and hasattr(form, '_update_errors'):  # Ensure form can handle it
                form._update_errors(e)  # This uses the custom add_error
            else:  # Fallback if no form or form can't handle _update_errors
                messages.error(request, _("Validation Error: %(errors)s") % {
                    'errors': e.messages_joined if hasattr(e, 'messages_joined') else str(e)})
            return  # Critical: return here to prevent super().save_model from being called with invalid obj

        # If form is invalid after all error additions, Django's ModelAdmin.changeform_view or add_view
        # will typically prevent super().save_model from being called if it checks form.is_valid() before this point.
        # However, save_model itself is called after some initial form validation.
        # We must ensure that if form becomes invalid due to errors added here, we don't proceed.
        # The `return` after `form._update_errors(e)` handles this.

        logger.info(
            f"AdminBase SaveModel: User '{request.user.name}' {'updating' if change else 'creating'} {obj._meta.verbose_name} "
            f"{'(PK: ' + str(obj.pk) + ')' if obj.pk else ''} for Company '{getattr(company_assigned_to_obj, 'name', 'N/A') if company_assigned_to_obj else 'N/A'}'.")
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request: HttpRequest, obj: Optional[models.Model] = None) -> Tuple[str, ...]:
        ro_fields = set(super().get_readonly_fields(request, obj) or [])
        if self._has_standard_company_field():
            if obj and obj.pk:  # Change view
                ro_fields.add('company')
            elif not request.user.is_superuser and not obj:  # Add view for non-SU
                ro_fields.add('company')

        audit_fields = {'created_at', 'created_by', 'updated_at', 'updated_by', 'deleted_at'}
        for f_name in audit_fields:
            # Check if model actually has these fields before adding to readonly
            try:
                self.model._meta.get_field(f_name)
                ro_fields.add(f_name)
            except FieldDoesNotExist:
                pass
        return tuple(ro_fields)

    def get_changeform_initial_data(self, request: HttpRequest) -> Dict[str, Any]:
        initial = super().get_changeform_initial_data(request)
        is_add_view = not request.resolver_match.kwargs.get('object_id')

        if is_add_view and self._has_standard_company_field():
            # For SU, if they have a request-level company context, pre-fill if 'company' is editable.
            if request.user.is_superuser:
                company_context_for_request = self._get_company_from_request_obj_or_form(request, None)
                if company_context_for_request and 'company' not in initial:
                    # Check if company is actually editable by SU on add
                    # (i.e., not in readonly_fields for SU on add view)
                    current_readonly_fields = self.get_readonly_fields(request, None)
                    is_company_editable_on_add_for_su = 'company' not in current_readonly_fields

                    fieldsets_add = self.get_fieldsets(request, None)
                    is_company_in_form = any('company' in (o.get('fields', []) or []) for _, o in fieldsets_add)

                    if is_company_editable_on_add_for_su and is_company_in_form:
                        initial['company'] = company_context_for_request.pk
                        logger.debug(
                            f"AdminBase InitialData (SU Add): Pre-filled 'company' with context '{company_context_for_request.name}'.")
            # For non-SU on add view, 'company' is readonly. The actual value is set on form.instance
            # by get_form, so initial data for the widget is less critical but could be set for display.
            # However, Django's readonly field rendering will use form.instance.company.
            # No explicit initial data setting needed here for non-SU as get_form handles the instance.

        return initial

    def formfield_for_foreignkey(self, db_field: models.ForeignKey, request: HttpRequest, **kwargs: Any) -> Optional[
        models.Field]:  # type: ignore
        current_object_being_edited = None
        object_id_str = request.resolver_match.kwargs.get('object_id')
        if object_id_str:
            try:
                current_object_being_edited = self.get_object(request, object_id_str)  # type: ignore
            except (self.model.DoesNotExist, ValidationError):  # type: ignore
                pass

        form_data_for_context = request.POST if not current_object_being_edited and request.method == 'POST' and request.POST else None
        company_context_for_field = self._get_company_from_request_obj_or_form(
            request, current_object_being_edited, form_data_for_context
        )

        RelatedModel: Type[models.Model] = db_field.related_model
        default_order_list = RelatedModel._meta.ordering
        default_order = (default_order_list[0] if default_order_list else RelatedModel._meta.pk.name)

        # logger.debug(f"AdminBase Formfield: Field '{db_field.name}' -> {RelatedModel.__name__}. CoCtx: '{company_context_for_field.name if company_context_for_field else 'None'}'.")

        if db_field.name == "company" and self._has_standard_company_field(self.model):
            if request.user.is_superuser:
                kwargs["queryset"] = Company.objects.all().order_by('name')
            elif company_context_for_field:  # Non-SU, should be their own company
                kwargs["queryset"] = Company.objects.filter(pk=company_context_for_field.pk)
            else:  # Non-SU without company context (should be rare if middleware is effective)
                kwargs["queryset"] = Company.objects.none()
        elif self._has_standard_company_field(RelatedModel):  # FK to another tenant model
            if company_context_for_field:
                kwargs["queryset"] = RelatedModel.objects.filter(company=company_context_for_field).order_by(
                    default_order)
            else:
                kwargs["queryset"] = RelatedModel.objects.none()
                if request.user.is_superuser and not current_object_being_edited:
                    messages.info(request,
                                  _("Select the main 'Company' for this new record to populate choices for '%(field)s'.") % {
                                      'field': db_field.verbose_name})
        else:  # FK to a non-tenant model or queryset already provided
            if "queryset" not in kwargs:
                kwargs["queryset"] = RelatedModel.objects.all().order_by(default_order)  # type: ignore

        field = super().formfield_for_foreignkey(db_field, request, **kwargs)

        # For non-SU on add view, if 'company' field is being processed (though it's readonly)
        # ensure its widget might reflect it's not user-editable.
        # get_readonly_fields already handles making it non-editable.
        # The widget for readonly fields is typically a display of the value.
        # No need for field.widget.attrs['disabled'] here if get_readonly_fields is correct.

        return field

    def get_list_display(self, request: HttpRequest) -> Tuple[str, ...]:
        ld = list(super().get_list_display(request))
        company_col_name = self.dynamic_company_display_field_name

        is_model_company_related = self._has_standard_company_field()  # Check direct field
        if not is_model_company_related:  # Check common parents if no direct field
            # Consider a list of common parent field names that link to a company-scoped model
            common_tenant_parent_fields = ['voucher', 'account', 'fiscal_year', 'period',
                                           'ledger']  # Add others as needed
            for rel_name in common_tenant_parent_fields:
                if hasattr(self.model, rel_name):
                    try:
                        related_field = self.model._meta.get_field(rel_name)
                        if related_field.is_relation and self._has_standard_company_field(related_field.related_model):
                            is_model_company_related = True
                            break
                    except FieldDoesNotExist:
                        continue

        if request.user.is_superuser and is_model_company_related:
            if company_col_name not in ld:
                insert_pos = 1  # Default to second column (after the first item, often the PK or __str__)

                # Try to insert after 'name' if it exists, or after the primary key field name
                model_pk_name = self.model._meta.pk.name if self.model._meta.pk else None

                if 'name' in ld:
                    try:
                        insert_pos = ld.index('name') + 1
                    except ValueError:  # Should not happen if 'name' is in ld
                        pass
                elif model_pk_name and model_pk_name in ld:
                    try:
                        insert_pos = ld.index(model_pk_name) + 1
                    except ValueError:  # Should not happen if model_pk_name is in ld
                        pass
                elif ld:  # If there's at least one item, insert after it
                    insert_pos = 1
                else:  # If list_display is empty (unlikely but possible)
                    insert_pos = 0

                # Ensure insert_pos is within bounds
                if insert_pos > len(ld):
                    insert_pos = len(ld)

                ld.insert(insert_pos, company_col_name)
        elif not request.user.is_superuser and company_col_name in ld:
            # Remove the company column if user is not superuser and it's present
            try:
                ld.remove(company_col_name)
            except ValueError:  # Should not happen if company_col_name is in ld
                pass

        return tuple(ld)
    @admin.display(description=_('Company'), ordering='company__name')
    def get_record_company_display(self, obj: models.Model) -> str:
        company: Optional[Company] = None
        if self._has_standard_company_field(obj):
            company = getattr(obj, 'company', None)
        elif hasattr(obj, 'company_id') and obj.company_id:  # Fallback if obj.company isn't pre-fetched
            try:
                company = Company.objects.get(pk=obj.company_id)
            except Company.DoesNotExist:
                pass
        else:  # Try to find company via common related tenant models
            for attr_name in ['voucher', 'account', 'fiscal_year', 'period', 'ledger']:  # Add other common parents
                parent_obj = getattr(obj, attr_name, None)
                if parent_obj and self._has_standard_company_field(parent_obj):
                    company = getattr(parent_obj, 'company', None)
                    if company: break
        return company.name if company and company.name else "—"

    def has_view_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_view_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_change_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        # For soft-delete, check if obj is already deleted if model supports it.
        # Standard delete permission check remains.
        if hasattr(obj, 'is_deleted') and obj.is_deleted:  # type: ignore
            # If object is already soft-deleted, maybe different logic applies for "deleting" again.
            # For now, assume if it's soft-deleted, "delete" means "hard delete" or is disallowed.
            # Let's assume we prevent further deletion if already soft-deleted via UI action.
            pass  # Keep this in mind for specific soft-delete action logic.
        return super().has_delete_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        if not super().has_add_permission(request): return False
        # For non-SU, they must have a company context to add a company-scoped record.
        if not request.user.is_superuser and self._has_standard_company_field(self.model):
            form_data = request.POST if request.method == 'POST' else None  # For _get_company_from_request_obj_or_form
            can_determine_company = self._get_company_from_request_obj_or_form(request, None, form_data) is not None
            if not can_determine_company:
                logger.warning(
                    f"AdminBasePerm: Add denied for non-SU '{request.user.name}' due to no company context for new {self.model._meta.verbose_name}.")
                return False
        return True

    # --- Soft Delete Actions (Example, requires django-safedelete or similar) ---
    # Ensure your model actually supports soft deletion for these to make sense.
    # The `get_actions` method will dynamically add these if the model supports it.

    def get_actions(self, request: HttpRequest) -> Dict[str, Any]:
        actions = super().get_actions(request)
        # Check for a common soft-delete indicator (e.g., from django-safedelete)
        # Safedelete uses _safedelete_policy and all_objects_including_deleted manager
        model_supports_soft_delete = hasattr(self.model, '_safedelete_policy') and \
                                     hasattr(self.model, 'all_objects_including_deleted')  # type: ignore

        if model_supports_soft_delete:
            action_defs = {
                'action_soft_delete_selected': _("Soft delete selected %(verbose_name_plural)s"),
                'action_undelete_selected': _("Restore (undelete) selected %(verbose_name_plural)s")
            }
            for name, desc_template in action_defs.items():
                if name not in actions and hasattr(self, name):
                    actions[name] = (
                        getattr(self, name), name,
                        desc_template % {'verbose_name_plural': self.opts.verbose_name_plural}
                    )
        else:  # Remove if model doesn't support them
            actions.pop('action_soft_delete_selected', None)
            actions.pop('action_undelete_selected', None)
        return actions

    @admin.action(description=_("Soft delete selected items"))  # Description set by get_actions
    def action_soft_delete_selected(self, request: HttpRequest, queryset: QuerySet):
        processed_count, skipped_perms, skipped_already_deleted = 0, 0, 0
        items_to_delete = []

        for obj in queryset:
            # Assuming 'is_deleted' or similar attribute exists for soft-deleted check
            is_already_deleted = hasattr(obj, 'is_deleted') and obj.is_deleted  # type: ignore
            if is_already_deleted:
                skipped_already_deleted += 1
                continue
            if self.has_delete_permission(request, obj):  # Standard delete perm check
                items_to_delete.append(obj)
            else:
                skipped_perms += 1

        if not items_to_delete:
            if skipped_perms or skipped_already_deleted:
                msg_parts = []
                if skipped_perms: msg_parts.append(
                    _("%(count)d item(s) skipped: no permission.") % {'count': skipped_perms})
                if skipped_already_deleted: msg_parts.append(
                    _("%(count)d item(s) already deleted.") % {'count': skipped_already_deleted})
                messages.warning(request, " ".join(msg_parts))
            else:
                messages.info(request, _("No items eligible for soft deletion."))
            return

        try:
            with transaction.atomic():
                for obj in items_to_delete:
                    obj.delete(force_soft=True)  # Assuming model's delete() supports force_soft
                    processed_count += 1
            logger.info(
                f"AdminBase Action: User '{request.user.name}' soft-deleted {processed_count} {self.model._meta.verbose_name_plural}.")  # type: ignore
        except Exception as e:
            logger.exception(f"AdminBase Action: Error during batch soft-delete for user '{request.user.name}'.")
            messages.error(request, _("Error during batch soft-delete: %(err)s") % {'err': str(e)})
            return

        if processed_count: messages.success(request, _("%(count)d item(s) soft-deleted.") % {'count': processed_count})
        if skipped_perms: messages.warning(request,
                                           _("%(count)d item(s) skipped: no permission.") % {'count': skipped_perms})
        if skipped_already_deleted: messages.info(request, _("%(count)d item(s) were already deleted.") % {
            'count': skipped_already_deleted})

    @admin.action(description=_("Restore selected items"))  # Description set by get_actions
    def action_undelete_selected(self, request: HttpRequest, queryset: QuerySet):
        # This action needs to operate on *all* objects, including deleted ones.
        # Safedelete provides `Model.all_objects_including_deleted.filter(...)`
        if not (hasattr(self.model, 'all_objects_including_deleted') and hasattr(self.model,
                                                                                 'undelete')):  # type: ignore
            messages.error(request, _("This model does not support undelete operations through this admin action."))
            return

        sd_field_name = getattr(self.model._meta, 'safedelete_field_name', 'deleted_at')  # type: ignore

        # Get PKs from the initial queryset (which might only contain non-deleted items depending on default manager)
        selected_pks = list(queryset.values_list('pk', flat=True))
        if not selected_pks:
            messages.info(request, _("No items selected for restore."))
            return

        # Query for these PKs using the manager that includes soft-deleted items
        items_to_consider_qs = self.model.all_objects_including_deleted.filter(  # type: ignore
            pk__in=selected_pks,
            **{f"{sd_field_name}__isnull": False}  # Ensure they are actually soft-deleted
        )

        processed_count, skipped_perms, skipped_not_deleted = 0, 0, 0
        actual_items_to_restore = []

        # Check which of the initially selected items are actually soft-deleted
        soft_deleted_pks_from_consider_qs = set(items_to_consider_qs.values_list('pk', flat=True))

        for pk in selected_pks:
            if pk not in soft_deleted_pks_from_consider_qs:
                skipped_not_deleted += 1

        for obj in items_to_consider_qs:  # Iterate only over those confirmed to be soft-deleted
            # For undelete, typically 'change' permission is appropriate.
            if self.has_change_permission(request, obj):
                actual_items_to_restore.append(obj)
            else:
                skipped_perms += 1

        if not actual_items_to_restore:
            if skipped_perms or skipped_not_deleted:
                msg_parts = []
                if skipped_perms: msg_parts.append(
                    _("%(count)d item(s) skipped: no permission.") % {'count': skipped_perms})
                if skipped_not_deleted: msg_parts.append(
                    _("%(count)d item(s) were not soft-deleted.") % {'count': skipped_not_deleted})
                messages.warning(request, " ".join(msg_parts))
            else:
                messages.warning(request,
                                 _("No selected items were eligible for restore (e.g., not deleted or no permission)."))
            return

        try:
            with transaction.atomic():
                for obj in actual_items_to_restore:
                    obj.undelete()  # Call model's undelete method
                    processed_count += 1
            logger.info(
                f"AdminBase Action: User '{request.user.name}' restored {processed_count} {self.model._meta.verbose_name_plural}.")  # type: ignore
        except Exception as e:
            logger.exception(f"AdminBase Action: Error during batch restore for user '{request.user.name}'.")
            messages.error(request, _("Error during batch restore: %(err)s") % {'err': str(e)})
            return

        if processed_count: messages.success(request, _("%(count)d item(s) restored.") % {'count': processed_count})
        if skipped_perms: messages.warning(request, _("%(count)d item(s) skipped: no permission to restore.") % {
            'count': skipped_perms})
        if skipped_not_deleted: messages.info(request, _("%(count)d selected item(s) were not soft-deleted.") % {
            'count': skipped_not_deleted})