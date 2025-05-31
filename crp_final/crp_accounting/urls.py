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
    # path('permissions-overview/',
    #      admin_voucher_permissions_overview_view,
    #      name='admin_voucher_permissions_overview'),
]
