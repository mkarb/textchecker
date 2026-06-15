"""Hand the deterministic findings to a local Qwen (OpenAI-compatible API) and
get back a structured judgment: the final acronym table, confirmed misspellings,
the resolved customer name. This is judgment over a curated list -- no tools, no
re-scanning the document.

Slots into the Qwen-Agent runner from the architecture doc: swap this single
chat call for an Assistant turn when you want the model to be able to call
glossary_lookup / standard_definition mid-judgment.
"""
from __future__ import annotations
import json
import os
import time

import requests

SYSTEM = (
    "You are a technical-manual proofreading assistant.\n"
    "You are given the OUTPUT of deterministic scans already run on a document -- NOT the\n"
    "document itself. Do not invent issues unsupported by the inputs. Use judgment to:\n"
    "- Build a clean acronym table from existing definitions and from recurring long phrases\n"
    "  (propose sensible acronyms; mark each 'existing' or 'proposed'). Skip product names,\n"
    "  part numbers, and sentence fragments -- only genuine multi-word technical terms.\n"
    "- Resolve acronym problems (same acronym/different meanings; used but undefined).\n"
    "- For spelling candidates, KEEP only genuine misspellings; DISCARD domain terminology,\n"
    "  part numbers, and proper nouns.\n"
    "- For organizations, name the single primary customer and flag any name that looks like a\n"
    "  different or leftover customer.\n"
    "Return ONLY valid minified JSON -- no prose, no markdown fences -- matching this schema:\n"
    '{"acronym_table":[{"acronym":"","expansion":"","status":"existing|proposed","note":""}],'
    '"acronym_issues":[{"acronym":"","problem":"","note":""}],'
    '"misspellings":[{"word":"","suggestion":"","context":""}],'
    '"customer":{"primary":"","normalize":[""],"suspected_wrong":[""]}}'
)

# The same schema, machine-enforced. Passed as Ollama's `format`, the server constrains
# decoding so the model PHYSICALLY cannot return anything else -- which eliminates both
# malformed JSON and the failure mode where the model echoes the input findings keys
# back instead of transforming them. additionalProperties:false is what blocks the echo.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "acronym_table": {"type": "array", "items": {
            "type": "object",
            "properties": {"acronym": {"type": "string"}, "expansion": {"type": "string"},
                           "status": {"type": "string", "enum": ["existing", "proposed"]},
                           "note": {"type": "string"}},
            "required": ["acronym", "expansion", "status"],
            "additionalProperties": False}},
        "acronym_issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"acronym": {"type": "string"}, "problem": {"type": "string"},
                           "note": {"type": "string"}},
            "required": ["acronym", "problem"],
            "additionalProperties": False}},
        "misspellings": {"type": "array", "items": {
            "type": "object",
            "properties": {"word": {"type": "string"}, "suggestion": {"type": "string"},
                           "context": {"type": "string"}},
            "required": ["word", "suggestion"],
            "additionalProperties": False}},
        "customer": {"type": "object",
                     "properties": {"primary": {"type": "string"},
                                    "normalize": {"type": "array", "items": {"type": "string"}},
                                    "suspected_wrong": {"type": "array", "items": {"type": "string"}}},
                     "required": ["primary"],
                     "additionalProperties": False},
    },
    "required": ["acronym_table", "acronym_issues", "misspellings", "customer"],
    "additionalProperties": False,
}


