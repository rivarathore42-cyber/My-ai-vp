---
title: Galaxy AI
emoji: 🌌
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Galaxy AI Engine v7.0

A private assistant with 20 agents, a chat page with a voice orb, and file and location sharing.

Copy this card and `deploy/huggingface/Dockerfile` to the root of the Space repository before you use them there.
Hugging Face needs its Dockerfile at the root.

Open the Space URL: the page opens straight into the assistant. There is no pairing step.

Anyone who can reach this Space can chat as the owner and read answers that draw on the owner's notes. Keep the Space
private, or add access control before you share the link.

Free Spaces sleep when idle and do not keep their storage across restarts, so conversations and notes are lost then.
The local model is off by default (`GALAXY_SKIP_LLM=1`); the 20 agents still answer in heuristics mode.
