# monolaunch

monolaunch is my replacement of roslaunch.

it is just a launch file generator, but in python instead of ugly XML.
the basic structure is the same, but we _fix_ some weird behaviors.
it supports parameter resolving, resource management and more.

## Prerequisites
ros-noetic, ssh, sshpass, rsync

## Usage
this is my old launch file
```xml
<launch>
  <arg name="vehicle_name" default="$(env vehicle_name)"/>
  <arg name="vins_config" default="$(env vins_config)"/>
  <arg name="rosconsole_config" default="$(env rosconsole_config)"/>

  <machine
    name="$(arg vehicle_name)"
    user="$(env vehicle_machine_user)"
    address="$(env vehicle_machine_address)"
    env-loader="$(env vehicle_machine_env_loader)"
    />

  <group ns="$(arg vehicle_name)">
    <arg name="feature_topic_expr" default="__import__('yaml').safe_load(open(vins_config).get('feature_topic', ''))"/>
    <arg name="feature_topic" default="$(eval eval(feature_topic_expr))"/>

    <arg name="use_external_features_expr" default="__import__('yaml').safe_load(open(vins_config).get('use_external_features', False))"/>
    <arg name="use_external_features" default="$(eval eval(use_external_features_expr))"/>

    <group unless="$(eval bool(feature_topic) if use_external_features else True)">
      <node error="feature_topic should be given if use_external_features is true"/>
    </group>

    <arg name="camera_topics_expr" default="__import__('yaml').safe_load(open(vins_config).get('camera_topics', []))"/>
    <arg name="camera_topics" default="$(eval eval(camera_topics_expr))"/>

    <arg name="camera_topics_remaps" default="$(eval
      ' '.join(
        f'~input_{i}/image_raw:={t}'
        for i, t in enumerate(camera_topics.split('\n'))
      )
    "/>
    <node
      machine="$(arg vehicle_name)"
      name="vins"
      pkg="vins"
      type="vins_node"
      args="$(arg camera_topics_remaps)">
      <env name="ROSCONSOLE_CONFIG_FILE" value="$(arg rosconsole_config)"/>
      <rosparam command="load" file="$(arg vins_config)"/>
      <remap from="~concat/image_raw" to="camera/concat/image_raw"/>

      ...
```
it's ugly and contains a lot of abuse of eval.
rewriting with monolaunch in python just fixes it.

we provide a simple launch file converter `launch_converter.py` (written with help of AI),
it is of cause barely runnable but good for migration.
or just rewrite your launch script by yourself
```python
from monolaunch.monolaunch import *

def drone_vins(vins_config: Link, rosconsole_config: Path, vehicle_name: str, vehicle_machine_uri: str):
    with machine(vehicle_machine_uri, name=vehicle_name), group(ns=vehicle_name):
        if get_value(vins_config / "use_external_features", False):
            assert get_value(vins_config / "feature_topic", ""), "feature_topic should be given if use_external_features is true"

        with node(name="vins", pkg="vins", type="vins_node"):
            set_env({'ROSCONSOLE_CONFIG_FILE': str(rosconsole_config)})
            load_param({'~': vins_config})
            remap({
                '~concat/image_raw': 'camera/concat/image_raw',
                **{
                    f'~input_{i}/image_raw': t
                    for i, t in enumerate(get_value(vins_config / "camera_topics", []))
                },
            })
            
            ...
```
it's now clean and readable.
to make it launchable, you should write a main function
```python
@run
def main():
    import argparse
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--config")
    args = argparser.parse_args()
    
    with machine("machine://localhost/wks/devel/setup.bash?=setup", name="local"):
        config = Link.parse(args.config)
        vins_config = config / "vins"
        rosconsole_config = Path(get_value(config / "rosconsole", ""))
        vehicle_name = get_value(config / "vehicle_name", "")
        vehicle_machine_uri = get_value(config / "vehicle", "")

        drone_vins(vins_config, rosconsole_config, vehicle_name, vehicle_machine_uri)
        
        ...
```
it is recommended to manage all configurations in one config file like this.
you must put local machine at the top level.
now you can execute this script to launch your nodes,
it will generate one launch file and launch it.

