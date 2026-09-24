# Mechanisms

this document explains all the mechanisms in detail, including how bad the launch xml format is, and why my design like this way.

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
however, we don't collect set/load param inside included launch files.
we load aggreated param at the end of generated launch file,
so that you can overwrite parameters outside included launch files.
that means set/load param only preserve order up to include.
it's recommended to convert all your launch files into monolaunch scripts.

source yaml files, resolved yaml file and ros parameter server can be synchronized dynamically.
to use this function, user need to launch param_loader on launcher machine:
```python
with node(pkg="monolaunch", type="param_loader.py", ...):
    pass
```
<!-- TODO: fix me -->


## get_value
one can also read data from yaml file.
note that parameters cannot be read directly after `set_param`/`load_param` in monolaunch,
because the actual parameters are set during the launch phase.
however, you can extract data from a yaml file and then set it as a parameter
```python
subfield = get_value("file/path/to/userconfig.yaml#/sub/field", int)
set_param({ "sub/field": subfield + 1 })
```
where `int` ensures the type of `subfield`.
with `get_value`, control flow of launching can be changed from user configuration.
retrieving the value of the !resource field will give you the string of the original resource uri,
but the local file resource uri will be rewritten on the parameter server,
so that remote nodes can access the resources synchronized by monolaunch.
see below for detail.

accessing the value along two pointers and one combined pointer should be the same.
for example, `get_value("camera_config.yaml#/camera_info")["intrinsic"]["fx"]`
should equal to `get_value("camera_config.yaml#/camera_info/intrinsic/fx")`.
note that getting value from the root will resolve all contents in depth
```python
userconfig = get_value("file/path/to/userconfig.yaml", dict)
set_param({ "sub/field": userconfig["sub"]["field"] + 1 })
```
this is not a good pattern, since now the code potentially depends on all configurations.
it is recommended to use `load_param` to load specific part from the link,
so that changed configuration take effect after generation phase.
if you only need property of structure, use special field:
```python
keys = get_value("file/path/to/userconfig.yaml#/sub/struct/__keys__", list) # get keys of map
length = get_value("file/path/to/userconfig.yaml#/sub/list/__len__", int) # get length of seq
typename = get_value("file/path/to/userconfig.yaml#/sub/data/__class__", str) # get type name of field
```

we provide json pointer class, it can be extended like `pathlib.Path`
```python
userconfig = PathWithJPointer.parse("file/path/to/userconfig.yaml")
subfield = userconfig / "sub" / "field"
set_param({ "sub/field": get_value(subfield, int) })
load_param({ "sub/field": subfield })
```
furthermore, it can be attached with json shema
```python
userconfig = PathWithJPointer.parse("file/path/to/userconfig.yaml").with_schema("file/path/to/user.schema.json")
subfield = userconfig / "sub" / "field"
set_param({ "sub/field": get_value(subfield, int) })
load_param({ "sub/field": subfield })
```
now when you `get_value` and `load_param`, the value will be checked by schema.
note that due to presence of type information,
correct default value will be contructed if the field is absence or null.
moreover, scalar default value described by schema will be used.
you should define default values in the json schema, instead of writing the magic number directly in the code.
that means schema is not just an annotation, but will affect the behavior of `get_value`.
see below for detail.


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


# Monoparam
configurations are stored and managed as yaml files.
yaml is just a human readable format of json object, but supports expandable tag semantics.
you can use the `!include` tag to include multiple yaml files,
which allows you to separate configurations and brings great benefits.
`!merge` tag is also supported, so that one can overlay included yaml file instead of modifying it directly.
it allows you to modify/extend part of existing configuration without touching it.
to manage resources, we invent `!resource` tag, so that following url can be recognized and modified accordingly.

## Include
`!include` should be followed by the path of yaml file to be included.
for example,

