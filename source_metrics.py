"""Read-only source quality checks for the two supplier sample packs."""

from collections import Counter
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from source_audit import NS, colnum, shared_strings, sheets

ROOT = Path(r"C:\Users\Alex\Desktop\материалы HackAlem")


def rows(path: Path, sheet_index=0):
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        target = list(sheets(archive))[sheet_index][1]
        with archive.open(target) as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if element.tag != f"{{{NS['m']}}}row":
                    continue
                out = {}
                for cell in element.findall("m:c", NS):
                    typ = cell.attrib.get("t", "n")
                    value = cell.findtext("m:v", default="", namespaces=NS)
                    if typ == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                    elif typ == "s" and value:
                        value = strings[int(value)]
                    if value != "":
                        out[colnum(cell.attrib["r"])] = value
                yield int(element.attrib["r"]), out
                element.clear()


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def transactions(path):
    dates = []
    years = Counter()
    months = Counter()
    sign = Counter()
    docs = Counter()
    warehouses = Counter()
    skus = set()
    missing = Counter()
    max_abs = (0, "", "", "")
    for rowno, row in rows(path):
        if rowno == 1 or row.get(1) == "Итого":
            continue
        date = row.get(1, "")
        match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", date)
        if match:
            iso = f"{match[3]}-{match[2]}-{match[1]}"
            dates.append(iso)
            years[match[3]] += 1
            months[f"{match[3]}-{match[2]}"] += 1
        else:
            missing["date"] += 1
        sku = row.get(4)
        if sku:
            skus.add(sku)
        else:
            missing["sku"] += 1
        qty = number(row.get(8))
        if qty is None:
            missing["qty"] += 1
        else:
            sign["positive" if qty > 0 else "negative" if qty < 0 else "zero"] += 1
            if abs(qty) > max_abs[0]:
                max_abs = (abs(qty), str(qty), sku, row.get(2, ""))
        docs[row.get(3, "").split(" ")[0]] += 1
        warehouses[row.get(7, "")] += 1
    print("TX", path.parent.name, "rows", sum(years.values()), "dates", min(dates), max(dates), "years", dict(years), "sign", dict(sign), "sku", len(skus), "missing", dict(missing), "max", max_abs)
    print("TX_DOC", docs.most_common(10), "WAREHOUSES", warehouses.most_common(15), "months_tail", sorted(months.items())[-5:])
    return skus


def monthly(path, sku_col, start_row, first_month_col, last_month_col, kind):
    skus = set()
    duplicate = Counter()
    negative = 0
    zero = 0
    blank = 0
    numeric = 0
    latest = Counter()
    values = []
    for rowno, row in rows(path):
        if rowno < start_row or row.get(1) == "Итого":
            continue
        sku = row.get(sku_col)
        if not sku:
            continue
        skus.add(sku)
        duplicate[sku] += 1
        for col in range(first_month_col, last_month_col+1):
            val = number(row.get(col))
            if val is None:
                blank += 1
            else:
                numeric += 1
                if val < 0:
                    negative += 1
                elif val == 0:
                    zero += 1
        v = number(row.get(last_month_col))
        latest["blank" if v is None else "negative" if v < 0 else "zero" if v == 0 else "positive"] += 1
        values.append((sku, v))
    print(kind, path.parent.name, "rows", sum(duplicate.values()), "unique", len(skus), "duplicate_keys", sum(x-1 for x in duplicate.values() if x>1), "monthly", {"numeric":numeric,"blank":blank,"zero":zero,"negative":negative}, "latest", dict(latest))
    return skus


def moq(path, sku_col, qty_col):
    skus=set(); dup=Counter(); vals=Counter(); errors=[]
    for rowno, row in rows(path):
        if rowno==1 or row.get(2)=="Итого":
            continue
        sku=row.get(sku_col)
        if not sku: continue
        skus.add(sku);dup[sku]+=1
        n=number(row.get(qty_col))
        if n is None or n <= 0:
            errors.append((rowno,sku,row.get(qty_col)))
        else:
            vals[n]+=1
    print("MOQ",path.parent.name,"rows",sum(dup.values()),"unique",len(skus),"duplicate_keys",sum(x-1 for x in dup.values() if x>1),"qty",vals.most_common(15),"invalid",errors[:20],"invalid_total",len(errors))
    return skus


def transit(path, sku_col, first_qty_col, last_qty_col, start_row):
    skus=set(); positive_skus=set(); nonempty=Counter(); sums=Counter(); invalid=[]
    for rowno,row in rows(path):
        if rowno<start_row: continue
        sku=row.get(sku_col)
        if not sku: continue
        skus.add(sku)
        for col in range(first_qty_col,last_qty_col+1):
            n=number(row.get(col))
            if n is not None:
                nonempty[col]+=1
                sums[col]+=n
                if n>0: positive_skus.add(sku)
                if n<0: invalid.append((rowno,col,n))
    print("TRANSIT",path.parent.name,"rows",len(skus),"positive_skus",len(positive_skus),"filled",dict(nonempty),"sums",dict(sums),"negative",invalid[:10])
    return skus


def intersections(group, tx, sales, stock, minq, transit_skus):
    print("MATCH",group,"tx_sales",len(tx&sales),"tx_only",len(tx-sales),"sales_only",len(sales-tx),"sales_stock",len(sales&stock),"sales_without_stock",len(sales-stock),"sales_moq",len(sales&minq),"sales_without_moq",len(sales-minq),"sales_transit",len(sales&transit_skus))


def main():
    a=ROOT/"Systeme electric"
    b=ROOT/"IEK"
    tx=transactions(a/"Динамика продаж_Syseme Electric_2025-2026.xlsx")
    sa=monthly(a/"Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx",2,3,5,37,"SALES")
    st=monthly(a/"Ежемесячные остатки SystemElectric 2024-2026.xlsx",3,4,5,37,"STOCK")
    mq=moq(a/"MOQ SystemElectric.xlsx",3,5)
    tr=transit(a/"Товар в пути_SystemElectric на 22.09.2026.xlsx",3,55,60,3)
    intersections("Systeme",tx,sa,st,mq,tr)
    tx=transactions(b/"Динамика продаж_2025-2026.xlsx")
    sa=monthly(b/"Ежемесячные продажи в количественном выражении за последние 2 года.xlsx",2,3,3,35,"SALES")
    st=monthly(b/"Ежемесячные остатки продукции за последние 2 года  ИЭК.xlsx",3,4,4,36,"STOCK")
    mq=moq(b/"MOQ  ИЭК.xlsx",2,5)
    tr=transit(b/"Путь ИЭК 22.09.2026.xlsx",1,4,9,2)
    intersections("IEK",tx,sa,st,mq,tr)


if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
