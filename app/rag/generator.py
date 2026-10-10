"""Answer generation and extraction (PRD 8.1, 12, 16, 17, 26, 35).

The LLM is any OpenAI-compatible chat endpoint (``LLM_BASE_URL``); the model name is read
per call from settings (``llm.model`` — editable from the UI or env), so the same code covers
a hosted gateway and the PRD's self-hosted target (a local vLLM/Ollama serving e.g. Qwen3 4B)
with no change. ``Qwen/Qwen3-4B`` is only the code default, not a requirement.

Anti-hallucination is enforced twice:

1. structurally — ``strict_grounding`` refuses to call the LLM at all when the
   retrieval stage produced no candidate above threshold (PRD 16);
2. by prompt — the system prompt forbids outside knowledge and requires citations
   (PRD 17), and states that retrieved documents are untrusted data (PRD 35).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.parsing.sanitize import foreign_tokens, strip_foreign_tokens
from app.rag.context import UNTRUSTED_NOTICE, BuiltContext

logger = get_logger(__name__)

NO_ANSWER_ID = "Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia."
NO_ANSWER_EN = "The requested information was not found in the available knowledge base."

# Frasa yang menyatakan isi konteks tidak menjawab (bukan fakta dari dokumen).
META_REFUSAL_MARKERS = (
    "tidak ditemukan",
    "informasi tersebut tidak",
    "tidak ada informasi",
    "tidak memuat informasi",
    "tidak menyebutkan",
    "tidak menjelaskan",
    "tidak cukup informasi",
    "tidak tersedia dalam",
    "not found",
    "no information",
    "does not contain",
    "does not mention",
)

NOT_FOUND_MARKERS = (
    "tidak ditemukan",
    "tidak terdapat",
    "informasi tersebut tidak",
    "not found",
    "no information",
    "tidak cukup informasi",
    "tidak ada informasi",
)

SYSTEM_PROMPT = """You are an organizational knowledge assistant.

Answer only using the provided context.

Rules:
1. Do not invent information. Every fact in the answer must come from the context.
2. If the context contains nothing that answers the question, reply with exactly this sentence
   and nothing else: "{no_answer_id}" (for an English question: "{no_answer_en}").
3. If the context answers only part of the question, answer that part (with citations) and then
   say briefly which part is not in the context. Do not refuse a question you can partly answer.
4. Do not use knowledge from other organizations.
5. Cite the source for every factual sentence with its context number, e.g. [1] or [2][3]. Only
   use numbers that exist in the context.
6. Preserve numbers, dates, names, and policies accurately - copy them exactly as written.
7. Context blocks from the same document are given in document order; read neighbouring blocks
   together, because a sentence or table can continue into the next block.
8. Blocks can come from different documents (see each block's Document header). Never merge
   facts from different documents into one statement unless the question asks to compare them.
   If documents give different values for the same thing (e.g. another branch, unit, product,
   or year), answer from the document that matches the question; if the question does not say
   which one, list each value separately with its document name. Ignore blocks that are about a
   different subject than the question.
9. Do not expose internal metadata unless requested and permitted.

Formatting:
- Answer in Markdown (GitHub-flavoured). It is rendered in a chat panel, so use Markdown to make
  the answer easy to scan - never output a file, an attachment, or a download link.
- Use `##`/`###` headings when the answer has sections; `-` bullets for lists; a Markdown table
  when the data is tabular (one column per field, header row included).
- Use **bold** for the key term or figure in a sentence, and `code` for column names, table names,
  field names, and literal values copied from the document.
- Do not wrap the whole answer in a code fence, and do not repeat the question.

Language and spelling (important - the answer is read by people):
- Write every word out in full. Never split a word, never join two words without a space, and
  never insert punctuation inside a word.
- Keep one language throughout: answer in the question's language and do not blend in words from
  another language. Copy technical terms, column names, and proper nouns exactly as written in
  the context.
- If you are unsure of a spelling, use the spelling that appears in the context.

Script (important):
- Write using ONLY the letters of the answer's language (Latin letters for Indonesian and
  English). Never insert characters from another writing system - Chinese, Japanese, Korean,
  Arabic, Cyrillic, Thai, or any other script - even for a single word.
