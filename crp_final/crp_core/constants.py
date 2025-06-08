# core/constants.py

"""
Contains static constants like ACCOUNT_ROLE_GROUPS used for seeding
a comprehensive Chart of Accounts (COA) structure, reflecting common
accounting standards and detail found in systems like Tally.
"""

# =============================================================================
# Comprehensive Chart of Accounts Structure Definition
# =============================================================================
# Format: (account_code, account_name, optional_nature_override_if_different_from_group_default)
# If nature_override is not provided, the nature will be derived based on the AccountType,
# which itself is derived from the top-level group. Your model's ACCOUNT_TYPE_TO_NATURE
# dictionary should be the primary source for these defaults during seeding if no override is present.

ACCOUNT_ROLE_GROUPS = {

    # ========================== ASSETS ==========================
    'Assets - Current Assets': [
        # --- Cash & Bank ---
        ('1000_cash', 'Cash on Hand'),
        ('1001_petty_cash', 'Petty Cash'),
        ('1010_bank_accounts', 'Bank Accounts'), # Summary?
        ('1011_bank_account_checking', 'Bank Account - Checking'),
        ('1012_bank_account_savings', 'Bank Account - Savings'),
        ('1013_bank_account_money_market', 'Bank Account - Money Market'),
        ('1015_undeposited_funds', 'Undeposited Funds'),
        ('1020_marketable_securities_short_term', 'Marketable Securities - Short Term'),
        # --- Receivables ---
        ('1030_accounts_receivable_trade', 'Accounts Receivable - Trade'), # Summary? Control Account
        ('1032_accounts_receivable_other', 'Accounts Receivable - Other'),
        ('1035_notes_receivable_current', 'Notes Receivable - Current'),
        ('1036_interest_receivable', 'Interest Receivable'),
        ('1039_allowance_for_doubtful_accounts', 'Allowance for Doubtful Accounts', 'CREDIT'), # Contra Asset
        ('1040_advances_to_suppliers', 'Advances to Suppliers'),
        ('1045_employee_advances', 'Employee Advances'),
        ('1046_due_from_related_parties_current', 'Due from Related Parties - Current'),
        # --- Inventory ---
        ('1050_inventory', 'Inventory'), # Summary?
        ('1051_inventory_raw_materials', 'Inventory - Raw Materials'),
        ('1052_inventory_work_in_progress', 'Inventory - Work In Progress (WIP)'),
        ('1053_inventory_finished_goods', 'Inventory - Finished Goods'),
        ('1054_inventory_merchandise', 'Inventory - Merchandise (Trading Goods)'),
        ('1055_inventory_spares_consumables', 'Inventory - Spares and Consumables'),
        ('1059_inventory_provision_for_obsolescence', 'Inventory - Provision for Obsolescence', 'CREDIT'), # Contra Asset
        # --- Prepaid Expenses ---
        ('1070_prepaid_expenses', 'Prepaid Expenses'), # Summary?
        ('1071_prepaid_rent', 'Prepaid Rent'),
        ('1072_prepaid_insurance', 'Prepaid Insurance'),
        ('1073_prepaid_advertising', 'Prepaid Advertising'),
        ('1074_prepaid_salaries', 'Prepaid Salaries'),
        ('1075_prepaid_subscriptions', 'Prepaid Subscriptions'),
        # --- Other Current Assets ---
        ('1080_accrued_income', 'Accrued Income (Revenue Receivable)'),
        ('1090_other_current_assets', 'Other Current Assets'), # Summary?
        ('1100_short_term_investments_other', 'Short-Term Investments (Other)'),
        ('1110_tax_recoverable_gst_vat', 'Tax Recoverable - GST/VAT'),
        ('1111_tax_recoverable_income_tax', 'Tax Recoverable - Income Tax'),
        ('1112_tax_recoverable_other', 'Tax Recoverable - Other'),
    ],
    'Assets - Non-Current Assets': [
        # --- Long-Term Investments ---
        ('1200_notes_receivable_long_term', 'Notes Receivable - Long-Term'),
        ('1220_long_term_investments_securities', 'Long-Term Investments - Securities'),
        ('1230_investments_in_subsidiaries_associates', 'Investments in Subsidiaries / Associates'),
        ('1235_investment_property', 'Investment Property'),
        ('1240_other_long_term_investments', 'Other Long-Term Investments'),
        # --- Property, Plant, Equipment (Fixed Assets) ---
        ('1300_land', 'Land (Freehold)'),
        ('1301_land_leasehold', 'Land (Leasehold)'),
        ('1305_land_improvements', 'Land Improvements'),
        ('1306_land_improvements_accum_depr', 'Land Improvements - Accum. Depreciation', 'CREDIT'),
        ('1310_buildings', 'Buildings'),
        ('1311_buildings_accum_depr', 'Buildings - Accum. Depreciation', 'CREDIT'),
        ('1320_plant_and_machinery', 'Plant and Machinery'),
        ('1321_plant_and_machinery_accum_depr', 'Plant and Machinery - Accum. Depreciation', 'CREDIT'),
        ('1330_furniture_and_fixtures', 'Furniture and Fixtures'),
        ('1331_furniture_fixtures_accum_depr', 'Furniture & Fixtures - Accum. Depreciation', 'CREDIT'),
        ('1340_vehicles', 'Vehicles'),
        ('1341_vehicles_accum_depr', 'Vehicles - Accum. Depreciation', 'CREDIT'),
        ('1350_office_equipment', 'Office Equipment'),
        ('1351_office_equipment_accum_depr', 'Office Equipment - Accum. Depreciation', 'CREDIT'),
        ('1360_computer_hardware', 'Computer Hardware'),
        ('1361_computer_hardware_accum_depr', 'Computer Hardware - Accum. Depreciation', 'CREDIT'),
        ('1370_leased_assets_finance_lease', 'Assets under Finance Lease'),
        ('1371_leased_assets_accum_depr', 'Assets under Finance Lease - Accum. Depreciation', 'CREDIT'),
        ('1380_capital_work_in_progress', 'Capital Work-in-Progress (CWIP)'),
        ('1385_assets_held_for_sale_non_current', 'Assets Held for Sale (Non-Current)'),
        # --- Intangible Assets ---
        ('1400_intangible_assets', 'Intangible Assets'), # Summary?
        ('1410_goodwill', 'Goodwill'),
        ('1420_patents_and_copyrights', 'Patents and Copyrights'),
        ('1421_patents_copyrights_accum_amort', 'Patents & Copyrights - Accum. Amortization', 'CREDIT'),
        ('1430_trademarks_and_brand_names', 'Trademarks and Brand Names'),
        ('1431_trademarks_brand_names_accum_amort', 'Trademarks & Brand Names - Accum. Amortization', 'CREDIT'),
        ('1440_licenses_and_franchises', 'Licenses and Franchises'),
        ('1441_licenses_franchises_accum_amort', 'Licenses & Franchises - Accum. Amortization', 'CREDIT'),
        ('1450_software_developed_purchased', 'Software (Developed/Purchased)'),
        ('1451_software_accum_amort', 'Software - Accum. Amortization', 'CREDIT'),
        ('1460_research_development_capitalized', 'Research & Development Costs (Capitalized)'),
        ('1461_research_development_accum_amort', 'R&D Costs - Accum. Amortization', 'CREDIT'),
        ('1470_other_intangible_assets', 'Other Intangible Assets'),
        ('1471_other_intangibles_accum_amort', 'Other Intangibles - Accum. Amortization', 'CREDIT'),
        # --- Other Non-Current Assets ---
        ('1500_deferred_tax_assets', 'Deferred Tax Assets'),
        ('1510_security_deposits_long_term', 'Security Deposits - Long Term'),
        ('1520_other_non_current_assets', 'Other Non-Current Assets'), # Summary?
        ('1521_due_from_related_parties_non_current', 'Due from Related Parties - Non-Current'),
    ],

    # ======================= LIABILITIES ========================
    'Liabilities - Current Liabilities': [
        # --- Payables ---
        ('2000_accounts_payable_trade', 'Accounts Payable - Trade'), # Summary? Control Account
        ('2002_accounts_payable_other', 'Accounts Payable - Other'),
        ('2005_notes_payable_current', 'Notes Payable - Current'),
        ('2006_interest_payable', 'Interest Payable'),
        ('2007_bills_payable', 'Bills Payable'),
        # --- Accrued Expenses ---
        ('2010_salaries_and_wages_payable', 'Salaries and Wages Payable'),
        ('2015_payroll_taxes_payable', 'Payroll Taxes Payable (e.g., PAYE, NI)'),
        ('2016_employee_benefits_payable', 'Employee Benefits Payable (e.g., Pension contributions)'),
        ('2020_income_taxes_payable', 'Income Taxes Payable'),
        ('2021_sales_tax_payable_gst_vat', 'Sales Tax Payable (GST/VAT)'),
        ('2022_other_taxes_payable', 'Other Taxes Payable (e.g., Property Tax Payable)'),
        ('2030_accrued_rent_payable', 'Accrued Rent Payable'),
        ('2031_accrued_utilities_payable', 'Accrued Utilities Payable'),
        ('2032_accrued_professional_fees', 'Accrued Professional Fees Payable'),
        ('2035_other_accrued_expenses', 'Other Accrued Expenses'),
        # --- Other Current Liabilities ---
        ('2040_current_portion_long_term_debt', 'Current Portion of Long-Term Debt'),
        ('2041_current_portion_finance_lease_liabilities', 'Current Portion of Finance Lease Liabilities'),
        ('2050_deferred_revenue_current', 'Deferred Revenue - Current (Unearned Income)'),
        ('2060_customer_advances_deposits', 'Customer Advances / Deposits'),
        ('2070_dividends_payable', 'Dividends Payable'),
        ('2080_bank_overdraft', 'Bank Overdraft'),
        ('2090_other_current_liabilities', 'Other Current Liabilities'), # Summary?
        ('2091_due_to_related_parties_current', 'Due to Related Parties - Current'),
    ],
    'Liabilities - Non-Current Liabilities': [
        ('2200_notes_payable_long_term', 'Notes Payable - Long-Term'),
        ('2210_bonds_payable', 'Bonds Payable'),
        ('2215_debentures', 'Debentures'),
        ('2220_mortgage_payable', 'Mortgage Payable'),
        ('2225_loans_from_financial_institutions_long_term', 'Loans from Financial Institutions - Long-Term'),
        ('2230_lease_liabilities_long_term', 'Lease Liabilities - Long-Term (Finance Lease)'),
        ('2240_deferred_tax_liabilities', 'Deferred Tax Liabilities'),
        ('2250_pension_benefit_obligations', 'Pension & Other Post-Employment Benefit Obligations'),
        ('2260_provisions_long_term', 'Provisions - Long-Term (e.g., Warranty, Restructuring)'),
        ('2270_other_long_term_liabilities', 'Other Long-Term Liabilities'), # Summary?
        ('2271_due_to_related_parties_non_current', 'Due to Related Parties - Non-Current'),
    ],

    # ========================== EQUITY ==========================
    'Equity': [
        # --- Contributed Capital (For Corporations) ---
        ('3000_common_stock_ordinary_shares', 'Common Stock / Ordinary Shares (Par/Stated Value)'),
        ('3001_preferred_stock', 'Preferred Stock (Par/Stated Value)'),
        ('3020_additional_paid_in_capital_share_premium', 'Additional Paid-in Capital / Share Premium'),
        # --- Owner Specific (Sole Proprietorship/Partnership) ---
        ('3100_owners_capital', 'Owner’s Capital Account'),
        ('3101_partners_capital_account_a', 'Partner A - Capital Account'),
        ('3102_partners_capital_account_b', 'Partner B - Capital Account'),
        ('3110_owners_drawings_withdrawals', 'Owner’s Drawings / Withdrawals', 'DEBIT'), # Contra Equity
        ('3111_partners_drawings_a', 'Partner A - Drawings', 'DEBIT'), # Contra Equity
        # --- Retained Earnings ---
        ('3200_retained_earnings', 'Retained Earnings (Accumulated Profit/Loss)'),
        ('3205_current_year_earnings', 'Current Year Earnings'), # This is often a calculated value for reports, not a direct GL account.
        ('3210_dividends_declared_paid', 'Dividends Declared/Paid', 'DEBIT'), # Its effect on equity is a debit.
        # --- Other Equity Components ---
        ('3300_treasury_stock', 'Treasury Stock (At Cost)', 'DEBIT'), # Contra Equity
        ('3400_accumulated_other_comprehensive_income_aoci', 'Accumulated Other Comprehensive Income (AOCI)'), # Summary?
        ('3410_foreign_currency_translation_reserve', 'Foreign Currency Translation Reserve (AOCI)'),
        ('3420_revaluation_surplus_reserve', 'Revaluation Surplus Reserve (AOCI - e.g., for PPE)'),
        ('3430_cash_flow_hedge_reserve', 'Cash Flow Hedge Reserve (AOCI)'),
        ('3440_fair_value_through_oci_reserve', 'FVOCI Investments Reserve (AOCI)'),
        ('3500_other_reserves', 'Other Reserves (e.g., Statutory, General, Capital Redemption)'), # Summary?
    ],

    # ========================== INCOME / REVENUE ==========================
    'Income - Operating Revenue': [
        ('4000_sales_revenue', 'Sales Revenue'), # Summary?
        ('4001_product_sales', 'Product Sales'),
        ('4002_service_revenue', 'Service Revenue'),
        ('4003_rental_and_leasing_income', 'Rental and Leasing Income'),
        ('4004_commission_and_fee_income', 'Commission and Fee Income'),
        ('4050_sales_returns_and_allowances', 'Sales Returns and Allowances', 'DEBIT'), # Contra Revenue
        ('4060_sales_discounts', 'Sales Discounts', 'DEBIT'), # Contra Revenue
    ],
    'Income - Other Income': [
        ('4100_interest_income', 'Interest Income'),
        ('4110_dividend_income_from_investments', 'Dividend Income (from Investments)'),
        ('4130_gain_on_sale_of_ppe', 'Gain on Sale of Property, Plant & Equipment'),
        ('4131_gain_on_sale_of_investments', 'Gain on Sale of Investments'),
        ('4140_foreign_exchange_gain_non_operating', 'Foreign Exchange Gain - Non-Operating'),
        ('4150_insurance_claim_proceeds', 'Insurance Claim Proceeds (Gain portion)'),
        ('4190_miscellaneous_other_income', 'Miscellaneous Other Income'),
    ],

    # =================== COST OF GOODS SOLD / COST OF SALES ===================
    'Cost of Goods Sold': [
        ('5000_cost_of_goods_sold_merchandise', 'Cost of Goods Sold - Merchandise'),
        ('5001_cost_of_goods_sold_manufactured', 'Cost of Goods Sold - Manufactured'),
        ('5005_cost_of_services_rendered', 'Cost of Services Rendered'),
        ('5010_opening_stock', 'Opening Stock (Periodic)'),
        ('5015_purchases_of_goods_for_resale', 'Purchases of Goods for Resale'),
        ('5016_purchase_returns_and_allowances', 'Purchase Returns and Allowances', 'CREDIT'), # Contra COGS/Purchase
        ('5017_purchase_discounts_taken', 'Purchase Discounts Taken', 'CREDIT'), # Contra COGS/Purchase
        ('5020_freight_in_carriage_inwards', 'Freight-In / Carriage Inwards'),
        ('5025_import_duties_cogs', 'Import Duties (COGS)'),
        ('5030_direct_labor_cost', 'Direct Labor Cost'),
        ('5040_manufacturing_overhead_applied', 'Manufacturing Overhead Applied'), # Summary?
        ('5041_factory_rent_cogs', 'Factory Rent (COGS)'),
        ('5042_factory_utilities_cogs', 'Factory Utilities (COGS)'),
        ('5043_factory_supplies_cogs', 'Factory Supplies (COGS)'),
        ('5044_depreciation_factory_ppe_cogs', 'Depreciation - Factory PPE (COGS)'),
        ('5050_inventory_write_downs_obsolescence_cogs', 'Inventory Write-Downs & Obsolesescence (COGS)'),
        ('5090_closing_stock', 'Closing Stock (Periodic)','CREDIT'), # Contra COGS
    ],

    # ========================= EXPENSES =========================
    'Expenses - Operating Expenses': [
        # --- Selling & Distribution Expenses ---
        ('6000_selling_distribution_expenses', 'Selling & Distribution Expenses'), # Summary?
        ('6010_sales_staff_salaries_commissions', 'Sales Staff Salaries & Commissions'),
        ('6015_sales_staff_benefits_pension', 'Sales Staff Benefits & Pension'),
        ('6020_advertising_and_promotion_expense', 'Advertising and Promotion Expense'),
        ('6030_marketing_expense', 'Marketing Expense'),
        ('6040_travel_entertainment_sales', 'Travel & Entertainment - Sales'),
        ('6050_freight_out_carriage_outwards', 'Freight-Out / Carriage Outwards'),
        ('6060_showroom_expenses', 'Showroom Expenses'),
        ('6070_depreciation_sales_assets', 'Depreciation - Sales Assets'),
        # --- General & Administrative Expenses ---
        ('6100_general_administrative_expenses', 'General & Administrative Expenses'), # Summary?
        ('6110_office_staff_salaries_wages', 'Office Staff Salaries & Wages'),
        ('6115_directors_remuneration', 'Directors\' Remuneration'),
        ('6116_admin_staff_benefits_pension', 'Admin Staff Benefits & Pension'),
        ('6120_rent_expense_office_admin', 'Rent Expense - Office/Admin'),
        ('6121_rates_and_taxes_property', 'Rates and Taxes - Property'),
        ('6130_office_supplies_and_stationery_expense', 'Office Supplies & Stationery Expense'),
        ('6135_printing_and_stationery', 'Printing and Stationery'),
        ('6140_utilities_office_admin', 'Utilities - Office/Admin'), # Summary?
        ('6141_utilities_electricity_admin', 'Utilities - Electricity (Admin)'),
        ('6142_utilities_water_admin', 'Utilities - Water (Admin)'),
        ('6144_communication_expenses_phone_internet', 'Communication Expenses (Phone, Internet)'),
        ('6150_insurance_expense_general_admin', 'Insurance Expense - General/Admin'),
        ('6160_repairs_maintenance_office_admin', 'Repairs & Maintenance - Office/Admin'),
        ('6170_depreciation_office_admin_assets', 'Depreciation - Office/Admin Assets'),
        ('6180_amortization_admin_intangibles', 'Amortization - Admin Intangibles'),
        ('6200_professional_fees_general', 'Professional Fees - General'), # Summary?
        ('6201_legal_fees', 'Legal Fees'),
        ('6202_audit_fees', 'Audit Fees'),
        ('6203_consultancy_fees', 'Consultancy Fees'),
        ('6210_bank_charges_and_commissions', 'Bank Charges and Commissions'),
        ('6220_bad_debts_written_off_expense', 'Bad Debts Written Off (Expense)'),
        ('6230_travel_entertainment_admin_general', 'Travel & Entertainment - Admin/General'),
        ('6240_postage_and_courier_expenses', 'Postage and Courier Expenses'),
        ('6250_licenses_fees_and_permits', 'Licenses, Fees, and Permits'),
        ('6260_training_and_development_staff', 'Training and Development - Staff'),
        ('6270_recruitment_expenses', 'Recruitment Expenses'),
        ('6290_miscellaneous_general_admin_expense', 'Miscellaneous General & Admin Expense'),
        # --- Research & Development Expenses (if not capitalized) ---
        ('6300_research_development_expensed', 'Research & Development (Expensed)'),
    ],
    'Expenses - Other Expenses / Income': [ # Should mainly contain expense items or net loss items
        # --- Financial Costs ---
        ('6500_interest_expense_on_loans', 'Interest Expense on Loans'),
        ('6501_interest_expense_on_leases', 'Interest Expense on Leases'),
        ('6505_loan_processing_fees', 'Loan Processing Fees'),
        # --- Non-Operating Items ---
        ('6510_loss_on_sale_disposal_of_ppe', 'Loss on Sale/Disposal of PPE'),
        ('6511_loss_on_sale_disposal_of_investments', 'Loss on Sale/Disposal of Investments'),
        ('6520_foreign_exchange_loss_non_operating', 'Foreign Exchange Loss - Non-Operating'), # Net losses
        ('6530_income_tax_expense_current', 'Income Tax Expense - Current Period'),
        ('6531_income_tax_expense_deferred', 'Income Tax Expense - Deferred'),
        ('6540_impairment_loss_goodwill', 'Impairment Loss - Goodwill'),
        ('6541_impairment_loss_other_assets', 'Impairment Loss - Other Assets'),
        ('6590_other_non_operating_expenses', 'Other Non-Operating Expenses'),
    ],
}

# Note: The ACCOUNT_NATURE_FOR_GROUP dictionary is no longer strictly necessary for seeding
# if your seeding script correctly determines the AccountType from the top-level group key
# and then uses your CoA model's ACCOUNT_TYPE_TO_NATURE mapping for defaults,
# prioritizing the explicit overrides from ACCOUNT_ROLE_GROUPS above.
# However, it can be kept for documentation or as an ultimate fallback.
ACCOUNT_NATURE_FOR_GROUP = {
    'Assets - Current Assets': 'DEBIT',
    'Assets - Non-Current Assets': 'DEBIT',
    'Liabilities - Current Liabilities': 'CREDIT',
    'Liabilities - Non-Current Liabilities': 'CREDIT',
    'Equity': 'CREDIT',
    'Income - Operating Revenue': 'CREDIT',
    'Income - Other Income': 'CREDIT',
    'Cost of Goods Sold': 'DEBIT',
    'Expenses - Operating Expenses': 'DEBIT',
    'Expenses - Other Expenses / Income': 'DEBIT',
}