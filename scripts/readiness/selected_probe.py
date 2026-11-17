"""Program text of the fixed target-host preamble for one selected check.

``selected.py`` embeds ``PROGRAM`` in the single signed Lenovo job.  The
program re-verifies the requested cwd, the check executable and the checkout
revision on the target host, and prints one bounded JSON line.  A refusal
prints ``state: NOT_RUN`` and exits 78 before the check is spawned; a check
timeout or abnormal exit prints ``state: FAIL`` and exits 79.  Otherwise it
prints ``state: RUN`` with the real child return code and exits with that
same code, so the signed envelope exit and the receipt agree.

The check argv is carried as data inside the base64 argument and is executed
without a shell, so characters such as ``%``, ``!`` or ``|`` stay literal.
Child stdout/stderr are discarded: the only bytes on stdout are this fixed
receipt, which keeps output bounded and never relays private check output.
"""

PROGRAM = r'''import base64,json,shutil,subprocess,sys
from pathlib import Path

def emit(state, reason, code, observed):
    print(json.dumps({"schema":"lenovo-selected-check/1","state":state,
                      "reason":reason,"exitCode":code,
                      "observedRevision":observed},separators=(",",":")))
    if state == "RUN":
        raise SystemExit(code)
    raise SystemExit(78 if state == "NOT_RUN" else 79)

cfg=json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
cwd=cfg["cwd"]
if not Path(cwd).is_dir(): emit("NOT_RUN","cwd-missing",None,None)
tool=cfg["argv"][0]
found=(Path(tool).is_file() if Path(tool).is_absolute()
       else shutil.which(tool) is not None)
if not found: emit("NOT_RUN","tool-missing",None,None)
try:
    run=subprocess.run([cfg["git"],"-C",cwd,"rev-parse","HEAD"],
                       capture_output=True,text=True,timeout=15,check=False)
    actual=run.stdout.strip().lower()
    if run.returncode!=0 or len(actual)!=40: emit("NOT_RUN","revision-unconfirmed",None,None)
    if actual!=cfg["expectedRevision"]: emit("NOT_RUN","revision-stale",None,actual)
except Exception: emit("NOT_RUN","revision-unconfirmed",None,None)
try:
    proc=subprocess.run(cfg["argv"],cwd=cwd,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=cfg["timeoutSeconds"],check=False)
except subprocess.TimeoutExpired: emit("FAIL","timeout",None,actual)
except OSError: emit("NOT_RUN","command-unavailable",None,actual)
code=proc.returncode
if not isinstance(code,int) or code<0: emit("FAIL","abnormal-exit",None,actual)
emit("RUN","completed",code,actual)
'''
