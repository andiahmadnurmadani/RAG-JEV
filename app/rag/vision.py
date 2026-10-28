"""Visi: membaca gambar (grafik, tabel, diagram, halaman pindaian) lewat model yang menerima gambar.

Dipakai klien yang menyusun slide/laporan: isi berbentuk gambar di dokumen sumber tidak ikut
terindeks sebagai teks, jadi angka grafik dan baris tabel hanya bisa didapat dengan "melihat"
gambarnya. Layanan ini tidak menyimpan gambar apa pun; hasilnya dikembalikan ke pemanggil.

Endpoint, kunci, dan model visi bisa diatur terpisah (bagian ``vision`` di Setelan); yang kosong
ikut bagian ``llm``. Kunci LLM hanya dipakai bila endpoint visi SAMA dengan endpoint LLM, supaya
mengganti alamat visi tidak ikut mengirim kunci LLM ke server lain.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import Settings
from app.core.errors import AppError
from app.rag.generator import LLMClient, LLMUsage, MockLLMClient, _loads_tolerant

VISION_SYSTEM_PROMPT = (
    "Kamu membaca gambar dari dokumen kantor (grafik, tabel, diagram, slide, halaman pindaian).\n"
    "Aturan:\n"
    "1. Tulis hanya yang benar-benar terlihat di gambar. Jangan menebak angka: angka yang tidak "
    "terbaca jelas diisi null dan disebut di kolom catatan bila skemanya punya.\n"
    "2. Pertahankan angka, satuan, dan ejaan persis seperti di gambar (koma desimal Indonesia "
    "diubah ke titik hanya bila diminta format JSON angka).\n"
    "3. Baca teks sesuai susunan visualnya: judul, angka utama, lalu keterangan; tabel per baris "
    "dari atas ke bawah, kolom dari kiri ke kanan.\n"
    "4. Ikuti instruksi pengguna. Bila diminta JSON, balas JSON saja tanpa teks pembuka atau pagar kode."
)

_MIME_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


@dataclass
class VisionImage:
    data: bytes
    mime: str
    width: int = 0
    height: int = 0


@dataclass
class VisionResult:
    answer: str
    model: str
    usage: LLMUsage
    parsed: Optional[Any] = None
    images: List[Dict[str, Any]] = field(default_factory=list)


class _VisionSettings:
    """Setelan LLM dengan endpoint/kunci/model visi di atasnya (LLMClient membaca ``llm_*`` per panggilan)."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings
        base_url = (settings.vision_base_url or "").strip() or settings.llm_base_url
        same_host = base_url.rstrip("/") == (settings.llm_base_url or "").rstrip("/")
        self.llm_base_url = base_url
        self.llm_api_key = (settings.vision_api_key or "").strip() or (settings.llm_api_key if same_host else "")
        self.llm_model = (settings.vision_model or "").strip() or settings.llm_model
        self.llm_timeout = settings.vision_timeout or settings.llm_timeout
        self.llm_max_tokens = settings.vision_max_tokens or settings.llm_max_tokens
        # parameter sampling opsional tidak dikirim: banyak endpoint visi menolaknya
        self.llm_top_p = 0
        self.llm_frequency_penalty = 0
        self.llm_presence_penalty = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _image_size(data: bytes) -> Tuple[int, int]:
    """Lebar x tinggi tanpa dependensi gambar tambahan (PyMuPDF sudah dipakai untuk PDF)."""
    try:
        import fitz  # PyMuPDF

        pixmap = fitz.Pixmap(data)
        return int(pixmap.width), int(pixmap.height)
    except Exception:  # noqa: BLE001
        return 0, 0


