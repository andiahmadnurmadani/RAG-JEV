"""Perhitungan deterministik atas tabel: rencana divalidasi, angka dihitung kode.

Alur (tidak ada angka yang dikarang LLM):

1. :func:`build_plan` meminta LLM **hanya** memilih operasi dan nama kolom dari skema nyata.
2. :func:`validate_plan` menolak operasi/kolom yang tidak ada, kolom non-numerik untuk
   agregasi angka, dan nilai filter yang tidak masuk akal -- sebelum satu angka dihitung.
3. :func:`execute_plan` menghitung dari SELURUH baris tabel (bukan potongan retrieval).
4. :func:`narrate` meminta LLM menarasikan angka hasil hitung, dengan larangan menghitung
   ulang atau menambah angka lain.

Setiap kegagalan mengembalikan alasan yang bisa dibaca manusia sehingga lapisan API bisa
menjawab jujur ("kolom 'Total' tidak ada; kolom tersedia: ...") alih-alih menebak.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.errors import AppError
from app.core.logging import get_logger
from app.tables.store import StoredTable, TableStore

logger = get_logger(__name__)

NUMERIC_OPERATIONS = ("sum", "avg", "min", "max", "top_n")
SUPPORTED_OPERATIONS = ("sum", "avg", "min", "max", "count", "count_distinct", "top_n", "list_rows", "none")
MAX_GROUPS = 50
MAX_PREVIEW_ROWS = 50

PLAN_INSTRUCTIONS = """Kamu adalah perencana kueri data. Kamu TIDAK menjawab pertanyaan pengguna.

Tugasmu hanya memilih satu operasi dari daftar dan nama kolom PERSIS seperti pada skema tabel.
Balas HANYA JSON, tanpa penjelasan, dengan bentuk:

{"table": 0,
 "operation": "sum|avg|min|max|count|count_distinct|top_n|list_rows|none",
 "metric": "nama kolom untuk dihitung (wajib untuk sum/avg/min/max/top_n)",
 "group_by": "nama kolom pengelompokan atau null",
 "where": {"column": "nama kolom", "operator": "= | contains | != | not_contains", "value": "teks"} atau null,
 "order": "desc" atau "asc",
 "limit": 5}

Aturan:
- Pakai "top_n" untuk pertanyaan peringkat seperti "paling laku", "tertinggi", "terbanyak",
  "termurah" (metric = kolom angka, group_by = kolom yang diperingkat).
- Pakai "sum" untuk total, "avg" untuk rata-rata, "count" untuk jumlah baris/data.
- Gunakan "none" HANYA bila pertanyaan memang tidak bisa dijawab dengan menghitung kolom
  di tabel ini (mis. tentang kebijakan, narasi, atau dokumen lain).
- Nama kolom wajib disalin apa adanya dari skema. Jika kolom yang dibutuhkan tidak ada,
  gunakan "none" -- jangan mengarang kolom.
"""

NARRATION_INSTRUCTIONS = """Kamu menyajikan hasil perhitungan yang SUDAH dihitung oleh sistem dari data riil.

Aturan mutlak:
- Angka pada blok HASIL adalah satu-satunya angka yang boleh kamu tulis. Jangan menghitung
  ulang, jangan menjumlahkan sendiri, jangan menambah angka lain, jangan membulatkan.
