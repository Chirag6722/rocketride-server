// =============================================================================
// thread_local storage cannot cross a shared library boundary on Windows
// (dllexport/dllimport on TLS is a hard MSVC restriction), so every module
// needs its own copy of ap::async::ThreadApi::m_thisCtx - the same definition
// apLib/async/api.cpp provides for engineMod.
//
// This means a node's thread context is its own, independent of the engine's.
// Every C++ node needs this translation unit.
// =============================================================================

#include <apLib/ap.h>

namespace ap::async {

thread_local Variant<std::monostate, ThreadCtx *, ThreadCtx>
    ThreadApi::m_thisCtx = {};

}  // namespace ap::async
