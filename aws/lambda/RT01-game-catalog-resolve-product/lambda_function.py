import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

bedrock = boto3.client("bedrock-runtime", config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))
s3vectors = boto3.client("s3vectors")
dynamodb = boto3.resource("dynamodb")

TABLE_NAME = "game-catalog-products"
table = dynamodb.Table(TABLE_NAME)

EXTRACT_MODEL_ID = "us.amazon.nova-micro-v1:0"
DECISION_MODEL_ID = "us.amazon.nova-lite-v1:0"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
VECTOR_BUCKET = "game-catalog-vectors"
INDEX_NAME = "games-v2"
PROMPT_VERSION = "v8"                     

TOP_K = 20
CANDIDATE_MAX_DISTANCE = 0.25
GRAY_MAX_DISTANCE = 0.08
SEARCH_GRAY_MAX_DISTANCE = 0.20          
MATCH_FIELDS = ["platform", "edition", "format", "region"]
DEFAULT_REGION = "US"
DETAIL_KEYS = ["Manufacturer", "Type of item", "Rated", "Item model number"]

SYSTEM_PROMPT = """
You are a product data specialist. You extract normalized attributes from messy e-commerce video game listings. Return ONLY valid JSON matching the schema.

Purpose:
Your output is used to detect duplicate listings. Two listings of the same game must produce exactly the same base_title, written character by character the same way, even if their original titles look different. Different games must produce different base_titles. Consistency is more important than preserving the original wording.

Rules learned from the data:
- Titles may contain marketing filler, keyword stuffing or scraped page text. Ignore it.
- Tags in brackets carry attributes: [Download], [Digital Code], [Online Game Code] mean digital format; [Japan Import] means region JP; [2 Pack] means a bundle.
- platform: read it first from categories, where the category path names the console (["Video Games", "Legacy Systems", "Nintendo Systems", "Wii", "Games"] -> "Wii"). Use the most specific console in the path, or the platform in the title. Never return "unknown" if the categories or the title name a console. Write it with the matching name from the platform list in the schema ("Nintendo Wii" -> "Wii", "PS4" -> "PlayStation 4", "Nintendo 3DS & 2DS" -> "Nintendo 3DS"). If the listing is for several platforms at once, use "Multi-platform".
- Use format "digital" only when the listing says so (download, digital code, online code, instant access). On this marketplace digital products are always labeled, so any listing without that evidence is format "physical". The platform alone is never evidence of a digital product.
- "store" may be a reseller, not the publisher. Prefer details.Manufacturer or the title.
- Normalize company names (e.g. "SONY COMPUTER ENTERTAINME" -> "Sony").
- Keep the edition number or year in base_title ("NHL 22", not "NHL").
- Sequel numbers and subtitles are part of the game: "Gears of War 2" is a different game from "Gears of War". Never drop them from base_title.
- Canonicalize installment numbers in base_title: write any number that identifies a sequel or installment with Arabic digits, whether it appears as a Roman numeral, a digit or a word. Examples:
  "Metal Gear Solid V" -> "Metal Gear Solid 5"
  "Dragon Quest VIII" -> "Dragon Quest 8"
  "Kingdom Hearts III" -> "Kingdom Hearts 3"
  "Dead Rising Two" -> "Dead Rising 2"
  "Mega Man X" stays "Mega Man X" (X is part of the name, not a number)
  Never drop the number.
- Expand well-known abbreviations to the full game name: "MGS" -> "Metal Gear Solid", "KH" -> "Kingdom Hearts".
- Use the full official game title when you recognize it: "uncharted 2 ps3" -> "Uncharted 2: Among Thieves".
- If a console name is part of the official game title, keep it in base_title: "PlayStation All-Stars Battle Royale".
- Remasters and enhanced versions are different products: keep suffixes such as "3D", "HD" or "Remastered" in base_title ("Okami HD", not "Okami").
- Budget reprint labels (Platinum, Greatest Hits, Player's Choice, Nintendo Selects, Essentials) are the same product as the original release: edition is "Standard".
- Packs, battle packs, bundles and collections of several products are product_type "bundle", not "game".
- Cheat code discs, XP boost codes, toys, replicas and compilations of several games ("1 & 2", "Collection", "Trilogy") are not single games: use accessory, dlc, merchandise or bundle.
- If no special edition is mentioned (Deluxe, Gold, GOTY, Collector's...), edition is "Standard".
- Product condition (Renewed, Refurbished, Used, Pre-owned, Occasion) is not an edition: ignore it.
- Never guess: use "unknown" when an attribute is not stated or cannot be inferred.

Schema:
{
  "product_type": "game | dlc | bundle | accessory | console | merchandise | other",
  "base_title": "official game name with installment numbers as Arabic digits, without the platform it is sold for, edition, format or region",
  "platform": "exactly one of: PlayStation, PlayStation 2, PlayStation 3, PlayStation 4, PlayStation 5, PlayStation Portable, PlayStation Vita, Xbox, Xbox 360, Xbox One, Xbox Series X|S, NES, Super Nintendo, Nintendo 64, GameCube, Wii, Wii U, Nintendo Switch, Game Boy, Game Boy Color, Game Boy Advance, Nintendo DS, Nintendo 3DS, Sega Master System, Sega Genesis, Sega CD, Sega Saturn, Sega Dreamcast, Sega Game Gear, Neo Geo Pocket, Atari 2600, PC, Mac, Multi-platform, unknown",
  "edition": "Standard | Deluxe | Gold | GOTY | Collector's | Complete | other | unknown (budget reprints are Standard)",
  "format": "physical | digital (digital only with explicit evidence; otherwise physical)",
  "region": "US | EU | JP | UK | other | unknown",
  "publisher": "normalized publisher name or unknown",
  "confidence": "high | medium | low"
}

Examples:

Input: {"title": "Metal Gear Solid V: The Phantom Pain (Greatest Hits)", "store": "Konami", "categories": ["Video Games", "PlayStation 4", "Games"]}
Output: {"product_type": "game", "base_title": "Metal Gear Solid 5: The Phantom Pain", "platform": "PlayStation 4", "edition": "Standard", "format": "physical", "region": "unknown", "publisher": "Konami", "confidence": "high"}

Input: {"title": "Dragon Quest VIII [Japan Import]", "store": "Square Enix", "categories": ["Video Games", "Legacy Systems", "PlayStation Systems", "PlayStation 2", "Games"]}
Output: {"product_type": "game", "base_title": "Dragon Quest 8", "platform": "PlayStation 2", "edition": "Standard", "format": "physical", "region": "JP", "publisher": "Square Enix", "confidence": "high"}
"""

