"""This code's version, from its git tags (see release.bash), the same way in every cmo repo:
1.3.0 on a release, 1.3.0+4 four commits after it, 1.3.0+4.dirty (1.3.0+dirty on the release itself)
with uncommitted changes too, unknown outside a git checkout or before the first release.

  import version
  version.CODE          # "cmo_client:1.3.0", for log lines and JSON files
  version.code_version(path)   # "1.3.0" for the git checkout at path
"""
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def code_version(path):
    try:
        r = subprocess.run(["git", "-C", str(path), "describe", "--tags", "--match", "v[0-9]*", "--long", "--dirty"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    m = re.match(r"^v([0-9.]+)-([0-9]+)-g[0-9a-f]+(-dirty)?$", r.stdout.decode().strip())
    if r.returncode != 0 or not m:
        return "unknown"
    version, ahead, dirty = m.groups()
    if ahead != "0":
        version += "+" + ahead
    if dirty:
        version += ".dirty" if ahead != "0" else "+dirty"
    return version


VERSION = code_version(HERE)
CODE = "cmo_client:" + VERSION
