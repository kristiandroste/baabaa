"""A tiny MCP server over stdio for tests: counts sheep and reports the weather on a hill."""
import json
import sys

TOOLS = [
    {"name": "count_sheep", "description": "Count the sheep in a field and return the number.",
     "inputSchema": {"type": "object", "properties": {"field": {"type": "string"}}, "required": ["field"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "shear_sheep", "description": "Shear a named sheep (changes its state).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, id_ = msg.get("method"), msg.get("id")
    if id_ is None:
        continue  # a notification
    if method == "initialize":
        reply(id_, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "sheep", "version": "1"}})
    elif method == "tools/list":
        reply(id_, {"tools": TOOLS})
    elif method == "tools/call":
        p = msg.get("params") or {}
        a = p.get("arguments") or {}
        if p.get("name") == "count_sheep":
            n = 7 + len(a.get("field", "")) % 5
            reply(id_, {"content": [{"type": "text", "text": f"There are {n} sheep in {a.get('field')}."}]})
        elif p.get("name") == "shear_sheep":
            reply(id_, {"content": [{"type": "text", "text": f"{a.get('name')} is now shorn."}]})
        else:
            reply(id_, None, {"code": -32602, "message": "unknown tool"})
    else:
        reply(id_, None, {"code": -32601, "message": "method not found"})
