"""
Retail catalog scraper — Éxito (VTEX API)
==========================================
Fetches food ingredients from exito.com public VTEX API and generates
a SQL seed file to enrich the global ingredients catalog.

Usage:
    python scripts/scrape_exito_catalog.py

Output:
    scripts/output/seed_retail_ingredients_YYYYMMDD.sql
    scripts/output/scrape_report_YYYYMMDD.json

Requires: requests (pip install requests)
"""

import json
import time
import re
import logging
from datetime import date
from pathlib import Path
from typing import Optional
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
BASE_URL = "https://www.exito.com/api/catalog_system/pub/products/search"
PAGE_SIZE = 50          # max per request
SLEEP_BETWEEN = 1.2     # seconds between requests (respectful crawling)
SIMILARITY_AUTO_SKIP = 0.75   # if an existing ingredient is this close, skip
OUTPUT_DIR = Path(__file__).parent / "output"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WaRo-CatalogBot/1.0; internal-tool)",
    "Accept": "application/json",
}

# ──────────────────────────────────────────────
# Search terms — Colombian restaurant staples
# ──────────────────────────────────────────────
SEARCH_QUERIES = [
    # Vegetables
    "espinaca baby",
    "espinaca fresca",
    "acelga",
    "lechuga batavia",
    "lechuga romana",
    "lechuga crespa",
    "repollo morado",
    "repollo blanco",
    "brocoli",
    "coliflor",
    "apio",
    "pepino cohombro",
    "berenjena",
    "remolacha",
    "rabano",
    "alcachofa",
    "esparragos",
    "puerro",
    "nabo",
    # Fruits
    "brevas arequipe",
    "mango tommy",
    "mango tommy fresco",
    "papaya maradol",
    "guanabana",
    "lulo",
    "feijoa",
    "mora castilla",
    "fresa fresca",
    "pitahaya",
    "maracuya",
    # Grains & dry goods
    "maiz desgranado",
    "maiz pira",
    "garbanzo",
    "lenteja",
    "frijol cargamanto",
    "frijol bolo",
    "arveja verde",
    "haba",
    "quinoa",
    "avena en hojuelas",
    # Proteins
    "pechuga pollo fresca",
    "muslo pollo",
    "lomo cerdo",
    "costilla cerdo",
    "salmon fresco",
    "tilapia fresca",
    "camarones",
    "atun lata",
    # Dairy
    "queso campesino",
    "queso doble crema",
    "queso costeño",
    "crema de leche",
    "leche condensada",
    "mantequilla con sal",
    "yogur natural",
    # Coffee & beverages
    "cafe tostao",
    "cafe oma molido",
    "cafe juan valdez",
    "cafe sello rojo molido",
    "panela redonda",
    "panela pulverizada",
    # Condiments & sauces
    "vinagre blanco",
    "vinagre balsámico",
    "aceite de oliva",
    "aceite de girasol",
    "pasta de tomate",
    "salsa inglesa",
    "salsa de soya",
    "mostaza",
    "mayonesa",
    # Flours & starches
    "harina de trigo",
    "harina de maiz",
    "almidon de yuca",
    "fecula de maiz maizena",
    "harina para todo uso",
    # Packaged / preserves
    "champiñones lata",
    "aceitunas negras",
    "alcaparras",
    "pepinillos encurtidos",
    "corazones de alcachofa",
]

# ──────────────────────────────────────────────
# Unit mapping helpers
# ──────────────────────────────────────────────

