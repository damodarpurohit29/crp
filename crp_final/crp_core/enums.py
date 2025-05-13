# crp_core/enums.py (Enhanced)

from django.db import models
from django.utils.translation import gettext_lazy as _

# -------------------- CORE ACCOUNTING CLASSIFICATIONS --------------------

class AccountType(models.TextChoices):
    """
    Fundamental accounting classification for an Account.
    Determines the account's role in financial statements (Balance Sheet or P&L).
    The choice here dictates how balances are aggregated and reported.
    """
    # --- Existing Members (Unchanged) ---
    ASSET     = 'ASSET', _('Asset')         # Resources owned (Cash, AR, Buildings)
    LIABILITY = 'LIABILITY', _('Liability')     # Obligations owed (AP, Loans)
    EQUITY    = 'EQUITY', _('Equity')        # Owner's stake (Capital, Retained Earnings)
    INCOME    = 'INCOME', _('Income')        # Revenues from operations (Sales, Service Revenue) - Also known as REVENUE
    EXPENSE   = 'EXPENSE', _('Expense')       # Costs incurred (Salaries, Rent, Utilities)
    COST_OF_GOODS_SOLD = 'COGS', _('Cost of Goods Sold') # Direct costs of producing goods/services sold

class AccountNature(models.TextChoices):
    """
    Defines the normal balance side (Debit or Credit) for an Account Type.
    Crucial for calculations, validation, and reporting logic. This is typically
    derived automatically from the AccountType.
    """
    # --- Existing Members (Unchanged) ---
    DEBIT  = 'DEBIT', _('Debit')    # Normal balance increases with debits (Assets, Expenses, COGS)
    CREDIT = 'CREDIT', _('Credit')   # Normal balance increases with credits (Liabilities, Equity, Income)
    # --- End Existing ---

class DrCrType(models.TextChoices):
    """
    Specifies whether a Journal Line represents a Debit or a Credit amount.
    The foundation of double-entry bookkeeping.
    """
    # --- Existing Members (Unchanged) ---
    DEBIT  = 'DEBIT', _('Dr') # Display 'Dr' for brevity
    CREDIT = 'CREDIT', _('Cr') # Display 'Cr' for brevity
    # --- End Existing ---

# -------------------- JOURNAL & TRANSACTION CLASSIFICATIONS --------------------

class VoucherType(models.TextChoices):
    """
    Categorizes the Voucher based on its business nature or source,
    similar to Tally Voucher Types. Can drive reporting, validation rules, and workflow.
    """
    # --- Existing Members (Unchanged) ---
    GENERAL = 'GENERAL', _('General Voucher')     # Adjustments, corrections, opening/closing, non-standard
    SALES = 'SALES', _('Sales Voucher')           # From sales invoices/transactions (Revenue recognition)
    PURCHASE = 'PURCHASE', _('Purchase Voucher')    # From purchase bills/invoices (Expense/Asset recognition)
    RECEIPT = 'RECEIPT', _('Receipt Voucher')       # Recording cash/bank inflows (AR collection, other income)
    PAYMENT = 'PAYMENT', _('Payment Voucher')       # Recording cash/bank outflows (AP payment, expense payment)
    CONTRA = 'CONTRA', _('Contra Voucher')         # Transfers between Cash & Bank accounts ONLY
    # --- End Existing ---

    # --- Added Members (Common Accounting Vouchers) ---
    DEBIT_NOTE = 'DEBIT_NOTE', _('Debit Note')     # Issued for Sales Returns / Purchase Price Increase / Corrections
    CREDIT_NOTE = 'CREDIT_NOTE', _('Credit Note')    # Issued for Purchase Returns / Sales Price Decrease / Corrections
    STOCK_JOURNAL = 'STOCK_JOURNAL', _('Stock Journal') # Pure inventory value/quantity adjustments (if tracking inventory)
    DEPRECIATION = 'DEPRECIATION', _('Depreciation')   # Recording fixed asset depreciation (often system-generated)
    # --- Potentially More Specific Types (Consider adding if needed) ---
    # PAYROLL = 'PAYROLL', _('Payroll Voucher')       # For payroll processing entries
    # MANUFACTURING = 'MANUFACTURING', _('Manufacturing Journal') # If tracking production costs
    # FX_ADJUSTMENT = 'FX_ADJUSTMENT', _('FX Adjustment') # Forex gain/loss recognition

