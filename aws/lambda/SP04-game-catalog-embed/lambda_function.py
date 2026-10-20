import json
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

s3 = boto3.client("s3")
s3vectors = boto3.client("s3vectors")
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=16),
)

EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024
PUT_BATCH_SIZE = 500   # max vectors per PutVectors call


def identity_text(row: dict) -> str:
    """The text that represents the product's identity (clean attributes, no noise)."""
    parts = [row["base_title"], row["platform"], row["edition"], row["format"]]
    return " | ".join(p for p in parts if p and p != "unknown")


def embed(text: str) -> list:
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
    )
    return json.loads(resp["body"].read())["embedding"]


def to_vector(row: dict) -> dict:
    return {
        "key": row["parent_asin"],
        "data": {"float32": embed(identity_text(row))},
        "metadata": {
            # filterable (used to narrow the search)
            "platform": row["platform"],
            "edition": row["edition"],
            "format": row["format"],
            "region": row["region"],
            # non-filterable (only to display results)
            "base_title": row["base_title"],
            "title": (row.get("title") or "")[:500],
        },
    }


def lambda_handler(event, context):
    bucket = event["bucket"]
    run_name = event["run_name"]
    input_key = event.get("input_key", f"attributes/{run_name}/attributes_games.jsonl")
    vector_bucket = event.get("vector_bucket", "game-catalog-vectors")
    index_name = event.get("index_name", "games-v2")

    body = s3.get_object(Bucket=bucket, Key=input_key)["Body"]
    rows = [json.loads(line) for line in body.iter_lines() if line.strip()]

    with ThreadPoolExecutor(max_workers=8) as pool:
        vectors = list(pool.map(to_vector, rows))

    for i in range(0, len(vectors), PUT_BATCH_SIZE):
        s3vectors.put_vectors(
            vectorBucketName=vector_bucket,
            indexName=index_name,
            vectors=vectors[i:i + PUT_BATCH_SIZE],
        )

    summary = {
        "vectors_written": len(vectors),
        "index": f"{vector_bucket}/{index_name}",
        "example_identity_text": identity_text(rows[0]) if rows else None,
    }
    print(json.dumps(summary))
    return summary