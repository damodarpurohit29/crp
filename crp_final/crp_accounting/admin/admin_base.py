# crp_accounting/admin_base.py

import logging
from typing import Optional, Tuple, Any, Type, List, Dict
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError, ImproperlyConfigured, FieldDoesNotExist
from django.db import models, transaction
from django.db.models.query import QuerySet
from django.forms import BaseForm
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from django.utils.text import get_text_list

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

logger = logging.getLogger("crp_accounting.admin_base")  # Specific logger for this module


class TenantAccountingModelAdmin(admin.ModelAdmin):
    """
    Base ModelAdmin for tenant-scoped accounting models.
    Provides robust multi-tenancy logic, permission handling, and utility methods.
    """
    # Standardized name for the list_display method showing the company.
    # Child admins should NOT include this in their list_display directly;
    # get_list_display in this base class will add it for superusers.
    dynamic_company_display_field_name = 'get_record_company_display'

    # Child admins can define a list of ForeignKey field names that point to other
    # tenant-scoped models (e.g., AccountingPeriod.fiscal_year).
    # save_model will check if these parent objects belong to the same company.
    tenant_parent_fk_fields: List[str] = []

    # --- Company Context Helper ---
    def _get_company_from_request_obj_or_form(
            self, request: HttpRequest, obj: Optional[models.Model] = None,
            form_data_for_add_view_post: Optional[Dict[str, Any]] = None
    ) -> Optional[Company]:
        """
        Reliably determines the current Company context for admin operations.
        Order of precedence for determining the company:
        1. From `obj.company` if `obj` is provided (editing an existing record).
        2. If adding a new record (obj is None) AND user is SU AND it's a POST request:
           From `form_data_for_add_view_post['company']` (the company selected in the form for the new record).
        3. From `request.company` (set by CompanyMiddleware).
        4. From `get_current_company()` (thread-local, also set by CompanyMiddleware).
        """
        # 1. From object instance (most definitive if obj exists)
        if obj and hasattr(obj, 'company_id') and obj.company_id:
            # Use pre-loaded related object for efficiency if available
            if hasattr(obj, 'company') and isinstance(obj.company, Company):
                logger.debug(
                    f"AdminBaseCTX: Company '{obj.company.name}' (PK: {obj.company_id}) derived from obj.company for '{obj._meta.object_name}' (PK: {obj.pk}).")
                return obj.company
            try:  # Fallback: fetch if only company_id is present
                company_instance = Company.objects.get(pk=obj.company_id)
                logger.debug(
                    f"AdminBaseCTX: Company '{company_instance.name}' (PK: {obj.company_id}) fetched for obj '{obj._meta.object_name}' (PK: {obj.pk}).")
                return company_instance
            except Company.DoesNotExist:
                logger.error(
                    f"AdminBaseCTX: Object '{obj._meta.object_name}' (PK: {obj.pk}) has company_id {obj.company_id} but Company not found. Data integrity issue?")
                # Fall through, as this company_id is invalid.

        # 2. From form_data (for superuser adding new record, during POST after company selection)
        if not obj and request.user.is_superuser and form_data_for_add_view_post and 'company' in form_data_for_add_view_post:
            company_pk_from_form = form_data_for_add_view_post.get('company')
            if isinstance(company_pk_from_form, Company):  # If form field resolved to instance
                logger.debug(
                    f"AdminBaseCTX: Company '{company_pk_from_form.name}' derived from form_data (instance) for new obj by SU '{request.user}'.")
                return company_pk_from_form
            if company_pk_from_form:  # If form field is PK
                try:
                    company_instance = Company.objects.get(pk=company_pk_from_form)
                    logger.debug(
                        f"AdminBaseCTX: Company '{company_instance.name}' derived from form_data (PK '{company_pk_from_form}') for new obj by SU '{request.user}'.")
                    return company_instance
                except (Company.DoesNotExist, ValueError):
                    logger.warning(
                        f"AdminBaseCTX: Invalid company PK '{company_pk_from_form}' in form_data by SU '{request.user}'.")
                    # Fall through, as form data company is invalid.

        # 3. From request.company (set by middleware)
        request_company = getattr(request, 'company', None)
        if isinstance(request_company, Company):
            logger.debug(
                f"AdminBaseCTX: Company '{request_company.name}' derived from request.company for user '{request.user}'.")
            return request_company

        # 4. From thread-local (set by middleware)
        thread_local_company = get_current_company()
        if isinstance(thread_local_company, Company):
            logger.debug(
                f"AdminBaseCTX: Company '{thread_local_company.name}' derived from thread-local for user '{request.user}'.")
            return thread_local_company

        log_level = logging.WARNING if not request.user.is_superuser else logging.DEBUG
        logger.log(log_level,
                   f"AdminBaseCTX: Could not determine any company context for user '{request.user}' (SU: {request.user.is_superuser}). Obj provided: {bool(obj)}.")
        return None

    def _has_standard_company_field(self, model_or_instance: Optional[models.Model] = None) -> bool:
        target_model = model_or_instance._meta.model if model_or_instance else self.model
        try:
            field = target_model._meta.get_field('company')
            return field.is_relation and field.remote_field.model == Company
        except FieldDoesNotExist:
            return False

    def _user_has_company_object_permission(self, request: HttpRequest, obj: Optional[models.Model]) -> bool:
        if request.user.is_superuser: return True

        # Get the company context for the *requesting user* (ignoring obj's company for this part)
        request_user_company_context = self._get_company_from_request_obj_or_form(request, None)

        if not request_user_company_context:
            logger.warning(
                f"AdminBasePerm: Non-SU '{request.user.username}' lacks request company context. Denying access to obj: {obj}.")
            return False

        if obj is None: return True  # For list/add views, permission based on user's context is sufficient

        if not self._has_standard_company_field(obj):
            logger.debug(
                f"AdminBasePerm: Model {obj._meta.model_name} (obj PK {obj.pk}) lacks direct 'company' field. Perm check bypassed here.")
            return True

        obj_company_id = getattr(obj, 'company_id', None)
        if obj_company_id != request_user_company_context.id:
            obj_co_name = getattr(obj.company, 'name', f"ID {obj_company_id}") if hasattr(obj,
                                                                                          'company') and obj.company else f"ID {obj_company_id or 'Unknown'}"
            logger.warning(
                f"AdminBasePerm Denied: User '{request.user.username}' (Context: '{request_user_company_context.name}') "
                f"accessing {obj._meta.verbose_name} (PK: {obj.pk}) of Company '{obj_co_name}'.")
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
                    f"AdminBase GetQueryset: Non-SU '{request.user}' has NO company context for {self.model.__name__}. Returning empty queryset.")
                return qs.none()

        return qs.select_related('company')  # Always select_related company if field exists

    def save_model(self, request: HttpRequest, obj: models.Model, form: BaseForm, change: bool) -> None:
        is_new = not obj.pk
        is_standard_tenant_model = self._has_standard_company_field(obj)
        company_assigned_to_obj: Optional[Company] = None

        # 1. Determine and set the object's company (most critical for 'new' objects)
        if is_new and is_standard_tenant_model and not getattr(obj, 'company_id', None):
            form_data = form.cleaned_data if form and form.is_bound else None
            company_assigned_to_obj = self._get_company_from_request_obj_or_form(request, None, form_data)

            if company_assigned_to_obj:
                obj.company = company_assigned_to_obj
            elif not request.user.is_superuser:
                msg = _("Cannot save: Your user session is not associated with an active company.")
                messages.error(request, msg)
                logger.error(
                    f"AdminBase SaveModel(New): Non-SU '{request.user}' for {obj._meta.verbose_name} has no company context. Denying save.")
                raise PermissionDenied(msg)
        elif hasattr(obj, 'company') and obj.company:  # Existing obj or company already set on new obj by form
            company_assigned_to_obj = obj.company

        # 2. Set audit fields
        user = request.user if request.user.is_authenticated else None
        if user:
            if is_new and hasattr(obj, 'created_by_id') and not obj.created_by_id: obj.created_by = user
            if hasattr(obj, 'updated_by_id'): obj.updated_by = user

        # 3. Infer company from parent FK if still not set (before full_clean)
        parent_fk_fields = getattr(self, 'tenant_parent_fk_fields', [])
        if not getattr(obj, 'company_id', None) and is_standard_tenant_model and parent_fk_fields:
            for field_name in parent_fk_fields:
                parent_obj = getattr(obj, field_name, None)
                if parent_obj and hasattr(parent_obj, 'company') and parent_obj.company:
                    obj.company = parent_obj.company
                    company_assigned_to_obj = obj.company
                    logger.info(
                        f"AdminBase SaveModel: Inferred Company '{obj.company.name}' for new {obj._meta.verbose_name} from parent '{field_name}'.")
                    break

                    # Re-check parent FK integrity now that obj.company should be definitively set
        if company_assigned_to_obj and parent_fk_fields:
            for field_name in parent_fk_fields:
                parent_obj = getattr(obj, field_name, None)
                parent_obj_company_id = getattr(parent_obj, 'company_id', None) if parent_obj else None
                if parent_obj and parent_obj_company_id != company_assigned_to_obj.id:
                    # Construct informative error message
                    parent_company_name = getattr(parent_obj.company, 'name', f"ID {parent_obj_company_id}") if hasattr(
                        parent_obj, 'company') and parent_obj.company else f"ID {parent_obj_company_id or 'Unknown'}"
                    msg = _(
                        "Integrity Error: The selected '%(parent_field)s' (%(parent_val)s from Company '%(parent_co)s') "
                        "does not match the record's Company '%(obj_co)s'.") % {
                              'parent_field': obj._meta.get_field(field_name).verbose_name,
                              'parent_val': str(parent_obj),
                              'parent_co': parent_company_name, 'obj_co': company_assigned_to_obj.name
                          }
                    if form and field_name in form.fields:
                        form.add_error(field_name, msg)
                    else:
                        messages.error(request, msg)
                    logger.error(
                        f"AdminBase SaveModel: Integrity fail on {obj._meta.verbose_name} ID {obj.pk or 'NEW'}. Field '{field_name}': {msg}")
                    raise ValidationError({field_name: msg})  # Prevent save

        # 4. Perform model's full_clean
        try:
            obj.full_clean()
        except ValidationError as e:
            logger.warning(
                f"AdminBase SaveModel: Model validation failed for {obj._meta.verbose_name} {obj.pk or 'NEW'} (Co: {company_assigned_to_obj.name if company_assigned_to_obj else 'Unset'}): {e.message_dict}")
            if form:
                form._update_errors(e)
            else:
                messages.error(request, _("Validation Error: %(errors)s") % {'errors': e.messages_joined})
            return

        logger.info(
            f"AdminBase SaveModel: User '{request.user}' {'updating' if change else 'creating'} {obj._meta.verbose_name} "
            f"{'(PK: ' + str(obj.pk) + ')' if obj.pk else ''} for Company '{company_assigned_to_obj.name if company_assigned_to_obj else 'N/A'}'.")
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request: HttpRequest, obj: Optional[models.Model] = None) -> Tuple[str, ...]:
        ro_fields = set(super().get_readonly_fields(request, obj) or [])
        if self._has_standard_company_field():
            if obj and obj.pk:
                ro_fields.add('company')  # Company not changeable after creation
            elif not request.user.is_superuser and not obj:
                ro_fields.add('company')  # Non-SU cannot set company on add

        audit_fields = {'created_at', 'created_by', 'updated_at', 'updated_by', 'deleted_at'}
        for f_name in audit_fields:
            if hasattr(self.model, f_name): ro_fields.add(f_name)
        return tuple(ro_fields)

    def get_changeform_initial_data(self, request: HttpRequest) -> Dict[str, Any]:
        initial = super().get_changeform_initial_data(request)
        if not request.resolver_match.kwargs.get('object_id'):  # Add view
            # For SU, if they are "acting as" a company, pre-fill it if company field is editable.
            # For non-SU, company field is readonly and derived, so no need to pre-fill here (save_model handles).
            if request.user.is_superuser:
                company_context_for_request = self._get_company_from_request_obj_or_form(request, None)
                if company_context_for_request and self._has_standard_company_field() and 'company' not in initial:
                    is_company_editable_on_add = 'company' not in self.get_readonly_fields(request, None)
                    fieldsets_add = self.get_fieldsets(request, None)
                    is_company_in_form = any('company' in (o.get('fields', []) or []) for _, o in fieldsets_add)
                    if is_company_editable_on_add and is_company_in_form:
                        initial['company'] = company_context_for_request.pk
                        logger.debug(
                            f"AdminBase InitialData (SU Add): Pre-filled 'company' with context '{company_context_for_request.name}'.")
        return initial

    def formfield_for_foreignkey(self, db_field: models.ForeignKey, request: HttpRequest, **kwargs: Any) -> Optional[
        models.Field]:
        current_object_being_edited = None
        object_id_str = request.resolver_match.kwargs.get('object_id')
        if object_id_str:  # Change view
            try:
                current_object_being_edited = self.get_object(request, object_id_str)
            except (self.model.DoesNotExist, ValidationError):
                pass

        form_data_for_context = request.POST if not current_object_being_edited and request.method == 'POST' and request.POST else None
        company_context_for_field = self._get_company_from_request_obj_or_form(request, current_object_being_edited,
                                                                               form_data_for_context)

        RelatedModel: Type[models.Model] = db_field.related_model
        default_order = (RelatedModel._meta.ordering[0] if RelatedModel._meta.ordering else RelatedModel._meta.pk.name)

        logger.debug(
            f"AdminBase Formfield: Field '{db_field.name}' -> {RelatedModel.__name__}. Effective Company Context for Field: '{company_context_for_field.name if company_context_for_field else 'None'}'.")

        if db_field.name == "company" and self._has_standard_company_field(self.model):
            if request.user.is_superuser:
                kwargs["queryset"] = Company.objects.all().order_by('name')
            elif company_context_for_field:
                kwargs["queryset"] = Company.objects.filter(pk=company_context_for_field.pk)
            else:
                kwargs["queryset"] = Company.objects.none()
        elif self._has_standard_company_field(RelatedModel):  # FK to another tenant model
            if company_context_for_field:
                kwargs["queryset"] = RelatedModel.objects.filter(company=company_context_for_field).order_by(
                    default_order)
                logger.debug(
                    f"AdminBase Formfield: Filtered '{db_field.name}' choices by Company '{company_context_for_field.name}'. Count: {kwargs['queryset'].count()}.")
            else:
                kwargs["queryset"] = RelatedModel.objects.none()
                if request.user.is_superuser and not current_object_being_edited:  # SU on 'add' form, no company selected yet for main obj
                    messages.info(request,
                                  _("Select the main 'Company' for this new record to populate choices for '%(field)s'.") % {
                                      'field': db_field.verbose_name})
                logger.debug(
                    f"AdminBase Formfield: No company context for '{db_field.name}' related choices. Empty queryset.")
        else:  # FK to a non-tenant model
            if "queryset" not in kwargs: kwargs["queryset"] = RelatedModel.objects.all().order_by(default_order)
            logger.debug(
                f"AdminBase Formfield: FK '{db_field.name}' to non-tenant model {RelatedModel.__name__}. No company filter from base.")

        # Allow child admin to further refine the queryset AFTER base class sets it
        # For example, AccountingPeriodAdmin will filter FiscalYear choices by status
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        return field

    def get_list_display(self, request: HttpRequest) -> Tuple[str, ...]:
        ld = list(super().get_list_display(request))
        company_col_name = self.dynamic_company_display_field_name  # Use the consistent name

        # Determine if model or its common parents are company-scoped
        is_model_company_related = self._has_standard_company_field() or \
                                   any(hasattr(self.model, rel_name) and hasattr(getattr(self.model, rel_name),
                                                                                 'field') and
                                       self._has_standard_company_field(
                                           getattr(self.model, rel_name).field.related_model)
                                       for rel_name in ['voucher', 'account', 'fiscal_year', 'period'])

        if request.user.is_superuser and is_model_company_related:
            if company_col_name not in ld:
                insert_pos = 1  # Default to second column
                try:
                    insert_pos = ld.index('name') + 1
                except ValueError:
                    try:
                        insert_pos = ld.index(self.model._meta.pk.name) + 1
                    except (AttributeError, ValueError):
                        pass
                ld.insert(insert_pos, company_col_name)
        elif not request.user.is_superuser and company_col_name in ld:
            ld.remove(company_col_name)
        return tuple(ld)

    @admin.display(description=_('Company'), ordering='company__name')
    def get_record_company_display(self, obj: models.Model) -> str:  # Renamed
        # ... (logic to find company directly or via parent, as before) ...
        company: Optional[Company] = None
        if self._has_standard_company_field(obj):
            company = getattr(obj, 'company', None)
        elif hasattr(obj, 'company_id') and obj.company_id:
            try:
                company = Company.objects.get(pk=obj.company_id)
            except Company.DoesNotExist:
                pass
        else:
            for attr in ['voucher', 'account', 'fiscal_year', 'period']:
                parent = getattr(obj, attr, None)
                if parent and self._has_standard_company_field(parent):
                    company = getattr(parent, 'company', None)
                    if company: break
        return company.name if company else "—"

    def has_view_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_view_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_change_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[models.Model] = None) -> bool:
        return super().has_delete_permission(request, obj) and self._user_has_company_object_permission(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        if not super().has_add_permission(request): return False
        form_data = request.POST if request.method == 'POST' else None
        can_determine_company = self._get_company_from_request_obj_or_form(request, None, form_data) is not None
        if not (request.user.is_superuser or can_determine_company):
            logger.warning(
                f"AdminBasePerm: Add denied for non-SU '{request.user}' due to no company context for new {self.model._meta.verbose_name}.")
            return False
        return True

    def get_actions(self, request: HttpRequest) -> Dict[str, Any]:
        actions = super().get_actions(request)
        if hasattr(self.model, '_safedelete_policy') and hasattr(self.model, 'all_objects_including_deleted'):
            action_defs = {
                'action_soft_delete_selected': _("Soft delete selected %(verbose_name_plural)s"),
                'action_undelete_selected': _("Restore (undelete) selected %(verbose_name_plural)s")
            }
            for name, desc_template in action_defs.items():
                if name not in actions and hasattr(self, name):  # Check if method exists
                    actions[name] = (
                        getattr(self, name), name,
                        desc_template % {'verbose_name_plural': self.opts.verbose_name_plural}
                    )
        else:  # Remove if model doesn't support them
            actions.pop('action_soft_delete_selected', None);
            actions.pop('action_undelete_selected', None)
        return actions

    @admin.action(description=_("Soft delete selected items"))
    def action_soft_delete_selected(self, request: HttpRequest, queryset: QuerySet):
        # ... (refined soft delete logic as per previous robust version, with logging) ...
        processed_count, skipped_count = 0, 0;
        items_to_delete = []
        for obj in queryset:
            if self.has_delete_permission(request, obj):
                items_to_delete.append(obj)
            else:
                skipped_count += 1

        if not items_to_delete:
            if skipped_count:
                messages.warning(request, _("%(count)d item(s) skipped: no permission.") % {'count': skipped_count})
            else:
                messages.info(request, _("No items selected or eligible for soft deletion.")); return
        try:
            with transaction.atomic():
                for obj in items_to_delete: obj.delete(force_soft=True); processed_count += 1
                logger.info(
                    f"AdminBase Action: User '{request.user}' soft-deleted {processed_count} {self.model._meta.verbose_name_plural}.")
        except Exception as e:
            logger.exception(f"AdminBase Action: Error during batch soft-delete for user '{request.user}'.")
            messages.error(request, _("Error during batch soft-delete: %(err)s") % {'err': str(e)});
            return
        if processed_count: messages.success(request, _("%(count)d items soft-deleted.") % {'count': processed_count})
        if skipped_count: messages.warning(request,
                                           _("%(count)d items skipped: no permission.") % {'count': skipped_count})

    @admin.action(description=_("Restore selected items"))
    def action_undelete_selected(self, request: HttpRequest, queryset: QuerySet):
        # ... (refined undelete logic as per previous robust version, with logging) ...
        sd_field = getattr(self.model._meta, 'safedelete_field_name', 'deleted_at')
        eligible_pks = list(queryset.values_list('pk', flat=True))  # Get PKs from initial queryset
        if not eligible_pks: messages.info(request, _("No items selected for restore.")); return

        # Fetch from all_objects_including_deleted to ensure we can target soft-deleted ones
        items_to_consider_qs = self.model.all_objects_including_deleted.filter(
            pk__in=eligible_pks, **{f"{sd_field}__isnull": False}
        )
        processed_count, skipped_count = 0, 0;
        actual_items_to_restore = []
        for obj in items_to_consider_qs:
            if self.has_change_permission(request, obj):
                actual_items_to_restore.append(obj)  # Use change perm for restore
            else:
                skipped_count += 1

        if not actual_items_to_restore:
            if skipped_count:
                messages.warning(request,
                                 _("%(count)d items skipped: no permission to restore.") % {'count': skipped_count})
            else:
                messages.warning(request, _("No selected items were eligible for restore.")); return
        try:
            with transaction.atomic():
                for obj in actual_items_to_restore: obj.undelete(); processed_count += 1
                logger.info(
                    f"AdminBase Action: User '{request.user}' restored {processed_count} {self.model._meta.verbose_name_plural}.")
        except Exception as e:
            logger.exception(f"AdminBase Action: Error during batch restore for user '{request.user}'.")
            messages.error(request, _("Error during batch restore: %(err)s") % {'err': str(e)});
            return
        if processed_count: messages.success(request, _("%(count)d items restored.") % {'count': processed_count})
        if skipped_count: messages.warning(request, _("%(count)d items skipped: no permission to restore.") % {
            'count': skipped_count})
