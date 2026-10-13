#!/usr/bin/env python3
"""Downloads the GGUF model named by GALAXY_GGUF_REPO and GALAXY_GGUF_FILE into GALAXY_GGUF_PATH, once.

It never stops the start-up: without the file the engine runs in heuristics mode and the logs say so. Set
GALAXY_SKIP_LLM=1 to skip it, or leave GALAXY_GGUF_REPO empty.
"""
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    repo = os.environ.get("GALAXY_GGUF_REPO", "").strip()
    name = os.environ.get("GALAXY_GGUF_FILE", "").strip()
    target = Path(os.environ.get("GALAXY_GGUF_PATH", "models/model.gguf"))
    if os.environ.get("GALAXY_SKIP_LLM") == "1":
        print("fetch_model: skipped (GALAXY_SKIP_LLM=1)", flush=True)
        return 0
    if not repo or not name:
        print("fetch_model: skipped (set GALAXY_GGUF_REPO and GALAXY_GGUF_FILE to load a local model)", flush=True)
        return 0
    if target.is_file() and target.stat().st_size > 0:
        print(f"fetch_model: {target} is already present", flush=True)
        return 0
    try:
        from huggingface_hub import hf_hub_download
        target.parent.mkdir(parents=True, exist_ok=True)
        cached = hf_hub_download(repo_id=repo, filename=name)
        shutil.copyfile(cached, target)
        print(f"fetch_model: downloaded {repo}/{name} to {target}", flush=True)
    except Exception as exc:  # the engine must still start
        print(f"fetch_model: could not fetch the model ({type(exc).__name__}); the engine starts in heuristics mode",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
