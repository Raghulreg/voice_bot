import json
from pathlib import Path

d = json.loads(Path("call_data", "072a0627-7771-43c9-b944-bb14916c570e.json").read_text(encoding="utf-8"))
print("status", d.get("status"), "ended", d.get("ended_at"), "created", d.get("created_at"))
print("duration", d.get("duration_seconds"), "call_sid", (d.get("call_sid") or "")[:8])
print("public keys", sorted(k for k in d if "url" in k.lower() or "public" in k.lower() or k in ("ivr", "latency_summary")))
print("latency", d.get("latency_summary"))
for t in d.get("transcript") or []:
    print("--", t.get("role"), t.get("stage"), repr((t.get("text") or "")[:180]))
    print("   keys", sorted(t.keys()))