DECISION_PROMPT = """You compare two e-commerce video game listings and decide how they relate.
- duplicate: the same product (same game, platform, edition and format). Condition such as
  used, renewed or refurbished does NOT make it a different product.
- variant: the same base game, but a different platform, edition, format or region.
- different: different games.
If either listing is not a video game itself (a toy, replica, cheat code disc, XP or bonus code,
accessory or merchandise), the answer is "different".
Return ONLY valid JSON: {"relation": "duplicate | variant | different", "reason": "one short sentence"}"""


# ---------- helpers ----------

def parse_json_text(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def ask_model(model_id: str, system: str, user_payload: dict, max_tokens: int) -> dict:
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
        inferenceConfig={"temperature": 0, "maxTokens": max_tokens},
    )
    return parse_json_text(resp["output"]["message"]["content"][0]["text"])


def build_payload(product: dict) -> dict:
    """Same field selection as the batch pipeline."""
    details = product.get("details") or {}
    payload = {
        "title": product.get("title"),
        "store": product.get("store"),
        "categories": product.get("categories"),
        "details": {k: details[k] for k in DETAIL_KEYS if k in details},
    }
    features = product.get("features") or []
    if features:
        payload["features"] = features[:3]
    return payload


def identity_text(attrs: dict) -> str:
    parts = [attrs.get("base_title"), attrs.get("platform"), attrs.get("edition"), attrs.get("format")]
    return " | ".join(p for p in parts if p and p != "unknown")


def embed(text: str) -> list:
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
    )
    return json.loads(resp["body"].read())["embedding"]


def search_candidates(vector: list, exclude_key: str = None) -> list:
    resp = s3vectors.query_vectors(
        vectorBucketName=VECTOR_BUCKET, indexName=INDEX_NAME,
        queryVector={"float32": vector}, topK=TOP_K,
        returnMetadata=True, returnDistance=True,
    )
    return [v for v in resp["vectors"]
            if v["distance"] < CANDIDATE_MAX_DISTANCE and v["key"] != exclude_key]


def load_catalog_items(keys: list) -> dict:
    if not keys:
        return {}
    resp = dynamodb.batch_get_item(
        RequestItems={TABLE_NAME: {"Keys": [{"parent_asin": k} for k in keys]}}
    )
    return {item["parent_asin"]: item for item in resp["Responses"].get(TABLE_NAME, [])}


def normalize_title(title: str) -> str:
    title = re.sub(r"['’]", "", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def compatible(field: str, a: str, b: str) -> bool:
    a, b = (a or "unknown").lower(), (b or "unknown").lower()
    if field == "region":                       
        a = DEFAULT_REGION.lower() if a == "unknown" else a
        b = DEFAULT_REGION.lower() if b == "unknown" else b
    return a == b or "unknown" in (a, b)


def decide(new: dict, cand: dict, distance: float, gray_max: float) -> dict:     
    if normalize_title(new.get("base_title")) == normalize_title(cand.get("base_title")):
        diffs = [f for f in MATCH_FIELDS if not compatible(f, new.get(f), cand.get(f))]
        if not diffs:
            return {"relation": "duplicate", "method": "rule", "reason": "same base_title and compatible attributes"}
        return {"relation": "variant", "method": "rule", "reason": "differs in " + ", ".join(diffs)}
    if distance >= gray_max:                                                    
        return {"relation": "different", "method": "rule", "reason": "different base_title"}
    fields = ["title", "base_title", "platform", "edition", "format", "region"]
    out = ask_model(DECISION_MODEL_ID, DECISION_PROMPT,
                    {"listing_a": {f: new.get(f) for f in fields},
                     "listing_b": {f: cand.get(f) for f in fields}}, 150)
    return {"relation": out.get("relation", "different"), "method": "bedrock", "reason": out.get("reason", "")}


def to_dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v not in (None, "")}
    return value


