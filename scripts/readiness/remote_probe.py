"""Program text of the read-only probe the readiness check runs on Lenovo.

``check.py`` embeds ``PROGRAM`` in the signed job command; this host never
executes it.  It lives beside the Dell-side orchestration so both files stay
reviewable and inside the file token guard.

The program performs local filesystem/process lookup and git object-database
reads.  It has no network operation and writes no file.  Its input contains
paths, but its JSON output contains component names and states only.
"""

PROGRAM = r'''import hashlib,json,shutil,subprocess,sys
from pathlib import Path
cfg=json.loads(__import__("base64").b64decode(sys.argv[1]).decode("utf-8"))
out=[]
def add(name,state,reason,next_action):
 out.append({"name":name,"location":"lenovo","state":state,"reason":reason,"nextAction":next_action})
def path_check(item):
 p=Path(item["path"]); kind=item["type"]
 ok=p.is_file() if kind=="file" else p.is_dir()
 if not ok:
  add(item["name"],"missing",kind+" is absent","provide the required "+item["name"]); return
 expected=item.get("sha256")
 if expected:
  try: actual=hashlib.sha256(p.read_bytes()).hexdigest()
  except OSError:
   add(item["name"],"unconfirmed","file could not be read","restore readable access"); return
  if actual!=expected:
   add(item["name"],"stale","digest differs from the selected revision","update "+item["name"]); return
 add(item["name"],"confirmed",kind+" is present"+(" at the expected digest" if expected else ""),"none")
for item in cfg.get("components",[]): path_check(item)
for item in cfg.get("tools",[]):
 command=item["command"]
 found=(Path(command).is_file() if Path(command).is_absolute() else shutil.which(command) is not None)
 add(item["name"],"confirmed" if found else "missing","executable is available" if found else "executable is absent","none" if found else "install or select the required executable")
print(json.dumps({"schema":"lenovo-readiness-probe/1","scenario":cfg["scenario"],"components":out},separators=(",",":")))
'''
