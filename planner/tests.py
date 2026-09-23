from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .engine import calculate, seasonal_indices
from .models import (InboundLine, MonthlySale, PlanningPolicy, Product,
                     SaleLine, StockoutPeriod, StockSnapshot, Supplier)


class RecommendationTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(code='test', name='Test supplier')
        self.policy = PlanningPolicy.objects.create(supplier=self.supplier, lead_days=30, review_days=0, safety_days=0, confirmed=True)
        self.product = Product.objects.create(supplier=self.supplier, sku='001_', min_order=1, pack_multiple=10, category='A')

    def sales(self, product=None, peak_month=None, peak=100, start=2025, end=2026):
        product = product or self.product
        for year in range(start, end + 1):
            for month in range(1, 13):
                quantity = peak if month == peak_month else 10
                MonthlySale.objects.create(product=product, month=date(year, month, 1), quantity=quantity)

    def stock(self, as_of, product=None, quantity=0):
        StockSnapshot.objects.create(product=product or self.product, warehouse='Все', as_of=as_of, on_hand=quantity, free=quantity)

    def test_seasonality_changes_december_forecast(self):
        self.sales(peak_month=12)
        self.stock(date(2026, 5, 31))
        self.stock(date(2026, 11, 30))
        june = calculate(self.supplier, date(2026, 5, 31)).recommendations.get(product=self.product)
        december = calculate(self.supplier, date(2026, 11, 30)).recommendations.get(product=self.product)
        self.assertGreater(december.details['forecast_horizon'], june.details['forecast_horizon'] * 2)
        self.assertEqual(december.details['seasonality_source'], 'SKU')

    def test_stockout_compensation_increases_demand(self):
        self.sales()
        item = MonthlySale.objects.get(product=self.product, month=date(2025, 12, 1))
        item.quantity = 5
        item.save()
        self.stock(date(2026, 11, 30))
        raw = calculate(self.supplier, date(2026, 11, 30)).recommendations.get(product=self.product)
        StockoutPeriod.objects.create(product=self.product, warehouse='Все', starts=date(2025, 12, 1), ends=date(2025, 12, 15))
        corrected = calculate(self.supplier, date(2026, 11, 30)).recommendations.get(product=self.product)
        self.assertGreater(corrected.details['stockout_compensation'], 0)
        self.assertGreater(corrected.details['forecast_horizon'], raw.details['forecast_horizon'])

    def test_one_off_document_is_excluded(self):
        self.sales()
        item = MonthlySale.objects.get(product=self.product, month=date(2026, 11, 1))
        item.quantity = 1010
        item.save()
        self.stock(date(2026, 12, 1))
        from django.utils import timezone
        for document, quantity in [('normal', 10), ('project', 1000)]:
            SaleLine.objects.create(product=self.product, sold_at=timezone.make_aware(__import__('datetime').datetime(2026, 11, 10)), document=document, document_type='Расходная', warehouse='Все', quantity=quantity)
        rec = calculate(self.supplier, date(2026, 12, 1)).recommendations.get(product=self.product)
        self.assertGreater(rec.details['one_off_excluded'], 900)
        self.assertLess(rec.details['forecast_horizon'], 50)

    def test_inbound_and_stock_reduce_order_and_pack_is_respected(self):
        self.sales()
        self.policy.review_days = 30
        self.policy.save()
        self.stock(date(2026, 9, 22), quantity=0)
        first = calculate(self.supplier, date(2026, 9, 22)).recommendations.get(product=self.product)
        InboundLine.objects.create(product=self.product, reference='PO-1', eta=date(2026, 10, 1), quantity=10)
        second = calculate(self.supplier, date(2026, 9, 22)).recommendations.get(product=self.product)
        self.assertLess(second.final_quantity, first.final_quantity)
        self.assertEqual(second.final_quantity % 10, 0)

    def test_category_policy_changes_result(self):
        other = Product.objects.create(supplier=self.supplier, sku='002_', min_order=1, pack_multiple=1, category='B')
        PlanningPolicy.objects.create(supplier=self.supplier, category='B', lead_days=60, review_days=0, safety_days=0, confirmed=True)
        self.sales()
        self.sales(product=other)
        self.stock(date(2026, 9, 22))
        self.stock(date(2026, 9, 22), product=other)
        run = calculate(self.supplier, date(2026, 9, 22))
        self.assertGreater(run.recommendations.get(product=other).details['forecast_horizon'], run.recommendations.get(product=self.product).details['forecast_horizon'])
        category_run = calculate(self.supplier, date(2026, 9, 22), category='B')
        self.assertEqual(category_run.recommendations.count(), 1)

    def test_approval_requires_confirmed_inputs_and_export(self):
        self.sales()
        self.stock(date(2026, 9, 22))
        user = get_user_model().objects.create_superuser(username='buyer', password='strong-pass', email='buyer@example.test')
        self.client.force_login(user)
        run = calculate(self.supplier, date(2026, 9, 22), user=user)
        self.assertEqual(self.client.get('/').status_code, 200)
        detail = self.client.get(f'/run/{run.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Финальная проверка заказа')
        self.assertEqual(self.client.get(f'/run/{run.id}/?only=blocked').status_code, 200)
        response = self.client.post(f'/run/{run.id}/approve/')
        run.refresh_from_db()
        self.assertIsNotNone(run.approved_at)
        self.assertEqual(response.status_code, 302)
        exported = self.client.get(f'/run/{run.id}/export/')
        self.assertEqual(exported.status_code, 200)
        self.assertIn(b'001_', exported.content)

    def test_ordinary_user_cannot_approve(self):
        self.sales()
        self.stock(date(2026, 9, 22))
        user = get_user_model().objects.create_user(username='viewer', password='strong-pass')
        self.client.force_login(user)
        run = calculate(self.supplier, date(2026, 9, 22), user=user)
        self.assertEqual(self.client.post(f'/run/{run.id}/approve/').status_code, 403)
        run.refresh_from_db()
        self.assertIsNone(run.approved_at)

    def test_manual_change_requires_reason_and_is_audited(self):
        self.sales()
        self.stock(date(2026, 9, 22))
        user = get_user_model().objects.create_superuser(username='editor', password='strong-pass', email='editor@example.test')
        self.client.force_login(user)
        run = calculate(self.supplier, date(2026, 9, 22), user=user)
        rec = run.recommendations.get(product=self.product)
        self.client.post(f'/recommendation/{rec.id}/edit/', {'quantity': '20', 'reason': ''})
        rec.refresh_from_db()
        self.assertEqual(rec.changed_reason, '')
        self.client.post(f'/recommendation/{rec.id}/edit/', {'quantity': '20', 'reason': 'Проверенный заказ клиента'})
        rec.refresh_from_db()
        self.assertEqual(rec.final_quantity, 20)
        self.assertEqual(rec.changed_by, user)
        self.assertEqual(rec.changed_reason, 'Проверенный заказ клиента')