```yaml
# file: base.yaml
a: 1
b: 2
```
```yaml
# file: config.yaml
base: !include base.yaml
```
resolve to:
```yaml
base:
  a: 1
  b: 2
```
where the path can be an absolute path or a path relative to the file containing the statement.
it also can be appended with a json pointer
```yaml
base: !include base.yaml#/key/2/item
```
note that the path is relative to the absolute path of this file,
so you can safely symlink this file to any location.
for example, assume you have a global network setting
```yaml
# file: network.yaml
local: !include local.yaml
worker1: !include worker1.yaml
worker2: !include worker2.yaml
```
it includes multiple setting files under the same directory.
you have another configuration file for certain application
```yaml
# file: src/my_package/config/setting.yaml
my_package:
  ...
network: !include network.yaml
```
you don't need to copy all network settings here;
just create a symlink to the network setting file in the root directory:
```
# file: src/my_package/config/network.yaml (symlink)
../../../network.yaml
```
the resolver will look for the files `local.yaml`, `worker1.yaml`, and `worker2.yaml`
next to the absolute path of `src/my_package/config/network.yaml`,
not under `src/my_package/config`.

the functionality of `!include` allows you to manage configuration structurally.
our recommended design is:
separate configurations by purpose, and `!include` all configurations into one yaml file.
let the entry point of launch script only accept that file as input,
launch script then distributes each part to private namespace of each node.
monolaunch will resolve all redistributed configurations into another yaml file.

the advantages of this design are obvious:
- smaller configuration files are more readable and reusable.
- single input yaml file is easy to search; single resolved yaml file is easy to debug.
- configurations should be separated based on purpose (single responsibility principle).
- separation of user configurations and launcher configurations.  
  the reason for designing user configurations first and then redistributing them to establish launcher configurations,
  rather than directly asking users to provide launcher configurations, is because:
  - launcher configurations are usually not suitable for user.
  - the nested structure of launcher configurations is related to the implementation of the launcher,
    which is not an issue that users should care about.
  - redistribution makes single source of truth possible.

based on purposes, configurations can be categorized into:
- calibration data:  
  the calibration of sensors, such as intrinsics/extriniscs of stereo camera, imu-cam extrinsics,
  should remain unchanged unless the mechanical structures or internal components change.
  also calibration data are usually produced by another program,
  which should be managed in a separated directory,
  instead of manually copying into your configuration file.
- algorithm parameters:  
  algorithms typically contain many parameters (such as PID, resolution, fps, etc)
  that need to be adjusted depending on the specific circumstances.
  tuning parameters often require some relevant knowledge, which frontend users cannot master.
  you also don't want to touch it once it's tuned to the optimal state.
  separating them from other configurations can also hide their complexity.
  aside from their purposes, the algorithm parameters are similar to calibration data;
  the only difference is whether there is a systematic method to determining these parameters.
- feature settings:  
  these are about general settings of features, such as use which algorithm,
  connect to which sensors, frame ID name, publish to which topics,
  delay time of some operations, etc.
  they usually can be configured by strings or integers, which are easy to adjust for frontend users.
- environmental settings:  
  some configurations are necessarily related to the machine/device you are using,
  and cannot be ported to the same application on different machines.
  such as network/authentication settings, sensor's device ID, buildspace path, etc.


## Merge
`!merge` should be followed by the sequence of objects to be merged.
for example,
```yaml
config: !merge
  - timeout: 10
    retries: 3
  - timeout: 30
```
resolve to:
```yaml
config:
  timeout: 30
  retries: 3
```
the nested structures will be merged in depth,
```yaml
config: !merge
  - optimizer:
      use_BA: false
      buffer_size: 10
    controller:
      timeout: 10
      retries: 3
  - optimizer:
      use_BA: true
    controller:
      timeout: 30
```
resolve to:
```yaml
config:
  optimizer:
    use_BA: true
    buffer_size: 10
  controller:
    timeout: 30
    retries: 3
```
rules are simple:
- null <> any = any <> null = any   --  null behaves like empty slot
- scalar <> scalar = later one
- map <> map = union zip with <>
- seq <> seq = zip longest with <>  --  just think of seq as a mapping of consecutive number keys
- non-null type <> another non-null type = later one

command `rosparam load` also has merging behavior, but it is slightly different from !merge.
rosparam cannot access sequence through index, even if sequence contain more than just scalars.
similarly, rosparam treats sequence as a scalar during merge; it just replaces all contents of sequence.
I think rosparam's seq merging rule is better for configuration,
but I stick to current rule since it is more symmetrical.

in our context, `!merge` is used for modifying/extending part of existing configuration without touching original file,
but not any kind of modification can be done via `!merge`.
I think they are the best solution at present.

