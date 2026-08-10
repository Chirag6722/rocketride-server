# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

cmake_minimum_required(VERSION 3.20 FATAL_ERROR)

#
# rocketride_add_node - Builds a C++ node as a shared library
#
# A C++ node is a shared library the engine loads at startup and initializes
# through its exported initializeNode/deinitializeNode entry points. It imports
# the engine ABI from engineMod rather than linking engLib itself: linking the
# static archive again would give the node its own disconnected copy of every
# registry (Factory, UrlConfig, log levels) and its factories would then be
# invisible to the engine.
#
# Usage, from nodes/src/nodes/<node-dir>/CMakeLists.txt:
#   rocketride_add_node(cppExample)
#
# The target name doubles as the library base name, so `cppExample` produces
# cppExample.dll / libcppExample.so / libcppExample.dylib. That base name is
# what the node's services.json must carry in its "path" field - it is how the
# engine finds the library at runtime.
#
# Arguments:
#   targetName - The CMake target and library base name (camelCase by
#                convention, matching engineMod)
#   ARGN       - Source globs, defaulting to src/*.cpp src/*.hpp src/*.h
#
function(rocketride_add_node targetName)
    if(ARGN)
        set(sourceGlobs ${ARGN})
    else()
        set(sourceGlobs
            ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp
            ${CMAKE_CURRENT_SOURCE_DIR}/src/*.hpp
            ${CMAKE_CURRENT_SOURCE_DIR}/src/*.h)
    endif()

    rocketride_load_sources(targetDeps ${sourceGlobs})

    add_library(${targetName} SHARED ${targetDeps})

    set_target_properties(${targetName} PROPERTIES
        OUTPUT_NAME ${targetName}
        UNITY_BUILD FALSE)

    # Import mode. ROCKETRIDE_CORE_BUILD/JSON_DLL_BUILD are deliberately absent
    # - they are engineMod's export side and would contradict this. Note the
    # engine ABI itself resolves through import thunks rather than dllimport
    # declarations; see ROCKETRIDE_CORE_API in apLib/ap.h for why.
    target_compile_definitions(${targetName} PRIVATE
        ROCKETRIDE_CORE_IMPORT
        JSON_DLL)

    target_link_libraries(${targetName} PRIVATE engineMod)

    # A node compiles engLib's headers, so it needs the usage requirements
    # engLib publishes for them - include paths (its own, plus the JDK, since
    # engLib/headers.h reaches for jni.h) and compile definitions. The defines
    # matter more than they look: _TURN_OFF_PLATFORM_STRING turns off cpprest's
    # U(x) string-literal macro, and without it every `U(...)` in the engine
    # headers - including a placement new of a template parameter named U -
    # silently expands to something else.
    #
    # These are inherited rather than copied because engineMod links engLib
    # PRIVATE, so none of it propagates on its own. Only the *interface* is
    # taken; the archive itself is never linked, per the note above.
    target_include_directories(${targetName} PRIVATE
        $<TARGET_PROPERTY:engLib,INTERFACE_INCLUDE_DIRECTORIES>
        $<TARGET_PROPERTY:apLib,INTERFACE_INCLUDE_DIRECTORIES>
        ${ROCKETRIDE_PACKAGES_DIR}/server/engine-core
        ${VCPKG_INSTALLED_TRIPLET_DIR}/include)

    # engLib's interface definitions only. apLib's are deliberately NOT
    # inherited: they carry ROCKETRIDE_CORE_BUILD/JSON_DLL_BUILD, which would
    # flip this target back into export mode.
    target_compile_definitions(${targetName} PRIVATE
        $<TARGET_PROPERTY:engLib,INTERFACE_COMPILE_DEFINITIONS>)

    # Third-party libraries that apLib/engLib link PRIVATE, so engineMod does
    # not re-export them. None of them holds engine state a node could
    # duplicate, so the node links its own copy - unlike engLib itself.
    #
    # Python and pybind11 are not optional even for a node holding no python
    # code: filter.hpp puts pybind11 types on its virtual interface, and
    # engLib/headers.h instantiates enough of pybind11 to reach the Python C API.
    find_package(Python3 COMPONENTS Development REQUIRED)
    find_package(pybind11 REQUIRED)
    target_link_libraries(${targetName} PRIVATE Python3::Python pybind11::embed)

    find_package(ICU REQUIRED COMPONENTS i18n)
    target_link_libraries(${targetName} PRIVATE ICU::i18n)

    if(ROCKETRIDE_PLAT_WIN)
        find_package(Boost CONFIG REQUIRED COMPONENTS stacktrace_windbg)
        target_link_libraries(${targetName} PRIVATE Boost::stacktrace_windbg)
    endif()

    # A node needs engLib's headers force-included ahead of its own sources,
    # exactly as engLib gets them. Python.h declares `struct _ts`, and apLib
    # defines `_ts` as a macro - whichever is seen second loses, so Python has
    # to be parsed before <apLib/ap.h>. Note this builds a fresh PCH rather
    # than REUSE_FROM engLib: engLib's was compiled with ROCKETRIDE_CORE_BUILD
    # and would flip every import declaration above back to an export.
    target_precompile_headers(${targetName} PRIVATE
        "$<$<COMPILE_LANGUAGE:CXX>:${ROCKETRIDE_PACKAGES_DIR}/server/engine-lib/engLib/headers.h>")

    rocketride_set_common_target_options(${targetName})

    if(ROCKETRIDE_PLAT_WIN)
        # C4275: dll-interface classes deriving from non-dll-interface std
        # bases (ap::json::Exception : std::exception, and friends). JsonCpp's
        # own JSONCPP_DISABLE_DLL_INTERFACE_WARNING does not reach every TU.
        target_compile_options(${targetName} PRIVATE /wd4275)
    endif()

    # The loader resolves a node library relative to the engine executable, so
    # the build has to land it in dist/server/nodes/<node-dir>. The directory
    # name comes from the source tree, which is what the engine derives from
    # the services.json location.
    get_filename_component(nodeDir ${CMAKE_CURRENT_SOURCE_DIR} NAME)
    set(distDir "${ROCKETRIDE_PROJECT_ROOT}/dist/server/nodes/${nodeDir}")

    if(ROCKETRIDE_CMAKE_KIST)
        add_custom_command(TARGET ${targetName} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E make_directory "${distDir}"
            COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:${targetName}> "${distDir}/"
            COMMENT "Copying $<TARGET_FILE_NAME:${targetName}> to dist/server/nodes/${nodeDir}")
    endif()

    # On *nix the node resolves engineMod out of the engine's own directory,
    # two levels up from nodes/<node-dir>
    if(ROCKETRIDE_PLAT_LIN)
        set_target_properties(${targetName} PROPERTIES
            INSTALL_RPATH "\$ORIGIN/../..:\$ORIGIN/../../lib"
            BUILD_WITH_INSTALL_RPATH TRUE)
    elseif(ROCKETRIDE_PLAT_MAC)
        set_target_properties(${targetName} PROPERTIES
            INSTALL_RPATH "@loader_path/../..;@loader_path/../../lib"
            BUILD_WITH_INSTALL_RPATH TRUE)
    endif()
endfunction()
