"""Section Z: deployment on a Hugging Face Space, and the contract of all 20 agents (Phase 10).

The Space listens on 0.0.0.0:7860 (its app_port), runs as a non-root user, bakes no secret into the image, and starts even
when no model can be fetched. Every one of the 20 agents
returns a structured answer for ordinary prompts: text when it can answer, and a plain reason when it cannot. No agent
raises, and none returns an empty reply.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from galaxy import auth
from galaxy.memory import MemoryStore
from galaxy.orchestrator import AGENT_REGISTRY, Orchestrator

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ["calculate 144/12", "what is photosynthesis"]


class _Url:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class _Request:
    def __init__(self, scheme: str, headers=None) -> None:
        self.url = _Url(scheme)
        self.headers = headers or {}


async def run(check) -> None:
    dockerfile = (ROOT / "deploy" / "huggingface" / "Dockerfile").read_text(encoding="utf-8")
    check("EXPOSE 7860" in dockerfile and "PORT=7860" in dockerfile and "HOST=0.0.0.0" in dockerfile,
          "Z-docker: the server listens on 0.0.0.0:7860, the Space's app port")
    check("USER user" in dockerfile and "uid" not in dockerfile.lower().replace("-u 1000", ""),
          "Z-docker: the container runs as a non-root user")
    check("GALAXY_AUTH" not in dockerfile, "Z-docker: the image sets no access mode: the page opens straight in")
    check("GALAXY_API_TOKEN=" not in dockerfile and "sk-" not in dockerfile,
          "Z-docker: no secret is baked into the image")
    check('CMD ["sh", "-c", "python scripts/fetch_model.py; exec python run.py"]' in dockerfile,
          "Z-docker: the model fetch never blocks the start; the engine always starts")
    check("llama-cpp-python" in dockerfile and "|| echo" in dockerfile,
          "Z-docker: a failed local-model build cannot stop the Space")
    readme = (ROOT / "deploy" / "huggingface" / "README.md").read_text(encoding="utf-8")
    check(readme.startswith("---") and "sdk: docker" in readme and "app_port: 7860" in readme,
          "Z-space: the Space card declares the docker SDK and port 7860")
    requirements = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")]
    check("fastapi" in requirements and "uvicorn" in requirements and not any(r.startswith("llama") for r in requirements),
          "Z-requirements: the runtime is listed; the optional model engine is built separately")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    check("logs" in ignore and "*.db" in ignore, "Z-docker: logs and databases stay out of the image")

    api_source = (ROOT / "galaxy" / "api.py").read_text(encoding="utf-8")
    check("Set-Cookie" not in api_source and "set_cookie" not in api_source and "is_https" not in api_source,
          "Z-cookie: the server sends no cookies, so there is no session cookie to protect")

    env = {k: v for k, v in os.environ.items() if not k.startswith("GALAXY_GGUF")}
    skipped = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_model.py")], capture_output=True, text=True,
                             env={**env, "GALAXY_GGUF_REPO": ""}, timeout=60)
    check(skipped.returncode == 0 and "skipped" in skipped.stdout, "Z-model: with no repo configured the fetch is skipped")
    offline = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_model.py")], capture_output=True, text=True,
                             env={**env, "GALAXY_GGUF_REPO": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                                  "GALAXY_GGUF_FILE": "x.gguf", "GALAXY_GGUF_PATH": str(Path(tempfile.mkdtemp()) / "m.gguf"),
                                  "HF_HUB_OFFLINE": "1", "GALAXY_SKIP_LLM": "0"}, timeout=120)
    check(offline.returncode == 0 and "heuristics mode" in offline.stdout and "Traceback" not in offline.stderr,
          "Z-model: a failed download never stops the start-up; the engine says it is in heuristics mode")

    check(len(AGENT_REGISTRY) == 20, "Z-agents: the registry has exactly 20 agents")
    store = MemoryStore(Path(tempfile.mkdtemp(prefix="hf_z_")) / "z.db")
    orch = Orchestrator(store)
    check({a[2] for a in AGENT_REGISTRY} - {"orchestrator"} == set(orch.agents) and len(orch.agents) == 19,
          "Z-agents: 19 sub-agents are built by key; the 20th, the orchestrator, routes every request itself")
    orchestrator_plan = await orch._run_section("orchestrator", "calculate 144/12 and what is photosynthesis", {}, "standard")
    check(orchestrator_plan.ok and "Routing plan" in orchestrator_plan.text,
          "Z-agents: the orchestrator answers with the routing it applies to a request")
    ctx = {"user_id": None, "history": []}
    bad = []
    for _, name, key in AGENT_REGISTRY:
        for prompt in PROMPTS:
            try:
                result = await asyncio.wait_for(orch._run_section(key, prompt, ctx, "standard"), 90)
            except Exception as exc:
                bad.append(f"{key}: raised {type(exc).__name__}")
                continue
            text = result.text if result.ok else None
            if result.ok and not (isinstance(text, str) and text.strip()):
                bad.append(f"{key}: ok but empty")
            elif not result.ok and not (isinstance(result.error, str) and result.error.strip()):
                bad.append(f"{key}: failed without a reason")
            elif "(no answer)" in ((text or "") + (result.error or "")):
                bad.append(f"{key}: printed '(no answer)'")
    check(not bad, f"Z-agents: all 20 agents answer or say why, for {len(PROMPTS)} prompts ({bad[:4]})")
