import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from zipfile import BadZipFile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Max, Sum
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from openpyxl.utils.exceptions import InvalidFileException

from .engine import calculate
from .forms import RunForm, UploadForm
from .importers import import_file
from .models import (
    CalculationRun, ImportBatch, InboundLine, MonthlySale, PlanningPolicy,
    Product, Recommendation, SaleLine, StockoutPeriod, StockSnapshot, Supplier,
)


MONTH_NAMES = ('Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек')


def _chart_bars(rows, value_field='value'):
    """Add display widths in Python so dashboards work without client-side JS."""
    maximum = max((float(row[value_field]) for row in rows), default=0)
    for row in rows:
        value = float(row[value_field])
        row['percent'] = round(max(4, value / maximum * 100)) if value and maximum else 0
    return rows


def _latest_runs_by_supplier():
    """SQLite-compatible latest run lookup, one calculation per supplier."""
    latest = []
    seen = set()
    for run in CalculationRun.objects.select_related('supplier').order_by('supplier_id', '-created_at'):
        if run.supplier_id not in seen:
            latest.append(run)
            seen.add(run.supplier_id)
    return latest


def _supplier_readiness(supplier):
    """Describe precisely what can and cannot be calculated from loaded files."""
    products = Product.objects.filter(supplier=supplier)
    product_count = products.count()
    sale_rows = MonthlySale.objects.filter(product__supplier=supplier).count()
    stock_info = StockSnapshot.objects.filter(product__supplier=supplier).aggregate(latest=Max('as_of'))
    stock_count = StockSnapshot.objects.filter(product__supplier=supplier).count()
    inbound_count = InboundLine.objects.filter(product__supplier=supplier).count()
    stockout_count = StockoutPeriod.objects.filter(product__supplier=supplier).count()
    transaction_count = SaleLine.objects.filter(product__supplier=supplier).count()
    hashed_customers = SaleLine.objects.filter(
        product__supplier=supplier,
    ).exclude(customer_hash='').count()
    valid_moq = products.filter(min_order__isnull=False, pack_multiple__isnull=False).count()
    policies = list(PlanningPolicy.objects.filter(supplier=supplier))
    base_policy = next((policy for policy in policies if not policy.category), None)

    sales = {
        'state': 'good' if sale_rows else 'bad',
        'text': f'{sale_rows} помесячных значений' if sale_rows else 'нет количественной истории',
    }
    stock = {
        'state': 'good' if stock_count else 'bad',
        'text': f'снимок на {stock_info["latest"]}' if stock_count else 'нет свободного остатка',
    }
    moq = {
        'state': 'good' if product_count and valid_moq == product_count else 'warn' if valid_moq else 'bad',
        'text': f'{valid_moq} из {product_count} SKU' if product_count else 'нет номенклатуры',
    }
    policy = {
        'state': 'good' if base_policy and base_policy.confirmed else 'warn' if base_policy else 'bad',
        'text': 'подтверждена' if base_policy and base_policy.confirmed else 'рабочее допущение' if base_policy else 'не задана',
    }
    return {
        'supplier': supplier,
        'sales': sales,
        'stock': stock,
        'moq': moq,
        'policy': policy,
        'inbound': {
            'state': 'good' if inbound_count else 'neutral',
            'text': f'{inbound_count} строк поставок' if inbound_count else 'нет строк в пути',
        },
        'stockout': {
            'state': 'good' if stockout_count else 'neutral',
            'text': f'{stockout_count} периодов' if stockout_count else 'нет периодов: компенсация равна 0',
        },
        'customer': {
            'state': 'good' if hashed_customers else 'neutral',
            'text': f'{hashed_customers} строк с hash' if hashed_customers else (
                'аномалии ищутся по документам' if transaction_count else 'нет транзакций'
            ),
        },
        'ready': all(item['state'] == 'good' for item in (sales, stock, moq, policy)),
    }