class AccountingPurposeType(models.TextChoices):
    """
    Optionally classifies a Voucher by its specific accounting purpose,
    allowing for finer-grained analysis or filtering.
    """
    # --- Existing Members (Unchanged) ---
    STANDARD = 'STANDARD', _('Standard Entry')   # Regular day-to-day business transaction
    OPENING = 'OPENING', _('Opening Balance')  # Initial balances setup when migrating/starting
    CLOSING = 'CLOSING', _('Closing Entry')    # Period-end/Year-end closing of nominal accounts
    ADJUSTING = 'ADJUSTING', _('Adjusting Entry')  # Accruals, deferrals, estimations (e.g., bad debt)
    CORRECTION = 'CORRECTION', _('Correction Entry') # Fixing errors in previously posted entries
    REVERSING = 'REVERSING', _('Reversing Entry')  # Optional reversal of certain adjusting entries
    SYSTEM = 'SYSTEM', _('System Generated') # Automated (depreciation, calculated FX gain/loss, etc.)
    # --- End Existing ---

    # --- Added Members (Optional Granularity) ---
    ACCRUAL = 'ACCRUAL', _('Accrual') # Specific type of adjusting entry
    DEFERRAL = 'DEFERRAL', _('Deferral') # Specific type of adjusting entry
    PROVISION = 'PROVISION', _('Provision') # Recording provisions (e.g., warranty, legal)
    INTERCOMPANY = 'INTERCOMPANY', _('Intercompany') # Transactions between related entities

class TransactionStatus(models.TextChoices):
    """
    Defines the workflow status of a Voucher or other transaction document.
    Determines editability and impact on ledger balances.
    """
    # --- Existing Members (Unchanged) ---
    DRAFT = 'DRAFT', _('Draft')                 # Initial creation, freely editable, no ledger impact
    PENDING_APPROVAL = 'PENDING_APPROVAL', _('Pending Approval') # Submitted, locked for editing by creator, awaiting review
    POSTED = 'POSTED', _('Posted')               # Approved, impacts ledger balances, immutable (corrections via new entries)
    REJECTED = 'REJECTED', _('Rejected')             # Approval denied, may be editable for correction & resubmission
    CANCELLED = 'CANCELLED', _('Cancelled')           # Voided/Invalidated, no ledger impact (often achieved via reversing entry logic)
    # --- End Existing ---

    # --- Added Members (Optional Workflow States) ---
    # ON_HOLD = 'ON_HOLD', _('On Hold')               # Temporarily paused, needs info/action
    # PARTIALLY_APPROVED = 'PARTIALLY_APPROVED', _('Partially Approved') # If multi-level approval exists

class ApprovalActionType(models.TextChoices):
    """
    Defines the types of actions recorded in an approval log (e.g., VoucherApproval).
    Provides an audit trail of the workflow process.
    """
    # --- Existing Members (Unchanged) ---
    SUBMITTED = 'SUBMITTED', _('Submitted')         # User submitted for approval
    APPROVED = 'APPROVED', _('Approved')           # User approved the voucher (final or step)
    REJECTED = 'REJECTED', _('Rejected')           # User rejected the voucher
    CANCELLED = 'CANCELLED', _('Cancelled')         # Action of cancelling (if distinct from status change)
    COMMENTED = 'COMMENTED', _('Commented')         # User added a comment without changing status
    # --- End Existing ---

    # --- Added Members (Optional Granularity) ---
    REASSIGNED = 'REASSIGNED', _('Reassigned')       # Approval task assigned to another user
    FORWARDED = 'FORWARDED', _('Forwarded')         # Sent to another user for input/review (without formal approval)
    EDITED = 'EDITED', _('Edited')             # Log if edits are made during approval process (if allowed)

# -------------------- PARTY & TAX --------------------

