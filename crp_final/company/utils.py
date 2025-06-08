# company/utils.py
import contextvars
import logging
from typing import Optional, Union  # For type hinting
from django.http import HttpRequest

# Company model is imported locally within functions to avoid circular dependencies

logger = logging.getLogger(__name__)

# Use contextvars for both sync and async safety
current_company_context_var: contextvars.ContextVar[Optional['Company']] = contextvars.ContextVar(
    "current_company_context_var",
    default=None
)


# --- Custom Exception ---
class CompanySelectionRequired(Exception):
    """
    Raised when a user is associated with multiple effectively active companies
    but no specific company context (e.g., via session or default) has been established,
    requiring explicit user selection.
    """
    pass


# --- Session Key Management (Internal Helpers) ---
def _get_superuser_acting_as_company_session_key() -> str:
    """Returns the session key used for storing a superuser's 'acting as' company ID."""
    return "superuser_acting_as_company_id"


def _get_user_active_company_session_key(user_id: int) -> str:
    """
    Returns the session key for storing a regular user's explicitly selected active company ID.

    Args:
        user_id: The ID of the user.
    """
    return f"user_{user_id}_active_company_id"


# --- Core ContextVar Management (Public Interface) ---
def set_current_company(company_instance: Optional['Company']) -> None:
    """
    Sets the current company in the context variable for the current execution flow.
    This is typically called by middleware after identifying the company.

    Args:
        company_instance: The Company model instance to set as current, or None to clear.
    """
    current_company_context_var.set(company_instance)
    if company_instance:
        logger.debug(
            f"utils.set_current_company: Context set to Company '{getattr(company_instance, 'name', 'N/A')}' (ID: {getattr(company_instance, 'pk', 'N/A')})")
    else:
        logger.debug("utils.set_current_company: Context cleared (set to None)")


def clear_current_company() -> None:
    """
    Clears/resets the current company in the context variable to its default (None).
    Typically called by middleware after a request has been processed.
    """
    current_company_context_var.set(None)
    logger.debug("utils.clear_current_company: Context reset to default (None).")


def get_current_company() -> Optional['Company']:
    """
    Retrieves the current company from the context variable.
    This is the primary function used by CompanyManager and other parts of the
    application logic to access the company established for the current request/context.

    Returns:
        The active Company model instance, or None if no company context is set.
    """
    return current_company_context_var.get()


# --- Helper Functions for get_company_for_user_context (Internal Logic) ---
def _get_company_from_superuser_session(request: HttpRequest) -> Optional['Company']:
    """
    Internal helper: Retrieves the company a superuser is 'acting as',
    based on a company ID stored in their session. Validates that the
    company exists and is effectively active.

    Args:
        request: The current HttpRequest object.

    Returns:
        An active Company instance if found and valid, otherwise None.
    """
    from .models import Company  # Local import

    if not request.user.is_superuser:
        return None

    session_key = _get_superuser_acting_as_company_session_key()
    acting_as_company_id_str = request.session.get(session_key)

    if not acting_as_company_id_str:
        return None

    try:
        company_id = int(acting_as_company_id_str)
        company = Company.objects.filter(pk=company_id).first()
        if company and company.effective_is_active:
            return company

        if company:
            logger.warning(
                f"Superuser attempting to act as inactive Company ID {company_id} ('{company.name}'). Clearing session key.")
        else:
            logger.warning(f"Superuser acting_as_company_id {company_id} not found. Clearing session key.")

        if session_key in request.session:
            del request.session[session_key]
            request.session.modified = True
    except ValueError:
        logger.warning(f"Invalid superuser_acting_as_company_id '{acting_as_company_id_str}' in session. Clearing.")
        if session_key in request.session:
            del request.session[session_key]
            request.session.modified = True
    return None


def _get_company_from_user_session(request: HttpRequest, active_memberships_qs) -> Optional['Company']:
    """
    Internal helper: Retrieves the company a regular user has explicitly selected
    and stored in their session. Validates that the company is still accessible
    through their active memberships and is effectively active.

    Args:
        request: The current HttpRequest object.
        active_memberships_qs: A queryset of the user's active CompanyMembership objects,
                               expected to have `select_related('company')`.

    Returns:
        An active Company instance if found and valid, otherwise None.
    """
    if request.user.is_superuser:
        return None

    session_key = _get_user_active_company_session_key(request.user.pk)
    user_selected_company_id_str = request.session.get(session_key)

    if not user_selected_company_id_str:
        return None

    try:
        selected_id = int(user_selected_company_id_str)
        membership = active_memberships_qs.filter(company_id=selected_id).first()
        if membership and membership.company.effective_is_active:
            return membership.company

        logger.warning(
            f"User {request.user.username} session-selected company ID {selected_id} no longer valid/active. Clearing session key.")
        if session_key in request.session:
            del request.session[session_key]
            request.session.modified = True
    except ValueError:
        logger.warning(
            f"User {request.user.username} active_company_id '{user_selected_company_id_str}' is not valid int. Clearing session key.")
        if session_key in request.session:
            del request.session[session_key]
            request.session.modified = True
    return None


