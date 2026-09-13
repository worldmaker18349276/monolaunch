# Mechanisms

this document explains all the mechanisms in detail, including how bad the launch xml format is.

## Process
launch script has two phases: generation phase and launch phase:
- generation
  - run generation code, rerun after hitting the local machine for the first time
  - construct launch file
- launch
  - relaunch with roscore prefix for the first time
  - resolved parameters
  - synchronize resources
  - launch nodes

you can use `--dry-run (1|2|3)` to run until generating launch file/resolving parameters/synchronizing resources.

keep in mind that this is just a launch file generator,
you are constructing a launch file, not launching nodes directly.
only few operations are done during the launch phase:
`load_param`/`load_logger` only setup which file will be loaded,
the loading of yaml files occurs during the launch phase.
`launch_prefix` will be executed right before launching node.
parameter resolving and resource synchronization run during launch phase,
but before roscore is ready.

## Remap
the original mechanism of `<remap>` is:
- remap tags only affect contents after the tag, limited in the scope (launch, group, node),
  and also affect nested group and the contents of include.
  
- they affect a node just like bring those tags into node scope, that is,
  ```xml
  <remap .../>
  <node ...>
  </node>
  ```
  act just like
  ```xml
  <node ...>
      <remap .../>
  </node>
  ```
  
- to resolve a name, expand names under the node, than find the matched mapping.
  for example, under a node (`/ns/node_name`), resolving a name (`sub/field`):
  first, expand `sub/field` -> `/ns/sub/field`.
  then expand remap's name, for a remap (`field` -> `/another`), it becomes (`/ns/field` -> `/another`).
  it is different from `/ns/sub/field`, so it doesn't change.

  full expansion rule:
  - start with "/" -> no expansion
  - first element starts with "~" -> prepand with namespace and node name
  - otherwise -> prepand with namespace
  - special cases
    - `abc//efg` -> `abc/efg`     (warning, still work)
    - `/~abc/efg` -> `/~abc/efg`  (unusable)
    - `~/abc/efg` -> `/abc/efg`   (why???)

there are few downside:
- remap between relative paths is expanded under the place of node, not under the place of remap tag.
  for example, in `<remap from="~sub/field" .../>`, "~" refers to any node name in the affect region.
  a relative path remap outside the node scope may cause unexpected result.
  
- to remap a topic, you need to know which part is node namespace and which part is topic path,
  even though they are the same for connection.
  for example, a node (`/ns/node_name`) with topic (`sub/field`)
  is different from, a node (`/ns/sub/node_name`) with topic (`field`), since:
  - `<remap from="sub/field" .../>` only works on the first case;
  - `<remap from="field" .../>` only works on the second case;
  - `<remap from="/ns/sub/field" .../>` works on both cases.
  
- since namespacing a include file will change the full path of topics,
  the only reliable way to make a launch file with remaps is using relative path remapping,
  and it is aware of the node, you better to put remap into each node.
  
- remaps won't apply to topics under the namespace.
  `<remap from="sub" .../>` don't apply to topic `sub/topic`.
  to remap a series of topics, the only way is remap one by one.
  if some topics are added in the future, you need to add corresponding remaps manually.
  the only advantage of organizing topics by namespace is more pleasing.
  
  however, if you remap topic `camera/image_raw`, image_transport will automatically
  remap related topics (`camera/camera_info`, `camera/image_raw/compressed`, etc.) for you.
  this is done by programmatically detecting remaps and dealing with them accordingly.
  in other words, this is custom magic; there is no universal way to do it.

- remaps can sometimes be chained together, and sometimes it can not.
  ```xml
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  ```
  will make topic `a` -> `c`, order is unrelated.
  but for three steps case,
  ```xml
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  <remap from="c" to="d"/>
  ```
  still map topic `a` to `c` instead of `d`.
  
  `rospy.resolve_name('a')` only maps once, so we got `'b'`.
  loop is valid somehow
  ```xml
  <remap from="a" to="b"/>
  <remap from="b" to="a"/>
  ```
  but I don't know where it remaps to finally.
  for ambiguous remapping
  ```xml
  <remap from="a" to="b"/>
  <remap from="a" to="c"/>
  ```
  the later one wins.
  parameters can be remapped too, however, they will not chain together.
  ```xml
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  ```
  will make parameter `a` -> `b`.
  
the biggest mistake is the expansion timing.
in monolaunch, remaps are always expand under the current scope,
you can confidently inspect which path will be remapped to where,
and no need to know which part is namespace and which part is topic path.
we still don't recommand you to use absolute path remapping.
we will chain the mapping to fix `rospy.resolve_name`, and will detect the looping problem.


