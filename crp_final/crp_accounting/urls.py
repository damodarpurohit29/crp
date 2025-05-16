# crp_accounting/urls_api.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

# --- Import DRF API Views ---
from .views import coa as coa_views, TrialBalanceView, ProfitLossView, BalanceSheetView
from .views import journal as journal_views
from .views import party as party_views
from .views import period as period_views
# Assuming AR report API views would be in views/reports_ar.py or similar if you create them

# --- Import Custom Admin Views ---
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
    # New AR Admin View Imports
    admin_ar_aging_report_view,
    download_ar_aging_excel,
    download_ar_aging_pdf,
    admin_customer_statement_view,  # This view handles both base and detail
    download_customer_statement_pdf, admin_reports_hub_view, admin_vendor_statement_view, admin_ap_aging_report_view,
)

# --- Router Setup (for DRF ViewSets) ---
router = DefaultRouter()

# COA related
router.register(r'account-groups', coa_views.AccountGroupViewSet, basename='accountgroup-api')
router.register(r'accounts', coa_views.AccountViewSet, basename='account-api')

# Journal, Party, Period related
router.register(r'vouchers', journal_views.VoucherViewSet, basename='voucher-api')
router.register(r'parties', party_views.PartyViewSet, basename='party-api') # Parties include Customers
router.register(r'fiscal-years', period_views.FiscalYearViewSet, basename='fiscalyear-api')
router.register(r'accounting-periods', period_views.AccountingPeriodViewSet, basename='accountingperiod-api')


app_name = 'crp_accounting_api'

# Define pk converters if needed (assuming Party PK might also be UUID or other type)
account_pk_converter = 'uuid:account_pk' # As per your existing code
party_pk_converter = 'uuid:customer_pk' # Assuming Party PK is also UUID, adjust if different (e.g., 'int:customer_pk')


# --- URL Patterns ---
urlpatterns = [
    # ====================================================
    # 1. DRF API URLs
    # ====================================================
    path('', include(router.urls)),

    # Specific API paths for non-ViewSet COA views
    path(
        f'accounts/<{account_pk_converter}>/balance/',
        coa_views.AccountBalanceAPIView.as_view(),
        name='account-balance-api'
    ),
    path(
        f'accounts/<{account_pk_converter}>/ledger/',
        coa_views.AccountLedgerAPIView.as_view(),
        name='account-ledger-api'
    ),
    path('admin-reports/hub/', admin_reports_hub_view, name='admin-reports-hub'),
    # Report API Endpoints
    path('api/reports/trial-balance/', TrialBalanceView.as_view(), name='api_report_trial_balance'),
    path('api/reports/profit-loss/', ProfitLossView.as_view(), name='api_report_profit_loss'),
    path('api/reports/balance-sheet/', BalanceSheetView.as_view(), name='api_report_balance_sheet'),
    # TODO: Add DRF API endpoints for AR Aging and Customer Statement if needed
    # e.g., path('api/reports/ar-aging/', YourAR AgingAPIView.as_view(), name='api_report_ar_aging'),

    # ============================================================================
    # 2. Custom Admin-Related URLs (HTML reports within admin theme)
    # ============================================================================
    path('admin-reports/trial-balance/', admin_trial_balance_view, name='admin-view-trial-balance'),
    path('admin-reports/trial-balance/excel/', download_trial_balance_excel, name='admin-download-trial-balance-excel'),
    path('admin-reports/trial-balance/pdf/', download_trial_balance_pdf, name='admin-download-trial-balance-pdf'),

    path('admin-reports/profit-loss/', admin_profit_loss_view, name='admin-view-profit-loss'),
    path('admin-reports/profit-loss/excel/', download_profit_loss_excel, name='admin-download-profit-loss-excel'),
    path('admin-reports/profit-loss/pdf/', download_profit_loss_pdf, name='admin-download-profit-loss-pdf'),

    path('admin-reports/balance-sheet/', admin_balance_sheet_view, name='admin-view-balance-sheet'),
    path('admin-reports/balance-sheet/excel/', download_balance_sheet_excel, name='admin-download-balance-sheet-excel'),
    path('admin-reports/balance-sheet/pdf/', download_balance_sheet_pdf, name='admin-download-balance-sheet-pdf'),

    path(f'admin-reports/account/<{account_pk_converter}>/ledger/', admin_account_ledger_view, name='admin-view-account-ledger'),

    # --- NEW: AR Aging Report URLs ---
    path('admin-reports/ar-aging/', admin_ar_aging_report_view, name='admin-view-ar-aging'),
    path('admin-reports/ar-aging/excel/', download_ar_aging_excel, name='admin-download-ar-aging-excel'),
    path('admin-reports/ar-aging/pdf/', download_ar_aging_pdf, name='admin-download-ar-aging-pdf'),

    # --- NEW: Customer Statement URLs ---
    # Base view for customer selection or if accessed without a PK
    path('admin-reports/customer-statement/', admin_customer_statement_view, name='admin-view-customer-statement-base'),
    # View for a specific customer (customer_pk is passed to the view)
    path(f'admin-reports/customer-statement/<{party_pk_converter}>/', admin_customer_statement_view, name='admin-view-customer-statement-detail'),
    # PDF Download for a specific customer's statement
    path(f'admin-reports/customer-statement/<{party_pk_converter}>/pdf/', download_customer_statement_pdf, name='admin-download-customer-statement-pdf'),
    path('admin-reports/ap-aging/', admin_ap_aging_report_view, name='admin-view-ap-aging'),
    path('admin-reports/vendor-statement/', admin_vendor_statement_view, name='admin-view-vendor-statement-base'),

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