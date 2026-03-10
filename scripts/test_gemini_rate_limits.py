"""
Temporary script — Issue #39
Tests rate limits of candidate Gemini models to decide which one to use in production.

Usage:
    python scripts/test_gemini_rate_limits.py

Reads GOOGLE_API_KEY from .env automatically.
"""
import asyncio
import time
import os
import sys
from typing import List, Dict, Optional
from pathlib import Path

# Load .env from project root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("❌ google-genai not installed. Run: pip install google-genai")
    sys.exit(1)

API_KEY = os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    print("❌ GOOGLE_API_KEY not found in environment or .env")
    sys.exit(1)

MODELS_TO_TEST = [
    "gemini-2.5-flash-lite",   # current (preview)
    "gemini-2.0-flash",        # candidate (stable GA)
]

# Simple prompt — minimal tokens, just to trigger a response
SIMPLE_PROMPT = "Responde solo con el número 42."

N_REQUESTS = 10       # consecutive requests per model
DELAY_BETWEEN = 0.5   # seconds between requests (simulate real usage)


async def test_model(model_name: str) -> Dict:
    client = genai.Client(api_key=API_KEY)
    results = []
    latencies = []

    print(f"\n{'='*50}")
    print(f"Testing: {model_name}")
    print(f"{'='*50}")

    for i in range(1, N_REQUESTS + 1):
        start = time.monotonic()
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[SIMPLE_PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain"
                )
            )
            elapsed = time.monotonic() - start
            latencies.append(elapsed)
            results.append("OK")
            print(f"  [{i:02d}/{N_REQUESTS}] ✅ OK — {elapsed:.2f}s — {response.text.strip()[:40]!r}")
        except Exception as e:
            elapsed = time.monotonic() - start
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                results.append("429")
                print(f"  [{i:02d}/{N_REQUESTS}] ❌ 429 RATE_LIMIT — {elapsed:.2f}s")
            elif "404" in err or "not found" in err.lower():
                results.append("404")
                print(f"  [{i:02d}/{N_REQUESTS}] ⚠️  404 MODEL_NOT_FOUND — model may not be available")
                break
            else:
                results.append("ERR")
                print(f"  [{i:02d}/{N_REQUESTS}] ⚠️  ERROR — {err[:80]}")

        if i < N_REQUESTS:
            await asyncio.sleep(DELAY_BETWEEN)

    ok_count = results.count("OK")
    rate_limited = results.count("429")
    avg_latency = sum(latencies) / len(latencies) if latencies else 0

    return {
        "model": model_name,
        "total": len(results),
        "ok": ok_count,
        "rate_limited": rate_limited,
        "errors": len(results) - ok_count - rate_limited,
        "avg_latency_s": round(avg_latency, 3),
        "verdict": _verdict(ok_count, rate_limited, len(results)),
    }


def _verdict(ok: int, rate_limited: int, total: int) -> str:
    if rate_limited == 0 and ok == total:
        return "✅ VIABLE — no rate limits hit"
    elif rate_limited > 0 and ok == 0:
        return "❌ NOT VIABLE — all requests rate limited"
    elif rate_limited > 0:
        return f"⚠️  PARTIAL — {ok}/{total} succeeded before hitting limit"
    else:
        return "⚠️  CHECK ERRORS — unexpected failures"


async def main() -> None:
    print("\n🔬 Gemini Rate Limit Test — Issue #39")
    print(f"   Requests per model : {N_REQUESTS}")
    print(f"   Delay between reqs : {DELAY_BETWEEN}s")
    print(f"   Prompt             : {SIMPLE_PROMPT!r}")

    all_results: List[Dict] = []
    for model in MODELS_TO_TEST:
        result = await test_model(model)
        all_results.append(result)
        # Small pause between models
        if model != MODELS_TO_TEST[-1]:
            print("\n  Pausing 3s before next model...")
            await asyncio.sleep(3)

    # Final report
    print(f"\n{'='*50}")
    print("📊 FINAL REPORT")
    print(f"{'='*50}")
    for r in all_results:
        print(f"\nModel : {r['model']}")
        print(f"  OK          : {r['ok']}/{r['total']}")
        print(f"  Rate limited: {r['rate_limited']}")
        print(f"  Avg latency : {r['avg_latency_s']}s")
        print(f"  Verdict     : {r['verdict']}")

    print(f"\n{'='*50}")
    # Recommendation
    viable = [r for r in all_results if r["rate_limited"] == 0 and r["ok"] == r["total"]]
    if viable:
        best = min(viable, key=lambda x: x["avg_latency_s"])
        print(f"✅ RECOMMENDATION: use '{best['model']}' (fastest viable model)")
    else:
        partial = [r for r in all_results if r["ok"] > 0]
        if partial:
            best = max(partial, key=lambda x: x["ok"])
            print(f"⚠️  RECOMMENDATION: '{best['model']}' is best but still hits limits — consider queuing strategy")
        else:
            print("❌ No model passed. Check API key, billing, or try with N_REQUESTS=3")


if __name__ == "__main__":
    asyncio.run(main())
