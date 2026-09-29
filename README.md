# ComfyUI Knowledge Kit

A portable starting point for a ComfyUI model knowledge library, two selective Hermes skills, and an optional verified OpenList backup. This repository contains **code and blank examples only**—no personal model catalog, weights, workflows, conversation history, machine inventory, server address, or credentials.

The scanner discovers installed model files; the skills guide an agent to research the **exact** model version and maintain sourced notes. It does not silently scrape the web or claim that filename guesses are verified facts. The active model-guide skill publishes only guidance for currently relevant models and tasks.

## Requirements and setup

- Linux, Python 3.11+, and an existing ComfyUI installation.
- `pip install -r requirements.txt` for `extra_model_paths.yaml` support.
- Hermes Agent is optional; `publish` installs skills into its creative skill directory.
- OpenList is optional and only needed for remote backup.

Clone this repository into `~/comfyui-knowledge`, or set `COMFY_KNOWLEDGE_ROOT` to another absolute path. Set `COMFY_ROOT` to your actual ComfyUI directory. If needed, set `HERMES_HOME` and `COMFY_WORKFLOWS_ROOT` as well.

```bash
export COMFY_ROOT="$HOME/ComfyUI"
export COMFY_KNOWLEDGE_ROOT="$HOME/comfyui-knowledge"
python3 knowledge.py refresh
python3 knowledge.py status
python3 knowledge.py publish
python3 knowledge.py capture
```

`refresh` scans ComfyUI's standard model categories, configured `extra_model_paths.yaml`, and optional extra roots in `system/settings.json`. It creates model records under `models/<category>/<family>/<publisher-model>/` and a generated `MODELS.md`. Use `lookup 'exact filename'`, `profile '<record ID>' --task t2i`, and `search 'specific topic' --model '<record ID>'` to read only relevant material. Task choices are `t2i`, `edit`, `rgba`, `t2v`, `i2v`, and `ref`.

The `skill-source/` files are editable source for the two Hermes skills. `publish` snapshots existing deployed versions, then projects the current model-specific notes into Hermes. If both the source and deployed copy changed, it stops for a manual merge. `capture` archives saved workflow JSON and snapshots those two skills; **it does not copy Hermes secrets, host configuration, installed packages, or arbitrary experiment folders**. To archive another workflow directory, set `COMFY_WORKFLOWS_ROOT`.

Runtime directories (`models/`, `workflows/`, `experience/`, `system/`, `backup/state/`, and skill snapshots) are ignored by Git. Model weights are not copied by `refresh`; if you deliberately place original weight files inside the knowledge directory, the optional backup will include them. Keep any credentials outside this directory.

## Optional OpenList backup

The backup uses a dedicated OpenList account restricted to a backup directory. Copy `backup.env.example` to `~/.config/comfyui-knowledge-kit/backup.env`, fill in **your own** values, and set mode `0600`. The example values are placeholders and will not connect to a real server. Do not commit the private file or place it under the knowledge root.

```bash
mkdir -p "$HOME/.config/comfyui-knowledge-kit"
chmod 700 "$HOME/.config/comfyui-knowledge-kit"
# Create ~/.config/comfyui-knowledge-kit/backup.env from backup.env.example, then:
chmod 600 "$HOME/.config/comfyui-knowledge-kit/backup.env"
python3 backup/backup.py --test
python3 backup/backup.py --now
python3 backup/backup.py --install-timer
```

The daily schedule defaults to 03:15 UTC; change it in `backup/settings.json` (for example `{"time":"03:15","timezone":"Asia/Shanghai","interval_days":1,"checkpoint_every_months":1,"delete_superseded_after_days":1}`). Re-run `--install-timer` after changing the schedule. A user-systemd timer needs user lingering enabled if it must run after logout. The timer uses the Python interpreter that installed it.

The script packages the knowledge directory as ZIP, checks every archived member's SHA-256, uploads it, downloads the complete remote file to verify SHA-256, and keeps ownership records before considering retention. Monthly checkpoints are on by default; old regular backups are eligible for deletion only after a newer verified backup has existed for at least one day. Backups stay in `<OpenList account base path>/<COMFY_BACKUP_INSTANCE>/`.

Useful commands: `--status`, `--verify-latest`, `--check-archive FILE`, and `--restore FILE --destination EMPTY_DIR`. Verify a restore into an empty directory before replacing a live library. This backs up the **knowledge directory**, not all of ComfyUI, Python environments, or the operating system.

## Privacy and publishing

Before publishing your own fork, inspect `git status`, `git diff --cached`, and the full commit history. `.gitignore` reduces accidents but cannot remove secrets from an earlier commit. If credentials ever reach a remote repository, rotate them and rewrite or replace the affected history.
