# company/managers.py
import logging
from django.db import models
from .utils import get_current_company  # Relies on utils.py to provide the Company instance
from .models import Company  # For type checking in create method

logger = logging.getLogger(__name__)


class CompanyManager(models.Manager):
    """
    Filters querysets by the current company from thread-local context.
    Assumes models using it have a 'company' ForeignKey to the Company model.
    """
    _allow_unfiltered_global_access = False  # Default: strict tenant isolation

    def get_queryset(self):
        queryset = super().get_queryset()
        company = get_current_company()  # Fetches Company instance or None

        if company:
            if not hasattr(self.model, 'company'):
                logger.error(f"CompanyManager on {self.model.__name__} which lacks 'company' field.")
                return queryset.none()  # Prevent accidental data leakage

            logger.debug(
                f"CompanyManager: Filtering {self.model.__name__} by Company '{company.name}' (ID: {company.id})")
            return queryset.filter(company=company)
        else:
            # No company in context
            logger.debug(f"CompanyManager: No active company in context for {self.model.__name__}.")
            if self._allow_unfiltered_global_access:
                logger.warning(
                    f"CompanyManager: Unfiltered queryset for {self.model.__name__} (global access allowed and no company context).")
                return queryset  # Superuser/system task access
            else:
                logger.debug(
                    f"CompanyManager: Empty queryset for {self.model.__name__} (no company context, global access disallowed).")
                return queryset.none()  # Strict isolation

    def create(self, **kwargs):
        """
        Automatically sets the 'company' field on new objects if:
        1. 'company' is not already in kwargs.
        2. A current company context exists (via get_current_company()).
        3. The model has a 'company' ForeignKey to the Company model.
        """
        company_from_context = get_current_company()

        if 'company' not in kwargs:  # If company not explicitly passed
            if company_from_context:
                if hasattr(self.model, 'company'):
                    company_field = self.model._meta.get_field('company')
                    if company_field.is_relation and company_field.related_model == Company:
                        kwargs['company'] = company_from_context
                    else:
                        logger.error(
                            f"CompanyManager: {self.model.__name__}.company is not FK to Company. Cannot auto-assign.")
                        raise ValueError(f"{self.model.__name__}.company misconfigured for CompanyManager.")
                else:
                    logger.error(f"CompanyManager: {self.model.__name__} has no 'company' field. Cannot auto-assign.")
                    raise ValueError(f"{self.model.__name__} missing 'company' field for CompanyManager.")
            elif not self._allow_unfiltered_global_access and hasattr(self.model, 'company'):
                # No company context, not global access, and 'company' is needed
                logger.error(
                    f"CompanyManager: Cannot create {self.model.__name__} without Company context or explicit 'company' kwarg.")
                raise ValueError(f"Create {self.model.__name__}: No company context and 'company' not provided.")

        return super().create(**kwargs)


class UnfilteredCompanyManager(CompanyManager):
    """
    A manager that allows access to all company data, bypassing tenant filtering.
    USE WITH EXTREME CAUTION, typically for superuser admin views or system tasks.
    """
    _allow_unfiltered_global_access = True