def _shrink_findings(findings, budget=60000):
    """Size-bound the findings payload WITHOUT ever corrupting it. The old
    json.dumps(...)[:60000] sliced mid-token, silently feeding the model broken JSON on
    large manuals. This drops the bulkiest, least-needed fields first and always emits
    valid JSON; what was elided is reported so the provenance block can disclose it.
    Returns (json_str, shrink_meta_or_None). existing_acronyms is never reduced."""
    payload = {k: v for k, v in findings.items() if not str(k).startswith("_")}
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= budget:
        return s, None
    import copy
    p = copy.deepcopy(payload)
    steps = []

    def out():
        return json.dumps(p, ensure_ascii=False)

    for key in ("recurring_phrases", "acronym_issues", "spelling_candidates"):
        for r in p.get(key) or []:
            if isinstance(r, dict):
                r.pop("locations", None)
    steps.append("dropped per-item locations")
    if len(out()) > budget:
        for r in p.get("spelling_candidates") or []:
            if isinstance(r, dict) and isinstance(r.get("context"), str):
                r["context"] = r["context"][:60]
        steps.append("trimmed spelling contexts")
    caps = {"recurring_phrases": 60, "spelling_candidates": 80, "acronym_issues": 80}
    while len(out()) > budget:
        shrunk = False
        for key, floor in (("recurring_phrases", 10), ("spelling_candidates", 10),
                           ("acronym_issues", 10)):
            lst = p.get(key)
            if isinstance(lst, list) and len(lst) > caps.get(key, floor):
                if key == "recurring_phrases":
                    lst.sort(key=lambda r: -(r.get("count") or 0) if isinstance(r, dict) else 0)
                p[key] = lst[:caps[key]]
                steps.append(f"capped {key} at {caps[key]}")
                caps[key] = max(floor, caps[key] // 2)
                shrunk = True
                if len(out()) <= budget:
                    break
        if not shrunk:
            break                       # floor reached: send valid JSON even if over budget
    final = out()
    return final, {"original_chars": len(s), "sent_chars": len(final), "steps": steps}


def _call_native(native_url, model, messages, think_off, max_tokens, timeout, fmt="json"):
    """Ollama's native /api/chat. Unlike the /v1 OpenAI-compat endpoint, this one
    actually honours think:false -- on /v1, current Ollama builds ignore every thinking
    switch, so reasoning keeps eating the token budget. Returns the common 5-tuple."""
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": (not think_off),          # False => reasoning disabled (real effect here)
        "format": fmt,                      # "json" or a full JSON schema (constrained decoding)
        "options": {"temperature": 0, "num_predict": max_tokens},
    }
    ctx = os.environ.get("PROOFER_NUM_CTX")
    if ctx:
        body["options"]["num_ctx"] = int(ctx)
    r = requests.post(native_url, json=body, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    msg = d.get("message") or {}
    usage = {"prompt_tokens": d.get("prompt_eval_count"),
             "completion_tokens": d.get("eval_count"),
             "total_tokens": (d.get("prompt_eval_count") or 0) + (d.get("eval_count") or 0)}
    return msg.get("content", ""), d.get("done_reason"), usage, d.get("model"), native_url


def _call_openai(base_url, model, messages, think_off, max_tokens, timeout):
    """OpenAI-compatible /chat/completions (vLLM, llama.cpp server, non-Ollama).
    Thinking-off here is best-effort: chat_template_kwargs (vLLM honours it) plus the
    /no_think already appended to the user turn."""
    body = {"model": model, "temperature": 0, "stream": False,
            "max_tokens": max_tokens,
            "messages": messages}
    if think_off:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    r = requests.post(f"{base_url}/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    choice = (d.get("choices") or [{}])[0]
    return ((choice.get("message") or {}).get("content", ""),
            choice.get("finish_reason"), d.get("usage", {}), d.get("model"),
            f"{base_url}/chat/completions")


def _validate_judgment(result):
    """Structural validation ONLY -- required keys, container types, enum, non-empty
    required strings -- mirroring OUTPUT_SCHEMA. Deliberately NOT semantic: rules like
    "expansion initials must match the acronym" misfire on legitimate entries (MX for
    maintenance, nested acronyms), and a false validation error triggers a repair round
    that can mutate a perfectly good judgment. Code validates; the model only repairs.
    Matters most on the /v1 fallback and PROOFER_SCHEMA=0 paths, where no decoder
    grammar protects the shape. Returns a bounded list of error strings."""
    errs = []
    if not isinstance(result, dict):
        return ["top-level value is not a JSON object"]
    for key, typ in (("acronym_table", list), ("acronym_issues", list),
                     ("misspellings", list), ("customer", dict)):
        if key not in result:
            errs.append(f"missing required key: {key}")
        elif not isinstance(result[key], typ):
            errs.append(f"{key} must be a JSON {'array' if typ is list else 'object'}")
    for i, row in enumerate(result.get("acronym_table") or []):
        if not isinstance(row, dict):
            errs.append(f"acronym_table[{i}] is not an object")
            continue
        for req in ("acronym", "expansion", "status"):
            if not isinstance(row.get(req), str) or not row.get(req, "").strip():
                errs.append(f"acronym_table[{i}].{req} missing or empty")
        if isinstance(row.get("status"), str) and row["status"] not in ("existing", "proposed"):
            errs.append(f"acronym_table[{i}].status must be 'existing' or 'proposed', got {row['status']!r}")
    for i, row in enumerate(result.get("misspellings") or []):
        if not isinstance(row, dict):
            errs.append(f"misspellings[{i}] is not an object")
            continue
        if not isinstance(row.get("word"), str) or not row.get("word", "").strip():
            errs.append(f"misspellings[{i}].word missing or empty")
    cust = result.get("customer")
    if isinstance(cust, dict) and not isinstance(cust.get("primary", ""), str):
        errs.append("customer.primary must be a string")
    return errs[:12]                       # bound the repair prompt


def judge(findings, base_url=None, model=None, timeout=180, context=None):
    base_url = base_url or os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
    model = model or os.environ.get("MODEL", "qwen3:30b-a3b")
    max_tokens = int(os.environ.get("PROOFER_MAX_TOKENS", "8000"))
    think_off = os.environ.get("PROOFER_THINK", "").lower() not in ("1", "true", "yes")

    # Send only the deterministic findings, never the document text. Internal keys
    # ("_"-prefixed, e.g. _doc_text) are stripped, and oversized findings are shrunk
    # structurally (valid JSON always) instead of char-sliced mid-token.
    user_json, shrunk = _shrink_findings(findings)
    user = "Deterministic scan output:\n" + user_json
    if context:
        user = ("Document context provided by the author (use it to judge domain terms, the "
                "correct acronym expansions, and the primary customer):\n"
                + context.strip()[:4000] + "\n\n" + user)
    if think_off:
        user += "\n\n/no_think"             # best-effort for the OpenAI-compat fallback path
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    # Default to Ollama's native endpoint -- it's the only one that actually turns
    # thinking off. Derive it from the configured /v1 base. Set PROOFER_OLLAMA_NATIVE=0
    # for a non-Ollama OpenAI-compatible server; native transport errors also fall back.
    use_native = os.environ.get("PROOFER_OLLAMA_NATIVE", "1").lower() not in ("0", "false", "no")
    native_url = base_url.split("/v1")[0].rstrip("/") + "/api/chat"

    # Constrained decoding: pass the full output schema as Ollama's `format` so the
    # model cannot emit malformed JSON or echo the input keys. PROOFER_SCHEMA=0 reverts
    # to plain "json" mode (e.g. for servers that predate schema support).
    schema_on = os.environ.get("PROOFER_SCHEMA", "1").lower() not in ("0", "false", "no")
    fmt = OUTPUT_SCHEMA if schema_on else "json"

    t0 = time.time()
    served_native = use_native

    def _transport(msgs, mt):
        nonlocal served_native
        if served_native:
            try:
                return _call_native(native_url, model, msgs, think_off, mt, timeout, fmt)
            except (requests.RequestException, ValueError):
                served_native = False       # unreachable OR non-JSON body -> fall back
        return _call_openai(base_url, model, msgs, think_off, mt, timeout)

    retry_on = os.environ.get("PROOFER_RETRY", "1").lower() not in ("0", "false", "no")
    retries = {}
    content, finish, usage, model_reported, endpoint = _transport(messages, max_tokens)
    result = _parse_json(content)

    if retry_on and "_parse_error" in result and finish in ("length", "limit"):
        # Truncated mid-JSON: one retry with double the output budget.
        bigger = min(max_tokens * 2, 16000)
        retries["truncation"] = {"num_predict": [max_tokens, bigger]}
        content2, finish2, usage2, mr2, ep2 = _transport(messages, bigger)
        r2 = _parse_json(content2)
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["truncation"]["recovered"] = True
    elif retry_on and "_parse_error" in result:
        # Malformed despite a normal finish: one repair round. With the schema
        # constraint on this is nearly unreachable, but it costs one branch.
        repair = messages + [
            {"role": "assistant", "content": content[:6000]},
            {"role": "user", "content": "That reply was not valid JSON for the required "
                                        "schema. Return ONLY the corrected JSON object -- "
                                        "no prose, no fences."}]
        retries["repair"] = {"attempted": True}
        content2, finish2, usage2, mr2, ep2 = _transport(repair, max_tokens)
        r2 = _parse_json(content2)
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["repair"]["recovered"] = True
    elif retry_on and "_parse_error" not in result:
        # Parsed fine -- now CODE validates the shape; the model only ever repairs.
        verrs = _validate_judgment(result)
        if verrs:
            repair = messages + [
                {"role": "assistant", "content": content[:6000]},
                {"role": "user", "content": "Your JSON parsed but failed validation:\n- "
                                            + "\n- ".join(verrs)
                                            + "\nReturn ONLY the corrected JSON object -- "
                                              "no prose, no fences."}]
            retries["validation"] = {"attempted": True, "errors": verrs}
            content2, finish2, usage2, mr2, ep2 = _transport(repair, max_tokens)
            r2 = _parse_json(content2)
            if "_parse_error" not in r2 and not _validate_judgment(r2):
                content, finish, usage, model_reported, endpoint, result = (
                    content2, finish2, usage2, mr2, ep2, r2)
                retries["validation"]["recovered"] = True
            # else: KEEP the original parsed result -- partially valid beats discarded;
            # render_md is defensive (.get) and the attempt is disclosed in _meta.
    # Server-attested provenance: model name + completion_tokens come from the server,
    # so non-zero completion_tokens with a model name proves the model generated this.
    result["_meta"] = {
        "model_requested": model,
        "model_reported": model_reported,
        "base_url": endpoint,
        "usage": usage,
        "finish_reason": finish,
        "response_chars": len(content),
        "latency_ms": round((time.time() - t0) * 1000),
        "schema_constrained": bool(schema_on and served_native),
    }
    if shrunk:
        result["_meta"]["findings_shrunk"] = shrunk
    if retries:
        result["_meta"]["retries"] = retries
    if "_parse_error" in result and finish in ("length", "limit"):
        result["_parse_error"] = (
            "model output was truncated at the token limit (finish_reason=%s); the JSON was "
            "incomplete. Trim the candidate lists/scope, confirm thinking is off, or raise the "
            "model's context window." % finish)
    return result


def _parse_json(text):
    """Tolerant: strips code fences and any <think> preamble by slicing to the
    outermost JSON object."""
    t = text.strip()
    if "{" in t and "}" in t:
        t = t[t.find("{"): t.rfind("}") + 1]
    try:
        obj = json.loads(t)
    except Exception as e:
        return {"_parse_error": str(e), "_raw": text[:2000]}
    if not isinstance(obj, dict):
        # a top-level array/string would crash result["_meta"] = ... downstream
        return {"_parse_error": "model returned non-object JSON (top-level %s)"
                                % type(obj).__name__, "_raw": text[:2000]}
    return obj


def probe(base_url=None, model=None, timeout=20):
    """Actively verify the inference server: which models it offers, plus a tiny
    chat round-trip. Returns a dict safe to expose via an API/diagnostics route."""
    base_url = base_url or os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
    model = model or os.environ.get("MODEL", "qwen3:30b-a3b")
    out = {"base_url": base_url, "model_configured": model, "reachable": False}
    try:
        m = requests.get(f"{base_url}/models", timeout=5)
        m.raise_for_status()
        out["models_available"] = [x.get("id") for x in m.json().get("data", [])]
        out["reachable"] = True
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        t0 = time.time()
        r = requests.post(f"{base_url}/chat/completions", json={
            "model": model, "temperature": 0, "stream": False, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
        }, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        out.update({
            "model_reported": d.get("model"),
            "sample_reply": ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()[:80],
            "usage": d.get("usage", {}),
            "latency_ms": round((time.time() - t0) * 1000),
            "chat_ok": True,
        })
    except Exception as e:
        out["chat_ok"] = False
        out["chat_error"] = str(e)
    return out
