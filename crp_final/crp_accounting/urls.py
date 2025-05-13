# crp_accounting/urls_api.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

# --- Import DRF API Views (now tenant-aware) ---
from .views import coa as coa_views, TrialBalanceView, ProfitLossView, BalanceSheetView
from .views import journal as journal_views
from .views import party as party_views
from .views import period as period_views
# Assuming report API views are in views/reports.py

# --- Import Custom Admin Views (Function-based or custom AdminSite views) ---
# These might need adjustments to be fully tenant-aware if not already.
# If they are function-based views, they'll need to manually use request.company.
# If they are part of a custom AdminSite, that site needs to be company-aware.
from .admin_views import (
    admin_trial_balance_view,
    admin_profit_loss_view,
    admin_balance_sheet_view,
    admin_account_ledger_view,
    download_trial_balance_excel,
    download_profit_loss_excel,
    download_balance_sheet_excel,
    download_trial_balance_pdf,
    download_profit_loss_pdf,
    download_balance_sheet_pdf,
)

# --- Router Setup (for DRF ViewSets) ---
router = DefaultRouter()

# COA related
router.register(r'account-groups', coa_views.AccountGroupViewSet, basename='accountgroup-api')
router.register(r'accounts', coa_views.AccountViewSet, basename='account-api')

# Journal, Party, Period related
router.register(r'vouchers', journal_views.VoucherViewSet, basename='voucher-api')
router.register(r'parties', party_views.PartyViewSet, basename='party-api')
router.register(r'fiscal-years', period_views.FiscalYearViewSet, basename='fiscalyear-api')
router.register(r'accounting-periods', period_views.AccountingPeriodViewSet, basename='accountingperiod-api')


app_name = 'crp_accounting_api' # Can be 'crp_accounting' if you prefer consistency


account_pk_converter = 'uuid:account_pk'