def respond(body: dict, from_api: bool):
    if from_api:
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(body, ensure_ascii=False, default=str)}
    return json.loads(json.dumps(body, default=str))


# ---------- entry point ----------

def lambda_handler(event, context):
    from_api = "body" in event
    if from_api:
        event = json.loads(event.get("body") or "{}")

    product = event.get("product")
    mode = "register" if product else "search"
    title = product.get("title") if product else event["query"]
    payload = build_payload(product) if product else {"title": title}

    # 1-2. Extract attributes with the same prompt as the batch pipeline
    attrs = ask_model(EXTRACT_MODEL_ID, SYSTEM_PROMPT, payload, 300)
    attrs["title"] = title
    attrs["product_type"] = str(attrs.get("product_type", "other")).strip().lower()   
    if mode == "register" and attrs.get("product_type") != "game":
        return respond({"mode": mode, "decision": "out_of_scope", "attributes": attrs,
                        "explanation": f"product_type is '{attrs.get('product_type')}', only games are deduplicated"}, from_api)

    gray_max = SEARCH_GRAY_MAX_DISTANCE if mode == "search" else GRAY_MAX_DISTANCE    

    # 3-5. Embed, retrieve neighbours, enrich with catalog IDs
    text = identity_text(attrs)
    vector = embed(text)
    neighbors = search_candidates(vector, exclude_key=product.get("parent_asin") if product else None)
    catalog = load_catalog_items([n["key"] for n in neighbors])

    # 6. Decide the relation with each candidate (closest first)
    candidates = []
    for n in sorted(neighbors, key=lambda v: v["distance"]):
        cand = {**n["metadata"], **catalog.get(n["key"], {}), "parent_asin": n["key"]}
        candidates.append({**decide(attrs, cand, n["distance"], gray_max),       
                           "parent_asin": n["key"], "distance": round(n["distance"], 4),
                           "title": cand.get("title"),
                           "canonical_id": cand.get("canonical_id"), "family_id": cand.get("family_id")})

    duplicate = next((c for c in candidates if c["relation"] == "duplicate"), None)
    variant = next((c for c in candidates if c["relation"] == "variant"), None)
    duplicate_groups = sorted({c["canonical_id"] for c in candidates                   
                               if c["relation"] == "duplicate" and c["canonical_id"]})
    asin = product.get("parent_asin") if product else None

    # 7. Resolve IDs
    if duplicate:
        decision, canonical_id, family_id, matched = "duplicate", duplicate["canonical_id"], duplicate["family_id"], duplicate
    elif variant:
        decision, canonical_id, family_id, matched = "variant", (f"CAN-{asin}" if asin else None), variant["family_id"], variant
    else:
        decision, canonical_id, family_id, matched = "new", (f"CAN-{asin}" if asin else None), (f"FAM-{asin}" if asin else None), None

    # 8. Register: save to DynamoDB and to the vector index
    if mode == "register":
        item = {**product, **{k: attrs.get(k) for k in
                              ["product_type", "base_title", "platform", "edition", "format", "region", "publisher", "confidence"]},
                "canonical_id": canonical_id, "family_id": family_id,
                "prompt_version": PROMPT_VERSION, "model_id": EXTRACT_MODEL_ID,
                "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        item.pop("details", None)
        item.pop("features", None)
        table.put_item(Item=to_dynamo(item))
        s3vectors.put_vectors(
            vectorBucketName=VECTOR_BUCKET, indexName=INDEX_NAME,
            vectors=[{"key": asin, "data": {"float32": vector},
                      "metadata": {"platform": attrs.get("platform"), "edition": attrs.get("edition"),
                                   "format": attrs.get("format"), "region": attrs.get("region"),
                                   "base_title": attrs.get("base_title"), "title": (title or "")[:500]}}],
        )

    return respond({
        "mode": mode,
        "decision": decision,
        "canonical_id": canonical_id,
        "family_id": family_id,
        "ambiguous": len(duplicate_groups) > 1,                                   
        "matching_canonical_ids": duplicate_groups,                               
        "matched_with": matched and {k: matched[k] for k in ("parent_asin", "title", "distance", "method", "reason")},
        "attributes": attrs,
        "identity_text": text,
        "candidates": candidates[:5],
    }, from_api)