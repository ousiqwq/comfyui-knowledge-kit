#!/usr/bin/env python3
"""Self-contained ComfyUI knowledge backup to OpenList; Python standard library."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo
from openlist_support import OpenList, BackupError, atomic_json, digest_file, json_bytes, log, process_lock

ROOT = Path(__file__).resolve().parent.parent
def load_private_env():
    """Read backup credentials from a file outside the backed-up knowledge tree."""
    path = Path(os.environ.get('COMFY_BACKUP_ENV',
        Path.home() / '.config/comfyui-knowledge-kit/backup.env')).expanduser()
    if not path.is_file():
        return
    allowed = {'OPENLIST_URL', 'OPENLIST_USERNAME', 'OPENLIST_PASSWORD',
               'OPENLIST_BASE_PATH', 'COMFY_BACKUP_INSTANCE'}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if separator and key in allowed:
            os.environ.setdefault(key, value)


load_private_env()
INSTANCE = os.environ.get('COMFY_BACKUP_INSTANCE', 'my-machine')
if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', INSTANCE):
    raise RuntimeError('COMFY_BACKUP_INSTANCE must use letters, digits, dot, underscore or dash')
OPENLIST = {'url': os.environ.get('OPENLIST_URL', ''),
            'username': os.environ.get('OPENLIST_USERNAME', ''),
            'password': os.environ.get('OPENLIST_PASSWORD', ''),
            'expected_base_path': os.environ.get('OPENLIST_BASE_PATH', ''),
            'verify_attempts': 12, 'retry_delay_seconds': 5,
            'directory_ready_attempts': 24, 'directory_ready_delay_seconds': 5}
STATE_DIR = ROOT / 'backup/state'
STATE_FILE = STATE_DIR / 'state.json'
STAGING = Path.home() / '.cache/comfyui-knowledge-backup'
FORMAT = 'comfyui-knowledge-backup-v1'
PATTERN = re.compile(r'comfyui-(regular|checkpoint)-\d{8}T\d{6}Z-[0-9a-f]{32}\.zip\Z')
DEFAULTS = {'time':'03:15','timezone':'UTC','interval_days':1,
            'checkpoint_every_months':1,'delete_superseded_after_days':1}

def require_config():
    missing = [name for name, value in (
        ('OPENLIST_URL', OPENLIST['url']),
        ('OPENLIST_USERNAME', OPENLIST['username']),
        ('OPENLIST_PASSWORD', OPENLIST['password']),
        ('OPENLIST_BASE_PATH', OPENLIST['expected_base_path']),
    ) if not value]
    if missing:
        raise BackupError('Set private backup configuration: ' + ', '.join(missing))

def settings():
    p=ROOT / 'backup/settings.json'
    result=DEFAULTS | (json.loads(p.read_text()) if p.exists() else {})
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',result['time']):
        raise BackupError('time must be HH:MM')
    if type(result['interval_days']) is not int or result['interval_days'] < 1:
        raise BackupError('interval_days must be a positive integer')
    if type(result['checkpoint_every_months']) is not int or result['checkpoint_every_months'] < 0:
        raise BackupError('checkpoint_every_months must be zero or a positive integer')
    if result['delete_superseded_after_days'] < 1:
        raise BackupError('Keep superseded backups for at least one day')
    ZoneInfo(result['timezone'])
    return result

def knowledge():
    os.environ.setdefault('COMFY_KNOWLEDGE_ROOT', str(ROOT))
    spec=importlib.util.spec_from_file_location('knowledge',ROOT / 'knowledge.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if module.ROOT != ROOT:
        raise BackupError('COMFY_KNOWLEDGE_ROOT does not match this deployment')
    return module

def load_state():
    require_config()
    binding={'root':str(ROOT),'instance':INSTANCE,'url':OPENLIST['url'],'base':OPENLIST['expected_base_path']}
    if STATE_FILE.exists():
        state=json.loads(STATE_FILE.read_text())
        if state.get('format')!=FORMAT or state.get('binding')!=binding:
            raise BackupError('State belongs to another deployment')
    else:
        state={'format':FORMAT,'binding':binding,'owner':uuid.uuid4().hex,'records':[],'latest':None,'pending':None}
        atomic_json(STATE_FILE,state)
    return state

def ownership(client,state):
    client.mkdir('/'+INSTANCE)
    path=f'/{INSTANCE}/knowledge-backup-owner.json'
    marker={'format':FORMAT,'instance':INSTANCE,'owner':state['owner']}
    if client.entry(path) is None:
        if client.listing('/'+INSTANCE):
            raise BackupError('Remote directory has data but no matching owner marker')
        client.upload(path,json_bytes(marker))
    else:
        _,_,data=client.download(path,capture=True)
        if json.loads(data)!=marker:
            raise BackupError('Remote owner differs; no upload or cleanup performed')
    for name in ('regular','checkpoints'):
        client.mkdir(f'/{INSTANCE}/{name}')

def validate_record(record,state):
    filename=record.get('filename','')
    kind='checkpoint' if record.get('checkpoint') else 'regular'
    folder='checkpoints' if record.get('checkpoint') else 'regular'
    if not PATTERN.fullmatch(filename) or not filename.startswith('comfyui-'+kind+'-'):
        raise BackupError('Invalid backup filename')
    if record.get('owner')!=state['owner'] or record.get('remote')!=f'/{INSTANCE}/{folder}/{filename}':
        raise BackupError('Record path/owner mismatch')
    if not re.fullmatch('[a-f0-9]{64}',record.get('sha256','')) or record.get('size',0)<=0:
        raise BackupError('Invalid archive hash/size')
    if record.get('manifest_remote')!=record['remote']+'.manifest.json':
        raise BackupError('Invalid manifest path')
    if not re.fullmatch('[a-f0-9]{64}',record.get('manifest_sha256','')) or record.get('manifest_size',0)<=0:
        raise BackupError('Invalid manifest hash/size')

def source_files():
    result=[]
    for parent,dirs,files in os.walk(ROOT):
        dirs[:]=[x for x in dirs if x not in ('.git','__pycache__','.venv','.cache')]
        for name in dirs:
            if (Path(parent)/name).is_symlink():
                raise BackupError('Symlink directory is not a complete copy: '+str(Path(parent)/name))
        for name in files:
            path=Path(parent)/name
            rel=path.relative_to(ROOT).as_posix()
            if (name.endswith('.lock') or name.startswith('.knowledge-') or name.endswith('.pyc')
                    or name == '.env' or name.startswith('.env.') or name.endswith(('.pem','.key'))):
                continue
            if path.is_symlink():
                raise BackupError('Copy selected original files into the library instead of symlinking: '+rel)
            info=path.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            result.append((path,rel,info.st_size,info.st_mtime_ns,stat.S_IMODE(info.st_mode)))
    return sorted(result,key=lambda x:x[1])

def verify_zip(path):
    with zipfile.ZipFile(path) as z:
        manifest=json.loads(z.read('MANIFEST.json'))
        names=z.namelist()
        if len(names)!=len(set(names)) or set(names)!={'MANIFEST.json', *manifest['files'].keys()}:
            raise BackupError('ZIP member list does not match manifest')
        for name,entry in manifest['files'].items():
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name or not name.startswith('comfyui-knowledge/'):
                raise BackupError('Unsafe archive path')
            h=hashlib.sha256(); size=0
            with z.open(name) as src:
                for block in iter(lambda:src.read(1024*1024),b''):
                    size+=len(block); h.update(block)
            if (size,h.hexdigest())!=(entry['size'],entry['sha256']):
                raise BackupError('Archive member hash mismatch: '+name)
        return manifest

def create_archive(path):
    module=knowledge()
    with module.lock():
        log('Refreshing inventory, skill snapshots, environment and saved workflows')
        module.refresh()
        module.publish()
        module.capture()
        files=source_files()
        manifest={'format':FORMAT,'created_at':datetime.now(timezone.utc).isoformat(),'root':str(ROOT),'files':{}}
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as z:
            for p,rel,size,mtime,mode in files:
                name='comfyui-knowledge/'+rel
                h=hashlib.sha256(); count=0
                info=zipfile.ZipInfo.from_file(p,name)
                info.compress_type=zipfile.ZIP_STORED if p.suffix.lower() in ('.safetensors','.gguf','.zip','.png','.jpg','.mp4','.webp') else zipfile.ZIP_DEFLATED
                with p.open('rb') as src,z.open(info,'w',force_zip64=True) as dest:
                    for block in iter(lambda:src.read(1024*1024),b''):
                        dest.write(block); h.update(block); count+=len(block)
                if count!=size:
                    raise BackupError('Source size changed during backup: '+rel)
                manifest['files'][name]={'size':count,'sha256':h.hexdigest(),'mode':mode}
            if [(r,s,m) for _,r,s,m,_ in source_files()] != [(r,s,m) for _,r,s,m,_ in files]:
                raise BackupError('Knowledge changed while packaging; previous backups retained')
            z.writestr('MANIFEST.json',json_bytes(manifest))
    verify_zip(path)
    return len(manifest['files'])

def latest(state):
    return next((r for r in state['records'] if r['id']==state['latest']),None)

def verify_remote(client,record,state):
    validate_record(record,state)
    client.verify(record['remote'],record['size'],record['sha256'])
    client.verify(record['manifest_remote'],record['manifest_size'],record['manifest_sha256'])

def complete_pending(client,state):
    record=state['pending']; validate_record(record,state)
    local=STAGING/record['filename']; sidecar=STAGING/(record['filename']+'.manifest.json')
    if digest_file(local)!=(record['size'],record['sha256']) or digest_file(sidecar)!=(record['manifest_size'],record['manifest_sha256']):
        raise BackupError('Pending local package changed')
    client.upload(record['remote'],local)
    client.upload(record['manifest_remote'],sidecar)
    record['verified_at']=time.time()
    for old in state['records']:
        if not old['checkpoint'] and 'superseded_at' not in old:
            old['superseded_at']=record['verified_at']
    state['records'].append(record); state['latest']=record['id']; state['pending']=None
    atomic_json(STATE_FILE,state)
    log('VERIFIED '+record['remote']+' SHA256='+record['sha256'])
    for path in (local,sidecar):
        if path.resolve().parent!=STAGING.resolve() or path.is_symlink():
            raise BackupError('Invalid local staging path')
        path.unlink()

def cleanup(client,state,cfg):
    current=latest(state)
    if not current: return
    candidates=[r for r in state['records'] if r['id']!=current['id'] and not r['checkpoint']
        and r.get('superseded_at',float('inf'))+cfg['delete_superseded_after_days']*86400<=time.time()]
    if not candidates: return
    ownership(client,state); verify_remote(client,current,state)
    for r in candidates:
        validate_record(r,state)
        # Only an exactly owned, fully hash-verified file may be removed.
        client.remove_file(r['remote'],r['size'],r['sha256'])
        client.remove_file(r['manifest_remote'],r['manifest_size'],r['manifest_sha256'])
        state['records'].remove(r); atomic_json(STATE_FILE,state)

def run(force=False,verify=False):
    cfg=settings(); state=load_state()
    for r in state['records']: validate_record(r,state)
    client=OpenList(OPENLIST); client.login(); ownership(client,state)
    if verify:
        if not latest(state): raise BackupError('No verified backup')
        verify_remote(client,latest(state),state); log('Latest archive and sidecar fully verified'); return
    if state['pending']:
        complete_pending(client,state)
    else:
        current=latest(state); tz=ZoneInfo(cfg['timezone']); today=datetime.now(tz)
        due=force or not current or (today.date()-datetime.fromtimestamp(current['verified_at'],tz).date()).days>=cfg['interval_days']
        if due:
            interval=cfg['checkpoint_every_months']
            month=today.year*12+today.month-1
            period=str(month-month%interval) if interval else None
            checkpoint=bool(interval and not any(r['checkpoint'] and r.get('period')==period for r in state['records']))
            kind='checkpoint' if checkpoint else 'regular'; run_id=uuid.uuid4().hex
            filename=f"comfyui-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{run_id}.zip"
            STAGING.mkdir(parents=True,exist_ok=True)
            local=STAGING/filename
            count=create_archive(local); size,digest=digest_file(local)
            remote=f"/{INSTANCE}/{'checkpoints' if checkpoint else 'regular'}/{filename}"
            record={'id':run_id,'filename':filename,'owner':state['owner'],'remote':remote,'checkpoint':checkpoint,
                    'period':period,'size':size,'sha256':digest,'members':count,'created_at':time.time()}
            content=json_bytes({'format':FORMAT,**record})
            sidecar=STAGING/(filename+'.manifest.json'); sidecar.write_bytes(content)
            record.update(manifest_remote=remote+'.manifest.json',manifest_size=len(content),manifest_sha256=hashlib.sha256(content).hexdigest())
            state['pending']=record; atomic_json(STATE_FILE,state)
            complete_pending(client,state)
        else:
            log('Not due; checking superseded retention only')
    cleanup(client,state,cfg)

def install_timer():
    require_config()
    cfg=settings(); units=Path.home()/'.config/systemd/user'; units.mkdir(parents=True,exist_ok=True)
    script=Path(__file__).resolve()
    if any(c in str(script) for c in ('"','\n','%')):
        raise BackupError('Unsupported systemd script path')
    texts={'comfyui-knowledge-backup.service':f'''# Managed by comfyui-knowledge backup.py
[Unit]
Description=Verified ComfyUI knowledge and skill backups to OpenList
[Service]
Type=oneshot
UMask=0077
Environment=PYTHONUNBUFFERED=1
ExecStart="{sys.executable}" "{script}" --run
TimeoutStartSec=infinity
''','comfyui-knowledge-backup.timer':f'''# Managed by comfyui-knowledge backup.py
[Unit]
Description=Daily ComfyUI knowledge backup
[Timer]
OnCalendar=*-*-* {cfg['time']}:00 {cfg['timezone']}
Persistent=true
AccuracySec=1min
Unit=comfyui-knowledge-backup.service
[Install]
WantedBy=timers.target
'''}
    for name,text in texts.items():
        path=units/name
        if path.exists() and not path.read_text().startswith('# Managed by comfyui-knowledge backup.py'):
            raise BackupError('Existing unmanaged unit: '+str(path))
        path.write_text(text)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','comfyui-knowledge-backup.timer'],check=True)
    subprocess.run(['systemctl','--user','restart','comfyui-knowledge-backup.timer'],check=True)
    log('Timer enabled at '+cfg['time']+' '+cfg['timezone'])

def test_remote():
    require_config()
    client=OpenList(OPENLIST); client.login()
    with tempfile.TemporaryDirectory(prefix='comfyui-backup-test-') as td:
        p=Path(td)/('comfyui-test-'+uuid.uuid4().hex+'.zip')
        with zipfile.ZipFile(p,'w') as z: z.writestr('probe.bin',os.urandom(256*1024))
        size,digest=client.upload('/'+p.name,p)
        client.remove_file('/'+p.name,size,digest)
    log('TEST PASSED: upload, complete download, SHA256 and exact probe deletion')

def restore_zip(path,destination):
    manifest=verify_zip(path); dest=Path(destination).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        raise BackupError('Restore into an empty directory first')
    dest.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for name,entry in manifest['files'].items():
            output=dest/Path(*PurePosixPath(name).parts[1:])
            output.parent.mkdir(parents=True,exist_ok=True)
            with z.open(name) as src,output.open('wb') as out: shutil.copyfileobj(src,out)
            output.chmod(entry['mode'] & 0o777)
            if digest_file(output)!=(entry['size'],entry['sha256']):
                raise BackupError('Restored file hash mismatch')
    log(f'Restored and verified {len(manifest["files"])} files into {dest}')

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__); mode=p.add_mutually_exclusive_group(required=True)
    for flag in ('run','now','test','verify-latest','install-timer','status'):
        mode.add_argument('--'+flag,action='store_true')
    mode.add_argument('--check-archive',type=Path)
    mode.add_argument('--restore',type=Path); p.add_argument('--destination',type=Path)
    args=p.parse_args()
    if args.install_timer: install_timer(); return
    if args.test: test_remote(); return
    if args.check_archive:
        m=verify_zip(args.check_archive); print(json.dumps({'verified_files':len(m['files'])})); return
    if args.restore:
        if not args.destination: p.error('--restore requires --destination')
        restore_zip(args.restore,args.destination); return
    if args.status:
        state=load_state(); print(json.dumps({'settings':settings(),'latest':latest(state),'pending':state['pending']},ensure_ascii=False,indent=2)); return
    with process_lock(STATE_DIR):
        run(force=args.now,verify=args.verify_latest)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        log('FAILED: '+str(exc)+'; existing remote backups retained.'); sys.exit(1)