the basic structure is the same, almost every tag has a corresponding function:
| python                                | launch                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `run(launch_func)`                    | run launch script.  it will generate a launch file and launch the      |
|                                       | program under current work directory.                                  |
| `with group(ns=...)`                  | `<group>`.                                                             |
| `with node(name, pkg, type, ...)`     | `<node>`.                                                              |
| `with include(file, **args)`          | `<include>`, can contain env, remap, param.                            |
|                                       | file is an absolute path to the launch file to run.                    |
| `set_env(dict)`                       | `<env>`.                                                               |
| `remap(dict)`                         | `<remap>`.                                                             |
| `set_param(dict)`                     | `<param>`.                                                             |
| `load_param(dict)`                    | `<rosparam>`, load parameters from file, support json pointer.         |
| `get_value("file.yaml#/sub/field")`   | get value from given yaml file.                                        |
| `get_value("file.yaml#/sub/field", type)` |  if type doesn't match, error will be raised.                      |
| `with machine(name, address, ...)`    | `<machine>` with scope, set machine as default in a scope.             |
|                                       | for your convenience, you can pass in url like                         |
|                                       | "machine://user:pswd@addr/path/to/env_loader.sh" directly.             |
| `env(name, fallback)`                 | `$(env name)` or `$(optenv name fallback)`.                            |
| `find(pkg)`                           | `$(find pkg)`.                                                         |
| `anon(name)`                          | `$(anon name)`.                                                        |
| `dirname()`                           | `$(dirname)`.                                                          |
| `ns()`                                | get current namespace, or use `ns("~")` for private namespace.         |
| `@launch_prefix`                      | make launch_prefix function, see `launch_prefix`.                      |
| `load_logger("file.yaml#/logging")`   | load logger config.                                                    |
| `set_logger({"logger_name": "INFO"})` | set logger config directly.                                            |
| `with master(mode="auto")`            | borrow removed `<master>` tag, for launching roscore remotely.         |

the generated launch file includes all functionalities
(auto-launch remote roscore, initial parameter resolving, remote resources synchronization, remote network settings, etc),
and it can be moved to any location within the same machine.
it is recommended to run launch script everytimes.

you don't need to configure network to run launch script or launch the generated launch file,
all network settings are included in the generated launch file.
you should leave `ROS_MASTER_URI` untouched and don't pass `--wait` by yourself.
you should not pass launch file to roslaunch command via piping.

## Why
the purpose of launch file is providing a simple, direct and explicit way to manage nodes.
it is barely programmable, and only supports simple branch and simple python expression evaluation.
the benefit is clarity and simplicity, but it is insufficient to cope with increasingly complex tasks.

ros2 provides a launch python library (and seems want to treat it as default),
which is able to construct launch process directly, but it loses the simplicity and processes become unclear.
there is no library in the middle, and it is not easy to migrate from simple launch file to complex roslaunch library.

to keep up with increasing complexity, I have already push the programmability of launch files to its limit.
launch file only allows simple python expression eval, where `__` is banned, and `globals()` is deleted.
however, most of cases it is possible to squeeze logic into one line,
and the safety guard for `__` are easily bypassed -- just write down expression in another arg tag,
so that import is accessible:
```xml
<arg name="print_argv_expr" default="print(__import__('sys').argv)"/>
<arg name="print_argv" default="$(eval eval(print_argv_expr))"/>
```
by the way, deleting `globals()` is completely useless.

with this, we can even do some for-loop like operations, for example, remap variable number of topics:
```xml
<arg name="remap_args" default="$(eval ' '.join(
    f'~input_{i}/image_raw:={topic}'
    for i, topic in enumerate(topics.split(','))
))"/>
<node ... args="$(arg remap_args)"/>
```

