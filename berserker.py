"""berserker.py
Adversarial Red Teaming Agent. Uses Gemma 4 to silently sabotage core.py.
"""
import shutil
import requests
import re
from rich.console import Console

console = Console()
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:e2b" # Adjust to your exact local tag
TARGET_FILE = "sandbox/core.py"
BACKUP_FILE = "sandbox/core.py.bak"

PROMPT = """You are 'Berserker', an elite adversarial AI chaos agent.
Your mission is to silently compromise the following Python orchestration file.
Inject a subtle logical bug (a "contract breach") into the `execute` method of `GatewayEngine` or the `classify_error` function. 
It MUST NOT cause a SyntaxError. It must pass basic compilation but cause the integration test suite to fail (e.g. return wrong HTTP status codes, ignore errors).
Output ONLY the modified Python code inside a ```python block.

CODE:
{code}
"""

def inject_chaos():
    console.print(f"[bold red]☠️  \\[Berserker] Creating backup of {TARGET_FILE}...[/]")
    shutil.copy(TARGET_FILE, BACKUP_FILE)
    
    with open(TARGET_FILE, "r") as f:
        code = f.read()
        
    console.print("[bold red]☠️  \\[Berserker] Generating adversarial semantic mutation via Gemma 4...[/]")
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(code=code)}],
        "stream": False
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload).json()
        mutated_text = response.get("message", {}).get("content", "")
        
        match = re.search(r"```python\n(.*?)```", mutated_text, re.DOTALL)
        if match:
            with open(TARGET_FILE, "w") as f:
                f.write(match.group(1))
            console.print("[bold red]☠️  \\[Berserker] Cut Vertex compromised successfully.[/]")
        else:
            raise ValueError("No code block generated.")
    except Exception as e:
        console.print("[bold yellow]⚠️ \\[Berserker] LLM generation failed, falling back to manual AST sabotage...[/]")
        # Fallback: subtly break error classification
        mutated = code.replace("return status", "return 200  # Berserker sabotage")
        with open(TARGET_FILE, "w") as f:
            f.write(mutated)
        console.print("[bold red]☠️  \\[Berserker] Fallback mutation applied.[/]")

if __name__ == "__main__":
    inject_chaos()
