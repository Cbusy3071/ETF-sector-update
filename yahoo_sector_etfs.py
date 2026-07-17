"""Pull Yahoo Finance sector ETF data and create raw sector files plus a compact Excel file."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import re
import time
from typing import Any, Callable

import pandas as pd
import yfinance as yf


RAW_DIR = Path("data/raw")
EXCEL_DIR = Path("data/excel")
COMPACT_FILE = EXCEL_DIR / "sector_etfs_compact.csv"

SECTORS = {
    "Technology": ("technology", "sec-ind_sec-top-etfs_technology"),
    "Healthcare": ("healthcare", "sec-ind_sec-top-etfs_healthcare"),
    "Industrials": ("industrials", "sec-ind_sec-top-etfs_industrials"),
    "Financials": ("financial-services", "sec-ind_sec-top-etfs_financial-services"),
    "Consumer Discretionary": ("consumer-cyclical", "sec-ind_sec-top-etfs_consumer-cyclical"),
    "Consumer Staples": ("consumer-defensive", "sec-ind_sec-top-etfs_consumer-defensive"),
    "Energy": ("energy", "sec-ind_sec-top-etfs_energy"),
    "Telecom": ("communication-services", "sec-ind_sec-top-etfs_communication-services"),
}


def snake(text: Any) -> str:
    value = str(text)
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def clean(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, dict):
        if "raw" in value:
            return clean(value["raw"])
        return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def flatten(data: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in (data or {}).items():
        column = f"{prefix}{snake(key)}"
        if isinstance(value, dict) and "raw" not in value:
            output.update(flatten(value, f"{column}_"))
        else:
            output[column] = clean(value)
    return output


def request(label: str, function: Callable[[], Any], attempts: int = 4) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return function()
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"{label} failed: {exc}") from exc
            wait_seconds = 2 ** (attempt - 1)
            print(f"{label} failed; retrying in {wait_seconds}s: {exc}", flush=True)
            time.sleep(wait_seconds)
    raise RuntimeError(f"{label} failed unexpectedly")


def safe_numeric(value: Any) -> float | None:
    try:
        if value is None or value is pd.NA:
            return None
        number = float(value)
        if math.isnan(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def percent_value(value: Any) -> float | None:
    number = safe_numeric(value)
    if number is None:
        return None
    return number * 100 if abs(number) <= 2 else number


def first_series(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    existing = [column for column in columns if column in df.columns]
    if not existing:
        return pd.Series(pd.NA, index=df.index, dtype="object")
    result = df[existing[0]].copy()
    for column in existing[1:]:
        result = result.combine_first(df[column])
    return result


def value_from_operations(operations: pd.DataFrame, symbol: str, labels: list[str]) -> Any:
    if not isinstance(operations, pd.DataFrame) or operations.empty:
        return None
    value_columns = [c for c in operations.columns if str(c).lower() != "category average"]
    if not value_columns:
        return None
    value_column = symbol if symbol in operations.columns else value_columns[0]
    targets = {snake(label) for label in labels}
    for index_value, row in operations.iterrows():
        if snake(index_value) in targets:
            return clean(row.get(value_column))
    return None


def extract_equity_metric(equity_holdings: pd.DataFrame, labels: list[str]) -> Any:
    if not isinstance(equity_holdings, pd.DataFrame) or equity_holdings.empty:
        return None
    targets = {snake(label) for label in labels}
    for index_value, row in equity_holdings.iterrows():
        if snake(index_value) in targets:
            for column in equity_holdings.columns:
                if "category" not in snake(column):
                    value = clean(row.get(column))
                    if value is not None:
                        return value
    for column in equity_holdings.columns:
        if snake(column) in targets:
            series = equity_holdings[column].dropna()
            if not series.empty:
                return clean(series.iloc[0])
    return None


def top_ten_weight(top_holdings: pd.DataFrame) -> float | None:
    if not isinstance(top_holdings, pd.DataFrame) or top_holdings.empty:
        return None
    weight_column = next(
        (c for c in top_holdings.columns if snake(c) in {"holding_percent", "weight", "percent_assets"}),
        None,
    )
    if weight_column is None:
        return None
    weights = pd.to_numeric(top_holdings[weight_column], errors="coerce").dropna()
    if weights.empty:
        return None
    result = float(weights.head(10).sum())
    return result * 100 if abs(result) <= 2 else result


def fund_metadata(ticker: yf.Ticker, symbol: str) -> dict[str, Any]:
    funds = ticker.get_funds_data()
    if funds is None:
        return {}

    output: dict[str, Any] = {"fund_description": clean(getattr(funds, "description", None))}
    output.update(flatten(getattr(funds, "fund_overview", None), "fund_overview_"))
    output.update(flatten(getattr(funds, "asset_classes", None), "fund_asset_class_"))
    output.update(flatten(getattr(funds, "sector_weightings", None), "fund_sector_weight_"))
    output.update(flatten(getattr(funds, "bond_ratings", None), "fund_bond_rating_"))

    operations = getattr(funds, "fund_operations", None)
    if isinstance(operations, pd.DataFrame) and not operations.empty:
        value_columns = [c for c in operations.columns if str(c).lower() != "category average"]
        if value_columns:
            value_column = symbol if symbol in operations.columns else value_columns[0]
            for attribute, row in operations.iterrows():
                key = snake(attribute)
                output[f"fund_{key}"] = clean(row.get(value_column))
                output[f"fund_{key}_category_average"] = clean(row.get("Category Average"))

        output["compact_annual_holdings_turnover_pct"] = percent_value(
            value_from_operations(
                operations,
                symbol,
                ["Annual Holdings Turnover", "Holdings Turnover", "Turnover", "Annual Turnover"],
            )
        )
        output["compact_expense_ratio_pct"] = percent_value(
            value_from_operations(
                operations,
                symbol,
                ["Annual Report Expense Ratio", "Expense Ratio", "Net Expense Ratio"],
            )
        )

    top_holdings = getattr(funds, "top_holdings", None)
    equity_holdings = getattr(funds, "equity_holdings", None)
    bond_holdings = getattr(funds, "bond_holdings", None)

    output["compact_top_10_holdings_weight_pct"] = top_ten_weight(top_holdings)

    if isinstance(top_holdings, pd.DataFrame) and not top_holdings.empty:
        output["fund_top_holdings_json"] = top_holdings.reset_index().to_json(orient="records")

    if isinstance(equity_holdings, pd.DataFrame) and not equity_holdings.empty:
        output["fund_equity_holdings_json"] = equity_holdings.reset_index().to_json(orient="records")
        output["compact_price_to_earnings"] = extract_equity_metric(
            equity_holdings,
            ["Price/Earnings", "Price Earnings", "Price/Earnings Ratio", "P/E"],
        )
        output["compact_price_to_book"] = extract_equity_metric(
            equity_holdings,
            ["Price/Book", "Price Book", "Price/Book Ratio", "P/B"],
        )
        output["compact_weighted_average_market_cap"] = extract_equity_metric(
            equity_holdings,
            ["Median Market Cap", "Average Market Cap", "Weighted Average Market Cap", "Market Cap"],
        )

    if isinstance(bond_holdings, pd.DataFrame) and not bond_holdings.empty:
        output["fund_bond_holdings_json"] = bond_holdings.reset_index().to_json(orient="records")

    return output


def ticker_metadata(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    output: dict[str, Any] = {"ticker": symbol}
    errors: list[str] = []

    try:
        info = request(f"{symbol} info", lambda: ticker.get_info() or {})
        output.update(flatten(info, "info_"))
    except Exception as exc:
        errors.append(f"info: {exc}")

    try:
        output.update(
            request(
                f"{symbol} fund data",
                lambda: fund_metadata(ticker, symbol),
                attempts=3,
            )
        )
    except Exception as exc:
        errors.append(f"fund data: {exc}")

    if errors:
        output["retrieval_errors"] = " | ".join(errors)

    return output


def build_compact(df: pd.DataFrame) -> pd.DataFrame:
    compact = pd.DataFrame(index=df.index)

    compact["sector"] = df["sector"]
    compact["rank"] = df["rank"]
    compact["ticker"] = df["ticker"]
    compact["etf_name"] = df["etf_name"]
    compact["fund_family"] = first_series(df, ["info_fund_family", "fund_overview_family", "fund_overview_fund_family"])
    compact["category"] = first_series(df, ["info_category", "fund_overview_category", "screen_category_name"])
    compact["inception_date"] = first_series(df, ["info_fund_inception_date", "fund_overview_inception_date", "screen_fund_inception_date"])
    compact["total_assets"] = first_series(df, ["info_total_assets", "screen_total_assets", "screen_net_assets"])
    compact["expense_ratio_pct"] = first_series(
        df,
        [
            "compact_expense_ratio_pct",
            "info_annual_report_expense_ratio",
            "fund_annual_report_expense_ratio",
            "screen_net_expense_ratio",
            "screen_expense_ratio",
        ],
    ).map(percent_value)
    compact["one_year_return_pct"] = first_series(
        df,
        [
            "screen_total_return_1_year",
            "screen_total_return_1y",
            "screen_one_year_return",
            "info_one_year_return",
            "info_52_week_change",
        ],
    ).map(percent_value)
    compact["five_year_return_annualised_pct"] = first_series(
        df,
        [
            "screen_total_return_5_year",
            "screen_total_return_5_year_annualized",
            "screen_total_return_5y",
            "info_five_year_average_return",
            "info_5_year_average_return",
        ],
    ).map(percent_value)
    compact["beta_3y"] = first_series(
        df,
        ["info_beta3_year", "info_beta_3_year", "screen_beta3_year", "screen_beta_3_year", "screen_beta"],
    )

    current_price = pd.to_numeric(
        first_series(df, ["info_current_price", "info_regular_market_price", "screen_regular_market_price", "screen_price"]),
        errors="coerce",
    )
    high_52 = pd.to_numeric(
        first_series(df, ["info_fifty_two_week_high", "screen_fifty_two_week_high", "screen_52_week_high"]),
        errors="coerce",
    )
    compact["distance_from_52_week_high_pct"] = ((current_price / high_52) - 1) * 100

    compact["distribution_yield_pct"] = first_series(
        df,
        ["info_yield", "info_trailing_annual_dividend_yield", "screen_yield", "screen_trailing_annual_dividend_yield"],
    ).map(percent_value)
    compact["number_of_holdings"] = first_series(
        df,
        ["info_holdings", "info_number_of_holdings", "fund_overview_holdings", "fund_overview_number_of_holdings", "screen_holdings_count"],
    )
    compact["top_10_holdings_weight_pct"] = first_series(df, ["compact_top_10_holdings_weight_pct"])
    compact["annual_holdings_turnover_pct"] = first_series(
        df,
        ["compact_annual_holdings_turnover_pct", "fund_annual_holdings_turnover", "fund_holdings_turnover"],
    ).map(percent_value)
    compact["price_to_earnings"] = first_series(df, ["compact_price_to_earnings", "info_trailing_pe", "screen_trailing_pe"])
    compact["price_to_book"] = first_series(df, ["compact_price_to_book", "info_price_to_book", "screen_price_to_book"])
    compact["weighted_average_market_cap"] = first_series(
        df,
        ["compact_weighted_average_market_cap", "fund_median_market_cap", "fund_average_market_cap"],
    )
    compact["currency"] = first_series(df, ["info_currency", "screen_currency"])
    compact["retrieved_at_utc"] = df["retrieved_at_utc"]

    return (
        compact.drop_duplicates(subset=["sector", "ticker"], keep="first")
        .sort_values(["sector", "rank"], kind="stable")
        .reset_index(drop=True)
    )


def write_csv_atomic(df: pd.DataFrame, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = output_file.with_suffix(".tmp.csv")
    df.to_csv(temporary_file, index=False, encoding="utf-8-sig")
    temporary_file.replace(output_file)


def main() -> None:
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    EXCEL_DIR.mkdir(parents=True, exist_ok=True)

    all_sector_rows: list[dict[str, Any]] = []

    for sector, (sector_key, screener_id) in SECTORS.items():
        print(f"\nFetching {sector} screener...", flush=True)
        result = request(
            f"{sector} screener",
            lambda sid=screener_id: yf.screen(sid, count=250),
        )
        quotes = result.get("quotes", [])
        if not quotes:
            raise RuntimeError(f"Yahoo returned no ETFs for {sector}")

        sector_rows: list[dict[str, Any]] = []
        for rank, quote in enumerate(quotes, start=1):
            symbol = quote.get("symbol")
            if not symbol:
                continue
            row = {
                "sector": sector,
                "yahoo_sector_key": sector_key,
                "rank": rank,
                "ticker": symbol,
                "etf_name": quote.get("longName") or quote.get("shortName"),
                "source_url": f"https://finance.yahoo.com/research-hub/screener/{screener_id}/",
                "retrieved_at_utc": retrieved_at,
            }
            row.update(flatten(quote, "screen_"))
            sector_rows.append(row)
            all_sector_rows.append(row)

        write_csv_atomic(
            pd.DataFrame(sector_rows),
            RAW_DIR / f"{snake(sector)}_screener.csv",
        )

    sector_df = pd.DataFrame(all_sector_rows)
    symbols = sorted(sector_df["ticker"].dropna().astype(str).unique())

    metadata: list[dict[str, Any]] = []
    for number, symbol in enumerate(symbols, start=1):
        print(f"ETF details {number}/{len(symbols)}: {symbol}", flush=True)
        metadata.append(ticker_metadata(symbol))
        time.sleep(0.35)

    full_df = sector_df.merge(pd.DataFrame(metadata), on="ticker", how="left")

    for sector in SECTORS:
        sector_raw = (
            full_df.loc[full_df["sector"] == sector]
            .drop_duplicates(subset=["ticker"], keep="first")
            .sort_values("rank", kind="stable")
            .reset_index(drop=True)
        )
        write_csv_atomic(sector_raw, RAW_DIR / f"{snake(sector)}.csv")

    compact_df = build_compact(full_df)
    write_csv_atomic(compact_df, COMPACT_FILE)

    print(
        f"Saved {len(compact_df)} compact rows and "
        f"{len(compact_df.columns)} columns to {COMPACT_FILE.resolve()}",
        flush=True,
    )


if __name__ == "__main__":
    main()
