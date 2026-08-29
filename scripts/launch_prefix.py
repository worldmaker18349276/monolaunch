#!/usr/bin/env python3

import sys
import os

name = sys.argv[0]
code = sys.argv[1]
args_num = int(sys.argv[2])
args = tuple(sys.argv[3:3+args_num])
del sys.argv[:3+args_num]

exec(code)
exec(f"{name}{args!r}")

os.execv(sys.argv[0], sys.argv)