def _dashboard_context(run):
    if not run:
        return {
            'dashboard_run': None, 'dashboard_stats': None, 'trend_data': [],
            'category_data': [], 'risk_data': [], 'driver_data': [],
            'trend_points': [], 'trend_ticks': [], 'trend_line': '', 'trend_area': '',
            'trend_has_data': False, 'category_has_data': False,
            'risk_has_data': False, 'driver_has_data': False,
        }

    recommendations = list(run.recommendations.select_related('product'))
    orders = [rec for rec in recommendations if rec.final_quantity > 0]
    category_rows = defaultdict(lambda: {'label': 'Без категории', 'quantity': 0.0, 'positions': 0, 'risk': 0})
    urgency = {'высокая': 0, 'средняя': 0, 'обычная': 0}
    lost_demand = one_off_excluded = inbound = 0.0
    for rec in recommendations:
        urgency[rec.urgency] = urgency.get(rec.urgency, 0) + 1
        lost_demand += float(rec.details.get('stockout_compensation') or 0)
        one_off_excluded += float(rec.details.get('one_off_excluded') or 0)
        inbound += float(rec.details.get('inbound_before_horizon') or 0)
        if rec.final_quantity > 0:
            label = rec.product.category or 'Без категории'
            row = category_rows[label]
            row['label'] = label
            row['quantity'] += float(rec.final_quantity)
            row['positions'] += 1
            row['risk'] += rec.urgency == 'высокая'
    category_data = sorted(category_rows.values(), key=lambda row: row['quantity'], reverse=True)[:7]
    risk_data = [
        {'label': 'Высокий риск', 'value': urgency.get('высокая', 0), 'tone': 'critical'},
        {'label': 'Средний риск', 'value': urgency.get('средняя', 0), 'tone': 'warning'},
        {'label': 'Плановый заказ', 'value': urgency.get('обычная', 0), 'tone': 'neutral'},
    ]
    driver_data = [
        {'label': 'Компенсация stockout', 'value': round(lost_demand, 1), 'tone': 'positive'},
        {'label': 'Исключено разовых продаж', 'value': round(one_off_excluded, 1), 'tone': 'negative'},
        {'label': 'Учтено товаров в пути', 'value': round(inbound, 1), 'tone': 'primary'},
    ]

    month_start = date(run.as_of.year - 1, run.as_of.month, 1)
    month_end = date(run.as_of.year, run.as_of.month, 1)
    sales_by_month = {
        item['month']: float(item['total'] or 0)
        for item in MonthlySale.objects.filter(
            product__supplier=run.supplier, month__gte=month_start, month__lt=month_end,
        ).values('month').annotate(total=Sum('quantity'))
    }
    trend_data = []
    year, month = month_start.year, month_start.month
    for _ in range(12):
        current = date(year, month, 1)
        trend_data.append({'label': f'{MONTH_NAMES[month - 1]} {str(year)[2:]}', 'value': round(sales_by_month.get(current, 0), 1)})
        month += 1
        if month == 13:
            month, year = 1, year + 1

    # Fixed SVG coordinates make the graph visible even when JavaScript is blocked.
    chart_width, chart_height = 760, 250
    left, top, right, bottom = 58, 16, 16, 42
    plot_width, plot_height = chart_width - left - right, chart_height - top - bottom
    trend_max = max((item['value'] for item in trend_data), default=0)
    trend_points = []
    for index, item in enumerate(trend_data):
        x = left + plot_width * index / max(len(trend_data) - 1, 1)
        y = top + plot_height - plot_height * item['value'] / trend_max if trend_max else top + plot_height
        trend_points.append({
            **item, 'x': round(x), 'y': round(y),
            'show_label': index % 2 == 0 or index == len(trend_data) - 1,
        })
    trend_line = ' '.join(f"{point['x']},{point['y']}" for point in trend_points)
    trend_area = f"M {left} {top + plot_height} L {trend_line} L {left + plot_width} {top + plot_height} Z"
    trend_ticks = [
        {'y': round(top + plot_height - plot_height * index / 4), 'value': round(trend_max * index / 4, 1)}
        for index in range(5)
    ]

    return {
        'dashboard_run': run,
        'dashboard_stats': {
            'positions': len(orders),
            'quantity': round(sum(float(rec.final_quantity) for rec in orders), 1),
            'critical': urgency.get('высокая', 0),
            'blocked': sum(bool(rec.blockers) for rec in orders),
        },
        'trend_data': trend_data,
        'category_data': _chart_bars(category_data, value_field='quantity'),
        'risk_data': _chart_bars(risk_data),
        'driver_data': _chart_bars(driver_data),
        'trend_points': trend_points,
        'trend_ticks': trend_ticks,
        'trend_line': trend_line,
        'trend_area': trend_area,
        'trend_has_data': trend_max > 0,
        'category_has_data': bool(category_data),
        'risk_has_data': any(item['value'] > 0 for item in risk_data),
        'driver_has_data': any(item['value'] > 0 for item in driver_data),
    }