due to the merging rule, you should not put different type of configuration into the same field,
it will be messed up badly after merging.
you should mimic sum type with product type:
```yaml
flag: "real_camera"
real_camera: {...}
virtual_camera: null
```

merging variable-sized sequence not always make sense.
for example, merging set of flags:
```yaml
!merge
- flags: ["flag1", "flag2"] # turn on flag1 and flag2
- flags: ["flag4"] # turn on flag4
```
doesn't resolve to
```yaml
flags: ["flag1", "flag2", "flag4"] # union
```
or
```yaml
flags: ["flag4"] # override
```
but get
```yaml
flags: ["flag4", "flag2"]
```
the merging rule doesn't make sense here:
the position of an element is meaningless, but it will affect the merge result.
thus, additive set of items cannot be represented under this merging rules.
for rosparam's merging rule, additive behavior still cannot be made,
but overriding full set does make sense in another aspect.
you can use map keys as set instead:
```yaml
!merge
- flag_set:
    flag1: true
    flag2: true
- flag_set:
    flag4: true
```
resolve to correct one
```yaml
flag_set:
  flag1: true
  flag2: true
  flag4: true
```

similarly, it also doesn't make sense for sequence that represents order:
```yaml
!merge
- stereo_depth:
    filter_order: ["decimate", "median", "bilateral"]
    ...
- stereo_depth:
    filter_order: ["bilateral", "decimate"]
    ...
```
however, this is fine for rosparam's merging rule.
you should use single string
```yaml
!merge
- stereo_depth:
    filter_order: "decimate,median,bilateral"
    ...
- stereo_depth:
    filter_order: "bilateral,decimate"
    ...
```

another example, assume you have a set of cameras, which have configurations stored as a sequence
```yaml
- fov: 60
  resolution: [640, 640]
  fps: 30
  type: "mono"
- fov: 57
  resolution: [320, 320]
  fps: 60
  type: "stereo"
```
under our merging rules, it is impossible to remove one of camera, or replace both completely to single camera.
if you use rosparam merging rules, now the problem becomes you cannot override single property of one of camera.
you should use map instead, so that each camera can be referenced with proper name.

a good design principle is: don't use sequence in configuration, always use map.
if you must use seq, then only include scalars.
the only exception is matrices: no one would be happy to write `{m00: 1, m01: 0, m10: 0, m11: 1}` instead of `[[1, 0], [0, 1]]`.


## Resource
`!resource` tag is used for transferring resource across nodes in different machines,
it can be followed by a string in the form:
- file://{path_to_resource}
- package://{pkg_name}/{path_to_resource}
- ros_home://{path_to_resource}

file uri is the file path relative to current location (directory of the file contains this term);
package uri refers to the workspace overlay where this item being read;
ros_home uri refers to the ros home of current runtime when this item being used.

resource uri should be transformed properly after switching carrier, otherwise the meanings may change.
file uri should not be shared across machine since it is local resource;
package uri can be shared across machine as long as they have the same overlay;
ros_home uri is a runtime resource and should be prepared before each run on given machine.

monoparam will rewrite local file uri as ros_home uri,
and collect all synchronization tasks then hand over to monoresource.
this is done by parameter resolver,
that means the path you get during the launch script and in the node are different.
with this mechanism, you can reference local files in your yaml file as a !resource,
and in any node run on any machine, you can read that file through the resolved resource uri.

to make a single resource uri that is accessible on any machine,
a meaningful, generic path prefix is needed, and `$ROS_HOME` is the most suitable.
however, we don't allow environmental variable appear in the path, otherwise the meaning becomes runtime dependent.
ros_home uri is invented for this purpose: it opens a special case for `$ROS_HOME`, and anyone can understand it at a glance.

local resources will be synchronized to the machine when it is loaded/set into monolaunch,
that is,
```python
with machine_1:
    set_param({"file": Path(file_path)})  # file will be synced to machine_1
with machine_2:
    load_param({"config": Link.parse(config_link)})  # resources in config_link will be synced to machine_2
```
to make parameter resolver know that information (synchronization tasks are built by parameter resolver),
we also attach some context onto `!include` and `!resource`,
```yaml
file: !resource file://path/to/local.png?runtime_machine=machine://...
config: !include path/to/config.yaml#/sub/field?runtime_machine=machine://...
```
where the query string contains all necessary information for synchronization.
note that the syntax is slightly different from the standard url.
the context will be inherited during resolving inclusion,
so all contained resources will be synchronized to that machine.
it is just for resolver, it should be eliminated after resolving.

