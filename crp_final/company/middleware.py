# company/middleware.py

import logging
from typing import Optional

from django.conf import settings
from django.http import Http404  # For raising 404
from django.urls import reverse, NoReverseMatch  # For reversing URLs
from django.shortcuts import redirect  # For redirect behavior
from django.utils.deprecation import MiddlewareMixin

# from django.utils.module_loading import import_string # If using string paths for utils in settings

# --- Model Imports ---
try:
    from .models import Company
except ImportError:
    Company = None  # type: ignore
    logging.critical(
        "CompanyMiddleware: Could not import Company model from .models. Middleware will not function at all.")

try:
    from .models import CompanyMembership  # Assuming CompanyMembership is in the same app's models.py
except ImportError:
    CompanyMembership = None  # type: ignore
    logging.warning(
        "CompanyMiddleware: CompanyMembership model not imported. User-based company context will be disabled.")

# --- Optional: Thread-local utilities (if you use them elsewhere) ---
# try:
#     from .utils import set_current_company_for_thread, clear_current_company_for_thread
# except ImportError:
#     set_current_company_for_thread = None
#     clear_current_company_for_thread = None
#     logging.debug("CompanyMiddleware: Thread-local company utilities not found or not configured.")


logger = logging.getLogger("company.middleware")  # Specific logger for this middleware

# --- Configuration fetched from settings (Define these in your settings.py) ---
BASE_DOMAIN = getattr(settings, 'BASE_DOMAIN', None)
NON_TENANT_SUBDOMAINS = getattr(settings, 'NON_TENANT_SUBDOMAINS',
                                ['www', 'api', 'admin', 'static', 'media'])  # Added common ones
MISSING_COMPANY_BEHAVIOR = getattr(settings, 'MISSING_COMPANY_BEHAVIOR',
                                   'raise_404')  # 'raise_404', 'redirect', 'ignore'
MISSING_COMPANY_REDIRECT_URL_NAME = getattr(settings, 'MISSING_COMPANY_REDIRECT_URL_NAME',
                                            None)  # e.g., 'public_homepage'