class PartyType(models.TextChoices):
    """
    Classifies the type of external or internal entity involved in transactions.
    Used for Party master records, reporting, and Control Account linkage.
    """
    # --- Existing Members (Unchanged) ---
    CUSTOMER = 'CUSTOMER', _('Customer') # Buys goods/services from us
    SUPPLIER = 'SUPPLIER', _('Supplier') # Sells goods/services to us (also Vendor)
    EMPLOYEE = 'EMPLOYEE', _('Employee') # Internal party for payroll, expense claims, advances
    OTHER    = 'OTHER', _('Other')       # Miscellaneous parties not fitting other categories
    # --- End Existing ---

    # --- Added Members (Common Entities) ---
    BANK     = 'BANK', _('Bank')         # Financial institutions
    GOVERNMENT = 'GOVERNMENT', _('Government Agency') # Tax authorities, regulators, etc.
    INVESTOR = 'INVESTOR', _('Investor/Shareholder') # For equity/loan transactions
    INTERCOMPANY = 'INTERCOMPANY', _('Intercompany') # Related legal entities within a group

class TaxType(models.TextChoices):
    """
    Defines various types of taxes applicable to transactions or parties.
    Used for tax calculations, reporting, and linking to tax-specific ledger accounts.
    """
    # --- Existing Members (Unchanged) ---
    VAT           = 'VAT', _('VAT')                     # Value Added Tax
    GST           = 'GST', _('GST')                     # Goods and Services Tax (Umbrella Term)
    SALES_TAX     = 'SALES_TAX', _('Sales Tax')           # State/Local Sales Tax
    INCOME_TAX    = 'INCOME_TAX', _('Income Tax')         # Corporate/Personal Income Tax
    WITHHOLDING   = 'WITHHOLDING', _('Withholding Tax')   # Tax Deducted at Source (TDS)
    SERVICE_TAX   = 'SERVICE_TAX', _('Service Tax')       # Specific levy on services (may be replaced by GST/VAT)
    CUSTOM_DUTY   = 'CUSTOM_DUTY', _('Custom Duty')       # Tax on imports/exports
    NONE          = 'NONE', _('None Applicable')      # Explicitly state no tax is relevant
    # --- End Existing ---

    # --- Added Members (Common Taxes & GST Breakdown) ---
    CGST          = 'CGST', _('CGST')                   # Central GST (India)
    SGST          = 'SGST', _('SGST/UTGST')             # State/Union Territory GST (India)
    IGST          = 'IGST', _('IGST')                   # Integrated GST (India - Interstate)
    EXCISE_DUTY   = 'EXCISE_DUTY', _('Excise Duty')       # Tax on manufacture of goods
    PROPERTY_TAX  = 'PROPERTY_TAX', _('Property Tax')     # Tax on real estate
    PAYROLL_TAX   = 'PAYROLL_TAX', _('Payroll Tax')       # Taxes related to employee wages (e.g., FICA, Unemployment)

# -------------------- SUPPORTING ENUMS --------------------

class CurrencyType(models.TextChoices):
    """
    Represents standard currency codes (ISO 4217).
    Essential for multi-currency accounting and reporting.
    """
    # --- Existing Members (Unchanged) ---
    USD = 'USD', _('US Dollar')
    EUR = 'EUR', _('Euro')
    INR = 'INR', _('Indian Rupee')
    GBP = 'GBP', _('British Pound')
    AED = 'AED', _('UAE Dirham')
    JPY = 'JPY', _('Japanese Yen')
    CAD = 'CAD', _('Canadian Dollar')
    AUD = 'AUD', _('Australian Dollar')
    # --- End Existing ---

    # --- Added Members (More Common Currencies) ---
    CHF = 'CHF', _('Swiss Franc')
    CNY = 'CNY', _('Chinese Yuan Renminbi')
    SGD = 'SGD', _('Singapore Dollar')
    HKD = 'HKD', _('Hong Kong Dollar')
    NZD = 'NZD', _('New Zealand Dollar')
    # Consider adding regional currencies based on your primary user base

    OTHER = 'OTHER', _('Other') # Fallback, use sparingly, requires handling

