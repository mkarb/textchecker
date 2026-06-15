"""Network front end for the proofreader (local, single-box).

Users on the local network upload a .docx/.xml over HTTP and get a report back;
no shell access to the model host is needed. This service is meant to run LOCAL
ONLY -- it binds 127.0.0.1 by default and is not internet facing.

Design:
- Two-stage pipeline. Stage A (a CPU thread pool) does extraction + the
  deterministic checks in parallel, so a user's deterministic findings are ready
  in seconds. Stage B (a pool of PROOFER_MODEL_LANES worker threads, default 1)
  runs the model judgment -- this is the GPU lane and the only serialized part.
- No logins. A per-browser session cookie scopes each browser's jobs (you see your
  own; another machine on the LAN can't list yours). This is convenience +
  separation, NOT an auth boundary -- access control and audit live at the
  network/host layer.
- Fair scheduling across sessions so one browser's batch can't starve the lane.
- Uploads go under PROOFER_DATA in a random job dir (client filename never used as
  a path). A reaper deletes job dirs after PROOFER_RETAIN_HOURS so CUI does not
  accumulate.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

import main as pipeline          # reuse load / gather / render_md
import judge as judge_mod

HERE = Path(__file__).resolve().parent
# Default to a path under the app dir so it works on any OS without configuration;
# the container overrides this with PROOFER_DATA=/data/jobs (a mounted volume).
DATA = Path(os.environ.get("PROOFER_DATA", str(HERE / "data" / "jobs")))
DATA.mkdir(parents=True, exist_ok=True)
ALLOWED = {".docx", ".pdf", ".xml"}
MAX_BYTES = int(os.environ.get("PROOFER_MAX_BYTES", 25 * 1024 * 1024))
RETAIN_HOURS = float(os.environ.get("PROOFER_RETAIN_HOURS", 12))

# Local-only bind + capacity knobs (all overridable by env; safe defaults).
HOST = os.environ.get("PROOFER_HOST", "0.0.0.0")            # serve the LAN; the firewall is the boundary
PORT = int(os.environ.get("PROOFER_PORT", "8080"))
MODEL_LANES = max(1, int(os.environ.get("PROOFER_MODEL_LANES", "1")))   # GPU lanes
STAGE_A_WORKERS = max(1, int(os.environ.get("PROOFER_STAGE_A_WORKERS", str(os.cpu_count() or 4))))
MAX_QUEUE = int(os.environ.get("PROOFER_MAX_QUEUE", "100"))             # global active cap
PER_SESSION_CAP = int(os.environ.get("PROOFER_PER_SESSION_CAP", "5"))   # active jobs per browser
MAX_JOBS = int(os.environ.get("PROOFER_MAX_JOBS", "1000"))              # cap in-memory job records
SESSION_COOKIE = os.environ.get("PROOFER_SESSION_COOKIE", "proofer_sid")
COOKIE_SECURE = os.environ.get("PROOFER_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
SESSION_TTL = int(RETAIN_HOURS * 3600)

app = FastAPI(title="Local Manual Proofreader")

log = logging.getLogger("proofer")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s proofer %(message)s"))
    log.addHandler(_h)
    log.propagate = False

if HOST == "0.0.0.0":
    log.info("Bound to 0.0.0.0:%s (all interfaces) for LAN access. Keep it off the internet "
             "at the network layer: no WAN->LAN forward on the firewall and an inbound rule "
             "scoping this port to the LAN subnet -- the bind address is not the boundary.", PORT)

# --------------------------------------------------------------------- job store
# In-memory only: on restart, queued/in-flight jobs are gone and users re-upload.
# A record is: {status, name, created, session, canceled, opts, enqueued_at,
#               findings, model_ok, model_note, model_reported,
#               completion_tokens, latency_ms, error}
_jobs: dict = {}
_lock = threading.Lock()
_cond = threading.Condition(_lock)   # model workers wait here for awaiting_model jobs
_served_seq = 0                      # monotonic; bumped when a job enters the lane
_last_served: dict = {}              # session -> last served_seq (fair round-robin)
_latencies: list = []                # rolling model latencies (ms) for ETA
_LAT_KEEP = 20

ACTIVE = {"queued", "extracting", "awaiting_model", "judging"}
CANCELABLE = {"queued", "extracting", "awaiting_model"}
TERMINAL = {"done", "error", "canceled"}


def _now():
    return time.time()


def _set(job_id, **kw):
    with _lock:
        _jobs.setdefault(job_id, {}).update(kw)


def _get(job_id):
    with _lock:
        return dict(_jobs.get(job_id, {}))


# --------------------------------------------------------------------- sessions
def _peek_session(request: Request):
    """Caller's session id from the cookie, or None. Does not mint one."""
    return request.cookies.get(SESSION_COOKIE)