we will warn the case of the synchronized resource uri being assigned to
the private namespace of a node run on the different machine.
best practice is to always set up resources within the private scope of the nodes that need these files:
```python
with machine_1:
    with node_1:
        set_param({"file": Path(file_path)})  # file will be synced to the machine of node_1, that is, machine_1
```
even if two nodes use the same resources, they should be configured separately.
we cannot restrict users to using only this method,
because `set_param` and `load_param` can also be used elsewhere,
and we cannot check it without look into included yaml files.


## Schema
yaml files can be annotated directly with schema.
simply add `$schema: path/to/your.schema.json` to the root level in the yaml file,
and lsp should be able to recognize it, but it probably cannot understand tags `!include`/`!merge`/`!resource`.
to use schema based type checking in monolaunch, you should use `PathWithJPointer.with_schema`, see above.

we only support a specific form of json schema:
```
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
```
each `<schema>` (except for ref) can have additional properties for metadata:
```
<metadata>  = {
                ("description": <string>,)?
                ("oneOf": [ ({ "const": <json> },)* ],)?
                ("default": <json>,)?
                ("minimum": <number>,)?
                ("maximum": <number>,)?
              }
```
struct and dict are for map, but struct must have fixed numbers of keys,
and you cannot mix struct and dict;
only one case is allowed in anyOf, it is just for wrapping up a schema so that additional metadata can be attached.

we allow some fields in yaml file to be absence,
which will then be filled in with proper default values,
so that we don't need to write down everything.
such mechanism required an explicit structure definition.
different types have different default value resolving rules
- seq: `[]`
- dict: `{}`
- struct: `{ <key1>: <default value of this field>, ... }`
- null: `null`
- scalar: default value described by metadata, or zero value of given type
- enum: default value described by metadata, or the first value of enum
- wrap: default value of underlying type
- ref: default value of underlying type
- any: `null`
note that dict and struct both are encoded into map, but have different defaulting behavior.
similar to the merging rules, we treat null as an empty slot:
value `null` annotated with given schema will be resolved to the corresponding default value.
for example, `{ x: 1.0, y: 2.0, z: null }` just behaves like `{ x: 1.0, y: 2.0 }`,
which is resolved to `{ x: 1.0, y: 2.0, z: 0.0 }` for point type.

even though parameters can be grouped up structurally,
ros only provides unstructured paramater getting/setting methods,
you can access any field as any type with random default value anywhere and anytime,
which is flexible but terrible to manage.
to find the full parameter list of a node required, you have to search over entire codebase.
the best way is to define parameter type once, load once and use everywhere.
parameter types can be defined in separate place,
and be aggregated into one giant parameter group at the entry of the node,
then you only need to load into this parameter group once,
and all possible configuration errors will be revealed early.

json schema offers similar benefits.
it allows the advantages of type checking to be realized in data format and resolver.
with json pointer attached with schema, content will be type-checked during `get_value` and `load_param`.
compared with non-typed version, you have to deal with absence of `get_value` and assign proper default value,
which make the parsing logic be scattered throughout the launch script.
`with_schema` provides a way to setup schema of full config file once and use everywhere.

monolaunch doesn't utilize python's static type checking because it is hard to integrate typing between multiple languages.
on the other hand, with json schema, external types can be included dynamically in any language.
another reason is:
I want to nerf the use of deserialization, otherwise the utility of `load_param` will be greatly reduced.
it is clearly that parsing full configuration directly is easier
```python
my_config = MyConfig.from_json(my_config_link) # read data and statically type checked 
set_param({
    "config": my_config.node_config, # this become easier and statically type checked
})
load_param({
    "config": my_config_link / "node_config", # this is relatively harder and dynamically type checked
})
```
however, `load_param` is the better for the future use:
don't resolve config in the generation phase, include it, so that you can change it before launch phase.
the ease of full deserializing becomes a trap here.
