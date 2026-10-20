import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

s3 = boto3.client("s3")
s3vectors = boto3.client("s3vectors", config=Config(max_pool_connections=16))
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=16),
)

VECTOR_BUCKET = "game-catalog-vectors"
TOP_K = 20
CANDIDATE_MAX_DISTANCE = 0.25   
GRAY_MAX_DISTANCE = 0.08        
MATCH_FIELDS = ["platform", "edition", "format", "region"]

DECISION_PROMPT = """You compare two e-commerce video game listings and decide how they relate.
- duplicate: the same product (same game, platform, edition and format). Condition such as
  used, renewed or refurbished does NOT make it a different product.
- variant: the same base game, but a different platform, edition, format or region.
- different: different games.
If either listing is not a video game itself (a toy, replica, cheat code disc, XP or bonus code,
accessory or merchandise), the answer is "different".
Return ONLY valid JSON: {"relation": "duplicate | variant | different", "reason": "one short sentence"}"""


def normalize_title(title: str) -> str:
    """Lowercase, drop apostrophes and punctuation: "Link's" == "Links", "Ocarina: of Time" == "Ocarina of Time"."""
    title = re.sub(r"['’]", "", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def compatible(a: str, b: str) -> bool:
    """Equal, or at least one is unknown (unknown is not treated as a difference)."""
    a, b = (a or "unknown").lower(), (b or "unknown").lower()
    return a == b or "unknown" in (a, b)


def list_all_vectors(index_name: str) -> list:
    vectors, token = [], None
    while True:
        params = {"vectorBucketName": VECTOR_BUCKET, "indexName": index_name,
                  "returnData": True, "returnMetadata": True, "maxResults": 500}
        if token:
            params["nextToken"] = token
        resp = s3vectors.list_vectors(**params)
        vectors.extend(resp["vectors"])
        token = resp.get("nextToken")
        if not token:
            return vectors


def find_neighbors(vector: dict, index_name: str) -> list:
    resp = s3vectors.query_vectors(
        vectorBucketName=VECTOR_BUCKET, indexName=index_name,
        queryVector=vector["data"], topK=TOP_K,
        returnMetadata=True, returnDistance=True,
    )
    return [c for c in resp["vectors"]
            if c["key"] != vector["key"] and c["distance"] < CANDIDATE_MAX_DISTANCE]


def rule_decision(a: dict, b: dict, distance: float):
    """Returns (relation, reason) or None when the pair must go to Bedrock."""
    if normalize_title(a.get("base_title")) == normalize_title(b.get("base_title")):
        diffs = [f for f in MATCH_FIELDS if not compatible(a.get(f), b.get(f))]
        if not diffs:
            return "duplicate", "same base_title and compatible attributes"
        return "variant", "differs in " + ", ".join(diffs)
    if distance < GRAY_MAX_DISTANCE:
        return None
    return "different", "different base_title"


def bedrock_decision(a: dict, b: dict, model_id: str) -> tuple:
    fields = ["title", "base_title", "platform", "edition", "format", "region"]
    payload = {"listing_a": {f: a.get(f) for f in fields},
               "listing_b": {f: b.get(f) for f in fields}}
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": DECISION_PROMPT}],
        messages=[{"role": "user", "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 150},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        out = json.loads(text)
        return out.get("relation", "different"), out.get("reason", "")
    except json.JSONDecodeError:
        return "different", f"unparseable answer: {text[:100]}"


def decide(pair: dict, model_id: str) -> dict:
    result = rule_decision(pair["meta_a"], pair["meta_b"], pair["distance"])
    if result:
        relation, reason, method = *result, "rule"
    else:
        relation, reason = bedrock_decision(pair["meta_a"], pair["meta_b"], model_id)
        method = "bedrock"
    return {
        "asin_a": pair["asin_a"], "asin_b": pair["asin_b"],
        "distance": round(pair["distance"], 4),
        "relation": relation, "method": method, "reason": reason,
        "title_a": pair["meta_a"].get("title"), "title_b": pair["meta_b"].get("title"),
    }


def lambda_handler(event, context):
    bucket = event["bucket"]
    run_name = event["run_name"]
    index_name = event.get("index_name", "games-v2")
    model_id = event.get("decision_model_id", "us.amazon.nova-lite-v1:0")

    vectors = list_all_vectors(index_name)

    with ThreadPoolExecutor(max_workers=8) as pool:
        neighbor_lists = list(pool.map(lambda v: find_neighbors(v, index_name), vectors))

    candidate_pairs = {}
    for vector, neighbors in zip(vectors, neighbor_lists):
        for c in neighbors:
            a, b = sorted([vector["key"], c["key"]])
            if (a, b) in candidate_pairs:
                continue
            meta = {vector["key"]: vector["metadata"], c["key"]: c["metadata"]}
            candidate_pairs[(a, b)] = {"asin_a": a, "asin_b": b, "distance": c["distance"],
                                       "meta_a": meta[a], "meta_b": meta[b]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda p: decide(p, model_id), candidate_pairs.values()))

    s3.put_object(
        Bucket=bucket, Key=f"pairs/{run_name}/pairs.jsonl",
        Body="\n".join(json.dumps(d, ensure_ascii=False) for d in decisions).encode("utf-8"),
    )

    gray = [d for d in decisions if d["method"] == "bedrock"]
    summary = {
        "run_name": run_name,
        "index_name": index_name,
        "decision_model_id": model_id,
        "vectors": len(vectors),
        "candidate_pairs": len(decisions),
        "by_relation": dict(Counter(d["relation"] for d in decisions)),
        "by_method": dict(Counter(d["method"] for d in decisions)),
        "bedrock_by_relation": dict(Counter(d["relation"] for d in gray)),
        "bedrock_examples": [{k: d[k] for k in ("title_a", "title_b", "relation", "reason")}
                             for d in gray[:8]],
    }
    s3.put_object(Bucket=bucket, Key=f"reports/{run_name}/pairs_summary.json",
                  Body=json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"))
    print(json.dumps(summary, ensure_ascii=False))
    return summary