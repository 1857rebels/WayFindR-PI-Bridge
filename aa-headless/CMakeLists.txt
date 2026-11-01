cmake_minimum_required(VERSION 3.16)
project(aa-headless LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 14)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(AASDK_DIR $ENV{HOME}/aa/aasdk CACHE PATH "aasdk source tree (built)")

find_package(Boost REQUIRED COMPONENTS system log)
find_package(Protobuf REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)
find_path(LIBUSB_INCLUDE_DIR libusb.h PATH_SUFFIXES libusb-1.0 REQUIRED)
find_library(LIBUSB_LIBRARY usb-1.0 REQUIRED)

add_executable(aa-headless main.cpp)
target_compile_definitions(aa-headless PRIVATE BOOST_ALL_DYN_LINK)
target_include_directories(aa-headless PRIVATE
    ${AASDK_DIR}/include ${AASDK_DIR}/build ${LIBUSB_INCLUDE_DIR})
target_link_directories(aa-headless PRIVATE ${AASDK_DIR}/lib)
target_link_libraries(aa-headless PRIVATE
    aasdk aasdk_proto ${Protobuf_LIBRARIES} OpenSSL::SSL OpenSSL::Crypto
    Boost::system Boost::log ${LIBUSB_LIBRARY} Threads::Threads)
# The aasdk libraries ship next to the binary.
target_link_options(aa-headless PRIVATE "-Wl,-rpath-link,${AASDK_DIR}/lib")
set_target_properties(aa-headless PROPERTIES BUILD_RPATH "\$ORIGIN")
