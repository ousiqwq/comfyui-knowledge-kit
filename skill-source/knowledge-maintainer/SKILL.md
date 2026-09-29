---
name: comfyui-knowledge-maintainer
description: Discover installed ComfyUI models, research their exact versions, maintain model-specific guidance and workflows, and publish only relevant guidance to Hermes.
metadata:
  hermes:
    tags: [ComfyUI, models, research, knowledge, backup]
---

# ComfyUI knowledge maintenance

The complete library is `${COMFY_KNOWLEDGE_ROOT:-$HOME/comfyui-knowledge}`. Its `knowledge.py` is the source of truth; the active Hermes model-guide skill is only a selective projection. Never register the whole library as a skill search root.

```bash
KNOWLEDGE="${COMFY_KNOWLEDGE_ROOT:-$HOME/comfyui-knowledge}"
python3 "$KNOWLEDGE/knowledge.py" status
```

## After a model is added or changed

1. Run `knowledge.py refresh`, then `lookup` by the exact installed filename or record ID. The scanner discovers ComfyUI model categories, optional extra model paths, and locally recorded download metadata. A guessed family or publisher is not a verified identity.
2. Research the exact version using the original model card, author documentation, example workflows, and recent community guidance when needed. Record source URLs, access date, which claims are official versus community reports, compatibility, license, prompting format, known limitations, and what was actually tested locally. If evidence is missing, write “unverified”; do not invent trigger words or recommended parameters.
3. Store the durable record in `models/<ComfyUI-category>/<family>/<publisher-and-model>/`. Keep the model's `model.json`, `说明.md`, `使用要点.md`, and `提示词.md` separate. Put family-wide guidance in the family directory, task-specific guidance under `usage/` or `tasks/`, and one reusable observation per accurately named file in `experience/`.
4. Keep links to related workflow and experiment files in the model record rather than duplicating them. Use `knowledge.py publish`, then check `profile '<ID>' --task t2i` or the relevant task. This loads a compact model-specific profile, not every installed model.

Available tasks: `t2i`, `edit`, `rgba`, `t2v`, `i2v`, and `ref`. Extend the script and skill together if a genuinely new task needs its own profile.

## Removal and recovery

When the user confirms a weight was removed, refresh inventory and use `retire '<ID>' --reason '<reason>'` after confirming the file is absent. Retiring does not delete weights or erase historical research; it removes the active prompt projection. A temporarily missing mount is not proof that a model was deleted.

Saved workflows belong under `workflows/basic`, `workflows/model-specific/<family>/...`, or `workflows/projects/...` according to dependencies. Importing a workflow is not the same as successfully executing it.

Edit the library's `skill-source` first, then publish. Before/after snapshots help recover earlier active skills. If the source and deployed copy both changed, inspect and merge rather than overwrite either one.

## Backup

The optional OpenList backup lives in `backup/backup.py`; credentials belong in a private file outside this library. `--status`, `--now`, `--verify-latest`, and `--install-timer` are available. Confirm the configured destination and retention before enabling deletion. A backup of this library is not a full operating-system or Conda image.
