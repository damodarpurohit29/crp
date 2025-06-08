import logging
from typing import Optional, Tuple, Any, Type, List, Dict

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError, ImproperlyConfigured, FieldDoesNotExist
from django.db import models, transaction
from django.db.models import Q
from django.db.models.query import QuerySet
from django.forms import BaseForm
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
# from django.utils.text import get_text_list # Not used, can be removed

from crp_accounting.forms import TenantAdminBaseModelForm
from crp_accounting.models.base import ExchangeRate

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
    from crp_accounting.forms import TenantAdminBaseModelForm  # type: ignore

    logging.getLogger("crp_accounting.admin_base").warning(
        "TenantAdminBaseModelForm not found, falling back to django.forms.ModelForm. "
        "The ValueError fix for readonly 'company' field errors might not work as intended. "
        "Please create crp_accounting/forms.py with TenantAdminBaseModelForm."
    )

logger = logging.getLogger("crp_accounting.admin_base")


# --- Custom List Filter for Soft Deletion Status ---
class DeletionStatusListFilter(admin.SimpleListFilter):
    title = _('status')
    parameter_name = 'deletion_status_filter'
    _parameter_name_for_get_queryset_check = 'deletion_status_filter'

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> List[Tuple[Optional[str], str]]:
        if not (hasattr(model_admin.model, '_safedelete_policy') and
                hasattr(model_admin.model, 'all_objects_including_deleted')):
            return []
        return [
            (None, _('Active')),
            ('deleted', _('Deleted')),
            ('all', _('All (including deleted)')),
        ]

    def choices(self, changelist) -> List[Dict[str, Any]]:
        yield {
            'selected': self.value() is None,
            'query_string': changelist.get_query_string(remove=[self.parameter_name]),
            'display': _('Active'),
        }
        custom_lookup_choices = [lc for lc in self.lookup_choices if lc[0] is not None]
        for lookup, title in custom_lookup_choices:
            yield {
                'selected': self.value() == str(lookup),
                'query_string': changelist.get_query_string({self.parameter_name: lookup}),
                'display': title,
            }

    def _get_soft_delete_field_details(self, model: Type[models.Model]) -> Tuple[Optional[str], Optional[models.Field]]:
        """
        Determines the soft-delete field name and the field object itself.
        Prioritizes 'safedelete_field_name', then 'deleted', then 'deleted_at'.
        Returns (field_name, field_object) or (None, None) if not found.
        """
        sd_field_name = getattr(model._meta, 'safedelete_field_name', None)
        field_object = None

        if sd_field_name:
            try:
                field_object = model._meta.get_field(sd_field_name)
            except FieldDoesNotExist:
                logger.warning(
                    f"Configured 'safedelete_field_name' ('{sd_field_name}') not found on model {model.__name__}.")
                sd_field_name = None  # Reset if configured field doesn't exist

        if not sd_field_name:  # If not found via safedelete_field_name or it was invalid
            common_names = ['deleted', 'deleted_at']  # Prioritize 'deleted' as per error
            for name in common_names:
                if hasattr(model, name):
                    try:
                        field_object = model._meta.get_field(name)
                        sd_field_name = name
                        break  # Found a common field
                    except FieldDoesNotExist:
                        continue  # Should not happen if hasattr is true, but defensive

        if not sd_field_name:
            logger.warning(f"Model {model.__name__} might support soft-delete, but deletion field "
                           f"('safedelete_field_name', 'deleted', or 'deleted_at') not found.")
            return None, None

        return sd_field_name, field_object

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if not (hasattr(queryset.model, '_safedelete_policy') and
                hasattr(queryset.model, 'all_objects_including_deleted')):
            return queryset

        sd_field_name, field_object = self._get_soft_delete_field_details(queryset.model)

        if not sd_field_name or not field_object:
            return queryset  # Cannot determine field, return unfiltered

        if self.value() == 'deleted':
            if isinstance(field_object, models.BooleanField):
                return queryset.filter(**{sd_field_name: True})
            else:  # Assuming DateTimeField or similar that can be null for active
                return queryset.filter(**{f"{sd_field_name}__isnull": False})
        if self.value() is None:  # 'Active'
            if isinstance(field_object, models.BooleanField):
                return queryset.filter(**{sd_field_name: False})
            else:  # Assuming DateTimeField or similar that can be null for active
                return queryset.filter(**{f"{sd_field_name}__isnull": True})

        # For 'all', no further filtering by deletion status needed here
        return queryset


