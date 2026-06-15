"""
One-time migration: copy all Excel cache files -> SQLite .db files.
Run once: python migrate_to_sqlite.py
"""
import sqlite3
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

# (source xlsx, target db, is_stock_keyed)
MIGRATIONS = [
    ("price_volume.xlsx",  "price_volume.db",  True),
    ("large_holder.xlsx",  "large_holder.db",  True),
    ("broker_branch.xlsx", "broker_branch.db", True),
    ("signal_log.xlsx",    "signal_log.db",    False),
    ("taiex.xlsx",         "taiex.db",         False),
]


def migrate_stock_keyed(excel_path: Path, db_path: Path) -> None:
    print("  Loading {} ...".format(excel_path.name))
    sheets = pd.read_excel(excel_path, sheet_name=None, dtype=str)
    print("  {} sheets found".format(len(sheets)))

    with sqlite3.connect(db_path) as conn:
        for stock_id, df in sheets.items():
            df = df.copy()
            df["stock_id"] = str(stock_id)
            df.to_sql("data", conn, if_exists="append", index=False)

        print("  Creating index ...")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_data_sid_date ON data(stock_id, date)"
        )

    print("  -> {} done ({} stocks)\n".format(db_path.name, len(sheets)))


def migrate_flat(excel_path: Path, db_path: Path) -> None:
    print("  Loading {} ...".format(excel_path.name))
    sheets = pd.read_excel(excel_path, sheet_name=None, dtype=str)

    with sqlite3.connect(db_path) as conn:
        for sheet_name, df in sheets.items():
            df.to_sql(sheet_name, conn, if_exists="replace", index=False)

    print("  -> {} done\n".format(db_path.name))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for xlsx_name, db_name, stock_keyed in MIGRATIONS:
        xlsx_path = DATA_DIR / xlsx_name
        db_path   = DATA_DIR / db_name

        if not xlsx_path.exists():
            print("Skipping {} (not found)\n".format(xlsx_name))
            continue

        if db_path.exists():
            print("Skipping {} (target {} already exists)\n".format(xlsx_name, db_name))
            continue

        print("Migrating {} -> {}".format(xlsx_name, db_name))
        if stock_keyed:
            migrate_stock_keyed(xlsx_path, db_path)
        else:
            migrate_flat(xlsx_path, db_path)

    print("Migration complete.")
