#!/usr/bin/env python3
"""Local search web app for corpus structured + semantic lookup."""

from __future__ import annotations

import html
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
from search_corpus import paper_detail, semantic_search, structured_search  # noqa: E402

app = FastAPI(title="Corpus Search", version="1.0")


def _page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Corpus Search</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; margin: 18px; }
    .row { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; }
    input, select, button { padding: 8px; font-size: 14px; }
    input[type=text] { width: 560px; }
    .meta { color: #666; font-size: 12px; margin-bottom: 10px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
    .title { font-weight: 600; margin-bottom: 6px; }
    .snippet { color: #333; white-space: pre-wrap; margin-top: 6px; }
    .pill { display:inline-block; padding:2px 6px; border:1px solid #bbb; border-radius:12px; font-size:12px; margin-right:4px; }
    #detail { border-top:2px solid #eee; margin-top:18px; padding-top:14px; }
  </style>
</head>
<body>
  <h2>Corpus Search</h2>
  <div class="row">
    <label>Mode</label>
    <select id="mode">
      <option value="structured">Structured (DB)</option>
      <option value="semantic">Semantic (Vector)</option>
    </select>
    <input id="q" type="text" placeholder="Author, DOI, title, tags..."/>
    <button id="go">Search</button>
  </div>
  <div class="meta">
    Structured = exact metadata search in papers.db. Semantic = meaning-based search via vector index.
  </div>
  <div id="summary"></div>
  <div id="results"></div>
  <div id="detail"></div>
<script>
const modeEl = document.getElementById('mode');
const qEl = document.getElementById('q');
const resultsEl = document.getElementById('results');
const detailEl = document.getElementById('detail');
const summaryEl = document.getElementById('summary');
const goBtn = document.getElementById('go');

function modePlaceholder() {
  if (modeEl.value === 'semantic') {
    qEl.placeholder = 'Find papers about violated rhythmic prediction and expectancy';
  } else {
    qEl.placeholder = 'Author, DOI, title, year, rating, tags...';
  }
}
modeEl.addEventListener('change', modePlaceholder);
modePlaceholder();

function esc(s) {
  return (s ?? '').toString().replace(/[&<>\\"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;'}[c] || c));
}

async function loadDetail(id) {
  const r = await fetch(`/api/paper/${encodeURIComponent(id)}`);
  const d = await r.json();
  if (!d.ok) { detailEl.innerHTML = `<p>${esc(d.error || 'not found')}</p>`; return; }
  const p = d.paper;
  const authors = Array.isArray(p.authors) ? p.authors.join('; ') : (p.authors || '');
  const tags = p.tags || {};
  const tagTxt = Object.keys(tags).length ? `<pre>${esc(JSON.stringify(tags, null, 2))}</pre>` : '<i>No tags</i>';
  const pdfLink = p.pdf_path ? `<a href="/paper/${encodeURIComponent(id)}/pdf" target="_blank">Open PDF</a>` : '<i>PDF path missing</i>';
  detailEl.innerHTML = `
    <h3>${esc(p.title || id)}</h3>
    <div><b>ID:</b> ${esc(p.paper_id)}</div>
    <div><b>Authors:</b> ${esc(authors)}</div>
    <div><b>Year:</b> ${esc(p.year || '')} &nbsp; <b>Journal:</b> ${esc(p.journal || '')}</div>
    <div><b>DOI:</b> ${esc(p.doi || '')}</div>
    <div><b>Rating:</b> ${esc(p.rating || '')}</div>
    <div><b>PDF:</b> ${pdfLink}</div>
    <details><summary>Tags</summary>${tagTxt}</details>
  `;
}

async function runSearch() {
  const mode = modeEl.value;
  const q = qEl.value.trim();
  if (!q) { return; }
  summaryEl.innerHTML = 'Searching...';
  resultsEl.innerHTML = '';
  detailEl.innerHTML = '';
  const r = await fetch(`/api/search?mode=${encodeURIComponent(mode)}&q=${encodeURIComponent(q)}&top=20`);
  const d = await r.json();
  if (!d.ok) {
    summaryEl.innerHTML = `<span style="color:#b00">${esc(d.error || 'search failed')}</span>`;
    return;
  }
  summaryEl.innerHTML = `<b>${d.result_count}</b> results (${esc(d.mode)})`;
  if (!d.results || !d.results.length) {
    resultsEl.innerHTML = '<p>No matches.</p>';
    return;
  }
  const cards = d.results.map((x) => {
    const score = (x.score !== undefined) ? ` <span class="pill">score ${Number(x.score).toFixed(3)}</span>` : '';
    const snippet = x.snippet ? `<div class="snippet">${esc(x.snippet)}</div>` : '';
    return `<div class="card">
      <div class="title">${esc(x.title || x.paper_id)}${score}</div>
      <div>${esc(x.authors || '')}</div>
      <div>${esc(x.year || '')} ${x.journal ? ' - ' + esc(x.journal) : ''} ${x.rating ? ' - ' + esc(x.rating) : ''}</div>
      <div><a href="#" data-paper="${esc(x.paper_id)}">View details</a></div>
      ${snippet}
    </div>`;
  }).join('');
  resultsEl.innerHTML = cards;
  resultsEl.querySelectorAll('a[data-paper]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      loadDetail(a.getAttribute('data-paper'));
    });
  });
}

goBtn.addEventListener('click', runSearch);
qEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _page()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "db": str(config.DB_PATH), "vector_dir": str(config.VECTOR_DB_DIR)}


@app.get("/api/search")
def api_search(
    mode: str = Query("structured", pattern="^(structured|semantic)$"),
    q: str = Query(""),
    top: int = Query(20, ge=1, le=100),
    min_rating: str = Query(""),
    year_from: int | None = Query(None),
    year_to: int | None = Query(None),
) -> JSONResponse:
    if mode == "semantic":
        result = semantic_search(q, top_k=top, min_rating=min_rating, year_from=year_from, year_to=year_to)
        if result.get("ok"):
            enriched = []
            for row in result.get("results", []):
                detail = paper_detail(str(row.get("paper_id")))
                paper = detail.get("paper") if detail.get("ok") else {}
                enriched.append({**row, "pdf_path": paper.get("pdf_path", ""), "doi": paper.get("doi", "")})
            result["results"] = enriched
            result["result_count"] = len(enriched)
    else:
        result = structured_search(q, top_k=top, min_rating=min_rating, year_from=year_from, year_to=year_to)
    return JSONResponse(result)


@app.get("/api/paper/{paper_id}")
def api_paper(paper_id: str) -> JSONResponse:
    return JSONResponse(paper_detail(paper_id))


@app.get("/paper/{paper_id}/pdf")
def paper_pdf(paper_id: str) -> FileResponse:
    detail = paper_detail(paper_id)
    if not detail.get("ok"):
        raise HTTPException(status_code=404, detail="paper not found")
    pdf_path = Path(str(detail["paper"].get("pdf_path") or ""))
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="pdf missing")
    # Limit serving to source-pdfs tree.
    try:
        pdf_path.resolve().relative_to(config.SOURCE_PDFS.resolve())
    except Exception as exc:
        raise HTTPException(status_code=403, detail="pdf outside allowed directory") from exc
    return FileResponse(path=str(pdf_path), filename=pdf_path.name, media_type="application/pdf")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CORPUS_SEARCH_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port)
