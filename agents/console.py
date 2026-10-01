"""Read-only web console served by the monitor: Incidents and their Remediations."""
from __future__ import annotations

import json
import time

from aiops import crd

_CACHE_SECONDS = 3.0


def console_routes(store: crd.Store) -> dict:
    cache: dict = {"at": 0.0, "body": b"{}"}

    def state() -> tuple[str, bytes]:
        if time.time() - cache["at"] > _CACHE_SECONDS:
            rems = {r["metadata"]["name"]: r for r in store.list("remediations")}
            incidents = []
            for inc in sorted(store.list("incidents"), key=lambda i: i["metadata"]["creationTimestamp"],
                              reverse=True)[:60]:
                rem = rems.get(inc["metadata"]["name"])
                incidents.append({
                    "name": inc["metadata"]["name"],
                    "created": inc["metadata"]["creationTimestamp"],
                    "key": inc["spec"]["key"],
                    "symptom": inc["spec"]["symptom"],
                    "phase": crd.phase(inc),
                    "message": (inc.get("status") or {}).get("message", ""),
                    "remediation": None if rem is None else {
                        "phase": crd.phase(rem), "source": rem["spec"].get("source"),
                        "action": rem["spec"].get("action"), "params": rem["spec"].get("params"),
                        "summary": rem["spec"].get("summary"), "evaluation": rem["spec"].get("evaluation"),
                        "history": (rem.get("status") or {}).get("history", []),
                    },
                })
            lessons = [{"name": l["metadata"]["name"], **{k: l["spec"].get(k) for k in
                        ("symptom", "target", "action", "params", "verifiedAt")}}
                       for l in store.list("lessons")]
            cache.update(at=time.time(), body=json.dumps(
                {"incidents": incidents, "lessons": lessons}, ensure_ascii=False).encode("utf-8"))
        return "application/json; charset=utf-8", cache["body"]

    return {"/": lambda: ("text/html; charset=utf-8", _PAGE.encode("utf-8")), "/api/state": state}


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AIOps v2</title>
<style>
:root { color-scheme: light; --bg:#f4f3f0; --card:#fcfcfb; --border:#e4e2dc; --text:#0b0b0b; --text2:#52514e;
  --muted:#6f6d68; --good:#0ca30c; --warn:#fab219; --bad:#d03b3b; --accent:#2a78d6; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg:#121211; --card:#1a1a19; --border:#2e2e2b;
  --text:#fff; --text2:#c3c2b7; --muted:#9a998f; --accent:#3987e5; } }
* { box-sizing: border-box; } body { margin:0; font:14px/1.5 system-ui,"Noto Sans TC",sans-serif; background:var(--bg); color:var(--text); }
main { max-width:1100px; margin:0 auto; padding:20px 14px 40px; } h1 { font-size:20px; margin:0 0 2px; }
.sub { color:var(--text2); font-size:13px; margin:0 0 16px; }
.inc { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px 14px; margin-bottom:8px; }
.row { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; } .key { font-weight:600; word-break:break-all; }
.badge { display:inline-flex; gap:5px; align-items:center; font-size:12px; color:var(--text); }
.badge i { width:8px; height:8px; border-radius:50%; display:inline-block; }
.meta { color:var(--muted); font-size:12px; } .msg { color:var(--text2); font-size:13px; }
.flow { font-size:12px; color:var(--text2); margin-top:4px; } .flow b { color:var(--text); font-weight:600; }
details { margin-top:4px; font-size:12px; color:var(--text2); } pre { white-space:pre-wrap; margin:4px 0; }
h2 { font-size:15px; margin:22px 0 8px; } table { border-collapse:collapse; width:100%; font-size:13px; }
td, th { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border); } th { color:var(--muted); font-weight:500; }
</style></head><body><main>
<h1>AIOps v2 — incidents</h1>
<p class="sub">monitor → diagnose → repair → validate, handed over as Kubernetes resources · refreshes every 10 s</p>
<div id="list"></div><h2>Lessons (verified fixes)</h2><table id="lessons"></table>
</main><script>
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const COLOR = {Resolved:"var(--good)", Validated:"var(--good)", Unresolved:"var(--warn)", Recommended:"var(--warn)",
  Rejected:"var(--warn)", RolledBack:"var(--bad)", Failed:"var(--bad)"};
const badge = (p) => `<span class="badge"><i style="background:${COLOR[p] || "var(--accent)"}"></i>${esc(p)}</span>`;
async function load() {
  const st = await (await fetch("/api/state")).json();
  document.getElementById("list").innerHTML = st.incidents.length ? st.incidents.map((i) => {
    const r = i.remediation;
    const flow = r ? `<div class="flow">${esc(r.source)} → <b>${esc(r.action || "recommendation")}</b>
      ${r.params && Object.keys(r.params).length ? esc(JSON.stringify(r.params)) : ""} · remediation ${badge(r.phase)}</div>
      <details><summary>history & evaluation</summary><pre>${esc(r.history.map((h) => h.time + "  " + h.phase + "  " + h.message).join("\n"))}</pre>
      ${r.evaluation && Object.keys(r.evaluation).length ? `<pre>${esc(JSON.stringify(r.evaluation, null, 1))}</pre>` : ""}</details>` : "";
    return `<div class="inc"><div class="row"><span class="key">${esc(i.key)}</span>${badge(i.phase)}
      <span class="meta">${esc(i.name)} · ${new Date(i.created).toLocaleString()}</span></div>
      <div class="msg">${esc(i.message)}</div>${flow}</div>`; }).join("") : '<p class="sub">No incidents yet.</p>';
  document.getElementById("lessons").innerHTML = "<tr><th>workload</th><th>symptom</th><th>fix</th><th>verified</th></tr>" +
    st.lessons.map((l) => `<tr><td>${esc(l.target.namespace)}/${esc(l.target.deployment)}</td><td>${esc(l.symptom)}</td>
      <td>${esc(l.action)} ${esc(JSON.stringify(l.params))}</td><td>${esc(new Date(l.verifiedAt).toLocaleString())}</td></tr>`).join("");
}
load(); setInterval(load, 10000);
</script></body></html>
"""
