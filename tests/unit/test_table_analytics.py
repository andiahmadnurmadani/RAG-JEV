"""Uji perhitungan tabel: ekstraksi terstruktur, validasi rencana, dan aritmetika nyata.

Prinsip yang diuji: angka yang keluar HARUS berasal dari baris data, dan rencana yang
menyebut kolom/operasi tidak valid harus DITOLAK (bukan ditebak).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.parsing.tables import TableData, extract_tables, supports_tables
from app.tables import analytics
from app.tables.analytics import PlanError, TablePlan, format_number, parse_number
from app.tables.store import TableStore


def _zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _sales_xlsx() -> bytes:
    """xlsx dengan teks di sharedStrings dan angka sebagai nilai langsung (<v>)."""

    shared = (
        '<?xml version="1.0"?><sst xmlns="x">'
        "<si><t>Produk</t></si><si><t>Kategori</t></si><si><t>Jumlah</t></si><si><t>Harga</t></si>"
        "<si><t>Ayam Geprek</t></si><si><t>Makanan</t></si><si><t>Es Teh</t></si><si><t>Minuman</t></si>"
        "</sst>"
    )
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>',
        # sel B2 sengaja KOSONG: posisi kolom harus tetap sejajar
        '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="C2"><v>25</v></c><c r="D2"><v>25000</v></c></row>',
        '<row r="3"><c r="A3" t="s"><v>6</v></c><c r="B3" t="s"><v>7</v></c>'
        '<c r="C3"><v>42</v></c><c r="D3"><v>8000</v></c></row>',
        '<row r="4"><c r="A4" t="s"><v>4</v></c><c r="B4" t="s"><v>5</v></c>'
        '<c r="C4"><v>18</v></c><c r="D4"><v>25000</v></c></row>',
    ]
    sheet = '<?xml version="1.0"?><worksheet><sheetData>' + "".join(rows) + "</sheetData></worksheet>"
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


class StubClient:
    """Klien LLM palsu: hanya mengembalikan JSON rencana yang sudah ditentukan."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list = []

    def chat(self, messages, **kwargs):  # noqa: AN001 - tanda tangan mengikuti LLMClient
        self.calls.append(messages)
        return self.reply, None


# ---------------------------------------------------------------------- #
# Angka
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("18000", 18000.0),
        ("18.000", 18000.0),
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("Rp 25.000", 25000.0),
        ("(1.500)", -1500.0),
        ("-12", -12.0),
        ("15%", 15.0),
        ("2026-08-02", None),
        ("PRD-001", None),
        ("", None),
        ("tidak ada data", None),
    ],
)
def test_parse_number_is_strict_about_what_counts_as_a_number(text, expected):
    assert parse_number(text) == expected


def test_currency_codes_are_numbers_but_product_codes_are_not():
    assert analytics.is_numeric_cell("Rp 1.000")
    assert analytics.is_numeric_cell("25000")
    assert not analytics.is_numeric_cell("PRD-001")
    assert not analytics.is_numeric_cell("2026-08-02")


def test_format_number_uses_indonesian_thousands_separator():
    assert format_number(14830000.0) == "14.830.000"
    assert format_number(184.0) == "184"
    assert format_number(1234.5) == "1.234,5"


# ---------------------------------------------------------------------- #
# Ekstraksi
# ---------------------------------------------------------------------- #


def test_supports_tables_only_for_tabular_suffixes():
    assert supports_tables("penjualan.xlsx")
    assert supports_tables("data.CSV")
    assert not supports_tables("sop.pdf")
    assert not supports_tables("catatan.txt")


def test_xlsx_extraction_keeps_columns_aligned_when_a_cell_is_empty():
    tables = extract_tables(_sales_xlsx(), "penjualan.xlsx")
    assert len(tables) == 1
    table = tables[0]
    assert table.sheet == "Penjualan"
    assert table.headers == ["Produk", "Kategori", "Jumlah", "Harga"]
    assert table.row_count == 3
    assert table.rows[0] == ["Ayam Geprek", "", "25", "25000"]
    assert table.rows[1] == ["Es Teh", "Minuman", "42", "8000"]


def test_csv_extraction_sniffs_semicolon_and_bom():
    content = "\ufeffProduk;Jumlah\nAyam Geprek;184\nEs Teh;95\n".encode("utf-8")
    tables = extract_tables(content, "penjualan.csv")
    assert tables[0].headers == ["Produk", "Jumlah"]
    assert tables[0].rows == [["Ayam Geprek", "184"], ["Es Teh", "95"]]


