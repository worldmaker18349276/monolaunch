"""
monolaunch.py - ROS1 Python API for generating flattened .launch files declaratively.

All namespace / remap / env context is resolved and applied directly
to each `<node>` or `<include>` tag - no nested `<group>` tags in the output.
Params are hoisted to the top of the generated file.

see README for detail.
"""

# TODO: typecheck user input

import argparse
import contextlib
import functools
import inspect
import keyword
import re
import traceback
import warnings
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Generator, KeysView, List, Literal, Optional, Tuple, Sequence, Type, TypeVar, Union, cast, overload
from pathlib import Path
from dataclasses import dataclass, field
from collections import ChainMap
import sys
import os
import shlex
from uuid import uuid4
import yaml
from monolaunch.yaml_utils import JSON, FieldAccessError, JPointer, JSONScalar, JsonPath, PathWithJPointer, TypedPathWithJPointer
from . import monoparam
from .monoparam import FieldAccessWarning, Resource, SchemaSource, SourceLoader, SourcedJSON_deep_iter, SourcedNode, SourcedYAMLDumper
from .monoresource import Machine

__all__ = [
    "run",
    "group", "node", "include", "master",
    "set_param", "load_param", "get_value",
    "PathWithJPointer", "TypedPathWithJPointer",
    "load_logger", "set_logger",
    "remap", "set_env",
    "machine",
    "env", "find", "anon", "ns", "dirname", "launch_prefix",
    "as_bool", "set_strict",
]


# JSON + Path
JSONWithPath = Union[None, JSON, Path, List["JSONWithPath"], Dict[str, "JSONWithPath"]]
# JSON but only Path/PathWithJPointer as scalar
JSONWithOnlyLink = Union[None, str, Path, PathWithJPointer, "TypedPathWithJPointer", List["JSONWithOnlyLink"], Dict[str, "JSONWithOnlyLink"]]

def parse_rosparam_path(path: str) -> Tuple[str, ...]:
    return tuple(e for e in path.strip("/").split("/") if e)

