from django.contrib import admin

from .models import (CalculationRun, ImportBatch, InboundLine, MonthlySale,
                     MonthlyStock, PlanningPolicy, Product, Recommendation,
                     SaleLine, StockoutPeriod, StockSnapshot, Supplier)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('code', 'name')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'supplier', 'article', 'category', 'unit', 'min_order', 'pack_multiple', 'purchase_to_stock_factor', 'requires_conversion')
    list_filter = ('supplier', 'category')
    search_fields = ('sku', 'article', 'name')


@admin.register(PlanningPolicy)
class PlanningPolicyAdmin(admin.ModelAdmin):
    list_display = ('supplier', 'category', 'lead_days', 'review_days', 'safety_days', 'confirmed')
    list_filter = ('supplier', 'confirmed')


@admin.register(StockSnapshot)
class StockSnapshotAdmin(admin.ModelAdmin):
    list_display = ('product', 'warehouse', 'as_of', 'on_hand', 'reserved', 'free')
    list_filter = ('warehouse', 'as_of')
    search_fields = ('product__sku',)


@admin.register(StockoutPeriod)
class StockoutPeriodAdmin(admin.ModelAdmin):
    list_display = ('product', 'warehouse', 'starts', 'ends')
    search_fields = ('product__sku',)


@admin.register(InboundLine)
class InboundLineAdmin(admin.ModelAdmin):
    list_display = ('product', 'reference', 'eta', 'quantity', 'status')
    search_fields = ('product__sku', 'reference')


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ('supplier', 'kind', 'filename', 'as_of', 'row_count', 'imported_at')
    readonly_fields = ('supplier', 'kind', 'filename', 'sha256', 'as_of', 'row_count', 'warnings', 'imported_at')


@admin.register(CalculationRun)
class CalculationRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'supplier', 'as_of', 'warehouse', 'category', 'created_at', 'approved_at')
    readonly_fields = ('supplier', 'as_of', 'warehouse', 'category', 'allow_estimated_stock', 'created_at', 'created_by', 'approved_at', 'approved_by', 'source_batches')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ('run', 'product', 'suggested_quantity', 'final_quantity', 'urgency')
    search_fields = ('product__sku',)
    readonly_fields = ('run', 'product', 'suggested_quantity', 'final_quantity', 'urgency', 'explanation', 'details', 'blockers', 'changed_reason', 'changed_by', 'changed_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(MonthlySale)
admin.site.register(MonthlyStock)
admin.site.register(SaleLine)
