"""
Pull Yahoo Finance's Top ETF lists for selected sectors
and combine them into one CSV.

Install:
    pip install --upgrade yfinance pandas
"""

from datetime import datetime, timezone
from pathlib import Path
import time

import pandas as pd
import yfinance as yf


OUTPUT_FILE = Path("yahoo_sector_etfs.csv")

# Display name -> Yahoo Finance sector key
SECTORS = {
    "Technology": "technology",
    "Healthcare": "healthcare",
    "Industrials": "industrials",
    "Financials": "financial-services",
    "Consumer Discretionary": "consumer-cyclical",
    "Consumer Staples": "consumer-defensive",
    "Energy": "energy",
    "Telecom": "communication-services",
}


def get_sector_etfs(
    sector_name: str,
    sector_key: str,
    retrieved_at: str,
    max_attempts: int = 4,
) -> list[dict]:
    """Download one Yahoo Finance sector's Top ETFs list."""

    for attempt in range(1, max_attempts + 1):
        try:
            sector = yf.Sector(sector_key, region="US")
            etfs = sector.top_etfs

            if not isinstance(etfs, dict) or not etfs:
                raise RuntimeError(
                    f"Yahoo returned no ETFs for {sector_name}."
                )

            source_url = (
                "https://finance.yahoo.com/research-hub/screener/"
                f"sec-ind_sec-top-etfs_{sector_key}/"
            )

            return [
                {
                    "sector": sector_name,
                    "yahoo_sector_key": sector_key,
                    "rank": rank,
                    "ticker": ticker,
                    "etf_name": etf_name,
                    "source_url": source_url,
                    "retrieved_at_utc": retrieved_at,
                }
                for rank, (ticker, etf_name) in enumerate(
                    etfs.items(),
                    start=1,
                )
            ]

        except Exception as exc:
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Failed to retrieve {sector_name} after "
                    f"{max_attempts} attempts: {exc}"
                ) from exc

            wait_seconds = 2 ** attempt
            print(
                f"{sector_name}: attempt {attempt} failed. "
                f"Retrying in {wait_seconds} seconds..."
            )
            time.sleep(wait_seconds)

    return []


def main() -> None:
    retrieved_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    all_rows: list[dict] = []

    for sector_name, sector_key in SECTORS.items():
        print(f"Retrieving {sector_name}...")

        rows = get_sector_etfs(
            sector_name=sector_name,
            sector_key=sector_key,
            retrieved_at=retrieved_at,
        )

        all_rows.extend(rows)
        print(f"  Retrieved {len(rows)} ETFs")

        # Reduce the chance of Yahoo rate-limiting the requests.
        time.sleep(2)

    if not all_rows:
        raise RuntimeError("No ETF data was retrieved.")

    df = pd.DataFrame(all_rows)

    # Remove accidental duplicates without changing Yahoo's ranking order.
    df = df.drop_duplicates(
        subset=["sector", "ticker"],
        keep="first",
    )

    df = df.sort_values(
        ["sector", "rank"],
        kind="stable",
    ).reset_index(drop=True)

    # Write to a temporary file first so a failed run does not overwrite
    # an existing valid output file.
    temporary_file = OUTPUT_FILE.with_suffix(".tmp.csv")
    df.to_csv(
        temporary_file,
        index=False,
        encoding="utf-8-sig",
    )
    temporary_file.replace(OUTPUT_FILE)

    print(
        f"\nSaved {len(df)} ETF records across "
        f"{df['sector'].nunique()} sectors to:"
    )
    print(OUTPUT_FILE.resolve())


if __name__ == "__main__":
    main()