def test_non_table_extension_yields_no_tables():
    assert extract_tables(b"bukan tabel", "sop.docx") == []


def test_table_schema_marks_numeric_columns_and_samples():
    table = TableData(sheet="S", headers=["Produk", "Jumlah"], rows=[["Ayam", "184"], ["Es Teh", "95"]])
    schema = table.schema()
    columns = {column["name"]: column for column in schema["columns"]}
    assert columns["Jumlah"]["numeric"] is True
    assert columns["Produk"]["numeric"] is False
    assert columns["Produk"]["samples"] == ["Ayam", "Es Teh"]


# ---------------------------------------------------------------------- #
# Rencana
# ---------------------------------------------------------------------- #


def _store(tmp_path) -> TableStore:
    store = TableStore(str(tmp_path / "tables.sqlite"))
    store.replace_document(
        organization_id="org_a",
        knowledge_base_id="kb",
        document_id="doc_x",
        document_name="penjualan_agustus.xlsx",
        tables=[
            TableData(
                sheet="Penjualan",
                headers=["Produk", "Kategori", "Jumlah"],
                rows=[
                    ["Ayam Geprek", "Makanan", "25"],
                    ["Es Teh", "Minuman", "42"],
                    ["Ayam Geprek", "Makanan", "18"],
                    ["Es Teh", "Minuman", "8"],
                    ["Kopi", "Minuman", "30"],
                ],
            )
        ],
    )
    return store


def test_build_plan_accepts_json_wrapped_in_a_code_fence(tmp_path):
    store = _store(tmp_path)
    table = store.list_tables(organization_id="org_a")[0]
    client = StubClient("```json\n" + json.dumps({"table": 0, "operation": "top_n", "metric": "Jumlah", "group_by": "Produk", "order": "desc", "limit": 2}) + "\n```")
    plan = analytics.build_plan(client, query="produk apa yang paling laku?", tables=[table])
    assert plan is not None
    assert (plan.operation, plan.metric, plan.group_by) == ("top_n", "Jumlah", "Produk")
    assert plan.limit == 2


def test_build_plan_returns_none_for_non_computational_questions(tmp_path):
    store = _store(tmp_path)
    table = store.list_tables(organization_id="org_a")[0]
    assert analytics.build_plan(StubClient('{"operation": "none"}'), query="apa itu cuti?", tables=[table]) is None
    assert analytics.build_plan(StubClient("bukan json"), query="apa itu cuti?", tables=[table]) is None
    assert analytics.build_plan(StubClient('{"operation": "drop_table"}'), query="x", tables=[table]) is None


def test_unknown_column_is_rejected_with_the_available_columns(tmp_path):
    store = _store(tmp_path)
    table = store.list_tables(organization_id="org_a")[0]
    rows = store.load_rows(table.table_id)
    plan = TablePlan(operation="sum", metric="Total", table_index=0)
    with pytest.raises(PlanError) as excinfo:
        analytics.validate_plan(plan, table, rows)
    assert "Total" in excinfo.value.reason
    assert excinfo.value.available_columns == ["Produk", "Kategori", "Jumlah"]


def test_numeric_operation_on_a_text_column_is_rejected(tmp_path):
    store = _store(tmp_path)
    table = store.list_tables(organization_id="org_a")[0]
    rows = store.load_rows(table.table_id)
    with pytest.raises(PlanError) as excinfo:
        analytics.validate_plan(TablePlan(operation="sum", metric="Produk"), table, rows)
    assert "bukan kolom angka" in excinfo.value.reason


def test_column_lookup_ignores_case_and_punctuation():
    assert analytics.resolve_column("jumlah", ["Jumlah", "Harga Satuan"]) == "Jumlah"
    assert analytics.resolve_column("harga_satuan", ["Jumlah", "Harga Satuan"]) == "Harga Satuan"
    assert analytics.resolve_column("kolom_hantu", ["Jumlah"]) is None


# ---------------------------------------------------------------------- #
# Eksekusi
# ---------------------------------------------------------------------- #


def _plan(**kwargs) -> TablePlan:
    return TablePlan(**{"operation": "sum", "metric": "Jumlah", **kwargs})


def test_sum_counts_only_rows_that_really_are_numbers(tmp_path):
    table = TableData(
        sheet="S",
        headers=["Produk", "Jumlah"],
        rows=[["A", "10"], ["B", "20"], ["C", "tidak ada"], ["D", "30,5"]],
    )
    facts = analytics.execute_plan(_plan(), _table_store_row(table), table.rows)
    assert facts["result"][0]["Jumlah"] == 60.5
    assert facts["rows_matched"] == 4
    assert facts["rows_skipped"] == 1
    assert "1 nilai tidak terbaca" in facts["explanation"]


