if(NOT DEFINED ADB_EXECUTABLE OR ADB_EXECUTABLE STREQUAL "")
  set(ADB_EXECUTABLE adb)
endif()

if(NOT DEFINED TEST_EXE OR TEST_EXE STREQUAL "")
  message(FATAL_ERROR "TEST_EXE is required")
endif()

if(NOT EXISTS "${TEST_EXE}")
  message(FATAL_ERROR "test executable does not exist: ${TEST_EXE}")
endif()

if(NOT DEFINED REMOTE_PATH OR REMOTE_PATH STREQUAL "")
  get_filename_component(_test_name "${TEST_EXE}" NAME)
  set(REMOTE_PATH "/data/local/tmp/${_test_name}")
endif()

function(run_adb_step step_name)
  execute_process(
    COMMAND "${ADB_EXECUTABLE}" ${ARGN}
    RESULT_VARIABLE _result
    OUTPUT_VARIABLE _stdout
    ERROR_VARIABLE _stderr
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_STRIP_TRAILING_WHITESPACE)
  if(NOT "${_stdout}" STREQUAL "")
    message(STATUS "[${step_name}] ${_stdout}")
  endif()
  if(NOT "${_stderr}" STREQUAL "")
    message(STATUS "[${step_name}] ${_stderr}")
  endif()
  if(NOT _result EQUAL 0)
    message(FATAL_ERROR "adb ${step_name} failed with exit code ${_result}")
  endif()
endfunction()

message(STATUS "Running Android test through adb: ${TEST_EXE}")
run_adb_step("wait-for-device" wait-for-device)
run_adb_step("push" push "${TEST_EXE}" "${REMOTE_PATH}")
run_adb_step("chmod" shell chmod 755 "${REMOTE_PATH}")
run_adb_step("shell" shell "${REMOTE_PATH}")
