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
    _tag: str = ""

class TaggedDict(Dict[str, "TaggedJSON"]):
    _tag: str = ""

class TaggedList(List["TaggedJSON"]):
    _tag: str = ""

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

def deep_update(dst: JSON, src: JSON) -> JSON:
    """
    update dst by copying src.
    update dict and list, treat null as empty slot.
    """
    # skip null
    if dst is not None and src is None:
        return dst

    if type(dst) != type(src):
        return deep_copy(src)

    if isinstance(dst, dict):
        assert isinstance(src, dict)
        for k, v in src.items():
            dst[k] = deep_update(dst.get(k), v)
        return dst

    if isinstance(dst, list):
        assert isinstance(src, list)
        # zip longest
        if len(dst) < len(src):
            dst.extend([None]*(len(src) - len(dst)))
        elif len(dst) > len(src):
            src = [*src, *[None]*(len(dst) - len(src))]
        for i in range(len(dst)):
            dst[i] = deep_update(dst[i], src[i])
        return dst

    return deep_copy(src)

def _deep_merge(path: "FieldPath", dst: JSON, src: JSON) -> Tuple[JSON, List["FieldPath"]]:
    # skip null
    if dst is not None and src is None:
        return dst, []

    if dst is None and src is not None:
        return deep_copy(src), []

    if type(dst) != type(src):
        return dst, [path]

    if isinstance(dst, dict):
        assert isinstance(src, dict)
        inconsistencies: List[FieldPath] = []
        for k, v in src.items():
            v, a = _deep_merge(path.append(k), dst.get(k), v)
            dst[k] = v
            inconsistencies.extend(a)
        return dst, inconsistencies

    if isinstance(dst, list):
        assert isinstance(src, list)
        inconsistencies: List[FieldPath] = []
        # zip longest
        if len(dst) < len(src):
            dst.extend([None]*(len(src) - len(dst)))
        elif len(dst) > len(src):
            src = [*src, *[None]*(len(dst) - len(src))]
        for i in range(len(dst)):
            v, a = _deep_merge(path.append(i), dst[i], src[i])
            dst[i] = v
            inconsistencies.extend(a)
        return dst, inconsistencies

    if dst == src:
        return dst, []

    return dst, [path]

def deep_merge(dst: JSON, src: JSON) -> Tuple[JSON, List["FieldPath"]]:
    """
    merge dst by copying src.
    unlike update, different values at the same field will not be overrided, and warnings will be raised.
    """
    return _deep_merge(FieldPath(), dst, src)

def deep_eq(a: JSON, b: JSON) -> bool:
    """
    deep compare two jsons.
    noting that nan equal to nan, True and 1 and 1.0 are different.
    """
    stack: List[Tuple[JSON, JSON]] = [(a, b)]
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

        # special case: nan != nan
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            continue

        if a != b:
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
                if not isinstance(node, list) or key >= len(node):
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
    if isinstance(node, yaml.nodes.ScalarNode):
        link = loader.construct_scalar(node)
        link = Link.parse(link)

        subfilepath = loader.filepath.parent / link.filepath
        with open(subfilepath, 'r') as f:
            subloader = type(loader)(f)
            subloader.set_filepath(subfilepath)
            try:
                data = subloader.get_single_data()
            finally:
                subloader.dispose()
        return link.fieldpath.walk(data) # type: ignore
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a scalar, got {type(node).__name__}",
            node.start_mark,
        )

def _merge_constructor(loader: ExYAMLLoader, node: yaml.nodes.Node) -> TaggedJSON:
    if isinstance(node, yaml.nodes.SequenceNode):
        objs = loader.construct_sequence(node, deep=True)
        if not objs:
            return None
        obj = objs[0]
        for obj_ in objs[1:]:
            obj = deep_update(obj, obj_)
        return obj # type: ignore
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!merge expects a sequence, got {type(node).__name__}",
            node.start_mark,
        )

def _unknown_tag_constructor(loader: ExYAMLLoader, tag_suffix: str, node: yaml.nodes.Node) -> TaggedJSON:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node)
    else:
        assert False

    if isinstance(value, list):
        value = TaggedList(value)
        value._tag = tag_suffix
        return value
    if isinstance(value, dict):
        value = TaggedDict(value)
        value._tag = tag_suffix
        return value
    return TaggedScalar(value, tag_suffix)

ExYAMLLoader.add_constructor("!include", _include_constructor)
ExYAMLLoader.add_constructor("!merge", _merge_constructor)
ExYAMLLoader.add_multi_constructor("!", _unknown_tag_constructor)

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
            loader.dispose()
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

def is_vec_like(data: JSON) -> bool:
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
    node = self.represent_data(data.data)
    return self.represent_scalar(f"!{data._tag}", node.value)

def _tagged_list_representer(self: ExYAMLDumper, data: TaggedList):
    return self.represent_sequence(f"!{data._tag}", data, flow_style=is_vec_like(data))

def _tagged_dict_representer(self: ExYAMLDumper, data: TaggedDict):
    content = [
        (yaml.SafeDumper.represent_str(self, k), self.represent_data(v)) # type: ignore
        for k, v in data.items()
    ]
    return yaml.nodes.MappingNode(f"!{data._tag}", content, flow_style=is_vec_like(data))

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