def infer_base_unit(product_name: str, measurement_unit: str, unit_multiplier: float,
                    unit_weight: Optional[float] = None) -> str:
    """Maps VTEX measurementUnit + product name to our DB unit enum (gr, ml, und, kg, lt)."""
    name_lower = product_name.lower()
    mu = (measurement_unit or "").lower()

    # VTEX uses "kg" sometimes for produce sold by weight
    if mu == "kg":
        return "gr"   # we store base in gr

    # Liquid products
    liquid_keywords = ["jugo", "leche", "aceite", "vinagre", "crema", "yogur",
                       "agua", "salsa", "bebida", "refresco", "vino", "cerveza",
                       "ron", "aguardiente", "whisky", "café líquido", "ml", " l ",
                       "litro", "galon", "galón"]
    if any(k in name_lower for k in liquid_keywords):
        return "ml"

    # Weight-sold produce / bulk ingredients: if name has weight suffix → gr
    weight_keywords = [" gr", "gr)", "gramos", " kg", "kg)", " kilo", "libra",
                       "bulto", " g "]
    if any(k in name_lower for k in weight_keywords):
        return "gr"

    # Fresh produce, grains, spices typically sold by weight in restaurant context
    produce_keywords = ["espinaca", "lechuga", "acelga", "repollo", "brocoli",
                        "coliflor", "zanahoria", "tomate", "cebolla", "papa",
                        "yuca", "platano", "plátano", "mango", "mora", "fresa",
                        "maiz", "maíz", "garbanzo", "lenteja", "frijol", "arveja",
                        "quinoa", "avena", "almendra", "maní", "mani",
                        "salmon", "tilapia", "trucha", "atun", "atún",
                        "pechuga", "muslo", "lomo", "costilla", "cerdo", "res"]
    if any(k in name_lower for k in produce_keywords):
        return "gr"

    # If we parsed a weight from name → it's weight-measured
    if unit_weight and unit_weight > 0:
        # ml-range weights are probably liquid volumes
        if unit_weight > 5000:
            return "ml"
        return "gr"

    # Default: sold by unit
    return "und"


def parse_weight_from_name(name: str) -> Optional[float]:
    """
    Extracts weight in grams from product name strings like:
    'Espinaca Baby 150 gr', 'Café TOSTAO tostado gourmet (340 gr)', 'Leche 1L'
    Returns grams as float, or None if not parseable.
    """
    name_lower = name.lower()

    # Patterns: "340 gr", "340gr", "(340 gr)", "340 g)"
    gr_match = re.search(r'(\d+(?:[.,]\d+)?)\s*gr?\b', name_lower)
    if gr_match:
        return float(gr_match.group(1).replace(",", "."))

    # kg → convert to gr
    kg_match = re.search(r'(\d+(?:[.,]\d+)?)\s*kg\b', name_lower)
    if kg_match:
        return float(kg_match.group(1).replace(",", ".")) * 1000

    # ml
    ml_match = re.search(r'(\d+(?:[.,]\d+)?)\s*ml\b', name_lower)
    if ml_match:
        return float(ml_match.group(1).replace(",", "."))

    # L / lt
    lt_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:l|lt|litro)\b', name_lower)
    if lt_match:
        return float(lt_match.group(1).replace(",", ".")) * 1000

    return None


def build_purchase_units(base_unit: str, unit_weight: Optional[float], product_name: str) -> list:
    """Generate standard purchase units for the ingredient."""
    if base_unit == "gr":
        units = [
            {"purchase_unit": "kg", "label": "1 Kilogramo", "factor": 1000.0, "default": True},
            {"purchase_unit": "lb", "label": "1 Libra", "factor": 454.0, "default": False},
        ]
        if unit_weight and 50 < unit_weight < 30000:
            units.append({
                "purchase_unit": "und",
                "label": f"1 Unidad ({int(unit_weight)}g)",
                "factor": float(unit_weight),
                "default": False,
            })
        return units

    if base_unit == "ml":
        vol = unit_weight or 1000.0
        label = f"Botella {int(vol)}ml" if vol < 1000 else f"Botella {vol/1000:.4g}L"
        return [
            {"purchase_unit": "botella", "label": label, "factor": vol, "default": True},
            {"purchase_unit": "galon", "label": "Galón 3785ml", "factor": 3785.0, "default": False},
        ]

    # und
    if unit_weight and 50 < unit_weight < 30000:
        return [
            {"purchase_unit": "und",
             "label": f"1 Unidad ({int(unit_weight)}g)",
             "factor": float(unit_weight),
             "default": True},
        ]
    return []


