cmake_minimum_required(VERSION 3.0.2)
project(monolaunch)

find_package(catkin REQUIRED COMPONENTS
  rospy
)

catkin_python_setup()

catkin_package()

catkin_install_python(PROGRAMS
  scripts/param_loader.py
  scripts/launch_converter.py
  scripts/exec.sh
  scripts/yl
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
