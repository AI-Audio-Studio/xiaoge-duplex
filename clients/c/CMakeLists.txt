cmake_minimum_required(VERSION 3.10)
project(xiaoge_client C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# 依赖 libwebsockets(>=4.0)。安装见 README.md。
find_package(libwebsockets CONFIG QUIET)
if(NOT libwebsockets_FOUND)
  # 回退到 pkg-config(Linux 常见)
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(LWS REQUIRED libwebsockets)
endif()

add_library(xiaoge_client STATIC xiaoge_client.c)
target_include_directories(xiaoge_client PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${LWS_INCLUDE_DIRS})

add_executable(xiaoge_demo_file demo_file.c)
target_link_libraries(xiaoge_demo_file xiaoge_client)

if(libwebsockets_FOUND)
  target_link_libraries(xiaoge_client websockets)
else()
  target_link_libraries(xiaoge_client ${LWS_LIBRARIES})
  target_link_directories(xiaoge_client PUBLIC ${LWS_LIBRARY_DIRS})
endif()

# 非 Windows 需链接 pthread
if(NOT WIN32)
  find_package(Threads REQUIRED)
  target_link_libraries(xiaoge_client Threads::Threads)
endif()