- Jangan menyimpulkan hal yang tidak ada di HASIL.
- Sebutkan cakupan datanya: berkas/lembar dan jumlah baris yang dihitung.
- Untuk hasil peringkat, sebutkan nama pemenangnya lebih dulu, lalu daftar singkat.
- Bahasa jawaban mengikuti bahasa pertanyaan. Ringkas, tanpa pembukaan berbasa-basi.
"""


@dataclass
class PlanError(Exception):
    reason: str
    available_columns: List[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - hanya untuk log
        return self.reason


@dataclass
class TablePlan:
    operation: str
    table_id: Optional[int] = None
    table_index: Optional[int] = None
    metric: Optional[str] = None
    group_by: Optional[str] = None
    where: Optional[Dict[str, str]] = None
    order: str = "desc"
    limit: int = 5


# ---------------------------------------------------------------------- #
# Angka
# ---------------------------------------------------------------------- #


_DATE_LIKE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def parse_number(value: Any) -> Optional[float]:
    """Ubah nilai sel menjadi angka tanpa menebak arti: gagal -> ``None``.

    Sengaja ketat: tanggal (``2026-08-02``) dan kode barang (``PRD-001``) BUKAN angka,
    supaya kolom seperti itu tidak pernah dipakai sebagai kolom hitung.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DATE_LIKE.search(text) or re.search(r"[A-Za-z]", re.sub(r"(?i)\b(rp|idr|usd)\b", "", text)):
        return None
    negative = (text.startswith("(") and text.endswith(")")) or text.lstrip().startswith("-")
    cleaned = re.sub(r"[^0-9.,]", "", text)
    if not cleaned or not any(character.isdigit() for character in cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) == 3 and parts[0]:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            cleaned = cleaned.replace(".", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def numeric_ratio(values: Sequence[Any]) -> float:
    """Bagian nilai yang benar-benar angka (kode seperti ``PRD-001`` tidak dihitung angka)."""

    filled = 0
    numeric = 0
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        filled += 1
        if is_numeric_cell(text):
            numeric += 1
    return (numeric / filled) if filled else 0.0


def is_numeric_cell(text: str) -> bool:
    stripped = re.sub(r"(?i)\b(rp|idr|usd|rp\.|ribu|juta|kg|pcs|unit|%)\b", "", text)
    stripped = stripped.replace("$", "").replace("%", "").strip()
    if not stripped or re.search(r"[A-Za-z]", stripped):
        return False
    return parse_number(stripped) is not None


def format_number(value: float) -> str:
    """Tampilan angka Indonesia: 14830000.0 -> ``14.830.000``."""

    if value is None:
        return ""
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", ".")
    text = f"{value:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return text.rstrip("0").rstrip(",")


# ---------------------------------------------------------------------- #
# Rencana
# ---------------------------------------------------------------------- #


def build_plan(
    client: Any,
    *,
    query: str,
    tables: Sequence[StoredTable],
    max_columns: int = 40,
) -> Optional[TablePlan]:
    """Minta LLM memilih operasi + kolom dari skema nyata. ``None`` bila bukan soal hitung."""

    if not tables:
        return None
    if client is None or not hasattr(client, "chat"):
        return None

    lines: List[str] = ["SKEMA TABEL YANG TERSEDIA:"]
    for index, table in enumerate(tables[:5]):
        columns = ", ".join(table.headers[:max_columns])
        lines.append(
            f"tabel[{index}] berkas={table.document_name} lembar={table.sheet} "
            f"baris={table.row_count} kolom: {columns}"
        )
    payload = "\n".join(lines) + f"\n\nPERTANYAAN PENGGUNA:\n{query}\n\nJSON:"
    try:
        text, _usage = client.chat(
            [
                {"role": "system", "content": PLAN_INSTRUCTIONS},
                {"role": "user", "content": payload},
            ],
            temperature=0.0,
            max_tokens=200,
        )
    except AppError as exc:
        logger.warning("perencanaan tabel gagal: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("perencanaan tabel gagal tak terduga: %s", exc)
        return None

    data = _loads(text)
    if not isinstance(data, dict):
        logger.info("rencana tabel bukan JSON: %s", text[:120])
        return None
    operation = str(data.get("operation") or "none").strip().lower()
    if operation in ("", "none", "null"):
        return None
    if operation not in SUPPORTED_OPERATIONS:
        logger.info("operasi tabel tidak dikenal: %s", operation)
        return None

    def clean(name: Any) -> Optional[str]:
        value = str(name or "").strip()
        return value or None

    where = data.get("where")
    if not isinstance(where, dict):
        where = None
    index: Optional[int] = None
    raw_index = data.get("table")
    if isinstance(raw_index, (int, float, str)):
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = None
        if index is not None and index < 0:
            index = None

    return TablePlan(
        operation=operation,
        table_index=index,
        metric=clean(data.get("metric")),
        group_by=clean(data.get("group_by")),
        where=(
            {
                "column": str(where.get("column") or "").strip(),
                "operator": str(where.get("operator") or "=").strip(),
                "value": str(where.get("value") or "").strip(),
            }
            if where and where.get("column")
            else None
        ),
        order="asc" if str(data.get("order") or "desc").strip().lower() == "asc" else "desc",
        limit=_safe_limit(data.get("limit")),
    )


def _safe_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(MAX_PREVIEW_ROWS, limit))


def resolve_column(name: Optional[str], headers: Sequence[str]) -> Optional[str]:
    """Cocokkan nama kolom dari LLM dengan header nyata (tanpa mengarang kolom baru)."""

    if not name:
        return None
    target = name.strip().lower()
    for header in headers:
        if header.strip().lower() == target:
            return header
    # toleransi tanda baca umum tanpa mengubah arti
    normalized = re.sub(r"[^a-z0-9]+", "", target)
    for header in headers:
        if re.sub(r"[^a-z0-9]+", "", header.strip().lower()) == normalized:
            return header
    return None


def validate_plan(plan: TablePlan, table: StoredTable, rows: Sequence[Sequence[str]]) -> TablePlan:
    """Pastikan kolom benar-benar ada dan kolom metrik benar-benar berisi angka."""

    headers = list(table.headers)
    metric = resolve_column(plan.metric, headers)
    group_by = resolve_column(plan.group_by, headers)
    if plan.where is not None:
        where_column = resolve_column(plan.where.get("column"), headers)
        if where_column is None:
            raise PlanError(
                f"kolom filter '{plan.where.get('column')}' tidak ada di tabel", available_columns=headers
            )
        plan.where["column"] = where_column

    if plan.operation in NUMERIC_OPERATIONS:
        if metric is None:
            raise PlanError(
                f"kolom angka '{plan.metric}' tidak ada di tabel", available_columns=headers
            )
        index = headers.index(metric)
        values = [row[index] if index < len(row) else "" for row in rows]
        ratio = numeric_ratio(values)
        if ratio < 0.5:
            raise PlanError(
                f"kolom '{metric}' bukan kolom angka (hanya {ratio * 100:.0f}% nilai yang terbaca sebagai angka)",
                available_columns=headers,
            )
    if plan.operation in ("count_distinct", "list_rows") and plan.metric is not None and metric is None:
        raise PlanError(f"kolom '{plan.metric}' tidak ada di tabel", available_columns=headers)
    if plan.operation == "top_n" and group_by is None:
        raise PlanError(
            f"kolom pengelompokan '{plan.group_by}' tidak ada di tabel", available_columns=headers
        )

    plan.metric = metric
    plan.group_by = group_by
    return plan


# ---------------------------------------------------------------------- #
# Eksekusi
# ---------------------------------------------------------------------- #


def execute_plan(plan: TablePlan, table: StoredTable, rows: Sequence[Sequence[str]]) -> Dict[str, Any]:
    """Hitung dari seluruh baris. Tidak ada LLM di jalur ini."""

    headers = list(table.headers)
    matched = [row for row in rows if _matches(row, headers, plan.where)]
    metric_index = headers.index(plan.metric) if plan.metric in headers else None
    skipped = 0

    def numbers(rows_subset: Sequence[Sequence[str]]) -> List[float]:
        nonlocal skipped
        out: List[float] = []
        for row in rows_subset:
            raw = row[metric_index] if metric_index is not None and metric_index < len(row) else ""
            value = parse_number(raw)
            if value is None:
                if str(raw or "").strip():
                    skipped += 1
                continue
            out.append(value)
        return out

    result: List[Dict[str, Any]] = []
    operation = plan.operation

    if operation == "count":
        if metric_index is None:
            total: Any = len(matched)
        else:
            total = sum(1 for row in matched if str(row[metric_index] if metric_index < len(row) else "").strip())
        result = [{"jumlah_baris": len(matched), "nilai": total}]
    elif operation == "count_distinct":
        column = plan.metric if plan.metric in headers else (headers[0] if headers else None)
        index = headers.index(column) if column else None
        distinct = sorted({str(row[index]).strip() for row in matched if index is not None and index < len(row) and str(row[index]).strip()})
        result = [{"kolom": column, "jumlah_nilai_unik": len(distinct), "nilai": distinct[:MAX_PREVIEW_ROWS]}]
    elif operation == "list_rows":
        result = [
            {headers[index]: (row[index] if index < len(row) else "") for index in range(len(headers))}
            for row in matched[: plan.limit]
        ]
    elif operation == "top_n":
        group_index = headers.index(plan.group_by)
        buckets: Dict[str, float] = {}
        for row in matched:
            key = str(row[group_index] if group_index < len(row) else "").strip() or "(kosong)"
            value = parse_number(row[metric_index] if metric_index < len(row) else "")
            if value is None:
                if str(row[metric_index] if metric_index < len(row) else "").strip():
                    skipped += 1
                continue
            buckets[key] = buckets.get(key, 0.0) + value
        ordered = sorted(buckets.items(), key=lambda item: item[1], reverse=(plan.order == "desc"))
        result = [
            {plan.group_by: key, plan.metric: _round(value), "teks": format_number(value)}
            for key, value in ordered[: min(plan.limit, MAX_GROUPS)]
        ]
    elif operation in ("sum", "avg", "min", "max"):
        values = numbers(matched)
        if not values:
            raise PlanError(f"kolom '{plan.metric}' tidak berisi angka yang bisa dihitung")
        if operation == "sum":
            value = sum(values)
        elif operation == "avg":
            value = sum(values) / len(values)
        elif operation == "min":
            value = min(values)
        else:
            value = max(values)
        result = [{plan.metric: _round(value), "teks": format_number(value), "n": len(values)}]
    else:  # pragma: no cover - dijaga oleh SUPPORTED_OPERATIONS
        raise PlanError(f"operasi '{operation}' tidak didukung")

    scope = f"{table.document_name} / lembar '{table.sheet}'"
    explanation = _explain(plan, table, len(matched), skipped)
    return {
        "operation": operation,
        "metric": plan.metric,
        "group_by": plan.group_by,
        "order": plan.order,
        "where": plan.where,
        "document_id": table.document_id,
        "document_name": table.document_name,
        "sheet": table.sheet,
        "table_id": table.table_id,
        "columns": headers,
        "rows_total": table.row_count,
        "rows_scanned": len(rows),
        "rows_matched": len(matched),
        "rows_skipped": skipped,
        "scope": scope,
        "explanation": explanation,
        "result": result,
    }


def _round(value: float) -> float:
    return round(float(value), 4)


def _matches(row: Sequence[str], headers: Sequence[str], where: Optional[Dict[str, str]]) -> bool:
    if not where:
        return True
    column = where.get("column")
    if column not in headers:
        return False
    index = headers.index(column)
    cell = str(row[index] if index < len(row) else "").strip().lower()
    value = str(where.get("value") or "").strip().lower()
    operator = where.get("operator") or "="
    if operator == "contains":
        return value in cell
    if operator == "not_contains":
        return value not in cell
    if operator == "!=":
        return cell != value
    cell_number, value_number = parse_number(cell), parse_number(value)
    if cell_number is not None and value_number is not None and operator in ("=", ">", "<", ">=", "<="):
        if operator == "=":
            return cell_number == value_number
        if operator == ">":
            return cell_number > value_number
        if operator == "<":
            return cell_number < value_number
        if operator == ">=":
            return cell_number >= value_number
        return cell_number <= value_number
    return cell == value


def _explain(plan: TablePlan, table: StoredTable, matched: int, skipped: int) -> str:
    verbs = {
        "sum": "menjumlahkan",
        "avg": "merata-ratakan",
        "min": "mengambil nilai terkecil dari",
        "max": "mengambil nilai terbesar dari",
        "count": "menghitung baris",
        "count_distinct": "menghitung nilai unik",
        "top_n": "mengurutkan",
        "list_rows": "menampilkan baris",
    }
    parts = [f"{verbs.get(plan.operation, plan.operation)} kolom '{plan.metric}'" if plan.metric else verbs.get(plan.operation, plan.operation)]
    if plan.group_by:
        parts.append(f"dikelompokkan per '{plan.group_by}'")
    if plan.where:
        parts.append(
            f"difilter {plan.where.get('column')} {plan.where.get('operator')} {plan.where.get('value')!r}"
        )
    text = " ".join(parts)
    detail = (
        f"dihitung dari {matched} baris pada {table.document_name} (lembar '{table.sheet}'); "
        f"total {table.row_count} baris tabel. {text.capitalize()}."
    )
    if skipped:
        detail += f" {skipped} nilai tidak terbaca sebagai angka dan dilewati."
    return detail


def facts_block(facts: Dict[str, Any]) -> str:
    """Blok fakta untuk narasi: angka apa adanya, ditulis dalam bentuk final."""

    lines = [
        f"SUMBER: {facts.get('scope', '')}",
        f"PERHITUNGAN: {facts.get('explanation', '')}",
        f"BARIS DIHITUNG: {facts.get('rows_matched', 0)} dari {facts.get('rows_total', 0)}",
        "HASIL:",
    ]
    for row in facts.get("result", []):
        rendered = []
        for key, value in row.items():
            if key == "teks":
                continue
            if isinstance(value, float) and not value.is_integer():
                rendered.append(f"{key} = {value}")
            elif isinstance(value, float):
                rendered.append(f"{key} = {format_number(value)}")
            elif isinstance(value, list):
                rendered.append(f"{key} = {', '.join(str(item) for item in value[:20])}")
            else:
                rendered.append(f"{key} = {value}")
        lines.append("- " + "; ".join(rendered))
    return "\n".join(lines)


def narrate(client: Any, *, query: str, facts: Dict[str, Any]) -> Tuple[str, Any]:
    """Minta LLM menarasikan angka hasil hitung (tanpa menghitung ulang)."""

    if client is None or not hasattr(client, "chat"):
        return _fallback_text(facts), None
    user = f"PERTANYAAN PENGGUNA:\n{query}\n\nHASIL PERHITUNGAN (satu-satunya angka yang boleh ditulis):\n{facts_block(facts)}"
    try:
        text, usage = client.chat(
            [
                {"role": "system", "content": NARRATION_INSTRUCTIONS},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - narasi gagal tidak boleh menghilangkan angka
        logger.warning("narasi tabel gagal: %s", exc)
        return _fallback_text(facts), None
    if not (text or "").strip():
        return _fallback_text(facts), usage
    return text.strip(), usage


def _fallback_text(facts: Dict[str, Any]) -> str:
    lines = [facts["explanation"].capitalize()]
    for row in facts.get("result", [])[:10]:
        rendered = "; ".join(
            f"{key} = {format_number(value) if isinstance(value, float) else value}"
            for key, value in row.items()
            if key != "teks"
        )
        lines.append(f"- {rendered}")
    return "\n".join(lines)


def plan_for_scope(
    store: TableStore,
    *,
    organization_id: str,
    knowledge_base_id: Optional[str],
    document_ids: Optional[Sequence[str]] = None,
) -> List[StoredTable]:
    return store.list_tables(
        organization_id=organization_id,
        knowledge_base_id=knowledge_base_id,
        document_ids=document_ids,
    )


def _loads(text: str) -> Optional[Any]:
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*", "", candidate).strip().rstrip("`").strip()
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:  # noqa: BLE001
                return None
        return None
