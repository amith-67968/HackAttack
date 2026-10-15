import io
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


MIMES = {
    "spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "email": "text/plain; charset=utf-8",
    "report": "text/markdown; charset=utf-8",
}


def _supported(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f.get("value_status") == "found" and
            f.get("verification_status") == "source_supported" and f.get("field_value") is not None and
            f.get("supporting_excerpt")]


def _resolve_values(findings: list[dict], sources: list[dict]) -> list[dict]:
    by_source = {str(s.get("id")): s for s in sources}
    grouped: dict[tuple[str, str], list[dict]] = {}
    for f in _supported(findings):
        source = by_source.get(str(f.get("source_id")))
        excerpt = str(f.get("supporting_excerpt", ""))
        content = str((source or {}).get("page_content") or (source or {}).get("content_excerpt") or "")
        val = str(f["field_value"])
        if excerpt not in content or val.casefold() not in excerpt.casefold():
            continue
        grouped.setdefault((str(f.get("entity_name", "")), str(f.get("field_name", ""))), []).append(f)
    rows = []
    for (entity, field), matches in sorted(grouped.items()):
        values = {str(item["field_value"]) for item in matches}
        status = "conflict" if len(values) > 1 else "source-supported"
        rows.append({"entity": entity, "field": field, "value": "Conflicting values" if len(values) > 1 else next(iter(values)),
                     "status": status, "evidence": [{"source_id": item.get("source_id"), "url": by_source.get(str(item.get("source_id")), {}).get("url"),
                     "title": by_source.get(str(item.get("source_id")), {}).get("title"), "excerpt": item.get("supporting_excerpt")} for item in matches]})
    return rows


def build_artifact(task: dict, findings: list[dict], sources: list[dict], artifact_type: str, recipient: str | None = None) -> tuple[bytes, dict]:
    rows = _resolve_values(findings, sources)
    required_fields = (task.get("plan") or {}).get("required_fields") or (task.get("config") or {}).get("required_fields") or []
    entities = list(dict.fromkeys([str(item.get("entity_name")) for item in findings if item.get("entity_name")] + [r["entity"] for r in rows]))
    seen = {(r["entity"].casefold(), r["field"].casefold()) for r in rows}
    for entity in entities:
        for field in required_fields:
            if (entity.casefold(), str(field).casefold()) not in seen:
                rows.append({"entity": entity, "field": str(field), "value": "Not found in saved source-backed evidence", "status": "unverified", "evidence": []})
    task_status = str(task.get("status", "UNKNOWN"))
    query = str(task.get("query", "Research task"))
    supported_count = sum(1 for row in rows if row["status"] in {"source-supported", "conflict"})
    unverified_count = len(rows) - supported_count
    if artifact_type == "spreadsheet":
        wb = Workbook()
        result = wb.active
        result.title = "Results"
        result.append(["Entity", "Field", "Value", "Evidence status", "Source URLs"])
        for row in rows:
            result.append([row["entity"], row["field"], row["value"], row["status"], ", ".join(e["url"] or "" for e in row["evidence"])])
        evidence = wb.create_sheet("Sources & Evidence")
        evidence.append(["Entity", "Field", "Value", "Source title", "Source URL", "Supporting excerpt", "Verification", "Retrieval status"])
        evidenced_source_ids = set()
        for row in rows:
            for ev in row["evidence"]:
                evidence.append([row["entity"], row["field"], row["value"], ev.get("title") or "", ev.get("url") or "", ev["excerpt"], row["status"], "retrieved"])
                evidenced_source_ids.add(str(ev.get("source_id")))
        for source in sources:
            if str(source.get("id")) not in evidenced_source_ids:
                evidence.append(["", "", "", source.get("title") or "", source.get("url") or "", source.get("content_excerpt") or "", "no supported finding", source.get("fetch_status") or "unknown"])
        summary = wb.create_sheet("Research Summary")
        summary.append(["Research request", query]); summary.append(["Task status", task_status]); summary.append(["Generated at (UTC)", datetime.now(timezone.utc).isoformat()]); summary.append(["Supported result rows", len(rows)])
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
            # Treat all scraped/model-generated strings as text; never emit
            # untrusted spreadsheet formulas from evidence fields.
            for cells in ws.iter_rows():
                for cell in cells:
                    if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith(("=", "+", "-", "@"))):
                        cell.value = "'" + str(cell.value)
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="243B53")
            for column in ws.columns:
                letter = column[0].column_letter
                ws.column_dimensions[letter].width = min(60, max(14, max(len(str(c.value or "")) for c in column) + 2))
                for cell in column[1:]: cell.alignment = Alignment(wrap_text=True, vertical="top")
        for ws, url_column in ((result, 5), (evidence, 5)):
            for row_no in range(2, ws.max_row + 1):
                cell = ws.cell(row=row_no, column=url_column)
                if isinstance(cell.value, str) and cell.value.startswith(("https://", "http://")):
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"
        buf = io.BytesIO(); wb.save(buf); payload = buf.getvalue()
        check = load_workbook(io.BytesIO(payload), read_only=True)
        if check.sheetnames != ["Results", "Sources & Evidence", "Research Summary"] or check["Results"].max_row != len(rows) + 1:
            raise ValueError("Generated spreadsheet did not pass reopen validation")
        preview = {"rows": rows[:100], "task_status": task_status, "summary": f"{supported_count} source-backed row(s); {unverified_count} unverified row(s)"}
    elif artifact_type == "email":
        subject = f"Research results: {query[:120]}".replace("\n", " ")
        lines = [f"To: {recipient}" if recipient else "To: (add recipient)", f"Subject: {subject}", "", "Hello,", "", f"Here are the source-supported findings for: {query}", "", f"Research task status: {task_status}", ""]
        for row in rows:
            lines.append(f"- {row['entity']} — {row['field']}: {row['value']} ({row['status']})")
            for ev in row["evidence"]:
                lines.extend([f"  Source: {ev.get('title') or ev.get('url')}", f"  {ev.get('url')}", f"  Evidence: {ev['excerpt']}"])
        if not rows: lines.append("No source-supported findings were available in the saved research evidence.")
        lines.extend(["", "This is a draft for review; it has not been sent."])
        text = "\n".join(lines); payload = text.encode("utf-8"); preview = {"subject": subject, "body": text, "draft_only": True}
    elif artifact_type == "report":
        lines = [f"# Research report: {query}", "", f"**Task status:** {task_status}", "", "## Summary", f"Saved evidence contains {supported_count} source-backed entity-field result(s) and {unverified_count} unverified row(s). Values are not independently cross-checked.", "", "## Findings"]
        if not rows: lines.append("No source-supported findings were available in the saved research evidence.")
        for row in rows:
            lines.extend([f"### {row['entity']} — {row['field']}", f"**Value:** {row['value']}  ", f"**Evidence status:** {row['status']}"])
            for ev in row["evidence"]: lines.extend([f"> {ev['excerpt']}", f"[Source: {ev.get('title') or ev.get('url')}]({ev.get('url')})", ""])
        lines.extend(["## Sources", *[f"- [{source.get('title') or source.get('url')}]({source.get('url')}) — {source.get('fetch_status', 'unknown')}" for source in sources if source.get("url")], "", "## Limitations", f"The persisted research task status is {task_status}. Unavailable values are not filled in."])
        text = "\n".join(lines); payload = text.encode("utf-8"); preview = {"markdown": text, "task_status": task_status, "row_count": len(rows)}
    else:
        raise ValueError("Unsupported artifact type")
    return payload, preview


