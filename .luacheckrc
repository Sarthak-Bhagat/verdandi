-- luacheck config for the mpv scripts in this repo.
--
-- mpv runs scripts under LuaJIT (Lua 5.1 + its extensions), so that is the
-- standard library to check against. `mp` is the one global mpv itself
-- provides; it is read-only, so assigning to it or to a field of it still warns.
std = "luajit"
read_globals = { "mp" }
exclude_files = { ".venv/" }
