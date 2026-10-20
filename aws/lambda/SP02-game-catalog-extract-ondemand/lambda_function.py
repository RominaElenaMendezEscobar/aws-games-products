import json
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=16),
)

DEFAULT_MODEL_ID = "us.amazon.nova-micro-v1:0"


def invoke(record: dict, model_id: str) -> dict:
    """Send one batch-format request to the model. Output mimics Bedrock batch output."""
    try:
        resp = bedrock.invoke_model(modelId=model_id, body=json.dumps(record["modelInput"]))
        return {"recordId": record["recordId"], "modelOutput": json.loads(resp["body"].read())}
    except Exception as e:
        return {"recordId": record["recordId"],
                "error": {"errorCode": type(e).__name__, "errorMessage": str(e)}}


def lambda_handler(event, context):
    bucket = event["bucket"]
    input_key = event["input_key"]
    output_key = event.get(
        "output_key",
        input_key.replace("bedrock-input/", "bedrock-output/", 1) + ".out",
    )
    model_id = event.get("model_id", DEFAULT_MODEL_ID)
    max_workers = event.get("max_workers", 8)

    body = s3.get_object(Bucket=bucket, Key=input_key)["Body"]
    records = [json.loads(line) for line in body.iter_lines() if line.strip()]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda r: invoke(r, model_id), records))

    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body="\n".join(json.dumps(r, ensure_ascii=False) for r in results).encode("utf-8"),
    )

    ok = [r for r in results if "modelOutput" in r]
    summary = {
        "output_s3_uri": f"s3://{bucket}/{output_key}",
        "records": len(results),
        "succeeded": len(ok),
        "errors": len(results) - len(ok),
        "input_tokens": sum(r["modelOutput"]["usage"]["inputTokens"] for r in ok),
        "output_tokens": sum(r["modelOutput"]["usage"]["outputTokens"] for r in ok),
    }
    print(json.dumps(summary))
    return summary