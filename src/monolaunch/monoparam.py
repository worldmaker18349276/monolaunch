"""
monoparam.py - ROS1 Python API for resolving YAML files for rosparam.

it allows us to manage parameters in a cascading YAML file tree, including resources.

it supports YAML files with !include, !merge, !resource tags.
!resource is a set of URI schema which can be resolved to a file path:
- file://{path_to_resource}                ->  $(pwd)/{path_to_resource}
- package://{pkg_name}/{path_to_resource}  ->  $(rospack find pkg_name)/{path_to_resource}
- ros_home://{path_to_resource}            ->  $ROS_HOME/{path_to_resource}

same as !include, file resource path is relative to the current location (directory of the file contains this term).
it will warn on invalid include, bad merge, etc, and supports certain form of $schema.

it is designed for monolaunch, which utilizes this module to provide some tools to reorganize parameters.
for users, you can write a single YAML file with !include, !merge, !resource tags,
and use load_param/set_param to re-distribute part of them to each node as private parameters.
then monolaunch will construct single reorganized YAML file for this launch.

all local resource items will be aggregated,
so that monoresource can sync local resources to designated machines.
where !resource must be loaded under a `with machine(...)` scope,
otherwise it cannot know where to put those resources.
(this is achieved by query suffix of !include and !resource,
like "?context=value", attached by load_param/set_param.)
they will be put under ros home of each machine,
and this item will be rewritten as ros_home scheme url,
allowing remote machine to access synchronized resources.

before launch, reorganized YAML file will be resolved and loaded via rosparam,
and a synchronizer node will be launched,
which will synchronize resolved YAML file and source YAML files in real time,
changing one YAML file on each side will cause the other YAML file to be updated.
"""

import contextlib
from enum import Enum
from inspect import cleandoc
import json
from uuid import uuid4
from typing import Any, Dict, Generator, List, Literal, Tuple, Set, Union, Optional, Type, TypeVar, Callable, IO, cast, overload
import sys
import math
import yaml
import xml.etree.ElementTree as ET
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, field
import warnings
from monolaunch.yaml_utils import *
from monolaunch.yaml_utils import urlquote


# just an annotation
_F = TypeVar("_F", bound=Callable[..., Any])
def raises(*exceptions: Type[BaseException]):
    def decorator(func: _F) -> _F:
        func.__raises__ = exceptions  # type: ignore[attr-defined]
        return func
    return decorator

# just an annotation
def prerequisite(pred: Callable[..., bool]):
    def decorator(func: _F) -> _F:
        func.__prerequisite__ = pred  # type: ignore[attr-defined]
        return func
    return decorator

# JSON + Resource/Include/Merge, specially for Source
SourcedJSON = Union[
    None,
    bool, int, float, str, "Resource",
    "Include", "Merge",
    List["SourcedJSON"], Dict[str, "SourcedJSON"],
]

# JSON, specially for Schema
SchemaJSON = Union[
    None,
    bool, int, float, str,
    List["SchemaJSON"], Dict[str, "SchemaJSON"],
]

def is_SourcedJSON(data: Any) -> bool:
    if type(data) in (type(None), bool, int, float, str, Resource):
        return True
    elif type(data) == list:
        return all(is_SourcedJSON(e) for e in cast(List[Any], data))
    elif type(data) == dict:
        return all(type(k) == str and is_SourcedJSON(v) for k, v in cast(Dict[Any, Any], data).items())
    elif type(data) == Merge:
        return all(is_SourcedJSON(e) for e in data.items)
    else:
        return False

def assert_SourcedJSON(data: Any) -> SourcedJSON:
    if not is_SourcedJSON(data):
        raise TypeError(f"not sourced json: {data}")
    return data


