"""Generate deploy/50-dashboard.yaml (Grafana sidecar picks it up from the monitoring namespace).

    python deploy/dashboards/generate.py && kubectl apply -f deploy/50-dashboard.yaml
"""
import json
import os

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
# Categorical slots 1-4 (dark-surface steps); status colors only for state tiles.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]
GOOD, CRITICAL, WARNING = "#0ca30c", "#d03b3b", "#fab219"


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "gridPos": grid(0, y, 24, 1), "panels": []}


def stat(pid, title, expr, pos, steps=None, desc="", unit="none"):
    return {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": PROM, "gridPos": pos,
            "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "instant": True}],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute",
                                                        "steps": steps or [{"color": "text", "value": None}]}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"}}


def bars(pid, title, targets, pos, interval, colors=None, desc=""):
    overrides = [{"matcher": {"id": "byName", "options": n},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                 for n, c in (colors or {}).items()]
    return {"id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": PROM,
            "gridPos": pos, "interval": interval, "targets": targets,
            "fieldConfig": {"defaults": {"min": 0, "decimals": 0, "color": {"mode": "palette-classic"},
                                         "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 1,
                                                    "stacking": {"mode": "normal"}}},
                            "overrides": overrides},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def logs(pid, title, expr, pos):
    return {"id": pid, "type": "logs", "title": title, "datasource": LOKI, "gridPos": pos,
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"}}


phases = ["applied", "validated", "rejected", "rolled_back"]
panels = [
    row(1, "Now", 0),
    stat(2, "Open incidents", "max(aiops_incidents_open) or vector(0)", grid(0, 1, 4, 4),
         [{"color": GOOD, "value": None}, {"color": WARNING, "value": 1}],
         "Incidents an agent is still working on"),
    stat(3, "Fixes validated (24h)", 'sum(increase(aiops_remediations_total{phase="validated"}[24h])) or vector(0)',
         grid(4, 1, 4, 4)),
    stat(4, "Rolled back (24h)", 'sum(increase(aiops_remediations_total{phase="rolled_back"}[24h])) or vector(0)',
         grid(8, 1, 4, 4), [{"color": "text", "value": None}, {"color": WARNING, "value": 1}]),
    stat(5, "Refused by repair (24h)", 'sum(increase(aiops_remediations_total{phase="rejected"}[24h])) or vector(0)',
         grid(12, 1, 4, 4), desc="Proposals the repair agent would not apply (catalog, allowlist, pre-check)"),
    stat(6, "Lessons", "max(aiops_lessons) or vector(0)", grid(16, 1, 4, 4), desc="Verified fixes kept for reuse"),
    stat(7, "Agents healthy", "count(time() - aiops_agent_last_loop_timestamp_seconds < 120) or vector(0)",
         grid(20, 1, 4, 4), [{"color": CRITICAL, "value": None}, {"color": GOOD, "value": 4}],
         "Agents whose loop ran in the last 2 minutes (of 4)"),
    row(8, "Hand-over", 5),
    bars(9, "Incidents opened (per hour)",
         [{"refId": "A", "datasource": PROM, "legendFormat": "{{symptom}}",
           "expr": "sum by (symptom) (increase(aiops_incidents_created_total[1h]))"}], grid(0, 6, 12, 8), "1h"),
    bars(10, "Remediation outcomes (per hour)",
         [{"refId": p, "datasource": PROM, "legendFormat": p,
           "expr": f'sum(increase(aiops_remediations_total{{phase="{p}"}}[1h])) or vector(0)'} for p in phases],
         grid(12, 6, 12, 8), "1h", dict(zip(phases, SERIES))),
    row(11, "LLM decision quality", 14),
    stat(12, "Judge approved (7d)", 'sum(increase(aiops_judge_total{verdict="passed"}[7d])) or vector(0)',
         grid(0, 15, 4, 4)),
    stat(13, "Judge rejected (7d)", 'sum(increase(aiops_judge_total{verdict="rejected"}[7d])) or vector(0)',
         grid(4, 15, 4, 4), [{"color": "text", "value": None}, {"color": WARNING, "value": 1}]),
    stat(14, "Poisoned results blocked (7d)", "sum(increase(aiops_guard_blocked_total[7d])) or vector(0)",
         grid(8, 15, 4, 4), [{"color": "text", "value": None}, {"color": WARNING, "value": 1}]),
    logs(15, "Nightly simulation (aiops-eval)", '{namespace="aiops-system", container="eval"} |= "sim_summary"',
         grid(12, 15, 12, 4)),
    logs(16, "Decision records (diagnose)", '{namespace="aiops-system", app="aiops-diagnose"} |= "decision "',
         grid(0, 19, 24, 8)),
    row(17, "Agent logs", 27),
    logs(18, "monitor", '{namespace="aiops-system", app="aiops-monitor"}', grid(0, 28, 12, 9)),
    logs(19, "diagnose", '{namespace="aiops-system", app="aiops-diagnose"}', grid(12, 28, 12, 9)),
    logs(20, "repair", '{namespace="aiops-system", app="aiops-repair"}', grid(0, 37, 12, 9)),
    logs(21, "validate", '{namespace="aiops-system", app="aiops-validate"}', grid(12, 37, 12, 9)),
]
dashboard = {"uid": "aiops-v2", "title": "AIOps v2 (multi-agent)", "tags": ["aiops", "kubernetes"],
             "timezone": "browser", "editable": True, "schemaVersion": 39, "refresh": "30s",
             "time": {"from": "now-24h", "to": "now"}, "panels": panels,
             "templating": {"list": []}, "annotations": {"list": []}}

body = "\n".join("    " + line for line in json.dumps(dashboard, ensure_ascii=False, indent=1).splitlines())
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "50-dashboard.yaml")
with open(out, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(f"""# Generated by deploy/dashboards/generate.py
apiVersion: v1
kind: ConfigMap
metadata:
  name: dashboard-aiops-v2
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: AIOps
data:
  aiops-v2.json: |-
{body}
""")
print("wrote", out)