# --- Removed Enums Comment (Existing, Unchanged) ---
# - AssetType, LiabilityType, EquityType, ExpenseType, IncomeType (Use AccountGroup hierarchy)
# - TransactionRole (Belongs on Party or specific transaction documents)
# - PaymentStatus (Belongs on Invoice/Bill models, not core Journal/Account)


# from django.db import models
# from django.utils.translation import gettext_lazy as _
#
# # -------------------- CORE ACCOUNTING CLASSIFICATIONS --------------------
#
# class AccountType(models.TextChoices):
#     """
#     Fundamental accounting classification for an Account.
#     Determines the account's role in financial statements (Balance Sheet or P&L).
#     """
#     ASSET     = 'ASSET', _('Asset')
#     LIABILITY = 'LIABILITY', _('Liability')
#     EQUITY    = 'EQUITY', _('Equity')
#     INCOME    = 'INCOME', _('Income')
#     EXPENSE   = 'EXPENSE', _('Expense')
#     # Note: COGS is generally considered a type of Expense account/group.
#
# class AccountNature(models.TextChoices):
#     """
#     Defines the normal balance side (Debit or Credit) for an Account Type.
#     Crucial for calculations, validation, and reporting logic.
#     """
#     DEBIT  = 'DEBIT', _('Debit')
#     CREDIT = 'CREDIT', _('Credit')
#
# class DrCrType(models.TextChoices):
#     """
#     Specifies whether a Journal Line represents a Debit or a Credit amount.
#     The foundation of double-entry bookkeeping.
#     """
#     DEBIT  = 'DEBIT', _('Dr') # Display 'Dr' for brevity
#     CREDIT = 'CREDIT', _('Cr') # Display 'Cr' for brevity
#
# # -------------------- JOURNAL & TRANSACTION CLASSIFICATIONS --------------------
#
# class VoucherType(models.TextChoices):
#     """
#     Categorizes the Voucher based on its business nature or source,
#     similar to Tally Voucher Types. Drives reporting and workflow.
#     """
#     # Renamed from JournalType, refined choices
#     GENERAL = 'GENERAL', _('General Voucher')     # Default for adjustments, corrections, non-standard
#     SALES = 'SALES', _('Sales Voucher')           # From sales invoices/transactions
#     PURCHASE = 'PURCHASE', _('Purchase Voucher')    # From purchase bills/invoices
#     RECEIPT = 'RECEIPT', _('Receipt Voucher')       # Recording income (cash/bank)
#     PAYMENT = 'PAYMENT', _('Payment Voucher')       # Recording expenses/payments (cash/bank)
#     CONTRA = 'CONTRA', _('Contra Voucher')         # Transfers between cash & bank accounts
#     # Optional additions (consider if needed):
#     # DEBIT_NOTE = 'DEBIT_NOTE', _('Debit Note')     # Sales Returns / Price Adjustments
#     # CREDIT_NOTE = 'CREDIT_NOTE', _('Credit Note')    # Purchase Returns / Price Adjustments
#     # STOCK_JOURNAL = 'STOCK_JOURNAL', _('Stock Journal') # If handling pure inventory adjustments separately
#
# class AccountingPurposeType(models.TextChoices):
#     """
#     Optionally classifies a Voucher by its specific accounting purpose.
#     Could be used in a separate 'entry_purpose' field on the Voucher model.
#     """
#     # Renamed from JournalEntryType
#     STANDARD = 'STANDARD', _('Standard Entry')   # Regular business transaction
#     OPENING = 'OPENING', _('Opening Balance')  # Initial balances setup
#     CLOSING = 'CLOSING', _('Closing Entry')    # Period-end closing entries
#     ADJUSTING = 'ADJUSTING', _('Adjusting Entry')  # Accruals, deferrals, etc.
#     CORRECTION = 'CORRECTION', _('Correction Entry') # Fixing errors
#     REVERSING = 'REVERSING', _('Reversing Entry')  # Auto-reversal of accruals
#     SYSTEM = 'SYSTEM', _('System Generated') # Automated (e.g., depreciation, FX gain/loss)
#
# class TransactionStatus(models.TextChoices):
#     """
#     Defines the workflow status of a Voucher or other transaction.
#     """
#     DRAFT = 'DRAFT', _('Draft')                 # Initial creation, editable
#     PENDING_APPROVAL = 'PENDING_APPROVAL', _('Pending Approval') # Submitted, awaiting review
#     POSTED = 'POSTED', _('Posted')               # Approved, impacts ledger, immutable
#     REJECTED = 'REJECTED', _('Rejected')             # Approval denied, may need correction
#     CANCELLED = 'CANCELLED', _('Cancelled')           # Voided, often via reversing entry
#
# class ApprovalActionType(models.TextChoices):
#     """
#     Defines the types of actions recorded in the VoucherApproval log.
#     """
#     SUBMITTED = 'SUBMITTED', _('Submitted')         # User submitted for approval
#     APPROVED = 'APPROVED', _('Approved')           # User approved the voucher
#     REJECTED = 'REJECTED', _('Rejected')           # User rejected the voucher
#     CANCELLED = 'CANCELLED', _('Cancelled')         # Action of cancelling (if distinct from status)
#     COMMENTED = 'COMMENTED', _('Commented')
#
# # -------------------- PARTY & TAX --------------------
#
# class PartyType(models.TextChoices):
#     """
#     Classifies the type of external or internal entity involved in transactions.
#     Used for Party master records and Control Account linkage.
#     """
#     CUSTOMER = 'CUSTOMER', _('Customer') # Buys goods/services
#     SUPPLIER = 'SUPPLIER', _('Supplier') # Sells goods/services (also Vendor)
#     EMPLOYEE = 'EMPLOYEE', _('Employee') # Internal party for payroll, expenses
#     OTHER    = 'OTHER', _('Other')       # For miscellaneous parties
#
# class TaxType(models.TextChoices):
#     """
#     Defines various types of taxes applicable to transactions or parties.
#     Used for tax calculations, reporting, and tax-specific accounts.
#     """
#     VAT           = 'VAT', _('VAT')                     # Value Added Tax
#     GST           = 'GST', _('GST')                     # Goods and Services Tax
#     SALES_TAX     = 'SALES_TAX', _('Sales Tax')
#     INCOME_TAX    = 'INCOME_TAX', _('Income Tax')
#     WITHHOLDING   = 'WITHHOLDING', _('Withholding Tax')   # Tax deducted at source
#     SERVICE_TAX   = 'SERVICE_TAX', _('Service Tax')         # Specific service levy
#     CUSTOM_DUTY   = 'CUSTOM_DUTY', _('Custom Duty')       # Tax on imports/exports
#     NONE          = 'NONE', _('None Applicable')      # Explicitly state no tax
#
# # -------------------- SUPPORTING ENUMS --------------------
#
# class CurrencyType(models.TextChoices):
#     """
#     Represents standard currency codes (ISO 4217).
#     Essential for multi-currency accounting and reporting.
#     """
#     USD = 'USD', _('US Dollar')
#     EUR = 'EUR', _('Euro')
#     INR = 'INR', _('Indian Rupee')
#     GBP = 'GBP', _('British Pound')
#     AED = 'AED', _('UAE Dirham')
#     JPY = 'JPY', _('Japanese Yen')
#     CAD = 'CAD', _('Canadian Dollar')
#     AUD = 'AUD', _('Australian Dollar')
#     # Add more as needed based on operational regions
#     OTHER = 'OTHER', _('Other') # Fallback, use sparingly
#
# # --- Removed Enums (Handled by AccountGroup or belong elsewhere) ---
# # - AssetType (Use AccountGroup hierarchy: Assets -> Current Assets)
# # - LiabilityType (Use AccountGroup hierarchy: Liabilities -> Current Liabilities)
# # - EquityType (Use AccountGroup hierarchy: Equity -> Retained Earnings)
# # - ExpenseType (Use AccountGroup hierarchy: Expenses -> Operating Expenses)
# # - IncomeType (Use AccountGroup hierarchy: Income -> Sales Revenue)
# # - TransactionRole (Belongs on Party or specific transaction documents)
# # - PaymentStatus (Belongs on Invoice/Bill models, not core Journal/Account)