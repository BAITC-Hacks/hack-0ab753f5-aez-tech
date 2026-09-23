from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand

from planner.importers import import_file
from planner.models import PlanningPolicy, Supplier


class Command(BaseCommand):
    help = 'Import the supplied Systeme Electric and IEK Excel sample packs.'

    def add_arguments(self, parser):
        parser.add_argument('--source-root', type=Path, required=True, help='Directory containing "Systeme electric" and "IEK" subdirectories')
        parser.add_argument('--force', action='store_true', help='Replace previously imported data even when the file hash is unchanged')

    def handle(self, *args, **options):
        root = options['source_root']
        packs = [
            ('systeme', 'Systeme Electric', 'Systeme electric', [
                ('moq', 'MOQ SystemElectric.xlsx'),
                ('sales_monthly', 'Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx'),
                ('stock_monthly', 'Ежемесячные остатки SystemElectric 2024-2026.xlsx'),
                ('transactions', 'Динамика продаж_Syseme Electric_2025-2026.xlsx'),
                ('system_snapshot', 'Товар в пути_SystemElectric на 22.09.2026.xlsx'),
            ]),
            ('iek', 'IEK', 'IEK', [
                ('moq', 'MOQ  ИЭК.xlsx'),
                ('sales_monthly', 'Ежемесячные продажи в количественном выражении за последние 2 года.xlsx'),
                ('stock_monthly', 'Ежемесячные остатки продукции за последние 2 года  ИЭК.xlsx'),
                ('transactions', 'Динамика продаж_2025-2026.xlsx'),
                ('inbound_iek', 'Путь ИЭК 22.09.2026.xlsx'),
            ]),
        ]
        for code, name, folder, files in packs:
            supplier, _ = Supplier.objects.get_or_create(code=code, defaults={'name': name})
            PlanningPolicy.objects.get_or_create(supplier=supplier, category='', defaults={
                'lead_days': 30, 'review_days': 30, 'safety_days': 15, 'confirmed': False,
            })
            for kind, filename in files:
                path = root / folder / filename
                if not path.is_file():
                    raise FileNotFoundError(path)
                as_of = date(2026, 9, 22) if kind == 'system_snapshot' else None
                batch, created = import_file(supplier, kind, filename, path.read_bytes(), as_of, force=options['force'])
                self.stdout.write(f'{supplier.code}: {kind}: {batch.row_count} строк, предупреждений {len(batch.warnings)}' + ('' if created else ' (уже загружено)'))
        self.stdout.write(self.style.SUCCESS('Импорт завершён. Политики поставщиков — демонстрационные и не подтверждены.'))
