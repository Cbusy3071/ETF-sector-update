"""Pull Yahoo sector ETF screeners and all available ETF metadata into one CSV."""

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import re
import time

import pandas as pd
import yfinance as yf

OUTPUT_FILE = Path("yahoo_sector_etfs.csv")

# Display name: (Yahoo sector key, saved screener ID)
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


def snake(text):
    text = str(text)
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def clean(value):
    """Keep scalars as scalars and encode nested objects as JSON."""
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


def flatten(data, prefix=""):
    """Flatten every key Yahoo returns so new fields appear automatically."""
    output = {}

    for key, value in (data or {}).items():
        column = f"{prefix}{snake(key)}"

        if isinstance(value, dict) and "raw" not in value:
            output.update(flatten(value, f"{column}_"))
        else:
            output[column] = clean(value)

    return output


def request(label, function, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            return function()
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"{label} failed: {exc}") from exc
            wait = 2 ** (attempt - 1)
            print(f"{label} failed; retrying in {wait}s: {exc}")
            time.sleep(wait)


def fund_metadata(ticker, symbol):
    """Pull fund overview, fees, assets, ratings and sector weights."""
    funds = ticker.get_funds_data()
    if funds is None:
        return {}

    output = {"fund_description": funds.description}
    output.update(flatten(funds.fund_overview, "fund_overview_"))
    output.update(flatten(funds.asset_classes, "fund_asset_class_"))
    output.update(flatten(funds.sector_weightings, "fund_sector_weight_"))
    output.update(flatten(funds.bond_ratings, "fund_bond_rating_"))

    operations = funds.fund_operations
    if isinstance(operations, pd.DataFrame) and not operations.empty:
        value_columns = [c for c in operations.columns if c != "Category Average"]
        value_column = symbol if symbol in operations.columns else value_columns[0]

        for attribute, row in operations.iterrows():
            key = snake(attribute)
            output[f"fund_{key}"] = clean(row.get(value_column))
            output[f"fund_{key}_category_average"] = clean(
                row.get("Category Average")
            )

    # Preserve non-scalar tables without losing them.
    for name in ("top_holdings", "equity_holdings", "bond_holdings"):
        table = getattr(funds, name)
        if isinstance(table, pd.DataFrame) and not table.empty:
            output[f"fund_{name}_json"] = table.reset_index().to_json(
                orient="records"
            )

    return output


def ticker_metadata(symbol):
    ticker = yf.Ticker(symbol)
    output = {"ticker": symbol}
    errors = []

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


def first_available(df, columns):
    columns = [c for c in columns if c in df.columns]
    if not columns:
        return pd.Series(pd.NA, index=df.index)

    result = df[columns[0]].copy()
    for column in columns[1:]:
        result = result.combine_first(df[column])
    return result


def main():
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sector_rows = []

    for sector, (sector_key, screener_id) in SECTORS.items():
        print(f"Fetching {sector}...")
        result = request(
            f"{sector} screener",
            lambda sid=screener_id: yf.screen(sid, count=250),
        )
        quotes = result.get("quotes", [])

        if not quotes:
            raise RuntimeError(f"Yahoo returned no ETFs for {sector}")

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
                "source_url": (
                    "https://finance.yahoo.com/research-hub/screener/"
                    f"{screener_id}/"
                ),
                "retrieved_at_utc": retrieved_at,
            }
            row.update(flatten(quote, "screen_"))
            sector_rows.append(row)

    sector_df = pd.DataFrame(sector_rows)
    symbols = sorted(sector_df["ticker"].dropna().unique())

    metadata = []
    for number, symbol in enumerate(symbols, start=1):
        print(f"ETF details {number}/{len(symbols)}: {symbol}")
        metadata.append(ticker_metadata(symbol))
        time.sleep(0.35)

    df = sector_df.merge(pd.DataFrame(metadata), on="ticker", how="left")

    # Stable, readable columns for the fields you specifically asked for.
    df["morningstar_overall_rating"] = first_available(
        df,
        [
            "info_morning_star_overall_rating",
            "screen_morning_star_overall_rating",
            "screen_performance_rating_overall",
        ],
    )
    df["morningstar_risk_rating"] = first_available(
        df,
        [
            "info_morning_star_risk_rating",
            "screen_morning_star_risk_rating",
            "screen_risk_rating_overall",
        ],
    )
    df["expense_ratio_decimal"] = first_available(
        df,
        ["info_annual_report_expense_ratio", "fund_annual_report_expense_ratio"],
    )
    df["expense_ratio_pct"] = (
        pd.to_numeric(df["expense_ratio_decimal"], errors="coerce") * 100
    )
    df["beta"] = first_available(df, ["info_beta", "screen_beta"])
    df["beta_3y"] = first_available(
        df,
        ["info_beta3_year", "screen_beta3_year", "screen_beta_3_year"],
    )

    df = df.drop_duplicates(["sector", "ticker"]).sort_values(
        ["sector", "rank"], kind="stable"
    )

    front = [
        "sector",
        "rank",
        "ticker",
        "etf_name",
        "morningstar_overall_rating",
        "morningstar_risk_rating",
        "expense_ratio_decimal",
        "expense_ratio_pct",
        "beta",
        "beta_3y",
        "source_url",
        "retrieved_at_utc",
        "retrieval_errors",
    ]
    front = [column for column in front if column in df.columns]
    rest = sorted(column for column in df.columns if column not in front)
    df = df[front + rest].reset_index(drop=True)

    temporary_file = OUTPUT_FILE.with_suffix(".tmp.csv")
    df.to_csv(temporary_file, index=False, encoding="utf-8-sig")
    temporary_file.replace(OUTPUT_FILE)

    print(
        f"Saved {len(df)} rows, {len(symbols)} unique ETFs and "
        f"{len(df.columns)} columns to {OUTPUT_FILE.resolve()}"
    )


if __name__ == "__main__":
    main()