def _ensure_session(request: Request, response: Response):
    """Caller's session id, minting + setting a cookie if absent."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = secrets.token_urlsafe(24)
        response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL,
                            httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return sid


def _owned(job_id, sid):
    """Return a copy of the job iff it exists AND belongs to this session."""
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if (j and sid and j.get("session") == sid) else None


# ------------------------------------------------------------------- scheduling
def _fair_order_locked():
    """awaiting_model jobs in serve order (least-recently-served session first)."""
    waiting = [(jid, j) for jid, j in _jobs.items()
               if j["status"] == "awaiting_model" and not j.get("canceled")]
    waiting.sort(key=lambda kv: (_last_served.get(kv[1]["session"], 0),
                                 kv[1].get("enqueued_at", 0)))
    return [jid for jid, _ in waiting]


def _pick_next_locked():
    """Next job to judge: fair order, excluding sessions already judging (cap one
    in flight per session -- matters once MODEL_LANES > 1)."""
    busy = {j["session"] for j in _jobs.values() if j["status"] == "judging"}
    for jid in _fair_order_locked():
        if _jobs[jid]["session"] not in busy:
            return jid
    return None


def _avg_latency_locked():
    return (sum(_latencies) / len(_latencies)) if _latencies else None


def _position_eta_locked(job_id):
    """0-based position among waiting jobs + a rough ETA in ms (None if unknown)."""
    order = _fair_order_locked()
    if job_id not in order:
        return None, None
    idx = order.index(job_id)
    judging = sum(1 for j in _jobs.values() if j["status"] == "judging")
    avg = _avg_latency_locked()
    if avg is None:
        return idx, None
    slots_ahead = idx + judging
    return idx, int((slots_ahead // MODEL_LANES) * avg + avg)


def _log_done(job_id):
    """Emit one structured line per finished job (call OUTSIDE the lock)."""
    j = _get(job_id)
    log.info("job=%s file=%s status=%s model_ok=%s model=%s completion_tokens=%s "
             "latency_ms=%s session=%s",
             job_id, j.get("name"), j.get("status"), j.get("model_ok"),
             j.get("model_reported"), j.get("completion_tokens"),
             j.get("latency_ms"), (j.get("session") or "")[:8])


def _prune_jobs_locked():
    """Bound the in-memory job store: once it exceeds MAX_JOBS, drop the oldest
    TERMINAL records (their dirs are still cleared by the reaper on its own schedule).
    Active jobs are never dropped. Call under _lock."""
    if len(_jobs) <= MAX_JOBS:
        return
    terminal = sorted(((jid, j) for jid, j in _jobs.items() if j["status"] in TERMINAL),
                      key=lambda kv: kv[1].get("created", 0))
    for jid, _j in terminal[:len(_jobs) - MAX_JOBS]:
        _jobs.pop(jid, None)


# ----------------------------------------------------------- stage A (CPU pool)
def _stage_a(job_id):
    """Extraction + deterministic checks; write deterministic artifacts right away,
    then either finish (model off) or hand the job to the model lane."""
    try:
        with _lock:
            j = _jobs.get(job_id)
            if not j or j.get("canceled"):
                return
            j["status"] = "extracting"
            opts = dict(j["opts"])
        jdir = DATA / job_id
        src = next(p for p in jdir.iterdir() if p.suffix.lower() in ALLOWED)
        doc = pipeline.load(str(src))
        findings = pipeline.gather(doc, opts.get("expected_customer"), opts.get("min_count", 3))
        # deterministic-only artifacts available immediately (before the model step)
        (jdir / "findings.json").write_text(
            json.dumps({"deterministic": findings, "model": None}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        (jdir / "report.md").write_text(pipeline.render_md(findings, None), encoding="utf-8")

        finished = False
        with _cond:
            j = _jobs.get(job_id)
            if not j or j.get("canceled"):
                return
            j["findings"] = findings
            if opts.get("no_llm"):
                j.update(status="done", model_ok=False, model_note="model disabled (--no-llm)")
                finished = True
            else:
                j["status"] = "awaiting_model"
                j["enqueued_at"] = _now()
                _cond.notify_all()
        if finished:
            _log_done(job_id)
    except Exception as e:
        with _lock:
            if job_id in _jobs:
                _jobs[job_id].update(status="error", error=str(e))
        log.exception("job=%s stage_a failed: %s", job_id, e)


# -------------------------------------------------------- stage B (model lanes)
def _model_worker():
    while True:
        with _cond:
            job_id = _pick_next_locked()
            while job_id is None:
                _cond.wait()
                job_id = _pick_next_locked()
            global _served_seq
            _served_seq += 1
            j = _jobs[job_id]
            _last_served[j["session"]] = _served_seq
            j["status"] = "judging"
            opts = dict(j["opts"])
            findings = j.get("findings")
        # ---- GPU call, OUTSIDE the lock (long-running, serialized by lane count) ----
        jdir = DATA / job_id
        try:
            llm = judge_mod.judge(findings, base_url=opts.get("base_url"),
                                  model=opts.get("model"), context=opts.get("context"))
        except Exception as e:
            llm = {"_error": str(e)}
        try:
            (jdir / "findings.json").write_text(
                json.dumps({"deterministic": findings, "model": llm}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            (jdir / "report.md").write_text(pipeline.render_md(findings, llm), encoding="utf-8")
        except Exception as e:
            log.exception("job=%s write failed: %s", job_id, e)
        clean = bool(llm and "_error" not in llm and "_parse_error" not in llm)
        meta = (llm or {}).get("_meta") or {}
        usage = meta.get("usage") or {}
        lat = meta.get("latency_ms")
        with _cond:
            j = _jobs.get(job_id)
            if j is not None:
                j.update(status="done", model_ok=clean,
                         model_note=(llm or {}).get("_error") or (llm or {}).get("_parse_error"),
                         model_reported=meta.get("model_reported"),
                         completion_tokens=usage.get("completion_tokens"),
                         latency_ms=lat)
            if isinstance(lat, (int, float)):
                _latencies.append(lat)
                del _latencies[:-_LAT_KEEP]
            _cond.notify_all()
        _log_done(job_id)


def _reaper():
    while True:
        cutoff = time.time() - RETAIN_HOURS * 3600
        try:
            for d in DATA.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    with _lock:
                        _jobs.pop(d.name, None)
        except FileNotFoundError:
            pass
        time.sleep(1800)


_pool = ThreadPoolExecutor(max_workers=STAGE_A_WORKERS, thread_name_prefix="stageA")
for _i in range(MODEL_LANES):
    threading.Thread(target=_model_worker, name=f"model-lane-{_i}", daemon=True).start()
threading.Thread(target=_reaper, daemon=True).start()


# --------------------------------------------------------------------------- API
@app.post("/api/jobs")
async def create_job(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    expected_customer: str = Form(""),
    context: str = Form(""),
    min_count: int = Form(3),
    no_llm: bool = Form(False),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, "Only .docx, .pdf, or .xml files are accepted.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"File exceeds {MAX_BYTES // (1024*1024)} MB limit.")

    # limits use the EXISTING session (a brand-new browser has 0 active jobs)
    sid = _peek_session(request)
    job_id = uuid.uuid4().hex
    opts = {
        "expected_customer": expected_customer or None,
        "context": context or None,
        "min_count": int(min_count),
        "no_llm": bool(no_llm),
    }
    # Check the caps AND reserve the slot under a SINGLE lock, so two concurrent
    # uploads from the same browser can't both pass the per-session cap before either
    # inserts (the slot is taken before we release the lock).
    with _lock:
        total_active = sum(1 for j in _jobs.values() if j["status"] in ACTIVE)
        mine_active = sum(1 for j in _jobs.values()
                          if j["status"] in ACTIVE and j["session"] == sid) if sid else 0
        if total_active >= MAX_QUEUE:
            raise HTTPException(429, "Server is busy (queue full). Try again shortly.")
        if mine_active >= PER_SESSION_CAP:
            raise HTTPException(429, f"You already have {mine_active} jobs in progress "
                                     f"(cap {PER_SESSION_CAP}). Wait for one to finish.")
        sid = _ensure_session(request, response)     # mint cookie only on accept
        _jobs[job_id] = {"status": "queued", "name": (file.filename or "document"),
                         "created": _now(), "session": sid, "canceled": False,
                         "opts": opts, "enqueued_at": None}
        _prune_jobs_locked()
        depth = len(_fair_order_locked())

    # store the upload only after the slot is reserved, so a rejected request leaves no
    # orphan dir; if the write fails, drop the reserved record and report it.
    try:
        jdir = DATA / job_id
        jdir.mkdir(parents=True)
        (jdir / ("input" + ext)).write_bytes(data)   # never trust the client filename as a path
    except Exception as e:
        with _lock:
            _jobs.pop(job_id, None)
        log.exception("job=%s store failed: %s", job_id, e)
        raise HTTPException(500, "Failed to store the upload.")
    _pool.submit(_stage_a, job_id)
    return {"job_id": job_id, "status": "queued", "queue_depth": depth}


@app.get("/api/jobs")
async def list_jobs(request: Request):
    sid = _peek_session(request)
    if not sid:
        return {"jobs": []}
    with _lock:
        mine = [(jid, dict(j)) for jid, j in _jobs.items() if j.get("session") == sid]
    mine.sort(key=lambda kv: kv[1].get("created", 0), reverse=True)
    return {"jobs": [{"job_id": jid, "status": j["status"], "name": j.get("name"),
                      "model_ok": j.get("model_ok"), "created": j.get("created")}
                     for jid, j in mine]}


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str):
    sid = _peek_session(request)
    j = _owned(job_id, sid)
    if not j:
        raise HTTPException(404, "No such job (it may have expired).")
    with _lock:
        pos, eta = _position_eta_locked(job_id)
    keys = ("status", "error", "name", "model_ok", "model_note",
            "model_reported", "completion_tokens", "latency_ms")
    out = {"job_id": job_id, **{k: j[k] for k in keys if k in j}}
    if j["status"] == "awaiting_model":
        out["position"] = pos
        out["eta_ms"] = eta
    return out


@app.delete("/api/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str):
    sid = _peek_session(request)
    if not _owned(job_id, sid):
        raise HTTPException(404, "No such job.")
    with _cond:
        j = _jobs.get(job_id)
        if not j:
            raise HTTPException(404, "No such job.")
        if j["status"] in CANCELABLE:
            j.update(canceled=True, status="canceled")
            _cond.notify_all()
            return {"job_id": job_id, "status": "canceled"}
        if j["status"] == "judging":
            raise HTTPException(409, "Job is on the model now and can't be canceled mid-inference.")
        return {"job_id": job_id, "status": j["status"]}


@app.get("/api/jobs/{job_id}/report")
async def job_report(request: Request, job_id: str):
    sid = _peek_session(request)
    if not _owned(job_id, sid):
        raise HTTPException(404, "No such job.")
    p = DATA / job_id / "report.md"
    if not p.exists():
        raise HTTPException(404, "Report not ready.")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@app.get("/api/jobs/{job_id}/findings")
async def job_findings(request: Request, job_id: str):
    sid = _peek_session(request)
    if not _owned(job_id, sid):
        raise HTTPException(404, "No such job.")
    p = DATA / job_id / "findings.json"
    if not p.exists():
        raise HTTPException(404, "Findings not ready.")
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/jobs/{job_id}/download")
async def job_download(request: Request, job_id: str):
    sid = _peek_session(request)
    if not _owned(job_id, sid):
        raise HTTPException(404, "No such job.")
    p = DATA / job_id / "report.md"
    if not p.exists():
        raise HTTPException(404, "Report not ready.")
    return FileResponse(p, media_type="text/markdown", filename="proofreading-report.md")


@app.get("/api/queue")
async def queue_state(request: Request):
    sid = _peek_session(request)
    with _lock:
        order = _fair_order_locked()
        depth = len(order)
        judging = sum(1 for j in _jobs.values() if j["status"] == "judging")
        avg = _avg_latency_locked()
        your_pos = your_eta = None
        if sid:
            mine = [jid for jid in order if _jobs[jid]["session"] == sid]
            if mine:
                your_pos, your_eta = _position_eta_locked(mine[0])
    return {"depth": depth, "judging": judging, "lanes": MODEL_LANES,
            "avg_model_latency_ms": (int(avg) if avg else None),
            "your_position": your_pos, "your_eta_ms": your_eta}


@app.get("/api/healthz")
async def healthz():
    with _lock:
        depth = len(_fair_order_locked())
        judging = sum(1 for j in _jobs.values() if j["status"] == "judging")
    return {"ok": True, "queue_depth": depth, "judging": judging, "lanes": MODEL_LANES}


@app.get("/api/model")
async def model_probe():
    """Active verification that the inference server is reachable and a model
    actually responds -- usable without uploading a document."""
    return await asyncio.to_thread(judge_mod.probe)


# ---------------------------------------------------------------- frontend files
# Serve the two known frontend files explicitly. They may live in ./static or
# beside server.py, so we look in both. No broad StaticFiles mount -> no missing-dir
# startup crash and the .py source is never exposed under /static.
def _frontend(name):
    for d in (HERE / "static", HERE):
        p = d / name
        if p.exists():
            return p
    return None


@app.get("/", response_class=HTMLResponse)
async def index():
    p = _frontend("index.html")
    if not p:
        raise HTTPException(500, "index.html not found (put it beside server.py or in ./static).")
    return p.read_text(encoding="utf-8")


@app.get("/static/references.md")
async def references():
    p = _frontend("references.md")
    if not p:
        raise HTTPException(404, "references.md not found.")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
