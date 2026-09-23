"""Explainable, deterministic order recommendations."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
import calendar
from statistics import median

from django.db import transaction

from .models import (
    CalculationRun, ImportBatch, InboundLine, MonthlySale, MonthlyStock,
    PlanningPolicy, Product, Recommendation, SaleLine, StockoutPeriod,
    StockSnapshot,
)


def month_key(day):
    return date(day.year, day.month, 1)


def previous_month(day, offset=1):
    serial = day.year * 12 + day.month - 1 - offset
    return date(serial // 12, serial % 12 + 1, 1)


def stockout_days(periods, month):
    month_last = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    covered = set()
    for period in periods:
        start = max(period['starts'], month)
        end = min(period['ends'], month_last)
        if end >= start:
            covered.update(range(start.day, end.day + 1))
    return len(covered)


def seasonal_indices(month_values):
    """Return month multipliers from complete years, normalized around one."""
    by_year = defaultdict(dict)
    for month, value in month_values.items():
        by_year[month.year][month.month] = max(0.0, value)
    ratios = defaultdict(list)
    for year, values in by_year.items():
        if len(values) != 12 or sum(v > 0 for v in values.values()) < 6:
            continue
        mean = sum(values.values()) / 12
        if mean:
            for month, value in values.items():
                ratios[month].append(value / mean)
    return {month: min(2.5, max(0.4, median(values))) for month, values in ratios.items()}


def rounded_order(raw, minimum, multiple, factor):
    if raw <= 0:
        return Decimal(0)
    if not minimum or not multiple or minimum <= 0 or multiple <= 0 or factor <= 0:
        return Decimal(0)
    # Minimum and multiple are purchase units; forecast and stock are stock units.
    purchase_units = max(Decimal(str(raw)) / factor, minimum)
    packs = (purchase_units / multiple).to_integral_value(rounding=ROUND_CEILING)
    return packs * multiple


@transaction.atomic
def calculate(supplier, as_of, warehouse='Все', user=None, allow_estimated_stock=False, category=''):
    product_query = Product.objects.filter(supplier=supplier)
    if category:
        product_query = product_query.filter(category=category)
    products = list(product_query.order_by('sku'))
    ids = [p.id for p in products]
    history = defaultdict(dict)
    reported = defaultdict(dict)
    category_history = defaultdict(lambda: defaultdict(float))
    current_month = month_key(as_of)
    for item in MonthlySale.objects.filter(product_id__in=ids, month__lt=current_month).values('product_id', 'month', 'quantity', 'reported'):
        qty = float(item['quantity'])
        history[item['product_id']][item['month']] = qty
        reported[item['product_id']][item['month']] = item['reported']
    product_by_id = {p.id: p for p in products}
    for pid, values in history.items():
        category = product_by_id[pid].category or '__uncategorized__'
        for month, value in values.items():
            category_history[category][month] += max(0, value)
    category_season = {name: seasonal_indices(values) for name, values in category_history.items()}

    docs = defaultdict(lambda: defaultdict(float))
    transaction_month = defaultdict(float)
    lower_bound = previous_month(current_month, 12)
    for line in SaleLine.objects.filter(product_id__in=ids, sold_at__date__gte=lower_bound, sold_at__date__lt=current_month, document_type='Расходная').values('product_id', 'sold_at', 'document', 'quantity', 'customer_hash'):
        month = month_key(line['sold_at'].date())
        pid = line['product_id']
        qty = float(line['quantity'])
        transaction_month[(pid, month)] += qty
        if qty > 0:
            # A client hash joins its documents only when the upstream export supplies it.
            identifier = line['customer_hash'] or line['document']
            docs[pid][(month, identifier)] += qty

    stocks = defaultdict(list)
    for stock in StockSnapshot.objects.filter(product_id__in=ids, warehouse=warehouse, as_of__lte=as_of).values('product_id', 'as_of', 'free'):
        stocks[stock['product_id']].append(stock)
    fallback_stock = defaultdict(list)
    if allow_estimated_stock:
        for stock in MonthlyStock.objects.filter(product_id__in=ids, month__lte=current_month).values('product_id', 'month', 'opening_quantity'):
            if stock['opening_quantity'] is not None:
                fallback_stock[stock['product_id']].append(stock)
    inbound = defaultdict(list)
    for line in InboundLine.objects.filter(product_id__in=ids, eta__gt=as_of, status='confirmed').values('product_id', 'eta', 'quantity'):
        inbound[line['product_id']].append(line)
    stockouts = defaultdict(list)
    for period in StockoutPeriod.objects.filter(product_id__in=ids, warehouse=warehouse, starts__lt=current_month).values('product_id', 'starts', 'ends'):
        stockouts[period['product_id']].append(period)
    policies = {p.category: p for p in PlanningPolicy.objects.filter(supplier=supplier)}
    batches = list(ImportBatch.objects.filter(supplier=supplier).values('kind', 'sha256', 'filename', 'as_of', 'imported_at'))
    latest = {}
    for batch in batches:
        latest.setdefault(batch['kind'], batch)
    source_batches = [{k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in batch.items()} for batch in latest.values()]
    run = CalculationRun.objects.create(supplier=supplier, as_of=as_of, warehouse=warehouse, category=category, allow_estimated_stock=allow_estimated_stock, created_by=user, source_batches=source_batches)
    recommendations = []

    for product in products:
        pid = product.id
        values = history[pid]
        if not values and not docs[pid]:
            continue
        blockers = []
        if not values:
            blockers.append('Нет помесячной истории продаж.')
        policy = policies.get(product.category) or policies.get('')
        if policy is None:
            blockers.append('Не задан срок поставки и политика запаса.')
        elif not policy.confirmed:
            blockers.append('Срок поставки и запас заданы как демонстрационные допущения.')
        if product.min_order is None or product.pack_multiple is None:
            blockers.append('Нет подтверждённой минимальной партии или кратности.')
        if product.purchase_to_stock_factor <= 0:
            blockers.append('Некорректный перевод закупочной единицы в учётную.')
        if product.requires_conversion and product.purchase_to_stock_factor == 1:
            blockers.append('Нужно подтвердить пересчёт закупочной единицы в учётную (например, бухты в метры).')

        stock = max(stocks[pid], key=lambda x: x['as_of']) if stocks[pid] else None
        stock_date = stock['as_of'] if stock else None
        free = float(stock['free']) if stock else None
        if stock is None and fallback_stock[pid]:
            estimate = max(fallback_stock[pid], key=lambda x: x['month'])
            stock_date = estimate['month']
            free = float(estimate['opening_quantity'])
            blockers.append('Использован начальный остаток месяца вместо актуального свободного остатка.')
        elif stock is None:
            blockers.append('Нет актуального свободного остатка.')
        elif (as_of - stock['as_of']).days > 7:
            blockers.append('Снимок свободного остатка старше 7 дней.')

        months = [previous_month(current_month, i) for i in range(12, 0, -1)]
        monthly = {m: max(0.0, values.get(m, 0.0)) for m in months}
        positive_docs = list(docs[pid].values())
        doc_typical = median(positive_docs) if len(positive_docs) >= 3 else 0
        positive_months = [v for v in monthly.values() if v > 0]
        month_typical = median(positive_months) if positive_months else 0
        anomaly_threshold = max(10.0, 5 * doc_typical, 3 * month_typical)
        anomaly_removed = 0.0
        mismatch_months = []
        for month in months:
            doc_total = transaction_month.get((pid, month))
            if doc_total is None:
                continue
            report_total = values.get(month, 0.0)
            if abs(doc_total - report_total) > max(5.0, 0.1 * abs(report_total)):
                mismatch_months.append(month.isoformat())
                continue
            excess = sum(max(0.0, amount - anomaly_threshold) for (doc_month, _), amount in docs[pid].items() if doc_month == month)
            excess = min(excess, monthly[month])
            monthly[month] -= excess
            anomaly_removed += excess
        if mismatch_months:
            blockers.append(f'Транзакции не сходятся с помесячным отчётом в {len(mismatch_months)} мес.')

        regular_positive = [v for v in monthly.values() if v > 0]
        reference = median(regular_positive) if regular_positive else 0.0
        lost_total = 0.0
        for month in months:
            out_days = stockout_days(stockouts[pid], month)
            if not out_days:
                continue
            days = calendar.monthrange(month.year, month.month)[1]
            available = days - out_days
            daily = monthly[month] / available if available > 0 else reference / days
            lost = min(daily * out_days, max(reference * 2, daily * out_days if reference == 0 else 0))
            monthly[month] += lost
            lost_total += lost

        recent = [monthly[m] for m in months[-6:]]
        active = sum(v > 0 for v in recent)
        level = median(recent) if active >= 4 else sum(monthly.values()) / 12
        seasonal_values = {m: max(0.0, v) for m, v in values.items()}
        seasonal_values.update(monthly)
        sku_season = seasonal_indices(seasonal_values)
        seasonal = sku_season if sku_season else category_season.get(product.category or '__uncategorized__', {})
        seasonal_source = 'SKU' if sku_season else 'категория' if seasonal else 'нейтральный коэффициент'
        same_last_year = [previous_month(current_month, i + 12) for i in (1, 2, 3)]
        recent_three = [previous_month(current_month, i) for i in (1, 2, 3)]
        prior = [values.get(m, 0) for m in same_last_year]
        current = [monthly.get(m, 0) for m in recent_three]
        cap = float(policy.max_growth) if policy else 0.3
        growth = min(cap, max(0.0, sum(current) / sum(prior) - 1)) if sum(prior) > 0 and all(a > b for a, b in zip(current, prior)) else 0.0

        lead = policy.lead_days if policy else 30
        review = policy.review_days if policy else 30
        safety_days = policy.safety_days if policy else 0
        horizon = as_of + timedelta(days=lead + review)
        forecast = 0.0
        day = as_of + timedelta(days=1)
        while day <= horizon:
            factor = seasonal.get(day.month, 1.0)
            forecast += level * factor * (1 + growth) / calendar.monthrange(day.year, day.month)[1]
            day += timedelta(days=1)
        safety = level / 30.44 * safety_days
        arriving = sum(float(line['quantity']) for line in inbound[pid] if line['eta'] <= horizon)
        raw = max(0.0, forecast + safety - (free or 0) - arriving) if free is not None else 0.0
        quantity = rounded_order(raw, product.min_order, product.pack_multiple, product.purchase_to_stock_factor)
        daily_demand = forecast / max(1, lead + review)
        stock_days = (free / daily_demand) if free is not None and daily_demand > 0 else None
        urgency = 'высокая' if stock_days is not None and stock_days < lead else 'средняя' if stock_days is not None and stock_days < lead + review else 'обычная'
        detail = {
            'monthly_level': round(level, 3), 'seasonality_source': seasonal_source,
            'seasonal_factors': {str(k): round(v, 3) for k, v in seasonal.items()},
            'growth': round(growth, 4), 'stockout_compensation': round(lost_total, 3),
            'one_off_excluded': round(anomaly_removed, 3), 'mismatch_months': mismatch_months,
            'forecast_horizon': round(forecast, 3), 'safety_stock': round(safety, 3),
            'free_stock': free, 'stock_date': stock_date.isoformat() if stock_date else None,
            'inbound_before_horizon': round(arriving, 3), 'raw_need': round(raw, 3),
            'lead_days': lead, 'review_days': review, 'safety_days': safety_days,
            'min_order': str(product.min_order) if product.min_order else None,
            'pack_multiple': str(product.pack_multiple) if product.pack_multiple else None,
            'purchase_to_stock_factor': str(product.purchase_to_stock_factor),
        }
        explanation = (
            f'Прогноз {forecast:.1f} + страховой запас {safety:.1f} − свободный остаток '
            f'{free if free is not None else "нет данных"} − поступления {arriving:.1f} = '
            f'{raw:.1f} ед. учёта до округления. Сезонность: {seasonal_source}; рост {growth:.1%}; '
            f'исключено разовых продаж {anomaly_removed:.1f}; восстановлено за stockout {lost_total:.1f}.'
        )
        recommendations.append(Recommendation(run=run, product=product, suggested_quantity=quantity, final_quantity=quantity, urgency=urgency, explanation=explanation, details=detail, blockers=blockers))
    Recommendation.objects.bulk_create(recommendations, batch_size=1000)
    return run
