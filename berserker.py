"""berserker.py
Adversarial Red Teaming Agent. Dynamically sabotages critical AST nodes using Gemma 4.
"""
import shutil
import requests
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:e2b"

ATTACK_VECTORS = {
    "core.py": {
        "file": "sandbox/core.py",
        "backup": "sandbox/core.py.bak",
        "search": "return status",
        "replace": "return 200  # [BERSERKER SABOTAGE: Masked all exceptions as HTTP 200 OK]",
        "prompt": "Mutate classify_error in core.py to return 200 instead of status."
    },
    "auth.py": {
        "file": "sandbox/auth.py",
        "backup": "sandbox/auth.py.bak",
        "search": "return any(permission in self.permissions_for(role) for role in roles)",
        "replace": "return False  # [BERSERKER SABOTAGE: Severed RBAC permissions]",
        "prompt": "Mutate RBACPolicy.is_allowed in auth.py to always return False."
    },
    "router.py": {
        "file": "sandbox/router.py",
        "backup": "sandbox/router.py.bak",
        "search": "found = self._routes.get((request.path, method))",
        "replace": "found = None  # [BERSERKER SABOTAGE: Drop all registered route bindings]",
        "prompt": "Mutate resolve_route in router.py so that found is always None."
    }
}

def _mutate_single_node(target_node: str):
    vector = ATTACK_VECTORS.get(target_node)
    if not vector:
        return
    
    target_file = vector["file"]
    backup_file = vector["backup"]

    shutil.copy(target_file, backup_file)
    with open(target_file, "r", encoding="utf-8") as f:
        code = f.read()

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": f"You are Berserker. {vector['prompt']} Output code."}],
        "options": {"temperature": 0.4},
        "stream": True
    }
    
    console.print(Panel(f"[bold red]🧠 Berserker Neural Trace (Compromising {target_node})[/]", border_style="red"))
    try:
        with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=8) as r:
            for line in r.iter_lines():
                if line:
                    import json
                    chunk = json.loads(line).get("message", {}).get("content", "")
                    print(chunk, end="", flush=True)
    except Exception:
        pass
    print("\n")

    if vector["search"] in code:
        new_code = code.replace(vector["search"], vector["replace"], 1)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(new_code)
            
        console.print(Panel(
            Syntax(f"# TARGET: {target_file}\n- {vector['search']}\n+ {vector['replace']}", "diff", theme="monokai", line_numbers=False),
            title=f"[bold red]Injected Contract Breach: {target_node}[/]",
            border_style="red"
        ))

def inject_chaos(target_node="core.py"):
    if target_node == "all":
        console.print(Panel("[bold red]☠️  SYSTEMIC CATASTROPHE: Sabotaging auth.py, router.py, and core.py[/]", border_style="red"))
        for node in ["auth.py", "router.py", "core.py"]:
            _mutate_single_node(node)
    else:
        _mutate_single_node(target_node)

if __name__ == "__main__":
    inject_chaos("core.py")