def clean_name(raw: str) -> str:
    """Normalize product name: title-case, collapse spaces, strip parentheses with weight."""
    # Remove weight suffixes like "(340 gr)", "150 gr", "1 kg" at end
    cleaned = re.sub(r'\s*\(?\d+(?:[.,]\d+)?\s*(?:gr?|kg|ml|l|lt)\)?\s*$', '', raw, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.title()


def infer_category(product_name: str, vtex_categories) -> Optional[str]:
    """Map VTEX category path to our DB category values."""
    if isinstance(vtex_categories, dict):
        path = " ".join(vtex_categories.values()).lower()
    elif isinstance(vtex_categories, list):
        path = " ".join(vtex_categories).lower()
    else:
        path = ""
    name = product_name.lower()

    if any(k in path for k in ["frutas", "verdura", "vegetal", "hortalizas"]):
        return "Vegetales"
    if "café" in path or "cafe" in path or "café" in name or "cafe" in name:
        return "Cafe"
    if any(k in path for k in ["lacteo", "lácteo", "queso", "leche"]):
        return "Lácteos"
    if any(k in path for k in ["grano", "cereal", "legumbre", "arroz"]):
        return "Granos"
    if any(k in path for k in ["carne", "res", "cerdo", "pollo", "pescado", "proteína"]):
        return "Proteínas"
    if any(k in path for k in ["aceite", "salsa", "condimento", "especia"]):
        return "Condimentos"
    if any(k in path for k in ["harina", "almidón", "almidon", "masa"]):
        return "Harinas"
    if any(k in path for k in ["dulce", "confite", "postre", "golosina"]):
        return "Dulces"
    if any(k in path for k in ["bebida", "jugo", "agua", "refresco"]):
        return "Bebidas"
    return None


# ──────────────────────────────────────────────
# API fetching
# ──────────────────────────────────────────────

def fetch_products(query: str, from_idx: int = 0) -> list:
    """Fetch one page of products from VTEX API."""
    params = {"_from": from_idx, "_to": from_idx + PAGE_SIZE - 1}
    url = f"{BASE_URL}/{requests.utils.quote(query)}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch '{query}' from={from_idx}: {e}")
        return []


NON_FOOD_KEYWORDS = [
    "electrodoméstico", "electrodomestico", "utensilio", "vajilla", "menaje",
    "textil", "ropa", "juguete", "calzado", "zapato", "sandalia", "zueco",
    "baleta", "bota", "tenis", "jean", "pantalon", "pantalón", "camisa",
    "blusa", "vestido", "chaqueta", "charol", "dama", "caballero", "niño",
    "mueble", "silla", "sofá", "sofa", "cama", "colchon", "colchón",
    "decoracion", "decoración", "herramienta", "ferreteria", "ferretería",
    "escobilla", "brocha", "pintura pincel", "tecnología", "tecnologia",
    "celular", "tablet", "computador", "televisor", "audifonos",
    "perfume", "fragancia", "cosmético", "cosmetico", "maquillaje",
]

def is_food_product(product: dict) -> bool:
    """Filter out non-food items."""
    cats = " ".join(product.get("categories", [])).lower()
    name = product.get("productName", "").lower()
    combined = cats + " " + name
    return not any(k in combined for k in NON_FOOD_KEYWORDS)


# ──────────────────────────────────────────────
# SQL generation
# ──────────────────────────────────────────────

def esc(s: str) -> str:
    """Escape single quotes for SQL."""
    return s.replace("'", "''")


