# -*- coding: utf-8 -*-
"""READ-ONLY explorer of the Brazilian reciTAL extraction agent (GET requests only).

usage (run from the project dir, isolated: python3.13 -E -P ...):
    recital_fetch.py agents [name_filter]          list extraction agents
    recital_fetch.py agent  <doctype_id>           agent details (status, settings, groups)
    recital_fetch.py files  <doctype_id> [limit]   files of the agent (testing / agent stage)
    recital_fetch.py prod   <doctype_id> [limit]   production files of the agent (if any)
    recital_fetch.py fetch  <file_id> [prod]       meta + values + groups + words -> _recital/<file_id>/
    recital_fetch.py export <file_id> [prod]       fetch into _inbox/<file_id>/ + the two files the
                                                   Streamlit app takes:
                                                   _inbox/<file_id>/<file_id>_raw.json   (main upload)
                                                   _inbox/<file_id>/<file_id>_words.json (words layer,
                                                   used by default since 2026-11-12)
                                                   (_recital/ stays the harness corpus: 22 files)
    recital_fetch.py image  <file_id> <page> [prod] page image -> _recital/<file_id>/page_<n>.<ext>

The Brazilian agent is Brazil_OCR202606_V4 (doctype 5485). A file whose status is `to_verify` (or
`verified`) has its extraction finished. Uploading a PDF is NOT done here: upload it in the reciTAL web
UI (agent page), then find its id with `files 5485`.

Guardrails: no POST/PUT/PATCH/DELETE; never /system_2/verify/*, */next/*, */prepare/*,
/dataset/{id}/version/{v}/ (state-changing GETs). Downloads go to _recital/<file_id>/ (data only).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\4885523\projects_with_claude\india_postprocessing")
from recital_client import RecitalClient  # noqa: E402

PROJ = Path(__file__).resolve().parent.parent
OUT_ROOT = PROJ / "_recital"      # the harness corpus (ground_truth.yaml)
INBOX_ROOT = PROJ / "_inbox"      # new documents to look at (Streamlit uploads)
FORBIDDEN = ("/verify", "/next/", "/prepare/", "/version/")
BRAZIL_DOCTYPE = 5485


def get(c, path, **params):
    if any(f in path for f in FORBIDDEN):
        raise SystemExit(f"refused: {path} is a state-changing endpoint")
    return c._call("GET", path, params=params or None)


def fetch(c, fid, prod=False, quiet=False, root=OUT_ROOT):
    """meta + values + groups + words of one file -> <root>/<fid>/*.json. Returns the dir."""
    base = f"/production/files/{fid}" if prod else f"/files/{fid}"
    d = Path(root) / str(fid)
    d.mkdir(parents=True, exist_ok=True)
    meta = get(c, f"{base}/")
    cm = meta.get("custom_metadata") or {}
    if isinstance(cm, dict) and "callback_token" in cm:
        cm["callback_token"] = "***masked***"
    for name, path in (("file", None), ("values", f"{base}/values/"), ("groups", f"{base}/groups/"),
                       ("words", f"{base}/words/")):
        obj = meta if path is None else get(c, path)
        (d / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        if not quiet:
            n = len(obj) if isinstance(obj, list) else len(obj.keys())
            print(f"  {name:7} -> {d / (name + '.json')} ({n})")
    if not quiet:
        print(f"file {fid}: {meta.get('name')!r} status={meta.get('status')} pages={len(meta.get('pages') or [])}")
    return d


def export(d):
    """The two uploads of the Streamlit app, from a fetched dir: the raw OCR JSON = values + groups
    (a LIST, the shape structure_ocr reads) and the words layer (the second upload, used by default)."""
    fid = d.name
    values = json.loads((d / "values.json").read_text(encoding="utf-8"))
    groups = json.loads((d / "groups.json").read_text(encoding="utf-8"))
    raw = d / f"{fid}_raw.json"
    raw.write_text(json.dumps(values + groups, ensure_ascii=False), encoding="utf-8")
    words = d / f"{fid}_words.json"      # compact: a 175-page report stays well under the upload limit
    words.write_text(json.dumps(json.loads((d / "words.json").read_text(encoding="utf-8")), ensure_ascii=False),
                     encoding="utf-8")
    return raw, words


def page_image(c, fid, page, out_dir):
    """One page image of a file -> out_dir/page_<n>.<ext> (GET /image/file/{id}/{page}/)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = c.session.get(f"{c.base_url}/image/file/{fid}/{page}/", timeout=c.timeout)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    ctype = r.headers.get("content-type", "")
    ext = "png" if "png" in ctype else "jpg" if ("jpeg" in ctype or "jpg" in ctype) else "bin"
    p = out_dir / f"page_{page}.{ext}"
    p.write_bytes(r.content)
    return p, ctype, len(r.content)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = RecitalClient()
    cmd = sys.argv[1]
    if cmd == "agents":
        flt = sys.argv[2] if len(sys.argv) > 2 else None
        for status in ("active", "all"):
            try:
                ags = get(c, "/system_2/document_type/", name_filter=flt, limit=100, status=status)
            except Exception as exc:  # status enum may differ
                print(f"(status={status}: {exc})")
                continue
            print(f"--- status={status}: {len(ags)} agents")
            for a in ags:
                print(f"  id={a.get('id'):>6}  {a.get('name')!r:50} status={a.get('status')} locked={a.get('is_locked')} "
                      f"user_correction={a.get('user_correction')} created={str(a.get('created_at'))[:10]}")
    elif cmd == "agent":
        a = get(c, f"/system_2/document_type/{int(sys.argv[2])}/")
        slim = {k: v for k, v in a.items() if k not in ("extraction_groups", "data_points")}
        print(json.dumps(slim, ensure_ascii=False, indent=1)[:6000])
        for k in ("extraction_groups", "data_points"):
            v = a.get(k)
            if isinstance(v, list):
                print(f"{k}: {len(v)}")
                for g in v[:40]:
                    if isinstance(g, dict):
                        print("   ", {kk: g.get(kk) for kk in ("id", "name", "group_name", "labels", "aggregation_method") if kk in g})
    elif cmd in ("files", "prod"):
        dt = int(sys.argv[2]); limit = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        path = "/files/" if cmd == "files" else "/production/files/"
        out, off = [], 0
        while True:
            batch = get(c, path, document_type_id=dt, doctype_id=dt, limit=limit, offset=off, sort_desc=True) or []
            out += batch
            if len(batch) < limit or len(out) >= 2000:
                break
            off += limit
        print(f"{len(out)} files ({path})")
        for f in out:
            print(f"  id={f.get('id'):>8} pages={len(f.get('pages') or []) or f.get('nb_pages')!s:>4} status={str(f.get('status')):14} "
                  f"up={str(f.get('uploaded_at'))[:19]} name={f.get('name')!r}")
    elif cmd == "fetch":
        fetch(c, int(sys.argv[2]), len(sys.argv) > 3 and sys.argv[3] == "prod")
    elif cmd == "export":
        d = fetch(c, int(sys.argv[2]), len(sys.argv) > 3 and sys.argv[3] == "prod", root=INBOX_ROOT)
        raw, words = export(d)
        print(f"Streamlit uploads:\n  raw OCR JSON (main)    : {raw}\n  OCR words (default)    : {words}")
    elif cmd == "image":
        fid, page = int(sys.argv[2]), int(sys.argv[3])
        p, ctype, n = page_image(c, fid, page, OUT_ROOT / str(fid))
        print(f"  {ctype} {n} bytes -> {p}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
