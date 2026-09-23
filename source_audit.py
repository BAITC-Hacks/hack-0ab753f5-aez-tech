"""Read-only inspection of the supplied XLSX files using Python's standard library."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def colnum(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group()
    n = 0
    for char in letters:
        n = n * 26 + ord(char) - 64
    return n


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    values = []
    with archive.open("xl/sharedStrings.xml") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == f"{{{NS['m']}}}si":
                values.append("".join(t.text or "" for t in element.findall(".//m:t", NS)))
                element.clear()
    return values


def sheets(archive: zipfile.ZipFile):
    root = ET.fromstring(archive.read("xl/workbook.xml"))
    relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relroot}
    for sheet in root.findall(".//m:sheets/m:sheet", NS):
        target = rels[sheet.attrib[f"{{{NS['r']}}}id"]]
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        yield sheet.attrib["name"], target


def inspect(path: Path, sample_size: int):
    print(f"\nFILE\t{path.parent.name}\t{path.name}\t{path.stat().st_size}")
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        for name, target in sheets(archive):
            print(f"SHEET\t{name}\t{target}")
            rows = 0
            nonempty = 0
            maxcol = 0
            formulas = 0
            types = Counter()
            colcounts = Counter()
            samples = []
            last = None
            with archive.open(target) as handle:
                for _, element in ET.iterparse(handle, events=("end",)):
                    if element.tag != f"{{{NS['m']}}}row":
                        continue
                    cells = []
                    for cell in element.findall("m:c", NS):
                        typ = cell.attrib.get("t", "n")
                        raw = cell.findtext("m:v", default="", namespaces=NS)
                        if typ == "inlineStr":
                            raw = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                        elif typ == "s" and raw:
                            raw = strings[int(raw)]
                        if cell.find("m:f", NS) is not None:
                            formulas += 1
                        if raw != "":
                            col = colnum(cell.attrib["r"])
                            maxcol = max(maxcol, col)
                            colcounts[col] += 1
                            types[typ] += 1
                            nonempty += 1
                            raw = str(raw).replace("\n", " ").replace("\t", " ")[:100]
                            cells.append(f"{cell.attrib['r']}={raw}")
                    if cells:
                        rows += 1
                        if len(samples) < sample_size:
                            samples.append(" | ".join(cells[:65]))
                        last = " | ".join(cells[:12])
                    element.clear()
            print(f"STATS\trows={rows}\tcells={nonempty}\tmaxcol={maxcol}\tformulas={formulas}\ttypes={dict(types)}")
            print("COLCOUNTS\t" + " ".join(f"{col}:{count}" for col, count in colcounts.most_common(40)))
            for index, sample in enumerate(samples, 1):
                print(f"SAMPLE{index}\t{sample}")
            print(f"LAST\t{last}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--samples", type=int, default=8)
    args = parser.parse_args()
    for root in args.roots:
        for path in sorted(root.glob("*.xlsx")):
            inspect(path, args.samples)
