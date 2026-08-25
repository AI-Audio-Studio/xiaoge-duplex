cmake_minimum_required(VERSION 3.10)
project(xiaoge_client C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# 依赖 libwebsockets(>=4.0)。安装见 README.md。
find_package(libwebsockets CONFIG QUIET)
if(TARGET websockets)
  set(XIAOGE_LWS_TARGET websockets)
  set(XIAOGE_LWS_INCLUDE_DIRS ${LIBWEBSOCKETS_INCLUDE_DIRS})
  set(XIAOGE_LWS_DEP_LIBS ${LIBWEBSOCKETS_DEP_LIBS})
else()
  # 回退到 pkg-config(Linux 常见)
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(LWS REQUIRED libwebsockets)
  set(XIAOGE_LWS_INCLUDE_DIRS ${LWS_INCLUDE_DIRS})
  set(XIAOGE_LWS_LIBRARIES ${LWS_LIBRARIES})
  set(XIAOGE_LWS_LIBRARY_DIRS ${LWS_LIBRARY_DIRS})
endif()

find_package(CURL REQUIRED)

add_library(xiaoge_client STATIC xiaoge_client.c)
target_include_directories(xiaoge_client PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${XIAOGE_LWS_INCLUDE_DIRS})

add_executable(xiaoge_demo_file demo_file.c)
target_link_libraries(xiaoge_demo_file xiaoge_client CURL::libcurl)

if(WIN32)
  add_executable(xiaoge_demo_mic_win demo_mic_win.c)
  target_link_libraries(xiaoge_demo_mic_win xiaoge_client CURL::libcurl winmm)
endif()

add_executable(xiaoge_c_codec_test test_codec.c xiaoge_client.c)
target_include_directories(xiaoge_c_codec_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR} ${XIAOGE_LWS_INCLUDE_DIRS})
target_compile_definitions(xiaoge_c_codec_test PRIVATE XIAOGE_ENABLE_TEST_HOOKS)

if(XIAOGE_LWS_TARGET)
  target_link_libraries(xiaoge_client ${XIAOGE_LWS_TARGET} ${XIAOGE_LWS_DEP_LIBS})
  target_link_libraries(xiaoge_c_codec_test ${XIAOGE_LWS_TARGET} ${XIAOGE_LWS_DEP_LIBS})
else()
  target_link_libraries(xiaoge_client ${XIAOGE_LWS_LIBRARIES})
  target_link_libraries(xiaoge_c_codec_test ${XIAOGE_LWS_LIBRARIES})
  target_link_directories(xiaoge_client PUBLIC ${XIAOGE_LWS_LIBRARY_DIRS})
  target_link_directories(xiaoge_c_codec_test PRIVATE ${XIAOGE_LWS_LIBRARY_DIRS})
endif()

# 非 Windows 需链接 pthread
if(NOT WIN32)
  find_package(Threads REQUIRED)
  target_link_libraries(xiaoge_client Threads::Threads)
  target_link_libraries(xiaoge_c_codec_test Threads::Threads)
endif()

enable_testing()
if(CMAKE_CROSSCOMPILING AND ANDROID)
  find_program(ADB_EXECUTABLE
    NAMES adb adb.exe
    HINTS "$ENV{ANDROID_HOME}/platform-tools" "$ENV{ANDROID_SDK_ROOT}/platform-tools")
  if(NOT ADB_EXECUTABLE)
    set(ADB_EXECUTABLE adb)
  endif()
  add_test(NAME xiaoge_c_codec_test
    COMMAND ${CMAKE_COMMAND}
      "-DADB_EXECUTABLE=${ADB_EXECUTABLE}"
      "-DTEST_EXE=$<TARGET_FILE:xiaoge_c_codec_test>"
      "-DREMOTE_PATH=/data/local/tmp/xiaoge_c_codec_test"
      -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/run_adb_test.cmake")
  set_tests_properties(xiaoge_c_codec_test PROPERTIES
    LABELS "android;adb"
    TIMEOUT 30)
else()
  add_test(NAME xiaoge_c_codec_test COMMAND $<TARGET_FILE:xiaoge_c_codec_test>)
endif()
