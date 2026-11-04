"""Generate a sample inventory spreadsheet for testing.

Usage: python make_sample.py [output.xlsx]
"""
import sys

from openpyxl import Workbook

rows = [
    ["Asset Tag", "Description", "Location", "Status"],
    ["AST-001", "Label printer", "Warehouse A", "Active"],
    ["AST-002", "Handheld scanner", "Warehouse A", "Active"],
    ["AST-003", "Forklift tablet", "Dock 3", "Active"],
    ["AST-004", "Label printer", "Warehouse B", "Active"],
    ["AST-005", "Handheld scanner", "Warehouse B", "Retired"],
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_inventory.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    for row in rows:
        ws.append(row)
    wb.save(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
