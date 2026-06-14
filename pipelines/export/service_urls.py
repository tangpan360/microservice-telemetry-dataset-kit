from __future__ import annotations

import os

PROM_LOCAL_URL = "http://127.0.0.1:19090"
JAEGER_LOCAL_URL = "http://127.0.0.1:16686"


def default_prom_url() -> str:
    override = os.environ.get("EXPORT_PROM_URL", "").strip()
    if override:
        return override
    return PROM_LOCAL_URL


def default_jaeger_url() -> str:
    override = os.environ.get("EXPORT_JAEGER_URL", "").strip()
    if override:
        return override
    return JAEGER_LOCAL_URL


def resolve_prom_url(value: str | None) -> str:
    text = (value or "").strip()
    return text or default_prom_url()


def resolve_jaeger_url(value: str | None) -> str:
    text = (value or "").strip()
    return text or default_jaeger_url()
