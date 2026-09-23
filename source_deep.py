"""Targeted checks of source reconciliation and existing planning columns."""

from collections import Counter
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from source_audit import NS, colnum, shared_strings, sheets
from source_metrics import ROOT, number, rows


def reconciliation(tx_path, monthly_path, sku_col, start_row, first_col):
    tx = Counter()
    for no, r in rows(tx_path):
        if no == 1 or r.get(1) == "Итого":
            continue
        m = re.match(r"\d{2}\.(\d{2})\.(\d{4})", r.get(1, ""))
        q = number(r.get(8))
        if m and q is not None and r.get(4):
            tx[(r[4], f"{m[2]}-{m[1]}")] += q
    monthly = {}
    for no, r in rows(monthly_path):
        if no < start_row or r.get(1) == "Итого":
            continue
        sku = r.get(sku_col)
        if not sku:
            continue
        for offset in range(33):
            col=first_col+offset
            month=f"{2024+offset//12}-{offset%12+1:02}"
            monthly[(sku,month)] = number(r.get(col)) or 0.0
    for month in ("2024-08","2025-01","2025-08","2026-01","2026-08","2026-09"):
        keys={sku for sku,m in tx if m==month} | {sku for sku,m in monthly if m==month}
        t=sum(q for (sku,m),q in tx.items() if m==month)
        a=sum(q for (sku,m),q in monthly.items() if m==month)
        equal=sum(abs(tx.get((sku,month),0)-monthly.get((sku,month),0))<0.001 for sku in keys)
        mismatches=sorted(((abs(tx.get((sku,month),0)-monthly.get((sku,month),0)),sku,tx.get((sku,month),0),monthly.get((sku,month),0)) for sku in keys),reverse=True)[:3]
        print("RECON",tx_path.parent.name,month,"tx",round(t,2),"monthly",round(a,2),"equal",equal,"of",len(keys),"largest",mismatches)


def selected_rows(path, selected, maxcol=60):
    print("SELECTED",path.parent.name,path.name)
    for no, r in rows(path):
        if no in selected:
            print("ROW",no," | ".join(f"{col}={r[col]}" for col in sorted(r) if col<=maxcol))


def formulas(path, selected, columns):
    print("FORMULAS",path.parent.name,path.name)
    with zipfile.ZipFile(path) as archive:
        target=list(sheets(archive))[0][1]
        with archive.open(target) as handle:
            for _, row in ET.iterparse(handle,events=("end",)):
                if row.tag != f"{{{NS['m']}}}row":
                    continue
                no=int(row.attrib["r"])
                if no in selected:
                    for c in row.findall("m:c",NS):
                        col=colnum(c.attrib["r"])
                        if col in columns:
                            print(no,c.attrib["r"],"formula",c.findtext("m:f",default="",namespaces=NS),"cached",c.findtext("m:v",default="",namespaces=NS))
                row.clear()


def main():
    a=ROOT/"Systeme electric"
    b=ROOT/"IEK"
    reconciliation(a/"Динамика продаж_Syseme Electric_2025-2026.xlsx",a/"Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx",2,3,5)
    reconciliation(b/"Динамика продаж_2025-2026.xlsx",b/"Ежемесячные продажи в количественном выражении за последние 2 года.xlsx",2,3,3)
    selected_rows(a/"Товар в пути_SystemElectric на 22.09.2026.xlsx",{2,3,4},60)
    formulas(a/"Товар в пути_SystemElectric на 22.09.2026.xlsx",{3,4,5},{42,43,44,45,46,50,51,52,53,54,55})
    selected_rows(b/"Сезонность ИЭК.xlsx",set(range(7,16)),15)
    selected_rows(a/"Сезонность SystemElectric 2024-2026.xlsx",set(range(7,16)),15)


if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
