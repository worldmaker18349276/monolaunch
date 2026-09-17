#!/usr/bin/env python3
"""
uage:
with_roscore.py roslaunch <launch_file.launch> ...
with_roscore.py --filename <launch_file.launch> roslaunch ...
"""

import contextlib
from pathlib import Path
import shlex
import sys
import os
from typing import Optional, Tuple
from monolaunch.monoresource import Machine, prun
import xml.etree.ElementTree as ET


def is_master_online() -> bool:
    import rosgraph # pyright: ignore[reportMissingImports]
    return rosgraph.is_master_online() # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]

def _get_local_and_master_machine(launch_file: Path) -> Tuple[Machine, Optional[Machine], str]:
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
        raise RuntimeError(f"[with_roscore] local machine is not local: {local}")

    master = None
    master_mode = "auto"
    for node in root.findall("master"):
        machine_name = node.get("machine")
        if machine_name is None:
            raise RuntimeError(f"[with_roscore] cannot find machine attr in the master tag: {ET.tostring(node)}")
        master = get_machine(machine_name)

        master_mode = node.get("mode") or master_mode
        if master_mode not in ("start", "wait", "auto"):
            raise RuntimeError(f"[with_roscore] invalid mode attr in the master tag: {master_mode}")

        break

    return local, master, master_mode

def main():
    if len(sys.argv) <= 1:
        raise ValueError("[with_roscore] usage: with_roscore.py roslaunch <launch_file.launch> ...")

    if sys.argv[1] != "--filename":
        print("[with_roscore] --filename is not provided, will determine it automatically")
        if len(sys.argv) < 3 or not sys.argv[1].endswith("/roslaunch") or not sys.argv[2].endswith(".launch"):
            raise ValueError("[with_roscore] `with_roscore.py` must be prefixed before `roslaunch <launch_file.launch> ...`")
        filename = Path(sys.argv[2]).resolve()
        sys.argv[1:1] = ["--filename", str(filename)]

    if len(sys.argv) < 4 or not sys.argv[2].endswith(".launch") or not sys.argv[3].endswith("/roslaunch"):
        raise ValueError("[with_roscore] `with_roscore.py --filename <filename>` must be prefixed before `roslaunch ...`")
    if not Path(sys.argv[2]).exists():
        raise ValueError(f"[with_roscore] given filename {sys.argv[2]} doesn't exist")

    filename = Path(sys.argv[2])
    command = sys.argv[3:]

    local, master, master_mode = _get_local_and_master_machine(filename)
    
    if master is not None:
        ros_master_uri = f"http://{master.address}:11311"
        os.environ["ROS_MASTER_URI"] = ros_master_uri

    if master is None:
        command = local.command(command)
        print("[with_roscore] no master tag, just run roslaunch:\n" + shlex.join(command))
        os.execv(command[0], command)

    if master_mode == "wait":
        command[1:1] = ["--wait"]
        command = local.command(command)
        print("[with_roscore] master mode is wait, just run roslaunch:\n" + shlex.join(command))
        os.execv(command[0], command)

    if master_mode == "auto" and master.is_local():
        command = local.command(command)
        print("[with_roscore] master machine is local, just run roslaunch:\n" + shlex.join(command))
        os.execv(command[0], command)

    if master_mode == "auto" and is_master_online():
        command[1:1] = ["--wait"]
        command = local.command(command)
        print("[with_roscore] master is online, just run roslaunch:\n" + shlex.join(command))
        os.execv(command[0], command)

    roscore = master.command(["roscore"], tt=True)
    command[1:1] = ["--wait"] # force to wait my roscore
    command = local.command(command)

    print("[with_roscore] run roscore:\n" + shlex.join(roscore))
    #                  _________________ to prevent SIGINT propagates into subprocess
    with prun("roscore", roscore, start_new_session=True):
        print("[with_roscore] run roslaunch:\n" + shlex.join(command))
        with prun("roslaunch", command, force_exit=True, exit_timeout=30) as roslaunch_proc:
            result_returncode = roslaunch_proc.wait()
    sys.exit(result_returncode)

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
