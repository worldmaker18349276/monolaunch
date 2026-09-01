"""
a YAML utility tools for simple YAML.

it is based on pyyaml but focus on simple YAML.

YAML supports more flexible structure than JSON, such as:
- shared nodes (circular referencing is also valid)
- mapping with non-string keys
- custom objects

that are not what we want, we just want to serialize object in YAML format.
we only needs to consider:
- None
- distinguishable simple scalar type: bool, int, float (including nan and inf), string
- simple container type: list and dict with string keys

and we prefer the syle:
- None -> `null`, bool -> `false`, `true`
- single-line string always double quoted (except keys of map)
- multi-line string use `|` style if possible
- float always contains `.`, including `.nan` and `.inf`
- flow-style for simple list and dict (they have same scalar value type, and dict only contains single-letter keys)
- block seq style always indent, so that it can be folded in editor

we provide some tools for dealing with JSON object in deep.
where we treat null as an empty slot.
we also provide a simple resolver for !include and !merge tags:

- `!include` imports another YAML file.

  base.yaml:
  ```
  a: 1
  b: 2
  ```
  
  config.yaml:
  ```
  base: !include base.yaml
  ```
  
  resolve to:
  ```
  base:
    a: 1
    b: 2
  ```
  the path of !include is relative to the current location (directory of the file contains this term)

- `!merge` merges a list of mappings.

  ```
  config: !merge
    - timeout: 10
      retries: 3
    - timeout: 30
  ```
  
  resolve to:
  ```
  config:
    timeout: 30
    retries: 3
  ```

  rules:
  - null <> any = any <> null = any   --  null behave like empty slot
  - scalar <> scalar = later one
  - seq <> seq = zip longest with <>
  - map <> map = union zip with <>
  - non-null type <> another non-null type = later one

ExYAMLLoader also keeps unknown tags and construct them as TaggedScalar, TaggedList, TaggedDict,
and ExYAMLDumper is able to re-export those tagged objects.
you can use `python -m monolaunch.yaml_utils <yaml file>` directly to resolve YAML with !include and !merge.
"""
from inspect import cleandoc
import math
from typing import Any, Dict, Generator, List, Set, Tuple, Union, Optional
import os.path
from pathlib import Path
from dataclasses import dataclass, field
import yaml

__all__ = [
    "JSONScalar", "JSON",
    "is_JSON", "assert_JSON",
    "deep_update", "deep_merge", "deep_copy", "deep_eq", "deep_diff", "deep_iter",
    "FieldAccessError", "FieldPath", "Link",
    "SimpleYAMLLoader", "load_YAML", "SimpleYAMLDumper", "save_YAML",
    "TaggedScalar", "TaggedDict", "TaggedList", "TaggedJSON",
    "ExYAMLLoader", "load_ExYAML", "ExYAMLDumper", "save_ExYAML",
]


JSONScalar = Union[bool, int, float, str] # int, float are different, nan, inf are allowed
JSON = Union[None, JSONScalar, List["JSON"], Dict[str, "JSON"]]

@dataclass(frozen=True)
class TaggedScalar:
    data: JSONScalar
    tag: str = ""

class TaggedDict(Dict[str, "TaggedJSON"]):
    tag: str = ""

class TaggedList(List["TaggedJSON"]):
    tag: str = ""

TaggedJSON = Union[None, JSONScalar, List["TaggedJSON"], Dict[str, "TaggedJSON"], TaggedScalar, TaggedList, TaggedDict]


def is_JSON(data: Any) -> bool:
    if type(data) in (type(None), bool, int, float, str):
        return True
    elif type(data) == list:
        return all(is_JSON(e) for e in data) # type: ignore
    elif type(data) == dict:
        return all(type(k) == str and is_JSON(v) for k, v in data.items()) # type: ignore
    else:
        return False

def assert_JSON(data: Any) -> JSON:
    if not is_JSON(data):
        raise TypeError(f"not json: {data}")
    return data


def deep_copy(obj: JSON) -> JSON:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_copy(e) for e in obj]
    return obj