@login_required
def home(request):
    runs = list(CalculationRun.objects.select_related('supplier').order_by('-created_at')[:30])
    requested_run = request.GET.get('run')
    dashboard_run = next((run for run in runs if str(run.pk) == requested_run), runs[0] if runs else None)
    context = {
        'run_form': RunForm(initial={'as_of': timezone.localdate()}),
        'dashboard_runs': runs, 'selected_run_id': dashboard_run.pk if dashboard_run else None,
        'nav_section': 'dashboard',
    }
    context.update(_dashboard_context(dashboard_run))
    return render(request, 'planner/home.html', context)


@login_required
def orders(request):
    groups = []
    for run in _latest_runs_by_supplier():
        recommendations = run.recommendations.select_related('product').filter(final_quantity__gt=0)
        total = recommendations.count()
        groups.append({
            'run': run,
            'positions': total,
            'quantity': sum(float(rec.final_quantity) for rec in recommendations),
            'critical': recommendations.filter(urgency='высокая').count(),
            'blocked': sum(bool(rec.blockers) for rec in recommendations),
            'status': 'Утверждён' if run.approved else 'Черновик',
        })
    return render(request, 'planner/orders.html', {'groups': groups, 'nav_section': 'orders'})


@login_required
def data_status(request):
    batches = list(ImportBatch.objects.select_related('supplier').order_by('-imported_at'))
    latest = {}
    for batch in batches:
        latest.setdefault((batch.supplier_id, batch.kind), batch)
    rows = []
    for supplier in Supplier.objects.order_by('name'):
        supplier_batches = [batch for (supplier_id, _), batch in latest.items() if supplier_id == supplier.id]
        rows.append({'supplier': supplier, 'batches': sorted(supplier_batches, key=lambda batch: batch.kind)})
    return render(request, 'planner/data.html', {
        'upload_form': UploadForm(), 'supplier_rows': rows,
        'readiness_rows': [_supplier_readiness(supplier) for supplier in Supplier.objects.order_by('name')],
        'nav_section': 'data',
    })


@login_required
@require_POST
def upload(request):
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f'Проверьте поля загрузки: {form.errors.as_text()}')
        return redirect('home')
    supplier = form.cleaned_data['supplier']
    kind = form.cleaned_data['kind']
    as_of = form.cleaned_data['as_of']
    if kind in ('system_snapshot', 'stock_csv') and not as_of:
        messages.error(request, 'Для снимка остатков укажите дату.')
        return redirect('home')
    uploaded = form.cleaned_data['file']
    try:
        batch, created = import_file(supplier, kind, uploaded.name, uploaded.read(), as_of)
    except (ValueError, KeyError, IndexError, BadZipFile, InvalidFileException, UnicodeDecodeError, IntegrityError) as exc:
        messages.error(request, f'Импорт отменён: {exc}')
    else:
        messages.success(request, f'{"Импортировано" if created else "Уже загружено"}: {batch.row_count} строк; предупреждений {len(batch.warnings)}.')
    return redirect('home')


@login_required
@require_POST
def new_run(request):
    form = RunForm(request.POST)
    if not form.is_valid():
        messages.error(request, f'Проверьте параметры расчёта: {form.errors.as_text()}')
        return redirect('home')
    run = calculate(form.cleaned_data['supplier'], form.cleaned_data['as_of'], form.cleaned_data['warehouse'], request.user, form.cleaned_data['allow_estimated_stock'], form.cleaned_data['category'].strip())
    messages.success(request, f'Расчёт #{run.id} завершён.')
    return redirect('run_detail', pk=run.pk)


