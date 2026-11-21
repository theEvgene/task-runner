"""Program text of the read-only probe the readiness check runs on Lenovo.

``check.py`` embeds ``PROGRAM`` in the signed job command; this host never
executes it.  It lives beside the Dell-side orchestration so both files stay
reviewable and inside the file token guard.

The program performs local filesystem/process lookup and git object-database
reads.  It has no network operation and writes no file.  Its input contains
paths, but its JSON output contains component names, states, and for directory
components the observed 40-hex checkout revision plus a digest of that cwd.

Issue #160 adds the optional ``environment`` input: installer commands are
resolved with ``Get-Command`` in the installer shell (naming the
module-available-but-command-missing case that broke PAT rollout), and
runtime executables are version-checked against an owner minimum.  Those
entries emit an allowlisted ``kind``/``detail`` pair for the Dell gate;
the probe's reason/nextAction strings are advisory only.  The Dell
report rebuilds every bounded reason from the trusted configuration
and never relays worker text; observed versions, paths, keys, and
worker stderr never enter the report.
"""

PROGRAM = r'''import hashlib,json,re,shutil,subprocess,sys
from pathlib import Path
cfg=json.loads(__import__("base64").b64decode(sys.argv[1]).decode("utf-8"))
out=[]
def add(name,state,reason,next_action,**extra):
 out.append({"name":name,"location":"lenovo","state":state,"reason":reason,"nextAction":next_action,**extra})
def revision(path):
 try:
  run=subprocess.run(["git","-C",path,"rev-parse","HEAD"],capture_output=True,text=True,timeout=15,check=False)
  value=run.stdout.strip().lower()
  if run.returncode==0 and len(value)==40 and all(char in "0123456789abcdef" for char in value):
   return value
 except Exception:
  pass
 return None
def revision_fields(item):
 return {"observedRevision":revision(item["path"]),
         "cwdDigest":hashlib.sha256(item["path"].encode("utf-8")).hexdigest()}
def path_check(item):
 p=Path(item["path"]); kind=item["type"]
 extra=revision_fields(item) if kind=="directory" else {}
 ok=p.is_file() if kind=="file" else p.is_dir()
 if not ok:
  add(item["name"],"missing",kind+" is absent","provide the required "+item["name"],**extra); return
 expected=item.get("sha256")
 if expected:
  try: actual=hashlib.sha256(p.read_bytes()).hexdigest()
  except OSError:
   add(item["name"],"unconfirmed","file could not be read","restore readable access",**extra); return
  if actual!=expected:
   add(item["name"],"stale","digest differs from the selected revision","update "+item["name"],**extra); return
 add(item["name"],"confirmed",kind+" is present"+(" at the expected digest" if expected else ""),"none",**extra)
for item in cfg.get("components",[]): path_check(item)
for item in cfg.get("tools",[]):
 command=item["command"]
 found=(Path(command).is_file() if Path(command).is_absolute() else shutil.which(command) is not None)
 add(item["name"],"confirmed" if found else "missing","executable is available" if found else "executable is absent","none" if found else "install or select the required executable")
env=cfg.get("environment") or {}
shell_cfg=env.get("shell")
if shell_cfg is None:
 probe=shutil.which("powershell") or shutil.which("pwsh")
 base=[probe] if probe else None
elif isinstance(shell_cfg,str) and shell_cfg:
 base=[shell_cfg]
elif isinstance(shell_cfg,list) and shell_cfg and all(isinstance(part,str) and part for part in shell_cfg):
 base=list(shell_cfg)
else:
 base=False
def ps_run(code):
 try:
  run=subprocess.run(base+["-NoProfile","-NonInteractive","-Command",code],capture_output=True,text=True,timeout=15,check=False)
  return run.returncode
 except Exception:
  return None
VERSION_RE=re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
OBSERVED_VERSION_RE=re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")
def parse_version(text):
 found=VERSION_RE.search(text or "")
 if not found: return None
 return (int(found.group(1)),int(found.group(2) or 0),int(found.group(3) or 0))
def parse_observed_version(text):
 found=OBSERVED_VERSION_RE.findall(text or "")
 versions=[(int(major),int(minor),int(patch or 0)) for major,minor,patch in found]
 distinct=list(dict.fromkeys(versions))
 return distinct[0] if len(distinct)==1 else None
for item in env.get("commands") or []:
 name=item.get("name") if isinstance(item,dict) else None
 module=item.get("module") if isinstance(item,dict) else None
 if not isinstance(name,str) or not name:
  add("unknown","unconfirmed","installer command entry is malformed","repair or separately verify the required condition",kind="command",detail="probe-error"); continue
 if base is None:
  add(name,"unconfirmed","installer shell is unavailable","restore the installer shell on the target host",kind="command",detail="shell-unavailable"); continue
 if base is False:
  add(name,"unconfirmed","installer shell selection is malformed","repair or separately verify the required condition",kind="command",detail="probe-error"); continue
 seen=ps_run("Get-Command -Name '"+name+"' -ErrorAction Stop | Out-Null")
 if seen==0:
  add(name,"confirmed","required command is available","none",kind="command",detail="available"); continue
 if seen is None:
  add(name,"unconfirmed","installer command check failed","repair or separately verify the required condition",kind="command",detail="probe-error"); continue
 if not isinstance(module,str) or not module:
  add(name,"missing","required command is absent","provide the required command on the target host",kind="command",detail="command-absent"); continue
 listed=ps_run("Get-Module -ListAvailable -Name '"+module+"' | Out-Null")
 if listed is None:
  add(name,"unconfirmed","installer module check failed","repair or separately verify the required condition",kind="command",detail="probe-error"); continue
 if listed!=0:
  add(name,"missing","required command and its module are absent","provide the required module on the target host",kind="command",detail="module-missing"); continue
 after=ps_run("Import-Module '"+module+"' -ErrorAction Stop; Get-Command -Name '"+name+"' -ErrorAction Stop | Out-Null")
 if after is None:
  add(name,"unconfirmed","installer module check failed","repair or separately verify the required condition",kind="command",detail="probe-error"); continue
 if after==0:
  add(name,"missing","required command needs its module imported in the installer process","import the module in the installer process before use",kind="command",detail="module-available"); continue
 add(name,"missing","required command is absent even after importing its listed module","provide the required command on the target host",kind="command",detail="command-absent")
for item in env.get("runtimes") or []:
 name=item.get("name") if isinstance(item,dict) else None
 command=item.get("command") if isinstance(item,dict) else None
 minimum=item.get("minimum") if isinstance(item,dict) else None
 wanted=parse_version(minimum) if isinstance(minimum,str) else None
 if not isinstance(name,str) or not name or not isinstance(command,str) or not command or (isinstance(minimum,str) and wanted is None):
  label=name if isinstance(name,str) and name else "unknown"
  add(label,"unconfirmed","installer runtime entry is unreadable","repair or separately verify the required runtime",kind="runtime",detail="unreadable"); continue
 try:
  run=subprocess.run([command,"--version"],capture_output=True,text=True,timeout=15,check=False)
 except FileNotFoundError:
  add(name,"missing","required runtime executable is absent","install or select the required runtime on the target host",kind="runtime",detail="absent"); continue
 except Exception:
  add(name,"unconfirmed","required runtime version could not be read","repair or separately verify the required runtime",kind="runtime",detail="unreadable"); continue
 observed=parse_observed_version((run.stdout or "")+"\n"+(run.stderr or ""))
 if run.returncode!=0 or observed is None:
  add(name,"unconfirmed","required runtime version could not be read","repair or separately verify the required runtime",kind="runtime",detail="unreadable"); continue
 if wanted is not None and observed<wanted:
  add(name,"stale","required runtime is below the required minimum","update the required runtime",kind="runtime",detail="below-minimum"); continue
 add(name,"confirmed","required runtime is available","none",kind="runtime",detail="meets-minimum" if wanted is not None else "no-requirement")
print(json.dumps({"schema":"lenovo-readiness-probe/2","scenario":cfg["scenario"],"components":out},separators=(",",":")))
'''
