"""
sync resources before launch, so that resources can be managed in single place.

it uses commands: ssh, sshpass, rsync
"""
from inspect import cleandoc
import re
import shlex
import subprocess
import dataclasses
from pathlib import Path
import socket
import ipaddress

import urllib.parse
from monolaunch.yaml_utils import assert_JSON, Link, load_YAML


def urlquote(s: str, unsafe: str = r"%#@/:;?") -> str:
    return re.sub(
        f"[{re.escape(unsafe)}]",
        lambda m: ''.join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        s,
    )

class SchemeParseError(Exception):
    def __init__(self, scheme: str, url: str, format: str = ""):
        self.scheme = scheme
        self.url = url
        self.format = format
    
    def __str__(self):
        return f"invalid {self.scheme} scheme url: {self.url}" + (f"\nformat: {self.format}" if self.format else "")

@dataclasses.dataclass(frozen=True)
class Machine:
    user: str = ""
    password: str = ""
    address: str = ""
    env_loader: str = ""

    @staticmethod
    def parse(url: str) -> "Machine":
        """
        parse machine scheme url
        format: machine://user:pswd@addr/path/to/env_loader.sh
        """
        parse_result = urllib.parse.urlparse(url, scheme="machine")
        if parse_result.scheme != "machine":
            raise SchemeParseError("machine", url, "machine://user:pswd@addr/path/to/env_loader.sh")

        user = urllib.parse.unquote(parse_result.username or "")
        password = urllib.parse.unquote(parse_result.password or "")
        address = parse_result.hostname or ""
        env_loader = urllib.parse.unquote(parse_result.path)

        return Machine(user=user, password=password, address=address, env_loader=env_loader)
    
    def reduce_local(self) -> "Machine":
        if not self.address or self.is_loopback():
            return Machine(user="", password="", address="", env_loader=self.env_loader)
        return self

    def get_netloc(self) -> str:
        netloc = self.address
        if self.user:
            auth = urlquote(self.user, unsafe="%@:")
            if self.password:
                auth += ":" + urlquote(self.password, unsafe="%@:")
            netloc = auth + "@" + netloc
        return netloc

    def __str__(self) -> str:
        path = urlquote(self.env_loader, unsafe="%;#?")
        return urllib.parse.urlunparse(("machine", self.get_netloc(), path, "", "", ""))

    def is_loopback(self) -> bool:
        if not self.address:
            return True

        # Direct IP address
        try:
            return ipaddress.ip_address(self.address).is_loopback
        except ValueError:
            pass

        # Hostname: resolve all addresses
        try:
            infos = socket.getaddrinfo(self.address, None)
        except socket.gaierror:
            return False

        return any(
            ipaddress.ip_address(addr[0]).is_loopback
            for _family, _, _, _, addr in infos
        )

def remote_expandvars(machine: Machine, path: str) -> str:
    password_args = ["sshpass", "-p", machine.password] if machine.password else []
    remote_args = ["ssh", f"{machine.user}@{machine.address}" if machine.user else machine.address] if machine.address else ["bash", "-c"]
    remote_cmd = [
        "python3", "-c",
        "; ".join([
            "import os",
            "os.environ['DOLLARSIGN'] = '$'",
            "os.environ['ROS_HOME'] = os.environ.get('ROS_HOME', os.path.expandvars('$HOME/.ros'))",
            f"print(os.path.expandvars({str(path)!r}), end='')"
        ])
    ]
    if machine.env_loader:
        remote_cmd = [machine.env_loader, *remote_cmd]

    result = subprocess.run([
        *password_args,
        *remote_args,
        shlex.join(remote_cmd),
    ], capture_output=True, text=True, check=True)
    return result.stdout


def rsync(source: str, destination: str, machine: Machine, check_only: bool):
    password_args = ["sshpass", "-p", machine.password] if machine.password else []

    if Path(source).exists() and Path(source).is_dir():
        source = str(Path(source)) + "/"

    remote_args = ["ssh", f"{machine.user}@{machine.address}" if machine.user else machine.address] if machine.address else ["bash", "-c"]
    dst_parent = str(Path(destination).parent)
    print(f"create parent directory {dst_parent}")
    subprocess.run([
        *password_args,
        *remote_args,
        shlex.join(["mkdir", "-p", dst_parent]),
    ], check=True)

    destination_ = (f"{machine.user}@" if machine.user else "") + (f"{machine.address}:" if machine.address else "") + destination
    if check_only:
        print(f"check transfer {source} -> {destination_}")
        result = subprocess.run([
            *password_args,
            "rsync", "-azn", "--checksum", "--itemize-changes", "--del",
            source, destination_,
        ], check=True)
        if bool(result.stdout):
            raise ValueError(f"resource need to be transferred: {source} -> {destination_}")
    else:
        print(f"transfer {source} -> {destination_}")
        subprocess.run([
            *password_args,
            "rsync", "-avz", "--checksum",
            source, destination_,
        ], check=True)

def sync(params_link: str):
    """
    load resources to given paths under remote machines.

    params_link is a link to a yaml file in the format:
    ```
    - source: /path/to/source/in/local/machine
      destination: ${ROS_HOME}/path/to/destination/in/remote/machine
      machine: machine://user:pswd@addr/path/to/env_loader.sh
    ...
    ```
    """
    print(f"sync resources: {params_link}")
    params_link_ = Link.parse(params_link)
    params = assert_JSON(load_YAML(params_link_))
    if not isinstance(params, list):
        raise TypeError(f"{params_link_} is not seq")
    for i, resource in enumerate(params):
        curr_link = params_link_.append(i)
        if not isinstance(resource, dict):
            raise TypeError(f"{curr_link} is not map")
        
        param_machine = resource.get("machine")
        if not isinstance(param_machine, str):
            raise TypeError(f"{curr_link}/machine is not str")
        
        source = resource.get("source")
        if not isinstance(source, str):
            raise TypeError(f"{curr_link}/source is not str")
        
        destination = resource.get("destination")
        if not isinstance(destination, str):
            raise TypeError(f"{curr_link}/destination is not str")
        
        check_only = bool(resource.get("check_only", False))
        
        machine = Machine.parse(param_machine)
        machine = machine.reduce_local()

        expanded_destination = remote_expandvars(machine, destination)
        print(f"rsync {source} -> {expanded_destination}")
        rsync(source, expanded_destination, machine, check_only)

    return True

__all__ = ["sync"]

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python -m monolaunch.monoresource <param link>\n" + cleandoc(sync.__doc__ or ""), file=sys.stderr)
        exit(1)
    sync(sys.argv[1])