def deep_update(base: JSON, update: JSON) -> JSON:
    """
    update base by copying update, returns updated base.
    update dict and list, treating null as an empty slot.
    """
    # skip null
    if base is not None and update is None:
        return base

    if type(base) != type(update):
        return deep_copy(update)

    if isinstance(base, dict):
        assert isinstance(update, dict)
        for k, v in update.items():
            base[k] = deep_update(base.get(k), v)
        return base

    if isinstance(base, list):
        assert isinstance(update, list)
        # zip longest
        if len(base) < len(update):
            base.extend([None]*(len(update) - len(base)))
        elif len(base) > len(update):
            update = [*update, *[None]*(len(base) - len(update))]
        for i in range(len(base)):
            base[i] = deep_update(base[i], update[i])
        return base

    return deep_copy(update)

def _deep_merge(path: "FieldPath", base: JSON, update: JSON) -> Tuple[JSON, List["FieldPath"]]:
    # skip null
    if base is not None and update is None:
        return base, []

    if base is None and update is not None:
        return deep_copy(update), []

    if type(base) != type(update):
        return base, [path]

    if isinstance(base, dict):
        assert isinstance(update, dict)
        inconsistencies: List[FieldPath] = []
        for k, v in update.items():
            v, a = _deep_merge(path.append(k), base.get(k), v)
            base[k] = v
            inconsistencies.extend(a)
        return base, inconsistencies

    if isinstance(base, list):
        assert isinstance(update, list)
        inconsistencies: List[FieldPath] = []
        # zip longest
        if len(base) < len(update):
            base.extend([None]*(len(update) - len(base)))
        elif len(base) > len(update):
            update = [*update, *[None]*(len(base) - len(update))]
        for i in range(len(base)):
            v, a = _deep_merge(path.append(i), base[i], update[i])
            base[i] = v
            inconsistencies.extend(a)
        return base, inconsistencies

    if base == update:
        return base, []

    return base, [path]

def deep_merge(base: JSON, update: JSON) -> Tuple[JSON, List["FieldPath"]]:
    """
    merge base by copying update, returns merged base and updated paths.
    unlike deep_update, different values at the same field will not be overrided, and warnings will be raised.
    """
    return _deep_merge(FieldPath(), base, update)

def deep_eq(lhs: JSON, rhs: JSON) -> bool:
    """
    deep compare two jsons.
    noting that nan equal to nan, True and 1 and 1.0 are different.
    """
    stack: List[Tuple[JSON, JSON]] = [(lhs, rhs)]
    while stack:
        lhs, rhs = stack.pop()
        if type(lhs) != type(rhs):
            return False

        if isinstance(lhs, dict):
            assert isinstance(rhs, dict)
            if set(lhs.keys()) != set(rhs.keys()):
                return False
            for k in lhs:
                stack.append((lhs[k], rhs[k]))
            continue
        
        if isinstance(lhs, list):
            assert isinstance(rhs, list)
            if len(lhs) != len(rhs):
                return False
            for i in range(len(lhs)):
                stack.append((lhs[i], rhs[i]))
            continue

        # special case: nan != nan
        if isinstance(lhs, float) and isinstance(rhs, float) and math.isnan(lhs) and math.isnan(rhs):
            continue

        if lhs != rhs:
            return False

    return True

def deep_diff(old: JSON, new: JSON) -> Dict["FieldPath", Optional[JSON]]:
    """
    diff in the scalar level.
    keys are paths, values can be scalars for updating values, or maps/seqs for changing types, or None for deletion.
    """
    updated: Dict[FieldPath, Optional[JSON]] = {}

    stack: List[Tuple[FieldPath, JSON, JSON]] = [(FieldPath(), old, new)]
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

        # scalar
        if not deep_eq(a, b):
            updated[path] = b
            continue

    return updated