def SourcedJSON_deep_copy(obj: SourcedJSON) -> SourcedJSON:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: SourcedJSON_deep_copy(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return list(SourcedJSON_deep_copy(e) for e in obj)
    elif isinstance(obj, Merge):
        return Merge([SourcedJSON_deep_copy(e) for e in obj.items])
    else:
        return obj

def SourcedJSON_deep_eq(a: SourcedJSON, b: SourcedJSON) -> bool:
    stack: List[Tuple[SourcedJSON, SourcedJSON]] = [(a, b)]
    while stack:
        a, b = stack.pop()
        if type(a) != type(b):
            return False

        if isinstance(a, dict):
            assert isinstance(b, dict)
            if set(a.keys()) != set(b.keys()):
                return False
            for k in a:
                stack.append((a[k], b[k]))
            continue
        
        if isinstance(a, list):
            assert isinstance(b, list)
            if len(a) != len(b):
                return False
            for i in range(len(a)):
                stack.append((a[i], b[i]))
            continue
        
        if isinstance(a, Merge):
            assert isinstance(b, Merge)
            stack.append((a.items, b.items))
            continue

        # special case: nan != nan
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            continue

        if a != b:
            return False

    return True

def SourcedJSON_deep_diff(old: SourcedJSON, new: SourcedJSON) -> Dict[JPointer, Optional[SourcedJSON]]:
    """
    diff in the scalar level, will consider tags (!resource, !include, !merge).
    keys of resulting dictionary are paths in raw sourced json, not resolved one.
    this is for updating yaml file.
    """
    updated: Dict[JPointer, Optional[SourcedJSON]] = {}

    stack: List[Tuple[JPointer, SourcedJSON, SourcedJSON]] = [(JPointer(), old, new)]
    while stack:
        path, a, b = stack.pop()

        if type(a) != type(b):
            updated[path] = b
            continue

        if isinstance(a, dict):
            assert isinstance(b, dict)
            for k in a.keys():
                if k not in b:
                    updated[path.append(k)] = None
            for k in b.keys():
                if k not in a:
                    updated[path.append(k)] = b[k]
                else:
                    stack.append((path.append(k), a[k], b[k]))
            continue

        if isinstance(a, list):
            assert isinstance(b, list)
            for i in range(len(b)):
                if i < len(a):
                    stack.append((path.append(i), a[i], b[i]))
                else:
                    updated[path.append(i)] = b[i]
            for i in range(len(b), len(a)):
                updated[path.append(i)] = None
            continue

        if isinstance(a, Merge):
            assert isinstance(b, Merge)
            stack.append((path, a.items, b.items))
            continue

        if not SourcedJSON_deep_eq(a, b):
            updated[path] = b
            continue

    return updated

def SourcedJSON_deep_iter(obj: SourcedJSON) -> Generator[Tuple[JPointer, JPointer, Union[bool, int, float, str, "Resource", "Include"]], None, None]:
    """
    two paths are raw field path and resolved path
    """
    stack = [(JPointer(), JPointer(), obj)]
    while stack:
        raw_path, path, value = stack.pop()
        if isinstance(value, dict):
            for key in list(value.keys()):
                stack.append((raw_path.append(key), path.append(key), value[key]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append((raw_path.append(key), path.append(key), value[key]))
        elif isinstance(value, Merge):
            for key in range(len(value.items)):
                stack.append((raw_path.append(key), path, value.items[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield raw_path, path, value


class RosPackageNotFoundError(Exception):
    def __init__(self, package: str):
        self.package = package
    def __str__(self) -> str:
        return f"ros package not found: {self.package}"

class InvalidUriError(Exception):
    def __init__(self, uri: str):
        self.uri = uri
    def __str__(self) -> str:
        return f"unknown URI: {self.uri}"

def ros_home() -> str:
    import rospkg
    return rospkg.get_ros_home() # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]

def find_pkg(package_name: str) -> str:
    import rospkg
    try:
        package_path = rospkg.RosPack().get_path(package_name) # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    except Exception as e:
        raise RosPackageNotFoundError(package_name) from e
    assert isinstance(package_path, str)
    return package_path

@raises(RosPackageNotFoundError, InvalidUriError)
def retrieve_resource(uri: str) -> Path:
    """
    retrieve actual path of resource URI:
    - file://{path_to_resource}                ->  $(pwd)/{path_to_resource}
    - package://{pkg_name}/{path_to_resource}  ->  $(rospack find pkg_name)/{path_to_resource}
    - ros_home://{path_to_resource}            ->  $ROS_HOME/{path_to_resource}
    """
    if uri.startswith("file://"):
        path = uri[len("file://"):]
        return (Path.cwd() / Path(path)).resolve()

    elif uri.startswith("package://"):
        path = uri[len("package://"):]
        package_name, path = [*path.split("/", 1), ""][:2]
        return Path(find_pkg(package_name) + "/" + path).resolve()

    elif uri.startswith("ros_home://"):
        path = uri[len("ros_home://"):]
        return Path(ros_home() + "/" + path).resolve()

    else:
        raise InvalidUriError(uri)

@dataclass(frozen=True)
class Resource:
    """
    resource URI with context.
    
    supported resource URI:
    - file://{path_to_resource}                ->  $(pwd)/{path_to_resource}
    - package://{pkg_name}/{path_to_resource}  ->  $(rospack find pkg_name)/{path_to_resource}
    - ros_home://{path_to_resource}            ->  $ROS_HOME/{path_to_resource}

    file URI is the file path relative to current location (directory of the file contains this term);
    package URI refers to the workspace overlay where this item being read;
    ros_home URI refers to the ros home of current runtime when this item being used.

    resource URI should be transformed properly after switching carrier, otherwise the meanings may change.
    file URI should not be shared across machine since it is local resource;
    package URI can be shared across machine as long as they have the same overlay;
    ros_home URI is a runtime resource and should be prepared before each run on given machine.

    context can be attached by query suffix, like "?context=value".
    since it is parsed from the right side, if uri contains "?", just suffix with "?".
    note that this is different from standard url.

    context is designed for indicating that this resource is for which machine,
    so that resolver knows how to deal with local resource for remote machine.
    context is for resolver, it should be eliminated after resolving.
    """
    uri: str
    context: Tuple[Tuple[str, str], ...] = field(default_factory=lambda: ())

    @staticmethod
    def parse(uri: str) -> "Resource":
        uri, context = (*uri.rsplit("?", 1), "")[:2]
        resource = Resource(uri, tuple(urllib.parse.parse_qsl(context)))
        if not (uri.startswith("file://") or uri.startswith("package://") or uri.startswith("ros_home://")):
            warnings.warn(BadResourceSchemeURIWarning(resource))
        return resource

    def __str__(self) -> str:
        uri = self.uri
        # minimal query string encoding
        context = "&".join(
            urlquote(k, "?&=") + "=" + urlquote(v, "?&")
            for k, v in self.context
        )
        if context or "?" in uri:
            uri = f"{uri}?{context}"
        return uri

    @staticmethod
    def create(path: Union[str, Path]) -> "Resource":
        return Resource(f"file://{Path(path)}", ())

@dataclass(frozen=True)
class Include:
    """
    include a file as a node using path with json pointer (ex. /path/to/file.yaml#/sub/field).
    context can be attached by query suffix, like "?context=value".

    the included nodes will inherit this context, and prepend to underlying resources' context.
    since it is parsed from the right side, if json pointer contains "?", just suffix with "?".

    context is designed for indicating that underlying resources are for which machine,
    so that resolver know how to deal with local resources for remote machine.  
    """
    link: PathWithJPointer = field(default_factory=PathWithJPointer)
    context: Tuple[Tuple[str, str], ...] = field(default_factory=lambda: ())

    @staticmethod
    def parse(link: str) -> "Include":
        link, context = (*link.rsplit("?", 1), "")[:2]
        return Include(PathWithJPointer.parse(link), tuple(urllib.parse.parse_qsl(context)))

    def __str__(self) -> str:
        link = str(self.link)
        # minimal query string encoding
        context = "&".join(
            urlquote(k, "?&=") + "=" + urlquote(v, "?&")
            for k, v in self.context
        )
        if context or "?" in link:
            link = link + "?" + context
        return link

    @staticmethod
    def create(link: Union[str, Path, PathWithJPointer]) -> "Include":
        if isinstance(link, str):
            link = PathWithJPointer.parse(link)
        elif isinstance(link, Path):
            link = PathWithJPointer(link)
        return Include(link, ())

@dataclass(frozen=True)
class Merge:
    """
    accept sequence, merge all children.  

    - null <> any = any <> null = any   --  null behaves like empty slot
    - scalar <> scalar = later one
    - seq <> seq = zip longest with <>
    - map <> map = union zip with <>
    - non-null type <> another non-null type = later one  --  warning: incompatible types to merge
    """
    items: List[SourcedJSON] = field(default_factory=lambda: [])
    
    # def __post_init__(self):
    #     if not self.items:
    #         raise ValueError("merge should have one item at least")


class SourcedYAMLLoader(SimpleYAMLLoader):
    pass

def _resource_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Resource:
    if isinstance(node, yaml.nodes.ScalarNode):
        return Resource.parse(loader.construct_scalar(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!resource expects a scalar, got {type(node).__name__}",
            node.start_mark,
        )

def _include_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Include:
    if isinstance(node, yaml.nodes.ScalarNode):
        return Include.parse(loader.construct_scalar(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a scalar, got {type(node).__name__}",
            node.start_mark,
        )

def _merge_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Merge:
    if isinstance(node, yaml.nodes.SequenceNode):
        return Merge(loader.construct_sequence(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!merge expects a sequence, got {type(node).__name__}",
            node.start_mark,
        )

SourcedYAMLLoader.add_constructor("!resource", _resource_constructor)
SourcedYAMLLoader.add_constructor("!include", _include_constructor)
SourcedYAMLLoader.add_constructor("!merge", _merge_constructor)


class SourcedYAMLDumper(SimpleYAMLDumper):
    pass

def _resource_representer(self: SourcedYAMLDumper, data: Resource):
    #                                                    ________ doesn't work, always quoted
    return self.represent_scalar("!resource", str(data), style="") # pyright: ignore[reportUnknownMemberType]

def _include_representer(self: SourcedYAMLDumper, data: Include):
    return self.represent_scalar("!include", str(data), style="") # pyright: ignore[reportUnknownMemberType]

def _merge_representer(self: SourcedYAMLDumper, data: Merge):
    return self.represent_sequence("!merge", data.items)

SourcedYAMLDumper.add_representer(Resource, _resource_representer)
SourcedYAMLDumper.add_representer(Include, _include_representer)
SourcedYAMLDumper.add_representer(Merge, _merge_representer)


class ABSENCE(Enum):
    VALUE = "absence"
ABSENCE_VALUE = ABSENCE.VALUE

WARNING_VERBOSE = False

def _link_to_str(link: Union[Path, PathWithJPointer]) -> str:
    if WARNING_VERBOSE:
        return str(link)
    if isinstance(link, Path):
        return link.name
    else:
        return str(PathWithJPointer(Path(link.filepath.name), link.fieldpath))

class FormatError(Exception):
    pass

class SourceLoadError(FormatError):
    def __init__(self, path: Path):
        self.path = path

    def __str__(self):
        return f"fail to load YAML, file: {_link_to_str(self.path)}\n" + (f"\n{self.__cause__}" if self.__cause__ else "")

class SchemaLoadError(FormatError):
    def __init__(self, path: Path):
        self.path = path

    def __str__(self):
        return f"fail to load schema, file: {_link_to_str(self.path)}" + (f"\n{self.__cause__}" if self.__cause__ else "")

class FileAlreadyLoadedError(FormatError):
    def __init__(self, filepath: Path):
        self.filepath = filepath
    def __str__(self) -> str:
        return f"file is already loaded in SourcedLoader: {self.filepath}"

class SourceRefLoopError(FormatError):
    def __init__(self, link: PathWithJPointer):
        self.link = link

    def __str__(self):
        return f"source includes form a loop: {_link_to_str(self.link)}"

class SchemaRefLoopError(FormatError):
    def __init__(self, link: PathWithJPointer):
        self.link = link

    def __str__(self):
        return f"schema refs form a loop: {_link_to_str(self.link)}"

class SourceIncludeFieldAccessError(FormatError):
    def __init__(self, src_link: PathWithJPointer):
        self.src_link = src_link
    
    def __str__(self):
        return f"fail to access field path of inclusion {_link_to_str(self.src_link)} (as unresolved yaml)"

class SchemaRefFieldAccessError(FormatError):
    def __init__(self, link: PathWithJPointer):
        self.link = link
    
    def __str__(self):
        return f"fail to access field path of schema ref {_link_to_str(self.link)} (as json schema)"

class FormatErrorGroup(FormatError):
    def __init__(self, errors: List[FormatError]):
        self.errors = list(errors)

    def __str__(self):
        return "format error:" +  "".join("\n  " + str(e) for e in self.errors)

    @contextlib.contextmanager
    def collect(self):
        try:
            yield
        except FormatErrorGroup as e:
            self.errors.extend(e.errors)
        except FormatError as e:
            self.errors.append(e)

class FormatWarning(Warning):
    pass

class SchemaMetadataParseWarning(FormatWarning):
    def __init__(self, value: Any, expected: Union[type, Tuple[type, ...]]):
        self.value = value
        self.expected = (expected,) if isinstance(expected, type) else expected
    
    def __str__(self):
        return f"fail to parse {self.value!r} in schema, expect " + ", ".join(t.__name__ for t in self.expected)

class SchemaUnknownTypeWarning(FormatWarning):
    def __init__(self, link: PathWithJPointer, value: JSON):
        self.link = link
        self.value = value
    
    def __str__(self):
        return f"unknown type of schema {self.link}: {json.dumps(self.value)}"

class AccessWarning(Warning):
    pass

class FieldAccessWarning(AccessWarning):
    def __init__(self, link: PathWithJPointer):
        self.link = link
    
    def __str__(self):
        return f"fail to access field {_link_to_str(self.link)}"

class SchemaFieldAccessWarning(AccessWarning):
    def __init__(self, link: PathWithJPointer, key: Union[int, str]):
        self.link = link
        self.key = key
    
    def __str__(self):
        return f"fail to get schema of field {repr(self.key)} from {_link_to_str(self.link)}"

class FieldValueOverwriteWarning(AccessWarning):
    def __init__(self, link: PathWithJPointer, value: str):
        self.link = link
        self.value = value
    
    def __str__(self):
        return f"overwrite value at {_link_to_str(self.link)} to {self.value}"

class SchemaWarning(Warning):
    pass

class SchemaTypeMismatchWarning(SchemaWarning):
    def __init__(self, value_link: PathWithJPointer, value_type: str, schema_link: PathWithJPointer, schema_type: str):
        self.value_link = value_link
        self.value_type = value_type
        self.schema_link = schema_link
        self.schema_type = schema_type
    
    def __str__(self):
        return (
            f"field {_link_to_str(self.value_link)} ({self.value_type})"
            f" doesn't match schema {_link_to_str(self.schema_link)} ({self.schema_type})"
        )

class ResolveWarning(Warning):
    pass

class IncompatibleMergeWarning(ResolveWarning):
    def __init__(self, left_src_link: PathWithJPointer, left_type: str, right_src_link: PathWithJPointer, right_type: str):
        self.left_src_link = left_src_link
        self.left_type = left_type
        self.right_src_link = right_src_link
        self.right_type = right_type
    
    def __str__(self):
        return "incompatible types to merge:\n  left: {} as {}\n  right: {} as {}".format(
            str(_link_to_str(self.left_src_link)), self.left_type,
            str(_link_to_str(self.right_src_link)), self.right_type,
        )

class SchemaDefaultTypeMismatchWarning(ResolveWarning):
    def __init__(self, link: PathWithJPointer, default: JSON, expected_type: str):
        self.link = link
        self.default = default
        self.expected_type = expected_type
    
    def __str__(self):
        return f"schema default value at {self.link} ({self.default}) doesn't match its type {self.expected_type}"

class SyncResourceWarning(Warning):
    pass

class BadResourceSchemeURIWarning(SyncResourceWarning):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"bad resource scheme URI: {self.resource}"

class ManualSyncResourceWarning(SyncResourceWarning):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"sync resource cannot be specified manually: {self.resource}"

class SyncResourceUnknownRuntimeWarning(SyncResourceWarning):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"runtime of sync resource is unknown: {self.resource}"

class SyncResourceSourceNotAbsoluteWarning(SyncResourceWarning):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"source path of sync resource is not an absolute path: {self.resource}"

class YAMLSynchronizerWarning(Warning):
    pass

class FormatWarningGroup(YAMLSynchronizerWarning):
    def __init__(self, error: FormatErrorGroup):
        self.error = error

    def __str__(self):
        return str(self.error)

class RootIsNotMapWarning(YAMLSynchronizerWarning):
    def __init__(self, path: Path):
        self.path = path
    
    def __str__(self):
        return f"root node must be a map: {self.path}"

class SyncFileNotReadyWarning(YAMLSynchronizerWarning):
    def __init__(self, which: str):
        self.which = which
    
    def __str__(self):
        return f"{self.which} yaml file is not ready"

class UnsupportedDeletionSynchronizationWarning(YAMLSynchronizerWarning):
    def __init__(self, fieldpath: JPointer):
        self.fieldpath = fieldpath
    
    def __str__(self):
        return f"try to delete field {self.fieldpath} and sync to original file, it is unsupported"


@dataclass(frozen=True)
class SyncInfo:
    source: Path      # /path/to/source (can contain ${ENVVAR})
    destination: Path # ${ROS_HOME}/resources/sync/path/to/source
    machine: str      # machine://usr:pswd@host/path/to/env_loader.sh (empty -> local)

    @staticmethod
    def to_list(infos: List["SyncInfo"]) -> List[JSON]:
        return [
            {
                "source": str(info.source),
                "destination": str(info.destination),
                "machine": info.machine,
            }
            for info in infos
        ]

class SyncResourceManager:
    """
    manage how to deal with resource URI for synchronization.
    
    !resource URI given by user refers to actual resource locations,
    which can be used directly on runtime except for local resource `file://...`.
    local resource should be synchronized,
    and URI should be rewritten into target location before loading into parameter server,
    so that programs can access synchronized resources via resource URIs.
    """
    @raises(ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning)
    def rewrite_for_sync(self, resource: Resource, sync_resources: List[SyncInfo]) -> str:
        """
        rewrite Resource to str, so that it can be loaded by rosparam properly.
        it maps local file (file://) to runtime resource dir (ros_home://),
        and all resources need to be synchronized will be collected.
        """
        if self.get_sync_target(resource.uri) is not None:
            warnings.warn(ManualSyncResourceWarning(resource))
            return resource.uri

        if not resource.uri.startswith("file://"):
            return resource.uri

        machine = self.get_machine(resource)
        if not machine:
            warnings.warn(SyncResourceUnknownRuntimeWarning(resource))

        src_path = Path(resource.uri[len("file://"):])
        if not src_path.is_absolute():
            warnings.warn(SyncResourceSourceNotAbsoluteWarning(resource))
            src_path = src_path.resolve()
        ros_home_uri = self.set_sync_source(src_path)
        src_path_env = Path(str(src_path).replace("$", r"${DOLLARSIGN}"))
        dst_path_env = self.get_sync_target(ros_home_uri)
        assert dst_path_env is not None
        sync_resources.append(SyncInfo(src_path_env, dst_path_env, machine))
        return ros_home_uri

    def get_sync_target(self, uri: str) -> Optional[Path]:
        """
        get target path (with envvar) of a synchronized resource URI,
        it can be expanded to actual target path on designated machine.
        return None if it is not a synchronized resource.
        """
        if uri.startswith("ros_home://resources/sync"):
            return Path(r"${ROS_HOME}/" + uri[len("ros_home://"):].replace("$", r"${DOLLARSIGN}"))
        return None

    def set_sync_source(self, local_path: Path) -> str:
        """
        construct synchronized resource URI for given local path.
        """
        assert local_path.is_absolute()
        return "ros_home://resources/sync/" + str(local_path.relative_to("/"))

    def get_machine(self, resource: Resource) -> str:
        """
        get designated machine of a synchronized resource URI.
        """
        return dict(resource.context).get("runtime_machine", "")

    @overload
    def attach_machine(self, resource: Resource, machine: str) -> Resource: ...
    @overload
    def attach_machine(self, resource: Include, machine: str) -> Include: ...
    def attach_machine(self, resource: Union[Resource, Include], machine: str) -> Union[Resource, Include]:
        """
        attach machine to a synchronized resource URI or Include.
        """
        if isinstance(resource, Resource):
            if not resource.uri.startswith("file://"):
                return resource
            return Resource(resource.uri, resource.context + (("runtime_machine", machine),))
        else:
            return Include(resource.link, resource.context + (("runtime_machine", machine),))

_V = TypeVar("_V")

@dataclass(frozen=True)
class SchemaMetadata:
    description: str
    choices: Tuple[JSON, ...]
    default: JSON
    range: Tuple[float, float]

    @raises(SchemaMetadataParseWarning)
    @staticmethod
    def _SchemaJSON_checked_get(data: SchemaJSON, key: str, expected: Union[type, Tuple[type, ...]], default: _V) -> _V:
        if not isinstance(data, dict):
            warnings.warn(SchemaMetadataParseWarning(data, dict))
            return default
        if key not in data:
            return default
        value = data[key]
        if expected and not isinstance(value, expected):
            warnings.warn(SchemaMetadataParseWarning(value, expected))
            return default
        return cast(_V, value)

    @raises(SchemaMetadataParseWarning)
    @staticmethod
    def _SchemaJSON_checked_get_JSON(data: SchemaJSON, key: str) -> JSON:
        if not isinstance(data, dict):
            warnings.warn(SchemaMetadataParseWarning(data, dict))
            return None
        if key not in data:
            return None
        value = data[key]
        if not is_JSON(value):
            warnings.warn(SchemaMetadataParseWarning(value, object))
            return None
        return value

    @raises(SchemaMetadataParseWarning)
    @staticmethod
    def parse(node: SchemaJSON) -> "SchemaMetadata":
        if not isinstance(node, dict):
            node = {}
        return SchemaMetadata(
            SchemaMetadata._SchemaJSON_checked_get(node, "description", str, ""),
            tuple(
                SchemaMetadata._SchemaJSON_checked_get_JSON(choice, "const")
                for choice in SchemaMetadata._SchemaJSON_checked_get(node, "oneOf", list, [])
            ),
            SchemaMetadata._SchemaJSON_checked_get_JSON(node, "default"),
            (
                float(SchemaMetadata._SchemaJSON_checked_get(node, "minimum", (int, float), -math.inf)),
                float(SchemaMetadata._SchemaJSON_checked_get(node, "maximum", (int, float), math.inf)),
            ),
        )

AccessType = Union[
    Tuple[Literal["null"],   None,                        List["Source"]],
    Tuple[Literal["scalar"], Union[JSONScalar, Resource], List["Source"]],
    Tuple[Literal["map"],    List[str],                   List["Source"]],
    Tuple[Literal["seq"],    range,                       List["Source"]],
]

SchemaAccessType = Union[
    Tuple[Literal["any"],     None],
    Tuple[Literal["struct"],  Dict[str, "SchemaSource"]],
    Tuple[Literal["dict"],    "SchemaSource"],
    Tuple[Literal["array"],   "SchemaSource"],
    Tuple[Literal["null"],    Type[None]],
    Tuple[Literal["scalar"],  Union[Type[bool], Type[int], Type[float], Type[str]]],
    Tuple[Literal["enum"],    List[JSONScalar]],
]

@dataclass(frozen=True)
class SchemaSource:
    """
    simple json schema

    <schema>  = {                             // struct
                  "type": "object",
                  "properties": {
                    (<string>: <schema>,)*
                  }
                }
              | {                             // dict
                  "type": "object",
                  "additionalProperties": <schema>
                }
              | {                             // array
                  "type": "array",
                  "items": <schema>
                }
              | { "type": "null" }            // null
              | {                             // scalar
                  "type": "boolean" | "integer" | "number" | "string"
                }
              | { "enum": [ (<scalar>,)* ] }  // enumerated values
              | { "const": <scalar> }         // constant values
              | { "anyOf": [ <schema> ] }     // wrap
              | { "$ref": <path> }            // ref
              | {}                            // any
    
    data is the schema json.
    link is the file path of this schema + the field path to current node as a JSON object.
    """
    
    link: PathWithJPointer
    data: SchemaJSON

    @staticmethod
    def any() -> "SchemaSource":
        return SchemaSource(PathWithJPointer(), {})

    def get_inner(self) -> Optional["SchemaSource"]:
        if isinstance(self.data, dict) and isinstance(anyOf := self.data.get("anyOf", []), list) and len(anyOf) == 1:
            return SchemaSource(self.link.append("anyOf").append(0), anyOf[0])
        return None

    def get_ref(self) -> Optional[Union[PathWithJPointer, JPointer]]:
        if isinstance(self.data, dict) and isinstance(ref := self.data.get("$ref", None), str):
            if ref.startswith("#"):
                return JPointer.parse(ref[1:])
            else:
                return PathWithJPointer.parse(ref) # TODO: our parsing order is different from the standard
        return None

    def is_direct(self) -> bool:
        return self.get_inner() is None and self.get_ref() is None

    @raises(SchemaUnknownTypeWarning)
    @prerequisite(is_direct)
    def access(self) -> SchemaAccessType:
        assert self.is_direct()
        
        if isinstance(self.data, dict) and self.data.get("type") is None:
            return "any", None

        if (isinstance(self.data, dict) and isinstance(enum := self.data.get("enum"), list)
            and len(set(type(e) for e in enum)) == 1 and isinstance(enum[0], (bool, int, float, str))):
            return "enum", cast(List[JSONScalar], enum)

        if isinstance(self.data, dict) and isinstance(const := self.data.get("const"), (bool, int, float, str)):
            return "enum", [const]

        if isinstance(self.data, dict):
            node_type = self.data.get("type", "")

            if (
                node_type == "object"
                and isinstance(properties := self.data.get("properties", None), dict)
            ):
                return "struct", {
                    key: SchemaSource(self.link.append("properties").append(key), node)
                    for key, node in properties.items()
                }

            if (
                node_type == "object"
                and isinstance(additionalProperties := self.data.get("additionalProperties", None), dict)
            ):
                return "dict", SchemaSource(self.link.append("additionalProperties"), additionalProperties)

            if node_type == "array" and "items" in self.data:
                return "array", SchemaSource(self.link.append("items"), self.data["items"])

            if node_type == "null":
                return "null", type(None)
            if node_type == "boolean":
                return "scalar", bool
            if node_type == "integer":
                return "scalar", int
            if node_type == "number":
                return "scalar", float
            if node_type == "string":
                return "scalar", str

        warnings.warn(SchemaUnknownTypeWarning(self.link, self.data))
        return "any", None

    @raises(SchemaUnknownTypeWarning)
    @prerequisite(is_direct)
    def get_field(self, key: Union[int, str]) -> Optional["SchemaSource"]:
        type_value = self.access()

        if type_value[0] == "any":
            return self

        elif type_value[0] == "struct":
            if isinstance(key, str) and key in type_value[1]:
                return type_value[1][key]

        elif type_value[0] == "dict":
            if isinstance(key, str):
                return type_value[1]

        elif type_value[0] == "array":
            if isinstance(key, int):
                return type_value[1]

        return None

    @raises(SchemaUnknownTypeWarning, SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning)
    @prerequisite(is_direct)
    def access_default(self, schema_type_value: SchemaAccessType) -> AccessType:
        if schema_type_value[0] == "any":
            return "null", None, []
        elif schema_type_value[0] == "null":
            return "null", None, []
        elif schema_type_value[0] == "array":
            return "seq", range(0), []
        elif schema_type_value[0] == "dict":
            return "map", [], []
        elif schema_type_value[0] == "struct":
            return "map", list(schema_type_value[1].keys()), []

        metadata = SchemaMetadata.parse(self.data)
        if schema_type_value[0] == "scalar":
            if type(metadata.default) != schema_type_value[1]:
                warnings.warn(SchemaDefaultTypeMismatchWarning(self.link, metadata.default, schema_type_value[1].__name__))
                return "scalar", schema_type_value[1](), []
            else:
                return "scalar", cast(JSONScalar, metadata.default), []
        elif schema_type_value[0] == "enum":
            if metadata.default not in schema_type_value[1]:
                enum_str = " | ".join(repr(e) for e in schema_type_value[1])
                warnings.warn(SchemaDefaultTypeMismatchWarning(self.link, metadata.default, enum_str))
                return "scalar", schema_type_value[1][0], []
            else:
                return "scalar", metadata.default, []
        else:
            assert False

    def resolve_path(self, path: Path) -> Path:
        return (self.link.filepath.parent / path).resolve()

    def resolve_link(self, link: Union[PathWithJPointer, JPointer]) -> PathWithJPointer:
        if isinstance(link, JPointer):
            return PathWithJPointer(self.link.filepath, link)
        else:
            return PathWithJPointer(self.resolve_path(link.filepath), link.fieldpath)

    @raises(SchemaLoadError)
    @staticmethod
    def load(src: Path) -> SchemaJSON:
        try:
            with open(src, "r", encoding="utf-8") as f:
                return yaml.load(f, Loader=SimpleYAMLLoader)
        except (yaml.YAMLError, OSError) as e:
            raise SchemaLoadError(src) from e

@dataclass
class Source:
    """
    source of YAML file with !include, !merge, !resource tags.
    
    it keeps tagged structures (as Include, Merge, Resource objects) so that can be edited easily.
    
    itself is a reference, the actual node is accessing via data property,
    and can be assigned instead of replaced.
    
    it also track accumulated context from the root.
    
    link is the file path of this source + the field path to current node as an unresolved JSON object,
    that is, field path may contain index of list to be merged.
    """
    link: PathWithJPointer
    parent: Union[SourcedJSON, Dict[Path, Union[SourcedJSON, ABSENCE]]]
    key: Union[int, str, Path]
    context: Tuple[Tuple[str, str], ...]

    def __post_init__(self):
        # ensure accessibility
        self.data

    @property
    def data(self) -> SourcedJSON:
        return cast(SourcedJSON, self.parent[self.key]) # pyright: ignore[reportCallIssue, reportArgumentType, reportIndexIssue, reportOptionalSubscript]

    @data.setter
    def data(self, value: SourcedJSON):
        self.parent[self.key] = value # pyright: ignore[reportArgumentType, reportCallIssue, reportIndexIssue, reportOptionalSubscript]

    def is_direct(self) -> bool:
        return not isinstance(self.data, Merge) and not isinstance(self.data, Include)

    @raises(IncompatibleMergeWarning)
    @staticmethod
    def access(sources: List["Source"]) -> Tuple[Literal["null", "scalar", "map", "seq"], List["Source"]]:
        first_type = "null"
        first_source = None
        res: List[Source] = []
        for source in reversed(sources):
            data = source.data
            if data is None: continue

            inspected_type = ""
            if isinstance(data, dict):
                inspected_type = "map"
            elif isinstance(data, list):
                inspected_type = "seq"
            else:
                assert not isinstance(data, (Include, Merge))
                inspected_type = "scalar"
            
            if first_type != "null" and first_type != inspected_type:
                assert first_source is not None
                warnings.warn(IncompatibleMergeWarning(source.link, inspected_type, first_source.link, first_type))
                break
            first_type = inspected_type
            first_source = source
            res.append(source)

        return first_type, list(reversed(res))

    @staticmethod
    def get_field(sources: List["Source"], key: Union[int, str]) -> List["Source"]:
        # sources is the value returned by Source.access(),
        # key must match type of sources
        if isinstance(key, str):
            sources_: List[Source] = []
            for source in sources:
                data = source.data
                assert isinstance(data, dict)
                if key in data:
                    sources_.append(Source(source.link.append(key), data, key, source.context))
            return sources_

        else:
            sources_: List[Source] = []
            for source in sources:
                data = source.data
                assert isinstance(data, list)
                if key in range(len(data)):
                    sources_.append(Source(source.link.append(key), data, key, source.context))
            return sources_

    def resolve_path(self, path: Path) -> Path:
        return (self.link.filepath.parent / path).resolve()

    def resolve_link(self, link: PathWithJPointer) -> PathWithJPointer:
        return PathWithJPointer(self.resolve_path(link.filepath), link.fieldpath)

    def resolve_resource(self, resource: Resource) -> Resource:
        """resolve file://relative_path, prepend context"""
        if not resource.uri.startswith("file://"):
            return Resource(resource.uri, self.context + resource.context)
        path = self.resolve_path(Path(resource.uri[len("file://"):]))
        return Resource(f"file://{path}", self.context + resource.context)

    @raises(SourceLoadError)
    @staticmethod
    def load(src: Path) -> SourcedJSON:
        try:
            with open(src, "r", encoding="utf-8") as f:
                return yaml.load(f, Loader=SourcedYAMLLoader)
        except (yaml.YAMLError, OSError) as e:
            raise SourceLoadError(src) from e

@dataclass
class SourcedNode:
    """
    a node of YAML file with !include, !merge, !resource tags.
    
    it is composed of multiple Source and SchemaSource because !merge.
    
    link is the file path + field path of current node as resolved JSON object.
    the file path refers to the root YAML file, not included YAML files;
    the field path may walk into included YAML file.
    
    sources are nodes to be merged.
    schema is the schema to be checked, set by user explicitly,
    and error will be raised if you try to access the field invalid for these schema.
    if sources is empty, default value based on schema will be used.
    """
    link: PathWithJPointer
    sources: List[Source]
    schema: Optional[SchemaSource] = None

    def is_direct(self) -> bool:
        return all(not source.is_direct() for source in self.sources) and (self.schema is None or self.schema.is_direct())
    
    def print(self, stream: Optional[IO[str]] = None):
        stream = stream if stream is not None else sys.stdout
        print("# ---", file=stream)
        print("# !merge_all", file=stream)
        if self.schema is not None:
            print("# $schema: " + str(self.schema.link), file=stream)
        for source in self.sources:
            print("---", file=stream)
            print("# $id: " + str(source.link), file=stream)
            yaml.dump(source.data, stream, Dumper=SourcedYAMLDumper, sort_keys=False)

    @raises(IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning)
    @prerequisite(is_direct)
    def access(self) -> AccessType:
        assert self.is_direct()
        
        schema = self.schema or SchemaSource.any()
        type_, sources = Source.access(self.sources)

        if type_ == "null":
            schema_type_value = schema.access()

            if schema_type_value[0] == "any":
                return type_, None, sources
            elif schema_type_value[0] == "null":
                return type_, None, sources
            else:
                return schema.access_default(schema_type_value)

        elif type_ == "scalar":
            data = sources[-1].data
            assert isinstance(data, (bool, int, float, str, Resource))
            if isinstance(data, Resource):
                data = sources[-1].resolve_resource(data)

            schema_type_value = schema.access()

            if schema_type_value[0] == "any":
                return type_, data, sources
            elif schema_type_value[0] == "scalar":
                value = str(data) if isinstance(data, Resource) else data
                if type(value) != schema_type_value[1]:
                    warnings.warn(SchemaTypeMismatchWarning(self.link, type(value).__name__, schema.link, schema_type_value[1].__name__))
                return type_, data, sources
            elif schema_type_value[0] == "enum":
                value = str(data) if isinstance(data, Resource) else data
                if value not in schema_type_value[1]:
                    enum_str = " | ".join(repr(e) for e in schema_type_value[1])
                    warnings.warn(SchemaTypeMismatchWarning(self.link, repr(value), schema.link, enum_str))
                return type_, data, sources
            else:
                warnings.warn(SchemaTypeMismatchWarning(self.link, type_, schema.link, schema_type_value[0]))
                return type_, data, sources

        elif type_ == "map":
            keys: List[str] = []
            for source in sources:
                data = source.data
                assert isinstance(data, dict)
                for key in data.keys():
                    if key not in keys:
                        keys.append(key)

            schema_type_value = schema.access()

            if schema_type_value[0] == "any":
                return type_, keys, sources
            elif schema_type_value[0] == "dict":
                return type_, keys, sources
            elif schema_type_value[0] == "struct":
                schema_keys = set(schema_type_value[1].keys())
                keys.extend(schema_keys - set(keys))
                return type_, keys, sources
            else:
                warnings.warn(SchemaTypeMismatchWarning(self.link, type_, schema.link, schema_type_value[0]))
                return type_, keys, sources

        elif type_ == "seq":
            # zip longest
            length = 0
            for source in sources:
                data = source.data
                assert isinstance(data, list)
                length = max(length, len(data))

            schema_type_value = schema.access()

            if schema_type_value[0] == "any":
                return type_, range(length), sources
            elif schema_type_value[0] == "array":
                return type_, range(length), sources
            else:
                warnings.warn(SchemaTypeMismatchWarning(self.link, type_, schema.link, schema_type_value[0]))
                return type_, range(length), sources

        else:
            assert False

    @raises(SchemaUnknownTypeWarning)
    def schema_type(self) -> Literal["any", "struct", "dict", "array", "null", "scalar", "enum"]:
        return self.schema.access()[0] if self.schema is not None else "any"

@dataclass
class SourceLoader:
    """
    a loader for YAML file with !include, !merge, !resource tags.
    
    it provides methods to load/access/update sourced nodes.
    it caches included YAML files and schema files.
    
    the loader will warn on invalid include, bad merge, mismatched to schema, etc,
    and supports certain form of $schema.
    """
    all_includes: Dict[Path, Union[SourcedJSON, ABSENCE]] = field(default_factory=lambda: {})
    all_schema: Dict[Path, Union[SchemaJSON, ABSENCE]] = field(default_factory=lambda: {})
    sync_resource_manager: SyncResourceManager = field(default_factory=SyncResourceManager)

    def print(self, stream: Optional[IO[str]] = None):
        stream = stream if stream is not None else sys.stdout
        for filepath, data in self.all_includes.items():
            if data is not ABSENCE_VALUE:
                print("---", file=stream)
                print("# $id: " + str(filepath), file=stream)
                yaml.dump(data, stream, Dumper=SourcedYAMLDumper, sort_keys=False)

    def _anon_yaml_path(self) -> Path:
        while True:
            path = Path("anon_" + str(uuid4()).replace("-", "_") + ".yaml").resolve()
            if not path.exists() and path not in self.all_includes:
                return path

    def _anon_schema_path(self) -> Path:
        while True:
            path = Path("anon_" + str(uuid4()).replace("-", "_") + ".schema.json").resolve()
            if not path.exists() and path not in self.all_schema:
                return path

    @raises(SourceLoadError)
    def _load_source(self, filepath: Path, context: Tuple[Tuple[str, str], ...]) -> Source:
        # lazy load yaml file
        if filepath not in self.all_includes:
            self.all_includes[filepath] = ABSENCE_VALUE
            data = Source.load(filepath)
            self.all_includes[filepath] = data
        return Source(PathWithJPointer(filepath), self.all_includes, filepath, context)

    @raises(FileAlreadyLoadedError)
    def _new_source(self, filepath: Optional[Path], data: JSON = None) -> Source:
        if filepath is None:
            filepath = self._anon_yaml_path()
        if filepath in self.all_includes:
            raise FileAlreadyLoadedError(filepath)
        self.all_includes[filepath] = cast(SourcedJSON, data)
        return Source(PathWithJPointer(filepath), self.all_includes, filepath, ())

    @raises(SchemaLoadError)
    def _load_schema(self, filepath: Path) -> SchemaSource:
        if filepath not in self.all_schema:
            self.all_schema[filepath] = ABSENCE_VALUE
            schema = SchemaSource.load(filepath)
            self.all_schema[filepath] = schema
        schema = self.all_schema[filepath]
        assert schema is not ABSENCE_VALUE
        return SchemaSource(PathWithJPointer(filepath), schema)

    @raises(FileAlreadyLoadedError)
    def _new_schema(self, filepath: Optional[Path], schema: SchemaJSON) -> SchemaSource:
        if filepath is None:
            filepath = self._anon_schema_path()
        if filepath in self.all_schema:
            raise FileAlreadyLoadedError(filepath)
        self.all_schema[filepath] = schema
        return SchemaSource(PathWithJPointer(filepath), schema)

    @raises(SchemaLoadError, SchemaRefLoopError, SchemaRefFieldAccessError)
    def _resolve_indirect_schema(self, schema: SchemaSource) -> Tuple[SchemaSource, Set[Path]]:
        depends: Set[Path] = set()
        visited: Set[PathWithJPointer] = set()
        while True:
            inner = schema.get_inner()
            if inner is not None:
                schema = inner
                continue

            ref = schema.get_ref()
            if ref is not None:
                ref = schema.resolve_link(ref)
                if ref in visited:
                    # loop
                    raise SchemaRefLoopError(ref)
                visited.add(ref)
                depends.add(ref.filepath)

                schema_ = self._load_schema(ref.filepath)
                try:
                    schema_data = ref.fieldpath.walk(schema_.data)
                except FieldAccessError as err:
                    raise SchemaRefFieldAccessError(schema_.link.extend(err.path)) from err
                schema = SchemaSource(schema_.link.extend(ref.fieldpath), schema_data)
                continue
            
            break

        return schema, depends

    @raises(FormatErrorGroup)
    # SourceLoadError, SourceRefLoopError, SourceIncludeFieldAccessError
    def _resolve_indirect_source(self, source: Source) -> Tuple[List[Source], Set[Path]]:
        visited: Set[PathWithJPointer] = set()
        outputs: List[Source] = []
        depends: Set[Path] = set()
        inputs = [(source, JPointer())]
        error = FormatErrorGroup([])
        while inputs:
            source, fieldpath = inputs.pop()
            data = source.data

            with error.collect():
                if isinstance(data, Include):
                    link = source.resolve_link(data.link)
                    if link in visited:
                        # loop
                        raise SourceRefLoopError(link)
                    visited.add(link)
                    depends.add(link.filepath)

                    # try
                    source_include = self._load_source(link.filepath, source.context + data.context)
                    
                    inputs.append((source_include, link.fieldpath.extend(fieldpath)))
                    continue

                if isinstance(data, Merge):
                    for i in range(len(data.items)):
                        source_i = Source(source.link.append(i), data.items, i, source.context)
                        inputs.append((source_i, fieldpath))
                    continue

                if not fieldpath:
                    outputs.append(source)
                    continue

                key = fieldpath.elements[0]
                fieldpath = fieldpath[1:]

                if isinstance(data, dict):
                    if key not in data:
                        raise SourceIncludeFieldAccessError(source.link.append(key))
                elif isinstance(data, list):
                    if not JPointer.is_index(key) or int(key) >= len(data):
                        raise SourceIncludeFieldAccessError(source.link.append(key))
                    key = int(key)
                else:
                    raise SourceIncludeFieldAccessError(source.link.append(key))

                source_key = Source(source.link.append(key), data, key, source.context)
                inputs.append((source_key, fieldpath))
                continue

        if error.errors:
            raise error

        return outputs, depends

    @raises(FormatErrorGroup)
    # SchemaLoadError, SchemaRefLoopError, SchemaRefFieldAccessError,
    # SourceLoadError, SourceRefLoopError, SourceIncludeFieldAccessError
    def resolve_indirect_(self, node: SourcedNode) -> Tuple[SourcedNode, Set[Path]]:
        depends: Set[Path] = set()
        sources: List[Source] = []
        error = FormatErrorGroup([])
        for source in node.sources:
            with error.collect():
                sources_, depends_ = self._resolve_indirect_source(source)
                sources.extend(sources_)
                depends.update(depends_)

        schema = None
        if node.schema is not None:
            with error.collect():
                schema, depends_ = self._resolve_indirect_schema(node.schema)
                depends.update(depends_)
        
        if error.errors:
            raise error
        return SourcedNode(node.link, sources, schema), depends

    @raises(FieldAccessWarning, SchemaFieldAccessWarning)
    def _get(self, node: SourcedNode, type_keys: AccessType, key: str) -> Optional[SourcedNode]:
        # assert node.is_direct()

        if type_keys[0] == "map":
            if key not in type_keys[1]:
                warnings.warn(FieldAccessWarning(node.link.append(key)))
                return None
            key_ = key

        elif type_keys[0] == "seq":
            if not JPointer.is_index(key) or int(key) not in type_keys[1]:
                warnings.warn(FieldAccessWarning(node.link.append(key)))
                return None
            key_ = int(key)

        else:
            warnings.warn(FieldAccessWarning(node.link.append(key)))
            return None

        sources = Source.get_field(type_keys[2], key_)
        if node.schema is not None:
            schema = node.schema.get_field(key_)
            if schema is None:
                warnings.warn(SchemaFieldAccessWarning(node.schema.link, key_))
        else:
            schema = None
        node = SourcedNode(node.link.append(key_), sources, schema)
        return node

    @raises(FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning)
    def walk_(self, node: SourcedNode, fieldpath: JPointer) -> Tuple[Optional[SourcedNode], Set[Path]]:
        depends: Set[Path] = set()
        for key in fieldpath.elements:
            node, depends_ = self.resolve_indirect_(node)
            depends.update(depends_)

            type_keys = node.access()
            node_ = self._get(node, type_keys, key)
            if node_ is None:
                return None, depends
            node = node_

        return node, depends

    @raises(FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning)
    def walk(self, node: SourcedNode, fieldpath: JPointer) -> Optional[SourcedNode]:
        return self.walk_(node, fieldpath)[0]

    @raises(SourceLoadError)
    def load(self, src: Path) -> SourcedNode:
        src = src.resolve()
        source = self._load_source(src, ())
        return SourcedNode(PathWithJPointer(src), [source], None)

    @raises(SchemaLoadError, SchemaRefFieldAccessError)
    def with_schema(self, node: SourcedNode, schema_src: PathWithJPointer) -> SourcedNode:
        schema_src = schema_src.resolve()
        schema = self._load_schema(schema_src.filepath)
        try:
            schema_data = schema_src.fieldpath.walk(schema.data)
        except FieldAccessError as err:
            raise SchemaRefFieldAccessError(schema.link.extend(err.path)) from err
        schema = SchemaSource(schema.link.extend(schema_src.fieldpath), schema_data)
        return SourcedNode(node.link, list(node.sources), schema)

    @raises(FileAlreadyLoadedError)
    def new(self, src: Optional[Path], data: JSON = None) -> SourcedNode:
        if src is not None:
            src = src.resolve()
        source = self._new_source(src, data)
        return SourcedNode(PathWithJPointer(source.link.filepath), [source], None)

    @raises(FileAlreadyLoadedError)
    def with_new_schema(self, node: SourcedNode, schema_src: Optional[Path], schema: SchemaJSON) -> SourcedNode:
        if schema_src is not None:
            schema_src = schema_src.resolve()
        schema_source = self._new_schema(schema_src, schema)
        return SourcedNode(node.link, list(node.sources), schema_source)

    @raises(FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning)
    def resolve_all_(self, node: SourcedNode, sync_resources: Optional[List[SyncInfo]] = None) -> Tuple[JSON, Set[Path]]:
        """
        resolve full content of given node. returns resolved json object and its dependencies.
        the subnodes failed to resolve will be assigned to null.
        if sync_resources is not None, resource URI will be rewritten and collected into sync_resources.
        """
        node, depends = self.resolve_indirect_(node)

        root: Dict[str, JSON] = {}
        stack: List[Tuple[SourcedNode, Callable[[JSON], None]]]
        stack = [(node, lambda value, root=root: root.__setitem__("$", value))]
        error = FormatErrorGroup([])
        while stack:
            node, set_value = stack.pop()

            type_keys = node.access()
            if type_keys[0] == "map":
                res_: JSON = {}
                for key_ in reversed(type_keys[1]):
                    with error.collect():
                        subnode = self._get(node, type_keys, key_)
                        assert subnode is not None
                        subnode, depends_ = self.resolve_indirect_(subnode)
                        depends.update(depends_)
                        stack.append((subnode, lambda value, res_=res_, key_=key_: res_.__setitem__(key_, value)))
                set_value(res_)

            elif type_keys[0] == "seq":
                res_: JSON = []
                for key_ in reversed(type_keys[1]):
                    with error.collect():
                        subnode = self._get(node, type_keys, str(key_))
                        assert subnode is not None
                        subnode, depends_ = self.resolve_indirect_(subnode)
                        depends.update(depends_)
                        stack.append((subnode, lambda value, res_=res_, key_=key_: res_.__setitem__(slice(key_, key_), [value])))
                set_value(res_)

            else:
                value = type_keys[1]
                if isinstance(value, Resource):
                    if sync_resources is not None:
                        # TODO: collect errors
                        value = self.sync_resource_manager.rewrite_for_sync(value, sync_resources)
                    else:
                        value = value.uri
                set_value(value)

        if error.errors:
            raise error
        return root["$"], depends

    @raises(FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning)
    def resolve_all(self, node: SourcedNode, sync_resources: Optional[List[SyncInfo]] = None) -> JSON:
        return self.resolve_all_(node, sync_resources)[0]

    @raises(SourceLoadError, FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning)
    def load_resolve_all(self, link: PathWithJPointer) -> JSON:
        if isinstance(link, TypedPathWithJPointer):
            link.schema
            link.schema_root
            ...
        link = link.resolve()

        data = None
        node = self.load(link.filepath)
        node = self.walk(node, link.fieldpath)
        if node is not None:
            data = self.resolve_all(node)
        return data


    def _ensure_top(self, node: SourcedNode, ensure_null: bool):
        assert len(node.sources) > 0

        source = node.sources[-1]
        if isinstance(source.data, Include):
            source.data = Merge([source.data, None])

        elif isinstance(source.data, Merge):
            while isinstance(source.data, Merge):
                if len(source.data.items) == 0:
                    source.data = Merge([None])
                    continue
                index = len(source.data.items) - 1
                if isinstance(source.data.items[index], Merge):
                    source = Source(source.link.append(index), source.data.items, index, source.context)
                    continue
                if isinstance(source.data.items[index], Include):
                    source.data = Merge([*source.data.items, None])
                    continue
                if ensure_null and source.data.items[index] is not None:
                    source.data = Merge([*source.data.items, None])
                    break
                source = Source(source.link.append(index), source.data.items, index, source.context)

        else:
            if ensure_null and source.data is not None:
                source.data = Merge([source.data, None])

    @raises(FieldValueOverwriteWarning)
    def _ensure_get(self, node: SourcedNode, typ: Literal["map", "seq", "scalar", "null"], key: Union[int, str]):
        assert node.is_direct()
        assert len(node.sources) > 0

        source = node.sources[-1]
        if isinstance(key, str):
            if not isinstance(source.data, dict):
                if source.data is not None or typ != "map":
                    warnings.warn(FieldValueOverwriteWarning(node.link.append(key), "map"))
                source.data = {}
            if key not in source.data:
                source.data[key] = None
        else:
            if not isinstance(source.data, list):
                if source.data is not None or typ != "seq":
                    warnings.warn(FieldValueOverwriteWarning(node.link.append(key), "seq"))
                source.data = []
            if key not in range(len(source.data)):
                source.data.extend([None]*(1 + key - len(source.data)))

    @raises(FormatErrorGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            FieldValueOverwriteWarning)
    def _ensure_walk(self, node: SourcedNode, fieldpath: JPointer, ensure_null: bool) -> SourcedNode:
        for key in fieldpath.elements:
            self._ensure_top(node, False)
            node, _depends = self.resolve_indirect_(node)

            typ = node.access()[0]
            key_ = int(key) if typ == "seq" and JPointer.is_index(key) else key
            self._ensure_get(node, typ, key_)

            type_keys = node.access()
            node_ = self._get(node, type_keys, key)
            assert node_ is not None
            node = node_

        self._ensure_top(node, ensure_null)
        node, _depends = self.resolve_indirect_(node)
        return node

    @raises(FormatErrorGroup, TypeError,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            FieldValueOverwriteWarning)
    def update(self, node: SourcedNode, fieldpath: JPointer, value: Union[JSONScalar, Path], machine: str = ""):
        """
        update sourced node.
        only the file of current top layer will be mutated.
        warning will be raised if an existing field is overwritten.
        values can be paths, which will be converted to !resource, attached with machine information.

        <!> this may invalidate other sourced node.
        """
        if not isinstance(value, (bool, int, float, str, Path)): # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(value, (bool, int, float, str, Path))
        if isinstance(value, Path):
            value_ = Resource.create(value)
            if machine:
                value_ = self.sync_resource_manager.attach_machine(value_, machine)
        else:
            value_ = value

        subnode = self._ensure_walk(node, fieldpath, False)

        source = subnode.sources[-1]
        typ = subnode.access()[0]
        if typ not in ("scalar", "null"):
            warnings.warn(FieldValueOverwriteWarning(subnode.link, "scalar"))
        source.data = value_

    @raises(FormatErrorGroup, TypeError,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            FieldValueOverwriteWarning)
    def include(self, node: SourcedNode, fieldpath: JPointer, value: Union[str, Path, PathWithJPointer], machine: str = ""):
        """
        insert include into sourced node.
        only the file of current top layer will be mutated.
        warning will be raised if an existing field is overwritten.
        values must be paths, which will be converted to !include, attached with machine information.

        <!> this may invalidate other sourced node.
        """
        if not isinstance(value, (str, Path, PathWithJPointer)): # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(value, (str, Path, PathWithJPointer))
        include = Include.create(value)
        if machine:
            include = self.sync_resource_manager.attach_machine(include, machine)

        subnode = self._ensure_walk(node, fieldpath, True)

        source = subnode.sources[-1]
        assert source.data is None
        source.data = include


@raises(FormatErrorGroup,
        IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
        SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
        FieldAccessWarning, SchemaFieldAccessWarning,
        ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning)
def resolve_YAML(link: PathWithJPointer) -> JSON:
    """
    load and resolve yaml file, return resolved json
    (paths of local resources will not be rewritten).
    """
    return SourceLoader().load_resolve_all(link)


def get_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0

class YAMLWatcher:
    """
    watch mutation of dependent files of specific node, manage sourced node and resolved json.

    this is needed because loader will cache loaded files, but will not reload to latest verison automatically.
    """
    loader: SourceLoader
    mtimes: Dict[Path, int]
    path: Path
    depends: Set[Path]
    node: Optional[SourcedNode]
    data: JSON

    def __init__(self, path: Path):
        self.loader = SourceLoader()
        self.mtimes = {}

        self.path = path.resolve()
        self.depends = {self.path}
        self.node = None
        self.data = None

    def is_changed(self) -> bool:
        return any(self.mtimes.get(depend, 0) != get_mtime(depend) for depend in self.depends)

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning)
    def load(self, skip_empty: bool, aggregate_sync_resources: bool):
        """
        reload yaml file and resolved it.
        if skip_empty is true, null and empty mapping will be skipped.
        if aggregate_sync_resources is true, additional fields about all sync resources will be inserted.
        this is for monoresource.
        """
        self.depends = {self.path}
        self.node = None
        self.data = None

        for depend in list(self.mtimes.keys()):
            if self.mtimes[depend] != get_mtime(depend):
                del self.mtimes[depend]
                if depend in self.loader.all_includes:
                    del self.loader.all_includes[depend]
                if depend in self.loader.all_schema:
                    del self.loader.all_schema[depend]

        error = FormatErrorGroup([])

        depends = {self.path}
        with error.collect():
            node = self.loader.load(self.path)
        if error.errors:
            warnings.warn(FormatWarningGroup(error))
            return

        sync_resources: Optional[List[SyncInfo]] = [] if aggregate_sync_resources else None
        data = None
        with error.collect():
            data, depends_ = self.loader.resolve_all_(node, sync_resources)
            depends.update(depends_)
        if error.errors:
            warnings.warn(FormatWarningGroup(error))
            return

        self.mtimes = {
            depend: get_mtime(depend)
            for depend in [*self.loader.all_includes.keys(), *self.loader.all_schema.keys()]
        }
        
        if skip_empty:
            data = deep_copy_skip_empty(data)
        if aggregate_sync_resources:
            assert sync_resources is not None
            sync_resources_json = SyncInfo.to_list(sync_resources)
            if sync_resources_json:
                data = data if data is not None else {}

                if not isinstance(data, dict):
                    warnings.warn(RootIsNotMapWarning(self.path))
                    return

                if not isinstance(data.get("$sync_resources"), list):
                    data["$sync_resources"] = []
                cast(List[JSON], data["$sync_resources"]).extend(sync_resources_json)

        self.depends = depends
        self.node = node
        self.data = data

    def save(self, path: Path):
        """
        save corresponding sourced node to given yaml file.
        you cannot add new include paths in this way.

        mutating sourced node usually make resolved json and file out-of-sync with it.
        """
        if path in self.loader.all_includes:
            data = self.loader.all_includes[path]
        elif path in self.loader.all_schema:
            data = self.loader.all_schema[path]
        else:
            assert False
        assert data is not ABSENCE_VALUE
        with open(path, "w") as f:
            yaml.dump(data, f, Dumper=SourcedYAMLDumper, sort_keys=False)


def deep_copy_skip_empty(obj: JSON) -> Optional[JSON]:
    """
    copy json, remove null and empty map.
    since null/empty map in seq cannot be removed, empty map is filled in
    (rosparam bans null, even in seq).
    return null if obj is null.
    """
    if obj is None: return None

    if isinstance(obj, dict):
        out1: Dict[str, JSON] = {}
        for k, v in obj.items():
            v = deep_copy_skip_empty(v)
            if v is None: continue
            out1[k] = v
        if not out1: return None
        return out1

    if isinstance(obj, list):
        out2: List[JSON] = []
        for e in obj:
            e = deep_copy_skip_empty(e)
            e = e if e is not None else {}
            out2.append(e)
        return out2

    return deep_copy(obj)

def rosparam_diff(old: JSON, new: JSON) -> Dict[JPointer, Optional[JSON]]:
    """
    diff in the scalar level, returns dict from path to updated values (None for deletion).
    treat null as empty map, seq as scalar.
    (this is for updating ros parameter, because rosparam treat seq as scalar)
    """
    updated: Dict[JPointer, Optional[JSON]] = {}

    def walk(path: JPointer, a: JSON, b: JSON):
        nonlocal updated
        if a is None: a = {}
        if b is None: b = {}
        a_is_map = isinstance(a, dict)
        b_is_map = isinstance(b, dict)

        if not a_is_map and b_is_map:
            updated[path] = None
            walk(path, {}, b)

        elif a_is_map and not b_is_map:
            walk(path, a, {})
            updated[path] = b

        elif not a_is_map and not b_is_map:
            if not deep_eq(a, b):
                updated[path] = b

        elif a_is_map and b_is_map:
            keys = list(a.keys())
            for k in b.keys():
                if k not in keys:
                    keys.append(k)
            for k in keys:
                av = a.get(k, {})
                bv = b.get(k, {})
                walk(path.append(k), av, bv)

    walk(JPointer(), old, new)
    return updated

_ResolvedListener = Callable[[Dict[JPointer, Optional[JSON]]], None] # [JPointer]JSON? -> None
_BackResolvedListener = Callable[[Path, Dict[JPointer, Optional[SourcedJSON]]], None] # (Path, [JPointer]SourcedJSON?) -> None

class YAMLSynchronizer:
    """
    synchronize sourced yaml and resolved yaml files.
    both files must exist at initial.
    
    first spin will update resolved yaml if they are out-of-sync.
    
    mutating both sides at the same time may cause undefined behavior.
    """
    original: YAMLWatcher
    resolved: YAMLWatcher
    _resolved_listeners: List[_ResolvedListener]
    _back_resolved_listeners: List[_BackResolvedListener]
    skip_empty: bool = True
    aggregate_sync_resources: bool = True
    
    def __init__(self, original_path: Path, resolved_path: Path):
        self.original = YAMLWatcher(original_path)
        self.resolved = YAMLWatcher(resolved_path)
        self._resolved_listeners = []
        self._back_resolved_listeners = []

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning)
    def init(self):
        self.resolved.load(self.skip_empty, self.aggregate_sync_resources)

    def get_status(self):
        status = ""
        status += f"original: {self.original.path} " + ("(ready)" if self.original.node is not None else "(error)") + "\n"
        status += f"resolved: {self.resolved.path} " + ("(ready)" if self.resolved.node is not None else "(error)") + "\n"
        status += f"track: " + ", ".join(str(dep) for dep in self.original.depends) + "\n"
        return status

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning, SyncFileNotReadyWarning)
    def resolve(self) -> Dict[JPointer, JSON]:
        """
        resolve sourced yaml file and update resolved yaml file.
        returns difference for rosparam.
        skips if resolved and sourced yaml files are not loaded correctly.
        """
        # TODO: compare with $sync_resources, don't update it

        if self.resolved.node is None:
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        self.original.load(self.skip_empty, self.aggregate_sync_resources)
        if self.original.node is None:
            warnings.warn(SyncFileNotReadyWarning("original"))
            return {}

        diff = rosparam_diff(self.resolved.data, self.original.data)
        if diff:
            self.resolved.loader.all_includes[self.resolved.path] = cast(SourcedJSON, self.original.data)
            self.resolved.save(self.resolved.path)
        return diff

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning, SyncFileNotReadyWarning)
    def back_resolve(self) -> Dict[Path, Dict[JPointer, SourcedJSON]]:
        """
        back resolve resolved yaml file and update sourced yaml file.
        returns difference for each included path.
        due to merge machinism, fields cannot be removed.
        skips if resolved yaml file is not loaded correctly.
        """
        old_resolved_data = deep_copy(self.resolved.data)
        if self.resolved.node is None:
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        self.resolved.load(False, False)
        if self.resolved.node is None: # pyright: ignore[reportUnnecessaryComparison]
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        resolved_diff = deep_diff(old_resolved_data, self.resolved.data)

        diffs: Dict[Path, Dict[JPointer, SourcedJSON]] = {}

        if resolved_diff:
            if self.original.data is None:
                warnings.warn(SyncFileNotReadyWarning("original"))
                return {}

            old_original_sources = {
                depend: SourcedJSON_deep_copy(sourced_node)
                for depend in self.original.depends
                if (sourced_node := self.original.loader.all_includes.get(depend, ABSENCE_VALUE)) is not ABSENCE_VALUE
            }

            update: Dict[JPointer, Union[JSONScalar, Path]] = {}
            for key, value in resolved_diff.items():
                if value is None:
                    # TODO: try to delete standalone (not-merged) field
                    warnings.warn(UnsupportedDeletionSynchronizationWarning(key))
                else:
                    update[key] = cast(JSONScalar, value)
            assert self.original.node is not None
            for key, value in update.items():
                self.original.loader.update(self.original.node, key, value)

            for depend in old_original_sources.keys():
                old = old_original_sources[depend]
                new = self.original.loader.all_includes[depend]
                assert new is not ABSENCE_VALUE
                diff = SourcedJSON_deep_diff(old, new)
                if diff:
                    diffs[depend] = diff

        for depend in diffs.keys():
            self.original.save(depend)

        return diffs

    def add_resolved_listener(self, callback: _ResolvedListener):
        self._resolved_listeners.append(callback)

    def add_back_resolved_listener(self, callback: _BackResolvedListener):
        self._back_resolved_listeners.append(callback)

    def _resolved_listener(self, diff: Dict[JPointer, Optional[JSON]]):
        for lisener in self._resolved_listeners:
            lisener(diff)

    def _back_resolved_listener(self, path: Path, diff: Dict[JPointer, Optional[SourcedJSON]]):
        for lisener in self._back_resolved_listeners:
            lisener(path, diff)

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning, SyncFileNotReadyWarning)
    def spin_once(self):
        if self.original.is_changed():
            diff = self.resolve()
            if diff:
                self._resolved_listener(diff)
        if self.resolved.is_changed():
            diffs = self.back_resolve()
            for depend, diff in diffs.items():
                self._back_resolved_listener(depend, diff)

    @raises(FormatWarningGroup,
            IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
            SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
            FieldAccessWarning, SchemaFieldAccessWarning,
            ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
            RootIsNotMapWarning, SyncFileNotReadyWarning)
    def spin(self, dt: float = 0.1):
        import time
        status = ""
        while True:
            self.spin_once()
            status_ = self.get_status()
            if status != status_:
                print(status_)
            status = status_
            time.sleep(dt)


@dataclass(frozen=True)
class PathWithXPointer:
    """
    a xml file path with xml pointer, represents part of a xml node.  
    format: /path/to/file.yaml#xpointer(/sub/field[@sel='val'])  
    where "#/sub/field[@sel='val']" indicates the selector for this xml document.
    xml pointer should be %-encoded to escape '%' and '#'.
    since it is parsed from the right side, if file path contains "#", just suffix with "#".
    note that this is different from standard url.
    """
    filepath: Path
    xpath: str
    
    @staticmethod
    def parse(path: str) -> "PathWithXPointer":
        filepath, fieldpath = (*path.rsplit("#", 1), "")[:2]
        if fieldpath and not (fieldpath.startswith("xpointer(") and fieldpath.endswith(")")):
            raise ValueError(f"invalid xpointer: {path}")
        fieldpath = fieldpath[len("xpointer("):-len(")")]
        fieldpath = urllib.parse.unquote(fieldpath)
        return PathWithXPointer(Path(filepath), fieldpath)

    def __str__(self):
        # minimal %-encoded
        filepath = str(self.filepath)
        xpath = urlquote(self.xpath, "#")
        if xpath:
            return f"{filepath}#xpointer({xpath})"
        elif "#" in filepath:
            return f"{filepath}#"
        else:
            return filepath

    def get(self):
        with open(self.filepath, "r") as f:
            root = ET.parse(f)
        # in ET.Element.find(), ./launch/rosparam means: {whatever root node} > launch > rosparam
        # but I want: launch (is the root node) > rosparam
        wrapper = ET.Element("_document")
        wrapper.append(root.getroot())
        return wrapper.find("." + self.xpath)

def parse_jpointer_or_xpointer(path: Union[str, Path, PathWithJPointer, PathWithXPointer]) -> Union[PathWithJPointer, PathWithXPointer]:
    if isinstance(path, PathWithJPointer):
        return path
    if isinstance(path, PathWithXPointer):
        return path

    if isinstance(path, Path):
        path = str(path)
        if "#" in path:
            path += "#"

    suffix = Path(path.rsplit("#", 1)[0]).suffix

    if suffix in (".json", ".yaml"):
        link = PathWithJPointer.parse(path)
        link = PathWithJPointer(link.filepath.resolve(), link.fieldpath)
        return link
    
    elif suffix in (".xml", ".launch"):
        pointer = PathWithXPointer.parse(path)
        pointer = PathWithXPointer(pointer.filepath.resolve(), pointer.xpath)
        return pointer

    else:
        raise ValueError(f"unknown file type: {path}")

@raises(FormatWarningGroup,
        IncompatibleMergeWarning, SchemaUnknownTypeWarning, SchemaTypeMismatchWarning,
        SchemaMetadataParseWarning, SchemaDefaultTypeMismatchWarning,
        FieldAccessWarning, SchemaFieldAccessWarning,
        ManualSyncResourceWarning, SyncResourceUnknownRuntimeWarning, SyncResourceSourceNotAbsoluteWarning,
        RootIsNotMapWarning, SyncFileNotReadyWarning)
def to_resolved(source_path: Union[str, Path, PathWithJPointer, PathWithXPointer], skip_empty: bool = True, aggregate_sync_resources: bool = True) -> str:
    """
    resolve yaml file, save as {name}.resolved.yaml, returns resolved yaml file path.

    if skip_empty is true, null and empty mapping will be skipped.
    if aggregate_sync_resources is true, it will append field "$sync_resources" at the root.
    """
    
    source_path = parse_jpointer_or_xpointer(source_path)
    parent = source_path.filepath.parent
    stem = source_path.filepath.stem

    if isinstance(source_path, PathWithXPointer):
        extracted_path = parent / f"{stem}.extracted.yaml"
        print(f"yaml is embeded in a xml, extract into {extracted_path}", file=sys.stderr)
        elem = source_path.get()
        if elem is None:
            raise ValueError(f"fail to read embeded param: {source_path}")
        extracted_path.write_text(elem.text or "")
        source_path = PathWithJPointer(extracted_path)

    if source_path.fieldpath:
        extracted_path = parent / f"{stem}.extracted.yaml"
        print(f"only a portion of yaml need to be resolved, extract into {extracted_path}", file=sys.stderr)
        raw_source_json = load_ExYAML(source_path, True)
        raw_source_str = yaml.dump(raw_source_json, Dumper=ExYAMLDumper, sort_keys=False)
        extracted_path.write_text(raw_source_str)
        source_path = PathWithJPointer(extracted_path)

    source_path = source_path.filepath

    resolved_path = parent / f"{stem}.resolved.yaml"
    print(f"resolve {source_path} -> {resolved_path}", file=sys.stderr)
    if resolved_path.exists():
        resolved_path.unlink()
    resolved_path.touch()
    sync = YAMLSynchronizer(source_path, resolved_path)
    sync.skip_empty = skip_empty
    sync.aggregate_sync_resources = aggregate_sync_resources
    sync.init()
    sync.resolve()

    return str(resolved_path)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m monolaunch.monoparam <source yaml file>\n" + cleandoc(to_resolved.__doc__ or ""), file=sys.stderr)
        exit(1)
    print(to_resolved(sys.argv[1]))
