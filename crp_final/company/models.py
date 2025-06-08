from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator
from django.utils import timezone
import datetime
from dateutil.relativedelta import relativedelta
import pytz

from crp_core.enums import CurrencyType


class CompanyGroup(models.Model):
    name = models.CharField(_("Group Name"), max_length=255, unique=True)
    description = models.TextField(_("Description"), blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Company Group")
        verbose_name_plural = _("Company Groups")
        ordering = ['name']

    def __str__(self):
        return self.name

class Company(models.Model):
    subdomain_prefix = models.CharField(
        _("Subdomain Prefix"),
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for the tenant's subdomain (e.g., 'acme' for acme.yourdomain.com). Lowercase letters, numbers, hyphens."),
        validators=[
            RegexValidator(
                regex=r'^[a-z0-9-]+$',
                message=_("Subdomain can only contain lowercase letters, numbers, and hyphens.")
            )
        ]
    )
    name = models.CharField(
        _("Legal Company Name"),
        max_length=255,
        help_text=_("The official legal name of the company.")
    )
    display_name = models.CharField(
        _("Display Name / Trading Name"),
        max_length=255,
        blank=True,
        help_text=_("Name used for display purposes if different from legal name. Defaults to legal name.")
    )
    internal_company_code = models.CharField(
        _("Internal Code (Optional)"),
        max_length=50,
        blank=True, null=True,
        db_index=True,
        help_text=_("Optional internal short code if subdomain is not user-facing in all contexts."),
        validators=[
            RegexValidator(
                regex=r'^[A-Za-z0-9_-]+$',
                message=_("Internal code can only contain letters, numbers, hyphens, and underscores.")
            )
        ]
    )
    company_group = models.ForeignKey(
        CompanyGroup,
        verbose_name=_("Company Group"),
        on_delete=models.SET_NULL,
        related_name='companies',
        null=True, blank=True,
        help_text=_("Optional: Group this company belongs to for consolidated views.")
    )
    logo = models.ImageField(
        _("Company Logo"),
        upload_to='company_logos/',
        blank=True, null=True,
        help_text=_("Logo for branding on reports and UI.")
    )
    address_line1 = models.CharField(_("Address Line 1"), max_length=255, blank=True, default='')
    address_line2 = models.CharField(_("Address Line 2"), max_length=255, blank=True, default='')
    city = models.CharField(_("City"), max_length=100, blank=True, default='')
    state_province_region = models.CharField(_("State/Province/Region"), max_length=100, blank=True, default='')
    postal_code = models.CharField(_("Postal Code"), max_length=20, blank=True, default='')
    country_code = models.CharField(
        _("Country Code"),
        max_length=2,
        blank=True, default='',
        help_text=_("Two-letter ISO country code (e.g., US, GB, IN). Important for localization and tax.")
    )
    currency_decimal_places = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(0), MaxValueValidator(6)],  # Sensible range
        help_text="Number of decimal places to use for currency values."
    )
    primary_phone = models.CharField(_("Primary Phone"), max_length=30, blank=True, default='')
    primary_email = models.EmailField(_("Primary Email"), blank=True, default='')
    website = models.URLField(_("Website"), blank=True, default='')
    registration_number = models.CharField(
        _("Business Registration Number"),
        max_length=100, blank=True, default=''
    )
    tax_id_primary = models.CharField(
        _("Primary Tax ID"),
        max_length=50, blank=True, default=''
    )
    default_currency_code = models.CharField(
        _("Default Currency Code"),
        max_length=10,  # ISO currency codes are 3 letters
        choices=CurrencyType.choices,  # Use choices from your imported enum
        default=CurrencyType.USD.value,  # Set a default from your enum (e.g., USD)
        help_text=_("Company's primary reporting currency (e.g., USD, EUR, INR).")
    )
    default_currency_symbol = models.CharField(
        _("Default Currency Symbol"),
        max_length=5,  # e.g. $, ₹, €
        blank=True, null=True,  # Can be derived or set manually
        help_text=_("Symbol for the default currency (e.g., $, ₹, €). Auto-populated if left blank based on code.")
    )
    financial_year_start_month = models.PositiveSmallIntegerField(
        _("Financial Year Start Month"),
        default=1,
        choices=[(i, timezone.datetime(2000, i, 1).strftime('%B')) for i in range(1, 13)],
        help_text=_("The month your company's financial year starts.")
    )
    timezone_name = models.CharField(
        _("Timezone"),
        max_length=63,
        default='UTC',
        choices=[(tz, tz) for tz in pytz.common_timezones],
        help_text=_("Company's primary operational timezone (e.g., 'America/New_York', 'Asia/Kolkata').")
    )
    is_active = models.BooleanField(
        _("Tenant Account Active"), default=True,
        help_text=_("Designates whether this tenant account is active and can access the service.")

    )
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Registered By User"),
        on_delete=models.SET_NULL,
        related_name='registered_companies',
        null=True, blank=True
    )
    is_suspended_by_admin = models.BooleanField(default=False, verbose_name=_("Suspended by Admin"))
    created_at = models.DateTimeField(_("Registered At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Last Updated"), auto_now=True)

    class Meta:
        verbose_name = _("Company")
        verbose_name_plural = _("Company")
        ordering = ['name']

    def __str__(self):
        return f"{self.display_name or self.name} ({self.subdomain_prefix})"

    def save(self, *args, **kwargs):
        if not self.display_name:
            self.display_name = self.name
        for field_name in ['address_line1', 'address_line2', 'city', 'state_province_region', 'postal_code', 'country_code', 'primary_phone', 'primary_email', 'website', 'registration_number', 'tax_id_primary']:
            if getattr(self, field_name) is None:
                setattr(self, field_name, '')
        super().save(*args, **kwargs)

    @property
    def effective_is_active(self):
        return self.is_active

    def get_current_financial_year_dates(self, for_date=None):
        try:
            company_tz = pytz.timezone(self.timezone_name)
        except pytz.exceptions.UnknownTimeZoneError:
            company_tz = pytz.utc

        if for_date is None:
            for_date = timezone.localtime(timezone.now(), company_tz).date()
        elif isinstance(for_date, datetime.datetime):
            for_date = timezone.localtime(for_date, company_tz).date()
        elif not isinstance(for_date, datetime.date):
            raise ValueError("for_date must be a datetime.date or datetime.datetime object")

        year = for_date.year
        start_month = self.financial_year_start_month
        fy_start_date = datetime.date(year, start_month, 1)

        if for_date < fy_start_date:
            fy_start_date = datetime.date(year - 1, start_month, 1)
        fy_end_date = fy_start_date + relativedelta(years=1, days=-1)
        return fy_start_date, fy_end_date

class CompanyMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'OWNER', _('Owner/Super Admin')
        ADMIN = 'ADMIN', _('Administrator')
        ACCOUNTING_MANAGER = 'ACCOUNTING_MANAGER', _('Accounting Manager')
        ACCOUNTANT = 'ACCOUNTANT', _('Accountant')
        SALES_REP = 'SALES_REP', _('Sales Representative')
        PURCHASE_OFFICER = 'PURCHASE_OFFICER', _('Purchase Officer')
        DATA_ENTRY = 'DATA_ENTRY', _('Data Entry Clerk')
        AUDITOR = 'AUDITOR', _('Auditor (Read-Only)')
        VIEW_ONLY = 'VIEW_ONLY', _('View Only')

    company = models.ForeignKey(
        Company,
        verbose_name=_("Company"),
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("User"),
        on_delete=models.CASCADE,
        related_name='company_memberships'
    )
    role = models.CharField(
        _("Role"),
        max_length=30,
        choices=Role.choices,
        help_text=_("The user's role within this specific company, determining their permissions.")
    )
    is_active_membership = models.BooleanField(
        _("Membership is Active"), default=True,
        help_text=_("User can access this company if their membership is active and company is active.")
    )
    is_default_for_user = models.BooleanField(
        _("Is Default Company for this User"), default=False,
        help_text=_("If a user has access to multiple companies, this one is selected by default on login or context switch.")
    )
    can_manage_members = models.BooleanField(
        _("Can Manage Members"), default=False,
        help_text=_("Can this user invite, remove, or change roles of other members in this company.")
    )
    date_joined = models.DateTimeField(_("Date Joined Company"), auto_now_add=True)
    last_accessed_at = models.DateTimeField(_("Last Accessed This Company"), null=True, blank=True)

    class Meta:
        verbose_name = _("Company Membership")
        verbose_name_plural = _("Company Memberships")
        unique_together = ('company', 'user')
        ordering = ['company__name', 'user__email']
        indexes = [
            models.Index(fields=['user', 'is_default_for_user']),
            models.Index(fields=['user', 'company']),
        ]

    def __str__(self):
        return f"{self.user.get_username()} - {self.company.name} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        if self.is_default_for_user:
            CompanyMembership.objects.filter(user=self.user, is_default_for_user=True)\
                                     .exclude(pk=self.pk)\
                                     .update(is_default_for_user=False)
        super().save(*args, **kwargs)

    @property
    def effective_can_access(self):
        return self.is_active_membership and self.company.effective_is_active and self.user.is_active