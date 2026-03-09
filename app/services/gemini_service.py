from google import genai
from google.genai import types
from fastapi import HTTPException
from app.config import settings
from typing import Optional, List, Dict
import json
import logging
import asyncio
import re

logger = logging.getLogger(__name__)

_DAILY_QUOTA_IDS = {
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
}

def _parse_retry_delay(error_str: str) -> Optional[float]:
    """Extract retryDelay seconds from a Gemini 429 error string."""
    match = re.search(r"'retryDelay':\s*'(\d+)s'", error_str)
    if match:
        return float(match.group(1))
    match = re.search(r"retry in ([\d.]+)s", error_str)
    if match:
        return float(match.group(1))
    return None

def _is_daily_quota_exhausted(error_str: str) -> bool:
    return any(qid in error_str for qid in _DAILY_QUOTA_IDS)


def _fix_item_unit_prices(data: dict) -> None:
    """
    Post-processing: si el modelo devolvió precio_unitario = total_linea en vez de
    precio_unitario = total / cantidad, lo corrige automáticamente.

    Detecta el error cuando: precio_unitario ≈ total Y cantidad > 1
    En ese caso recalcula: precio_unitario = total / cantidad
    """
    for item in data.get("items", []):
        try:
            qty = float(item.get("cantidad") or 1)
            pu = float(item.get("precio_unitario") or 0)
            total = float(item.get("total") or 0)

            if qty <= 1 or total == 0 or pu == 0:
                continue

            # Si precio_unitario ≈ total (dentro del 1%), el modelo cometió el error clásico
            if abs(pu - total) / total < 0.01:
                corrected = round(total / qty, 2)
                logger.info(
                    f"OCR fix: '{item.get('descripcion')}' "
                    f"precio_unitario {pu} → {corrected} (total={total}, qty={qty})"
                )
                item["precio_unitario"] = corrected
        except (TypeError, ValueError, ZeroDivisionError):
            continue