@login_required
def run_detail(request, pk):
    run = get_object_or_404(CalculationRun.objects.select_related('supplier'), pk=pk)
    qs = run.recommendations.select_related('product').all()
    only = request.GET.get('only', 'orders')
    if only == 'orders':
        qs = qs.filter(final_quantity__gt=0)
    elif only == 'blocked':
        qs = qs.exclude(blockers=[])
    query = request.GET.get('q', '').strip()
    if query:
        qs = qs.filter(Q(product__sku__icontains=query) | Q(product__article__icontains=query) | Q(product__name__icontains=query))
    page = Paginator(qs.order_by('product__sku'), 50).get_page(request.GET.get('page'))
    order_rows = run.recommendations.filter(final_quantity__gt=0)
    blocked_orders = order_rows.exclude(blockers=[]).count()
    order_summary = {
        'quantity': order_rows.aggregate(total=Sum('final_quantity'))['total'] or 0,
        'critical': order_rows.filter(urgency='высокая').count(),
        'changed': order_rows.exclude(changed_reason='').count(),
        'blocked': blocked_orders,
        'ready': order_rows.exists() and not blocked_orders,
    }
    return render(request, 'planner/run.html', {
        'run': run, 'page': page, 'only': only, 'query': query,
        'nav_section': 'orders',
        'total': run.recommendations.count(),
        'orders': order_rows.count(),
        'blocked_orders': blocked_orders,
        'order_summary': order_summary,
    })


@login_required
@require_POST
def edit_recommendation(request, pk):
    rec = get_object_or_404(Recommendation.objects.select_related('run', 'product'), pk=pk)
    if rec.run.approved:
        messages.error(request, 'Утверждённый расчёт нельзя изменять.')
        return redirect('run_detail', pk=rec.run_id)
    reason = request.POST.get('reason', '').strip()
    try:
        value = Decimal(request.POST.get('quantity', ''))
    except InvalidOperation:
        value = Decimal(-1)
    if not reason or not value.is_finite() or value < 0:
        messages.error(request, 'Укажите неотрицательное количество и причину правки.')
        return redirect('run_detail', pk=rec.run_id)
    if value > 0:
        if rec.product.min_order and value < rec.product.min_order:
            messages.error(request, 'Количество меньше минимальной партии.')
            return redirect('run_detail', pk=rec.run_id)
        if rec.product.pack_multiple and value % rec.product.pack_multiple:
            messages.error(request, 'Количество не кратно упаковке.')
            return redirect('run_detail', pk=rec.run_id)
    rec.final_quantity = value
    rec.changed_reason = reason
    rec.changed_by = request.user
    rec.changed_at = timezone.now()
    rec.save(update_fields=['final_quantity', 'changed_reason', 'changed_by', 'changed_at'])
    messages.success(request, f'Позиция {rec.product.sku} обновлена.')
    return redirect('run_detail', pk=rec.run_id)


@login_required
@require_POST
def approve_run(request, pk):
    if not request.user.has_perm('planner.can_approve_order'):
        raise PermissionDenied('Нет права утверждать заказы.')
    run = get_object_or_404(CalculationRun, pk=pk)
    if run.approved:
        return redirect('run_detail', pk=run.pk)
    selected = run.recommendations.filter(final_quantity__gt=0)
    if not selected.exists():
        messages.error(request, 'В черновике нет позиций с положительным количеством.')
    elif selected.exclude(blockers=[]).exists():
        messages.error(request, 'Есть позиции с блокирующими проблемами. Исправьте данные или исключите эти позиции из заказа.')
    else:
        run.approved_at = timezone.now()
        run.approved_by = request.user
        run.save(update_fields=['approved_at', 'approved_by'])
        messages.success(request, 'Заказ утверждён. Доступна выгрузка CSV.')
    return redirect('run_detail', pk=run.pk)


@login_required
def export_run(request, pk):
    run = get_object_or_404(CalculationRun.objects.select_related('supplier'), pk=pk)
    if not run.approved:
        messages.error(request, 'Экспорт доступен после утверждения.')
        return redirect('run_detail', pk=pk)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="order_{run.supplier.code}_{run.as_of.isoformat()}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Код 1с', 'Артикул поставщика', 'Наименование', 'Количество закупки', 'Единица учёта', 'Поставщик', 'Расчёт'])
    for rec in run.recommendations.filter(final_quantity__gt=0).select_related('product').order_by('product__sku'):
        writer.writerow([rec.product.sku, rec.product.article, rec.product.name, rec.final_quantity, rec.product.unit, run.supplier.name, run.id])
    return response