## Param
the original mechanism of `<param>` is:
- outside the private scope of node,
  absolute names aren't expanded, and relative names are expanded by prepending current namespace:
  ```xml
  <group ns="/current/ns">
    <param name="sub/field" .../>
  </group>
  ```
  becomes
  ```xml
  <param name="/current/ns/sub/field" .../>
  ```
- inside the private scope of node,
  it always prepend with current private namespace:
  ```xml
  <node ns="/current/ns" name="node_name" ...>
    <param name="/sub/field" .../>
  </node>
  ```
  becomes
  ```xml
  <param name="/current/ns/node_name/sub/field" .../>
  ```
- if param name prefix with "~", it will apply to every nodes after this tag in current scope:
  ```xml
  <param name="~sub/field" .../>
  <node ns="/current/ns" name="node_name" .../>
  ```
  becomes
  ```xml
  <node ns="/current/ns" name="node_name" ...>
    <param name="sub/field" .../>
  </node>
  ```
- param with yaml type will replace all contents directly:
  ```xml
  <param name="/a/b" type="int" value="1"/>
  <param name="/a" type="yaml" value="{c: 2}"/>
  ```
  roslaunch shows confusing information
  ```
  PARAMETERS
   * /a/b: 1
   * /a: {'c': 2}
  ```
  `rosparam get /a` returns `{c: 2}` instead of `{b: 1, c: 2}`.
  this is different from `rosparam set`:
  ```bash
  rosparam set /a/b 1
  rosparam set /a '{c: 2}'
  rosparam get /a # => {b: 1, c: 2}
  ```
  after swapping positions, you will got `WARNING: parameter [/a/b] conflicts with parent parameter [/a]`,
  and `rosparam get /a` returns `{b: 1, c: 2}`.

even if the param name and the remap name are in the same world, they have different rules.
- param names outside the node are expanded under current scope;
  remap names outside the node are expanded under the node it applied to.
- param names prefixed with "~" will be applied to later nodes;
  remap names prefixed with "~" will be expanded under private namespace.
- param names inside the node are always expanded under private namespace;
  remap names inside the node has no different from the outside.
- who the fuck design those rules?

in monolaunch, names are always expanded under current namespace
(or current private namespace if prefixed with "~").
noting that `~/field` and `~field` are the same in monolaunch, just replace `~` with `/ns/node_name/`.
there is no way to apply a param to every nodes in the affect region, does anyone actually need this?

to set/load param in monolaunch, use set_param and load_param, they can be nested structure, for example:
```python
set_param({
    "nested": {
        "field_1": "nested",
        "field_2": "fields",
        "field_3": "are",
        "field_4": "valid",
    },
    "path/to/field": "folded path is also valid",
    "path/to": {
        "another/field": "nested folded path? of cause!",
        "": "empty path? fair enough~",
    },
    "~": {
        "field": "yes, this is equivalent to ~field",
    },
    "array/0/x": "number will be treated as indexing, so this refer to curr_param.array[0].x",
})
```

in load_param, its values are treated as file paths to load:
```python
load_param({"another/path": "file/path/to/subconfig.yaml#/sub/field"})
```
this will load `file/path/to/subconfig.yaml`, take field `/sub/field`, and put into `another/path`

we actually do not set/load param via rosparam command,
but aggregate them into a single param file using !include and !merge,
them resolve it before launch.

source yaml files, resolved yaml file and ros parameter server can be synchronized dynamically.
to use this function, user need to launch param_loader on launcher machine:
```python
with node(pkg="monolaunch", type="param_loader.py", ...):
    pass
```


## Logger
the ros logging system consists of two parts: rosout mechanism and language specific API.

rosout mechanism is simple, it is just a topic that accept logging messages.
each logging message contain message content, location, level and node name (no logger name).
but the functions to actually log messages differ from languages,
they depend on logging system of each language:
roscpp uses log4cxx, rospy uses native logging system.

and that is a problem, because now we need multiple config files for different languages.
lucky, log4cxx and python logging both use hierarchical logging framework,
where the hierarchy of loggers allows child loggers to override configuration.

for roscpp, full logger name of `ROS_XXX(...)` is `"ros.{package_name}"`,
and `ROS_XXX_NAMED(...)` append another name after it;
for rospy, full logger name is the `"rosout.{logger_name}"`,
where `logger_name` is the argument in `rospy.logxxx(..., logger_name=...)`.
(yes, it is inconsistent!)