- Do not translate a term into another script. If a term in the context is written in another
  script, keep it exactly as it is in the context; otherwise leave it out.

{untrusted_notice}
Cite sources with the bracketed context number, for example [1] or [2][3].
Answer in the same language as the user's question.""".format(
    untrusted_notice=UNTRUSTED_NOTICE, no_answer_id=NO_ANSWER_ID, no_answer_en=NO_ANSWER_EN
)

EXTRACT_SYSTEM_PROMPT = (
    """You extract structured data from organizational documents.

Return ONLY a JSON object that satisfies the requested JSON schema.
Use only the provided context. Never invent values that are not present.
If the context lacks the requested data, return an empty result.

"""
    + UNTRUSTED_NOTICE
)

# Ringkasan knowledge turunan. Aturannya sengaja ketat: ringkasan yang menyimpang dari isi
# dokumen akan dipakai sebagai "fakta" pada pertanyaan berikutnya, jadi kesalahan di sini
# berlipat. Tidak ada angka, nama, atau kebijakan yang boleh muncul tanpa ada di konteks.
SUMMARY_PROMPT_ID = (
    """Anda menyusun ringkasan resmi untuk sebuah dokumen pengetahuan organisasi.

Aturan:
1. Pakai HANYA isi konteks yang diberikan. Jangan menambah pengetahuan dari luar.
2. Jangan mengarang angka, tanggal, nama, atau kebijakan. Bila sesuatu tidak ada di konteks,
   jangan ditulis.
3. Tulis ringkasan padat dan mandiri: pembaca yang hanya membaca ringkasan ini harus paham isi
   pokok dokumennya.
4. Pertahankan istilah, nama kolom, nama tabel, dan angka persis seperti di dokumen.
5. Bila dokumen memuat daftar (tabel, langkah, syarat), sebutkan butir-butir utamanya -
   jangan hanya bilang "berisi daftar".
6. Jangan menyapa pembaca, jangan membuka dengan "berdasarkan dokumen", dan jangan menutup
   dengan tawaran bantuan. Langsung ke isinya.
7. Bahasa ringkasan mengikuti bahasa dokumen (Indonesia bila dokumennya Indonesia).
8. Tulis dalam Markdown (bukan berkas, bukan lampiran): judul `##` untuk bagian, `-` untuk
   daftar, dan tabel Markdown bila isinya memang tabel. Pakai `code` untuk nama kolom/tabel.
9. Pakai HANYA huruf Latin. Jangan menyisipkan aksara dari tulisan lain - China, Jepang, Korea,
   Arab, Kiril, Thai - walau hanya satu kata. Bila istilah di konteks tertulis dengan aksara
   lain, salin apa adanya; kalau tidak, jangan ditulis.

"""
    + UNTRUSTED_NOTICE
)

SUMMARY_PROMPT_EN = (
    """You write an official summary of an organizational knowledge document.

Rules:
1. Use ONLY the provided context. Do not add outside knowledge.
2. Do not invent numbers, dates, names, or policies. If something is not in the context,
   leave it out.
3. Write a dense, self-contained summary: a reader who only reads the summary must understand
   the document's substance.
4. Preserve terms, column names, table names, and figures exactly as written.
5. If the document contains a list (tables, steps, requirements), name the main items -
   do not just say "it contains a list".
6. Do not address the reader, do not open with "based on the document", and do not close with
   an offer of help. Go straight to the content.
7. Write the summary in the document's language.
8. Write in Markdown (not a file, not an attachment): `##` headings for sections, `-` for lists,
   and a Markdown table when the content is tabular. Use `code` for column/table names.
9. Use ONLY Latin letters. Never insert characters from another writing system - Chinese,
   Japanese, Korean, Arabic, Cyrillic, Thai - not even for a single word. If a term in the
   context is written in another script, copy it as is; otherwise leave it out.

"""
    + UNTRUSTED_NOTICE
)


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""
    # Alasan berhentinya model menurut penyedia: "stop" (selesai), "length" (kena batas token
    # keluaran), "content_filter", dsb. Dipakai supaya jawaban yang TERPOTONG tidak dilaporkan
    # sebagai "informasi tidak ditemukan" - dua hal yang sangat berbeda bagi pemakainya.
    finish_reason: str = ""


