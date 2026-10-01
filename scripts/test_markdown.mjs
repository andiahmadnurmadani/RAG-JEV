// Uji renderer Markdown (dijalankan dengan Node, tanpa dependensi).
//   node scripts/test_markdown.mjs
//
// Dua hal yang diuji: (1) konstruk Markdown yang dipakai jawaban benar-benar jadi HTML,
// (2) jawaban diperlakukan sebagai masukan berbahaya - tidak ada tag/handler dari jawaban
// yang boleh hidup di halaman.
import { createRequire } from "module";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { Markdown } = require(join(here, "..", "app", "ui", "markdown.js"));

// Tag yang HANYA boleh lahir dari renderer kami.
const ALLOWED_TAGS = /<\/?(?:p|strong|em|code|pre|h[1-6]|ul|ol|li|blockquote|hr|table|thead|tbody|tr|th|td|div|a|br)(?:\s[^<>]*)?\/?>/gi;

function unsafeReasons(html) {
  const reasons = [];
  // Setelah tag buatan kami dibuang, sisa '<' atau '>' berarti berasal dari jawaban mentah.
  const leftover = html.replace(ALLOWED_TAGS, "");
  if (/[<>]/.test(leftover)) reasons.push("ada tag mentah dari jawaban");
  if (/<[a-z][^<>]*\son\w+\s*=/i.test(html)) reasons.push("ada atribut event (on*)");
  if (/<a\b[^<>]*href\s*=\s*["']?\s*javascript:/i.test(html)) reasons.push("href javascript:");
  if (/<a\b[^<>]*href\s*=\s*["']?\s*data:/i.test(html)) reasons.push("href data:");
  return reasons;
}

const cases = [
  ["judul & tebal", "## Ringkasan\n\nKolom **user_id** wajib diisi.", ["<h3>Ringkasan</h3>", "<strong>user_id</strong>"]],
  ["daftar bersarang", "- satu\n- dua\n  - anak\n- tiga", ["<ul>", "<li>satu</li>", "<li>anak</li>"]],
  ["daftar angka", "1. pertama\n2. kedua", ["<ol>", "<li>pertama</li>", "<li>kedua</li>"]],
  ["tabel", "| Kolom | Tipe |\n|---|---|\n| user_id | int |", ["<table>", "<th>Kolom</th>", "<td>user_id</td>"]],
  ["kode inline & blok", "Pakai `helpdesk_reply`.\n\n```sql\nSELECT 1;\n```", ["<code>helpdesk_reply</code>", '<pre><code class="lang-sql">']],
  ["tautan aman", "[panduan](https://example.com/a)", ['href="https://example.com/a"']],
  ["kutipan & garis", "> catatan penting\n\n---", ["<blockquote>", "<hr>"]],
  ["teks biasa", "Ini kalimat biasa.", ["<p>Ini kalimat biasa.</p>"]],
  ["tanpa tag mentah", "<script>alert(1)</script>", ["&lt;script&gt;"]],
  ["tautan javascript ditolak", "[klik](javascript:alert(1))", []],
  ["gambar onerror tetap teks", "| a |\n|---|\n| <img src=x onerror=alert(1)> |", ["&lt;img"]],
];

let gagal = 0;
for (const [name, src, harusAda] of cases) {
  const out = Markdown.render(src);
  const problems = [];
  for (const needle of harusAda) {
    if (!out.includes(needle)) problems.push("tidak mengandung: " + needle);
  }
  const alasan = unsafeReasons(out);
  console.log((problems.length || alasan.length ? "GAGAL " : "OK    ") + name);
  if (problems.length) console.log("       " + problems.join("; "));
  if (alasan.length) console.log("       " + alasan.join("; ") + "  <- " + out.slice(0, 160));
  if (problems.length || alasan.length) gagal += 1;
}

console.log();
console.log(gagal === 0 ? "SEMUA LULUS (" + cases.length + " kasus)" : gagal + " dari " + cases.length + " GAGAL");
process.exit(gagal === 0 ? 0 : 1);
