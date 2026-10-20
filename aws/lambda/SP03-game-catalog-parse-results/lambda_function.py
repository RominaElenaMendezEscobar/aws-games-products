import json
from collections import Counter

import boto3

s3 = boto3.client("s3")

ATTRIBUTES = ["product_type", "base_title", "platform", "edition",
              "format", "region", "publisher", "confidence"]
KEEP_FROM_PRODUCT = ["title", "store", "price_value", "rating_number"]


def load_products(bucket: str, key: str) -> dict:
    """Index the original products by parent_asin, keeping only the fields needed later."""
    products = {}
    for line in s3.get_object(Bucket=bucket, Key=key)["Body"].iter_lines():
        if line.strip():
            p = json.loads(line)
            products[p["parent_asin"]] = {k: p.get(k) for k in KEEP_FROM_PRODUCT}
    return products


def parse_model_text(record: dict) -> dict:
    """The model answer is a JSON string inside the response: parse it into a dict."""
    text = record["modelOutput"]["output"]["message"]["content"][0]["text"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def lambda_handler(event, context):
    bucket = event["bucket"]
    run_name = event["run_name"]
    input_key = event.get("input_key", f"bedrock-output/{run_name}/requests.jsonl.out")
    mvp_key = event.get("mvp_key", "mvp/catalog_mvp_games.jsonl")

    products = load_products(bucket, mvp_key)
    rows, failures = [], []

    for line in s3.get_object(Bucket=bucket, Key=input_key)["Body"].iter_lines():
        if not line.strip():
            continue
        record = json.loads(line)
        asin = record["recordId"]
        base = {"parent_asin": asin, **products.get(asin, {})}

        if "error" in record:
            failures.append({**base, "reason": "model_error", "detail": record["error"]})
            continue
        try:
            attrs = parse_model_text(record)
            row = {**base, **{a: attrs.get(a, "unknown") for a in ATTRIBUTES}}
            row["product_type"] = str(row["product_type"]).strip().lower()   # NEW: "Game"/"DLC" -> "game"/"dlc"
            rows.append(row)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            failures.append({**base, "reason": "invalid_json", "detail": str(e)})

    games = [r for r in rows if r["product_type"] == "game"]

    def write_jsonl(key, items):
        s3.put_object(Bucket=bucket, Key=key,
                      Body="\n".join(json.dumps(i, ensure_ascii=False) for i in items).encode("utf-8"))

    write_jsonl(f"attributes/{run_name}/attributes_all.jsonl", rows)
    write_jsonl(f"attributes/{run_name}/attributes_games.jsonl", games)
    if failures:
        write_jsonl(f"attributes/{run_name}/failures.jsonl", failures)

    quality = {
        "run_name": run_name,
        "records": len(rows) + len(failures),
        "parsed_ok": len(rows),
        "failures": len(failures),
        "games": len(games),
        "product_type": dict(Counter(r["product_type"] for r in rows)),
        "platforms": dict(Counter(r["platform"] for r in games)),            
        "confidence": dict(Counter(r["confidence"] for r in rows)),
        "unknown_rate": {a: round(sum(r[a] == "unknown" for r in rows) / max(len(rows), 1), 3)
                         for a in ["platform", "edition", "format", "region", "publisher"]},
    }
    s3.put_object(Bucket=bucket, Key=f"reports/{run_name}/extraction_quality.json",
                  Body=json.dumps(quality, indent=2).encode("utf-8"))

    print(json.dumps(quality))
    return quality