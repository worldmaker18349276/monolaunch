#!/usr/bin/env python3

from pathlib import Path
import shlex
import subprocess
import signal
import sys
import os
import time
from typing import Optional
from monolaunch.monoresource import Machine
import xml.etree.ElementTree as ET


def _get_master_machine(launch_file: Path) -> Optional[Machine]:
    root = ET.parse(launch_file).getroot()

    machines = {
        machine.get("name"): machine.attrib
        for machine in root.findall("machine")
    }

    for node in root.findall("master"):
        machine_name = node.get("machine")
        assert machine_name is not None
        machine_tag = machines.get(machine_name)
        assert machine_tag is not None
        user = machine_tag.get("user", "")
        password = machine_tag.get("password", "")
        address = machine_tag.get("address", "")
        env_loader = machine_tag.get("env-loader", "")
        return Machine(user=user, password=password, address=address, env_loader=tuple(shlex.split(env_loader)))

    return None

def main():
    command = sys.argv[1:]
    if len(command) < 2 or command[0] != "roslaunch" or not command[1].endswith(".launch"):
        raise ValueError("with_roscore.py must be prefixed before `roslaunch <launch_file.launch> ...`")
    machine = _get_master_machine(Path(command[1])) or Machine()

    ros_master_uri = f"http://{machine.address}:11311"
    os.environ["ROS_MASTER_URI"] = ros_master_uri

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