the normal ways to affect the control flow of a launch file are using environment variables and command arguments,
but not yaml files, which are heavily used for controlling the behavior of nodes though.
somethimes environment variables and arguments are needed for controlling nodes
(ex. `ROSCONSOLE_CONFIG_FILE`, `static_transform_publisher` node),
however, although environment variables and arguments can flow into rosparam,
yaml files cannot be loaded as environment variables and arguments.
so I abuse eval
```xml
<arg name="frame_id_expr" default="__import__('yaml').safe_load(open(config))"/>
<arg name="frame_id" default="$(eval eval(frame_id_expr).get('frame_id', 'base_link'))"/>
```

even though, we sometimes need to write additional scripts to generate corresponding environment variables and yaml files.
every time I modify related logic, I have to search for different files, and try to confirm that is the statement I have to adjust.
the separation of logic causes a strong, inevitable coupling across different language.

the abuse of syntax and the separation of logic reveal that launching nodes is fundamentally not simple,
it shouldn't be crammed into a non-programmable language, so I make monolaunch.
making launch file programmable will definitely lose clarity.
to get both simplicity and clarity, monolaunch just generate a launch file instead of launching nodes by myself.
the basic structure is the same, almost every tag has a corresponding function.
since python is programmable, you no longer need to worry about how to make the launch script complicated,
keeping launch clear is now your responsibility.

the central idea of monolaunch is to manage launch and configuration in _one_ file.
when I say "one file," I mean that all related materials are linked through it,
and all of them are located in their proper places.
all launch logic are written in python, nice!
all configurations can be put into yaml file, good!
network settings can be separated from normal configuration, safest!
all resources can be managed in local place, the best!
in one word, monolaunch is pythonic: there should be one -- and preferably only one -- obvious way to do it.

we also fix some weird behaviors about param/remap/machine, and add some additional functionalities,
such as: launch-prefix written in python, native-like logger setting, remote network setup, remote roscore launch, etc.

## How
the following are some technical details of our implementation, which might not work for every versions of ros, it was only tested in ros-noetic.

since monolaunch use python, it is no longer necessary to use the arg tag as a variable needed in most cases.
all variables, branch and loop are evaluated during the generation phase.

param/remap/env/machine tags use weird rules for nodes and includes in the scope.
to fix, we collect all of them and manage by myself,
now all nodes and includes are flatten, remap and env directly apply to each node,
paramaters are collected into one file, machines are hoisted to the top level.

we abuse `$(eval ...)` to do some complex works,
it is possible because roslaunch fails to ban dunder and builtins
(/opt/ros/noetic/lib/python3/dist-packages/roslaunch/substitution_args.py, line 298, 343).
- call `monoparam.to_resolved` to resolve collected parameter file.
  it returns resolved parameter file, and will be loaded via `<load_param>`.
  note that arg evaluation happens before parameter loading, this should be fine.
- call `monoresource.sync` to synchronize resources to each machines.
  note that arg evaluation happens before launching nodes.
- access filename of generated launch file:
  launch file has `dirname` function but no `filename` function,
  we steal variable `filename` from `dirname` function's closure context,
  this may become invalid if implementation changed.
- relaunch roslaunch with the same command prefixed with `with_roscore.py --filename <filename>`.
  I have checked the source code of roslaunch, it should be fine for the latest version of ros-noetic.

to generate only one file, we embed collected parameters into xml plain text.
the yaml content contains !include/!merge/!resource and need to be resolved,
so it cannot be loaded directly.
it is put inside the tag `<rosparam command="load" file="..." param="/">`,
since it has `file` attribute, the inner text will be ignored.

since `with_roscore.py` and `monoparam.to_resolved` will parse original roslaunch file,
you cannot use piping to feed launch file: `roslaunch ... - < xxx.launch`.

we use invalid `<node>` tag to raise error conditionally:
```xml
<group if="$(eval ...)">
  <node error="..."/>
  <!-- fail with: RLException: <node> tag is missing required attribute: 'name'. Node xml is <node error="..."/> -->
</group>
```
it utilizes the way roslaunch resolves tags: it only checks if a tag is valid after it enters this branch.

we borrow the abandoned master tag for my auto-launch roscore mechanism.
roslaunch will ignore the master tag; it is only used to provide information to `with_roscore.py`.
technically, any unrecognized tag will be ignored.

the details will be documented in [mechanisms](mechanisms.md).
