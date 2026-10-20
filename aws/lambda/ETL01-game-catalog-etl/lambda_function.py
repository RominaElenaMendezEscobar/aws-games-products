import gzip
import json
from dataclasses import dataclass, field

import boto3
import numpy as np
import pandas as pd

s3 = boto3.client("s3")
CHUNK_SIZE = 10_000


# ---------------------------------------------------------------------------
# Cleaning rules (derived from the EDA)
# ---------------------------------------------------------------------------

@dataclass
class CleaningConfig:
    required_fields: list = field(default_factory=lambda: ["title", "categories"])
    drop_columns: list = field(default_factory=lambda: [
        "bought_together", "subtitle", "author", "videos", "main_category",
    ])
    drop_detail_keys: set = field(default_factory=lambda: {
        "Pricing", "Best Sellers Rank", "Is Discontinued By Manufacturer",
    })
    null_tokens: set = field(default_factory=lambda: {"", "—", "-", "N/A", "None"})


class CatalogCleaner:
    def __init__(self, config: CleaningConfig = None):
        self.config = config or CleaningConfig()
        self._report = []

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        self._report = []
        self._log("raw data", len(df), len(df))
        df = df.copy()
        df = self._drop_columns(df)
        df = self._normalize_disguised_nulls(df)
        df = self._drop_missing_required(df)
        df = self._clean_details(df)
        df = self._clean_price(df)
        df = self._strip_text(df)
        return df.reset_index(drop=True)

    def filter_scope(self, df: pd.DataFrame, category: str) -> pd.DataFrame:
        before = len(df)
        out = df[df["categories"].apply(lambda c: category in c)].reset_index(drop=True)
        self._log(f"scope: '{category}'", before, len(out))
        return out

    @property
    def report(self) -> pd.DataFrame:
        return pd.DataFrame(self._report)

    def _drop_columns(self, df):
        cols = [c for c in self.config.drop_columns if c in df.columns]
        self._log("drop columns", len(df), len(df))
        return df.drop(columns=cols)

    def _normalize_disguised_nulls(self, df):
        for col in df.select_dtypes(include="object").columns:
            mask = df[col].apply(self._is_empty)
            df.loc[mask, col] = None
        self._log("normalize disguised nulls", len(df), len(df))
        return df

    def _drop_missing_required(self, df):
        for col in self.config.required_fields:
            before = len(df)
            df = df[df[col].notna()]
            self._log(f"drop missing '{col}'", before, len(df))
        return df

    def _clean_details(self, df):
        keys = self.config.drop_detail_keys
        df["details"] = df["details"].apply(
            lambda d: {k: v for k, v in d.items() if k not in keys} if isinstance(d, dict) else None)
        self._log("clean details", len(df), len(df))
        return df

    def _clean_price(self, df):
        def parse(x):
            if isinstance(x, (int, float)) and not pd.isna(x):
                return float(x), False
            if isinstance(x, str) and x.lower().startswith("from"):
                try:
                    return float(x.split()[-1]), True
                except ValueError:
                    pass
            return np.nan, False

        parsed = df["price"].apply(parse)
        df["price_value"] = parsed.str[0]
        df["price_is_min"] = parsed.str[1]
        self._log("clean price", len(df), len(df))
        return df.drop(columns="price")

    def _strip_text(self, df):
        df["title"] = df["title"].str.strip()
        df["store"] = df["store"].str.strip()
        self._log("strip text", len(df), len(df))
        return df

    def _is_empty(self, x):
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return True
        if isinstance(x, str):
            return x.strip() in self.config.null_tokens
        if isinstance(x, (list, dict)):
            return len(x) == 0
        return False

    def _log(self, step, before, after):
        self._report.append({"step": step, "rows_before": before,
                             "rows_after": after, "removed": before - after})


# ---------------------------------------------------------------------------
# S3 streaming helpers
# ---------------------------------------------------------------------------

def iter_chunks(bucket: str, key: str, chunk_size: int = CHUNK_SIZE):
    """Read a JSONL (or .jsonl.gz) file from S3 in chunks, without loading it whole."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    lines = gzip.GzipFile(fileobj=body) if key.endswith(".gz") else body.iter_lines()
    batch = []
    for line in lines:
        if line.strip():
            batch.append(json.loads(line))
        if len(batch) == chunk_size:
            yield pd.DataFrame(batch)
            batch = []
    if batch:
        yield pd.DataFrame(batch)


def append_jsonl(df: pd.DataFrame, file_handle) -> None:
    if df.empty:
        return
    text = df.to_json(orient="records", lines=True, force_ascii=False)
    file_handle.write(text if text.endswith("\n") else text + "\n")


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    bucket = event["bucket"]
    key = event["key"]
    scope = event.get("scope_category", "Games")

    cleaner = CatalogCleaner()
    reports = []

    with open("/tmp/clean.jsonl", "w", encoding="utf-8") as f_clean, \
         open("/tmp/mvp.jsonl", "w", encoding="utf-8") as f_mvp:
        for i, chunk in enumerate(iter_chunks(bucket, key)):
            clean = cleaner.run(chunk)
            mvp = cleaner.filter_scope(clean, category=scope)
            reports.append(cleaner.report)
            append_jsonl(clean, f_clean)
            append_jsonl(mvp, f_mvp)
            print(f"chunk {i}: {len(chunk)} raw -> {len(clean)} clean -> {len(mvp)} in scope")

    clean_key = "clean/catalog_clean.jsonl"
    mvp_key = f"mvp/catalog_mvp_{scope.lower()}.jsonl"
    s3.upload_file("/tmp/clean.jsonl", bucket, clean_key)
    s3.upload_file("/tmp/mvp.jsonl", bucket, mvp_key)

    funnel = (pd.concat(reports)
                .groupby("step", sort=False)[["rows_before", "rows_after", "removed"]]
                .sum().reset_index())
    s3.put_object(Bucket=bucket, Key="reports/etl_funnel.json",
                  Body=funnel.to_json(orient="records"), ContentType="application/json")

    result = {
        "bucket": bucket,
        "clean_key": clean_key,
        "mvp_key": mvp_key,
        "funnel": funnel.to_dict(orient="records"),
    }
    print(json.dumps(result, indent=2))
    return result