class TenantAccountingModelAdmin(admin.ModelAdmin):
    form = TenantAdminBaseModelForm
    dynamic_company_display_field_name = 'get_record_company_display'
    tenant_parent_fk_fields: List[str] = []

    def _model_supports_soft_delete(self) -> bool:
        return (hasattr(self.model, '_safedelete_policy') and
                hasattr(self.model, 'all_objects_including_deleted'))

    def _get_soft_delete_field_details_for_model_admin(self) -> Tuple[Optional[str], Optional[models.Field]]:
        """Helper to get soft-delete field details for the ModelAdmin's model."""
        # Reuse logic from DeletionStatusListFilter or similar dedicated helper
        # For simplicity, directly calling a similar logic here.
        # In a real scenario, you might make _get_soft_delete_field_details a static method or utility.
        model = self.model
        sd_field_name = getattr(model._meta, 'safedelete_field_name', None)
        field_object = None

        if sd_field_name:
            try:
                field_object = model._meta.get_field(sd_field_name)
            except FieldDoesNotExist:
                sd_field_name = None

        if not sd_field_name:
            common_names = ['deleted', 'deleted_at']
            for name in common_names:
                if hasattr(model, name):
                    try:
                        field_object = model._meta.get_field(name)
                        sd_field_name = name
                        break
                    except FieldDoesNotExist:
                        continue

        return sd_field_name, field_object

    def _get_company_from_request_obj_or_form(
            self, request: HttpRequest, obj: Optional[models.Model] = None,
            form_data_for_add_view_post: Optional[Dict[str, Any]] = None
    ) -> Optional[Company]:
        if obj and hasattr(obj, 'company_id') and obj.company_id:
            if hasattr(obj, 'company') and isinstance(obj.company, Company):
                return obj.company
            try:
                return Company.objects.get(pk=obj.company_id)
            except Company.DoesNotExist:
                logger.error(
                    f"CTX: Obj '{obj._meta.object_name}' (PK: {obj.pk}) has company_id {obj.company_id} but Co not found.")
        if not obj and request.user.is_superuser and form_data_for_add_view_post and 'company' in form_data_for_add_view_post:
            company_pk = form_data_for_add_view_post.get('company')
            if isinstance(company_pk, Company): return company_pk
            if company_pk:
                try:
                    return Company.objects.get(pk=company_pk)
                except (Company.DoesNotExist, ValueError, TypeError):
                    logger.warning(f"CTX: Invalid company PK '{company_pk}' in form_data by SU.")
        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company): return request_company
        thread_local_company = get_current_company()
        if isinstance(thread_local_company, Company): return thread_local_company
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
        user_co_ctx = self._get_company_from_request_obj_or_form(request, None)
        if not user_co_ctx: return False
        if obj is None: return True
        if not self._has_standard_company_field(obj): return True
        obj_co_id = getattr(obj, 'company_id', None)
        if obj_co_id != user_co_ctx.id:
            logger.warning(
                f"Perm Denied: User '{request.user.name}' (Ctx: '{user_co_ctx.name}') accessing {obj._meta.verbose_name} (PK: {obj.pk}) of Co ID {obj_co_id}.")
            return False
        return True

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        filter_value = request.GET.get(DeletionStatusListFilter._parameter_name_for_get_queryset_check)
        qs: QuerySet

        if self._model_supports_soft_delete():
            if filter_value in ['deleted', 'all']:
                qs = self.model.all_objects_including_deleted.all()
            else:  # 'active' (None) or other
                qs = super().get_queryset(request)
                sd_field_name, field_object = self._get_soft_delete_field_details_for_model_admin()
                if sd_field_name and field_object:
                    if isinstance(field_object, models.BooleanField):
                        qs = qs.filter(**{sd_field_name: False})
                    else:
                        qs = qs.filter(**{f"{sd_field_name}__isnull": True})
                elif sd_field_name:  # Field name known but object not retrieved (should not happen with helper)
                    logger.warning(
                        f"Admin get_queryset: Soft-delete field '{sd_field_name}' identified for {self.model.__name__} but field object missing. Assuming DateTimeField for active filter.")
                    qs = qs.filter(**{f"{sd_field_name}__isnull": True})  # Fallback assumption
                # If sd_field_name is None, qs remains as from super()
        else:
            qs = super().get_queryset(request)

        if not self._has_standard_company_field(self.model): return qs
        if not request.user.is_superuser:
            company_ctx = self._get_company_from_request_obj_or_form(request)
            if company_ctx:
                qs = qs.filter(company=company_ctx)
            else:
                logger.warning(
                    f"Admin GetQueryset: Non-SU '{request.user.name}' has NO company context for {self.model.__name__}. Returning empty.")
                return qs.none()
        if self._has_standard_company_field(self.model): qs = qs.select_related('company')
        return qs

    def get_list_filter(self, request: HttpRequest) -> Tuple[Any, ...]:
        current_filters = list(super().get_list_filter(request))
        is_present = any(
            f == DeletionStatusListFilter or (isinstance(f, str) and f == DeletionStatusListFilter.__name__) for f in
            current_filters)
        if self._model_supports_soft_delete() and not is_present:
            current_filters.insert(0, DeletionStatusListFilter)
        return tuple(current_filters)

    # ... (get_form, save_model, get_readonly_fields, get_changeform_initial_data, formfield_for_foreignkey - largely unchanged) ...
    # Minor corrections for _has_standard_company_field calls if any were missed are included below for completeness

    def get_form(self, request: HttpRequest, obj: Optional[models.Model] = None, change: bool = False, **kwargs: Any) -> \
    Type[BaseForm]:
        BaseFormClass = super().get_form(request, obj, change=change, **kwargs)
        is_add_view = obj is None
        if is_add_view and not request.user.is_superuser and self._has_standard_company_field(self.model):
            user_company = self._get_company_from_request_obj_or_form(request, None)
            if user_company:
                class FormWithPresetCompany(BaseFormClass):  # type: ignore
                    def __init__(self, *form_args: Any, **form_kwargs: Any):
                        instance = form_kwargs.get('instance')
                        if instance is None:
                            instance = self._meta.model()
                            setattr(instance, 'company', user_company)
                            form_kwargs['instance'] = instance
                        super().__init__(*form_args, **form_kwargs)

                return FormWithPresetCompany
        return BaseFormClass

    def save_model(self, request: HttpRequest, obj: models.Model, form: BaseForm, change: bool) -> None:
        is_new, is_std_tenant = not obj.pk, self._has_standard_company_field(obj)
        obj_co: Optional[Company] = None
        if is_std_tenant:
            if hasattr(obj, 'company') and obj.company:
                obj_co = obj.company
            elif is_new:
                form_data = form.cleaned_data if form and hasattr(form, 'cleaned_data') and form.is_bound else None
                derived_co = self._get_company_from_request_obj_or_form(request, None, form_data)
                if derived_co:
                    obj.company = obj_co = derived_co
                elif not request.user.is_superuser:
                    msg = _("Cannot save: Company undetermined for new record and your session.")
                    messages.error(request, msg);
                    logger.error(
                        f"SaveModel(New): Non-SU '{request.user.name}' for {obj._meta.verbose_name}: company missing.");
                    raise PermissionDenied(msg)
            if not obj_co and hasattr(obj, 'company_id') and obj.company_id:
                try:
                    obj_co = Company.objects.get(pk=obj.company_id)
                except Company.DoesNotExist:
                    pass
        user = request.user if request.user.is_authenticated else None
        if user:
            if is_new and hasattr(obj, 'created_by_id') and not obj.created_by_id: setattr(obj, 'created_by', user)
            if hasattr(obj, 'updated_by_id'): setattr(obj, 'updated_by', user)
        parent_fks = getattr(self, 'tenant_parent_fk_fields', [])
        if is_std_tenant and not getattr(obj, 'company_id', None) and parent_fks:
            for field_name in parent_fks:
                parent = getattr(obj, field_name, None)
                if parent and hasattr(parent, 'company') and parent.company:
                    obj.company = obj_co = parent.company;
                    logger.info(
                        f"SaveModel: Inferred Co '{obj.company.name}' for new {obj._meta.verbose_name} from parent '{field_name}'.");
                    break
        if obj_co and parent_fks:
            for field_name in parent_fks:
                parent = getattr(obj, field_name, None)
                parent_co_id = getattr(parent, 'company_id', None) if parent else None
                if parent and parent_co_id != obj_co.id:
                    parent_co_name = getattr(parent.company, 'name', f"ID {parent_co_id}") if hasattr(parent,
                                                                                                      'company') and parent.company else f"ID {parent_co_id or 'Unknown'}"
                    msg = _("Integrity Error: '%(pf)s' (%(pv)s from Co '%(pc)s') mismatches record's Co '%(oc)s'.") % {
                        'pf': obj._meta.get_field(field_name).verbose_name, 'pv': str(parent), 'pc': parent_co_name,
                        'oc': obj_co.name}
                    (form.add_error(field_name, msg) if form and field_name in form.fields else form.add_error(None,
                                                                                                               msg));
                    logger.error(
                        f"SaveModel: Integrity fail on {obj._meta.verbose_name} ID {obj.pk or 'NEW'}. Field '{field_name}': {msg}")
        try:
            if is_std_tenant and not getattr(obj, 'company_id', None) and not obj._meta.get_field(
                    'company').blank and not obj._meta.get_field('company').null:
                logger.error(
                    f"SaveModel: Critical - company not set on '{obj._meta.verbose_name}' before full_clean, and it's required.");
                raise ValidationError({'company': _("Company required and undetermined.")})
            obj.full_clean()
        except ValidationError as e:
            logger.warning(
                f"SaveModel: Validation failed for {obj._meta.verbose_name} PK '{obj.pk or 'NEW'}' (Co: {getattr(obj_co, 'name', 'Unset')}): {e.message_dict if hasattr(e, 'message_dict') else e.messages}")
            if form and hasattr(form, '_update_errors'):
                form._update_errors(e)
            else:
                messages.error(request, _("Validation Error: %(errors)s") % {
                    'errors': e.messages_joined if hasattr(e, 'messages_joined') else str(e)})
            return
        logger.info(
            f"SaveModel: User '{request.user.name}' {'updating' if change else 'creating'} {obj._meta.verbose_name} {'(PK: ' + str(obj.pk) + ')' if obj.pk else ''} for Co '{getattr(obj_co, 'name', 'N/A') if obj_co else 'N/A'}'.")
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request: HttpRequest, obj: Optional[models.Model] = None) -> Tuple[str, ...]:
        ro = set(super().get_readonly_fields(request, obj) or [])
        if self._has_standard_company_field(self.model):
            if obj and obj.pk:
                ro.add('company')
            elif not request.user.is_superuser and not obj:
                ro.add('company')
        audit = {'created_at', 'created_by', 'updated_at', 'updated_by',
                 'deleted_at'}  # Assuming 'deleted_at' is the common name for a soft-delete timestamp if it exists, distinct from the 'deleted' field for safedelete logic.
        for f in audit:
            try:
                self.model._meta.get_field(f); ro.add(f)
            except FieldDoesNotExist:
                pass
        return tuple(ro)

    def get_changeform_initial_data(self, request: HttpRequest) -> Dict[str, Any]:
        initial = super().get_changeform_initial_data(request)
        is_add = not request.resolver_match.kwargs.get('object_id')
        if is_add and self._has_standard_company_field(self.model):
            if request.user.is_superuser:
                co_ctx = self._get_company_from_request_obj_or_form(request, None)
                if co_ctx and 'company' not in initial:
                    ro_fields = self.get_readonly_fields(request, None)
                    fieldsets = self.get_fieldsets(request, None)
                    is_co_editable = 'company' not in ro_fields and any(
                        'company' in (o.get('fields', []) or []) for _, o in fieldsets)
                    if is_co_editable: initial['company'] = co_ctx.pk; logger.debug(
                        f"InitialData (SU Add): Pre-filled 'company' with context '{co_ctx.name}'.")
        return initial

    def formfield_for_foreignkey(self, db_field: models.ForeignKey, request: HttpRequest, **kwargs: Any) -> Optional[
        models.Field]:
        obj_id = request.resolver_match.kwargs.get('object_id')
        current_obj = None
        if obj_id:
            try:
                current_obj = self.get_object(request, obj_id)
            except (self.model.DoesNotExist, ValidationError):
                pass
        form_data = request.POST if not current_obj and request.method == 'POST' and request.POST else None
        co_ctx = self._get_company_from_request_obj_or_form(request, current_obj, form_data)
        RelModel: Type[models.Model] = db_field.related_model
        default_order = (RelModel._meta.ordering[0] if RelModel._meta.ordering else RelModel._meta.pk.name)
        if db_field.name == "company" and self._has_standard_company_field(self.model):
            if request.user.is_superuser:
                kwargs["queryset"] = Company.objects.all().order_by('name')
            elif co_ctx:
                kwargs["queryset"] = Company.objects.filter(pk=co_ctx.pk)
            else:
                kwargs["queryset"] = Company.objects.none()
        elif self._has_standard_company_field(RelModel):
            if co_ctx:
                kwargs["queryset"] = RelModel.objects.filter(company=co_ctx).order_by(default_order)
            else:
                kwargs["queryset"] = RelModel.objects.none()
                if request.user.is_superuser and not current_obj: messages.info(request,
                                                                                _("Select 'Company' to populate choices for '%(field)s'.") % {
                                                                                    'field': db_field.verbose_name})
        else:
            if "queryset" not in kwargs: kwargs["queryset"] = RelModel.objects.all().order_by(default_order)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_list_display(self, request: HttpRequest) -> Tuple[str, ...]:
        ld = list(super().get_list_display(request))
        co_col = self.dynamic_company_display_field_name
        is_co_related = self._has_standard_company_field(self.model)
        if not is_co_related:
            parents = ['voucher', 'account', 'fiscal_year', 'period', 'ledger']
            for rel in parents:
                if hasattr(self.model, rel):
                    try:
                        rel_field = self.model._meta.get_field(rel)
                        if rel_field.is_relation and self._has_standard_company_field(
                            rel_field.related_model): is_co_related = True; break
                    except FieldDoesNotExist:
                        continue
        if request.user.is_superuser and is_co_related:
            if co_col not in ld:
                pos = 1;
                pk_name = self.model._meta.pk.name if self.model._meta.pk else None
                if 'name' in ld:
                    pos = ld.index('name') + 1
                elif pk_name and pk_name in ld:
                    pos = ld.index(pk_name) + 1
                elif ld:
                    pos = 1
                else:
                    pos = 0
                ld.insert(min(pos, len(ld)), co_col)
        elif not request.user.is_superuser and co_col in ld:
            try:
                ld.remove(co_col)
            except ValueError:
                pass
        return tuple(ld)

    @admin.display(description=_('Company'), ordering='company__name')
    def get_record_company_display(self, obj: models.Model) -> str:
        co: Optional[Company] = None
        if self._has_standard_company_field(obj):
            co = getattr(obj, 'company', None)
        elif hasattr(obj, 'company_id') and obj.company_id:
            try:
                co = Company.objects.get(pk=obj.company_id)
            except Company.DoesNotExist:
                pass
        else:
            parents = ['voucher', 'account', 'fiscal_year', 'period', 'ledger']
            for attr in parents:
                parent = getattr(obj, attr, None)
                if parent and self._has_standard_company_field(parent): co = getattr(parent, 'company', None);
                if co: break
        return co.name if co and co.name else "—"

    def has_view_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_view_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_change_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        # Note: Actual soft-delete field check (e.g., obj.deleted) is handled within action_soft_delete_selected
        return super().has_delete_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        if not super().has_add_permission(request): return False
        if not request.user.is_superuser and self._has_standard_company_field(self.model):
            form_data = request.POST if request.method == 'POST' else None
            if self._get_company_from_request_obj_or_form(request, None, form_data) is None:
                logger.warning(
                    f"AdminBasePerm: Add denied for non-SU '{request.user.name}' due to no company context for new {self.model._meta.verbose_name}.")
                return False
        return True

    def get_actions(self, request: HttpRequest) -> Dict[str, Any]:
        actions = super().get_actions(request)
        if self._model_supports_soft_delete():
            action_defs = {
                'action_soft_delete_selected': _("Soft delete selected %(verbose_name_plural)s"),
                'action_undelete_selected': _("Restore (undelete) selected %(verbose_name_plural)s")
            }
            for name, desc_template in action_defs.items():
                if name not in actions and hasattr(type(self), name):
                    unbound_method = getattr(type(self), name)
                    actions[name] = (
                    unbound_method, name, desc_template % {'verbose_name_plural': self.opts.verbose_name_plural})
        else:
            actions.pop('action_soft_delete_selected', None)
            actions.pop('action_undelete_selected', None)
        return actions

    def action_soft_delete_selected(self, request: HttpRequest, queryset: QuerySet):
        processed, no_perm, already_deleted = 0, 0, 0
        to_delete = []

        sd_field_name, field_object = self._get_soft_delete_field_details_for_model_admin()

        for obj in queryset:
            is_del = False
            if sd_field_name and field_object:  # Check if field exists to check its status
                current_val = getattr(obj, sd_field_name, None)
                if isinstance(field_object, models.BooleanField):
                    is_del = current_val is True
                else:
                    is_del = current_val is not None  # Assumes DateTimeField or similar
            elif sd_field_name and hasattr(obj, sd_field_name):  # Fallback if field_object not retrieved
                is_del = getattr(obj, sd_field_name, None) is not None  # Generic check

            if is_del: already_deleted += 1; continue
            if self.has_delete_permission(request, obj):
                to_delete.append(obj)
            else:
                no_perm += 1

        if not to_delete:
            if no_perm or already_deleted:
                parts = []
                if no_perm: parts.append(_("%(c)d item(s) skipped: no permission.") % {'c': no_perm})
                if already_deleted: parts.append(_("%(c)d item(s) already deleted.") % {'c': already_deleted})
                messages.warning(request, " ".join(parts))
            else:
                messages.info(request, _("No items eligible for soft deletion."))
            return
        try:
            with transaction.atomic():
                for obj in to_delete: obj.delete(
                    force_soft=True); processed += 1  # Assumes delete() supports force_soft
            logger.info(
                f"Admin Action: User '{request.user.name}' soft-deleted {processed} {self.model._meta.verbose_name_plural}.")
        except Exception as e:
            logger.exception(f"Admin Action: Error during batch soft-delete for '{request.user.name}'.");
            messages.error(request, _("Error during batch soft-delete: %(err)s") % {'err': str(e)})
            return
        if processed: messages.success(request, _("%(c)d item(s) soft-deleted.") % {'c': processed})
        if no_perm: messages.warning(request, _("%(c)d item(s) skipped: no permission.") % {'c': no_perm})
        if already_deleted: messages.info(request, _("%(c)d item(s) were already deleted.") % {'c': already_deleted})

    def action_undelete_selected(self, request: HttpRequest, queryset: QuerySet):
        if not (hasattr(self.model, 'all_objects_including_deleted') and hasattr(self.model, 'undelete')):
            messages.error(request, _("Model does not support undelete via this admin action."));
            return

        sd_field_name, field_object = self._get_soft_delete_field_details_for_model_admin()
        if not sd_field_name or not field_object:
            logger.error(f"action_undelete: Cannot determine soft-delete field for {self.model.__name__}.")
            messages.error(request, _("Configuration error: Cannot determine soft-delete field."));
            return

        pks = list(queryset.values_list('pk', flat=True))
        if not pks: messages.info(request, _("No items selected for restore.")); return

        filter_for_deleted = {}
        if isinstance(field_object, models.BooleanField):
            filter_for_deleted = {sd_field_name: True}
        else:
            filter_for_deleted = {f"{sd_field_name}__isnull": False}

        items_to_check = self.model.all_objects_including_deleted.filter(pk__in=pks, **filter_for_deleted)

        processed, no_perm, not_deleted_originally = 0, 0, 0
        to_restore = []

        pks_actually_deleted = set(items_to_check.values_list('pk', flat=True))
        for pk in pks:
            if pk not in pks_actually_deleted: not_deleted_originally += 1

        for obj in items_to_check:  # Iterate only over items confirmed to be soft-deleted
            if self.has_change_permission(request, obj):
                to_restore.append(obj)
            else:
                no_perm += 1

        if not to_restore:
            parts = []
            if no_perm: parts.append(_("%(c)d item(s) skipped: no permission.") % {'c': no_perm})
            if not_deleted_originally: parts.append(
                _("%(c)d selected item(s) were not soft-deleted.") % {'c': not_deleted_originally})
            if parts:
                messages.warning(request, " ".join(parts))
            else:
                messages.warning(request, _("No selected items eligible for restore."))
            return
        try:
            with transaction.atomic():
                for obj in to_restore: obj.undelete(); processed += 1
            logger.info(
                f"Admin Action: User '{request.user.name}' restored {processed} {self.model._meta.verbose_name_plural}.")
        except Exception as e:
            logger.exception(f"Admin Action: Error during batch restore for '{request.user.name}'.");
            messages.error(request, _("Error during batch restore: %(err)s") % {'err': str(e)})
            return
        if processed: messages.success(request, _("%(c)d item(s) restored.") % {'c': processed})
        if no_perm: messages.warning(request, _("%(c)d item(s) skipped: no permission to restore.") % {'c': no_perm})
        if not_deleted_originally: messages.info(request,
                                                 _("%(c)d selected item(s) were not initially soft-deleted.") % {
                                                     'c': not_deleted_originally})


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = (
    'company_display_er', 'from_currency', 'to_currency', 'date', 'rate_display_er', 'source_display_er',
    'updated_at')  # Renamed display methods
    list_filter = ('company', 'from_currency', 'to_currency', 'date', 'source')
    search_fields = ('company__name', 'from_currency', 'to_currency', 'source', 'rate')
    ordering = ('company__name', 'from_currency', 'to_currency', '-date')  # Default ordering
    date_hierarchy = 'date'
    list_select_related = ('company',)

    fieldsets = (
        (None, {
            'fields': ('company', ('from_currency', 'to_currency'), 'date', 'rate')
        }),
        (_('Optional Information'), {
            'fields': ('source',),
            'classes': ('collapse',)  # Keep it collapsed by default
        }),
        # Audit information is usually good to have, even if readonly
        (_('Audit Information'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('created_at', 'updated_at')  # These are auto-managed

    @admin.display(description=_("Company / Context"), ordering='company__name')  # Use ordering for consistency
    def company_display_er(self, obj: ExchangeRate) -> str:  # Added _er suffix for uniqueness
        return obj.company.name if obj.company_id and obj.company else _("<Global Rate>")

    @admin.display(description=_("Rate"), ordering='rate')
    def rate_display_er(self, obj: ExchangeRate) -> str:  # Added _er suffix
        # Display rate with a reasonable number of decimal places for the list view
        return f"{obj.rate:.6f}"  # Standard way to format Decimal/float

    @admin.display(description=_("Source"), ordering='source')
    def source_display_er(self, obj: ExchangeRate) -> str:  # Added _er suffix
        return obj.source or "—"  # Show a dash if source is empty

    def get_queryset(self, request: HttpRequest):
        """
        Superusers see all rates (global and company-specific).
        Non-superusers (if they have permission, though less common for exchange rates)
        would only see global rates and rates for their assigned company.
        """
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs  # Superuser sees all

        # For non-superusers:
        request_company = getattr(request, 'company', None)  # From CompanyMiddleware
        if isinstance(request_company, Company):
            # Non-SU sees global rates OR rates specific to their company.
            return qs.filter(Q(company__isnull=True) | Q(company=request_company))
        else:
            # Non-SU with no specific company context (e.g., if middleware didn't set one)
            # should probably only see global rates.
            return qs.filter(company__isnull=True)

    def formfield_for_foreignkey(self, db_field, request: HttpRequest, **kwargs):
        """
        Customize ForeignKey dropdowns, specifically for the 'company' field.
        """
        if db_field.name == "company":
            if request.user.is_superuser:
                # Superusers can assign a rate to any active company or make it global (by leaving blank).
                # The 'blank=True' on the ExchangeRate.company model field allows the blank option for global.
                kwargs["queryset"] = Company.objects.filter(is_active=True).order_by('name')
            else:
                # Non-superusers (if they were ever allowed to manage company-specific rates):
                # They should only be able to select their own company if creating/editing a company-specific rate.
                # For adding global rates, they wouldn't select a company.
                # This scenario (non-SU managing rates) is less common. Usually, SUs manage rates.
                request_company = getattr(request, 'company', None)
                if isinstance(request_company, Company) and request_company.is_active:
                    kwargs["queryset"] = Company.objects.filter(pk=request_company.pk)
                    # If they can ONLY manage their own company's rates and not global ones,
                    # you might make the field mandatory and pre-fill it.
                    # For now, this allows them to select their own company if the field is shown.
                else:
                    # If non-SU has no company or inactive company, they can't create company-specific rates.
                    # They could potentially still create global rates if allowed.
                    kwargs["queryset"] = Company.objects.none()  # No company choices for them.
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request: HttpRequest, obj: ExchangeRate, form, change):
        """
        Custom save logic, e.g., to ensure non-superusers don't assign rates
        to companies other than their own (if they have permissions to edit company field).
        """
        log_prefix = f"[ExRateAdmin SaveModel][User:{request.user.name}][Rate:{obj.pk or 'New'}]"

        # Validate company assignment if user is not superuser
        if not request.user.is_superuser:
            request_company_context = getattr(request, 'company', None)
            if obj.company and obj.company != request_company_context:
                # This non-SU is trying to save a rate for a company that isn't their context.
                logger.error(f"{log_prefix} Non-SU '{request.user.name}' attempted to save rate for company "
                             f"'{obj.company.name if obj.company else 'Global/Unassigned'}' but their context is "
                             f"'{request_company_context.name if request_company_context else 'None'}'. Denying save.")
                raise PermissionDenied(
                    _("You do not have permission to manage exchange rates for the selected company."))
            elif not obj.company and request_company_context:
                # If a non-SU is creating/editing a rate and the company field was somehow blank,
                # but they *have* a company context, should it default to their company or be global?
                # For global rates, obj.company should be None.
                # This scenario needs careful thought based on permissions.
                # If non-SUs can ONLY manage their own company's rates, then `obj.company` should always be their company.
                # If they can manage global rates, `obj.company` can be None.
                # The `formfield_for_foreignkey` already restricts their choices for the company field.
                pass  # Assuming form validation and field choices handle this.

        # Model's full_clean() is called by Django's save process or ModelAdmin's base save_model
        # No need to call obj.full_clean() explicitly here if super().save_model() does it.
        super().save_model(request, obj, form, change)
        logger.info(f"{log_prefix} ExchangeRate saved. From:{obj.from_currency}, To:{obj.to_currency}, "
                    f"Date:{obj.date}, Rate:{obj.rate:.6f}, Co:{obj.company.name if obj.company else '<Global>'}")

    # Permission methods: Customize who can add/change/delete rates.
    # By default, Django checks model-level permissions (add_exchangerate, change_exchangerate, etc.)
    # We add company-specific checks for non-superusers.

    def has_change_permission(self, request: HttpRequest, obj: Optional[ExchangeRate] = None) -> bool:
        # Check standard Django model permission first
        if not super().has_change_permission(request, obj):
            return False
        if obj is None:  # For the changelist view itself, permission granted if they have general change perm
            return True
        if request.user.is_superuser:
            return True

        # Non-superuser: can change global rates or rates for their own company
        request_company = getattr(request, 'company', None)
        if obj.company is None:  # It's a global rate
            # Decide if non-SUs can change global rates. Typically, yes, if they have general change perm.
            return True
        return obj.company == request_company  # Can change if it's for their company

    def has_delete_permission(self, request: HttpRequest, obj: Optional[ExchangeRate] = None) -> bool:
        # Similar logic to has_change_permission
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None: return True
        if request.user.is_superuser: return True

        request_company = getattr(request, 'company', None)
        if obj.company is None: return True  # Can delete global rates if has general delete perm
        return obj.company == request_company

    def has_add_permission(self, request: HttpRequest) -> bool:
        # If a non-SU has the general 'add_exchangerate' permission, they can attempt to add.
        # The `formfield_for_foreignkey` will limit their 'company' choices.
        # The `save_model` will do a final check.
        return super().has_add_permission(request)