"""Uji end-to-end fitur analitik tabel: unggah xlsx lalu TANYA seperti pengguna.

Yang dibuktikan bukan "endpoint mengembalikan 200", melainkan angka yang keluar sama dengan
data di dalam berkas (dihitung ulang di test dari baris yang sama), dan bahwa pertanyaan yang
meminta kolom tidak ada dijawab jujur -- bukan dikarang.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile

from app.rag.generator import LLMUsage

from tests.conftest import TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job

KB = "kb_penjualan"

# Kebenaran acuan (dihitung dari tabel di bawah):
#   Ayam Geprek: 25 + 18 = 43
#   Es Teh     : 42 + 8  = 50   <- paling laku
#   Kopi       : 30
#   total kolom Total: 1075000 + 336000 + 150000 + 200000 + 450000
ROWS = [
    ["Ayam Geprek", "Makanan", "25", "25000", "625000"],
    ["Es Teh", "Minuman", "42", "8000", "336000"],
    ["Ayam Geprek", "Makanan", "18", "25000", "450000"],
    ["Es Teh", "Minuman", "8", "8000", "64000"],
    ["Kopi", "Minuman", "30", "15000", "450000"],
]
HEADERS = ["Produk", "Kategori", "Jumlah", "Harga Satuan", "Total"]
TOP_SELLER = "Es Teh"
TOP_UNITS = 50
TOTAL_RUPIAH = 1925000  # 625000 + 336000 + 450000 + 64000 + 450000


def _zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _sales_xlsx_bytes() -> bytes:
    shared_items = HEADERS + [cell for row in ROWS for cell in row[:2]]
    shared = '<?xml version="1.0"?><sst xmlns="x">' + "".join(
        f"<si><t>{item}</t></si>" for item in shared_items
    ) + "</sst>"
    index = {item: position for position, item in enumerate(shared_items)}

    rows_xml = []
    header_cells = "".join(
        f'<c r="{chr(65 + position)}1" t="s"><v>{index[header]}</v></c>'
        for position, header in enumerate(HEADERS)
    )
    rows_xml.append(f'<row r="1">{header_cells}</row>')
    for row_number, row in enumerate(ROWS, start=2):
        cells = []
        for position, cell in enumerate(row):
            letter = chr(65 + position)
            if position < 2:
                cells.append(f'<c r="{letter}{row_number}" t="s"><v>{index[cell]}</v></c>')
            else:
                cells.append(f'<c r="{letter}{row_number}"><v>{cell}</v></c>')
        rows_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    sheet = '<?xml version="1.0"?><worksheet><sheetData>' + "".join(rows_xml) + "</sheetData></worksheet>"
    workbook = (
        '<?xml version="1.0"?><workbook><sheets>'
        '<sheet name="Penjualan" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0"?><Relationships>'
        '<Relationship Target="/xl/worksheets/sheet1.xml" Id="rId1"/></Relationships>'
    )
    return _zip(
        {
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/sharedStrings.xml": shared,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


class PlanStubLLM:
    """LLM palsu: mengembalikan rencana yang diminta test, lalu narasi dari fakta."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.narration_calls: list = []

    @property
    def model(self) -> str:
        return "stub-table-llm"

    def chat(self, messages, **kwargs):
        system = messages[0]["content"] if messages else ""
        usage = LLMUsage(input_tokens=10, output_tokens=5, latency_ms=1.0, model="stub-table-llm")
        if "perencana kueri data" in system:
            return json.dumps(self.plan), usage
        self.narration_calls.append(messages[-1]["content"])
        return "Jawaban dari perhitungan data riil.", usage


def _index_sheet(client, document_id: str = "doc_penjualan") -> None:
    payload = {
        "document_id": document_id,
        "knowledge_base_id": KB,
        "document_name": "penjualan_agustus_2026.xlsx",
        "content_base64": base64.b64encode(_sales_xlsx_bytes()).decode(),
    }
    response = client.post("/api/v1/knowledge/index", json=payload, headers=auth(TENANT_A_KEY))
    assert response.status_code == 202, response.text
    assert wait_for_job(client, document_id)["status"] == "completed"


def _use_plan(client, plan: dict) -> PlanStubLLM:
    stub = PlanStubLLM(plan)
    client.app.state.services.generator.rebind_llm_client(stub)
    return stub


def test_indexing_a_spreadsheet_stores_rows_for_computation(client):
    _index_sheet(client)
    response = client.get("/api/v1/tables", params={"knowledge_base_id": KB}, headers=auth(TENANT_A_KEY))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    table = data["tables"][0]
    assert table["headers"] == HEADERS
    assert table["row_count"] == len(ROWS)
    assert table["sheet"] == "Penjualan"