def deep_iter(obj: JSON) -> Generator[Tuple["FieldPath", JSONScalar], None, None]:
    """
    traverse into dict/list until scalar, skip null, yield path and scalar.
    """
    stack = [(FieldPath(), obj)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for key in list(value.keys()):
                stack.append((path.append(key), value[key]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append((path.append(key), value[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield path, value


class FieldAccessError(Exception):
    def __init__(self, path: "FieldPath", obj: str):
        self.obj = obj
        self.path = path
    
    def __str__(self):
        return f"fail to access {self.path} from {self.obj}"

@dataclass(frozen=True)
class FieldPath:
    """
    a path for traversing nested map and seq.
    element can be str for accessing map, or int for accessing seq.
    """
    elements: Tuple[Union[int, str], ...] = field(default_factory=tuple)

    def __post_init__(self):
        for key in self.elements:
            if isinstance(key, str) and "/" in key:
                raise ValueError(f"element of FieldPath cannot contain '/': {key}")

    @staticmethod
    def parse(fieldpath: str) -> "FieldPath":
        path: List[Union[int, str]] = []
        for e in fieldpath.strip("/").split("/"):
            if e:
                if all(d in "0123456789" for d in e):
                    e = int(e, 10)
                path.append(e)
        return FieldPath(tuple(path))

    def __str__(self) -> str:
        return "/" + "/".join(str(e) for e in self.elements)

    def __repr__(self) -> str:
        return f"FieldPath.parse({str(self)!r})"

    def __truediv__(self, key_or_subpath: Union[int, str, "FieldPath"]) -> "FieldPath":
        if isinstance(key_or_subpath, FieldPath):
            return self.extend(key_or_subpath)
        elif isinstance(key_or_subpath, str):
            return self.extend(FieldPath.parse(key_or_subpath))
        else:
            return self.append(key_or_subpath)
    
    def append(self, key: Union[int, str]) -> "FieldPath":
        return FieldPath(self.elements + (key,))
    
    def extend(self, subpath: "FieldPath") -> "FieldPath":
        return FieldPath(self.elements + subpath.elements)
    
    def __bool__(self) -> bool:
        return bool(self.elements)
    
    def __getitem__(self, index: slice) -> "FieldPath":
        assert isinstance(index, slice)
        return FieldPath(self.elements[index])

    def is_prefix(self, longer: "FieldPath") -> bool:
        return longer.elements[:len(self.elements)] == self.elements

    # @raises(FieldAccessError)
    def walk(self, node: JSON) -> JSON:
        for i, key in enumerate(self.elements):
            if isinstance(key, str):
                if not isinstance(node, dict) or key not in node:
                    raise FieldAccessError(self[:i+1], f"{type(node).__name__} object")
                node = node[key]
            else:
                if not isinstance(node, list) or key not in range(len(node)):
                    raise FieldAccessError(self[:i+1], f"{type(node).__name__} object")
                node = node[key]
        return node

@dataclass(frozen=True)
class Link:
    """
    a json pointer represents part of a json object.  
    format: /path/to/file.yaml#/sub/field  

    "#/sub/field" indicates the subfield of this json object.
    "#/sub/field" and "#sub/field" have no difference.
    since it is parsed from the right side, field path cannot contain "#",
    and if file path contains "#", just suffix with "#".
    """
    filepath: Path = field(default_factory=Path)
    fieldpath: FieldPath = field(default_factory=FieldPath)
    
    @staticmethod
    def parse(file_field_path: str) -> "Link":
        filepath, fieldpath = (*file_field_path.rsplit("#", 1), "")[:2]
        return Link(Path(filepath), FieldPath.parse(fieldpath))

    @staticmethod
    def create(link: Union[str, Path, "Link"]) -> "Link":
        if isinstance(link, str):
            link = Link.parse(link)
        elif isinstance(link, Path):
            link = Link(link)
        return link

    def __truediv__(self, key: Union[int, str, FieldPath]) -> "Link":
        """right concat"""
        return Link(self.filepath, self.fieldpath / key)

    def append(self, key: Union[int, str]) -> "Link":
        return Link(self.filepath, self.fieldpath.append(key))

    def extend(self, subfieldpath: FieldPath) -> "Link":
        return Link(self.filepath, self.fieldpath.extend(subfieldpath))

    def relative_to(self, path: Path) -> "Link":
        """left divide"""
        return Link(Path(os.path.relpath(self.filepath, path)), self.fieldpath)
    
    def __str__(self) -> str:
        if self.fieldpath or "#" in str(self.filepath):
            return str(self.filepath) + "#" + str(self.fieldpath)
        else:
            return str(self.filepath)

    def __repr__(self) -> str:
        return f"Link.parse({str(self)!r})"


class SimpleYAMLLoader(yaml.SafeLoader):
    """
    load yaml format as json object: no complex key for maps, no alias.
    """
    def compose_node(self, parent: Optional[yaml.nodes.Node], index: int):
        if self.check_event(yaml.AliasEvent): # type: ignore
            raise yaml.YAMLError("Aliases are not allowed")

        event = self.peek_event() # type: ignore
        if getattr(event, "anchor", None) is not None: # type: ignore
            raise yaml.YAMLError("Anchors are not allowed")

        return super().compose_node(parent, index)

def _dict_constructor(loader: SimpleYAMLLoader, node: yaml.nodes.Node) -> Dict[str, Any]:
    assert isinstance(node, yaml.nodes.MappingNode)
    res = loader.construct_mapping(node, deep=True)
    wrong_key_type = next((type(key).__name__ for key in res.keys() if type(key) != str), None)
    if wrong_key_type is not None:
        raise yaml.constructor.ConstructorError(f"key of map must be str, got: {wrong_key_type}")
    return res # type: ignore

SimpleYAMLLoader.add_constructor("tag:yaml.org,2002:map", _dict_constructor)

# @raises(FieldAccessError)
def load_YAML(link: Link) -> JSON:
    """
    load yaml format as json object: no complex key for maps, no alias.
    """
    with open(link.filepath, 'r') as f:
        data = yaml.load(f, Loader=SimpleYAMLLoader)
    return link.fieldpath.walk(data)


class ExYAMLLoader(SimpleYAMLLoader):
    def set_filepath(self, filepath: Path):
        self.filepath = filepath

def _include_constructor(loader: ExYAMLLoader, node: yaml.nodes.Node) -> TaggedJSON:
    if not isinstance(node, yaml.nodes.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a str scalar, got {type(node).__name__}",
            node.start_mark,
        )

    link = loader.construct_scalar(node)
    if not isinstance(link, str): # pyright: ignore[reportUnnecessaryIsInstance]
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a str scalar, got {type(node).__name__}",
            node.start_mark,
        )

    link = Link.parse(link)

    subfilepath = loader.filepath.parent / link.filepath
    with open(subfilepath, 'r') as f:
        subloader = type(loader)(f)
        subloader.set_filepath(subfilepath)
        try:
            data = subloader.get_single_data()
        finally:
            subloader.dispose() # pyright: ignore[reportUnknownMemberType]
    return link.fieldpath.walk(data) # type: ignore

def _merge_constructor(loader: ExYAMLLoader, node: yaml.nodes.Node) -> TaggedJSON:
    if not isinstance(node, yaml.nodes.SequenceNode):
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!merge expects a sequence, got {type(node).__name__}",
            node.start_mark,
        )

    objs = loader.construct_sequence(node, deep=True)
    if not objs:
        return None
    obj = objs[0]
    for obj_ in objs[1:]:
        obj = deep_update(obj, obj_)
    return obj # type: ignore

def _unknown_tag_constructor(loader: ExYAMLLoader, tag_suffix: str, node: yaml.nodes.Node) -> TaggedJSON:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = _dict_constructor(loader, node)
    else:
        assert False

    if isinstance(value, list):
        value = TaggedList(value)
        value.tag = tag_suffix
        return value
    if isinstance(value, dict):
        value = TaggedDict(value)
        value.tag = tag_suffix
        return value
    return TaggedScalar(value, tag_suffix)

ExYAMLLoader.add_constructor("!include", _include_constructor)
ExYAMLLoader.add_constructor("!merge", _merge_constructor)
ExYAMLLoader.add_multi_constructor("!", _unknown_tag_constructor) # pyright: ignore[reportUnknownMemberType]

# @raises(FieldAccessError)
def load_ExYAML(link: Link) -> TaggedJSON:
    """
    load yaml with !include and !merge, and keep other tags.
    """
    filepath = link.filepath.resolve()
    with open(filepath, 'r') as f:
        loader = ExYAMLLoader(f)
        loader.set_filepath(filepath)
        try:
            data = loader.get_single_data()
        finally:
            loader.dispose() # type: ignore
    return link.fieldpath.walk(data) # type: ignore


class SimpleYAMLDumper(yaml.SafeDumper):
    """
    dump json object as yaml format without alias.
    str scalars always quote.
    block style seqs always indent.
    vector-like seqs/maps use flow style.
    """
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        # for indent block style seqs.
        # prefer
        # ```
        # seq:
        #   - item1
        #   - item2
        # ```
        # instead of
        # ```
        # seq:
        # - item1
        # - item2
        # ```
        return super().increase_indent(flow, False)

    def ignore_aliases(self, data: Any):
        return True

def is_vec_like(data: Union[JSON, TaggedJSON]) -> bool:
    DTYPES: List[Set[type]] = [{bool}, {int}, {float}, {str}]
    if isinstance(data, dict) and all(len(k) == 1 for k in data.keys()) and set(type(v) for v in data.values()) in DTYPES:
        return True
    if isinstance(data, list) and set(type(v) for v in data) in DTYPES:
        return True
    return False

def _list_representer(self: SimpleYAMLDumper, data: List[JSON]):
    return self.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=is_vec_like(data))

def _dict_representer(self: SimpleYAMLDumper, data: Dict[str, JSON]):
    content = [
        (yaml.SafeDumper.represent_str(self, k), self.represent_data(v)) # type: ignore
        for k, v in data.items()
    ]
    return yaml.nodes.MappingNode('tag:yaml.org,2002:map', content, flow_style=is_vec_like(data))

def _dstr_representer(self: SimpleYAMLDumper, data: str):
    if "\n" in data:
        return self.represent_scalar('tag:yaml.org,2002:str', data, style='|') # type: ignore
    else:
        return self.represent_scalar('tag:yaml.org,2002:str', data, style='"') # type: ignore

SimpleYAMLDumper.add_representer(list, _list_representer)
SimpleYAMLDumper.add_representer(dict, _dict_representer)
SimpleYAMLDumper.add_representer(str, _dstr_representer)

def save_YAML(data: JSON, path: Path):
    """
    dump json object as yaml format without alias.
    str scalars always quote.
    block style seqs always indent.
    vector-like seqs/maps use flow style.
    """
    with open(path, 'w') as f:
        yaml.dump(data, f, Dumper=SimpleYAMLDumper, sort_keys=False)


class ExYAMLDumper(SimpleYAMLDumper):
    pass

def _tagged_scalar_representer(self: ExYAMLDumper, data: TaggedScalar):
    node = self.represent_data(data.data) # pyright: ignore[reportUnknownMemberType]
    return self.represent_scalar(f"!{data.tag}", node.value) # pyright: ignore[reportUnknownMemberType]

def _tagged_list_representer(self: ExYAMLDumper, data: TaggedList):
    return self.represent_sequence(f"!{data.tag}", data, flow_style=is_vec_like(data))

def _tagged_dict_representer(self: ExYAMLDumper, data: TaggedDict):
    content = [
        (yaml.SafeDumper.represent_str(self, k), self.represent_data(v)) # type: ignore
        for k, v in data.items()
    ]
    return yaml.nodes.MappingNode(f"!{data.tag}", content, flow_style=is_vec_like(data))

ExYAMLDumper.add_representer(TaggedScalar, _tagged_scalar_representer)
ExYAMLDumper.add_representer(TaggedList, _tagged_list_representer)
ExYAMLDumper.add_representer(TaggedDict, _tagged_dict_representer)

def save_ExYAML(data: TaggedJSON, path: Path):
    """
    dump tagged json object, same as save_YAML.
    """
    with open(path, 'w') as f:
        yaml.dump(data, f, Dumper=ExYAMLDumper, sort_keys=False)


def _resolve_yaml(link: str):
    """
    load and dumps resolved yaml (!include, !merge are resolved, other tags are kept)
    """

    import warnings
    def formatwarning(message, category, filename, lineno, line=None): # type: ignore
        return "".join(
            f"# {'     ' if i else 'WARN:'} {line}\n"
            for i, line in enumerate(str(message).splitlines()) # type: ignore
        )
    warnings.formatwarning = formatwarning

    data = load_ExYAML(Link.parse(link))
    data_str = yaml.dump(data, Dumper=ExYAMLDumper, sort_keys=False)

    sys.stderr.flush()
    print(data_str)

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("python -m monolaunch.yaml_utils <yaml file>\n" + cleandoc(_resolve_yaml.__doc__ or ""), file=sys.stderr)
        exit(1)
    _resolve_yaml(sys.argv[1])
