import json
from collections import Counter, defaultdict
from decimal import Decimal

import boto3

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table("game-catalog-products")

CONSTRAINED_FIELDS = ["platform", "edition", "format", "region"]


class UnionFind:
    """Groups items connected by pairs: if A~B and B~C, then A, B and C share a group.
    With constraints, two groups are merged only if their known values do not conflict
    (prevents 'unknown' listings from bridging digital and physical, JP and US, etc.)."""

    def __init__(self, items, attributes=None, fields=None, default_region="US"):
        self.parent = {i: i for i in items}
        self.fields = fields or []
        self.known = {}
        if attributes:
            for i in items:
                self.known[i] = {}
                for f in self.fields:
                    value = attributes[i].get(f)
                    if f == "region" and value in (None, "unknown"):
                        value = default_region   
                    self.known[i][f] = {value} - {None, "unknown"}
        self.blocked = 0

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.fields:
            for f in self.fields:
                if len(self.known[ra][f] | self.known[rb][f]) > 1:
                    self.blocked += 1        
                    return
        root, child = min(ra, rb), max(ra, rb)
        self.parent[child] = root
        if self.fields:
            for f in self.fields:
                self.known[root][f] |= self.known[child][f]

    def groups(self):
        out = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return list(out.values())


def read_jsonl(bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    return [json.loads(line) for line in body.iter_lines() if line.strip()]


def to_dynamo(value):
    """DynamoDB does not accept floats or empty strings."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v not in (None, "")}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def lambda_handler(event, context):
    bucket = event["bucket"]
    run_name = event["run_name"]
    pairs_key = event["pairs_key"]
    attribute_keys = event["attribute_keys"]

    # 1. All products (also the ones without any pair)
    products = {}
    for key in attribute_keys:
        for row in read_jsonl(bucket, key):
            products[row["parent_asin"]] = row

    pairs = [p for p in read_jsonl(bucket, pairs_key)
             if p["asin_a"] in products and p["asin_b"] in products]

    # 2. canonical groups (duplicates, with constraints) and family groups (duplicates + variants)
    canonical_uf = UnionFind(products, attributes=products, fields=CONSTRAINED_FIELDS)
    family_uf = UnionFind(products)
    for p in sorted(pairs, key=lambda p: p["distance"]):      
        if p["relation"] == "duplicate":
            canonical_uf.union(p["asin_a"], p["asin_b"])
            family_uf.union(p["asin_a"], p["asin_b"])
        elif p["relation"] == "variant":
            family_uf.union(p["asin_a"], p["asin_b"])

    # 3. Stable IDs (smallest ASIN in the group) + representative listing
    canonical_groups = canonical_uf.groups()
    for group in canonical_groups:
        canonical_id = f"CAN-{min(group)}"
        family_id = f"FAM-{family_uf.find(group[0])}"
        representative = max(group, key=lambda a: (products[a].get("rating_number") or 0, a))
        for asin in group:
            products[asin].update({
                "canonical_id": canonical_id,
                "family_id": family_id,
                "canonical_size": len(group),
                "is_representative": asin == representative,
            })

    # 4. Save to S3 (traceability) and DynamoDB (serving)
    rows = list(products.values())
    s3.put_object(Bucket=bucket, Key=f"clusters/{run_name}/products_with_ids.jsonl",
                  Body="\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))

    with table.batch_writer(overwrite_by_pkeys=["parent_asin"]) as batch:
        for r in rows:
            batch.put_item(Item=to_dynamo(r))

    # 5. Summary
    family_sizes = Counter(r["family_id"] for r in rows)
    biggest = sorted(canonical_groups, key=len, reverse=True)[:5]
    summary = {
        "run_name": run_name,
        "products": len(rows),
        "canonical_products": len(canonical_groups),
        "families": len(family_sizes),
        "products_with_duplicates": sum(1 for r in rows if r["canonical_size"] > 1),
        "duplicate_listings_removed": len(rows) - len(canonical_groups),
        "merges_blocked_by_constraints": canonical_uf.blocked,
        "largest_duplicate_groups": [
            {"canonical_id": f"CAN-{min(g)}", "listings": len(g),
             "titles": [products[a]["title"] for a in g][:4]}
            for g in biggest
        ],
    }
    s3.put_object(Bucket=bucket, Key=f"reports/{run_name}/clusters_summary.json",
                  Body=json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"))
    print(json.dumps(summary, ensure_ascii=False))
    return summary