def _table_store_row(table: TableData):
    from app.tables.store import StoredTable

    return StoredTable(
        table_id=1,
        organization_id="org_a",
        knowledge_base_id="kb",
        document_id="doc_x",
        document_name="penjualan.xlsx",
        sheet=table.sheet,
        headers=list(table.headers),
        row_count=len(table.rows),
    )


def test_top_n_groups_and_sorts_descending():
    table = TableData(
        sheet="Penjualan",
        headers=["Produk", "Jumlah"],
        rows=[["Ayam", "25"], ["Es Teh", "42"], ["Ayam", "18"], ["Es Teh", "8"], ["Kopi", "30"]],
    )
    plan = TablePlan(operation="top_n", metric="Jumlah", group_by="Produk", order="desc", limit=2)
    facts = analytics.execute_plan(plan, _table_store_row(table), table.rows)
    assert [row["Produk"] for row in facts["result"]] == ["Es Teh", "Ayam"]
    assert facts["result"][0]["Jumlah"] == 50
    assert facts["result"][0]["teks"] == "50"
    assert facts["rows_matched"] == 5


def test_where_filter_and_average():
    table = TableData(
        sheet="Penjualan",
        headers=["Produk", "Kategori", "Jumlah"],
        rows=[["Ayam", "Makanan", "25"], ["Es Teh", "Minuman", "42"], ["Kopi", "Minuman", "30"]],
    )
    plan = TablePlan(
        operation="avg",
        metric="Jumlah",
        where={"column": "Kategori", "operator": "=", "value": "Minuman"},
    )
    facts = analytics.execute_plan(plan, _table_store_row(table), table.rows)
    assert facts["result"][0]["Jumlah"] == 36.0
    assert facts["rows_matched"] == 2


def test_count_and_list_rows_are_supported():
    table = TableData(sheet="S", headers=["Produk", "Jumlah"], rows=[["A", "1"], ["B", "2"], ["C", "3"]])
    counted = analytics.execute_plan(TablePlan(operation="count"), _table_store_row(table), table.rows)
    assert counted["result"][0]["jumlah_baris"] == 3
    listed = analytics.execute_plan(
        TablePlan(operation="list_rows", limit=2), _table_store_row(table), table.rows
    )
    assert listed["result"] == [{"Produk": "A", "Jumlah": "1"}, {"Produk": "B", "Jumlah": "2"}]


def test_facts_block_shows_rendered_numbers_for_the_llm(tmp_path):
    store = _store(tmp_path)
    table = store.list_tables(organization_id="org_a")[0]
    rows = store.load_rows(table.table_id)
    facts = analytics.execute_plan(
        TablePlan(operation="sum", metric="Jumlah"), table, rows
    )
    block = analytics.facts_block(facts)
    assert "123" in block  # 25 + 42 + 18 + 8 + 30
    assert "5 dari 5" in block
    assert "penjualan_agustus.xlsx" in block


def test_narration_falls_back_to_the_numbers_when_the_llm_fails():
    class Broken:
        def chat(self, messages, **kwargs):
            raise RuntimeError("llm mati")

    facts = {
        "explanation": "menjumlahkan kolom 'Jumlah'",
        "scope": "penjualan.xlsx / lembar 'Penjualan'",
        "result": [{"Jumlah": 184.0, "teks": "184"}],
    }
    text, usage = analytics.narrate(Broken(), query="total?", facts=facts)
    assert usage is None
    assert "184" in text


# ---------------------------------------------------------------------- #
# Store
# ---------------------------------------------------------------------- #


def test_store_replaces_document_atomically_and_keeps_tenants_apart(tmp_path):
    store = _store(tmp_path)
    store.replace_document(
        organization_id="org_a",
        knowledge_base_id="kb",
        document_id="doc_x",
        document_name="baru.xlsx",
        tables=[TableData(sheet="S", headers=["A"], rows=[["1"]])],
    )
    assert len(store.list_tables(organization_id="org_a")) == 1  # tidak menumpuk
    assert store.list_tables(organization_id="org_b") == []
    assert store.load_rows(store.list_tables(organization_id="org_a")[0].table_id) == [["1"]]
    assert store.delete_document(organization_id="org_a", document_id="doc_x") == 1
    assert store.stats()["rows"] == 0


def test_store_requires_a_tenant_id(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(Exception):
        store.list_tables(organization_id="")
