from django.conf import settings
from django.db import models


class Supplier(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=120)

    def __str__(self):
        return self.name


class Product(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    sku = models.CharField(max_length=64)  # Keep leading zeroes and trailing underscores.
    article = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=500, blank=True)
    unit = models.CharField(max_length=24, blank=True)
    category = models.CharField(max_length=80, blank=True)
    min_order = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    pack_multiple = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    purchase_to_stock_factor = models.DecimalField(max_digits=16, decimal_places=3, default=1)
    requires_conversion = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['supplier', 'sku'], name='unique_supplier_sku')]
        ordering = ['supplier__name', 'sku']

    def __str__(self):
        return f'{self.supplier.code} {self.sku}'


class ImportBatch(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    kind = models.CharField(max_length=32)
    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    as_of = models.DateField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    row_count = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list)

    class Meta:
        ordering = ['-imported_at']


class MonthlySale(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    month = models.DateField()
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    reported = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'month'], name='unique_monthly_sale')]


class MonthlyStock(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    month = models.DateField()
    opening_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'month'], name='unique_monthly_stock')]


class SaleLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    sold_at = models.DateTimeField()
    document = models.CharField(max_length=80)
    document_type = models.CharField(max_length=80, blank=True)
    warehouse = models.CharField(max_length=100)
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    customer_hash = models.CharField(max_length=128, blank=True)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['product', 'sold_at'])]


class StockSnapshot(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    warehouse = models.CharField(max_length=100)
    as_of = models.DateField()
    on_hand = models.DecimalField(max_digits=18, decimal_places=3)
    reserved = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    free = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'warehouse', 'as_of'], name='unique_stock_snapshot')]


class InboundLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    reference = models.CharField(max_length=160)
    eta = models.DateField()
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    status = models.CharField(max_length=20, default='confirmed')

    class Meta:
        indexes = [models.Index(fields=['product', 'eta'])]


class StockoutPeriod(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    warehouse = models.CharField(max_length=100)
    starts = models.DateField()
    ends = models.DateField()

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.starts and self.ends and self.ends < self.starts:
            raise ValidationError('Конец периода раньше начала.')


class PlanningPolicy(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    category = models.CharField(max_length=80, blank=True)
    lead_days = models.PositiveIntegerField()
    review_days = models.PositiveIntegerField()
    safety_days = models.PositiveIntegerField()
    max_growth = models.DecimalField(max_digits=5, decimal_places=3, default='0.300')
    confirmed = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['supplier', 'category'], name='unique_supplier_category_policy')]

    def __str__(self):
        return f'{self.supplier.name}: {self.category or "все категории"}'


class CalculationRun(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    as_of = models.DateField()
    warehouse = models.CharField(max_length=100)
    category = models.CharField(max_length=80, blank=True)
    allow_estimated_stock = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    source_batches = models.JSONField(default=list)

    class Meta:
        permissions = [('can_approve_order', 'Can approve supplier orders')]

    @property
    def approved(self):
        return self.approved_at is not None


class Recommendation(models.Model):
    run = models.ForeignKey(CalculationRun, on_delete=models.CASCADE, related_name='recommendations')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    suggested_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    final_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    urgency = models.CharField(max_length=20, default='normal')
    explanation = models.TextField()
    details = models.JSONField(default=dict)
    blockers = models.JSONField(default=list)
    changed_reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'product'], name='unique_run_product')]
        ordering = ['product__sku']