@dataclass
class GeneratedAnswer:
    answer: str
    grounded: bool
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: str = ""
    citations_used: List[int] = field(default_factory=list)
    # True bila model berhenti karena batas token keluaran, bukan karena selesai menjawab.
    truncated: bool = False


class MockLLMClient:
    """Deterministic stand-in for tests and offline demos (``LLM_PROVIDER=mock``).

    It answers strictly from the retrieved context: every sentence comes from the
    context block, so grounded/ungrounded behaviour can be tested without a model.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model = "mock-llm"

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> tuple[str, LLMUsage]:
        user = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
        started = time.perf_counter()
        if response_format and response_format.get("type") == "json_object" and "Extract the requested data" in user:
            content = json.dumps(self._mock_items(user), ensure_ascii=False)
        else:
            content = self._mock_answer(user)
        usage = LLMUsage(
            input_tokens=max(1, len(user.split())),
            output_tokens=max(1, len(content.split())),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            model=self.model,
        )
        return content, usage

    @staticmethod
    def _context_lines(user: str) -> List[str]:
        inside = False
        lines: List[str] = []
        for line in user.splitlines():
            if line.startswith("<<<RETRIEVED_CONTEXT"):
                inside = True
                continue
            if line.startswith("RETRIEVED_CONTEXT>>>"):
                break
            if inside:
                lines.append(line)
        return lines

    def _mock_answer(self, user: str) -> str:
        lines = self._context_lines(user)
        text = " ".join(line for line in lines if line and not line.startswith(("[", "[DOCUMENT]", "[CONTENT]", "[SECTION]", "</")))
        snippet = text.strip()[:400]
        if not snippet:
            return NO_ANSWER_ID
        return f"Berdasarkan konteks [1]: {snippet}"

    def _mock_items(self, user: str) -> Dict[str, Any]:
        lines = [line for line in self._context_lines(user) if "|" in line or line.strip().startswith("-")]
        items = []
        for line in lines[:5]:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 2:
                items.append({"row": parts[0], "value": parts[1]})
        return {"items": items, "not_found": not items}

    def health(self) -> str:
        return "ok"


class LLMClient:
    """Thin OpenAI-compatible chat client with explicit failure mapping."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        # Read per call: the settings screen can change the model of a running process.
        return self._settings.llm_model

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> tuple[str, LLMUsage]:
        import httpx

        settings = self._settings
        url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        # Sampling: sebelumnya dua nilai ini tidak pernah dikirim, jadi endpoint memakai
        # bawaannya. top_p yang lebih rapat memangkas ekor distribusi - sumber kata aneh
        # ("pemb.cgiian", "praktikumaccording": kata terpotong / bocor bahasa lain).
        # Nilai 0 berarti "jangan kirim", supaya operator bisa menyerahkan ke endpoint.
        #
        # PENTING: sebagian gateway menolak parameter yang tidak dikenal dengan 400 (dan ada yang
        # membalas pesan menyesatkan seperti "insufficient credits"). Karena itu parameter
        # opsional dikirim sebagai "percobaan", dan bila ditolak kita ulangi tanpa parameter itu -
        # bukan menggagalkan seluruh permintaan. Kegagalan permanen hanya untuk permintaan dasar.
        optional: Dict[str, Any] = {}
        if settings.llm_top_p and settings.llm_top_p > 0:
            optional["top_p"] = settings.llm_top_p
        if settings.llm_frequency_penalty:
            optional["frequency_penalty"] = settings.llm_frequency_penalty
        if settings.llm_presence_penalty:
            optional["presence_penalty"] = settings.llm_presence_penalty
        if response_format:
            body["response_format"] = response_format

        started = time.perf_counter()
        payload = None
        rejected: List[str] = []
        last_error: Optional[str] = None
        for attempt in range(len(optional) + 1):
            request_body = dict(body)
            request_body.update(optional)
            try:
                response = httpx.post(url, json=request_body, headers=headers, timeout=timeout or settings.llm_timeout)
                response.raise_for_status()
                payload = response.json()
                if rejected:
                    logger.warning(
                        "gateway menolak parameter sampling %s; permintaan dilanjutkan tanpanya",
                        ", ".join(rejected),
                    )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{exc}"
                # 4xx/422 -> parameter opsional mungkin penyebabnya: buang satu, coba lagi.
                # Galat jaringan/5xx langsung dilaporkan (bukan soal parameter).
                if optional and ("400" in last_error or "422" in last_error):
                    dropped = sorted(optional)[-1]
                    optional.pop(dropped, None)
                    rejected.append(dropped)
                    continue
                raise AppError("LLM_FAILED", f"LLM request failed: {exc}") from exc
        if payload is None:
            # Semua percobaan gagal: laporkan galat ASLI terakhir, jangan menyalahkan parameter.
            raise AppError(
                "LLM_FAILED",
                f"LLM request failed: {last_error or 'endpoint menolak setiap permintaan'}",
            )

        latency_ms = (time.perf_counter() - started) * 1000
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("LLM_FAILED", "LLM response did not contain a completion") from exc

        usage_block = payload.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(usage_block.get("prompt_tokens") or 0),
            output_tokens=int(usage_block.get("completion_tokens") or 0),
            latency_ms=round(latency_ms, 2),
            model=payload.get("model") or self.model,
            finish_reason=str(choice.get("finish_reason") or ""),
        )
        return text.strip(), usage

    def health(self) -> str:
        try:
            self.chat([{"role": "user", "content": "ping"}], max_tokens=4, temperature=0.0, timeout=30)
            return "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm health check failed: %s", exc)
            return "error"