class CompanyMiddleware(MiddlewareMixin):
    """
    Identifies the current Company (tenant) for the request.
    Priority:
    1. Subdomain-based identification (for tenant-specific URLs).
    2. User-based identification (for authenticated users on central domains/APIs,
       based on their active/default CompanyMembership).
    Sets `request.company`.
    """

    def __init__(self, get_response):
        super().__init__(get_response)
        if Company is None:
            # This is a critical failure; middleware cannot function.
            raise ImportError("CompanyMiddleware: Company model is not available. Middleware cannot operate.")
        if not BASE_DOMAIN and not settings.DEBUG:
            # In production, BASE_DOMAIN is crucial for reliable subdomain matching.
            # In DEBUG, we might allow more flexible host matching for local development.
            logger.critical(
                "CompanyMiddleware: settings.BASE_DOMAIN is not configured. "
                "Subdomain matching will be unreliable or effectively disabled in production."
            )
        elif not BASE_DOMAIN and settings.DEBUG:
            logger.warning(
                "CompanyMiddleware: settings.BASE_DOMAIN is not configured. "
                "Subdomain matching will use fallback heuristics (less reliable)."
            )

    def _get_company_from_subdomain(self, host: str) -> Optional[Company]:
        """Attempts to identify a company based on the subdomain in the host."""
        log_prefix = f"[CoMiddleware][Host:{host}]"
        subdomain_prefix = None

        if BASE_DOMAIN and host.endswith(f".{BASE_DOMAIN}"):  # Ensure it's a subdomain of BASE_DOMAIN
            prefix = host[:-len(f".{BASE_DOMAIN}")]  # Get part before .BASE_DOMAIN
            if prefix and prefix not in NON_TENANT_SUBDOMAINS:
                subdomain_prefix = prefix
            else:
                logger.debug(f"{log_prefix} Host identified as non-tenant (root or excluded prefix '{prefix}').")
        elif not BASE_DOMAIN and settings.DEBUG:  # Fallback for DEBUG if no BASE_DOMAIN
            parts = host.split('.')
            # Heuristic: if more than 2 parts (e.g., sub.domain.com) or 2 parts if not common TLDs (e.g. sub.localhost)
            # This is tricky and less reliable without BASE_DOMAIN.
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in ['com', 'org', 'net', 'io', 'app', 'localhost']):
                potential_prefix = parts[0]
                if potential_prefix not in NON_TENANT_SUBDOMAINS:
                    subdomain_prefix = potential_prefix
                    logger.warning(
                        f"{log_prefix} Attempting subdomain match for '{subdomain_prefix}' without BASE_DOMAIN (DEBUG mode).")
            else:
                logger.debug(
                    f"{log_prefix} Host does not appear to be a clear tenant subdomain (BASE_DOMAIN not set, DEBUG mode).")
        elif not BASE_DOMAIN:  # No BASE_DOMAIN and not DEBUG - subdomain matching effectively disabled
            logger.debug(f"{log_prefix} Subdomain matching skipped: BASE_DOMAIN not set and not in DEBUG mode.")

        if subdomain_prefix:
            try:
                company_obj = Company.objects.get(subdomain_prefix__iexact=subdomain_prefix)  # Case-insensitive match

                # CRITICAL: Check if the company is effectively active AFTER fetching
                if hasattr(company_obj, 'effective_is_active') and not company_obj.effective_is_active:
                    logger.warning(
                        f"{log_prefix} Company for subdomain '{subdomain_prefix}' (Name: '{company_obj.name}') found but is NOT effectively active. Treating as not found.")
                    return None  # Treat inactive company as not found for subdomain routing
                elif not hasattr(company_obj, 'effective_is_active') and not getattr(company_obj, 'is_active', True):
                    # Fallback if no effective_is_active, just check is_active
                    logger.warning(
                        f"{log_prefix} Company for subdomain '{subdomain_prefix}' (Name: '{company_obj.name}') found but is_active=False. Treating as not found.")
                    return None

                logger.info(
                    f"{log_prefix} Identified Company '{company_obj.name}' (ID: {company_obj.id}) from subdomain '{subdomain_prefix}'.")
                return company_obj
            except Company.DoesNotExist:
                logger.warning(f"{log_prefix} No Company found for subdomain_prefix '{subdomain_prefix}'.")
                return None  # Explicitly return None, _handle_missing_company will be called by main process_request
            except Exception as e:
                logger.exception(
                    f"{log_prefix} Error looking up company for subdomain_prefix '{subdomain_prefix}': {e}")
                # Depending on policy, either raise Http404 or return None to let user-based logic try
                # For now, let's assume a DB error here is critical for subdomain routing.
                raise Http404("Error identifying company information from domain.")
        return None

    def _get_company_from_user(self, request) -> Optional[Company]:
        """Attempts to identify company based on authenticated user's memberships."""
        user = getattr(request, 'user', None)
        log_prefix = f"[CoMiddleware][User:{user.name if user and user.is_authenticated else 'Anon'}]"

        if not (user and user.is_authenticated and CompanyMembership):
            if user and user.is_authenticated and not CompanyMembership:
                logger.debug(f"{log_prefix} User-based company lookup skipped: CompanyMembership model not available.")
            return None

        try:
            # Prioritize default, active membership to an effectively active company
            membership = CompanyMembership.objects.select_related('company').filter(
                user=user,
                is_active_membership=True,
                company__is_active=True,
                # Assuming Company has is_active; add company__is_suspended_by_admin=False if separate
                # For more complex effective_is_active, you might need to filter company PKs first
                # or iterate results if `company__effective_is_active=True` isn't a direct DB query.
            ).order_by('-is_default_for_user', 'company__name').first()  # Default first, then by name

            if membership:
                # Double check the company's effective_is_active property if complex
                if hasattr(membership.company, 'effective_is_active') and not membership.company.effective_is_active:
                    logger.warning(
                        f"{log_prefix} User {user.name} has membership to Company '{membership.company.name}', but it's not effectively active.")
                    return None

                logger.info(
                    f"{log_prefix} Identified Company '{membership.company.name}' (ID: {membership.company.id}) from user's active/default membership.")
                return membership.company
            else:
                logger.debug(
                    f"{log_prefix} User {user.name} has no active/default membership to an effectively active company.")
                return None
        except Exception as e:
            logger.exception(f"{log_prefix} Error determining company from user membership for {user.name}: {e}")
            return None

    def process_request(self, request):
        request.company = None  # Initialize on request object
        host = request.get_host().split(':')[0].lower()
        log_prefix = f"[CoMiddleware][Path:{request.path}][Host:{host}][User:{request.user.name if request.user and request.user.is_authenticated else 'Anon'}]"

        # --- Attempt 1: Identify company from subdomain ---
        # This is useful for tenant-specific frontend URLs.
        company_from_subdomain = self._get_company_from_subdomain(host)

        # --- Special handling if a subdomain was detected but no active company found ---
        # This checks if the host structure *looked like* a tenant subdomain.
        # Crude check: if it's not BASE_DOMAIN and not www.BASE_DOMAIN (and BASE_DOMAIN is set)
        # A more precise `is_potential_tenant_host` flag could be set in `_get_company_from_subdomain`
        is_potential_tenant_subdomain_url = False
        if BASE_DOMAIN and host.endswith(f".{BASE_DOMAIN}"):
            prefix = host[:-len(f".{BASE_DOMAIN}")].rstrip('.')
            if prefix and prefix not in NON_TENANT_SUBDOMAINS:
                is_potential_tenant_subdomain_url = True
        # Add similar heuristic for no BASE_DOMAIN / DEBUG if desired

        if company_from_subdomain:
            request.company = company_from_subdomain
        elif is_potential_tenant_subdomain_url:  # A tenant subdomain was accessed but no active company found
            logger.warning(
                f"{log_prefix} A tenant-like subdomain was accessed ('{host}') but did not resolve to an active company. Applying MISSING_COMPANY_BEHAVIOR.")
            # Extract the attempted subdomain prefix again for _handle_missing_company
            attempted_subdomain_prefix = host.split('.')[0]  # Simplistic, refine if needed
            if BASE_DOMAIN and host.endswith(f".{BASE_DOMAIN}"):
                attempted_subdomain_prefix = host[:-len(f".{BASE_DOMAIN}")].rstrip('.')

            response_from_handler = self._handle_missing_company(request, attempted_subdomain_prefix)
            if response_from_handler:  # If handler returned a redirect or other response
                return response_from_handler
            # If 'ignore', request.company remains None, proceed to user-based.

        # --- Attempt 2: Identify company from authenticated user (if not found by subdomain) ---
        # This is crucial for APIs on a central domain or for logged-in users on the main site.
        if request.company is None:  # Only try user-based if subdomain didn't yield a company
            company_from_user = self._get_company_from_user(request)
            if company_from_user:
                request.company = company_from_user

        # --- Set thread-local if used ---
        # if set_current_company_for_thread:
        #     set_current_company_for_thread(request.company)

        if request.company:
            logger.info(
                f"{log_prefix} Final request.company set to: '{request.company.name}' (ID: {request.company.id})")
        else:
            logger.debug(f"{log_prefix} No company context established for this request. request.company is None.")
            # For certain paths (e.g., global signup, platform admin), this might be expected.
            # Your views (like BalanceSheetView) will then correctly deny access if company is required for non-SUs.

        return None  # Continue to next middleware/view

    def _handle_missing_company(self, request, subdomain_prefix_attempted: str):
        """
        Handles behavior when a company is not found for an accessed subdomain
        that appeared to be a tenant subdomain.
        """
        log_prefix = f"[CoMiddleware][Path:{request.path}][User:{request.user.name if request.user and request.user.is_authenticated else 'Anon'}]"

        if MISSING_COMPANY_BEHAVIOR == 'redirect':
            if MISSING_COMPANY_REDIRECT_URL_NAME:
                try:
                    redirect_url = reverse(MISSING_COMPANY_REDIRECT_URL_NAME)
                    logger.info(
                        f"{log_prefix} Redirecting missing company request for subdomain '{subdomain_prefix_attempted}' to URL '{MISSING_COMPANY_REDIRECT_URL_NAME}' ({redirect_url}).")
                    # You could add the attempted subdomain as a query param to the redirect if the target view can use it.
                    # from django.utils.http import urlencode
                    # params = urlencode({'attempted_subdomain': subdomain_prefix_attempted})
                    # return redirect(f"{redirect_url}?{params}")
                    return redirect(redirect_url)
                except NoReverseMatch:
                    logger.exception(
                        f"{log_prefix} CRITICAL: Failed to reverse redirect URL '{MISSING_COMPANY_REDIRECT_URL_NAME}' for missing company. Raising Http404 instead.")
                    raise Http404(
                        f"Company account for '{subdomain_prefix_attempted}' not found or is inactive, and system redirect failed.")
            else:
                logger.error(
                    f"{log_prefix} CRITICAL: MISSING_COMPANY_BEHAVIOR is 'redirect' but MISSING_COMPANY_REDIRECT_URL_NAME is not set in settings. Raising Http404.")
                raise Http404(f"Company account for '{subdomain_prefix_attempted}' not found or is inactive.")

        elif MISSING_COMPANY_BEHAVIOR == 'ignore':
            logger.info(
                f"{log_prefix} Behavior for missing company on subdomain '{subdomain_prefix_attempted}' is 'ignore'. Request will proceed with request.company=None.")
            return None  # Allow request to continue; view must handle request.company=None

        else:  # Default behavior is 'raise_404'
            logger.info(
                f"{log_prefix} Behavior for missing company on subdomain '{subdomain_prefix_attempted}' is 'raise_404'. Raising Http404.")
            raise Http404(f"Company account for '{subdomain_prefix_attempted}' not found or is inactive.")

    # def process_response(self, request, response): # If using thread locals
    #     if clear_current_company_for_thread:
    #         clear_current_company_for_thread()
    #     return response