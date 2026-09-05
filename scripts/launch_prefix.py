#!/usr/bin/env python3
"""
usage: launch_prefix.py <func name> <func code> <args num> <args>* <forward cmd>

example:
```
rosrun monolaunch launch_prefix.py set_abc '
def set_abc(n):
    import os
    os.environ["abc"] = n
' 1 123 $(which printenv) abc
```
"""

import sys
import os

name = sys.argv[1]
code = sys.argv[2]
args_num = int(sys.argv[3])
args = tuple(sys.argv[4:4+args_num])
del sys.argv[:4+args_num]

print(f"[launch_prefix.py] run {name}{args!r}, where\n{code}", file=sys.stderr)
exec(code)
exec(f"{name}{args!r}")

os.execv(sys.argv[0], sys.argv)
