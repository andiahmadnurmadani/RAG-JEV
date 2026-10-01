"""Beban uji: berapa banyak knowledge & organisasi yang masih sehat di instance ini.

Menjalankan uji ini **tidak menyentuh data/live**: jalankan instance terpisah dengan direktori
sendiri (lihat contoh perintah di docstring bawah), supaya dokumen contoh di UI tidak tercampur.

Yang diukur:
  1. latensi indeks per dokumen saat satu knowledge base tumbuh (mengekspos biaya tulis ulang
     BM25/`jobs.json` yang tumbuh linear terhadap isi KB);
  2. latensi kueri (p50/p95) pada scope besar;
  3. efek banyak scope (org x KB) terhadap memori proses dan latensi kueri bersamaan;
  4. ukuran berkas penyimpanan (sparse JSON, jobs.json, qdrant) setelah N dokumen;
  5. latensi `GET /knowledge` (daftar dokumen) dan `/ready`.

Contoh:

    # 1) kunci uji untuk banyak organisasi
    python scripts/load_probe.py --keys-out data/tmp/load-keys.json --orgs 20

    # 2) instance terpisah (port 8101, penyimpanan sendiri)
    APP_PORT=8101 QDRANT_LOCAL_PATH=E:/rag-service/data/load/qdrant \
    QDRANT_COLLECTION=load_chunks SPARSE_DIR=E:/rag-service/data/load/sparse \
    JOB_STORE_PATH=E:/rag-service/data/load/jobs.json STORAGE_DIR=E:/rag-service/data/load/storage \
    EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none LLM_PROVIDER=mock JEV_ENABLED=false \
    API_KEYS_FILE=data/tmp/load-keys.json .venv/Scripts/python.exe -m app.main

    # 3) jalankan beban
    python scripts/load_probe.py --base http://127.0.0.1:8101 --keys data/tmp/load-keys.json \
        --pid <PID server> --report data/tmp/load-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

WORDS = (
    "kebijakan cuti tahunan karyawan tetap kuota hari kerja pengajuan atasan persetujuan "
    "lembur upah laporan bulanan pemeliharaan server jadwal piket keamanan prosedur backup "
    "anggaran pengadaan vendor kontrak evaluasi kinerja pelatihan sertifikasi dokumentasi "
    "insiden pemulihan audit internal risiko kontrol aset inventaris gudang pengiriman "
    "pelanggan faktur pembayaran pajak rekonsiliasi neraca laba rugi arus kas"
).split()


def synth_document(org: str, kb: str, index: int, chars: int = 2600) -> str:
    """Teks deterministik tapi unik per dokumen (ada token pembeda untuk pengukuran)."""
    rng = (hash((org, kb, index)) & 0xFFFF) + 1
    out: List[str] = [f"Dokumen {index} untuk {org} pada {kb}. Kode unik DOC{index:05d}{rng:04d}."]
    total = 0
    step = 0
    while total < chars:
        word = WORDS[(rng * (step + 7) + step * 13) % len(WORDS)]
        sentence = (
            f"Bagian {step + 1}: ketentuan {word} diatur dalam pasal {((step + rng) % 40) + 1} "
            f"dan berlaku untuk unit kerja terkait dengan nomor rujukan {org}-{kb}-{step:03d}."
        )
        out.append(sentence)
        total += len(sentence)
        step += 1
    return "\n\n".join(out)


def call(base: str, method: str, path: str, key: str, body: dict | None = None, timeout: float = 180.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + key)
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode() or "{}")
        return response.status, payload, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw[:200]}
        return error.code, payload, (time.perf_counter() - started) * 1000


def write_keys(path: Path, orgs: int, kbs: int) -> Dict[str, dict]:
    entries: Dict[str, dict] = {}
    for org_index in range(orgs):
        org = f"org_{org_index:03d}"
        entries[f"load-key-{org}"] = {
            "user_id": f"user_{org_index:03d}",
            "organization_id": org,
            "application_id": "load_probe",
            "permissions": ["read", "write", "admin"],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"kunci uji ditulis: {path} ({len(entries)} organisasi)")
    return entries


def rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        import psutil

        return round(psutil.Process(pid).memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return None


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return round(total / (1024 * 1024), 2)


def percentile(values: List[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[position], 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8101")
    parser.add_argument("--keys", default="data/tmp/load-keys.json")
    parser.add_argument("--keys-out", default="")
    parser.add_argument("--orgs", type=int, default=1)
    parser.add_argument("--kbs", type=int, default=1)
    parser.add_argument("--docs", type=int, default=200, help="dokumen per knowledge base (mode sekuensial)")
    parser.add_argument("--docs-per-scope", type=int, default=5)
    parser.add_argument("--pid", type=int, default=0, help="PID proses server untuk membaca RSS")
    parser.add_argument("--data-dir", default="E:/rag-service/data/load")
    parser.add_argument("--report", default="data/tmp/load-report.json")
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()

    if args.keys_out:
        write_keys(Path(args.keys_out), args.orgs, args.kbs)
        if not args.skip_index and args.docs == 0:
            return 0

    keys = json.loads(Path(args.keys).read_text(encoding="utf-8"))
    orgs = sorted({entry["organization_id"] for entry in keys.values()})
    base = args.base.rstrip("/")
    data_dir = Path(args.data_dir)
    report: Dict[str, object] = {"base": base, "orgs": len(orgs), "kbs": args.kbs}

    status, ready, _ = call(base, "GET", "/api/v1/ready", next(iter(keys)))
    report["ready"] = {"status": ready.get("data", {}).get("status"), "detail": ready.get("data", {}).get("detail", {})}
    print(f"instance: {base} status={(ready.get('data') or {}).get('status')} orgs={len(orgs)}")

    # ---------------------------------------------------------------- 1) KB tumbuh
    growth: List[Dict[str, float]] = []
    if not args.skip_index:
        org = orgs[0]
        key = next(k for k, v in keys.items() if v["organization_id"] == org)
        kb = "kb_growth"
        for index in range(1, args.docs + 1):
            document_id = f"doc_{index:05d}"
            payload = {
                "document_id": document_id,
                "knowledge_base_id": kb,
                "document_name": f"catatan_{index:05d}.txt",
                "text": synth_document(org, kb, index),
            }
            started = time.perf_counter()
            status, response, _ = call(base, "POST", "/api/v1/knowledge/index", key, payload)
            if status != 202:
                print(f"  GAGAL unggah {document_id}: {status} {response.get('error')}")
                break
            job = {}
            for _ in range(240):
                _, listing, _ = call(base, "GET", f"/api/v1/knowledge/{document_id}", key)
                job = listing.get("data") or {}
                if job.get("status") in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            wall_ms = (time.perf_counter() - started) * 1000
            row = {
                "doc": index,
                "wall_ms": round(wall_ms, 1),
                "job_ms": float(job.get("duration_ms") or 0),
                "chunks": int(job.get("chunks") or 0),
                "sparse_mb": dir_size_mb(data_dir / "sparse"),
                "jobs_kb": round((data_dir / "jobs.json").stat().st_size / 1024, 1) if (data_dir / "jobs.json").exists() else 0.0,
                "rss_mb": rss_mb(args.pid) or 0.0,
            }
            growth.append(row)
            if index in {1, 5, 10, 25, 50, 100, 150, 200, 300, 400, 500} or index == args.docs:
                print(
                    f"  dok {index:>4}  wall={row['wall_ms']:>8.1f} ms  job={row['job_ms']:>7.1f} ms  "
                    f"chunks={row['chunks']:>2}  sparse={row['sparse_mb']:>6.2f} MB  jobs={row['jobs_kb']:>7.1f} KB  rss={row['rss_mb']:>7.1f} MB"
                )
    report["growth"] = growth

    # ---------------------------------------------------------------- 2) banyak org x KB
    if args.kbs > 1 or len(orgs) > 1:
        print("menyebar dokumen ke organisasi/knowledge base lain...")
        t0 = time.perf_counter()
        total = 0
        for org in orgs:
            key = next(k for k, v in keys.items() if v["organization_id"] == org)
            for kb_index in range(1, args.kbs + 1):
                kb = f"kb_{kb_index:02d}"
                for doc_index in range(1, args.docs_per_scope + 1):
                    document_id = f"doc_{kb_index:02d}_{doc_index:03d}"
                    call(
                        base,
                        "POST",
                        "/api/v1/knowledge/index",
                        key,
                        {
                            "document_id": document_id,
                            "knowledge_base_id": kb,
                            "document_name": f"{kb}_{doc_index}.txt",
                            "text": synth_document(org, kb, doc_index),
                        },
                    )
                    total += 1
        # tunggu semua selesai (job store bersifat global)
        deadline = time.time() + 300
        while time.time() < deadline:
            _, listing, _ = call(base, "GET", "/api/v1/knowledge?limit=500", next(iter(keys)))
            documents = (listing.get("data") or {}).get("documents") or []
            pending = [d for d in documents if d.get("status") in {"queued", "processing"}]
            if not pending:
                break
            time.sleep(1)
        report["fanout"] = {
            "documents_uploaded": total,
            "seconds": round(time.perf_counter() - t0, 1),
            "scopes": len(orgs) * args.kbs,
            "rss_mb": rss_mb(args.pid),
            "sparse_mb": dir_size_mb(data_dir / "sparse"),
            "sparse_files": len(list((data_dir / "sparse").glob("*.json"))) if (data_dir / "sparse").exists() else 0,
            "jobs_kb": round((data_dir / "jobs.json").stat().st_size / 1024, 1) if (data_dir / "jobs.json").exists() else 0.0,
        }
        print(f"  selesai: {total} dokumen, {report['fanout']['seconds']} s, "
              f"scope={report['fanout']['scopes']}, sparse={report['fanout']['sparse_mb']} MB, rss={report['fanout']['rss_mb']} MB")

    # ---------------------------------------------------------------- 3) kueri
    print("latensi kueri...")
    latencies: List[float] = []
    for org in orgs[: min(len(orgs), 5)]:
        key = next(k for k, v in keys.items() if v["organization_id"] == org)
        for kb in ["kb_growth"] + [f"kb_{i:02d}" for i in range(1, min(args.kbs, 3) + 1)]:
            for _ in range(4):
                _, _, ms = call(
                    base, "POST", "/api/v1/search", key,
                    {"query": "ketentuan cuti tahunan", "knowledge_base_id": kb, "options": {"top_k": 5, "use_hybrid": True}},
                )
                latencies.append(ms)
    report["query_latency_ms"] = {
        "count": len(latencies),
        "p50": percentile(latencies, 0.50),
        "p95": percentile(latencies, 0.95),
        "max": round(max(latencies), 1) if latencies else 0.0,
    }
    print(f"  kueri n={len(latencies)} p50={report['query_latency_ms']['p50']} ms "
          f"p95={report['query_latency_ms']['p95']} ms max={report['query_latency_ms']['max']} ms")

    # ---------------------------------------------------------------- 4) kueri bersamaan
    print("kueri bersamaan dari beberapa organisasi (mengekspos lock sparse global)...")
    concurrent: List[float] = []
    errors: List[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        org = orgs[index % len(orgs)]
        key = next(k for k, v in keys.items() if v["organization_id"] == org)
        for _ in range(5):
            try:
                status, _, ms = call(
                    base, "POST", "/api/v1/search", key,
                    {"query": "prosedur backup dan pemulihan", "knowledge_base_id": "kb_growth",
                     "options": {"top_k": 5, "use_hybrid": True}},
                )
                if status != 200:
                    with lock:
                        errors.append(f"{status}")
                with lock:
                    concurrent.append(ms)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(str(exc)[:80])

    while len(concurrent) < 40:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if len(concurrent) >= 40:
            break
    report["concurrent"] = {
        "count": len(concurrent),
        "p50": percentile(concurrent, 0.50),
        "p95": percentile(concurrent, 0.95),
        "errors": errors[:5],
    }
    print(f"  bersamaan n={len(concurrent)} p50={report['concurrent']['p50']} ms p95={report['concurrent']['p95']} ms "
          f"errors={len(errors)}")

    # ---------------------------------------------------------------- 5) daftar & storage
    key = next(iter(keys))
    _, listing, list_ms = call(base, "GET", "/api/v1/knowledge?limit=500", key)
    _, _, metrics_ms = call(base, "GET", "/api/v1/metrics", key)
    documents = (listing.get("data") or {}).get("documents") or []
    report["list_documents"] = {
        "returned": len(documents),
        "latency_ms": round(list_ms, 1),
        "metrics_latency_ms": round(metrics_ms, 1),
    }
    report["storage"] = {
        "sparse_mb": dir_size_mb(data_dir / "sparse"),
        "qdrant_mb": dir_size_mb(data_dir / "qdrant"),
        "jobs_kb": round((data_dir / "jobs.json").stat().st_size / 1024, 1) if (data_dir / "jobs.json").exists() else 0.0,
        "rss_mb": rss_mb(args.pid),
    }
    print(f"  GET /knowledge: {len(documents)} dokumen dalam {report['list_documents']['latency_ms']} ms; "
          f"storage sparse={report['storage']['sparse_mb']} MB qdrant={report['storage']['qdrant_mb']} MB "
          f"jobs={report['storage']['jobs_kb']} KB rss={report['storage']['rss_mb']} MB")

    # tren: bandingkan rata-rata 10 dokumen pertama vs 10 terakhir
    if len(growth) >= 20:
        first = statistics.mean(row["wall_ms"] for row in growth[:10])
        last = statistics.mean(row["wall_ms"] for row in growth[-10:])
        report["growth_trend"] = {
            "first10_ms": round(first, 1),
            "last10_ms": round(last, 1),
            "ratio": round(last / first, 2) if first else 0.0,
        }
        print(f"  tren indeks: 10 dok pertama {round(first)} ms -> 10 terakhir {round(last)} ms "
              f"(x{report['growth_trend']['ratio']})")

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("laporan:", args.report)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    raise SystemExit(main())