for example, `ROS_INFO("sth")` in package `planner` will log to `ros.planner`,
and `ROS_INFO_NAMED("sub", "sth")` will log to `ros.planner.sub`;
`rospy.loginfo("sth")` will log to `rosout`,
and `rospy.loginfo("sth", logger_name="controller")` will log to `rosout.controller`,
no matter what package it is in.
(inconsistent again!)

logger settings for API part can be setup by environment variables
`ROSCONSOLE_CONFIG_FILE` and `ROS_PYTHON_LOG_CONFIG_FILE` initially,
and there is no universal method for combining logger settings.
because they only accept files, management is cumbersome in a multi-machine environment.

we provide some methods to setup logger for both roscpp and rospy at once.
the logger settings apply to all nodes in the scope,
and can be partially overrided by logger settings in subscope.

in above example, you can configure their logging levels by:
```yaml
ros.planner: DEBUG
ros.planner.sub: DEBUG
rosout.controller: DEBUG
```
we will generate config files for each node and configure environment variables.


## Machine
the original machanism of `<machine default="true">` simply sets to default globally,
regardless of which scope/namespace/include it is located in
(see: https://github.com/ros/ros_comm/issues/1884).

in monolaunch, you can use `with machine(...)` to set default machine **in this scope**.
to specify the machine the node run on, just use it as context manager:
```python
with remote_machine:
    with node(name="remote_node"):
        pass
```
just like `<machine>` tag, you can call `machine(...)` with explicit arguments (user, password, address, env_loader),
or use machine scheme url, which is in the form: `machine://usr:psd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2`.

there are three roles for launching nodes on multiple machines: launcher, worker and master.
they can locate in different machines, and require some environmental setups:
- launcher:
  `ROS_MASTER_URI` should be set, and it will be passed to the node process.
  `ROS_IP` should be set, that is for launch server.
- worker:
  `ROS_IP` should be set, which is for advertising topics.
  it should be setup by env_loader, invoked by roslaunch.
- master:
  `ROS_IP` should be set, which is for running roscore.
  it must match the address of `ROS_MASTER_URI`.

if `ROS_IP` is not set (`ROS_HOSNAME` will be used then) or `ROS_MASTER_URI` uses hostname,
it is needed to setup `/etc/hosts` for all machines, so that they can find each other.
the benefit of using `ROS_HOSTNAME` is that it is more robust to network changes.
one can configure SSH keys for each workers in launcher's machine,
so that no plain-text password is needed to provide to roslaunch.

in general, `rosrun` just searches up and runs an executable, no matter whether it is a node.
if it is a node and network settings (`ROS_IP` and `ROS_MASTER_URI`) are missing, it will use localhost by default.
that is, if you just want to test your node locally,
you only need to source your `devel/setup.bash`, no network setting is needed.
but for running a multi-machine launch, network settings become necessary.
you need to prepare additional setup files for setting up `ROS_IP` and `ROS_MASTER_URI`,
and put user name/password/address/env loader path into corresponding machine tags,
which is unpleasant.

### worker

to launch nodes remotely, one should write env-loader script on remote machine,
which usually do: source setup script, setup `ROS_IP`.
in the launch file, the env-loader attribute of corresponding machine tag is basically the absolute path to this file.
the network configurations (`ROS_IP`) are scattered across multiple machines, which is inconvenient.
luckly, env-loader scripts can be replaced by the trick: `bash -c '...' --`.
we provide a simplified machine scheme url to solve this problem: `machine://usr@addr/path/to/devel/setup.bash?=setup`,
where env-loader will be expanded to an inline bash script that source `/path/to/devel/setup.bash` and setup `ROS_IP`.
on the worker machine, all you need to do is keep the builds in sync.

### master

by default, roslaunch will start roscore automatically if roscore isn't open,
but if ros master uri refer to a remote machine, roslaunch will wait for roscore.
community says it is recommended to run roscore by yourself instead of relying on roslaunch.
I think running roscore alongside with roslaunch is better than keeping roscore up,
later one make previous parameters interfere with next run, causing awkward bugs.
it would be convenient if it can launch roscore remotely,
so we add master node to the launcher:
```python
with master_machine:
    with master(): # roscore will be launched at master_machine
        pass
```
it will be translated into `<master machine="..."/>`,
which is a removed tag and will be skipped by roslaunch.
we write a prefix script `with_roscore.py` for command `roslaunch generated_launcher.launch ...`,
which will parse master tag and launch roscore remotely.
normally it is impossible to launch roscore as a node from roslaunch itself,
but I use an absurd technique to achieve it:
just use `roslaunch` to launch generated launch file, it will be relaunched with prefix.
note that the launch file must directly after `roslaunch`,
and `ROS_MASTER_URI` must be unset/untouched/local,
and roscore is not running.

### launcher

for two nodes with remote machine tags which only differ from env-loader,
they will be prefixed with coorresponding env-loader.
however, when the user and address of the machine of a node is equivalent to current user and localhost,
it will be executed directly without prefixing with env-loader,
otherwise it may cause some weird bugs due to the difference between composing commands and running commands.

the awkward part is, we have to configure `ROS_IP`, `ROS_MASTER_URI` for launcher and local nodes,
env-loader of machine scheme uri for local nodes just doesn't work.
it is difficult to determine whether a machine tag is local at a glance,
and configuring remote and local machine in different ways is dissatisfied.
the best way is let launch file configure its machine setting inside itself, that is,
parse desired machine tag from the launch file, and use it to run the launch file itself.
for the launch file generated by monolaunch, `with_roscore.py` will parse local and master machine
and run roscore and roslaunch with correct env-loader.

however, it also affects how we generate launch file:
generating launch file and running launch file should also be in the same environment.
the best way still is let launcher generator configure its machine setting inside itself,
but now it is too complicated to parse.

in monolaunch, you need to put the machine context manager named local at the top scope:
```python
with machine(..., name="local"): # its address must be local
    ... # all nodes should be put under it
```
which indicates the machine setting of the launcher itself,
when the code execute to this line at the first time,
it will abort and run again with correct env-loader.
user should not put any branch and important operation before it.
you don't need to setup network settings (`ROS_IP` and `ROS_MASTER_URI`) for running launcher script,
they will be configured automatically according to the local machine you set.


## Resource
some ros packages, such as rviz marker, accept resource uri:
`http://<link path>`, `package://<package name>/<relative path>` and `file://<absolute path>`.
where file path is the local file path, which does not always work if node changes the place.
roslaunch doesn't have proper resources management system, user should synchronize resource manually.
technically, one can store/transfer files as binary data through ros parameter server, but it is not a good idea for large data.

in monolaunch, you can use !resource to mark the string scalar as resource uri,
and if it is local file, that is `file://<relative or absolute path>`,
it will be synchronized to proper machine.
or via
```python
set_param({
    "my_resource": Path(my_resource_path), # Path object is recognized as !resource file://...
})
```
the machine is determined by when it is loaded/set into monolaunch
```python
with machine_1:
    set_param({"file": Path(file_path)})  # file will be synced to machine_1
with machine_2:
    load_param({"config": Link.parse(config_link)})  # resources in config_link will be synced to machine_2
```
they should be in put into private namespace of the node requires them.

monoparam will translate them to resource uri, like `ros_home://<relative path to ROS_HOME>`,
so to access those reosurces, just get the resolved resource uri from param server.

resources are synchronized by monoresource, one can run it by yourself via
`python -m monolaunch.monoresource <resolved param file>#/$sync_resources`.


## Launch Prefix
a node process accepts four types of inputs: environmental variables, command arguments, ros parameters and files.
for roslaunch, they can be set by `<env>` tag, `args` attribute of `<node>` tag, `<param>` or `<rosparam>` tag, and resource uri.
launch environmental variables and launch arguments can flow into env/args/param easily,
but values in yaml files cannot flow into env/args directly.
in monolaunch, it is possible via `get_value`, the value will be read during the generation phase.
`load_param` will load yaml file directly, so you can change your yaml file freely.
but `set_param` set parameters during the generation phase, the value will not change if you don't generate again.

things get more complicated when files are involved.
as long as param contains resource paths, monoparam and monoresource will synchronize it to proper place.
but it does not work for env/args (for example, `ROSCONSOLE_CONFIG_FILE`, `rviz -d <config.rviz>`).
if you obtain path via `get_value` and set to env/args,
because it is done during the generation phase, resource uri is not resolved (`file://...` haven't be resolved to `ros_home://...`).
in this case, the best timing to setup env/args is right before launching the node, that is, by launch-prefix.
logger configuration settings are also done in this way.

writting launch-prefix script for each case and managing them accross machines are tedious,
so we provide a decorator to let you write launch prefix in python:
```python
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
the source code will be transferred and executed on the machine of the node,
so launch-prefix function cannot be a closure,
arguments cannot have defaults (it may refer to outer variable),
and can only accept string arguments (arguments are also need to be transferred).
if a module has been imported globally, it will be captured and become a closure.
to fix it, use `__import__` instead in this case.
<!-- TODO: this should be our responsibility -->


## Monoparam
...