# --- URL Patterns ---
urlpatterns = [
    # ====================================================
    # 1. DRF API URLs (Scoped under this app's root)
    #    These views inherit CompanyScoped mixins.
    # ====================================================
    path('', include(router.urls)), # Includes all ViewSet URLs registered above

    # Specific API paths for non-ViewSet COA views
    path(
        f'accounts/<{account_pk_converter}>/balance/',
        coa_views.AccountBalanceAPIView.as_view(),
        name='account-balance-api' # Changed name for clarity
    ),
    path(
        f'accounts/<{account_pk_converter}>/ledger/',
        coa_views.AccountLedgerAPIView.as_view(),
        name='account-ledger-api' # Changed name for clarity
    ),

    # Report API Endpoints (assuming these are DRF APIViews using CompanyScopedAPIViewMixin)
        path('api/reports/trial-balance/', TrialBalanceView.as_view(), name='api_report_trial_balance'),
        path('api/reports/profit-loss/', ProfitLossView.as_view(), name='api_report_profit_loss'),
        path('api/reports/balance-sheet/', BalanceSheetView.as_view(), name='api_report_balance_sheet'),
    # path('reports/cash-flow/', report_views.CashFlowAPIView.as_view(), name='report-cash-flow-api'), # Example

    # ============================================================================
    # 2. Custom Admin-Related URLs (e.g., for HTML reports within admin theme)
    #    These are NOT DRF APIs. Their tenant awareness depends on their implementation.
    #    If included here, they will be prefixed by whatever prefix you give
    #    to 'crp_accounting.urls_api' in your project's urls.py.
    #    Consider moving these to a separate crp_accounting/urls_admin.py if you
    #    want a different top-level prefix (e.g., /admin-extra/accounting/).
    # ============================================================================
    path('admin-reports/trial-balance/', admin_trial_balance_view, name='admin-view-trial-balance'), # Renamed for clarity
    path('admin-reports/trial-balance/excel/', download_trial_balance_excel, name='admin-download-trial-balance-excel'),
    path('admin-reports/trial-balance/pdf/', download_trial_balance_pdf, name='admin-download-trial-balance-pdf'),

    path('admin-reports/profit-loss/', admin_profit_loss_view, name='admin-view-profit-loss'),
    path('admin-reports/profit-loss/excel/', download_profit_loss_excel, name='admin-download-profit-loss-excel'),
    path('admin-reports/profit-loss/pdf/', download_profit_loss_pdf, name='admin-download-profit-loss-pdf'),

    path('admin-reports/balance-sheet/', admin_balance_sheet_view, name='admin-view-balance-sheet'),
    path('admin-reports/balance-sheet/excel/', download_balance_sheet_excel, name='admin-download-balance-sheet-excel'),
    path('admin-reports/balance-sheet/pdf/', download_balance_sheet_pdf, name='admin-download-balance-sheet-pdf'),

    # Using UUID for admin ledger view as well, assuming Account PK is UUID
    path(f'admin-reports/account/<{account_pk_converter}>/ledger/', admin_account_ledger_view, name='admin-view-account-ledger'),
]
# # crp_accounting/urls.py (Correct - Uses Original View Names inheriting Mixins)
#
# from django.urls import path, include
# from rest_framework.routers import DefaultRouter
#
# # --- Import DRF API Views (which now inherit CompanyScoped Mixins) ---
# from .views.coa import (
#     AccountGroupViewSet, AccountViewSet,
#     AccountBalanceView, AccountLedgerView
# )
# from .views.journal import VoucherViewSet
# from .views.party import PartyViewSet
# from .views.period import FiscalYearViewSet, AccountingPeriodViewSet
# # --- Import Report Views (using original names, inheriting Mixin) ---
# from .views.trial_balance import TrialBalanceView
# from .views.profit_loss import ProfitLossView
# from .views.balance_sheet import BalanceSheetView
#
# # --- Import Custom Admin Views (Function-based, handle company via query param) ---
# from .admin_views import (
#     admin_trial_balance_view,
#     admin_profit_loss_view,
#     admin_balance_sheet_view,
#     admin_account_ledger_view,
#     download_trial_balance_excel,
#     download_profit_loss_excel,
#     download_balance_sheet_excel,
#     download_trial_balance_pdf,
#     download_profit_loss_pdf,
#     download_balance_sheet_pdf,
# )
#
# # --- Router Setup (for DRF ViewSets) ---
# router = DefaultRouter()
# # Register tenant-aware ViewSets (inheriting CompanyScopedViewSetMixin)
# router.register(r'account-groups', AccountGroupViewSet, basename='accountgroup')
# router.register(r'accounts', AccountViewSet, basename='account')
# router.register(r'vouchers', VoucherViewSet, basename='voucher')
# router.register(r'parties', PartyViewSet, basename='party')
# router.register(r'fiscal-years', FiscalYearViewSet, basename='fiscal-year')
# router.register(r'accounting-periods', AccountingPeriodViewSet, basename='accounting-period')
#
# # --- App Namespace ---
# app_name = 'crp_accounting'
#
# # --- URL Patterns ---
# urlpatterns = [
#     # ==========================================
#     # 1. DRF API URLs (Scoped under /api/)
#     #    Uses Views/ViewSets inheriting CompanyScoped mixins
#     # ==========================================
#     path('api/', include(router.urls)), # ViewSet URLs
#     # Specific API paths
#     path('api/accounts/<int:account_pk>/balance/', AccountBalanceView.as_view(), name='api_account_balance'),
#     path('api/accounts/<int:account_pk>/ledger/', AccountLedgerView.as_view(), name='api_account_ledger'),
#     # Report API Endpoints (using original view names inheriting mixin)
#     path('api/reports/trial-balance/', TrialBalanceView.as_view(), name='api_report_trial_balance'),
#     path('api/reports/profit-loss/', ProfitLossView.as_view(), name='api_report_profit_loss'),
#     path('api/reports/balance-sheet/', BalanceSheetView.as_view(), name='api_report_balance_sheet'),
#     # path('api/reports/cash-flow/', CashFlowView.as_view(), name='api_report_cash_flow'),
#
#
#     # =============================================================
#     # 2. Custom Admin URLs (Scoped under /admin-extra/)
#     #    Uses function-based views from admin_views.py (expecting company_id query param)
#     # =============================================================
#     path('admin-extra/reports/trial-balance/', admin_trial_balance_view, name='admin_report_trial_balance'),
#     path('admin-extra/reports/trial-balance/excel/', download_trial_balance_excel, name='admin_report_trial_balance_excel'),
#     path('admin-extra/reports/trial-balance/pdf/', download_trial_balance_pdf, name='admin_report_trial_balance_pdf'),
#
#     path('admin-extra/reports/profit-loss/', admin_profit_loss_view, name='admin_report_profit_loss'),
#     path('admin-extra/reports/profit-loss/excel/', download_profit_loss_excel, name='admin_report_profit_loss_excel'),
#     path('admin-extra/reports/profit-loss/pdf/', download_profit_loss_pdf, name='admin_report_profit_loss_pdf'),
#
#     path('admin-extra/reports/balance-sheet/', admin_balance_sheet_view, name='admin_report_balance_sheet'),
#     path('admin-extra/reports/balance-sheet/excel/', download_balance_sheet_excel, name='admin_report_balance_sheet_excel'),
#     path('admin-extra/reports/balance-sheet/pdf/', download_balance_sheet_pdf, name='admin_report_balance_sheet_pdf'),
#
#     path('admin-extra/account/<int:account_pk>/ledger/', admin_account_ledger_view, name='admin_report_account_ledger'),
#     # Add download URLs for Ledger if needed
# ]
# # from django.urls import path, include
# # from rest_framework.routers import DefaultRouter
# #
# # # --- Import DRF Views ---
# # from .views import TrialBalanceView, BalanceSheetView # DRF API Views
# # from .views.coa import (
# #     AccountGroupViewSet, AccountViewSet,
# #     AccountBalanceView, AccountLedgerView
# # )
# # from .views.journal import VoucherViewSet
# # from .views.party import PartyViewSet
# # from .views.period import FiscalYearViewSet, AccountingPeriodViewSet
# # from .views.profit_loss import ProfitLossView # DRF API View
# #
# # # --- Import Custom Admin Views (HTML Display & Downloads) ---
# # from .admin_views import (
# #     admin_trial_balance_view,
# #     admin_profit_loss_view,
# #     admin_balance_sheet_view,
# #     download_trial_balance_excel,  # <<< New Excel Download View
# #     download_profit_loss_excel,  # <<< New Excel Download View
# #     download_balance_sheet_excel,  # <<< New Excel Download View
# #     download_trial_balance_pdf,  # <<< New PDF Download View
# #     download_profit_loss_pdf,  # <<< New PDF Download View
# #     download_balance_sheet_pdf, admin_account_ledger_view  # <<< New PDF Download View
# # )
# #
# # # --- Router Setup (for DRF ViewSets) ---
# # router = DefaultRouter()
# # router.register(r'account-groups', AccountGroupViewSet, basename='accountgroup')
# # router.register(r'accounts', AccountViewSet, basename='account')
# # router.register(r'vouchers', VoucherViewSet, basename='voucher')
# # router.register(r'parties', PartyViewSet, basename='party')
# # router.register(r'fiscal-years', FiscalYearViewSet, basename='fiscal-year')
# # router.register(r'accounting-periods', AccountingPeriodViewSet, basename='accounting-period')
# #
# # # --- App Namespace (Used in reverse() calls) ---
# # app_name = 'crp_accounting_api'
# #
# # # --- URL Patterns ---
# # urlpatterns = [
# #     # ==========================================
# #     # 1. DRF API URLs (Prefixed with /api/)
# #     # ==========================================
# #     path('api/', include(router.urls)), # ViewSet URLs
# #     path('api/accounts/<int:account_pk>/balance/', AccountBalanceView.as_view(), name='account-balance'),
# #     path('api/accounts/<int:account_pk>/ledger/', AccountLedgerView.as_view(), name='account-ledger'),
# #     # --- DRF Report API Endpoints ---
# #     path('api/reports/trial-balance/', TrialBalanceView.as_view(), name='report-trial-balance'),
# #     path('api/reports/profit-loss/', ProfitLossView.as_view(), name='report-profit-loss'),
# #     path('api/reports/balance-sheet/', BalanceSheetView.as_view(), name='balance-sheet-report'),
# #     # path('api/reports/cash-flow/', CashFlowView.as_view(), name='report-cash-flow'), # Example for future
# #
# #     # =============================================================
# #     # 2. Custom Admin Report URLs (HTML Views & Downloads)
# #     #    Prefixed with /admin-reports/
# #     # =============================================================
# #
# #     # --- Trial Balance ---
# #     path(
# #         'admin-reports/trial-balance/',
# #         admin_trial_balance_view,
# #         name='admin_report_trial_balance' # HTML view
# #     ),
# #     path(
# #         'admin-reports/trial-balance/excel/',
# #         download_trial_balance_excel,
# #         name='admin_report_trial_balance_excel' # Excel download
# #     ),
# #     path(
# #         'admin-reports/trial-balance/pdf/',
# #         download_trial_balance_pdf,
# #         name='admin_report_trial_balance_pdf' # PDF download
# #     ),
# #
# #     # --- Profit & Loss ---
# #     path(
# #         'admin-reports/profit-loss/',
# #         admin_profit_loss_view,
# #         name='admin_report_profit_loss' # HTML view
# #     ),
# #     path(
# #         'admin-reports/profit-loss/excel/',
# #         download_profit_loss_excel,
# #         name='admin_report_profit_loss_excel' # Excel download
# #     ),
# #     path(
# #         'admin-reports/profit-loss/pdf/',
# #         download_profit_loss_pdf,
# #         name='admin_report_profit_loss_pdf' # PDF download
# #     ),
# #
# #     # --- Balance Sheet ---
# #     path(
# #         'admin-reports/balance-sheet/',
# #         admin_balance_sheet_view,
# #         name='admin_report_balance_sheet' # HTML view
# #     ),
# #     path(
# #         'admin-reports/balance-sheet/excel/',
# #         download_balance_sheet_excel,
# #         name='admin_report_balance_sheet_excel' # Excel download
# #     ),
# #     path(
# #         'admin-reports/balance-sheet/pdf/',
# #         download_balance_sheet_pdf,
# #         name='admin_report_balance_sheet_pdf' # PDF download
# #     ),
# # path(
# #         'admin-reports/account/<int:account_pk>/ledger/', # <<< New Ledger URL
# #         admin_account_ledger_view,
# #         name='admin_report_account_ledger'
# #     ),
# # ]
# # # # crp_accounting/urls.py
# # #
# # # from django.urls import path, include
# # # from rest_framework.routers import DefaultRouter
# # #
# # # from .views import TrialBalanceView, BalanceSheetView
# # # # --- Import ALL necessary views ---
# # # # Import from the specific view modules based on your structure
# # # from .views.coa import (
# # #     AccountGroupViewSet,
# # #     AccountViewSet,
# # #     AccountBalanceView,
# # #     AccountLedgerView
# # # )
# # # from .views.journal import VoucherViewSet
# # # from .views.party import PartyViewSet
# # # from .views.period import FiscalYearViewSet, AccountingPeriodViewSet
# # # # --- Import from the reports views ---
# # #
# # # from .views.profit_loss import ProfitLossView # <<< IMPORT the new view
# # #
# # # # --- Router Setup ---
# # # router = DefaultRouter()
# # # # ... (router registrations remain the same) ...
# # # router.register(r'account-groups', AccountGroupViewSet, basename='accountgroup')
# # # router.register(r'accounts', AccountViewSet, basename='account')
# # # router.register(r'vouchers', VoucherViewSet, basename='voucher')
# # # router.register(r'parties', PartyViewSet, basename='party')
# # # router.register(r'fiscal-years', FiscalYearViewSet, basename='fiscal-year')
# # # router.register(r'accounting-periods', AccountingPeriodViewSet, basename='accounting-period')
# # #
# # #
# # # # --- App Namespace ---
# # # app_name = 'crp_accounting_api'
# # #
# # # # --- URL Patterns ---
# # # urlpatterns = [
# # #     # 1. Include all URLs generated by the router for the ViewSets
# # #     path('', include(router.urls)),
# # #
# # #     # 2. Add specific paths for non-ViewSet COA views (Ledger/Balance)
# # #     path('accounts/<int:account_pk>/balance/', AccountBalanceView.as_view(), name='account-balance'),
# # #     path('accounts/<int:account_pk>/ledger/', AccountLedgerView.as_view(), name='account-ledger'),
# # #
# # #     # 3. Add specific paths for Report views
# # #     path(
# # #         'reports/trial-balance/',
# # #         TrialBalanceView.as_view(),
# # #         name='report-trial-balance'
# # #     ),
# # #     # --- ADDED URL pattern for Profit & Loss ---
# # #     path(
# # #         'reports/profit-loss/',          # URL path for the P&L report
# # #         ProfitLossView.as_view(),        # The view class instance
# # #         name='report-profit-loss'        # URL name for reversing
# # #     ),
# # #     path(
# # #         'reports/balance-sheet/',  # <<< The URL endpoint
# # #         BalanceSheetView.as_view(),  # <<< Link to the view class
# # #         name='balance-sheet-report'  # <<< Unique name for reversing
# # #     ),
# # #     # path('reports/cash-flow/', CashFlowView.as_view(), name='report-cash-flow'),
# # #
# # # ]