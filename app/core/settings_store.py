"""Runtime overrides for the *service-side* model configuration (LLM + Jev).

Env vars stay the source of truth at boot; this file lets an operator change the two
things that are genuinely machine- or vendor-specific - which LLM and which Jev - from
the settings screen, without a redeploy or a restart.

Design rules:

* **Global, therefore admin-only.** LLM and Jev configuration is not per-tenant: one
  tenant editing it would change generation for every other tenant. The routes that
  touch this file require the ``admin`` permission, and tenants without it never see
  the values (``/ready`` keeps publishing only the model *name*, which is already
  visible in every answer's metadata).
* **Secrets are write-only.** ``GET /settings`` never returns a key; it returns
  ``api_key_set`` plus a short hint. Anything else would put a live key in a browser
  history, a log line, or a screenshot.
* **The file wins over env, and empty means "keep".** A field absent from an update
  keeps its current value; ``""`` on a key field clears it; ``null`` is not accepted.

The file holds real credentials, so it is written atomically with mode 0600.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.jsonfile import read_json, write_json_atomic

# section -> {field: (settings attribute, type)}
SPEC: Dict[str, Dict[str, Tuple[str, str]]] = {
    "llm": {
        "provider": ("llm_provider", "str"),
        "base_url": ("llm_base_url", "str"),
        "model": ("llm_model", "str"),
        "api_key": ("llm_api_key", "secret"),
        # Batas token keluaran: yang menentukan jawaban panjang selesai atau terpotong.
        "max_tokens": ("llm_max_tokens", "int"),
        # Sampling & perbaikan teks rusak. Bisa disetel tanpa redeploy karena kualitas keluaran
        # bergantung pada model yang dipakai - dan model dipilih dari layar ini.
        "temperature": ("llm_temperature", "float"),
        "top_p": ("llm_top_p", "float"),
        "frequency_penalty": ("llm_frequency_penalty", "float"),
        "repair_attempts": ("llm_repair_attempts", "int"),
        # Buang aksara dari tulisan lain yang diselipkan model (bukan yang ada di dokumen).
        "strip_foreign_scripts": ("text_strip_foreign", "bool"),
    },
    "jev": {
        "enabled": ("jev_enabled", "bool"),
        "mode": ("jev_mode", "str"),
        "provider": ("jev_provider", "str"),
        "systemone_url": ("jev_systemone_url", "str"),
        "model": ("jev_model", "str"),
        "mcp_url": ("jev_mcp_url", "str"),
        "api_key": ("jev_api_key", "secret"),
    },
    # Format berkas yang boleh jadi knowledge + batas ukurannya. Global seperti LLM/Jev:
    # ini kebijakan layanan, bukan preferensi satu tenant.
    "uploads": {
        "extensions": ("upload_extensions", "list"),
        "max_upload_mb": ("max_upload_mb", "int"),
    },
    # Perilaku pengambilan konteks - inilah yang menentukan berapa banyak isi dokumen yang
    # benar-benar dibaca model. Global, karena satu tenant yang menaikkannya akan memakai
    # kuota model bersama. Disediakan supaya pemasangan bisa disetel tanpa redeploy: anggaran
    # konteks naik ketika dokumennya besar, dan turun ketika modelnya berjendela kecil.
    "retrieval": {
        "context_max_tokens": ("context_max_tokens", "int"),
        "final_top_k": ("final_top_k", "int"),
        "max_chunks_per_document": ("max_chunks_per_document", "int"),
        "context_expand_documents": ("context_expand_documents", "bool"),
        "reranker_enabled": ("reranker_enabled", "bool"),
        "reranker_provider": ("reranker_provider", "str"),
        "strict_grounding": ("strict_grounding", "bool"),
        # Kualitas jawaban: gerbang "tidak ditemukan", ambang relatif, konteks di sekitar hasil.
        "min_relevance": ("min_relevance", "float"),
        "relevance_threshold": ("relevance_threshold", "float"),
        "context_neighbor_chunks": ("context_neighbor_chunks", "int"),
        "context_full_document_tokens": ("context_full_document_tokens", "int"),
        "context_expand_max_documents": ("context_expand_max_documents", "int"),
        "hash_dense_weight": ("hash_dense_weight", "float"),
    },
    # Ringkasan dokumen (knowledge turunan). Global seperti setelan lain: satu tenant yang
    # menyalakannya memakai kuota model bersama, dan ringkasannya tersimpan di indeks bersama.
    "summary": {
        "enabled": ("document_summary_enabled", "bool"),
        "window_tokens": ("summary_window_tokens", "int"),
        "max_tokens": ("summary_max_tokens", "int"),
        "max_documents": ("summary_max_documents", "int"),
    },
    # Pertanyaan yang tidak terjawab: apa yang dicatat dan berapa lama disimpan.
    "unanswered": {
        "enabled": ("unanswered_enabled", "bool"),
        "reasons": ("unanswered_reasons", "list"),
        "retention_days": ("unanswered_retention_days", "int"),
        "max_entries": ("unanswered_max_entries", "int"),
    },
    # Sumber dari web. Global karena crawl membebani jaringan keluar dan situs orang lain -
    # satu tenant yang menaikkannya akan memakai kuota bersama.
    "web": {
        "enabled": ("web_crawl_enabled", "bool"),
        "max_pages": ("web_crawl_max_pages", "int"),
        "max_depth": ("web_crawl_max_depth", "int"),
        "same_host": ("web_crawl_same_host", "bool"),
        "follow_files": ("web_crawl_follow_files", "bool"),
        "respect_robots": ("web_crawl_respect_robots", "bool"),
        "allow_private_urls": ("allow_private_urls", "bool"),
    },
}

def mask_secret(value: str) -> Optional[str]:
    """``sk-cc8bfe83...ab50`` -> ``sk-c...ab50``; short values -> ``****``."""
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def read_overrides(path: Path) -> Dict[str, Dict[str, Any]]:
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def write_overrides(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    write_json_atomic(path, data)


def apply_overrides(settings: Any, overrides: Dict[str, Dict[str, Any]]) -> list[str]:
    """Push stored overrides onto a live Settings object. Returns the fields applied."""
    applied: list[str] = []
    for section, fields in SPEC.items():
        stored = overrides.get(section) or {}
        if not isinstance(stored, dict):
            continue
        for field, (attribute, kind) in fields.items():
            if field not in stored:
                continue
            value = stored[field]
            if kind == "bool":
                value = bool(value)
            elif kind == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif kind == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            elif kind == "list":
                items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
                value = ",".join(str(item).strip().lower() for item in items if str(item).strip())
            elif kind == "secret":
                value = "" if value is None else str(value)
            else:
                value = "" if value is None else str(value)
            setattr(settings, attribute, value)
            applied.append(f"{section}.{field}")
    return applied


def describe(settings: Any, path: Optional[Path] = None) -> Dict[str, Any]:
    """Masked view for the settings screen: values in, secrets out."""
    out: Dict[str, Any] = {"sections": {}, "source": str(path) if path else None}
    for section, fields in SPEC.items():
        block: Dict[str, Any] = {}
        for field, (attribute, kind) in fields.items():
            value = getattr(settings, attribute, None)
            if kind == "secret":
                block["api_key_set"] = bool(value)
                block["api_key_hint"] = mask_secret(str(value or ""))
            elif kind == "bool":
                block[field] = bool(value)
            elif kind == "list":
                items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
                block[field] = [str(item).strip().lower() for item in items if str(item).strip()]
            elif kind == "int":
                try:
                    block[field] = int(value)
                except (TypeError, ValueError):
                    block[field] = None
            elif kind == "float":
                try:
                    block[field] = float(value)
                except (TypeError, ValueError):
                    block[field] = None
            else:
                block[field] = value
        out["sections"][section] = block
    return out


def extract_updates(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Validate an incoming update: only known sections/fields survive."""
    updates: Dict[str, Dict[str, Any]] = {}
    for section, fields in SPEC.items():
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        accepted: Dict[str, Any] = {}
        for field, (_, kind) in fields.items():
            if field not in incoming:
                continue
            value = incoming[field]
            if kind == "bool":
                accepted[field] = bool(value)
            elif kind == "list":
                if isinstance(value, str):
                    value = [part for part in value.split(",") if part.strip()]
                if not isinstance(value, (list, tuple)):
                    continue
                accepted[field] = [str(item).strip().lower() for item in value if str(item).strip()]
            elif kind == "int":
                try:
                    accepted[field] = int(value)
                except (TypeError, ValueError):
                    continue
            elif kind == "float":
                try:
                    accepted[field] = float(value)
                except (TypeError, ValueError):
                    continue
            elif value is None:
                continue  # null is not accepted: use "" to clear a key
            else:
                accepted[field] = str(value).strip()
        if accepted:
            updates[section] = accepted
    return updates


def merge(stored: Dict[str, Dict[str, Any]], updates: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    merged = {section: dict(fields) for section, fields in stored.items() if isinstance(fields, dict)}
    for section, fields in updates.items():
        merged.setdefault(section, {}).update(fields)
    return merged


def describe_secrets_present(stored: Dict[str, Dict[str, Any]]) -> Dict[str, bool]:
    return {
        section: bool((stored.get(section) or {}).get("api_key"))
        for section in SPEC
    }
