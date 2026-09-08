#!/usr/bin/env python3

from pathlib import Path
import shlex
import subprocess
import signal
import sys
import os
from typing import Any, Tuple
from monolaunch.monoresource import Machine
import xml.etree.ElementTree as ET


def _get_local_and_master_machine(launch_file: Path) -> Tuple[Machine, Machine]:
    root = ET.parse(launch_file).getroot()

    machines = {
        machine.get("name"): machine.attrib
        for machine in root.findall("machine")
    }

    def get_machine(machine_name: str) -> Machine:
        machine_tag = machines.get(machine_name)
        if machine_tag is None:
            raise RuntimeError(f"[with_roscore] cannot find machine tag with name {machine_name!r}")
        user = machine_tag.get("user", "")
        password = machine_tag.get("password", "")
        address = machine_tag.get("address", "")
        env_loader = machine_tag.get("env-loader", "")
        return Machine(user=user, password=password, address=address, env_loader=tuple(shlex.split(env_loader)))

    local = get_machine("local")
    if not local.is_local():
        raise RuntimeError("[with_roscore] local machine is not local")

    master = None
    for node in root.findall("master"):
        machine_name = node.get("machine")
        if machine_name is None:
            raise RuntimeError("[with_roscore] cannot find machine attr in the master tag")
        master = get_machine(machine_name)
        break

    if master is None:
        raise RuntimeError("[with_roscore] cannot find master tag")

    return local, master

def main():
    command = sys.argv[1:]
    if len(command) < 2 or not command[1].endswith(".launch"):
        raise ValueError("[with_roscore] with_roscore.py must be prefixed before `roslaunch <launch_file.launch> ...`")
    command[1:1] = ["--wait"] # force to wait my roscore
    os.environ["NO_RELAUNCH_WITH_ROSCORE"] = '1'

    local, master = _get_local_and_master_machine(Path(command[2]))

    ros_master_uri = f"http://{master.address}:11311"
    os.environ["ROS_MASTER_URI"] = ros_master_uri

    roscore = master.command(["roscore"])
    print("[with_roscore] run roscore", roscore)
    roscore_proc = subprocess.Popen(roscore)
    # TODO: exit if roscore is dead

    def cleanup(*_: Any):
        print("[with_roscore] cleanup roscore...")
        roscore_proc.terminate()
        try:
            roscore_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            roscore_proc.kill()
            roscore_proc.wait()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, cleanup)

    try:
        command = local.command(command)
        print("[with_roscore] run roslaunch", command)
        result = subprocess.run(command)

    finally:
        cleanup()

    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