def generate_sql(ingredients: list) -> str:
    lines = [
        "-- ============================================================",
        f"-- Retail catalog seed — Éxito VTEX scrape {date.today()}",
        "-- Generated by scripts/scrape_exito_catalog.py",
        "-- Run AFTER enabling pg_trgm (CREATE EXTENSION IF NOT EXISTS pg_trgm)",
        "-- ============================================================",
        "",
        "BEGIN;",
        "",
        "-- Ingredients",
        "INSERT INTO ingredients (name, unit, category, description, minimum_order_quantity)",
        "VALUES",
    ]

    values = []
    for ing in ingredients:
        name = esc(ing["name"])
        unit = ing["unit"]
        cat = esc(ing.get("category") or "")
        desc = esc(ing.get("description") or "")
        values.append(f"  ('{name}', '{unit}', NULLIF('{cat}',''), NULLIF('{desc}',''), 1)")

    lines.append(",\n".join(values))
    lines.append("ON CONFLICT (name) DO NOTHING;")
    lines.append("")

    # Purchase units
    lines.append("-- Purchase units")
    for ing in ingredients:
        name = esc(ing["name"])
        for pu in ing.get("purchase_units", []):
            pu_unit = esc(pu["purchase_unit"])
            pu_label = esc(pu["label"])
            factor = pu["factor"]
            default = "TRUE" if pu["default"] else "FALSE"
            lines.append(
                f"INSERT INTO ingredient_purchase_units "
                f"(ingredient_id, purchase_unit, purchase_unit_label, conversion_factor, is_default, is_active) "
                f"SELECT id, '{pu_unit}', '{pu_label}', {factor}, {default}, TRUE "
                f"FROM ingredients WHERE name = '{name}' "
                f"ON CONFLICT DO NOTHING;"
            )

    lines.append("")
    lines.append("COMMIT;")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")

    seen_names: set[str] = set()
    results: list[dict] = []
    skipped_count = 0

    for query in SEARCH_QUERIES:
        logger.info(f"Searching: '{query}'")
        products = fetch_products(query)
        time.sleep(SLEEP_BETWEEN)

        for product in products:
            if not is_food_product(product):
                continue

            raw_name = product.get("productName", "")
            if not raw_name:
                continue

            name = clean_name(raw_name)
            if name in seen_names:
                continue
            seen_names.add(name)

            items = product.get("items", [{}])
            item = items[0] if items else {}
            measurement_unit = item.get("measurementUnit", "un")
            unit_multiplier = float(item.get("unitMultiplier") or 1)

            unit_weight = parse_weight_from_name(raw_name)
            # For VTEX kg-sold products, unitMultiplier is the weight in kg
            if measurement_unit == "kg" and unit_weight is None:
                unit_weight = unit_multiplier * 1000

            base_unit = infer_base_unit(name, measurement_unit, unit_multiplier, unit_weight)
            category = infer_category(
                name,
                product.get("categories") or product.get("productCategoriesIds") or {}
            )
            purchase_units = build_purchase_units(base_unit, unit_weight, name)

            # Brand as description hint
            brand = product.get("brand", "")

            entry = {
                "name": name,
                "unit": base_unit,
                "category": category,
                "description": brand if brand and brand.upper() != "GENERICO" else None,
                "unit_weight_gr": unit_weight,
                "purchase_units": purchase_units,
                "source_raw_name": raw_name,
            }
            results.append(entry)

        if not products:
            skipped_count += 1

    logger.info(f"Scraped {len(results)} unique ingredients ({skipped_count} queries returned nothing)")

    # Write SQL
    sql_path = OUTPUT_DIR / f"seed_retail_ingredients_{today}.sql"
    sql_path.write_text(generate_sql(results), encoding="utf-8")
    logger.info(f"SQL written to {sql_path}")

    # Write JSON report
    report_path = OUTPUT_DIR / f"scrape_report_{today}.json"
    report = {
        "date": today,
        "source": "exito.com VTEX API",
        "total_ingredients": len(results),
        "queries_run": len(SEARCH_QUERIES),
        "queries_empty": skipped_count,
        "ingredients": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Report written to {report_path}")

    # Print summary table
    print(f"\n{'Name':<45} {'Unit':<6} {'Category':<20} {'Weight'}")
    print("-" * 90)
    for r in sorted(results, key=lambda x: x["name"]):
        w = f"{int(r['unit_weight_gr'])}g" if r.get("unit_weight_gr") else "-"
        print(f"{r['name']:<45} {r['unit']:<6} {str(r.get('category') or ''):<20} {w}")


if __name__ == "__main__":
    main()