def build_llm_client(settings: Settings):
    """``LLM_PROVIDER=mock`` keeps the whole pipeline runnable with zero models."""
    if settings.llm_provider == "mock":
        return MockLLMClient(settings)
    return LLMClient(settings)


class Generator:
    def __init__(self, settings: Settings, client: Optional[LLMClient] = None) -> None:
        self._settings = settings
        self._client = client or build_llm_client(settings)

    @property
    def client(self):
        return self._client

    def rebind_llm_client(self, client) -> None:
        """Swap the client after a runtime settings change (mock <-> real provider)."""
        self._client = client

    @property
    def model(self) -> str:
        return self._client.model

    # ------------------------------------------------------------------ #
    def answer(
        self,
        *,
        query: str,
        context: BuiltContext,
        strict_grounding: bool = True,
        no_candidate_reason: Optional[str] = None,
        history: Optional[Sequence[Dict[str, str]]] = None,
    ) -> GeneratedAnswer:
        if not context.used:
            return GeneratedAnswer(
                answer=NO_ANSWER_ID if self._settings.answer_language == "id" else NO_ANSWER_EN,
                grounded=False,
                usage=LLMUsage(model=self.model),
            )

        user_prompt = f"{context.text}\n\nUser query:\n{query}"
        messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Riwayat percakapan hanya sebagai rujukan "apa yang sedang dibicarakan"; konteks
        # dokumen tetap hanya di pesan terakhir, jadi jawaban tetap bersumber dari konteks.
        for turn in history or []:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])[:2000]})
        messages.append({"role": "user", "content": user_prompt})
        text, usage = self._client.chat(messages)
        if isinstance(context.used, (list, tuple)):
            text = clean_citations(text, len(context.used))
        truncated = self._looks_truncated(text, usage)
        grounded = not truncated and not self._looks_like_refusal(text)

        # Perbaikan teks rusak: model gratis kadang menghasilkan kata terpotong/tercampur
        # ("pemb.cgiian", "hanyaLEMpar", "praktikumaccording"). Faktanya benar, tapi pemakai
        # melihat kata aneh. Bila terdeteksi, minta model menulis ulang jawaban yang SAMA -
        # tanpa menambah/mengurangi fakta - lalu pakai hasil yang lebih bersih.
        corrupted = self._corrupted_words(text, context.text)
        repaired = False
        if corrupted and not truncated and self._settings.llm_repair_attempts > 0:
            cleaned = self._repair_text(text, corrupted)
            if cleaned and not self._corrupted_words(cleaned, context.text):
                logger.info(
                    "jawaban diperbaiki: %d kata rusak (%s) -> bersih",
                    len(corrupted),
                    ", ".join(sorted(corrupted)[:5]),
                )
                text, repaired = cleaned, True

        # Aksara asing yang model selipkan ("dokumen finals完整的"): faktanya benar, tapi pemakai
        # melihat aksara yang tidak bisa dibaca. Hanya dibuang bila TIDAK ada di konteks - kutipan
        # asing yang memang ada di dokumen adalah fakta dan harus utuh.
        text, foreign = self._strip_foreign_script(text, context.text)
        if foreign:
            logger.info(
                "aksara asing dibuang dari jawaban: %s",
                ", ".join(foreign[:5]),
            )

        if truncated:
            logger.warning(
                "jawaban model terpotong (finish_reason=%r, output_tokens=%s, batas=%s)",
                usage.finish_reason,
                usage.output_tokens,
                self._settings.llm_max_tokens,
            )
        elif strict_grounding and not grounded:
            logger.info("strict grounding: model reported insufficient context (%s)", no_candidate_reason or "low_relevance")
        elif corrupted and not repaired:
            logger.warning(
                "jawaban masih memuat %d kata rusak setelah perbaikan: %s",
                len(corrupted),
                ", ".join(sorted(corrupted)[:5]),
            )
        return GeneratedAnswer(
            answer=text,
            grounded=grounded,
            usage=usage,
            raw=text,
            citations_used=extract_citation_numbers(text),
            truncated=truncated,
        )

    # ------------------------------------------------------------------ #
    # Kata umum bahasa Indonesia/Inggris: boleh muncul di jawaban tanpa ada di konteks.
    _COMMON_WORDS = frozenset(
        """
        yang dan atau untuk dari pada dengan ini itu tidak ada adalah akan bila jika karena
        juga saja lebih paling dapat bisa harus oleh dalam ke di se para setiap agar supaya
        sebagai antara tanpa setelah sebelum saat ketika namun tetapi sedangkan serta yaitu
        bahwa hal tersebut berikut jumlah total nilai data tabel kolom baris bagian isi
        catatan contoh berikutnya pertama kedua ketiga akhir awal semua seluruh hanya
        the and for from with this that not are was were will can may must should have has
        table column row data value total number note example section first second third
        """.split()
    )

    def _strip_foreign_script(self, text: str, context_text: str = "") -> tuple:
        """Buang aksara dari tulisan lain yang TIDAK ada di konteks.

        Mengembalikan ``(teks_bersih, daftar_yang_dibuang)``. Perbandingan dengan konteks itu
        intinya: dokumen yang memang berbahasa/beraksara lain tidak boleh dirusak. Yang dibuang
        hanya selipan model - aksara yang muncul di jawaban tetapi tidak ada di sumbernya.
        """
        if not text:
            return text, []
        if not getattr(self._settings, "text_strip_foreign", True):
            return text, []
        tokens = foreign_tokens(text, allowed=[context_text])
        if not tokens:
            return text, []
        return strip_foreign_tokens(text, tokens), tokens

    def _corrupted_words(self, text: str, context_text: str = "") -> set:
        """Kumpulkan kata yang tampak rusak: terpotong, tercampur, atau salah tempel.

        Contoh nyata dari model gratis: ``pemb.cgiian`` (titik di tengah kata),
        ``hanyaLEMpar`` (huruf besar di tengah kata kecil), ``praktikumaccording`` (dua kata
        berbeda bahasa menempel). Ini bukan salah ketik pemakai - teks datang dari model.

        Pemeriksaannya sengaja konservatif. Sinyal utamanya: sebuah kata yang **tidak ada di
        konteks** (dokumen sumber) dan juga bukan kata umum - padahal jawaban seharusnya hanya
        memakai kata dari konteks. Istilah teknis, nama kolom, dan singkatan yang sah tetap
        lolos karena mereka memang hadir di konteks.
        """
        if not text:
            return set()
        context_lower = (context_text or "").lower()
        context_words = set(re.findall(r"[a-z\u00C0-\u024F]{2,}", context_lower))
        broken = set()

        # (a) Kata yang mengandung titik DI TENGAH (mis. "pemb.cgiian"). Titik di akhir
        #     kalimat tidak dihitung: kita hanya melihat "huruf.huruf" tanpa spasi di sekitarnya.
        for token in re.findall(r"[A-Za-z\u00C0-\u024F]{2,}(?:\.[A-Za-z\u00C0-\u024F]{2,})+", text):
            cleaned = token.replace(".", "")
            if cleaned.lower() in self._COMMON_WORDS or cleaned.lower() in context_words:
                continue
            if self._looks_corrupted(cleaned):
                broken.add(token)

        # (b) Kata biasa.
        for token in re.findall(r"[A-Za-z\u00C0-\u024F]{2,}", text):
            lowered = token.lower()
            if lowered in self._COMMON_WORDS or lowered in context_words:
                continue
            if len(lowered) < 5:
                continue
            if self._looks_corrupted(token):
                broken.add(token)

        # (c) Kata yang menempel pada angka tahun ("Laporan Tahunan2025"): huruf kecil langsung
        #     diikuti 4 digit. Kasus nyata dari produksi. Diperiksa hanya bila bentuk itu TIDAK
        #     ada di konteks - istilah sah seperti "SQLite3" atau "PSL2025" tetap lolos karena
        #     memang tertulis begitu di dokumen.
        for token in re.findall(r"[A-Za-z\u00C0-\u024F]{2,}(?:19|20)\d{2}", text):
            if token.lower() in context_words:
                continue
            # Angka di akhir setelah huruf kecil = hampir pasti dua kata yang menempel.
            if re.search(r"[a-z\u00C0-\u024F](?:19|20)\d{2}$", token):
                broken.add(token)
        return broken

    @staticmethod
    def _looks_corrupted(token: str) -> bool:
        """Pola yang hampir pasti rusak (bukan sekadar kata asing yang belum dikenal)."""
        if len(token) < 5:
            return False
        # 1. huruf besar di tengah kata kecil: "hanyaLEMpar", "praktikumAccording".
        #    camelCase yang lazim ("knowledgeBase", "employeeId") dikecualikan.
        if re.search(r"[a-z][A-Z]{2,}", token):
            return True
        if re.search(r"[a-z]{3,}[A-Z][a-z]", token) and not re.fullmatch(r"[a-z]+[A-Z][a-z]+", token):
            return True
        # 2. huruf yang sama tiga kali atau lebih: "pemb.cgiiian", "aaaan".
        if re.search(r"([A-Za-z])\1{2,}", token):
            return True
        # 3. konsonan beruntun sangat panjang: "cgiian" -> "cgii", "praktikumacc" -> "ktkmcc".
        if re.search(r"[bcdfghjklmnpqrstvwxyz]{5,}", token.lower()):
            return True
        # 4. dua kata bahasa berbeda menempel: "praktikumaccording" - ekor kata Inggris yang
        #    menempel pada kata Indonesia tanpa pemisah. Dikenali dari sufiks Inggris khas.
        if re.search(r"[a-z]{4,}(according|because|however|therefore|which|where|about|between)$", token.lower()):
            return True
        # 5. Pola vokal yang tidak mungkin dalam suku kata Indonesia/Inggris: "cgiian" punya
        #    "iia" (dua vokal sama berturut lalu vokal lain). Kata sah seperti "sesuai"
        #    ("uai": tiga vokal BERBEDA) dikecualikan - hanya vokal sama yang berulang ditandai.
        lowered = token.lower()
        if re.search(r"([aeiou])\1[aeiou]", lowered):
            return True
        return False

    def _repair_text(self, text: str, corrupted: set) -> str:
        """Minta model menulis ulang jawaban yang SAMA dengan ejaan bersih.

        Aturannya ketat: jangan menambah, mengurangi, atau mengubah fakta - hanya perbaiki kata
        yang rusak. Jawaban tetap harus dari konteks yang sama (tidak ada panggilan retrieval).
        """
        instruction = (
            "Perbaiki ejaan jawaban berikut. JANGAN menambah, mengurangi, atau mengubah fakta, "
            "angka, nama, atau urutan. Hanya betulkan kata yang terpotong atau tercampur bahasa "
            "lain, dan pastikan setiap kata ditulis utuh dalam bahasa jawaban. "
            "Kembalikan jawaban yang sudah diperbaiki saja, tanpa komentar.\n\n"
            f"Kata yang tampak rusak: {', '.join(sorted(corrupted)[:20])}"
        )
        try:
            cleaned, _ = self._client.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{instruction}\n\nJawaban:\n{text}"},
                ],
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - perbaikan bersifat opsional
            logger.warning("perbaikan jawaban gagal: %s", exc)
            return ""
        candidate = (cleaned or "").strip()
        # Tolak hasil yang jelas merusak: kosong, jauh lebih pendek, atau kehilangan sitasi.
        if len(candidate) < max(40, int(len(text) * 0.6)):
            return ""
        if extract_citation_numbers(text) and not extract_citation_numbers(candidate):
            return ""
        return candidate

    def _looks_truncated(self, text: str, usage: LLMUsage) -> bool:
        """True bila model berhenti karena batas token keluaran.

        Tanpa pemeriksaan ini, jawaban yang terpotong (atau kosong karena seluruh jatah token
        habis) terbaca sebagai "informasi tidak ditemukan" - padahal datanya ada di konteks dan
        yang perlu dinaikkan hanyalah batas token keluaran.
        """

        if usage.finish_reason == "length":
            return True
        limit = int(self._settings.llm_max_tokens or 0)
        if limit and usage.output_tokens >= limit and not text.strip():
            return True
        return False

    # ------------------------------------------------------------------ #
    def summarize(
        self,
        *,
        context: BuiltContext,
        document_name: str = "",
        language: str = "",
        max_tokens: Optional[int] = None,
    ) -> GeneratedAnswer:
        """Ringkas isi satu dokumen dari potongan yang sudah terkumpul.

        Dipakai untuk membuat knowledge turunan: ringkasan disimpan sebagai dokumen tersendiri,
        sehingga pertanyaan "ringkas dokumen X" bisa dijawab dari ringkasannya (satu potongan
        padat) alih-alih menyeret seluruh isi dokumen ke konteks.

        Ringkasan dibuat dari potongan yang BENAR-BENAR ada di indeks (pemanggilnya mengambil
        lewat jalur yang sama dengan konteks tanya-jawab), jadi tidak ada isi yang dikarang dan
        batas tenant tetap dihormati.
        """
        if not context.used:
            return GeneratedAnswer(answer="", grounded=False, usage=LLMUsage(model=self.model))

        instruction = SUMMARY_PROMPT_ID if (language or self._settings.answer_language) == "id" else SUMMARY_PROMPT_EN
        user_prompt = (
            f"{context.text}\n\n"
            f"Nama dokumen: {document_name or '(tanpa nama)'}\n\n"
            "Buat ringkasan dokumen di atas."
        )
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": user_prompt},
        ]
        text, usage = self._client.chat(messages, max_tokens=max_tokens)
        truncated = self._looks_truncated(text, usage)
        if truncated:
            logger.warning(
                "ringkasan terpotong (finish_reason=%r, output_tokens=%s) untuk %s",
                usage.finish_reason,
                usage.output_tokens,
                document_name,
            )
        return GeneratedAnswer(
            answer=(text or "").strip(),
            grounded=bool(text and text.strip()) and not truncated,
            usage=usage,
            raw=text or "",
            citations_used=extract_citation_numbers(text or ""),
            truncated=truncated,
        )

    # ------------------------------------------------------------------ #
    def extract(
        self,
        *,
        query: str,
        context: BuiltContext,
        output_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not context.used:
            return {"items": [], "not_found": True}

        instruction = (
            "Extract the requested data from the context.\n"
            f"JSON schema to satisfy:\n{json.dumps(output_schema or {'type': 'object'}, ensure_ascii=False)}\n\n"
            f"User request:\n{query}"
        )
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"{context.text}\n\n{instruction}"},
        ]
        text, usage = self._client.chat(
            messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        payload = _loads_tolerant(text)
        if payload is None:
            # one repair attempt: ask for strict JSON only
            repair, _ = self._client.chat(
                [
                    {"role": "system", "content": "Return valid JSON only. No prose, no markdown fences."},
                    {"role": "user", "content": f"Convert this into valid JSON:\n{text}"},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            payload = _loads_tolerant(repair) or {"items": [], "not_found": True}
        if isinstance(payload, list):
            payload = {"items": payload}
        payload.setdefault("usage", {})
        payload["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "model": usage.model,
        }
        return payload

    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_refusal(text: str) -> bool:
        """Apakah jawaban model = "tidak ditemukan" (bukan jawaban yang kebetulan memuat frasa itu)?

        * Kalimat penolakan baku di awal jawaban = penolakan, walau model menempelkan sitasi.
        * Penanda "meta" (tidak ditemukan / tidak ada informasi / tidak memuat informasi ...) pada
          jawaban pendek = penolakan, juga walau ada sitasi: model sering menolak sambil menunjuk
          dokumen yang ia baca ("Dokumen [1] tidak memuat informasi tentang gaji").
        * "Tidak terdapat biaya pendaftaran [1]" BUKAN penolakan: itu fakta dari dokumen (nol),
          jadi frasa faktual seperti "tidak terdapat" hanya dihitung penolakan bila tanpa sitasi.
        """
        if not text or not text.strip():
            return True
        lowered = " ".join(text.lower().split())
        for sentence in (NO_ANSWER_ID, NO_ANSWER_EN):
            if lowered.startswith(sentence.lower().rstrip(".")):
                return True
        cited = bool(extract_citation_numbers(text))
        if any(marker in lowered for marker in META_REFUSAL_MARKERS) and len(lowered) < 250:
            return True
        if not cited and any(marker in lowered for marker in NOT_FOUND_MARKERS):
            return len(lowered) < 300
        return False

    def health(self) -> str:
        return self._client.health()


_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


def extract_citation_numbers(text: str) -> List[int]:
    return sorted({int(match) for match in _CITATION_RE.findall(text or "")})


def clean_citations(text: str, count: int) -> str:
    """Buang sitasi ``[n]`` yang menunjuk blok konteks yang tidak ada (n < 1 atau n > count).

    Sitasi palsu lebih buruk daripada tanpa sitasi: pembaca mengira klaimnya punya sumber.
    """
    if not text or count <= 0:
        return text

    def keep(match: "re.Match[str]") -> str:
        number = int(match.group(1))
        return match.group(0) if 1 <= number <= count else ""

    return _CITATION_RE.sub(keep, text)


def _loads_tolerant(text: str) -> Optional[Any]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
