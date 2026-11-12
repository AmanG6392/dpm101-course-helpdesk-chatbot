"""Thin Gemini REST client: retries with backoff on 429/5xx, typed errors, JSON-schema output."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Callable

import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.0

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "in_docs": {"type": "BOOLEAN"},
        "sources": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["answer", "in_docs", "sources"],
}


class GeminiError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind  # rate_limit | unavailable | config | bad_request | bad_response


def _parse(data: dict) -> dict:
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise GeminiError("bad_response", "empty or blocked response")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise GeminiError("bad_response", "non-JSON response")
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            raise GeminiError("bad_response", "unparseable JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("answer"), str) or not obj["answer"].strip():
        raise GeminiError("bad_response", "missing answer")
    srcs = obj.get("sources") or []
    return {
        "answer": obj["answer"].strip(),
        "in_docs": bool(obj.get("in_docs", True)),
        "sources": [s for s in srcs if isinstance(s, str)],
    }


async def generate(system: str, contents: list[dict], on_call: Callable[[], None],
                   client: httpx.AsyncClient | None = None) -> dict:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise GeminiError("config", "GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gen_cfg: dict = {"temperature": 0.1, "maxOutputTokens": 700,
                     "responseMimeType": "application/json", "responseSchema": SCHEMA}
    if "2.5" in model and "pro" not in model:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # fast + cheap; task is simple lookup
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
            "generationConfig": gen_cfg}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    kind, delay = "unavailable", BACKOFF_BASE
    try:
        for attempt in range(MAX_ATTEMPTS):
            on_call()  # every real HTTP attempt counts as an API call
            try:
                r = await client.post(f"{BASE}/{model}:generateContent", json=body,
                                      headers={"x-goog-api-key": key})
            except httpx.HTTPError:
                kind = "unavailable"
            else:
                if r.status_code == 200:
                    return _parse(r.json())
                if r.status_code == 429:
                    kind = "rate_limit"
                elif r.status_code >= 500:
                    kind = "unavailable"
                else:  # 400/401/403/404: retrying will not help
                    raise GeminiError("bad_request", f"HTTP {r.status_code}: {r.text[:200]}")
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(delay)
                delay *= 2
        raise GeminiError(kind, "retries exhausted")
    finally:
        if own:
            await client.aclose()