def _get_default_or_only_active_company(request: HttpRequest, active_memberships_qs) -> Union[Optional['Company'], str]:
    """
    Internal helper: Attempts to find a company context for a regular user by
    first checking for a default company, then for a single effectively active company.
    If multiple active companies exist without a default, it returns a special marker string.

    Args:
        request: The current HttpRequest object.
        active_memberships_qs: A queryset of the user's active CompanyMembership objects,
                               expected to have `select_related('company')`.

    Returns:
        An active Company instance, None, or the string "MULTIPLE_ACTIVE_COMPANIES_NO_SELECTION".
    """
    if request.user.is_superuser:
        return None

    default_membership = active_memberships_qs.filter(is_default_for_user=True).first()
    if default_membership and default_membership.company.effective_is_active:
        request.session[_get_user_active_company_session_key(request.user.pk)] = default_membership.company.pk
        request.session.modified = True
        return default_membership.company

    effectively_active_companies = [
        mem.company for mem in active_memberships_qs if mem.company.effective_is_active
    ]

    if len(effectively_active_companies) == 1:
        single_company = effectively_active_companies[0]
        request.session[_get_user_active_company_session_key(request.user.pk)] = single_company.pk
        request.session.modified = True
        return single_company

    if len(effectively_active_companies) > 1:
        return "MULTIPLE_ACTIVE_COMPANIES_NO_SELECTION"

    return None


# --- Main Request-Based Company Identification Logic (Public Interface) ---
def get_company_for_user_context(request: HttpRequest) -> Optional['Company']:
    """
    Determines an appropriate company context for a logged-in user based on their
    memberships, session settings, and default preferences. This function is typically
    used in scenarios where company identification is not handled by other means
    (e.g., subdomain routing middleware).

    The order of determination is:
    1. For Superusers: Checks if they are 'acting as' a specific company via session.
    2. For Regular Users:
        a. Checks for an explicitly selected company in their session.
        b. Checks for a company marked as their default (`is_default_for_user=True`).
        c. If no default, checks if they belong to only one effectively active company.

    Args:
        request: The current HttpRequest object.

    Returns:
        An active Company model instance if a single, unambiguous context can be
        determined, otherwise None.

    Raises:
        CompanySelectionRequired: If the user has multiple effectively active company
                                  memberships but none are explicitly selected or
                                  marked as default, indicating that the user needs
                                  to choose a company context.
    """
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return None

    company = _get_company_from_superuser_session(request)
    if company is not None:
        return company

    if request.user.is_superuser:  # Superuser not "acting as" anyone
        return None

    active_memberships = request.user.company_memberships.filter(
        is_active_membership=True
    ).select_related('company')

    if not active_memberships.exists():
        logger.debug(f"User {request.user.username} has no active company memberships.")
        return None

    company = _get_company_from_user_session(request, active_memberships)
    if company is not None:
        return company

    company_or_marker = _get_default_or_only_active_company(request, active_memberships)

    if company_or_marker == "MULTIPLE_ACTIVE_COMPANIES_NO_SELECTION":
        logger.info(f"User {request.user.username} has multiple active companies. Selection required.")
        raise CompanySelectionRequired(
            f"User {request.user.username} has multiple active companies. A selection is required."
        )

    from .models import Company  # Local import for isinstance check
    if isinstance(company_or_marker, Company) or company_or_marker is None:
        return company_or_marker  # This will be Company or None

    # Should not be reached if logic above is correct
    logger.error(
        f"Unexpected state in get_company_for_user_context for user {request.user.username}. Marker: {company_or_marker}")
    return None


# --- Test Utility Context Manager (Public Interface) ---
class override_current_company:
    """
    A context manager for temporarily overriding the current company context,
    primarily intended for use in automated tests.

    This allows tests to simulate different company contexts without needing
    to manipulate HTTP requests or sessions directly for `get_current_company()`.

    Usage:
        from .models import Company
        from .utils import override_current_company, get_current_company

        test_company = Company.objects.get(pk=1)
        with override_current_company(test_company):
            # Code within this block will see test_company when calling get_current_company()
            assert get_current_company() == test_company

        # Outside the block, the original context is restored.
        assert get_current_company() == original_context_company_or_none
    """

    def __init__(self, company_instance: Optional['Company']):
        """
        Args:
            company_instance: The Company instance to set as current within the context,
                              or None to simulate no company context.
        """
        self.company_instance_to_set = company_instance
        self.original_company = None  # Will store the company context at entry

    def __enter__(self) -> Optional['Company']:
        """Called when entering the 'with' block. Sets the new company context."""
        self.original_company = get_current_company()
        set_current_company(self.company_instance_to_set)
        logger.debug(
            f"override_current_company: CONTEXT ENTER. Set company to '{getattr(self.company_instance_to_set, 'name', 'None')}'. Original was '{getattr(self.original_company, 'name', 'None')}'.")
        return self.company_instance_to_set

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'with' block. Restores the original company context."""
        set_current_company(self.original_company)
        logger.debug(
            f"override_current_company: CONTEXT EXIT. Restored company to '{getattr(self.original_company, 'name', 'None')}'.")
        # Do not suppress exceptions: return False or None implicitly to re-raise.