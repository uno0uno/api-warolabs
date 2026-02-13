import google.generativeai as genai
from fastapi import HTTPException
from app.config import settings
import json
import logging

logger = logging.getLogger(__name__)

# Configure the library
if settings.google_api_key:
    genai.configure(api_key=settings.google_api_key)

INVOICE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "proveedor": {"type": "string"},
        "nit": {"type": "string"},
        "fecha": {"type": "string", "description": "YYYY-MM-DD"},
        "total_factura": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descripcion": {"type": "string"},
                    "cantidad": {"type": "number"},
                    "precio_unitario": {"type": "number"},
                    "total": {"type": "number"}
                },
                "required": ["descripcion", "cantidad", "precio_unitario", "total"]
            }
        },
        "advertencia": {"type": "string", "nullable": True}
    },
    "required": ["proveedor", "fecha", "total_factura", "items"]
}

async def process_invoice(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """
    Process an invoice image using Google Gemini 1.5 Flash.
    Returns structured JSON data.
    """
    if not settings.google_api_key:
        logger.error("Google API Key not found")
        raise HTTPException(status_code=500, detail="Google API Key not configured")

    try:
        # Initialize model
        model = genai.GenerativeModel('gemini-flash-latest')

        prompt = """
        Contexto: Eres un asistente contable experto en restaurantes colombianos.
        Tarea: Analiza la imagen adjunta (factura de proveedor).
        Salida: Genera UNICAMENTE un objeto JSON válido con la siguiente estructura.
        
        Esquema JSON:
        {
            "proveedor": "Nombre del proveedor o Razón Social",
            "nit": "Número de identificación tributaria si es visible",
            "fecha": "DD/MM/AAAA",
            "numero_factura": "Número de la factura",
            "total_factura": Numero (sin simbolos de moneda),
            "items": [
                {
                    "descripcion": "Nombre del producto",
                    "cantidad": 1.0,
                    "precio_unitario": 0,
                    "total": 0
                }
            ],
            "subtotal": 0,
            "iva": 0,
            "forma_pago": "Efectivo/Credito/etc",
            "observaciones": "Observaciones si las hay",
            "advertencia": "Si la imagen es borrosa o ilegible, escribe 'ILEGIBLE', sino deja null"
        }

        Instrucciones Adicionales:
        - Si ves "IVA" o "Impoconsumo", ignóralos en el precio unitario, extrae el valor neto.
        - Corrige errores tipográficos obvios de OCR (ej. "T0mate" -> "Tomate").
        - Las facturas colombianas usan puntos para miles (10.000) y comas para decimales (1,50) O VICEVERSA. Usa el contexto para decidir.
        - Fecha formato YYYY-MM-DD.
        """

        # Prepare content parts
        cookie_picture = {
            'mime_type': mime_type,
            'data': image_bytes
        }

        # Generate content
        response = model.generate_content(
            [prompt, cookie_picture],
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json"
            )
        )

        # Parse response
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Gemini response: {response.text}")
            raise HTTPException(status_code=502, detail="Invalid JSON response from AI model")

    except Exception as e:
        logger.error(f"Gemini API error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI Processing Error: {str(e)}")
