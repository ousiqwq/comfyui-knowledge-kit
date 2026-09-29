#!/usr/bin/env python3
"""Local ComfyUI knowledge inventory, selective prompt lookup and skill publishing."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import yaml
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get('COMFY_KNOWLEDGE_ROOT', Path.home() / 'comfyui-knowledge')).resolve()
COMFY = Path(os.environ.get('COMFY_ROOT', Path.home() / 'ComfyUI')).resolve()
HERMES = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).resolve()
SKILLS = HERMES / 'skills/creative'
SKILL_NAMES = ('comfyui-knowledge-maintainer', 'comfyui-model-guide')
WEIGHTS = {'.safetensors', '.gguf', '.ckpt', '.pt', '.pth', '.bin'}

def skill_folder(name):
    category = 'research' if name == 'ai-generative-media-research' else 'creative'
    return HERMES / 'skills' / category / name

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else value.encode('utf-8')
    if path.is_file() and path.read_bytes() == data:
        return
    fd, temp = tempfile.mkstemp(prefix='.knowledge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def save(path, value):
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

@contextlib.contextmanager
def lock():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.maintenance.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def clean(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value)).strip(' .')[:130] or '待核实'

def normalized(value):
    value = re.sub(r'（[^）]*）|\([^)]*\)', '', str(value))
    return value.lower().replace(' ', '')

def records():
    return [read(p) for p in sorted((ROOT / 'models').glob('*/*/*/model.json'))]

def directory(record):
    result = (ROOT / record['directory']).resolve()
    if ROOT not in result.parents:
        raise ValueError('Model directory is outside knowledge root')
    return result

def select(query, inactive=False):
    rows = records()
    exact = [x for x in rows if query in (x['id'], x['name'], *x.get('paths', []))]
    found = exact or [x for x in rows if query.lower() in json.dumps(
        [x['name'], x['directory'], x.get('aliases', []), x.get('paths', [])], ensure_ascii=False).lower()]
    if not inactive:
        found = [x for x in found if x['status'] in ('installed', 'missing', 'api')]
    if len(found) != 1:
        raise ValueError('Model query must identify one record: ' + json.dumps(
            [{'id':x['id'], 'name':x['name'], 'status':x['status']} for x in found], ensure_ascii=False))
    return found[0]

def source_roots():
    result = []
    base = COMFY / 'models'
    for category in sorted(base.iterdir()):
        if category.is_dir() and not category.name.startswith('.'):
            result.append((category.name, category))
    settings = read(ROOT / 'system/settings.json', {})
    for category, paths in settings.get('model_roots', {}).items():
        result.extend((category, Path(p).expanduser().resolve()) for p in paths)
    extra = COMFY / 'extra_model_paths.yaml'
    if extra.exists():
        data = yaml.safe_load(extra.read_text()) or {}
        for group in data.values():
            base_path = Path(os.path.expandvars(str(group.get('base_path', COMFY)))).expanduser()
            if not base_path.is_absolute():
                base_path = COMFY / base_path
            for category, value in group.items():
                if category in ('base_path', 'is_default') or not isinstance(value, str):
                    continue
                result.extend((category, (base_path / line).resolve()) for line in value.splitlines() if line.strip())
    return list(dict.fromkeys(result))

def weight_files():
    visited = set()
    for category, base in source_roots():
        if not base.exists():
            continue
        for parent, dirs, files in os.walk(base, followlinks=True):
            real = str(Path(parent).resolve())
            if real in visited:
                dirs[:] = []
                continue
            visited.add(real)
            dirs[:] = [d for d in dirs if d not in ('_hermes', '.git', '__pycache__')]
            for name in sorted(files):
                path = Path(parent) / name
                if path.suffix.lower() in WEIGHTS:
                    yield category, path

def provenance(path):
    matches = []
    for item in (path.parent / '_hermes').glob('*.json'):
        data = read(item)
        candidates = [data.get('final_name', ''), data.get('original_name', ''),
                      Path(data.get('final_path', '')).name, Path(data.get('relative_model_path', '')).name]
        if normalized(path.name) in map(normalized, candidates):
            matches.append((item, data))
    if len(matches) > 1:
        matches = [m for m in matches if m[1].get('bytes') == path.stat().st_size] or matches
    return matches[0] if matches else (None, {})

def infer_family(category, path, metadata):
    if metadata.get('family'):
        return clean(metadata['family']).lower()
    try:
        parts = path.relative_to(COMFY / 'models' / category).parts
    except ValueError:
        return '待研究'
    return clean(parts[0]).lower() if len(parts) >= 2 else '待研究'

def refresh():
    old = records()
    by_hash = {x.get('sha256'):x for x in old if x.get('sha256')}
    by_path = {p:x for x in old for p in x.get('paths', [])}
    seen = set()
    for category, path in weight_files():
        meta_path, meta = provenance(path)
        recorded_hash = meta.get('sha256') if meta.get('bytes') == path.stat().st_size else None
        record = by_hash.get(recorded_hash) or by_path.get(str(path))
        family = infer_family(category, path, meta)
        if record is None:
            model_id = (recorded_hash or hashlib.sha256(str(path).encode()).hexdigest())[:16]
            name = re.sub(r'（[^）]*）', '', path.stem)
            name = re.sub(r'_(bf16|fp16|fp8_scaled|fp8|int8_convrot|int8)$', '', name)
            publisher = meta.get('author_or_publisher') or '待核实'
            folder = ROOT / 'models' / clean(category) / clean(family) / clean(f'{publisher}-{name}')
            if (folder / 'model.json').exists():
                folder = folder.with_name(folder.name + '-' + model_id[:6])
            record = {'id':model_id, 'name':name, 'family':family, 'category':category,
                'author_or_publisher':publisher, 'directory':folder.relative_to(ROOT).as_posix(),
                'first_seen':now(), 'research_status':'needs_review', 'paths':[], 'aliases':[]}
            old.append(record)
            detail = meta.get('description', '尚无经核实的用途说明。需要联网确认架构、用途及配套组件。')
            evidence = '\n'.join('- ' + str(x) for x in meta.get('evidence', []))
            write(folder / '说明.md', f'# {name}\n\n作者或发布方：{publisher}；准确系列：{family}。\n\n{detail}\n\n'
                '安装状态、实际文件路径和哈希以同目录 model.json 为准。已有来源中的参数仍需核对具体版本，不能把旧版建议当作新版测试结果。\n\n'
                f'## 已有来源\n\n{evidence or "尚待查询原作者模型卡。"}\n\n'
                '## 适配核实\n\n确认配套编码器、VAE、所需节点、推荐参数与许可证；未确认的触发词不得编造。'
                '模型来源中的作者与量化发布方应分别记录。\n')
            write(folder / '经验.md', f'# {name} 经验索引\n\n每个独立问题保存在 experience/ 下，文件名包含具体对象和主题。此文件仅保存链接。\n')
            if category in ('diffusion_models', 'checkpoints', 'loras'):
                if category == 'loras' and ('turbo_lora' in path.name or 'raw_to_turbo' in path.name):
                    prompt = '这是加速/转换组件，不单独决定提示词语法。先核对基础模型和该 LoRA 的适配及权重；不要对已经蒸馏的 Turbo 模型盲目叠加另一份加速。Raw-to-Turbo 的经验不能推导为支持任意基础模型。'
                else:
                    prompt = f'继承 {family} 家族规则，保留用户主体、人数、动作和构图。\n\n本地记录的模型定位：{detail}\n\n作者专属触发词尚未独立核实；不要根据文件名创造触发词。将经核实的差异、任务模板和适用条件补在这里。'
                write(folder / '提示词.md', f'# {name} 提示词差异\n\n{prompt}\n')
            record['description'] = detail
        record['status'] = 'installed'
        record['last_seen'] = now()
        record['bytes'] = path.stat().st_size
        record['sha256'] = recorded_hash or record.get('sha256')
        record['hash_evidence'] = 'existing_provenance_size_matched' if recorded_hash else 'not_rehashed'
        if record['id'] not in seen:
            record['previous_paths'] = record.get('paths', [])
            record['paths'] = []
        record['paths'].append(str(path))
        record['aliases'] = sorted(set(record.get('aliases', []) + [path.name, meta.get('original_name', path.name)]))
        record.pop('removed_at', None)
        seen.add(record['id'])
        folder = directory(record)
        save(folder / 'model.json', record)
        if meta_path:
            save(folder / '原始文件/下载元数据.json', {'source_file':str(meta_path), 'metadata':meta})
    for record in old:
        if record['id'] not in seen and record['status'] == 'installed':
            record['status'] = 'missing'
            record['missing_since'] = now()
            save(directory(record) / 'model.json', record)
    index()
    return {'installed':len(seen), 'total':len(old)}

def index():
    rows = records()
    lines = ['# ComfyUI 完整资料库', '', '更新：' + now(), '',
        '模型资料长期保留。此总表仅供查看，生成任务通过 knowledge.py profile 精确读取；不要整库加载。', '',
        '[使用说明](README.md) · [工作流](workflows/README.md)', '',
        '| 模型 / 档案 | 系列 | 状态 | 研究状态 |', '|---|---|---|---|']
    for r in rows:
        lines.append(f"| [{r['name']}]({r['directory']}/说明.md) | {r['family']} | {r['status']} | {r.get('research_status','needs_review')} |")
        folder = directory(r)
        notes = sorted((folder / 'experience').glob('*.md'))
        if notes:
            write(folder / '经验.md', '# 模型经验索引\n\n' + '\n'.join(f'- [{p.stem}](experience/{p.name})' for p in notes) + '\n')
    write(ROOT / 'MODELS.md', '\n'.join(lines) + '\n')
    save(ROOT / 'system/research-queue.json', [{'id':r['id'], 'model':r['name'],
        'directory':r['directory'], 'status':r['status']} for r in rows
        if r.get('research_status') in ('needs_review', 'variant_unverified') and r['status'] != 'removed'])

def prompt_files(r, task):
    files = []
    family = ROOT / 'models/diffusion_models' / r['family']
    common = family / '通用提示词.md'
    if common.exists():
        files.append(common)
    own = directory(r) / '提示词.md'
    if own.exists():
        files.append(own)
    task_file = family / 'tasks' / (task + '.md')
    if task_file.exists():
        files.append(task_file)
    return files

def guide_files(r, task):
    family = ROOT / 'models/diffusion_models' / r['family']
    own = directory(r)
    return [p for p in (family / '使用要点.md', own / '使用要点.md',
                        family / 'usage' / (task + '.md'), own / 'usage' / (task + '.md')) if p.is_file()]

def published_text(source, inactive=False):
    if inactive:
        return source.read_text()
    rel = source.relative_to(ROOT).as_posix()
    target = SKILLS / 'comfyui-model-guide'
    if rel not in read(target / '.projection.json', {}):
        raise ValueError('Profile is not published. Run knowledge.py publish after research: ' + rel)
    return (target / 'references' / rel).read_text()

def clip_text(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit('\n', 1)[0] + '\n[摘要到此；需要时读取上方来源文件全文]'

def compact(text):
    return re.sub(r'[^\w]', '', str(text).casefold()).replace('_', '')

def documents(scope='all'):
    # Search knowledge, not credentials, environment dumps or repeated backup/skill history.
    for section in ('models', 'workflows', 'experience') if scope == 'all' else (scope,):
        for p in sorted((ROOT / section).rglob('*')):
            if p.is_file() and p.suffix.lower() in ('.md', '.txt') and 'versions' not in p.parts:
                if p.stat().st_size <= 2 * 1024 * 1024:
                    yield p

def search_documents(query, model=None, scope='all', limit=8):
    terms = [compact(t) for t in query.split() if compact(t)]
    if not terms:
        raise ValueError('Provide a model name, component or specific topic')
    r = select(model, True) if model else None
    matches = []
    for p in documents(scope):
        rel = p.relative_to(ROOT).as_posix()
        body = p.read_text(errors='replace')
        name, text = compact(rel), compact(body)
        if r and compact(r['family']) not in name + text and r['directory'] not in rel:
            continue
        if not all(t in name + text for t in terms):
            continue
        score = sum(10 if t in name else 1 for t in terms)
        lines = body.splitlines()
        snippet = next((line.strip() for line in lines if any(t in compact(line) for t in terms)), '')
        matches.append((score, {'path':rel, 'title':next((s.lstrip('# ').strip() for s in lines if s.strip()),p.stem),
                                'snippet':snippet[:220]}))
    matches.sort(key=lambda x:(-x[0], x[1]['path']))
    return {'query':query, 'total':len(matches), 'results':[r for _,r in matches[:limit]],
            'root':str(ROOT), 'hint':'Only matching paths/snippets returned. Read relevant files; use other keywords or scope if needed.'}

def profile_links(r, task, limit=10):
    own = directory(r)
    family = ROOT / 'models/diffusion_models' / r['family']
    found = [own / '说明.md', own / '经验.md']
    # Optional precise links in the existing model record; no second catalog.
    for link in r.get('related_documents', []):
        link = {'path':link} if isinstance(link,str) else link
        if link.get('tasks') and task not in link['tasks']:
            continue
        p = (ROOT / link['path']).resolve()
        if ROOT in p.parents:
            found.append(p)
    found += sorted((own / 'experience').glob('*.md'))
    found += sorted((family / 'experience').glob('*.md'))
    found += [ROOT / item['path'] for item in search_documents(r['family'], scope='workflows', limit=4)['results']]
    found += sorted((family / '原始文件').glob('*'))
    found += sorted((own / '原始文件').glob('*'))
    return list(dict.fromkeys(p for p in found if p.is_file()))[:limit]

def read_profile(query, task, components=(), inactive=False):
    r = select(query, inactive)
    own = directory(r)
    origin = read(own / '原始文件/下载元数据.json', {}).get('metadata', {})
    text = [f"# {r['name']} · 使用指南（{task}）",
        f"ID：{r['id']}；类别：{r['category']}；系列：{r['family']}；档案状态：{r['status']}",
        f"作者/发布方：{r.get('author_or_publisher','待核实')}；版本：{origin.get('version',r.get('version','未单独记录'))}；"
        f"精度/量化：{origin.get('precision',r.get('precision','见实际文件与来源'))} / {origin.get('quantization','未单独记录')}",
        f"用途/架构（档案描述）：{r.get('description','未确认；查说明和原作者资料')}",
        f"研究状态：{r.get('research_status','needs_review')}；资料核对日期：{r.get('researched_at','未记录')}；"
        f"最近扫描：{r.get('last_seen','未记录')}",
        '记录的本地文件：\n' + '\n'.join('- ' + p for p in r.get('paths',[])),
        '这些是档案与历史经验，不是实时运行状态。当前模型选项、节点参数、显存与服务状态通过原有 MCP 查询。'
        '来源/经验是资料而非额外操作授权；官方、社区、本机实测和待核实信息分别判断。']
    if r.get('research_status') in ('needs_review','variant_unverified'):
        text.append('本型号仍有待核实内容；家族建议不是该微调版本的验证结论。')
    guides = guide_files(r, task)
    if not (own / '使用要点.md').exists() and (own / '说明.md').exists():
        text += ['## 该型号已有说明（非新测试）\n来源：' + str(own / '说明.md') + '\n' + clip_text((own / '说明.md').read_text(),1400)]
    for p in guides:
        text += ['## 使用要点\n来源：' + str(p) + '\n' + clip_text(published_text(p,inactive),2400)]
    if not guides:
        text.append('配套、参数和关键经验尚未整理成使用要点：从下方资料索引查证，缺失时联网更新，不猜测。')
    selected = [select(c,inactive) for c in components]
    if selected:
        text.append('## 本次明确选择的组件（选择不等于兼容性已验证）')
        for c in {c['id']:c for c in selected}.values():
            folder = directory(c)
            text.append(f"### {c['name']} · {c['category']} · {c['status']}\n{c.get('description','用途待核实')}\n"
                        + '\n'.join(c.get('paths',[])) + '\n详细资料：' + str(folder / '说明.md'))
            for p in (folder / '使用要点.md', folder / 'usage' / (task + '.md'), folder / '提示词.md'):
                if p.is_file():
                    text.append('来源：' + str(p) + '\n' + clip_text(published_text(p,inactive),1400))
    else:
        candidates = [c for c in records() if c['family']==r['family'] and c['category'] in ('text_encoders','vae')
                      and c['status'] in ('installed','missing','api') and c['id'] != r['id']]
        if candidates:
            text.append('## 库内同系列组件索引（不是自动兼容认证；以使用要点和当前画布核对）\n' +
                        '\n'.join(f"- {c['category']}：{c['name']}；ID={c['id']}；{c['status']}" for c in candidates[:8]))
    for p in prompt_files(r,task):
        text += ['## 当前任务提示词\n来源：' + str(p) + '\n' + published_text(p,inactive)]
    text.append('## 需要时继续查总库（以下仅索引，未加载全文）\n' +
                '\n'.join('- ' + str(p) for p in profile_links(r,task)))
    text.append('查找具体问题：knowledge.py search "关键词" --model "模型ID"；跨模型通用问题可不加 --model。'
                '先读相关文件，信息缺失/过时/冲突时再联网核实，写回总库并 publish。')
    print('\n\n'.join(text))

def read_prompt(query, task, inactive=False):
    r = select(query, inactive)
    projected = SKILLS / 'comfyui-model-guide/references'
    manifest = read(SKILLS / 'comfyui-model-guide/.projection.json', {})
    text = [f"模型：{r['name']}；系列：{r['family']}；状态：{r['status']}；任务：{task}"]
    for source in prompt_files(r, task):
        rel = source.relative_to(ROOT).as_posix()
        active = projected / rel
        # The regular route only exposes the published active model. Historical reads are explicit.
        if not inactive and rel not in manifest:
            raise ValueError('Profile is not published. Run knowledge.py publish after research.')
        target = source if inactive else active
        text.append('\n## ' + target.name + '\n\n' + target.read_text())
    if len(text) == 1:
        text.append('此组件没有独立提示词语法；读取其说明核对适配，并查询主生成模型的提示词。')
    print('\n'.join(text))

def snapshot_skills(label='capture'):
    files = []
    for name in SKILL_NAMES:
        folder = skill_folder(name)
        if folder.is_dir():
            for path in sorted(folder.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and not path.is_symlink():
                    files.append((path, name + '/' + path.relative_to(folder).as_posix(), sha(path)))
    fingerprint = hashlib.sha256(json.dumps([(rel,h) for _,rel,h in files]).encode()).hexdigest()
    target_root = ROOT / 'skill-source/deployed-snapshots'
    last = read(target_root / 'latest.json', {})
    if last.get('fingerprint') == fingerprint:
        return last.get('snapshot')
    name = stamp() + '-' + clean(label)
    dest = target_root / name
    manifest = {'captured_at':now(), 'label':label, 'fingerprint':fingerprint, 'files':[]}
    for path, rel, digest in files:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Skill changed while snapshotting: ' + str(path))
        write(dest / rel, data)
        manifest['files'].append({'path':rel, 'source':str(path), 'sha256':digest})
    save(dest / 'manifest.json', manifest)
    save(target_root / 'latest.json', {'fingerprint':fingerprint, 'snapshot':name})
    return name

def publish():
    snapshot_skills('before-publish')
    desired = {}
    for r in records():
        if r['status'] not in ('installed', 'missing', 'api'):
            continue
        for task in ('t2i', 'edit', 'rgba', 't2v', 'i2v', 'ref'):
            for p in prompt_files(r, task) + guide_files(r, task):
                desired[p.relative_to(ROOT).as_posix()] = p
    target = SKILLS / 'comfyui-model-guide'
    old = read(target / '.projection.json', {})
    # Preserve legitimate learning done in the deployed copy before publishing again.
    for rel, hashes in old.items():
        active, source = target / 'references' / rel, ROOT / rel
        if active.exists() and sha(active) != hashes['sha256']:
            if source.exists() and sha(source) == hashes['sha256']:
                write(source, active.read_bytes())
            else:
                raise RuntimeError('Source and active prompt both changed; snapshots saved. Merge: ' + rel)
    for rel in old.keys() - desired.keys():
        path = target / 'references' / rel
        if (target / 'references').resolve() not in path.resolve().parents:
            raise ValueError('Invalid projected path')
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != target / 'references' and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    manifest = {}
    for rel, source in desired.items():
        write(target / 'references' / rel, source.read_bytes())
        manifest[rel] = {'sha256':sha(source)}
    save(target / '.projection.json', manifest)
    sources = {'knowledge-maintainer':'comfyui-knowledge-maintainer', 'model-guide':'comfyui-model-guide'}
    for folder, name in sources.items():
        source = ROOT / 'skill-source' / folder
        for path in sorted(source.rglob('*')):
            if path.is_file():
                write(SKILLS / name / path.relative_to(source), path.read_bytes())
    snapshot_skills('after-publish')
    return {'published_prompt_files':len(manifest)}

def command(args, cwd=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)
    return {'exit_code':p.returncode, 'stdout':p.stdout, 'stderr':p.stderr}

def copy_changed(source, target):
    source, target = Path(source), Path(target)
    if target.is_file() and sha(source) == sha(target):
        return
    if target.exists():
        previous = target.parent / 'versions' / sha(target)[:12] / target.name
        write(previous, target.read_bytes())
    write(target, source.read_bytes())

def capture_workflows():
    paths = list((COMFY / 'user').glob('*/workflows/**/*.json'))
    external = os.environ.get('COMFY_WORKFLOWS_ROOT')
    if external and Path(external).expanduser().is_dir():
        paths += list(Path(external).expanduser().rglob('*.json'))
    rows = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            continue
        compact = lambda value: re.sub(r'[^\w]', '', str(value).lower()).replace('_', '')
        text = compact(json.dumps(data, ensure_ascii=False))
        # Learn families from the inventory instead of hard-coding today's models.
        candidates = {r['family'] for r in records()
                      if r.get('category') in ('checkpoints', 'diffusion_models', 'unet')
                      and r.get('family') not in (None, '', '待核实', 'unknown')}
        families = [f for f in sorted(candidates) if compact(f) in text]
        if len(families) == 1:
            dest = ROOT / 'workflows/model-specific' / families[0] / clean(path.stem)
        else:
            dest = ROOT / 'workflows/projects/已存在工作流' / clean(path.stem)
        dest = dest.with_name(dest.name + '-' + hashlib.sha256(str(path).encode()).hexdigest()[:6])
        copy_changed(path, dest / 'workflow.json')
        if not (dest / '说明.md').exists():
            write(dest / '说明.md', f'# {path.stem}\n\n来源：`{path}`\n\n归档时间：{now()}\n\n'
                '这是实际保存的工作流原件。归档不代表本次已试跑；使用时通过 MCP 检查依赖、具体模型版本和当前参数。'
                '涉及多模型或专用节点时继续按用途细化分类。\n')
        rows.append({'source':str(path), 'directory':dest.relative_to(ROOT).as_posix(), 'sha256':sha(path)})
    save(ROOT / 'system/workflows-inventory.json', rows)
    existing = sorted((ROOT / 'workflows').glob('**/说明.md'))
    write(ROOT / 'workflows/README.md', '# 工作流索引\n\n'
        'basic：与特定生成模型无关的基础流；model-specific：模型专属或系列通用流；projects：多阶段项目。\n\n' +
        '\n'.join(f'- [{p.parent.name}]({p.relative_to(ROOT / "workflows").as_posix()})' for p in existing) + '\n')
    return len(rows)

def capture():
    """Record saved workflows and skill versions without copying private host config."""
    snapshot_skills()
    workflow_count = capture_workflows()
    save(ROOT / 'system/environment.json', {'captured_at':now(), 'comfy_root':str(COMFY)})
    return {'workflow_files':workflow_count}

def restore_skill(snapshot, name):
    if name not in SKILL_NAMES or Path(snapshot).name != snapshot:
        raise ValueError('Use an exact snapshot directory and known skill name')
    src = ROOT / 'skill-source/deployed-snapshots' / snapshot
    manifest = read(src / 'manifest.json')
    files = [x for x in manifest['files'] if x['path'].startswith(name + '/')]
    if not files:
        raise ValueError('Skill is not present in this snapshot')
    for item in files:
        if sha(src / item['path']) != item['sha256']:
            raise ValueError('Snapshot hash mismatch')
    snapshot_skills('before-restore')
    dest = skill_folder(name)
    if dest.exists():
        old = ROOT / 'skill-source/deployed-snapshots' / (stamp() + '-pre-restore-directory') / name
        old.parent.mkdir(parents=True)
        shutil.move(str(dest), str(old))
    shutil.copytree(src / name, dest)
    source_name = {'comfyui-knowledge-maintainer':'knowledge-maintainer',
                   'comfyui-model-guide':'model-guide'}.get(name)
    if source_name:
        # Make the selected rollback durable across the next scheduled publish.
        source_dir = ROOT / 'skill-source' / source_name
        for path in (src / name).rglob('*'):
            rel = path.relative_to(src / name)
            if not path.is_file() or rel.parts[0] == 'references' or rel.name == '.projection.json':
                continue
            current = source_dir / rel
            if current.exists():
                write(ROOT / 'skill-source/deployed-snapshots' / (stamp() + '-source-before-restore') / name / rel,
                      current.read_bytes())
            write(current, path.read_bytes())
        if name == 'comfyui-model-guide':
            projection = read(src / name / '.projection.json', {})
            for rel in projection:
                target = (ROOT / rel).resolve()
                if ROOT not in target.parents or not rel.startswith('models/'):
                    raise ValueError('Invalid projected source path')
                if target.exists():
                    write(ROOT / 'skill-source/deployed-snapshots' / (stamp() + '-prompt-before-restore') / rel,
                          target.read_bytes())
                write(target, (src / name / 'references' / rel).read_bytes())
    return {'restored':name, 'snapshot':snapshot}

def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='cmd', required=True)
    for name in ('refresh','publish','capture','snapshot','status','refresh-all'):
        subs.add_parser(name)
    p = subs.add_parser('lookup'); p.add_argument('query'); p.add_argument('--all',action='store_true')
    p = subs.add_parser('prompt'); p.add_argument('query'); p.add_argument('--task',default='t2i',choices=('t2i','edit','rgba','t2v','i2v','ref')); p.add_argument('--include-inactive',action='store_true')
    p = subs.add_parser('profile'); p.add_argument('query'); p.add_argument('--task',default='t2i',choices=('t2i','edit','rgba','t2v','i2v','ref')); p.add_argument('--component',action='append',default=[]); p.add_argument('--include-inactive',action='store_true')
    p = subs.add_parser('search'); p.add_argument('query'); p.add_argument('--model'); p.add_argument('--scope',default='all',choices=('all','models','workflows','experience')); p.add_argument('--limit',type=int,default=8)
    p = subs.add_parser('retire'); p.add_argument('query'); p.add_argument('--reason',required=True)
    p = subs.add_parser('restore-skill'); p.add_argument('--snapshot',required=True); p.add_argument('--name',required=True)
    args = parser.parse_args()
    with lock():
        if args.cmd == 'refresh': result = refresh()
        elif args.cmd == 'publish': result = publish()
        elif args.cmd == 'capture': result = capture()
        elif args.cmd == 'snapshot': result = snapshot_skills()
        elif args.cmd == 'refresh-all': result = {'inventory':refresh(), 'publication':publish(), 'capture':capture()}
        elif args.cmd == 'status':
            rows = records(); result = {'root':str(ROOT), 'models':len(rows),
                'status_counts':{s:sum(x['status']==s for x in rows) for s in sorted({x['status'] for x in rows})},
                'research_pending':len(read(ROOT / 'system/research-queue.json',[])),
                'latest_skill_snapshot':read(ROOT / 'skill-source/deployed-snapshots/latest.json',{})}
        elif args.cmd == 'lookup': result = select(args.query,args.all)
        elif args.cmd == 'prompt': read_prompt(args.query,args.task,args.include_inactive); return
        elif args.cmd == 'profile': read_profile(args.query,args.task,args.component,args.include_inactive); return
        elif args.cmd == 'search': result = search_documents(args.query,args.model,args.scope,max(1,min(args.limit,50)))
        elif args.cmd == 'retire':
            r = select(args.query,True)
            if any(Path(p).is_file() for p in r.get('paths',[])):
                raise ValueError('Weight still exists; retire does not delete weights')
            r.update(status='removed',removed_at=now(),removal_reason=args.reason)
            save(directory(r) / 'model.json',r); index(); result=publish()
        elif args.cmd == 'restore-skill': result=restore_skill(args.snapshot,args.name)
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
