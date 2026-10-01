/* Markdown renderer for the RAG console. No build step, no CDN, no dependencies.
 *
 * Why this exists: the model is asked to answer in Markdown (headings, bullets, tables, inline
 * code). The chat used to print that text verbatim, so the operator saw raw `##` and `|` pipes.
 * This turns the answer into readable HTML.
 *
 * Security: the answer comes from an LLM reading untrusted documents, so it is treated as
 * hostile input. Escaping happens FIRST (escapeHtml), and only then are a small, fixed set of
 * Markdown constructs recognised. Every URL is re-checked (safeUrl) and no raw HTML from the
 * answer is ever executed - `<script>` in an answer shows up as visible text, not code.
 *
 * Scope is deliberately small: headings, bold/italic, inline code, fenced code, links, ordered
 * and unordered lists, blockquotes, horizontal rules, and GitHub-style tables. That covers what
 * a knowledge answer actually uses; anything else stays as plain text rather than being mangled.
 */

"use strict";

const Markdown = (() => {
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Only http(s)/mailto/relative links survive; javascript: and data: are dropped.
  function safeUrl(url) {
    const raw = String(url || "").trim();
    if (/^(https?:|mailto:)/i.test(raw)) return raw;
    if (/^[/#?][^:]/.test(raw) || /^[/#?]$/.test(raw)) return raw;
    return "";
  }

  // Inline constructs. Runs on text that is ALREADY escaped.
  function inline(text) {
    let out = text;
    // `code` first: its content must not be re-processed for bold/links.
    const codes = [];
    out = out.replace(/`([^`]+)`/g, (match, code) => {
      codes.push(code);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    // [label](url) - hanya bila URL-nya aman; kalau tidak, biarkan sebagai teks apa adanya.
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, url) => {
      const href = safeUrl(url);
      if (!href) return match;
      return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    });
    // **bold** and __bold__
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    // *italic* and _italic_ (single markers, not part of **)
    out = out.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_\w])_([^_\s][^_]*?)_(?!_)/g, "$1<em>$2</em>");
    // restore code spans
    out = out.replace(/\u0000(\d+)\u0000/g, (match, index) => "<code>" + codes[Number(index)] + "</code>");
    return out;
  }

  function splitRow(line) {
    let text = line.trim();
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|")) text = text.slice(0, -1);
    return text.split("|").map((cell) => cell.trim());
  }

  function isSeparatorRow(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function render(source) {
    // Escape HTML entities but keep `>` recognisable for blockquotes: the blockquote marker is
    // the FIRST character of a line, so we un-escape only that position afterwards.
    const lines = escapeHtml(source || "").replace(/\r\n?/g, "\n").split("\n");
    const html = [];
    let index = 0;

    const paragraph = [];
    const flushParagraph = () => {
      if (paragraph.length) {
        html.push("<p>" + inline(paragraph.join(" ")) + "</p>");
        paragraph.length = 0;
      }
    };

    while (index < lines.length) {
      const line = lines[index];

      // fenced code block
      const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
      if (fence) {
        flushParagraph();
        const marker = fence[1];
        const lang = fence[2] || "";
        const body = [];
        index += 1;
        while (index < lines.length && !new RegExp("^\\s*" + marker + "\\s*$").test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1; // closing fence (or end)
        const cls = lang ? ' class="lang-' + lang.replace(/[^\w+-]/g, "") + '"' : "";
        html.push("<pre><code" + cls + ">" + body.join("\n") + "</code></pre>");
        continue;
      }

      // table: a header row followed by a separator row
      if (line.includes("|") && index + 1 < lines.length && isSeparatorRow(lines[index + 1])) {
        flushParagraph();
        const header = splitRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        const head = header.map((cell) => "<th>" + inline(cell) + "</th>").join("");
        const body = rows.map((row) => {
          const cells = header.map((_, position) => "<td>" + inline(row[position] || "") + "</td>").join("");
          return "<tr>" + cells + "</tr>";
        }).join("");
        html.push('<div class="md-table"><table><thead><tr>' + head + "</tr></thead><tbody>" + body + "</tbody></table></div>");
        continue;
      }

      // heading
      const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        const level = Math.min(6, heading[1].length + 1); // # -> h2 (h1 is the panel title)
        html.push("<h" + level + ">" + inline(heading[2].trim()) + "</h" + level + ">");
        index += 1;
        continue;
      }

      // horizontal rule
      if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
        flushParagraph();
        html.push("<hr>");
        index += 1;
        continue;
      }

      // blockquote (consecutive ">" lines; escaped as &gt; after escapeHtml)
      if (/^\s*(?:>|&gt;)\s?/.test(line)) {
        flushParagraph();
        const quoted = [];
        while (index < lines.length && /^\s*(?:>|&gt;)\s?/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*(?:>|&gt;)\s?/, ""));
          index += 1;
        }
        html.push("<blockquote>" + render(quoted.join("\n")) + "</blockquote>");
        continue;
      }

      // list (ordered / unordered), with nesting by indentation
      const bullet = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        flushParagraph();
        const ordered = /\d/.test(bullet[2]);
        const tag = ordered ? "ol" : "ul";
        const baseIndent = bullet[1].length;
        const items = [];
        while (index < lines.length) {
          const match = lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          if (!match) break;
          if (match[1].length < baseIndent) break;
          if (match[1].length > baseIndent) {
            // nested list: render the sub-block and attach it to the previous item
            const sub = [];
            const subIndent = match[1].length;
            while (index < lines.length) {
              const inner = lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
              if (!inner || inner[1].length < subIndent) break;
              sub.push(lines[index].slice(subIndent));
              index += 1;
            }
            if (items.length) items[items.length - 1] += render(sub.join("\n"));
            continue;
          }
          if (/\d/.test(match[2]) !== ordered) break;
          items.push(inline(match[3]));
          index += 1;
        }
        html.push("<" + tag + ">" + items.map((item) => "<li>" + item + "</li>").join("") + "</" + tag + ">");
        continue;
      }

      // blank line ends the current paragraph
      if (line.trim() === "") {
        flushParagraph();
        index += 1;
        continue;
      }

      paragraph.push(line.trim());
      index += 1;
    }
    flushParagraph();
    return html.join("\n");
  }

  return { render, escapeHtml };
})();

// Exposed for the Node-based unit tests (no-op in the browser).
if (typeof module !== "undefined" && module.exports) module.exports = { Markdown };
