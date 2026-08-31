#!/usr/bin/env bash
# One sealed pose -> N inverse-fold sequences -> five folds per sequence.
set -euo pipefail
umask 077

usage() {
  printf '%s\n' \
    'usage: run_owner_only_inverse_fold.sh WORKSPACE_ROOT RUN_ID SEALED_SPEC NUM_SEQUENCES' \
    '  NUM_SEQUENCES must be 6..10; no design-diffusion stage is executed.'
}

if [ "${1:-}" = --help ]; then usage; exit 0; fi
[ "$#" -eq 4 ] || { usage >&2; exit 64; }
workspace_input=$1; run_id=$2; spec_input=$3; num_sequences=$4
[[ "$run_id" =~ ^[a-z0-9][a-z0-9_.-]{0,95}$ ]] || { echo 'unsafe RUN_ID' >&2; exit 64; }
case "$num_sequences" in ''|*[!0-9]*) echo 'NUM_SEQUENCES must be 6..10' >&2; exit 64;; esac
[ "$num_sequences" -ge 6 ] && [ "$num_sequences" -le 10 ] || { echo 'NUM_SEQUENCES must be 6..10' >&2; exit 64; }
for command_name in awk basename chmod cmp date df dirname find flock git head id \
  mkdir mktemp mv nvidia-smi python3 realpath rm sha256sum sort stat tail xargs; do
  command -v "$command_name" >/dev/null || { echo "missing command: $command_name" >&2; exit 69; }
done

