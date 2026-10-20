import json
import boto3
import re

s3 = boto3.client("s3")

DETAIL_KEYS = ["Manufacturer", "Type of item", "Rated", "Item model number"]
DESCRIPTION_FALLBACK_CHARS = 300
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


def build_payload(product: dict) -> dict:
    """Keep only the fields that help identify the product (fewer tokens, fewer misleading fields)."""
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
    else:
        description = " ".join(product.get("description") or [])
        if description:
            payload["description"] = description[:DESCRIPTION_FALLBACK_CHARS]
    return payload


def build_record(product: dict) -> dict:
    """One line of the Bedrock batch input file (Amazon Nova request format)."""
    return {
        "recordId": product["parent_asin"],
        "modelInput": {
            "schemaVersion": "messages-v1",
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": [{
                "role": "user",
                "content": [{"text": json.dumps(build_payload(product), ensure_ascii=False)}],
            }],
            "inferenceConfig": {"maxTokens": 300, "temperature": 0},
        },
    }


def lambda_handler(event, context):
    bucket = event["bucket"]
    source_key = event.get("key", "mvp/catalog_mvp_games.jsonl")
    run_name = event["run_name"]
    limit = event.get("limit")
    step = event.get("step", 1)
    title_patterns = [re.compile(p) for p in event.get("title_contains", [])]

    body = s3.get_object(Bucket=bucket, Key=source_key)["Body"]
    out_path = "/tmp/requests.jsonl"
    written = 0

    with open(out_path, "w", encoding="utf-8") as out:
        for i, line in enumerate(body.iter_lines()):
            if not line.strip() or i % step != 0:
                continue
            product = json.loads(line)

            title = (product.get("title") or "").lower()
            if title_patterns and not any(p.search(title) for p in title_patterns):
                continue

            out.write(json.dumps(build_record(product), ensure_ascii=False) + "\n")
            written += 1
            if limit and written >= limit:
                break

    output_key = f"bedrock-input/{run_name}/requests.jsonl"
    s3.upload_file(out_path, bucket, output_key)

    result = {"input_s3_uri": f"s3://{bucket}/{output_key}", "records": written}
    print(json.dumps(result))
    return result