def artifact_filename(artifact_type: str, task_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(task_id).lower()).strip("-")[:50]
    ext = {"spreadsheet": "xlsx", "email": "txt", "report": "md"}[artifact_type]
    return f"proofloop-{slug}-{artifact_type}.{ext}"


def store_local(root: str, artifact_id: str, payload: bytes) -> str:
    base = Path(root).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = (base / f"{artifact_id}.bin").resolve()
    if base not in path.parents:
        raise ValueError("Artifact path escaped configured storage directory")
    path.write_bytes(payload)
    return str(path)


def generate_and_save_artifact(task: dict, repository, settings, artifact_type: str, recipient: str | None = None) -> dict:
    """Build and persist one deliverable from a task's already saved evidence."""
    task_id = str(task["id"])
    generation_key = f"{artifact_type}:{hashlib.sha256((recipient or '').encode()).hexdigest()[:16]}"
    artifact_id = str(uuid5(NAMESPACE_URL, f"proofloop:{task_id}:{generation_key}"))
    existing = repository.get_artifact(artifact_id)
    if existing:
        return existing

    sources = repository.list_sources(task_id)
    findings = repository.list_findings(task_id)
    payload, preview = build_artifact(task, findings, sources, artifact_type, recipient)
    filename = artifact_filename(artifact_type, task_id)
    backend = settings.artifact_storage_backend
    if backend == "auto":
        backend = "supabase" if settings.supabase_configured else "local"
    if backend == "supabase":
        if not settings.supabase_configured:
            raise ValueError("Artifact storage is not configured")
        storage_path = f"{task_id}/{artifact_id}/{filename}"
        repository.upload_artifact(settings.supabase_storage_bucket, storage_path, payload, MIMES[artifact_type])
    else:
        storage_path = store_local(settings.artifact_dir, artifact_id, payload)
    row = {
        "id": artifact_id, "task_id": task_id, "artifact_type": artifact_type,
        "generation_key": generation_key, "filename": filename, "storage_backend": backend,
        "storage_path": storage_path, "mime_type": MIMES[artifact_type], "status": "READY",
        "preview": preview, "content_hash": hashlib.sha256(payload).hexdigest(),
    }
    return repository.save_artifact(row)