class UtilityTests(TestCase):
    def test_incomplete_year_has_no_sku_seasonality(self):
        self.assertEqual(seasonal_indices({date(2025, m, 1): 10 for m in range(1, 7)}), {})


class WorkspaceViewsTests(TestCase):
    """The three operational screens must render from one saved calculation."""

    def setUp(self):
        self.supplier = Supplier.objects.create(code='ui', name='UI supplier')
        PlanningPolicy.objects.create(
            supplier=self.supplier, lead_days=30, review_days=0,
            safety_days=0, confirmed=True,
        )
        self.product = Product.objects.create(
            supplier=self.supplier, sku='UI-001', category='Кабель',
            min_order=1, pack_multiple=1,
        )
        for year in (2025, 2026):
            for month in range(1, 13):
                MonthlySale.objects.create(
                    product=self.product, month=date(year, month, 1),
                    quantity=30 if month == 9 else 10,
                )
        StockSnapshot.objects.create(
            product=self.product, warehouse='Все', as_of=date(2026, 9, 22),
            on_hand=0, free=0,
        )
        self.run = calculate(self.supplier, date(2026, 9, 22))
        self.user = get_user_model().objects.create_user(
            username='dashboard-user', password='strong-pass',
        )
        self.client.force_login(self.user)

    def test_dashboard_exposes_run_kpis_and_charts(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dashboard_run'], self.run)
        self.assertContains(response, '<svg', html=False)
        self.assertContains(response, 'bar-fill', html=False)
        self.assertNotContains(response, 'json_script', html=False)

    def test_orders_and_data_screens_render(self):
        orders = self.client.get(reverse('orders'))
        data = self.client.get(reverse('data_status'))
        self.assertEqual(orders.status_code, 200)
        self.assertContains(orders, self.supplier.name)
        self.assertEqual(data.status_code, 200)
        self.assertContains(data, 'Данные для расчёта')
        self.assertContains(data, 'Готовность данных к расчёту')