def test_top_seller_question_is_computed_from_the_rows(client):
    _index_sheet(client)
    _use_plan(client, {"table": 0, "operation": "top_n", "metric": "Jumlah", "group_by": "Produk",
                       "order": "desc", "limit": 3})
    response = client.post(
        "/api/v1/query",
        json={"query": "produk apa yang paling laku?", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    computed = data["computed"]
    assert computed["operation"] == "top_n"
    assert computed["rows_matched"] == len(ROWS)  # dihitung dari SELURUH baris, bukan potongan
    assert computed["result"][0]["Produk"] == TOP_SELLER
    assert computed["result"][0]["Jumlah"] == TOP_UNITS
    assert data["grounded"] is True
    assert data["usage"]["computed_rows"] == len(ROWS)


def test_total_question_sums_the_real_column(client):
    _index_sheet(client)
    _use_plan(client, {"table": 0, "operation": "sum", "metric": "Total"})
    response = client.post(
        "/api/v1/query",
        json={"query": "berapa total penjualan seluruhnya?", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    computed = response.json()["data"]["computed"]
    assert computed["result"][0]["Total"] == TOTAL_RUPIAH
    assert computed["result"][0]["teks"] == "1.925.000"
    assert computed["rows_skipped"] == 0


def test_filtered_average_only_uses_matching_rows(client):
    _index_sheet(client)
    _use_plan(client, {"table": 0, "operation": "avg", "metric": "Jumlah",
                       "where": {"column": "Kategori", "operator": "=", "value": "Makanan"}})
    response = client.post(
        "/api/v1/query",
        json={"query": "rata-rata jumlah pembelian kategori makanan?", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    computed = response.json()["data"]["computed"]
    assert computed["rows_matched"] == 2
    assert computed["result"][0]["Jumlah"] == 21.5  # (25 + 18) / 2


def test_asking_for_a_column_that_does_not_exist_is_refused_honestly(client):
    _index_sheet(client)
    _use_plan(client, {"table": 0, "operation": "sum", "metric": "Diskon"})
    response = client.post(
        "/api/v1/query",
        json={"query": "berapa total kolom diskon?", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    data = response.json()["data"]
    assert data["computed"] is None  # tidak ada angka karangan
    # alasan penolakan wajib terlihat oleh klien, apa pun hasil jalur teks
    assert data["table_note"] is not None
    assert "Diskon" in data["table_note"]
    assert "Produk" in data["table_note"]  # kolom yang benar-benar ada disebutkan


def test_analytics_can_be_turned_off_per_request(client):
    _index_sheet(client)
    _use_plan(client, {"table": 0, "operation": "top_n", "metric": "Jumlah", "group_by": "Produk"})
    response = client.post(
        "/api/v1/query",
        json={
            "query": "produk apa yang paling laku?",
            "knowledge_base_id": KB,
            "options": {"table_analytics": False},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.json()["data"]["computed"] is None


def test_tables_are_not_visible_to_another_organization(client):
    _index_sheet(client)
    other = client.get("/api/v1/tables", headers=auth(TENANT_B_KEY))
    assert other.status_code == 200
    assert other.json()["data"]["count"] == 0
    # dan pertanyaan agregat dari tenant lain tidak mendapat hasil hitung
    _use_plan(client, {"table": 0, "operation": "top_n", "metric": "Jumlah", "group_by": "Produk"})
    response = client.post(
        "/api/v1/query",
        json={"query": "produk apa yang paling laku?", "knowledge_base_id": KB},
        headers=auth(TENANT_B_KEY),
    )
    assert response.json()["data"]["computed"] is None


def test_deleting_the_document_also_drops_its_rows(client):
    _index_sheet(client)
    assert client.delete("/api/v1/knowledge/doc_penjualan", headers=auth(TENANT_A_KEY)).json()["data"][
        "deleted_tables"
    ] == 1
    remaining = client.get("/api/v1/tables", headers=auth(TENANT_A_KEY)).json()["data"]
    assert remaining["count"] == 0


def test_non_tabular_documents_do_not_create_tables(client):
    client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_sop",
            "knowledge_base_id": KB,
            "document_name": "sop.txt",
            "text": "SOP cuti tahunan dua belas hari kerja untuk karyawan tetap.",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert wait_for_job(client, "doc_sop")["status"] == "completed"
    assert client.get("/api/v1/tables", headers=auth(TENANT_A_KEY)).json()["data"]["count"] == 0