async def process_invoice(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    catalog: Optional[List[Dict]] = None
) -> dict:
    """
    Process an invoice image using Google Gemini 2.5 Flash Lite.
    Returns structured JSON data. Retries once on per-minute 429 quota errors.

    If `catalog` is provided (list of {id, name, unit} dicts), Gemini will also
    attempt to match each item against the catalog and flag new ingredients.
    """
    if not settings.google_api_key:
        logger.error("Google API Key not found")
        raise HTTPException(status_code=500, detail="Google API Key not configured")

    # Build catalog context block for the prompt
    catalog_block = ""
    if catalog:
        lines = [f"{i['id']}|{i['name']}|{i['unit']}" for i in catalog]
        catalog_block = (
            "\n\nCATÁLOGO DE INGREDIENTES DEL SISTEMA (id|nombre|unidad_base):\n"
            + "\n".join(lines)
            + "\n\nUsa este catálogo para rellenar el campo 'ingredient_match' de cada item."
        )

    ingredient_match_schema = """
                    "detected_ingredient": "Nombre normalizado del ingrediente (corregido y sin marcas)",
                    "ingredient_match": {
                        "id": "UUID del catálogo si hay coincidencia, sino null",
                        "confidence": 0.0,
                        "should_create": false,
                        "suggested_name": null,
                        "suggested_unit": null
                    }"""

    ingredient_match_rules = """
        REGLAS PARA ingredient_match (aplica SOLO si se proporcionó catálogo):

        9. "detected_ingredient": nombre del producto limpio, sin cantidad ni marca comercial.
           Ej: "JAMON SDW ZENU X 450 G" → "Jamón" o "Jamón de sándwich".

        10. "ingredient_match.id": UUID del ingrediente del catálogo que mejor coincide con el
            producto de la factura. Usa coincidencia semántica, no exacta.
            Ej: "Aceite Gira 1L" → coincide con "Aceite de girasol" (id del catálogo).
            Si no hay ninguna coincidencia razonable → null.

        11. "ingredient_match.confidence": número entre 0 y 1 que indica tu certeza de la
            coincidencia. 1.0 = exacto, 0.5 = probable, 0.0 = sin coincidencia.

        12. "ingredient_match.should_create": true SOLO cuando:
            - confidence < 0.4 (no encontraste coincidencia suficiente), Y
            - El producto es claramente un ingrediente de cocina real (no un servicio, envase,
              embalaje, etc.)
            En cualquier otro caso debe ser false.

        13. "ingredient_match.suggested_name": si should_create=true, escribe el nombre
            normalizado del nuevo ingrediente en español, sin marca, sin cantidad.
            Ej: "Leche entera". Si should_create=false → null.

        14. "ingredient_match.suggested_unit": si should_create=true, elige la unidad base
            más apropiada. IMPORTANTE: usa EXACTAMENTE estos valores:
            - Sólidos pesables (carnes, quesos, verduras, harinas, etc.) → "gr"
            - Líquidos (aceites, salsas, lácteos, bebidas, etc.) → "ml"
            - Unidades contables (huevos, panes, porciones individuales, etc.) → "und"
            Si should_create=false → null.
    """

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
      try:
        client = genai.Client(api_key=settings.google_api_key)

        prompt = f"""
        Contexto: Eres un asistente contable experto en restaurantes colombianos.
        Tarea: Analiza la imagen adjunta (factura de proveedor).
        Salida: Genera UNICAMENTE un objeto JSON válido con la siguiente estructura.

        Esquema JSON:
        {{
            "proveedor": "Nombre del proveedor o Razón Social",
            "nit": "Número de identificación tributaria si es visible",
            "fecha": "YYYY-MM-DD",
            "numero_factura": "Número de la factura",
            "total_factura": 0,
            "items": [
                {{
                    "descripcion": "Nombre del producto tal como aparece en la factura",
                    "cantidad": 1.0,
                    "precio_unitario": 0,
                    "total": 0,
                    "peso_unidad_gr": null,
                    {ingredient_match_schema}
                }}
            ],
            "subtotal": 0,
            "iva": 0,
            "forma_pago": "Efectivo/Credito/etc",
            "observaciones": "Observaciones si las hay",
            "advertencia": "Si la imagen es borrosa o ilegible, escribe 'ILEGIBLE', sino deja null"
        }}
        {catalog_block}

        REGLAS CRÍTICAS PARA LOS ITEMS:

        1. "precio_unitario" = precio de UNA SOLA unidad del producto (NO el subtotal de la línea).
           - Fórmula correcta: precio_unitario = total_linea / cantidad
           - EJEMPLO CORRECTO: cantidad=4, total=68000 → precio_unitario=17000 (NO 68000)
           - EJEMPLO INCORRECTO: cantidad=4, total=68000 → precio_unitario=68000 ← ESTO ESTÁ MAL

        2. Siempre verifica: precio_unitario × cantidad debe ser aproximadamente igual a total.
           Si en la factura solo aparece el total de la línea (sin precio unitario explícito),
           calcula precio_unitario = total / cantidad.

        3. "peso_unidad_gr" = peso o volumen de UNA unidad del producto, expresado en gramos (o ml
           para líquidos, ya que 1ml ≈ 1gr a efectos de inventario). Extráelo del texto de la
           descripción cuando aparezcan patrones como "X 450 G", "X 1 KG", "500 ML", "1 LT",
           "2.5 KG", "KILO", "LIBRA", etc. Conversiones:
           - G / GR / GRS → valor directo en gramos (ej: "450 G" → 450)
           - KG / KILO / KILOGRAMO → × 1000 (ej: "1 KG" → 1000, "2.5 KG" → 2500)
           - ML / CC → valor directo (ej: "500 ML" → 500)
           - LT / LITRO / L → × 1000 (ej: "1 LT" → 1000)
           - LIBRA / LB → × 500 (libra colombiana = 500 gr; ej: "1 LIBRA" → 500)
           - Si NO hay indicación de peso en la descripción → null
           - EJEMPLOS:
             "JAMON SDW ZENU X 450 G"     → peso_unidad_gr: 450
             "PAPA RIPIO NACIONAL KILO"   → peso_unidad_gr: 1000
             "LECHE ENTERA X 1.1 LT"      → peso_unidad_gr: 1100
             "ACEITE X 3 LT"              → peso_unidad_gr: 3000
             "QUESO TAJADO"               → peso_unidad_gr: null
             "HUEVOS AA X 30 UND"         → peso_unidad_gr: null

        4. Si la descripción contiene el peso del producto, inclúyelo tal cual en "descripcion",
           NO lo uses como cantidad.

        5. Si ves "IVA" o "Impoconsumo", ignóralos en el precio_unitario, extrae el valor neto.

        6. Corrige errores tipográficos obvios de OCR (ej. "T0mate" → "Tomate").

        7. Las facturas colombianas usan puntos para miles (10.000) y comas para decimales (1,50)
           O VICEVERSA. Usa el contexto (valores de facturas típicas) para decidir.

        8. Todos los valores numéricos sin símbolos de moneda ($, COP, etc.).
        {ingredient_match_rules if catalog else ""}
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        try:
            data = json.loads(response.text)
            _fix_item_unit_prices(data)
            return data
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Gemini response: {response.text}")
            raise HTTPException(status_code=502, detail="Invalid JSON response from AI model")

      except HTTPException:
          raise
      except Exception as e:
          error_str = str(e)
          if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
              logger.error(f"Gemini API error: {error_str}")
              if _is_daily_quota_exhausted(error_str):
                  raise HTTPException(
                      status_code=503,
                      detail="AI service daily quota exhausted. Try again tomorrow or contact support.",
                      headers={"Retry-After": "86400"},
                  )
              retry_delay = _parse_retry_delay(error_str)
              if attempt < max_attempts and retry_delay is not None:
                  logger.warning(f"Gemini 429 (per-minute), retrying in {retry_delay}s (attempt {attempt}/{max_attempts})")
                  await asyncio.sleep(retry_delay + 1)
                  continue
              retry_after = str(int(retry_delay)) if retry_delay else "60"
              raise HTTPException(
                  status_code=503,
                  detail="AI service temporarily unavailable due to rate limiting. Please try again shortly.",
                  headers={"Retry-After": retry_after},
              )
          logger.error(f"Gemini API error: {error_str}")
          raise HTTPException(status_code=500, detail=f"AI Processing Error: {error_str}")
