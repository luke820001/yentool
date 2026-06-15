import pandas as pd
import openpyxl
from pathlib import Path
from datetime import datetime, timedelta
from config.settings import ROLLING_DAYS


def _get_cutoff_date() -> str:
    cutoff = datetime.today() - timedelta(days=ROLLING_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def load_sheet(file_path: Path, sheet_name: str) -> pd.DataFrame:
    if not file_path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(file_path, sheet_name=sheet_name, dtype=str)
        return df
    except Exception:
        return pd.DataFrame()


def _recover_workbook(file_path: Path, skip_sheet: str) -> dict:
    """
    Best-effort recovery of sheets from a corrupted Excel file.
    Returns {sheet_name: DataFrame} for every sheet that could be read.
    """
    recovered = {}
    wb = None
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        for sname in wb.sheetnames:
            if sname == skip_sheet:
                continue
            try:
                rows = list(wb[sname].values)
                if rows:
                    headers = [str(c) if c is not None else "" for c in rows[0]]
                    recovered[sname] = pd.DataFrame(
                        [list(r) for r in rows[1:]], columns=headers
                    )
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
    return recovered


def save_sheet(df: pd.DataFrame, file_path: Path, sheet_name: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.exists():
        # Manage ExcelWriter manually so we can force-close the handle on failure
        # before attempting unlink (avoids WinError 32 on Windows).
        ew = None
        append_ok = False
        try:
            ew = pd.ExcelWriter(
                file_path, engine="openpyxl", mode="a", if_sheet_exists="replace"
            )
            df.to_excel(ew, sheet_name=sheet_name, index=False)
            ew.close()
            append_ok = True
        except Exception:
            try:
                if ew is not None:
                    ew.close()
            except Exception:
                pass

        if append_ok:
            return

        # workbook is corrupted — recover intact sheets then rewrite
        recovered = _recover_workbook(file_path, skip_sheet=sheet_name)
        print("  [WARN] {} corrupted — rebuilding ({} sheets recovered)".format(
            file_path.name, len(recovered)))
        file_path.unlink(missing_ok=True)
        with pd.ExcelWriter(file_path, engine="openpyxl", mode="w") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            for sname, sdf in recovered.items():
                try:
                    sdf.to_excel(writer, sheet_name=sname, index=False)
                except Exception:
                    pass
    else:
        with pd.ExcelWriter(file_path, engine="openpyxl", mode="w") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)


def apply_rolling_window(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return df
    cutoff = _get_cutoff_date()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df[df[date_col] >= cutoff].copy()
    df[date_col] = df[date_col].dt.strftime("%Y-%m-%d")
    return df


def upsert_and_trim(
    file_path: Path,
    sheet_name: str,
    new_df: pd.DataFrame,
    date_col: str,
    key_cols: list,
) -> pd.DataFrame:
    existing = load_sheet(file_path, sheet_name)
    if existing.empty:
        combined = new_df.copy()
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=key_cols, keep="last")
    combined = apply_rolling_window(combined, date_col)
    combined = combined.sort_values(by=key_cols).reset_index(drop=True)
    save_sheet(combined, file_path, sheet_name)
    return combined
