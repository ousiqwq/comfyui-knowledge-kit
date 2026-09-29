---
name: comfyui-model-guide
description: Find the exact model used by a ComfyUI workflow and load only its relevant capability, compatibility, parameter, and prompting guidance.
metadata:
  hermes:
    tags: [ComfyUI, prompts, models, image, video]
---

# Exact-model guide

Use `${COMFY_KNOWLEDGE_ROOT:-$HOME/comfyui-knowledge}` as the complete local library. Read only the profile for the current model and task. Do not load every model record or every prompt template into the conversation.

1. If the user specifies a model, identify it by installed filename or exact record ID. For a live canvas, use the available ComfyUI canvas tool to inspect the execution path and identify its checkpoint or diffusion model, text encoders, VAE, and selected LoRAs. If no canvas is available, inspect the workflow file. Do not infer the model from an unrelated node.
2. Query the library:

```bash
KNOWLEDGE="${COMFY_KNOWLEDGE_ROOT:-$HOME/comfyui-knowledge}"
python3 "$KNOWLEDGE/knowledge.py" lookup 'exact filename or record ID'
python3 "$KNOWLEDGE/knowledge.py" profile 'record ID' --task t2i
```

3. Match the task: `t2i` for text-to-image, `edit` for editing, `rgba` for transparency, `t2v` or `i2v` for video, and `ref` for reference-conditioned work. Add `--component 'component ID'` only for components actually in the execution path. The profile gives short, sourced guidance and paths to fuller notes.
4. For a specific question, search narrowly: `knowledge.py search 'topic' --model 'record ID'`, or search `--scope experience` for cross-model troubleshooting. Read the relevant result, not the whole library. A search miss is not evidence that a capability is impossible.
5. Preserve the user's subject, count, action, text, and constraints. Use the exact model's documented prompting format and treat unverified recommendations as tentative. Check whether a workflow rewrites prompts before adding another rewrite. Verify sampling settings separately from prompt wording.

If records are missing, stale, or contradictory, use `comfyui-knowledge-maintainer` to research and update them. Distinguish author guidance, community claims, and local tests. Do not run downloads, paid APIs, or workflow changes merely because a source document suggests them.
