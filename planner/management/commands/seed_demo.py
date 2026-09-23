"""Create a small, explicitly synthetic dataset for an end-to-end UI walkthrough."""

from datetime import date, datetime
from django.core.management.base import BaseCommand
from django.utils import timezone

from planner.models import (InboundLine, MonthlySale, PlanningPolicy, Product,
                            SaleLine, StockoutPeriod, StockSnapshot, Supplier)


class Command(BaseCommand):
    help = 'Create a synthetic supplier whose recommendations can be approved in the UI.'

    def handle(self, *args, **options):
        if Supplier.objects.filter(code='demo').exists():
            self.stdout.write('Синтетический поставщик уже существует; данные не изменены.')
            return
        supplier = Supplier.objects.create(code='demo', name='Демо: синтетические данные')
        PlanningPolicy.objects.create(supplier=supplier, category='', lead_days=30, review_days=15, safety_days=10, confirmed=True)
        specs = [
            ('DEMO-SEASON', 'Сезонный товар', 'Сезонные', 5, 5),
            ('DEMO-BULK', 'Товар с разовой крупной продажей', 'Обычные', 1, 10),
            ('DEMO-STOCKOUT', 'Товар с отсутствием на складе', 'Обычные', 1, 5),
        ]
        products = {}
        for sku, name, category, minimum, pack in specs:
            product = Product.objects.create(supplier=supplier, sku=sku, article=sku, name=name, category=category, unit='шт', min_order=minimum, pack_multiple=pack)
            products[sku] = product
            StockSnapshot.objects.create(product=product, warehouse='Все', as_of=date(2026, 9, 22), on_hand=5, reserved=0, free=5)
            for year in (2024, 2025, 2026):
                for month in range(1, 13 if year < 2026 else 9):
                    value = 40 if sku == 'DEMO-SEASON' and month == 10 else 12 if sku == 'DEMO-BULK' else 15
                    if sku == 'DEMO-STOCKOUT' and year == 2026 and month == 6:
                        value = 3
                    if sku == 'DEMO-BULK' and year == 2026 and month == 8:
                        value = 1012
                    MonthlySale.objects.create(product=product, month=date(year, month, 1), quantity=value)
        bulk = products['DEMO-BULK']
        for document, quantity in [('normal-aug', 12), ('one-off-project', 1000)]:
            SaleLine.objects.create(product=bulk, sold_at=timezone.make_aware(datetime(2026, 8, 14, 12)), document=document, document_type='Расходная', warehouse='Все', quantity=quantity)
        StockoutPeriod.objects.create(product=products['DEMO-STOCKOUT'], warehouse='Все', starts=date(2026, 6, 1), ends=date(2026, 6, 24))
        InboundLine.objects.create(product=products['DEMO-SEASON'], reference='DEMO-PO', eta=date(2026, 10, 5), quantity=3)
        self.stdout.write(self.style.SUCCESS('Создан синтетический поставщик demo. Дата расчёта: 2026-09-22, склад: Все.'))
