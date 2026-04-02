cmake_minimum_required(VERSION 3.22)
project(opusjni)

# libopus.so liegt in jniLibs/arm64-v8a – als imported library einbinden
# CMAKE_CURRENT_SOURCE_DIR = .../app/src/main/cpp
set(OPUS_SO "${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libopus.so")

message(STATUS "libopus.so path: ${OPUS_SO}")

if(NOT EXISTS "${OPUS_SO}")
    message(FATAL_ERROR "libopus.so not found at: ${OPUS_SO}")
endif()

add_library(opus SHARED IMPORTED)
set_target_properties(opus PROPERTIES IMPORTED_LOCATION "${OPUS_SO}")

add_library(opusjni SHARED opusjni.c)
target_include_directories(opusjni PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(opusjni opus log)