workspace_root="$(realpath -e -- "$workspace_input")"
case "$workspace_root" in /home/*) ;; *) echo 'workspace must be under WSL /home' >&2; exit 64;; esac
repo_root="$workspace_root/GLP_"; owner_mode_root="$workspace_root/gpu_work/owner_mode"
owner_marker="$workspace_root/WINDOWS_OWNER_MODE.json"
test -d "$repo_root/.git" && test ! -L "$repo_root"
test -f "$owner_marker" && test ! -L "$owner_marker"

emergency_finalize() {
  local code=$?; trap - EXIT INT TERM; [ "$code" -ne 0 ] || code=74
  set +e
  python3 -I -S - "$staging_root" "$attempt_root" "$private_root" "$owner_mode_root" "$code" <<'PY'
import ctypes,errno,hashlib,json,os,secrets,shutil,stat,sys
from pathlib import Path
root=Path(sys.argv[1]); destination=Path(sys.argv[2]); private=Path(sys.argv[3]); owner=Path(sys.argv[4]).resolve(strict=True); code=int(sys.argv[5])
try:
 root=root.resolve(strict=True)
 if root.parent!=destination.parent.resolve(strict=True) or root.is_symlink(): raise SystemExit("unsafe emergency staging root")
 logs=root/"operator_logs"; logs.mkdir(mode=0o700,exist_ok=True)
 def atomic(path,data):
  if path.is_symlink(): raise SystemExit(f"unsafe emergency terminal path: {path}")
  tmp=path.with_name("."+path.name+".emergency.tmp"); tmp.unlink(missing_ok=True)
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,"wb") as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
  os.replace(tmp,path)
 receipt={"schema_version":"WINDOWS_OWNER_ONLY_INVERSE_FOLD_RUN_V1","status":"ONLY_INVERSE_FOLD_FAILED","exit_code":code,"generation_mode":"ONLY_INVERSE_FOLD_FROM_POSE_SPEC","design_diffusion_performed":False,"terminal_failure_reason":"BOOTSTRAP_OR_RESUME_PREFLIGHT_FAILED"}
 atomic(logs/"ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json",(json.dumps(receipt,indent=2,sort_keys=True)+"\n").encode()); atomic(logs/"STATUS.txt",b"ONLY_INVERSE_FOLD_FAILED\n"); atomic(logs/"exit_code.txt",f"{code}\n".encode())
 manifest=logs/"OUTPUT_SHA256SUMS"; temporary=logs/".OUTPUT_SHA256SUMS.tmp"; directory_manifest=logs/"OUTPUT_DIRECTORIES.txt"; directory_temporary=logs/".OUTPUT_DIRECTORIES.txt.tmp"
 manifest.unlink(missing_ok=True); temporary.unlink(missing_ok=True); directory_manifest.unlink(missing_ok=True); directory_temporary.unlink(missing_ok=True)
 def capture(base):
  rows={}; directories=set()
  for path in base.rglob("*"):
   rel=path.relative_to(base).as_posix()
   if rel in {"operator_logs/OUTPUT_SHA256SUMS","operator_logs/.OUTPUT_SHA256SUMS.tmp","operator_logs/.OUTPUT_DIRECTORIES.txt.tmp"}: continue
   info=path.lstat()
   if path.is_symlink(): raise SystemExit(f"emergency symlink member: {rel}")
   if stat.S_ISDIR(info.st_mode): directories.add(rel); continue
   if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise SystemExit(f"emergency unsafe member: {rel}")
   fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); h=hashlib.sha256()
   try:
    before=os.fstat(fd)
    while True:
     block=os.read(fd,1024*1024)
     if not block: break
     h.update(block)
    after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
   finally: os.close(fd)
   identity=lambda x:(x.st_dev,x.st_ino,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
   if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit(f"emergency member changed: {rel}")
   rows[rel]=h.hexdigest()
  return rows,directories
 initial=capture(root); directory_content="".join(f"./{rel}\n" for rel in sorted(initial[1],key=lambda x:x.encode())).encode(); atomic(directory_temporary,directory_content); os.replace(directory_temporary,directory_manifest)
 first=capture(root)
 if first[1]!=initial[1] or first[0].get("operator_logs/OUTPUT_DIRECTORIES.txt")!=hashlib.sha256(directory_content).hexdigest(): raise SystemExit("emergency sealed directory closure changed")
 content="".join(f"{digest}  ./{rel}\n" for rel,digest in sorted(first[0].items())).encode(); atomic(temporary,content); second=capture(root)
 if first!=second: raise SystemExit("emergency closure changed")
 os.replace(temporary,manifest); third=capture(root)
 if second!=third: raise SystemExit("emergency final replay changed")
 libc=ctypes.CDLL(None,use_errno=True); fn=getattr(libc,"renameat2",None)
 if fn is None: raise SystemExit("renameat2 required for emergency publication")
 fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
 if fn(-100,os.fsencode(root),-100,os.fsencode(destination),1)!=0:
  error=ctypes.get_errno(); raise OSError(error,os.strerror(error),destination)
 def replay_published(base):
  base=base.resolve(strict=True); published_manifest=base/"operator_logs/OUTPUT_SHA256SUMS"
  fd=os.open(published_manifest,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
  try:
   before=os.fstat(fd)
   while True:
    block=os.read(fd,1024*1024)
    if not block: break
    chunks.append(block)
   after=os.fstat(fd); current=os.stat(published_manifest,follow_symlinks=False)
  finally: os.close(fd)
  identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
  if identity(before)!=identity(after) or identity(after)!=identity(current) or after.st_nlink!=1: raise RuntimeError("emergency post-publish manifest identity drift")
  if b"".join(chunks)!=content: raise RuntimeError("emergency post-publish manifest content drift")
  replay=capture(base)
  if replay!=third: raise RuntimeError("emergency post-publish exact closure/hash replay failed")
 try:
  replay_published(destination)
 except Exception as exc:
  quarantine=None
  for _ in range(16):
   candidate=destination.with_name(destination.name+f".failed_publish.{os.getpid()}.{secrets.token_hex(8)}")
   if fn(-100,os.fsencode(destination),-100,os.fsencode(candidate),1)==0:
    quarantine=candidate; break
   error=ctypes.get_errno()
   if error!=errno.EEXIST: raise RuntimeError(f"emergency replay and quarantine failed: {exc}; {os.strerror(error)}") from exc
  if quarantine is None: raise RuntimeError(f"emergency replay failed and unique quarantine allocation exhausted: {exc}") from exc
  print(f"emergency post-publish replay failed; quarantined at {quarantine}: {exc}",file=sys.stderr)
  raise SystemExit(74) from exc
finally:
 if private.exists() and not private.is_symlink():
  resolved=private.resolve(strict=True)
  if resolved.parent==owner and resolved.name.startswith(".only_ifold_private.attempt_") and stat.S_ISDIR(private.lstat().st_mode): shutil.rmtree(private)
PY
  printf 'ONLY_INVERSE_FOLD_FAILED path=%s staging=%s\n' "$attempt_root" "$staging_root" >&2
  exit "$code"
}

# Install the transaction before project-specific preflight, or resume only
# the exact transaction created by the bootstrap copy of this runner.
private_resume=${OWNER_ONLY_IFOLD_PRIVATE_RESUME:-0}
run_root="$owner_mode_root/t11_only_inverse_fold_from_pose_spec/$run_id"
if [ "$private_resume" -eq 1 ]; then
  staging_root=${OWNER_ONLY_IFOLD_STAGING_ROOT:?}
  private_root=${OWNER_ONLY_IFOLD_PRIVATE_ROOT:?}
  attempt_root=${OWNER_ONLY_IFOLD_ATTEMPT_ROOT:?}
  attempt_id=${OWNER_ONLY_IFOLD_ATTEMPT_ID:?}
  operator_logs="$staging_root/operator_logs"
  resume_token_sha256=${OWNER_ONLY_IFOLD_RESUME_TOKEN_SHA256:?}
  # Do not arm a mutating failure trap for caller-provided paths until their
  # complete transaction shape and the still-unconsumed token are read-only
  # authenticated.
  python3 -I -S - "$owner_mode_root" "$run_root" "$staging_root" "$private_root" "$attempt_root" "$operator_logs" "$attempt_id" "$run_id" "$resume_token_sha256" <<'PY'
import hashlib,hmac,os,re,stat,sys
from pathlib import Path
owner,run,staging,private,attempt,logs=map(Path,sys.argv[1:7]); attempt_id,run_id,expected_token=sys.argv[7:10]
def canonical_dir(path,label):
 if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True)!=path: raise SystemExit(f"unsafe resumed {label} path")
 info=path.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022: raise SystemExit(f"unsafe resumed {label} identity/mode")
 return path
owner=canonical_dir(owner,"owner root"); run=canonical_dir(run,"run root")
staging=canonical_dir(staging,"staging"); private=canonical_dir(private,"private"); logs=canonical_dir(logs,"operator logs")
if not re.fullmatch(r"attempt_[0-9]{8}T[0-9]{6}Z",attempt_id): raise SystemExit("unsafe resumed attempt id")
if run!=owner/"t11_only_inverse_fold_from_pose_spec"/run_id or run.parent.parent!=owner: raise SystemExit("unsafe resumed run transaction")
if staging.parent!=run or not re.fullmatch(rf"\.{re.escape(attempt_id)}\.staging\.[A-Za-z0-9]{{6}}",staging.name): raise SystemExit("unsafe resumed staging transaction")
if private.parent!=owner or not re.fullmatch(rf"\.only_ifold_private\.{re.escape(attempt_id)}\.[A-Za-z0-9]{{6}}",private.name): raise SystemExit("unsafe resumed private transaction")
if logs!=staging/"operator_logs": raise SystemExit("unsafe resumed operator logs transaction")
if not attempt.is_absolute() or attempt!=run/attempt_id or attempt.exists() or attempt.is_symlink(): raise SystemExit("unsafe resumed attempt destination")
if not re.fullmatch(r"[0-9a-f]{64}",expected_token): raise SystemExit("invalid resume token contract")
token=private/"resume.token"
if token.is_symlink(): raise SystemExit("unsafe resume token path")
fd=os.open(token,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_uid,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
try:
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size!=64: raise SystemExit("unsafe resume token identity")
 while True:
  block=os.read(fd,1024)
  if not block: break
  chunks.append(block)
 after=os.fstat(fd); current=os.stat(token,follow_symlinks=False)
finally: os.close(fd)
if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit("resume token changed during read-only authentication")
if not hmac.compare_digest(hashlib.sha256(b"".join(chunks)).hexdigest(),expected_token): raise SystemExit("resume token digest mismatch")
PY
  trap emergency_finalize EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  python3 -I -S - "$private_root/resume.token" "$resume_token_sha256" "$operator_logs/resume_token_consumed.json" <<'PY'
import hashlib,hmac,json,os,stat,sys
from pathlib import Path
path=Path(sys.argv[1]); expected=sys.argv[2]; evidence=Path(sys.argv[3])
if len(expected)!=64 or path.is_symlink(): raise SystemExit("invalid resume token contract")
fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
try:
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size!=64: raise SystemExit("unsafe resume token identity")
 while True:
  block=os.read(fd,1024)
  if not block: break
  chunks.append(block)
 after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
finally: os.close(fd)
if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit("resume token changed during consumption")
token=b"".join(chunks); observed=hashlib.sha256(token).hexdigest()
if not hmac.compare_digest(observed,expected): raise SystemExit("resume token digest mismatch")
os.unlink(path)
if path.exists() or path.is_symlink(): raise SystemExit("resume token was not atomically consumed")
tmp=evidence.with_name("."+evidence.name+".tmp"); tmp.write_text(json.dumps({"status":"CONSUMED","sha256":observed,"launcher_binding_sha256":token[32:].hex()},sort_keys=True)+"\n"); os.replace(tmp,evidence)
PY
else
  attempt_stamp="$(date -u +'%Y%m%dT%H%M%SZ')"; attempt_id="attempt_$attempt_stamp"
  attempt_root="$run_root/$attempt_id"
  mkdir -p "$run_root"
  python3 -I -S - "$owner_mode_root" "$run_root" "$attempt_root" <<'PY'
import os,stat,sys
from pathlib import Path
owner,run,attempt=map(Path,sys.argv[1:]); owner=owner.resolve(strict=True); run=run.resolve(strict=True)
if run.parent.parent!=owner or run.is_symlink() or attempt.parent!=run or attempt.exists() or attempt.is_symlink(): raise SystemExit("unsafe owner run/attempt path")
for path in (owner,run):
 info=path.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022: raise SystemExit(f"unsafe directory owner/mode: {path}")
PY
  staging_root="$run_root/.${attempt_id}.staging.not-created"
  private_root="$owner_mode_root/.only_ifold_private.${attempt_id}.not-created"
  operator_logs="$staging_root/operator_logs"
  trap emergency_finalize EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  staging_root="$(mktemp -d "$run_root/.${attempt_id}.staging.XXXXXX")"
  operator_logs="$staging_root/operator_logs"; mkdir "$operator_logs"
  private_root="$(mktemp -d "$owner_mode_root/.only_ifold_private.${attempt_id}.XXXXXX")"
fi

copy_bound() {
  python3 -I -S - "$1" "$2" "${3:--}" "${4:--}" <<'PY'
import hashlib,json,os,stat,sys
from pathlib import Path
source=Path(sys.argv[1]); dest=Path(sys.argv[2]); expected_sha=None if sys.argv[3]=="-" else sys.argv[3]; expected_size=None if sys.argv[4]=="-" else int(sys.argv[4])
if source.is_symlink() or source.resolve(strict=True)!=source: raise SystemExit(f"source is not canonical/non-symlink: {source}")
dest.parent.mkdir(parents=True,exist_ok=True)
if dest.exists() or dest.is_symlink(): raise SystemExit(f"private destination exists: {dest}")
flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0); sfd=os.open(source,flags); dfd=None
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
try:
 before=os.fstat(sfd)
 if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise SystemExit(f"source is non-regular or hard-linked: {source}")
 dfd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_CLOEXEC",0),0o600); h=hashlib.sha256(); copied=0
 while True:
  block=os.read(sfd,4*1024*1024)
  if not block: break
  h.update(block); copied+=len(block); view=memoryview(block)
  while view:
   written=os.write(dfd,view)
   if written<=0: raise SystemExit("short private copy write")
   view=view[written:]
 os.fsync(dfd); after=os.fstat(sfd); current=os.stat(source,follow_symlinks=False)
 if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit(f"source changed during private copy: {source}")
 observed=h.hexdigest()
 if expected_sha is not None and observed!=expected_sha: raise SystemExit(f"accepted SHA mismatch: {source}")
 if expected_size is not None and copied!=expected_size: raise SystemExit(f"accepted size mismatch: {source}")
finally:
 os.close(sfd)
 if dfd is not None: os.close(dfd)
dfd=os.open(dest,flags); verify=hashlib.sha256()
try:
 dinfo=os.fstat(dfd)
 while True:
  block=os.read(dfd,4*1024*1024)
  if not block: break
  verify.update(block)
finally: os.close(dfd)
if not stat.S_ISREG(dinfo.st_mode) or dinfo.st_nlink!=1 or dinfo.st_size!=copied or verify.hexdigest()!=observed: raise SystemExit(f"unsafe private copy: {dest}")
print(json.dumps({"source":str(source),"private_copy":str(dest),"sha256":observed,"size_bytes":copied},sort_keys=True))
PY
}

verify_bound() {
  python3 -I -S - "$1" "$2" "${3:--}" <<'PY'
import hashlib,os,stat,sys
from pathlib import Path
path=Path(sys.argv[1]); expected=sys.argv[2]; size=None if sys.argv[3]=="-" else int(sys.argv[3])
if path.is_symlink() or path.resolve(strict=True)!=path: raise SystemExit(f"unsafe bound replay path: {path}")
fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); h=hashlib.sha256()
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
try:
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise SystemExit(f"unsafe bound replay identity: {path}")
 while True:
  block=os.read(fd,1024*1024)
  if not block: break
  h.update(block)
 after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
finally: os.close(fd)
if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit(f"bound replay changed: {path}")
if h.hexdigest()!=expected or (size is not None and after.st_size!=size): raise SystemExit(f"bound replay digest/size mismatch: {path}")
PY
}

verify_code_bindings_and_launcher() {
  python3 -I -S - "$operator_logs/code_bindings.SHA256SUMS" "$operator_logs/canonical_launcher_binding.json" "$private_runner" "$private_validator" "$private_builder" "$boltzgen_launcher" "$environment_launcher" <<'PY'
import hashlib,json,os,re,stat,sys
from pathlib import Path
manifest,binding_path=map(Path,sys.argv[1:3]); private_paths=list(map(Path,sys.argv[3:7])); canonical_launcher=Path(sys.argv[7]); token_evidence_path=binding_path.parent/"resume_token_consumed.json"
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_uid,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
def stable_bytes(path):
 if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True)!=path: raise SystemExit(f"unsafe code/launcher binding path: {path}")
 fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
 try:
  before=os.fstat(fd)
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1: raise SystemExit(f"unsafe code/launcher binding identity: {path}")
  while True:
   block=os.read(fd,1024*1024)
   if not block: break
   chunks.append(block)
  after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
 finally: os.close(fd)
 if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit(f"code/launcher binding changed: {path}")
 return b"".join(chunks),after
manifest_bytes,_=stable_bytes(manifest); rows={}
for line in manifest_bytes.decode().splitlines():
 match=re.fullmatch(r"([0-9a-f]{64})  (/.+)",line)
 if not match or match.group(2) in rows: raise SystemExit("code bindings manifest format/duplicate drift")
 rows[match.group(2)]=match.group(1)
expected_names={str(path) for path in private_paths}
if set(rows)!=expected_names or len(rows)!=4: raise SystemExit("code bindings manifest must contain exactly runner/validator/builder/launcher")
for path in private_paths:
 content,_=stable_bytes(path)
 if hashlib.sha256(content).hexdigest()!=rows[str(path)]: raise SystemExit(f"private code binding digest mismatch: {path}")
binding_bytes,_=stable_bytes(binding_path); binding=json.loads(binding_bytes); token_evidence=json.loads(stable_bytes(token_evidence_path)[0])
required={"source","private_copy","sha256","size_bytes"}
if set(binding)!=required or binding["source"]!=str(canonical_launcher) or binding["private_copy"]!=str(private_paths[-1]) or not re.fullmatch(r"[0-9a-f]{64}",binding["sha256"]) or not isinstance(binding["size_bytes"],int) or binding["size_bytes"]<=0: raise SystemExit("canonical launcher binding contract drift")
if set(token_evidence)!={"status","sha256","launcher_binding_sha256"} or token_evidence["status"]!="CONSUMED" or token_evidence["launcher_binding_sha256"]!=hashlib.sha256(binding_bytes).hexdigest(): raise SystemExit("launcher bootstrap binding/token cross-bind mismatch")
# Open both endpoints before reading either; stable path identities prevent
# replace/restore while the exact accepted bytes are compared.
fds=[]; states=[]; digests=[]
try:
 for path in (canonical_launcher,private_paths[-1]):
  if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True)!=path: raise SystemExit(f"unsafe launcher endpoint: {path}")
  fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); fds.append((path,fd)); before=os.fstat(fd)
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1: raise SystemExit(f"unsafe launcher endpoint identity: {path}")
  states.append(before)
 for path,fd in fds:
  h=hashlib.sha256(); size=0
  while True:
   block=os.read(fd,1024*1024)
   if not block: break
   h.update(block); size+=len(block)
  digests.append((h.hexdigest(),size))
 for index,(path,fd) in enumerate(fds):
  after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
  if identity(states[index])!=identity(after) or identity(after)!=identity(current): raise SystemExit(f"launcher endpoint changed during comparison: {path}")
finally:
 for _,fd in fds: os.close(fd)
expected=(binding["sha256"],binding["size_bytes"])
if digests!=[expected,expected] or rows[str(private_paths[-1])]!=binding["sha256"]: raise SystemExit("canonical/private launcher exact binding mismatch")
PY
}

seal_tree() {
  python3 -I -S - "$staging_root" <<'PY'
import hashlib,json,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(strict=True); logs=root/"operator_logs"; manifest=logs/"OUTPUT_SHA256SUMS"; tmp=logs/".OUTPUT_SHA256SUMS.tmp"; directory_manifest=logs/"OUTPUT_DIRECTORIES.txt"; directory_tmp=logs/".OUTPUT_DIRECTORIES.txt.tmp"
excluded={"operator_logs/OUTPUT_SHA256SUMS","operator_logs/.OUTPUT_SHA256SUMS.tmp","operator_logs/.OUTPUT_DIRECTORIES.txt.tmp"}
if manifest.exists() or manifest.is_symlink() or directory_manifest.exists() or directory_manifest.is_symlink(): raise SystemExit("output manifest exists")
def directory_snapshot():
 directories=set()
 for path in sorted(root.rglob("*")):
  rel=path.relative_to(root).as_posix(); info=path.lstat()
  if path.is_symlink(): raise SystemExit(f"symlink output member: {rel}")
  if stat.S_ISDIR(info.st_mode): directories.add(rel)
 return directories
directory_content="".join(f"./{rel}\n" for rel in sorted(directory_snapshot(),key=lambda x:x.encode())).encode()
fd=os.open(directory_tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
try:
 view=memoryview(directory_content)
 while view:
  written=os.write(fd,view)
  if written<=0: raise SystemExit("short directory manifest write")
  view=view[written:]
 os.fsync(fd)
finally: os.close(fd)
os.replace(directory_tmp,directory_manifest)
def capture():
 rows={}; identities={}; bound={}; directories=set(); wanted={"operator_logs/ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json","operator_logs/STATUS.txt","operator_logs/exit_code.txt","operator_logs/output_validation.json","operator_logs/OUTPUT_DIRECTORIES.txt"}
 for path in sorted(root.rglob("*")):
  rel=path.relative_to(root).as_posix()
  if rel in excluded: continue
  info=path.lstat()
  if path.is_symlink(): raise SystemExit(f"symlink output member: {rel}")
  if stat.S_ISDIR(info.st_mode): directories.add(rel); continue
  if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise SystemExit(f"unsafe/hard-linked output member: {rel}")
  fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); h=hashlib.sha256(); chunks=[] if rel in wanted else None
  try:
   before=os.fstat(fd)
   while True:
    block=os.read(fd,1024*1024)
    if not block: break
    h.update(block)
    if chunks is not None: chunks.append(block)
   after=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
  finally: os.close(fd)
  ident=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
  if ident(before)!=ident(after) or ident(after)!=ident(current): raise SystemExit(f"output changed while sealing: {rel}")
  rows[rel]=h.hexdigest(); identities[rel]=ident(after)
  if chunks is not None: bound[rel]=b"".join(chunks)
 return rows,identities,bound,directories
def validate(rows,bound,directories):
 required={"operator_logs/ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json","operator_logs/STATUS.txt","operator_logs/exit_code.txt"}
 if not required<=set(rows): raise SystemExit("terminal receipt members missing")
 directory_rel="operator_logs/OUTPUT_DIRECTORIES.txt"
 if directory_rel not in rows or directory_rel not in bound: raise SystemExit("sealed directory evidence missing")
 lines=bound[directory_rel].decode().splitlines(); sealed_directories=set()
 for line in lines:
  if not line.startswith("./"): raise SystemExit("invalid sealed directory evidence")
  rel=line[2:]; path=Path(rel)
  if not rel or path.is_absolute() or ".." in path.parts or path.as_posix()!=rel or rel in sealed_directories: raise SystemExit("invalid sealed directory evidence")
  sealed_directories.add(rel)
 if lines!=[f"./{rel}" for rel in sorted(sealed_directories,key=lambda x:x.encode())] or sealed_directories!=directories: raise SystemExit("sealed directory closure mismatch")
 receipt=json.loads(bound[next(x for x in required if x.endswith('.json'))].decode()); status=bound[next(x for x in required if x.endswith('STATUS.txt'))].decode().strip(); code=int(bound[next(x for x in required if x.endswith('exit_code.txt'))].decode())
 if receipt.get("status")!=status or receipt.get("exit_code")!=code: raise SystemExit("receipt/status/exit mismatch")
 if status=="ONLY_INVERSE_FOLD_COMPLETE":
  output=receipt.get("output_validation")
  if code!=0 or receipt.get("design_diffusion_performed") is not False or not isinstance(output,dict) or output.get("status")!="PASS": raise SystemExit("complete receipt contract failed")
  validation_rel="operator_logs/output_validation.json"
  if validation_rel not in rows or validation_rel not in bound: raise SystemExit("complete output validation evidence missing")
  if json.loads(bound[validation_rel].decode())!=output: raise SystemExit("receipt/output validation evidence mismatch")
  for row in output.get("semantic_payload_files",[]):
   if rows.get(row.get("path"))!=row.get("sha256"): raise SystemExit(f"semantic payload changed after validation: {row.get('path')}")
 elif status=="ONLY_INVERSE_FOLD_FAILED":
  if code==0: raise SystemExit("failed receipt has zero exit")
 else: raise SystemExit(f"invalid terminal status: {status}")
 return receipt
first=capture(); r1=validate(first[0],first[2],first[3]); second=capture(); r2=validate(second[0],second[2],second[3])
if (first[0],first[1],first[3])!=(second[0],second[1],second[3]) or r1!=r2: raise SystemExit("output closure/receipt changed between captures")
content="".join(f"{digest}  ./{rel}\n" for rel,digest in sorted(second[0].items(),key=lambda x:x[0].encode())).encode(); fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
try:
 view=memoryview(content)
 while view:
  written=os.write(fd,view)
  if written<=0: raise SystemExit("short manifest write")
  view=view[written:]
 os.fsync(fd)
finally: os.close(fd)
third=capture(); r3=validate(third[0],third[2],third[3])
if (second[0],second[1],second[3])!=(third[0],third[1],third[3]) or r2!=r3: tmp.unlink(missing_ok=True); raise SystemExit("output changed in seal window")
os.replace(tmp,manifest); fourth=capture(); r4=validate(fourth[0],fourth[2],fourth[3]); listed={}
fd=os.open(manifest,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
try:
 before=os.fstat(fd)
 while True:
  block=os.read(fd,1024*1024)
  if not block: break
  chunks.append(block)
 after=os.fstat(fd); current=os.stat(manifest,follow_symlinks=False)
finally: os.close(fd)
ident=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
if ident(before)!=ident(after) or ident(after)!=ident(current) or after.st_nlink!=1: raise SystemExit("final manifest changed during replay")
for line in b"".join(chunks).decode().splitlines():
 digest,rel=line.split("  ./",1)
 if len(digest)!=64 or rel in listed: raise SystemExit("invalid final manifest")
 listed[rel]=digest
if listed!=fourth[0] or fourth[3]!=third[3] or r4!=r3: raise SystemExit("final manifest/receipt replay failed")
PY
}

publish_staging() {
  python3 -I -S - "$staging_root" "$attempt_root" <<'PY'
import ctypes,errno,hashlib,os,secrets,stat,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(strict=True); manifest=root/"operator_logs/OUTPUT_SHA256SUMS"
fd=os.open(manifest,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
try:
 before=os.fstat(fd)
 while True:
  block=os.read(fd,1024*1024)
  if not block: break
  chunks.append(block)
 after=os.fstat(fd); current=os.stat(manifest,follow_symlinks=False)
finally: os.close(fd)
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
def read_sealed_directories(base):
 path=base/"operator_logs/OUTPUT_DIRECTORIES.txt"; pfd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); parts=[]
 try:
  pbefore=os.fstat(pfd)
  while True:
   block=os.read(pfd,1024*1024)
   if not block: break
   parts.append(block)
  pafter=os.fstat(pfd); pcurrent=os.stat(path,follow_symlinks=False)
 finally: os.close(pfd)
 if identity(pbefore)!=identity(pafter) or identity(pafter)!=identity(pcurrent) or pafter.st_nlink!=1: raise RuntimeError("sealed directory evidence identity drift")
 lines=b"".join(parts).decode().splitlines(); result=set()
 for line in lines:
  if not line.startswith("./"): raise RuntimeError("sealed directory evidence format drift")
  rel=line[2:]; candidate=Path(rel)
  if not rel or candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix()!=rel or rel in result: raise RuntimeError("sealed directory evidence path drift")
  result.add(rel)
 if lines!=[f"./{rel}" for rel in sorted(result,key=lambda x:x.encode())]: raise RuntimeError("sealed directory evidence ordering drift")
 return result
if identity(before)!=identity(after) or identity(after)!=identity(current) or after.st_nlink!=1: raise SystemExit("publication manifest changed")
listed={}; directories=set()
for line in b"".join(chunks).decode().splitlines():
 digest,rel=line.split("  ./",1)
 if len(digest)!=64 or rel in listed: raise SystemExit("invalid publication manifest")
 listed[rel]=digest
observed={}
for path in root.rglob("*"):
 rel=path.relative_to(root).as_posix(); info=path.lstat()
 if path.is_symlink(): raise SystemExit(f"publication symlink member: {rel}")
 if stat.S_ISDIR(info.st_mode): directories.add(rel); continue
 if rel=="operator_logs/OUTPUT_SHA256SUMS": continue
 if rel=="operator_logs/.OUTPUT_SHA256SUMS.tmp" or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise SystemExit(f"unsafe publication member: {rel}")
 pfd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); h=hashlib.sha256()
 try:
  pbefore=os.fstat(pfd)
  while True:
   block=os.read(pfd,1024*1024)
   if not block: break
   h.update(block)
  pafter=os.fstat(pfd); pcurrent=os.stat(path,follow_symlinks=False)
 finally: os.close(pfd)
 if identity(pbefore)!=identity(pafter) or identity(pafter)!=identity(pcurrent): raise SystemExit(f"publication member changed: {rel}")
 observed[rel]=h.hexdigest()
sealed_directories=read_sealed_directories(root)
if observed!=listed or directories!=sealed_directories: raise SystemExit("publication exact closure/hash/directory replay failed")
libc=ctypes.CDLL(None,use_errno=True); fn=getattr(libc,"renameat2",None)
if fn is None: raise SystemExit("renameat2 required")
fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
if fn(-100,os.fsencode(root),-100,os.fsencode(sys.argv[2]),1)!=0:
 error=ctypes.get_errno(); raise OSError(error,os.strerror(error),sys.argv[2])
destination=Path(sys.argv[2])
def replay_published(base):
 base=base.resolve(strict=True); mpath=base/"operator_logs/OUTPUT_SHA256SUMS"; mfd=os.open(mpath,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); mchunks=[]
 try:
  mbefore=os.fstat(mfd)
  while True:
   block=os.read(mfd,1024*1024)
   if not block: break
   mchunks.append(block)
  mafter=os.fstat(mfd); mcurrent=os.stat(mpath,follow_symlinks=False)
 finally: os.close(mfd)
 if identity(mbefore)!=identity(mafter) or identity(mafter)!=identity(mcurrent) or mafter.st_nlink!=1: raise RuntimeError("post-publish manifest identity drift")
 post_listed={}
 for line in b"".join(mchunks).decode().splitlines():
  digest,rel=line.split("  ./",1)
  if len(digest)!=64 or rel in post_listed: raise RuntimeError("post-publish manifest format drift")
  post_listed[rel]=digest
 post={}; post_directories=set()
 for path in base.rglob("*"):
  rel=path.relative_to(base).as_posix(); info=path.lstat()
  if path.is_symlink(): raise RuntimeError(f"post-publish symlink member: {rel}")
  if stat.S_ISDIR(info.st_mode): post_directories.add(rel); continue
  if rel=="operator_logs/OUTPUT_SHA256SUMS": continue
  if rel=="operator_logs/.OUTPUT_SHA256SUMS.tmp" or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise RuntimeError(f"post-publish unsafe member: {rel}")
  pfd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); h=hashlib.sha256()
  try:
   pbefore=os.fstat(pfd)
   while True:
    block=os.read(pfd,1024*1024)
    if not block: break
    h.update(block)
   pafter=os.fstat(pfd); pcurrent=os.stat(path,follow_symlinks=False)
  finally: os.close(pfd)
  if identity(pbefore)!=identity(pafter) or identity(pafter)!=identity(pcurrent): raise RuntimeError(f"post-publish member changed: {rel}")
  post[rel]=h.hexdigest()
 post_sealed_directories=read_sealed_directories(base)
 if post!=post_listed or post!=observed or post_directories!=directories or post_directories!=post_sealed_directories or post_sealed_directories!=sealed_directories: raise RuntimeError("post-publish exact manifest/closure/hash/directory replay failed")
try:
 replay_published(destination)
except Exception as exc:
 quarantine=None
 for _ in range(16):
  candidate=destination.with_name(destination.name+f".failed_publish.{os.getpid()}.{secrets.token_hex(8)}")
  if fn(-100,os.fsencode(destination),-100,os.fsencode(candidate),1)==0:
   quarantine=candidate; break
  error=ctypes.get_errno()
  if error!=errno.EEXIST: raise RuntimeError(f"post-publish replay failed and quarantine failed: {exc}; {os.strerror(error)}") from exc
 if quarantine is None: raise RuntimeError(f"post-publish replay failed and unique quarantine allocation exhausted: {exc}") from exc
 print(f"post-publish replay failed; quarantined at {quarantine}: {exc}",file=sys.stderr)
 raise SystemExit(74) from exc
PY
}
cleanup_private() {
  python3 -I -S - "$owner_mode_root" "$private_root" <<'PY'
import shutil,stat,sys
from pathlib import Path
owner=Path(sys.argv[1]).resolve(strict=True); target=Path(sys.argv[2]); resolved=target.resolve(strict=True)
if resolved.parent!=owner or not resolved.name.startswith(".only_ifold_private.attempt_") or target.is_symlink() or not stat.S_ISDIR(target.lstat().st_mode): raise SystemExit(f"unsafe private cleanup: {target}")
shutil.rmtree(target)
PY
}

terminal_complete=0
finalize() {
  local code=$?; trap - EXIT INT TERM; set +e
  if [ "$terminal_complete" -ne 1 ] && [ -d "$staging_root" ] && [ ! -L "$staging_root" ]; then
    [ "$code" -ne 0 ] || code=74
    rm -f -- "$operator_logs/OUTPUT_SHA256SUMS" "$operator_logs/.OUTPUT_SHA256SUMS.tmp" "$operator_logs/OUTPUT_DIRECTORIES.txt" "$operator_logs/.OUTPUT_DIRECTORIES.txt.tmp"
    python3 -I -S - "$operator_logs/ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json" "$code" <<'PY'
import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]); value={}
if p.is_file() and not p.is_symlink():
 try: value=json.loads(p.read_text())
 except Exception: value={}
value.update({"schema_version":"WINDOWS_OWNER_ONLY_INVERSE_FOLD_RUN_V1","status":"ONLY_INVERSE_FOLD_FAILED","exit_code":int(sys.argv[2]),"generation_mode":"ONLY_INVERSE_FOLD_FROM_POSE_SPEC","design_diffusion_performed":False}); t=p.with_name("."+p.name+".tmp"); t.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n"); os.replace(t,p)
PY
    printf '%s\n' ONLY_INVERSE_FOLD_FAILED > "$operator_logs/STATUS.txt"; printf '%s\n' "$code" > "$operator_logs/exit_code.txt"; date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/ended_at_utc.txt"
    if seal_tree; then
      publish_staging || code=74
    else
      code=74
    fi
  fi
  [ ! -d "$private_root" ] || cleanup_private || true
  printf 'ONLY_INVERSE_FOLD_FAILED path=%s staging=%s\n' "$attempt_root" "$staging_root" >&2; exit "$code"
}
trap finalize EXIT; trap 'exit 130' INT; trap 'exit 143' TERM
printf '%q ' "$0" "$@" > "$operator_logs/command.txt"; printf '\n' >> "$operator_logs/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/started_at_utc.txt"

python3 -I -S - "$owner_marker" <<'PY'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
for key,value in {"status":"ACTIVE","authority":"WINDOWS_CODEX","training_allowed":False,"model_weights_mutable":False,"mac_review_required":False}.items():
 if p.get(key)!=value: raise SystemExit(f"owner marker mismatch: {key}")
PY

if [ "$private_resume" -eq 0 ]; then
# Freeze the accepted environment evidence before parsing it.
acceptance_source="$(find "$owner_mode_root/local_env_acceptance" -mindepth 2 -maxdepth 2 -type f -name LOCAL_ENV_ACCEPTANCE.json -print | sort -V | tail -1)"
test -n "$acceptance_source" && test -f "$acceptance_source" && test ! -L "$acceptance_source"
acceptance_source_root="$(dirname "$acceptance_source")"; private_acceptance="$private_root/acceptance"; mkdir "$private_acceptance"
copy_bound "$acceptance_source_root/SHA256SUMS" "$private_acceptance/SHA256SUMS" >> "$operator_logs/private_copy_receipts.jsonl"
printf '%s\n' "$acceptance_source_root" > "$operator_logs/canonical_acceptance_path.txt"
sha256sum "$private_acceptance/SHA256SUMS" | awk '{print $1}' > "$operator_logs/canonical_acceptance_manifest.sha256"
python3 -I -S - "$private_acceptance/SHA256SUMS" <<'PY' > "$operator_logs/acceptance_rows.tsv"
import re,sys
from pathlib import Path
seen=set()
for line in Path(sys.argv[1]).read_text().splitlines():
 m=re.fullmatch(r"([0-9a-f]{64})  \./([^/\\\x00\r\n]+)",line)
 if not m or m.group(2) in seen: raise SystemExit("unsafe acceptance manifest")
 seen.add(m.group(2)); print(m.group(1),m.group(2),sep="\t")
PY
while IFS=$'\t' read -r expected_sha relative; do
  copy_bound "$acceptance_source_root/$relative" "$private_acceptance/$relative" "$expected_sha" - >> "$operator_logs/private_copy_receipts.jsonl"
done < "$operator_logs/acceptance_rows.tsv"
( cd "$private_acceptance" && sha256sum --strict -c SHA256SUMS >/dev/null )
acceptance_receipt="$private_acceptance/LOCAL_ENV_ACCEPTANCE.json"
python_bin="$(python3 -I -S - "$acceptance_receipt" <<'PY'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
if p.get("status")!="LOCAL_ENV_READY" or p.get("exit_code")!=0: raise SystemExit("local environment is not accepted")
print(p["python_bin"])
PY
)"; test -x "$python_bin"

# Freeze runner/validator/builder from the clean committed tree.
git -C "$repo_root" diff --quiet; git -C "$repo_root" diff --cached --quiet
test -z "$(git -C "$repo_root" ls-files --others --exclude-standard)" || { echo 'repository must be clean' >&2; exit 74; }
repo_head_before="$(git -C "$repo_root" rev-parse HEAD)"; repo_tree_before="$(git -C "$repo_root" rev-parse HEAD^{tree})"
private_code="$private_root/code"; mkdir "$private_code"; runner_path="$(realpath -e -- "$0")"; test ! -L "$0"
validator_source="$repo_root/boltzgen/main/windows_single_owner_20260831/scripts/validate_owner_only_inverse_fold.py"
builder_source="$repo_root/boltzgen/main/windows_single_owner_20260831/scripts/build_owner_pose_anchored_spec.py"
for source_path in "$runner_path" "$validator_source" "$builder_source"; do
  relative="${source_path#"$repo_root/"}"; test "$relative" != "$source_path"
  expected_sha="$(git -C "$repo_root" show "$repo_head_before:$relative" | sha256sum | awk '{print $1}')"; expected_size="$(git -C "$repo_root" cat-file -s "$repo_head_before:$relative")"
  copy_bound "$source_path" "$private_code/$(basename "$source_path")" "$expected_sha" "$expected_size" >> "$operator_logs/private_copy_receipts.jsonl"
done
private_runner="$private_code/$(basename "$runner_path")"; private_validator="$private_code/validate_owner_only_inverse_fold.py"; private_builder="$private_code/build_owner_pose_anchored_spec.py"
chmod 500 "$private_runner" "$private_validator" "$private_builder"
environment_launcher="$(dirname "$python_bin")/boltzgen-wsl-sm120"
launcher_binding_json="$(copy_bound "$environment_launcher" "$private_code/boltzgen-wsl-sm120" - -)"
printf '%s\n' "$launcher_binding_json" >> "$operator_logs/private_copy_receipts.jsonl"
printf '%s\n' "$launcher_binding_json" > "$operator_logs/canonical_launcher_binding.json"
chmod 500 "$private_code/boltzgen-wsl-sm120"; boltzgen_launcher="$private_code/boltzgen-wsl-sm120"
sha256sum "$private_runner" "$private_validator" "$private_builder" "$boltzgen_launcher" > "$operator_logs/code_bindings.SHA256SUMS"
resume_token_sha256="$(python3 -I -S - "$operator_logs/canonical_launcher_binding.json" "$private_root/resume.token" <<'PY'
import hashlib,os,secrets,stat,sys
from pathlib import Path
binding=Path(sys.argv[1]); token_path=Path(sys.argv[2]); fd=os.open(binding,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); chunks=[]
identity=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
try:
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise SystemExit("unsafe launcher binding identity")
 while True:
  block=os.read(fd,1024*1024)
  if not block: break
  chunks.append(block)
 after=os.fstat(fd); current=os.stat(binding,follow_symlinks=False)
finally: os.close(fd)
if identity(before)!=identity(after) or identity(after)!=identity(current): raise SystemExit("launcher binding changed during token creation")
binding_digest=hashlib.sha256(b"".join(chunks)).digest(); token=secrets.token_bytes(32)+binding_digest
tfd=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
try:
 view=memoryview(token)
 while view:
  written=os.write(tfd,view)
  if written<=0: raise SystemExit("short resume token write")
  view=view[written:]
 os.fsync(tfd)
finally: os.close(tfd)
print(hashlib.sha256(token).hexdigest())
PY
)"
printf '%s\n' "$resume_token_sha256" > "$operator_logs/resume_token.sha256"
printf '%s\n' "$repo_head_before" > "$operator_logs/source_commit.txt"; printf '%s\n' "$repo_tree_before" > "$operator_logs/source_tree.txt"

# The GPU-capable phase must execute bytes from the private, accepted copy.
export OWNER_ONLY_IFOLD_PRIVATE_RESUME=1
export OWNER_ONLY_IFOLD_STAGING_ROOT="$staging_root"
export OWNER_ONLY_IFOLD_PRIVATE_ROOT="$private_root"
export OWNER_ONLY_IFOLD_ATTEMPT_ROOT="$attempt_root"
export OWNER_ONLY_IFOLD_ATTEMPT_ID="$attempt_id"
export OWNER_ONLY_IFOLD_RESUME_TOKEN_SHA256="$resume_token_sha256"
set +e
( exec "$private_runner" "$@" )
resume_code=$?
set -e
if [ "$resume_code" -eq 0 ]; then
  terminal_complete=1
  trap - EXIT INT TERM
  exit 0
fi
exit "$resume_code"
else
private_acceptance="$private_root/acceptance"
acceptance_receipt="$private_acceptance/LOCAL_ENV_ACCEPTANCE.json"
python_bin="$(python3 -I -S - "$acceptance_receipt" <<'PY'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
if p.get("status")!="LOCAL_ENV_READY" or p.get("exit_code")!=0: raise SystemExit("private acceptance is not ready")
print(p["python_bin"])
PY
)"
test -x "$python_bin"
repo_head_before="$(head -1 "$operator_logs/source_commit.txt")"
repo_tree_before="$(head -1 "$operator_logs/source_tree.txt")"
private_code="$private_root/code"
private_runner="$private_code/run_owner_only_inverse_fold.sh"
private_validator="$private_code/validate_owner_only_inverse_fold.py"
private_builder="$private_code/build_owner_pose_anchored_spec.py"
boltzgen_launcher="$private_code/boltzgen-wsl-sm120"
environment_launcher="$(dirname "$python_bin")/boltzgen-wsl-sm120"
test "$(realpath -e -- "$0")" = "$private_runner"
git -C "$repo_root" diff --quiet
git -C "$repo_root" diff --cached --quiet
test -z "$(git -C "$repo_root" ls-files --others --exclude-standard)"
test "$(git -C "$repo_root" rev-parse HEAD)" = "$repo_head_before"
test "$(git -C "$repo_root" rev-parse HEAD^{tree})" = "$repo_tree_before"
for code_pair in \
  "run_owner_only_inverse_fold.sh:boltzgen/main/windows_single_owner_20260831/scripts/run_owner_only_inverse_fold.sh" \
  "validate_owner_only_inverse_fold.py:boltzgen/main/windows_single_owner_20260831/scripts/validate_owner_only_inverse_fold.py" \
  "build_owner_pose_anchored_spec.py:boltzgen/main/windows_single_owner_20260831/scripts/build_owner_pose_anchored_spec.py"; do
  private_name=${code_pair%%:*}; repo_relative=${code_pair#*:}
  expected_sha="$(git -C "$repo_root" show "$repo_head_before:$repo_relative" | sha256sum | awk '{print $1}')"
  expected_size="$(git -C "$repo_root" cat-file -s "$repo_head_before:$repo_relative")"
  verify_bound "$private_code/$private_name" "$expected_sha" "$expected_size"
done
verify_code_bindings_and_launcher
canonical_acceptance_root="$(dirname "$(find "$owner_mode_root/local_env_acceptance" -mindepth 2 -maxdepth 2 -type f -name LOCAL_ENV_ACCEPTANCE.json -print | sort -V | tail -1)")"
test "$canonical_acceptance_root" = "$(head -1 "$operator_logs/canonical_acceptance_path.txt")"
accepted_manifest_sha="$(head -1 "$operator_logs/canonical_acceptance_manifest.sha256")"
verify_bound "$canonical_acceptance_root/SHA256SUMS" "$accepted_manifest_sha" -
verify_bound "$private_acceptance/SHA256SUMS" "$accepted_manifest_sha" -
python3 -I -S - "$private_acceptance" <<'PY'
import re,stat,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(strict=True); rows=set()
for line in (root/"SHA256SUMS").read_text().splitlines():
 m=re.fullmatch(r"[0-9a-f]{64}  \./([^/\\\x00\r\n]+)",line)
 if not m or m.group(1) in rows: raise SystemExit("private acceptance manifest format drift")
 rows.add(m.group(1))
observed=set()
for path in root.iterdir():
 info=path.lstat()
 if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise SystemExit("unsafe private acceptance member")
 if path.name!="SHA256SUMS": observed.add(path.name)
if observed!=rows: raise SystemExit("private acceptance closure drift")
PY
( cd "$private_acceptance" && sha256sum --strict -c SHA256SUMS >/dev/null )
fi

# Capture and replay the sealed pose tree privately.
case "$spec_input" in /*) ;; *) echo 'SEALED_SPEC must be absolute' >&2; exit 64;; esac
test ! -L "$spec_input"; source_spec="$(realpath -e -- "$spec_input")"; test "$source_spec" = "$spec_input"; test "$(basename "$source_spec")" = design.yaml
"$python_bin" -I "$private_validator" preflight-spec "$source_spec" > "$operator_logs/source_spec_preflight.json"
source_pose_root="$(dirname "$(dirname "$source_spec")")"; private_pose="$private_root/pose"; mkdir "$private_pose"
source_top_sha="$(python3 -I -S - "$operator_logs/source_spec_preflight.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["top_manifest_sha256"])
PY
)"
copy_bound "$source_pose_root/SHA256SUMS" "$private_pose/SHA256SUMS" "$source_top_sha" - >> "$operator_logs/private_copy_receipts.jsonl"
python3 -I -S - "$private_pose/SHA256SUMS" "$source_pose_root" <<'PY' > "$operator_logs/pose_rows.tsv"
import os,re,sys
from pathlib import Path
seen=set(); root=Path(sys.argv[2])
for line in Path(sys.argv[1]).read_text().splitlines():
 m=re.fullmatch(r"([0-9a-f]{64})  \./([^\\\x00\r\n]+)",line)
 if not m: raise SystemExit("invalid pose manifest")
 rel=m.group(2); p=Path(rel)
 if p.is_absolute() or ".." in p.parts or p.as_posix()!=rel or rel in seen: raise SystemExit("unsafe pose path")
 seen.add(rel); print(m.group(1),os.stat(root/rel,follow_symlinks=False).st_size,rel,sep="\t")
PY
while IFS=$'\t' read -r expected_sha expected_size relative; do
  mkdir -p "$private_pose/$(dirname "$relative")"; copy_bound "$source_pose_root/$relative" "$private_pose/$relative" "$expected_sha" "$expected_size" >> "$operator_logs/private_copy_receipts.jsonl"
done < "$operator_logs/pose_rows.tsv"
spec_path="$private_pose/spec_bundle/design.yaml"; "$python_bin" -I "$private_validator" preflight-spec "$spec_path" > "$operator_logs/spec_preflight.json"

# Parse both accepted runtime contracts and copy only accepted SHA+size assets.
runtime_root="$workspace_root/boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
python3 -I -S - "$private_acceptance/runtime_assets.tsv" "$private_acceptance/runtime_expected_sha256.txt" <<'PY' > "$operator_logs/runtime_assets_contract.tsv"
import csv,re,sys
from pathlib import Path
wanted={"boltzgen1_adherence.ckpt","boltzgen1_ifold.ckpt","boltz2_conf_final.ckpt","mols.zip"}
with Path(sys.argv[1]).open(newline="") as stream: rows=list(csv.DictReader(stream,delimiter="\t"))
if not rows or set(rows[0])!={"expected_sha256","size_bytes","relative_path"}: raise SystemExit("runtime TSV header drift")
table={r["relative_path"]:(r["expected_sha256"],int(r["size_bytes"])) for r in rows}; expected={}
if len(table)!=len(rows): raise SystemExit("duplicate runtime TSV asset name")
for line in Path(sys.argv[2]).read_text().splitlines():
 m=re.fullmatch(r"([0-9a-f]{64})  ([^/\\\x00\r\n]+)",line)
 if not m or m.group(2) in expected: raise SystemExit("runtime expected SHA format drift")
 expected[m.group(2)]=m.group(1)
if not wanted<=set(table) or not wanted<=set(expected): raise SystemExit("accepted runtime assets missing")
for name in sorted(wanted):
 sha,size=table[name]
 if sha!=expected[name] or not re.fullmatch(r"[0-9a-f]{64}",sha) or size<=0: raise SystemExit("runtime contracts disagree")
 print(sha,size,name,sep="\t")
PY
private_runtime="$private_root/runtime"; mkdir "$private_runtime"
while IFS=$'\t' read -r expected_sha expected_size name; do
  copy_bound "$runtime_root/$name" "$private_runtime/$name" "$expected_sha" "$expected_size" >> "$operator_logs/private_copy_receipts.jsonl"
done < "$operator_logs/runtime_assets_contract.tsv"
configure_only_checkpoint="$private_runtime/boltzgen1_adherence.ckpt"; inverse_checkpoint="$private_runtime/boltzgen1_ifold.ckpt"; folding_checkpoint="$private_runtime/boltz2_conf_final.ckpt"; mols_path="$private_runtime/mols.zip"
sha256sum "$configure_only_checkpoint" "$inverse_checkpoint" "$folding_checkpoint" "$mols_path" > "$operator_logs/runtime_assets_private.SHA256SUMS"

# Only private inputs are used below. Acquire the canonical GPU lock last.
df -B1 "$workspace_root" > "$operator_logs/disk_before.txt"
exec 9<"/run/user/$(id -u)"; flock -n 9 || { echo 'the shared single-GPU lock is already held' >&2; exit 75; }
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits > "$operator_logs/gpu_compute_processes_before.csv"
test ! -s "$operator_logs/gpu_compute_processes_before.csv" || { echo 'another GPU compute process is active' >&2; exit 75; }
run_logged() { local label=$1; shift; local started ended result; started="$(date +%s)"; set +e; "$@" > "$operator_logs/$label.stdout.txt" 2> "$operator_logs/$label.stderr.txt"; result=$?; set -e; ended="$(date +%s)"; printf '%s\n' "$result" > "$operator_logs/$label.exit_code.txt"; printf '%s\n' "$((ended-started))" > "$operator_logs/$label.duration_seconds.txt"; return "$result"; }

run_logged configure "$boltzgen_launcher" configure "$spec_path" --output "$staging_root" \
  --steps inverse_folding folding analysis filtering --protocol nanobody-anything --only_inverse_fold \
  --num_designs 1 --diffusion_batch_size 1 --inverse_fold_num_sequences "$num_sequences" \
  --inverse_fold_avoid C --budget "$num_sequences" --design_checkpoints "$configure_only_checkpoint" \
  --inverse_fold_checkpoint "$inverse_checkpoint" --folding_checkpoint "$folding_checkpoint" \
  --moldir "$mols_path" --devices 1 --num_workers 4 --use_kernels auto \
  --config inverse_folding sampling_steps=200 recycling_steps=3 diffusion_samples=1 trainer.precision=32 \
  --config folding sampling_steps=200 recycling_steps=3 diffusion_samples=5 trainer.precision=bf16-mixed \
  --config analysis liability_modality=antibody --config filtering modality=antibody filter_bindingsite=true

run_logged resolved_config_validation "$python_bin" -I - "$staging_root" "$spec_path" "$num_sequences" "$private_root" <<'PY'
import json,sys,yaml
from pathlib import Path
root=Path(sys.argv[1]); spec=sys.argv[2]; count=int(sys.argv[3]); private=Path(sys.argv[4]).resolve(); names=[r["name"] for r in yaml.safe_load((root/"steps.yaml").read_text())["steps"]]
if names!=["inverse_folding","folding","analysis","filtering"] or (root/"config/design.yaml").exists(): raise SystemExit("only-inverse steps drift")
inv=yaml.safe_load((root/"config/inverse_folding.yaml").read_text()); fold=yaml.safe_load((root/"config/folding.yaml").read_text())
if inv.get("name")!="inverse_fold_only" or inv["data"]["cfg"].get("yaml_path")!=[spec] or inv["data"]["cfg"].get("multiplicity")!=count or fold.get("diffusion_samples")!=5: raise SystemExit("resolved topology drift")
expected_roles={
 "inverse_checkpoint":private/"runtime/boltzgen1_ifold.ckpt",
 "folding_checkpoint":private/"runtime/boltz2_conf_final.ckpt",
 "inverse_moldir":private/"runtime/mols.zip",
 "folding_moldir":private/"runtime/mols.zip",
}
observed_roles={
 "inverse_checkpoint":Path(inv.get("checkpoint","")).resolve(),
 "folding_checkpoint":Path(fold.get("checkpoint","")).resolve(),
 "inverse_moldir":Path(inv["data"]["cfg"].get("moldir","")).resolve(),
 "folding_moldir":Path(fold["data"]["cfg"].get("moldir","")).resolve(),
}
if observed_roles!=expected_roles: raise SystemExit(f"resolved private runtime role mismatch: {observed_roles!r}")
adherence=(private/"runtime/boltzgen1_adherence.ckpt").resolve()
if adherence in observed_roles.values(): raise SystemExit("configure-only adherence checkpoint entered an inference role")
print(json.dumps({"status":"PASS","steps":names,"sequences":count,"fold_samples":5,"private_input_root":str(private),"resolved_runtime_roles":{k:str(v) for k,v in observed_roles.items()},"configure_only_adherence_used_for_inference":False},indent=2,sort_keys=True))
PY
mv "$operator_logs/resolved_config_validation.stdout.txt" "$operator_logs/resolved_config_contract.json"
( cd "$staging_root" && find config -type f -print0 | sort -z | xargs -0 sha256sum > operator_logs/resolved_config.SHA256SUMS )
run_logged inverse_folding "$boltzgen_launcher" execute "$staging_root" --no_subprocess --steps inverse_folding
run_logged inverse_gate "$python_bin" -I "$private_validator" validate-inverse "$staging_root" "$spec_path" --sequences "$num_sequences"
mv "$operator_logs/inverse_gate.stdout.txt" "$operator_logs/inverse_gate.json"
for stage in folding analysis filtering; do run_logged "$stage" "$boltzgen_launcher" execute "$staging_root" --no_subprocess --steps "$stage"; done
run_logged validation "$python_bin" -I "$private_validator" validate-run "$staging_root" "$spec_path" --sequences "$num_sequences" --fold-samples 5
mv "$operator_logs/validation.stdout.txt" "$operator_logs/output_validation.json"

python3 -I -S - "$operator_logs/ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json" "$operator_logs/output_validation.json" "$operator_logs/spec_preflight.json" "$operator_logs/source_spec_preflight.json" "$operator_logs/runtime_assets_contract.tsv" "$operator_logs/inverse_gate.json" "$run_id" "$attempt_id" <<'PY'
import csv,json,sys
from pathlib import Path
receipt,validation_path,private_preflight,source_preflight,assets_path,inverse_gate_path,run_id,attempt_id=sys.argv[1:]; validation=json.loads(Path(validation_path).read_text()); private_pose=json.loads(Path(private_preflight).read_text()); source_pose=json.loads(Path(source_preflight).read_text()); inverse_gate=json.loads(Path(inverse_gate_path).read_text()); assets={}
with Path(assets_path).open(newline="") as stream:
 for sha,size,name in csv.reader(stream,delimiter="\t"): assets[name]={"accepted_sha256":sha,"accepted_size_bytes":int(size)}
payload={"schema_version":"WINDOWS_OWNER_ONLY_INVERSE_FOLD_RUN_V1","status":"ONLY_INVERSE_FOLD_FINALIZING","exit_code":0,"authority":"WINDOWS_CODEX","scope":"DEVELOPMENT_ONLY","generation_mode":"ONLY_INVERSE_FOLD_FROM_POSE_SPEC","design_diffusion_performed":False,"input_backbone_count":1,"inverse_fold_num_sequences":validation["observed_sequence_candidates"],"candidate_count":validation["observed_sequence_candidates"],"fold_samples_per_candidate":5,"fold_sample_count":validation["observed_fold_sample_count"],"candidate_ids":validation["candidate_ids"],"unique_designed_sequence_count":validation["unique_designed_sequence_count"],"checkpoint_hashes_and_sizes":assets,"configure_only_design_checkpoint_used_for_inference":False,"source_pose":source_pose,"private_pose_copy":private_pose,"inverse_stage_gate":inverse_gate,"output_validation":validation,"run_id":run_id,"attempt_id":attempt_id,"formal_gate_claimed":False,"scientific_claim_boundary":"AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE"}; Path(receipt).write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n")
PY

"$python_bin" -I "$private_validator" preflight-spec "$spec_path" > "$operator_logs/spec_preflight_terminal.json"
cmp -s "$operator_logs/spec_preflight.json" "$operator_logs/spec_preflight_terminal.json"
( cd / && sha256sum --strict -c "$operator_logs/runtime_assets_private.SHA256SUMS" >/dev/null )
verify_code_bindings_and_launcher
( cd "$staging_root" && sha256sum --strict -c operator_logs/resolved_config.SHA256SUMS >/dev/null )
test "$(git -C "$repo_root" rev-parse HEAD)" = "$repo_head_before"; test "$(git -C "$repo_root" rev-parse HEAD^{tree})" = "$repo_tree_before"
python3 -I -S - "$operator_logs/ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json" <<'PY'
import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]); value=json.loads(p.read_text()); value["status"]="ONLY_INVERSE_FOLD_COMPLETE"; t=p.with_name("."+p.name+".tmp"); t.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n"); os.replace(t,p)
PY
printf '%s\n' ONLY_INVERSE_FOLD_COMPLETE > "$operator_logs/STATUS.txt"; printf '0\n' > "$operator_logs/exit_code.txt"; date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/ended_at_utc.txt"
seal_tree; publish_staging; terminal_complete=1; trap - EXIT INT TERM
cleanup_private || printf 'warning: completed output published but private cleanup failed: %s\n' "$private_root" >&2
printf 'ONLY_INVERSE_FOLD_COMPLETE path=%s\n' "$attempt_root"