def decode_images(raw: List[Dict[str, Any]], settings: Settings) -> List[VisionImage]:
    """Base64 -> byte, dengan batas jumlah, ukuran berkas, jenis, dan sisi terpanjang."""
    limit = int(settings.vision_max_images or 8)
    if not raw:
        raise AppError("VALIDATION_ERROR", "images wajib berisi minimal 1 gambar")
    if len(raw) > limit:
        raise AppError("VALIDATION_ERROR", f"paling banyak {limit} gambar per permintaan", details={"images": len(raw)})
    max_bytes = int(float(settings.vision_max_image_mb or 2.0) * 1024 * 1024)
    max_side = int(settings.vision_max_side or 0)
    images: List[VisionImage] = []
    for index, item in enumerate(raw):
        data_field = str(item.get("data") or "")
        mime = str(item.get("mime") or "").strip().lower()
        if data_field.startswith("data:"):
            # data URL: "data:image/png;base64,...."
            header, _, data_field = data_field.partition(",")
            mime = mime or header[5:].split(";")[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if not mime:
            # tanpa mime: kenali dari beberapa byte pertama
            head = base64.b64decode(data_field[:24] + "=" * (-len(data_field[:24]) % 4), validate=False)
            mime = next((kind for kind, magic in _MIME_MAGIC.items() if head.startswith(magic)), "")
        if mime not in _MIME_MAGIC:
            raise AppError(
                "UNSUPPORTED_MEDIA_TYPE",
                f"gambar #{index + 1}: jenis '{mime or '?'}' tidak didukung (png, jpeg, webp)",
            )
        try:
            data = base64.b64decode(data_field, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", f"gambar #{index + 1}: data bukan base64 yang sah") from exc
        if not data:
            raise AppError("VALIDATION_ERROR", f"gambar #{index + 1}: data kosong")
        if len(data) > max_bytes:
            raise AppError(
                "PAYLOAD_TOO_LARGE",
                f"gambar #{index + 1}: {len(data) / 1048576:.1f} MB, batasnya {max_bytes / 1048576:.1f} MB",
            )
        if not data.startswith(_MIME_MAGIC[mime]) or (mime == "image/webp" and data[8:12] != b"WEBP"):
            raise AppError("UNSUPPORTED_MEDIA_TYPE", f"gambar #{index + 1}: isi berkas bukan {mime}")
        width, height = _image_size(data)
        if max_side and max(width, height) > max_side:
            raise AppError(
                "PAYLOAD_TOO_LARGE",
                f"gambar #{index + 1}: {width}x{height} piksel, sisi terpanjang paling besar {max_side}",
            )
        images.append(VisionImage(data=data, mime=mime, width=width, height=height))
    return images


def _wants_json(response_format: Optional[Dict[str, Any]]) -> bool:
    return bool(response_format) and str(response_format.get("type")) in {"json_object", "json_schema"}


class VisionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        return _VisionSettings(self._settings).llm_model

    def _client(self):
        if self._settings.llm_provider == "mock":
            return MockVisionClient()
        return LLMClient(_VisionSettings(self._settings))  # type: ignore[arg-type]

    def _messages(self, query: str, images: List[VisionImage], style: str, response_format) -> List[Dict[str, Any]]:
        instruction = query.strip()
        if response_format and response_format.get("type") == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema") or {}
            instruction += "\n\nBalas JSON yang memenuhi JSON Schema ini:\n" + json.dumps(schema, ensure_ascii=False)
        elif _wants_json(response_format):
            instruction += "\n\nBalas JSON saja."
        parts: List[Dict[str, Any]] = []
        for image in images:
            encoded = base64.b64encode(image.data).decode("ascii")
            if style == "anthropic":
                parts.append({"type": "image", "source": {"type": "base64", "media_type": image.mime, "data": encoded}})
            else:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{encoded}"}})
        parts.append({"type": "text", "text": instruction})
        return [{"role": "system", "content": VISION_SYSTEM_PROMPT}, {"role": "user", "content": parts}]

    def run(
        self,
        *,
        images: List[VisionImage],
        query: str,
        response_format: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> VisionResult:
        if not self._settings.vision_enabled:
            raise AppError("AUTH_FORBIDDEN", "endpoint visi dimatikan di Setelan (vision.enabled)")
        client = self._client()
        style = client._style() if isinstance(client, LLMClient) else "openai"
        messages = self._messages(query, images, style, response_format)
        cap = int(self._settings.vision_max_tokens or 4000)
        tokens = min(int(max_tokens or cap), cap)
        endpoint_format = None
        if _wants_json(response_format):
            # json_schema diteruskan bila diminta; endpoint yang belum mendukungnya dicoba ulang dengan json_object
            endpoint_format = response_format if response_format.get("type") == "json_schema" else {"type": "json_object"}
        text, usage = self._call(client, messages, tokens, endpoint_format)
        parsed = None
        if _wants_json(response_format):
            parsed = _loads_tolerant(text)
            if parsed is None:
                # satu kali perbaikan: model visi kadang menempelkan kalimat pembuka
                repair_messages = [
                    {"role": "system", "content": "Return valid JSON only. No prose, no markdown fences."},
                    {"role": "user", "content": f"Convert this into valid JSON without changing any value:\n{text}"},
                ]
                repaired, extra = self._call(client, repair_messages, tokens, {"type": "json_object"})
                usage = LLMUsage(
                    input_tokens=usage.input_tokens + extra.input_tokens,
                    output_tokens=usage.output_tokens + extra.output_tokens,
                    latency_ms=usage.latency_ms + extra.latency_ms,
                    model=usage.model,
                    finish_reason=extra.finish_reason or usage.finish_reason,
                )
                parsed = _loads_tolerant(repaired)
            if parsed is None:
                raise AppError(
                    "LLM_FAILED",
                    "model visi tidak mengembalikan JSON yang sah",
                    details={"finish_reason": usage.finish_reason, "model": usage.model or self.model},
                )
            text = json.dumps(parsed, ensure_ascii=False)
        return VisionResult(
            answer=text.strip(),
            model=usage.model or self.model,
            usage=usage,
            parsed=parsed,
            images=[{"mime": image.mime, "bytes": len(image.data), "width": image.width, "height": image.height} for image in images],
        )

    @staticmethod
    def _call(client, messages, max_tokens, response_format) -> Tuple[str, LLMUsage]:
        """Format JSON yang ditolak endpoint (400/422) diturunkan bertahap: json_schema -> json_object -> instruksi saja."""
        formats: List[Optional[Dict[str, Any]]] = [response_format]
        if response_format and response_format.get("type") == "json_schema":
            formats.append({"type": "json_object"})
        if response_format:
            formats.append(None)
        for position, current in enumerate(formats):
            try:
                return client.chat(messages, temperature=0.0, max_tokens=max_tokens, response_format=current)
            except AppError as exc:
                info = (exc.details or {}).get("llm") or {}
                upstream = str(info.get("upstream") or "").lower()
                if info.get("status") in (400, 404, 422) and any(
                    word in upstream for word in ("image", "vision", "multimodal", "multi-modal", "unsupported content")
                ):
                    raise AppError(
                        "LLM_FAILED",
                        f"model '{info.get('model') or '?'}' tidak menerima gambar; isi vision.model di Setelan dengan model visi",
                        details={"llm": info, "reason": "vision_unsupported"},
                    ) from exc
                last = position == len(formats) - 1
                if last or info.get("status") not in (400, 422):
                    raise
        raise AssertionError("unreachable")


class MockVisionClient(MockLLMClient):
    """Untuk tes dan LLM_PROVIDER=mock: tidak melihat gambar, hanya melaporkan apa yang diterimanya."""

    def __init__(self) -> None:
        self.model = "mock-vision"

    def chat(self, messages, *, temperature=None, max_tokens=None, response_format=None, timeout=None):
        images = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                images += sum(1 for part in content if part.get("type") in {"image_url", "image"})
        if response_format:
            content = json.dumps({"images_seen": images, "mock": True})
        else:
            content = f"Gambar diterima: {images}."
        return content, LLMUsage(input_tokens=images * 100, output_tokens=len(content.split()), model=self.model)