@overload
def JSONLike_deep_iter(folded_dict: JSONWithPath) -> Generator[Tuple[JsonPath, Union[JSONScalar, Path]], None, None]: ... # pyright: ignore[reportOverlappingOverload]
@overload
def JSONLike_deep_iter(folded_dict: JSONWithOnlyLink) -> Generator[Tuple[JsonPath, Union[str, Path, PathWithJPointer, "TypedPathWithJPointer"]], None, None]: ...
def JSONLike_deep_iter(folded_dict: JSON) -> Generator[Tuple[JsonPath, JSONScalar], None, None]: # pyright: ignore[reportInconsistentOverload]
    stack: List[Tuple[JsonPath, JSON]] = [((), folded_dict)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for subpath in list(value.keys()):
                stack.append(((*path, *parse_rosparam_path(subpath)), value[subpath]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append(((*path, key), value[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield path, value


# -- build context ------------------------------------------------------------

def _split_ns(path: str) -> Tuple[str, ...]:
    return tuple(e for e in path.split("/") if e)

def _join_ns(path: Tuple[str, ...]) -> str:
    return "/" + "/".join(path)

class DuplicatedNameError(Exception):
    def __init__(self, type: str, name: str):
        self.type = type
        self.name = name
    
    def __str__(self):
        return f"{self.type} name {self.name!r} is already used"

class NeverUseError(Exception):
    def __init__(self, statement: str):
        self.statement = statement
    
    def __str__(self):
        return f"{self.statement} object is created but not used"

class MultipleUseError(Exception):
    def __init__(self, statement: str):
        self.statement = statement
    
    def __str__(self):
        return f"{self.statement} object cannot be reused"

class UseInPrivateScopeError(Exception):
    def __init__(self, statement: str):
        self.statement = statement
    
    def __str__(self):
        return f"{self.statement} cannot be used inside node or include"

class FilePathNotAbsoluteError(Exception):
    def __init__(self, path: Path):
        self.path = path
    
    def __str__(self):
        return f"param file path must be absolute path, got: {self.path}, you may want to use dirname()"

class LoopRemapError(Exception):
    def __init__(self, name: str):
        self.name = name
    
    def __str__(self):
        return f"remap {self.name} causes loop"

class LocalMachineNotLocalError(Exception):
    def __init__(self, machine: str):
        self.machine = machine
    
    def __str__(self):
        return f"machine with name 'local' must be local machine, got: {self.machine}"

class NoTopLevelScopeError(Exception):
    def __str__(self):
        return "please put `with machine(..., name='local')` at the top scope"

LoggerConfig = Dict[str, Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]]

def resolve_logger_config(logger_config: JSON) -> LoggerConfig:
    if not isinstance(logger_config, dict):
        raise TypeError(f"expect dict, got {type(logger_config).__name__}")
    for level in logger_config.values():
        if level not in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
            raise TypeError(f"expect 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL', got {level}")
    return cast(LoggerConfig, logger_config)


@dataclass
class Scope:
    ns: Tuple[str, ...] = ()
    is_private: bool = False
    default_machine: Optional["MachineCtx"] = None
    remap: Dict[str, str] = field(default_factory=lambda: {})
    env: Dict[str, str] = field(default_factory=lambda: {})
    logger: List[Union[PathWithJPointer, LoggerConfig]] = field(default_factory=lambda: [])

@dataclass
class Ctx:
    scopes: List[Scope] = field(default_factory=lambda: [])
    param_loader: SourceLoader = field(default_factory=SourceLoader)
    param_node: Optional[SourcedNode] = None
    # node_name -> node, include_index -> include
    nodes: Dict[Union[str, int], Union["Node", "Include"]] = field(default_factory=lambda: {})
    machines: Dict[str, "MachineCtx"] = field(default_factory=lambda: {})
    params_filepath: Path = field(default_factory=lambda: Path(".yaml"))
    master: Optional["Master"] = None
    need_regen: bool = True

    @property
    def pns(self) -> Tuple[str, ...]:
        return tuple(x for scope in self.scopes for x in scope.ns if x)

    @property
    def ns(self) -> Tuple[str, ...]:
        scopes = self.scopes[:-1] if self.is_private else self.scopes
        return tuple(x for scope in scopes for x in scope.ns if x)

    @property
    def default_machine(self) -> "MachineCtx":
        if not self.scopes:
            raise NoTopLevelScopeError()
        return next(scope.default_machine for scope in self.scopes[::-1] if scope.default_machine)

    @default_machine.setter
    def default_machine(self, default_machine: "MachineCtx"):
        if not self.scopes:
            raise NoTopLevelScopeError()
        self.scopes[-1].default_machine = default_machine

    def add_node(self, ns: Tuple[str, ...], node: "Node"):
        full_name = _join_ns((*ns, node.name))
        if full_name in self.nodes:
            raise DuplicatedNameError("node", full_name)
        assert not any(node is node_ for node_ in self.nodes.values())
        self.nodes[full_name] = node

    def add_include(self, include: "Include"):
        assert id(include) not in self.nodes
        self.nodes[id(include)] = include

    def add_machine(self, machine: "MachineCtx"):
        if machine.name in self.machines and self.machines[machine.name] == machine:
            return
        if machine.name in self.machines and not self.find_machine(machine.machine):
            raise DuplicatedNameError("machine", machine.name)
        if machine.name == "local" and not machine.machine.is_local():
            raise LocalMachineNotLocalError(str(machine.machine))
        self.machines[machine.name] = machine
    
    def find_machine(self, machine: Machine) -> Optional["MachineCtx"]:
        return next((machines_ for machines_ in self.machines.values() if machines_.machine == machine), None)

    def push_group(self, ns: Tuple[str, ...] = (), is_private: bool = False, default_machine: Optional["MachineCtx"] = None):
        if not self.scopes and (default_machine is None or default_machine.name != "local"):
            raise NoTopLevelScopeError()
        if not self.scopes and default_machine and default_machine.name == "local":
            self.add_machine(default_machine)
        self.scopes.append(Scope(ns, is_private, default_machine))

    def pop_group(self):
        self.scopes.pop()

    def resolve_name(self, name: str) -> str:
        if name.startswith("~"):
            name = _join_ns(self.pns + _split_ns(name[1:]))
        elif not name.startswith("/"):
            name = _join_ns(self.ns + _split_ns(name))
        return name

    @property
    def is_private(self) -> bool:
        return bool(self.scopes) and self.scopes[-1].is_private

    # param
    def set_param(self, param: JSONWithPath, check: bool = True):
        self._set_param(False, param, self.default_machine, check)

    def load_param(self, param: JSONWithOnlyLink, check: bool = True):
        self._set_param(True, param, self.default_machine, check)

    def _set_param(self, is_load: bool, param: Union[JSONWithPath, JSONWithOnlyLink], machine: "MachineCtx", check: bool):
        if not isinstance(param, dict):
            raise TypeError("param should be a dictionary")

        param = cast(Union[JSONWithPath, JSONWithOnlyLink], {
            self.resolve_name(key): value
            for key, value in param.items()
        })

        if self.param_node is None:
            self.param_node = self.param_loader.new(self.params_filepath)
        assert self.param_node is not None
        if is_load:
            for path, value in JSONLike_deep_iter(cast(JSONWithOnlyLink, param)):
                if isinstance(value, (str, Path)):
                    inc_path = Path(value)
                elif isinstance(value, TypedPathWithJPointer):
                    inc_path = value.filepath
                elif isinstance(value, PathWithJPointer): # pyright: ignore[reportUnnecessaryIsInstance]
                    inc_path = value.filepath
                else:
                    raise TypeError(f"load_param dictionary should contain yaml file path to include, but got {value}")
                if not inc_path.is_absolute():
                    raise FilePathNotAbsoluteError(inc_path)

                if check:
                    # get_value for type checking
                    self.get_value(TypedPathWithJPointer.create(value), allow_attrs=False)
                self.param_loader.include(self.param_node, path, value, str(machine))
        else:
            for path, value in JSONLike_deep_iter(cast(JSONWithPath, param)):
                self.param_loader.update(self.param_node, path, value, str(machine))

    # TODO: resolve !resource -> Path
    def get_value(self, link: TypedPathWithJPointer, allow_attrs: bool = True) -> JSON:
        if not link.filepath.is_absolute():
            raise FilePathNotAbsoluteError(link.filepath)
        tmp_param_node = self.param_loader.load(link.filepath)
        with warnings.catch_warnings():
            warnings.simplefilter("error", FieldAccessWarning)
            tmp_param_node = self.param_loader.walk(tmp_param_node, link.fieldpath[:link.schema_root])
            assert tmp_param_node is not None
        if not SchemaSource.is_any(link.schema):
            tmp_param_node = self.param_loader.with_new_schema(tmp_param_node, None, link.schema)

        # special attr
        attr = "__self__"
        fieldpath = link.fieldpath[link.schema_root:]
        if allow_attrs and len(fieldpath.elements) > 0 and fieldpath.elements[-1] in ("__class__", "__len__", "__keys__", "__self__"):
            attr = fieldpath.elements[-1]
            fieldpath = fieldpath[:-1]

        with warnings.catch_warnings():
            warnings.simplefilter("error", FieldAccessWarning)
            tmp_param_node = self.param_loader.walk(tmp_param_node, fieldpath)
            assert tmp_param_node is not None
            tmp_param_node = self.param_loader.resolve_indirect_(tmp_param_node)[0]
        type_keys = tmp_param_node.access()
        if attr == "__class__":
            if type_keys[0] == "null":
                return type(None).__name__
            elif type_keys[0] == "seq":
                return list.__name__
            elif type_keys[0] == "map":
                return dict.__name__
            elif type_keys[0] == "scalar":
                return type(type_keys[1]).__name__ if not isinstance(type_keys[1], Resource) else str.__name__
            else:
                assert False

        elif attr == "__len__":
            if type_keys[0] != "seq":
                raise FieldAccessError(link.fieldpath, str(link.filepath))
            assert isinstance(type_keys[1], range)
            return len(type_keys[1])

        elif attr == "__keys__":
            if type_keys[0] != "map":
                raise FieldAccessError(link.fieldpath, str(link.filepath))
            assert isinstance(type_keys[1], list)
            return cast(JSON, type_keys[1])

        else:
            res = self.param_loader.resolve_all(tmp_param_node)
            return res

    # remap
    def _get_mapping(self) -> Tuple[KeysView[str], Callable[[str], Optional[str]]]:
        merged = ChainMap(*[scope.remap for scope in self.scopes[::-1]])
        def get(t: Optional[str]) -> Optional[str]:
            return merged.get(t) if t is not None else None
        def rget(t0: str) -> Optional[str]:
            t = t0
            t2 = t0
            while True:
                t_ = get(t)
                t2_ = get(get(t2))
                if t_ is not None and t2_ == t_:
                    return None
                if t_ is None: break
                t = t_
                t2 = t2_
            return t
        return merged.keys(), rget

    def get_remap(self) -> Dict[str, str]:
        keys, value_func = self._get_mapping()
        # chain mapping
        remap: Dict[str, str] = {}
        for k in keys:
            v = value_func(k)
            if v is None:
                raise LoopRemapError(k)
            remap[k] = v
        return remap

    def push_remap(self, mapping: Dict[str, str]):
        if not self.scopes:
            raise NoTopLevelScopeError()
        remap = self.scopes[-1].remap
        for k, v in mapping.items():
            k_ = self.resolve_name(k)
            v_ = self.resolve_name(v)
            if k_ == v_:
                # remove trivial map instead of error
                continue
            remap[k_] = v_

        # check loop of mapping
        _, value_func = self._get_mapping()
        for k in remap.keys():
            v = value_func(k)
            if v is None:
                raise LoopRemapError(k)

    # env
    def push_env(self, envvars: Dict[str, str]):
        if not self.scopes:
            raise NoTopLevelScopeError()
        self.scopes[-1].env.update(envvars)

    def get_env(self) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for scope in self.scopes:
            merged.update(scope.env)
        return merged

    # logger
    def load_logger(self, link: TypedPathWithJPointer, check: bool = True):
        if not self.scopes:
            raise NoTopLevelScopeError()
        if check:
            # get_value for type checking
            resolve_logger_config(self.get_value(link, allow_attrs=False))
        self.scopes[-1].logger.append(link)

    def set_logger(self, config: LoggerConfig):
        if not self.scopes:
            raise NoTopLevelScopeError()
        self.scopes[-1].logger.append(config)

    def assign_logger(self) -> bool:
        if not self.scopes:
            raise NoTopLevelScopeError()
        configs = [config for scope in self.scopes for config in scope.logger]
        if not configs: return False
        
        for config in configs:
            if isinstance(config, PathWithJPointer):
                self.load_param({"~$ros_logger_config": config}, check=False)
            else:
                self.set_param({"~$ros_logger_config": cast(JSONWithPath, config)})

        return True

def set_strict():
    """
    raise errors for failures of parameter loading/resolving/typechecking instead of warnings.
    """
    warnings.filterwarnings("error", category=monoparam.ResolveWarning)
    warnings.filterwarnings("error", category=monoparam.SchemaWarning)

# -- primitive value types ----------------------------------------------------

@dataclass
class Include:
    file: str
    args: Dict[str, Any]
    ns: Tuple[str, ...] = ()
    clear_params: bool = False
    machine: Optional["MachineCtx"] = None
    env: Dict[str, str] = field(default_factory=lambda: {})
    remap: Dict[str, str] = field(default_factory=lambda: {})

    _used: bool = False

    def __del__(self):
        if not self._used:
            raise NeverUseError("include")

    def __enter__(self):
        if self._used:
            raise MultipleUseError("include")
        self._used = True
        if ctx().is_private:
            raise UseInPrivateScopeError("inlcude")

        self.ns = ctx().ns
        ctx().add_include(self)
        self.machine = ctx().default_machine
        ctx().add_machine(self.machine)
        ctx().push_group((), False)
        return self

    def __exit__(self, *_):
        self.env = ctx().get_env()
        self.remap = ctx().get_remap()
        ctx().pop_group()

    def to_xml(self, machine_xml: ET.Element) -> ET.Element:
        el_ = ET.Element("group")
        el_.append(machine_xml)
        for f, t in self.remap.items():
            r = ET.SubElement(el_, "remap"); r.set("from", f); r.set("to", t)

        attrs: Dict[str, str] = {}
        attrs["file"] = self.file
        if self.clear_params:
            attrs["clear_params"] = "true"
        if self.ns:
            attrs["ns"] = _join_ns(self.ns)
        el = ET.Element("include", attrs)

        for k, v in self.env.items():
            e = ET.SubElement(el, "env"); e.set("name", k); e.set("value", str(v))
        for k, v in self.args.items():
            e = ET.SubElement(el, "arg"); e.set("name", k); e.set("value", str(v))
        el_.append(el)
        return el_

@dataclass
class Node:
    name: str
    pkg: str
    type: str
    output: Literal["log", "screen"] = "log"
    cwd: Literal["ROS_HOME", "node"] = "ROS_HOME"
    args: Tuple[Any, ...] = ()
    respawn: bool = False
    respawn_delay: float = 30.0
    clear_params: bool = False
    required: bool = False
    launch_prefix: Tuple[Any, ...] = ()
    ns: Tuple[str, ...] = ()
    env: Dict[str, str] = field(default_factory=lambda: {})
    remap: Dict[str, str] = field(default_factory=lambda: {})
    machine: Optional["MachineCtx"] = None
    has_logger: bool = False

    _used: bool = False

    def __del__(self):
        if not self._used:
            raise NeverUseError("node")

    def __enter__(self):
        if self._used:
            raise MultipleUseError("node")
        self._used = True
        if ctx().is_private:
            raise UseInPrivateScopeError("node")

        self.ns = ctx().ns
        ctx().add_node(ctx().ns, self)
        self.machine = ctx().default_machine
        ctx().add_machine(self.machine)
        ctx().push_group((self.name,), True)
        return self

    def __exit__(self, *_):
        self.env = ctx().get_env()
        self.remap = ctx().get_remap()
        self.has_logger = ctx().assign_logger()
        if self.has_logger:
            self.launch_prefix = ("rosrun", "monolaunch", "setup_logger.py") + self.launch_prefix
        ctx().pop_group()

    def to_xml(self) -> ET.Element:
        attrs: Dict[str, str] = {}

        if self.ns:
            attrs["ns"] = _join_ns(self.ns)

        attrs["name"] = self.name
        attrs["pkg"] = self.pkg
        attrs["type"] = self.type
        if self.output != "log": attrs["output"] = self.output
        if self.cwd != "ROS_HOME": attrs["cwd"] = self.cwd
        if self.args:
            attrs["args"] = shlex.join(str(a) for a in self.args)

        if self.machine:       attrs["machine"] = self.machine.name
        if self.respawn:       attrs["respawn"] = "true"; attrs["respawn_delay"] = str(self.respawn_delay)
        if self.clear_params:  attrs["clear_params"] = "true"
        if self.required:      attrs["required"] = "true"
        if self.launch_prefix: attrs["launch-prefix"] = shlex.join(str(a) for a in self.launch_prefix)

        el = ET.Element("node", attrs)
        for k, v in self.env.items():
            e = ET.SubElement(el, "env"); e.set("name", k); e.set("value", str(v))
        for f, t in self.remap.items():
            r = ET.SubElement(el, "remap"); r.set("from", f); r.set("to", t)
        return el

@dataclass
class Master:
    machine: Optional["MachineCtx"] = None
    mode: Literal["start", "wait", "auto"] = "auto"

    def __enter__(self):
        if ctx().master is not None:
            raise MultipleUseError("master")

        if ctx().is_private:
            raise UseInPrivateScopeError("master")

        ctx().master = self
        self.machine = ctx().default_machine
        ctx().add_machine(self.machine)
        ctx().push_group((), False)
        return self

    def __exit__(self, *_):
        ctx().pop_group()

    def to_xml(self) -> ET.Element:
        attrs: Dict[str, str] = {}
        attrs["mode"] = self.mode
        if self.machine:
            attrs["machine"] = self.machine.name
        el = ET.Element("master", attrs)
        return el

class _Regenerate(BaseException):
    def __init__(self, machine: Machine):
        self.machine = machine

@dataclass
class MachineCtx:
    name: str
    machine: Machine

    def __enter__(self):
        need_regen = ctx().need_regen and not ctx().scopes
        ctx().push_group((), False, self)
        if need_regen:
            raise _Regenerate(ctx().default_machine.machine)
        return self

    def __exit__(self, *_):
        ctx().pop_group()

    @staticmethod
    def parse(url: str) -> "MachineCtx":
        """
        parse machine scheme url
        format: machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2
        
        or: machine://user:pswd@addr/path/to/devel/setup.bash?=setup
        env_loader will be rewritten as: `/usr/bin/bash -c 'source /path/to/devel/setup.bash && ROS_IP={address} exec "$@"' --`
        """
        return MachineCtx(name="", machine=Machine.parse(url))

    def __str__(self) -> str:
        return str(self.machine)

    def to_xml(self, default: bool = False) -> ET.Element:
        attrs: Dict[str, str] = {}
        attrs["name"] = self.name
        attrs["address"] = self.machine.address
        if self.machine.env_loader:  attrs["env-loader"] = shlex.join(self.machine.env_loader)
        if self.machine.user:        attrs["user"] = self.machine.user
        if self.machine.password:    attrs["password"] = self.machine.password
        attrs["default"] = "true" if default else "false"
        el = ET.Element("machine", attrs)
        return el

@dataclass
class Group:
    ns: Tuple[str, ...] = ()

    def __enter__(self):
        ctx().push_group(self.ns)
        return self

    def __exit__(self, *_):
        ctx().pop_group()

# -- public helpers ------------------------------------------------------------

def env(name: str, fallback: Optional[str] = None) -> str:
    value = os.environ.get(name, fallback)
    if value is None:
        raise ValueError(f"envvar {name} not found")
    return value

def find(pkg: str) -> str:
    import rospkg
    return rospkg.RosPack().get_path(pkg) # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]

def sanitize_identifier(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not name.isidentifier() or keyword.iskeyword(name):
        name = "_" + name
    return name

def anon(name: str) -> str:
    return name + "_" + str(uuid4()).replace("-", "_")


JsonValueT = TypeVar("JsonValueT", Type[None], bool, int, float, str, List[JSON], Dict[str, JSON])

@overload
def get_value(link: Union[str, Path, PathWithJPointer, TypedPathWithJPointer]) -> JSON: ...
@overload
def get_value(link: Union[str, Path, PathWithJPointer, TypedPathWithJPointer], expected_type: Type[JsonValueT]) -> JsonValueT: ...

def get_value(link: Union[str, Path, PathWithJPointer, TypedPathWithJPointer], expected_type: Optional[Type[JSON]] = None) -> JSON: # pyright: ignore[reportInconsistentOverload]
    if isinstance(link, TypedPathWithJPointer):
        pass
    elif isinstance(link, PathWithJPointer):
        link = link.with_schema()
    elif isinstance(link, Path):
        link = TypedPathWithJPointer(link)
    elif isinstance(link, str): # pyright: ignore[reportUnnecessaryIsInstance]
        link = PathWithJPointer.parse(link).with_schema()
    else:
        raise TypeError(type(link))

    res = ctx().get_value(link)
    if expected_type is not None and type(res) != expected_type:
        raise TypeError(
            f"field {link} ({type(res).__name__})"
            f" doesn't match expected type {expected_type.__name__}"
        )
    return res

def set_param(json: JSONWithPath):      ctx().set_param(json)
def load_param(json: JSONWithOnlyLink): ctx().load_param(json)
def remap(mapping: Dict[str, str]):     ctx().push_remap(mapping)
def set_env(mapping: Dict[str, str]):   ctx().push_env(mapping)

_ctx: Optional[Ctx] = None

def ctx() -> Ctx:
    global _ctx
    assert _ctx is not None
    return _ctx

@contextlib.contextmanager
def _with_ctx():
    global _ctx
    try:
        _ctx = Ctx()
        yield
    finally:
        _ctx = None

def ns(n: Literal["", "~"] = "") -> str:
    if n == "~":
        return _join_ns(ctx().pns)
    else:
        return _join_ns(ctx().ns)

def dirname() -> Path:
    currentframe = inspect.currentframe()
    assert currentframe is not None
    f_back = currentframe.f_back
    assert f_back is not None
    file = f_back.f_globals["__file__"]
    return Path(file).parent.resolve()

class UncopyableFunctionWarning(Warning):
    def __init__(self, func_name: str):
        self.func_name = func_name
    
    def __str__(self):
        return f"function {self.func_name} is not a source-copyable function"

def launch_prefix(prefix_func: Callable[..., None]):
    """
    a launch-prefix function decorator.
    it allows you to write prefix logic in python.

    the source code will be transferred and executed on the machine of the node,
    so launch-prefix function cannot be a closure,
    arguments cannot have defaults (it may refer to outer variable),
    and can only accept string arguments (arguments are also need to be transferred).
    
    example:
    ```
    @launch_prefix
    def rviz_init(ns, delay):
        import rospy
        import os
        import sys
        import time
        rviz_config = rospy.get_param(f"{ns}/rviz_config") # you can read parameters
        rosconsole_config = rospy.get_param(f"{ns}/rosconsole_config")
        assert sys.argv[0] == "/opt/ros/noetic/lib/rviz"
        sys.argv[1:1] = ["-d", str(rviz_config)] # you can modify command arguments
        os.environ["ROSCONSOLE_CONFIG_FILE"] = str(rosconsole_config) # you can change environment variables
        time.sleep(float(delay)) # you can wait

    with node(name="rviz", pkg="rviz", type="rviz", launch_prefix=rviz_init(ns(), str(1.5))):
        pass
    ```
    if a module has been imported globally, it will be captured and become a closure.
    to fix it, use `__import__` instead in this case.
    """
    closurevars = inspect.getclosurevars(prefix_func)
    if closurevars.nonlocals or closurevars.globals or prefix_func.__defaults__:
        warnings.warn(UncopyableFunctionWarning(prefix_func.__name__))
    
    name = prefix_func.__name__
    code = inspect.cleandoc("\n"+inspect.getsource(prefix_func))
    
    @functools.wraps(prefix_func)
    def wrapped_prefix_func(*args: str) -> List[str]:
        args = tuple(str(arg) for arg in args)
        return ["rosrun", "monolaunch", "launch_prefix.py", name, code, str(len(args)), *args]
    
    return wrapped_prefix_func

def group(ns: str = "") -> Group:
    if ns.startswith("/"):
        raise ValueError(f"group ns should be relative path, got: {ns}")
    return Group(ns=_split_ns(ns))

def machine(url: str = "", *, name: str = "", address: str = "", env_loader: Sequence[str] = (), user: str = "", password: str = "") -> MachineCtx:
    if url:
        machine = MachineCtx.parse(url)
        if name:
            machine.name = name
    else:
        machine = MachineCtx(name=name, machine=Machine(address=address, env_loader=tuple(env_loader), user=user, password=password))
    if not machine.name:
        machine_ = ctx().find_machine(machine.machine)
        if machine_ is None:
            machine.name = anon(sanitize_identifier(machine.machine.user + "_" + machine.machine.address))
        else:
            machine.name = machine_.name
    return machine

def master(mode: Literal["start", "wait", "auto"] = "auto") -> Master:
    return Master(mode=mode)

def node(*, name: str = "", pkg: str = "", type: Union[str, Path],
         output: Literal["log", "screen"] = "log", cwd: Literal["ROS_HOME", "node"] = "ROS_HOME",
         args: Sequence[Any] = (), respawn: bool = False, respawn_delay: float = 30.0,
         clear_params: bool = False, required: bool = False, launch_prefix: Sequence[Any] = ()) -> Node:
    if output not in ("log", "screen"):
        raise TypeError(f"output should be one of log, screen")
    if cwd not in ("ROS_HOME", "node"):
        raise TypeError(f"cwd should be one of ROS_HOME, node")
    if not isinstance(args, (tuple, list)):
        raise TypeError(f"args should be list")

    if not name:
        name = anon(sanitize_identifier(str(type)))

    if not pkg:
        # treat type as direct path to script
        if not Path(type).is_absolute():
            raise FilePathNotAbsoluteError(Path(type))
        args = (type, *args)
        pkg = "monolaunch"
        type = "exec.sh"

    return Node(name=name, pkg=pkg, type=str(type), output=output, cwd=cwd,
                args=tuple(args), respawn=respawn, respawn_delay=respawn_delay,
                clear_params=clear_params, required=required, launch_prefix=tuple(launch_prefix))

def include(file: Union[str, Path], *, clear_params: bool = False, **args: Any) -> Include:
    if not Path(file).is_absolute():
        raise FilePathNotAbsoluteError(Path(file))

    return Include(file=str(file), clear_params=clear_params, args=args)

def as_bool(s: Union[str, bool]) -> bool:
    if isinstance(s, bool): return s
    if s in ("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"):
        return True
    elif s in ("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"):
        return False
    else:
        raise ValueError(f"{s} is not valid bool literal")


def load_logger(config_link: Union[str, Path, PathWithJPointer, TypedPathWithJPointer]) -> None:
    """
    load ros logger config in a scope (apply to nodes in the scope).
    
    about config format, see `set_logger`.
    """
    if isinstance(config_link, str):
        config_link = PathWithJPointer.parse(config_link).with_schema()
    elif isinstance(config_link, Path):
        config_link = TypedPathWithJPointer(config_link)
    elif isinstance(config_link, TypedPathWithJPointer):
        pass
    elif isinstance(config_link, PathWithJPointer): # pyright: ignore[reportUnnecessaryIsInstance]
        config_link = config_link.with_schema()
    else:
        raise TypeError(type(config_link))

    ctx().load_logger(config_link)

def set_logger(config: Dict[str, Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]]) -> None:
    """
    setup ros logger config in a scope (apply to nodes in the scope).

    `config` is a map from logger name to logging level (DEBUG, INFO, WARN, ERROR, FATAL).
    """
    config = resolve_logger_config(cast(JSON, config))
    ctx().set_logger(config)

class ForeignSyncResourceWarning(Warning):
    def __init__(self, resource_name: str, param_name: JPointer, runtime_machine_name: str, host_node_name: str, host_machine_name: str):
        self.resource_name = resource_name
        self.param_name = param_name
        self.runtime_machine_name = runtime_machine_name
        self.host_node_name = host_node_name
        self.host_machine_name = host_machine_name

    def __str__(self):
        return (
            f"resource {self.resource_name} "
            f"sync to {self.param_name} "
            f"under machine {self.runtime_machine_name} "
            f"but it is a param under {self.host_node_name}"
            + (f" ({self.host_machine_name})" if self.host_machine_name else "")
        )

def check_foreign_sync_resources(ctx: Ctx):
    if ctx.param_node is None: return
    for source in ctx.param_node.sources:
        for _raw_path, path, value in SourcedJSON_deep_iter(source.data):
            param_name = JPointer.from_list(path)
            # strip until index element
            if isinstance(value, (monoparam.Include, monoparam.Resource)):
                runtime_machine = dict(value.context).get("runtime_machine")
                runtime_machine_key = MachineCtx.parse(runtime_machine).machine if runtime_machine is not None else None
                host_node = None
                for node in ctx.nodes.values():
                    if isinstance(node, Node):
                        if JPointer((*node.ns, node.name)).is_prefix(param_name):
                            host_node = node
                            break
                    else:
                        if JPointer(node.ns).is_prefix(param_name):
                            host_node = node
                            break
                host_machine_key = host_node.machine.machine if host_node is not None and host_node.machine is not None else None
                if host_machine_key != runtime_machine_key:
                    resource_name = f"!include {value.link}" if isinstance(value, monoparam.Include) else value.uri
                    runtime_machine_name = runtime_machine or ""
                    if host_node is None:
                        host_node_name = "public namespace"
                        host_machine_name = ""
                    elif isinstance(host_node, Node):
                        host_machine_name = str(host_node.machine or "local machine")
                        host_node_name = f"node {_join_ns((*host_node.ns, host_node.name))}"
                    else:
                        host_machine_name = str(host_node.machine or "local machine")
                        host_node_name = f"included launch file {_join_ns(host_node.ns)}"

                    warnings.warn(ForeignSyncResourceWarning(resource_name, param_name, runtime_machine_name, host_node_name, host_machine_name))

FILENAME_EXPR = """
(lambda p:
    str(__import__('pathlib').Path(p).resolve())
    if p != 'string'
    else ''
)(
    dict(
        zip(dirname.__code__.co_freevars, dirname.__closure__)
    )['context'].cell_contents['filename']
)
"""

AUTO_RELAUNCH_WITH_ROSCORE_EXPR = """
(
    dry_run == 0
    and not __import__('os').getenv('NO_RELAUNCH_WITH_ROSCORE', '')
    and not __import__('os').putenv('NO_RELAUNCH_WITH_ROSCORE', '1')
    and (
        filename
        or print('launch through piping, disable relaunch with roscore')
    )
    and (lambda cmd:
        print(__import__('shlex').join(cmd))
        or __import__('os').execv(cmd[0], cmd)
    )([
        __import__('rospkg').RosPack().get_path('monolaunch') + '/scripts/with_roscore.py',
        '--filename', filename,
        *__import__('sys').argv
    ])
)
"""

def generate(launch_func: Callable[[], None], need_regen: bool = True) -> Path:
    with _with_ctx():
        ctx().need_regen = need_regen
        cwd = Path.cwd()
        name = launch_func.__name__
        ctx().params_filepath = cwd / f"{name}.yaml"

        try:
            launch_func()
        except _Regenerate:
            if not need_regen:
                raise RuntimeError("WTF!?")
            else:
                raise

        param_node = ctx().param_node
        check_foreign_sync_resources(ctx())
        if param_node is None:
            param_node = ctx().param_loader.new(ctx().params_filepath)
            assert param_node is not None
        param = param_node.sources[0].data

        launch_el = ET.Element("launch")

        # dry_run arg
        launch_el.append(ET.Element("arg", dict(name="dry_run", default="0")))

        # dry_run == 1
        dry_run_1 = ET.Element("group", {"if": "$(eval dry_run == 1)"})
        dry_run_1.append(ET.Element("node", dict(error="stop launch because dry_run == 1")))
        launch_el.append(dry_run_1)

        # extract filename
        launch_el.append(ET.Element("arg", dict(name="filename_expr", default=FILENAME_EXPR)))
        launch_el.append(ET.Element("arg", dict(name="filename", default="$(eval eval(filename_expr))")))

        # add auto relaunch with roscore
        launch_el.append(ET.Element("arg", dict(name="auto_relaunch_with_roscore_expr", default=AUTO_RELAUNCH_WITH_ROSCORE_EXPR)))
        launch_el.append(ET.Element("arg", dict(name="auto_relaunch_with_roscore_res", default="$(eval eval(auto_relaunch_with_roscore_expr))")))

        # add param resolver
        if param_node:
            launch_el.append(ET.Element("arg", dict(
                name="source_param_pointer",
                default="$(arg filename)#xpointer(/launch/rosparam[@param='/'])",
            )))
            launch_el.append(ET.Element("arg", dict(
                name="resolved_param_expr",
                default="__import__('monolaunch.monoparam').monoparam.to_resolved(source_param_pointer)",
            )))
            launch_el.append(ET.Element("arg", dict(name="resolved_param", default="$(eval eval(resolved_param_expr))")))

        # dry_run == 2
        dry_run_2 = ET.Element("group", {"if": "$(eval dry_run == 2)"})
        dry_run_2.append(ET.Element("node", dict(error="run until resolving param because dry_run == 2")))
        launch_el.append(dry_run_2)

        # add resource loader
        if param_node:
            sync_resources_expr = f"__import__('monolaunch.monoresource').monoresource.sync(resolved_param + '#/$sync_resources')"
            launch_el.append(ET.Element("arg", dict(name="sync_resources_expr", default=sync_resources_expr)))
            launch_el.append(ET.Element("arg", dict(name="sync_resources", default="$(eval eval(sync_resources_expr))")))

        # dry_run == 3
        dry_run_3 = ET.Element("group", {"if": "$(eval dry_run == 3)"})
        dry_run_3.append(ET.Element("node", dict(error="run until sync resources because dry_run == 3")))
        launch_el.append(dry_run_3)
        
        # add <machine>
        for machine in ctx().machines.values():
            launch_el.append(machine.to_xml())

        # add <master>, <node> and <include>
        if master := ctx().master:
            launch_el.append(master.to_xml())
        for node in list(ctx().nodes.values()):
            if isinstance(node, Node):
                launch_el.append(node.to_xml())

            else: # Include
                assert node.machine is not None
                launch_el.append(node.to_xml(machine_xml=node.machine.to_xml(default=True)))

        # load param in the end, so that it override other rosparam set/load in <include>
        if param_node:
            param_el = ET.Element("rosparam", dict(command="load", file="$(arg resolved_param)", param="/"))
            param_el.text = yaml.dump(param, Dumper=SourcedYAMLDumper, sort_keys=False)
            launch_el.append(param_el)

        _indent(launch_el)
        launch_str = ET.tostring(launch_el, encoding="unicode", xml_declaration=True)
        launch_str = _indent_attrib(launch_str)

        # save launch file
        launch_filepath = cwd / f"{name}.launch"
        launch_filepath.write_text(launch_str)

        return launch_filepath

# TODO: rename to launch
def run(launch_func: Callable[[], None]) -> Any:
    if launch_func.__globals__["__name__"] != "__main__":
        return launch_func
    # only run on main
    cmd = _run(launch_func)
    cmd()

def _run(launch_func: Callable[[], None]) -> Callable[[], None]:
    argparser = argparse.ArgumentParser(
        add_help=False,
        usage="%(prog)s [--gen-verbose] [--sync-latest-logs] [--dry-run STAGE] [ARGS ...]",
    )
    argparser.add_argument(
        "--dry-run",
        type=int, choices=range(4), default=0,
        help="0: just run, 1: until generating launch file, 2: until resolving yaml file, 3: until syncing resources"
    )
    argparser.add_argument(
        "--gen-verbose",
        action="store_true",
        help="increase output verbosity during generation phase"
    )
    argparser.add_argument(
        "--sync-latest-logs",
        action="store_true",
        help="synchronize latest logs from remote machines instead of launching them"
    )
    if "-h" in sys.argv or "--help" in sys.argv:
        argparser.print_help()
    args, unknown = argparser.parse_known_args()
    dry_run = int(args.dry_run)
    verbose = bool(args.gen_verbose)
    sync_latest_logs = bool(args.sync_latest_logs)
    need_regen = not bool(os.environ.get("NO_REGEN_WITH_LOCAL_ENV_LOADER", ""))
    os.environ["NO_REGEN_WITH_LOCAL_ENV_LOADER"] = "1"
    cmd = [sys.executable, *sys.argv]
    sys.argv[1:] = unknown

    with warnings.catch_warnings():
        def showwarning(message: Warning, category: Type[Warning], filename: Any, lineno: Any, file:Any=None, line:Any=None):
            if not verbose:
                print(f"[{category.__name__}] " + str(message).split("\n")[0], file=sys.stderr)
            else:
                print(f"[{category.__name__}] {str(message)}", file=sys.stderr)
                traceback.print_stack()
        warnings.showwarning = showwarning
    
        try:
            launch_filepath = generate(launch_func=launch_func, need_regen=need_regen)
        except Exception:
            traceback.print_exc()
            return lambda: exit(1)
        except _Regenerate as regen:
            cmd = regen.machine.command(cmd)
            print("regenerate launch file with local env-loader:\n" + shlex.join(cmd))
            return lambda: os.execvp(cmd[0], cmd)

        cmd = ("roslaunch", str(launch_filepath), *sys.argv[1:])
        if dry_run == 1:
            print("will not execute because --dry-run=1:\n" + shlex.join(cmd))
            return lambda: exit(0)
        if dry_run > 0:
            cmd = (*cmd, f"dry_run:={dry_run}")
        if sync_latest_logs:
            cmd = ("rosrun", "monolaunch", "sync_latest_logs.py", str(launch_filepath))
            print("synchronize latest logs:\n" + shlex.join(cmd))
            return lambda: os.execvp(cmd[0], cmd)
        print("start launch:\n" + shlex.join(cmd))
        return lambda: os.execvp(cmd[0], cmd)

TABSIZE = 4

def _indent(el: ET.Element, level: int = 0, is_last: bool = False):
    """
    indent tags and plain text
    """
    if len(el) or el.text:
        text = (el.text or "").strip("\n")
        text = "\n" if not text else f"\n{text}\n"
        el.text = text.replace("\n", "\n" + " " * TABSIZE * (level + 1))
        if len(el) == 0 and level > 0:
            el.text = el.text[:-TABSIZE]

    tail = (el.tail or "").strip("\n")
    tail = "\n" if not tail else f"\n{tail}\n"
    el.tail = tail.replace("\n", "\n" + " " * TABSIZE * level)
    if is_last and level > 0:
        el.tail = el.tail[:-TABSIZE]

    for i, child in enumerate(el):
        _indent(child, level + 1, i+1 == len(el))

def _indent_attrib(xml_str: str, level: int = 0) -> str:
    """
    recover newline of attributes with proper indentation.
    """
    res: List[str] = []
    for line in xml_str.split("\n"):
        line_ = line.lstrip()
        indent = line[:len(line) - len(line_)]
        line = line.replace("&#10;", "\n" + indent)
        res.append(line)
    return "\n".join(res)
