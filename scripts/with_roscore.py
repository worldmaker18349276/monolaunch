#!/usr/bin/env python3

import subprocess
import signal
import sys
import time
from monolaunch.monoresource import Machine

def main():
    machine = Machine.parse(sys.argv[1])
    command = sys.argv[2:]

    machine = machine.reduce_local()
    ssh = subprocess.Popen(machine.command(["roscore"]))

    def cleanup(*_):
        ssh.terminate()
        try:
            ssh.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ssh.kill()
            ssh.wait()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, cleanup)

    try:
        # Give roscore time to start.
        time.sleep(1)

        result = subprocess.run(command)

    finally:
        cleanup()

    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
