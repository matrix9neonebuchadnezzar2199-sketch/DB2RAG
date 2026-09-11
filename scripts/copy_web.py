from pathlib import Path

src = Path(r"H:\CURSOR\DB2RAG\docs")
dst = Path(r"H:\CURSOR\DB2RAG\web")
dst.mkdir(exist_ok=True)

repl = {
    "mockup.html": "/",
    "mockup_rag_create.html": "/wizard",
    "mockup_job_list.html": "/jobs",
    "mockup_job_progress.html": "/progress",
    "mockup_search_preview.html": "/search",
}
mapping = {
    "mockup.html": "index.html",
    "mockup_rag_create.html": "wizard.html",
    "mockup_job_list.html": "jobs.html",
    "mockup_job_progress.html": "progress.html",
    "mockup_search_preview.html": "search.html",
}

for name in ["mockup_cyber.css", "mockup_cyber.js"]:
    (dst / name).write_bytes((src / name).read_bytes())

script_tag = '  <script src="live.js"></script>\n</body>'
for src_name, dest_name in mapping.items():
    text = (src / src_name).read_text(encoding="utf-8")
    for old, new in repl.items():
        text = text.replace(old, new)
    text = text.replace("DB2RAG モック", "DB2RAG")
    if "live.js" not in text:
        text = text.replace("</body>", script_tag)
    (dst / dest_name).write_text(text, encoding="utf-8")

print("web files:", sorted(p.name for p in dst.iterdir()))
