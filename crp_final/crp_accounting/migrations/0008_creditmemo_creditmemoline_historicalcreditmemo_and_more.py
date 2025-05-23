# Generated by Django 5.2.1 on 2025-05-15 05:02

import django.db.models.deletion
import django.utils.timezone
import simple_history.models
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0002_companyaccountingsettings'),
        ('crp_accounting', '0007_remove_account_coa_co_spc_purpose_idx_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CreditMemo',
            fields=[
                ('deleted', models.DateTimeField(db_index=True, editable=False, null=True)),
                ('deleted_by_cascade', models.BooleanField(default=False, editable=False)),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Created At')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Updated At')),
                ('credit_memo_number', models.CharField(blank=True, db_index=True, help_text='Unique credit memo number (system or manual).', max_length=50, verbose_name='Credit Memo Number')),
                ('credit_memo_date', models.DateField(db_index=True, default=django.utils.timezone.now, verbose_name='Credit Memo Date')),
                ('reason', models.TextField(blank=True, help_text='E.g., Return, Allowance, Price Adjustment.', verbose_name='Reason for Credit')),
                ('notes', models.TextField(blank=True, verbose_name='Internal Notes')),
                ('subtotal_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Subtotal Credit')),
                ('tax_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Tax Credit')),
                ('total_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Total Credit Amount')),
                ('amount_applied', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Amount Applied')),
                ('amount_remaining', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Amount Remaining')),
                ('currency', models.CharField(choices=[('USD', 'US Dollar'), ('EUR', 'Euro'), ('INR', 'Indian Rupee'), ('GBP', 'British Pound'), ('AED', 'UAE Dirham'), ('JPY', 'Japanese Yen'), ('CAD', 'Canadian Dollar'), ('AUD', 'Australian Dollar'), ('CHF', 'Swiss Franc'), ('CNY', 'Chinese Yuan Renminbi'), ('SGD', 'Singapore Dollar'), ('HKD', 'Hong Kong Dollar'), ('NZD', 'New Zealand Dollar'), ('OTHER', 'Other')], help_text='Currency of the credit memo. Typically matches customer/company currency.', max_length=10, verbose_name='Currency')),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('OPEN', 'Open'), ('PARTIALLY_APPLIED', 'Partially Applied'), ('FULLY_APPLIED', 'Fully Applied'), ('VOID', 'Void')], db_index=True, default='DRAFT', max_length=20, verbose_name='Credit Memo Status')),
                ('company', models.ForeignKey(help_text='The company this record belongs to.', on_delete=django.db.models.deletion.PROTECT, related_name='%(app_label)s_%(class)s_related', to='company.company', verbose_name='Company')),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_credit_memos', to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(help_text="The customer receiving the credit. Must be 'Customer' type, same company.", on_delete=django.db.models.deletion.PROTECT, related_name='credit_memos', to='crp_accounting.party', verbose_name='Customer')),
                ('original_invoice', models.ForeignKey(blank=True, help_text='The original invoice this credit memo is for, if applicable.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='related_credit_memos', to='crp_accounting.customerinvoice', verbose_name='Original Invoice')),
                ('updated_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_%(app_label)s_%(class)s_set', to=settings.AUTH_USER_MODEL, verbose_name='Last Updated By')),
            ],
            options={
                'verbose_name': 'Credit Memo',
                'verbose_name_plural': 'Credit Memos',
                'ordering': ['company__name', '-credit_memo_date', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='CreditMemoLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.TextField(verbose_name='Description/Service Returned')),
                ('quantity', models.DecimalField(decimal_places=2, default=Decimal('1.0'), max_digits=12, verbose_name='Quantity')),
                ('unit_price', models.DecimalField(decimal_places=2, max_digits=20, verbose_name='Unit Price')),
                ('line_total', models.DecimalField(decimal_places=2, editable=False, max_digits=20, verbose_name='Line Total Credit (Pre-tax)')),
                ('tax_amount_on_line', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=20, verbose_name='Tax Credit on Line')),
                ('account_to_debit', models.ForeignKey(help_text='Account debited for this credit (e.g., Sales Returns). Must be same company as CM.', on_delete=django.db.models.deletion.PROTECT, related_name='credit_memo_lines_debit', to='crp_accounting.account', verbose_name='Account to Debit')),
                ('credit_memo', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='crp_accounting.creditmemo', verbose_name='Credit Memo')),
            ],
            options={
                'verbose_name': 'Credit Memo Line',
                'verbose_name_plural': 'Credit Memo Lines',
                'ordering': ['pk'],
            },
        ),
        migrations.CreateModel(
            name='HistoricalCreditMemo',
            fields=[
                ('deleted', models.DateTimeField(db_index=True, editable=False, null=True)),
                ('deleted_by_cascade', models.BooleanField(default=False, editable=False)),
                ('id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(blank=True, editable=False, verbose_name='Created At')),
                ('updated_at', models.DateTimeField(blank=True, editable=False, verbose_name='Updated At')),
                ('credit_memo_number', models.CharField(blank=True, db_index=True, help_text='Unique credit memo number (system or manual).', max_length=50, verbose_name='Credit Memo Number')),
                ('credit_memo_date', models.DateField(db_index=True, default=django.utils.timezone.now, verbose_name='Credit Memo Date')),
                ('reason', models.TextField(blank=True, help_text='E.g., Return, Allowance, Price Adjustment.', verbose_name='Reason for Credit')),
                ('notes', models.TextField(blank=True, verbose_name='Internal Notes')),
                ('subtotal_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Subtotal Credit')),
                ('tax_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Tax Credit')),
                ('total_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Total Credit Amount')),
                ('amount_applied', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Amount Applied')),
                ('amount_remaining', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=20, verbose_name='Amount Remaining')),
                ('currency', models.CharField(choices=[('USD', 'US Dollar'), ('EUR', 'Euro'), ('INR', 'Indian Rupee'), ('GBP', 'British Pound'), ('AED', 'UAE Dirham'), ('JPY', 'Japanese Yen'), ('CAD', 'Canadian Dollar'), ('AUD', 'Australian Dollar'), ('CHF', 'Swiss Franc'), ('CNY', 'Chinese Yuan Renminbi'), ('SGD', 'Singapore Dollar'), ('HKD', 'Hong Kong Dollar'), ('NZD', 'New Zealand Dollar'), ('OTHER', 'Other')], help_text='Currency of the credit memo. Typically matches customer/company currency.', max_length=10, verbose_name='Currency')),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('OPEN', 'Open'), ('PARTIALLY_APPLIED', 'Partially Applied'), ('FULLY_APPLIED', 'Fully Applied'), ('VOID', 'Void')], db_index=True, default='DRAFT', max_length=20, verbose_name='Credit Memo Status')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('company', models.ForeignKey(blank=True, db_constraint=False, help_text='The company this record belongs to.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='company.company', verbose_name='Company')),
                ('created_by', models.ForeignKey(blank=True, db_constraint=False, editable=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(blank=True, db_constraint=False, help_text="The customer receiving the credit. Must be 'Customer' type, same company.", null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='crp_accounting.party', verbose_name='Customer')),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('original_invoice', models.ForeignKey(blank=True, db_constraint=False, help_text='The original invoice this credit memo is for, if applicable.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='crp_accounting.customerinvoice', verbose_name='Original Invoice')),
                ('updated_by', models.ForeignKey(blank=True, db_constraint=False, editable=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Last Updated By')),
            ],
            options={
                'verbose_name': 'historical Credit Memo',
                'verbose_name_plural': 'historical Credit Memos',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.AddIndex(
            model_name='creditmemo',
            index=models.Index(fields=['company', 'customer', 'credit_memo_date'], name='cm_co_cust_date_idx'),
        ),
        migrations.AddIndex(
            model_name='creditmemo',
            index=models.Index(fields=['company', 'status'], name='cm_co_stat_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='creditmemo',
            unique_together={('company', 'credit_memo_number')},
        ),
    ]
