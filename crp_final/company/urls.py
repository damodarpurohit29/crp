# company/urls_api.py
from django.urls import path, include
from rest_framework_nested import routers # For nested routes like /companies/{pk}/members/
from .views import CompanyViewSet, CompanyGroupViewSet, CompanyMembershipViewSet

# Primary router for top-level resources
router = routers.DefaultRouter()
router.register(r'groups', CompanyGroupViewSet, basename='companygroup') # /api/company/groups/
router.register(r'companies', CompanyViewSet, basename='company')     # /api/company/companies/

# Nested router for CompanyMemberships under a specific Company
# This will generate URLs like: /api/company/companies/{company_pk}/members/
companies_router = routers.NestedDefaultRouter(router, r'companies', lookup='company')
companies_router.register(r'members', CompanyMembershipViewSet, basename='company-membership')
# The `lookup` parameter 'company' corresponds to the `company_pk` argument expected by CompanyMembershipViewSet.

app_name = 'company_api' # Namespace for these API URLs

urlpatterns = [
    path('', include(router.urls)),
    path('', include(companies_router.urls)),
    # You can add other non-viewset API paths here if needed
]

# Example generated URLs:
# /api/company/groups/
# /api/company/groups/{group_pk}/
# /api/company/companies/
# /api/company/companies/{company_pk}/
# /api/company/companies/{company_pk}/members/  (List members of a company, Create member for a company)
# /api/company/companies/{company_pk}/members/{membership_pk}/ (Retrieve, Update, Delete a specific membership)
# /api/company/companies/{company_pk}/members/  <-- This is a bit redundant with the previous list/create if using DefaultRouter for nested.
#                                                 rest_framework_nested handles this well usually.
#                                                 The 'members' action on CompanyViewSet also provides /api/company/companies/{pk}/members/

# Note on the "members" action in CompanyViewSet vs. CompanyMembershipViewSet:
# - CompanyViewSet's `@action(detail=True, methods=['get'], url_path='members')` gives you:
#   GET /api/company/companies/{company_pk}/members/ (to list members)
# - CompanyMembershipViewSet (nested) gives you:
#   GET /api/company/companies/{company_pk}/members/ (list)
#   POST /api/company/companies/{company_pk}/members/ (create)
#   GET /api/company/companies/{company_pk}/members/{membership_pk}/ (retrieve)
#   PUT/PATCH /api/company/companies/{company_pk}/members/{membership_pk}/ (update)
#   DELETE /api/company/companies/{company_pk}/members/{membership_pk}/ (delete)
# You might choose to use one over the other or keep both if they serve slightly different permission models or data.
# For full CRUD on memberships nested under a company, the CompanyMembershipViewSet is more standard.
# The action on CompanyViewSet is good for a quick read-only list.