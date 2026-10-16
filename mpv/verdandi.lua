-- Starts `verdandi watch` beside this mpv, on this mpv's own IPC socket.
--
-- This is how verdandi is "always running": no resident service and no systemd
-- unit. Every mpv starts its own watcher, and the watcher exits when mpv closes
-- the socket, so nothing runs while nothing is playing.
--
-- --script-opts=verdandi-enabled=no switches it off, so a test run of a real mpv
-- does not record a session.

local options = { enabled = true }
require("mp.options").read_options(options, "verdandi")

-- $1 is the socket. stderr goes to a file because a detached child's stderr is
-- discarded, and that is where `verdandi watch` reports a failed write, with the
-- whole session row.
--
-- ~/.local/bin, where `uv tool install` puts verdandi, is APPENDED to PATH: an
-- mpv opened from the desktop inherits the session's PATH, which lacks it, and
-- appending keeps whatever `verdandi` PATH already finds the one that runs.
local PATH = 'PATH="$PATH:$HOME/.local/bin" '
local LAUNCH = 'd="${XDG_STATE_HOME:-$HOME/.local/state}/verdandi"; mkdir -p "$d"; '
    .. PATH
    .. 'exec verdandi watch --socket "$1" 2>>"$d/watch.stderr"'

-- Say it where he will see it. A watcher that never starts records nothing, and
-- the only previous sign was a log line nobody reads -- which is exactly how
-- trakt-scrobbler went unnoticed for six days (vault: mpv.md, the IPC socket).
--
-- What this CANNOT see: a watcher that starts and then dies. It is detached, so
-- mpv gets no exit status from it, and its own stderr goes to watch.stderr.
local function fail(what, notify)
    mp.msg.error("verdandi: " .. what)
    mp.osd_message("verdandi: " .. what, 5)
    if notify then
        mp.command_native_async({
            name = "subprocess",
            -- Normal urgency, not critical: a critical notification stays on
            -- screen until dismissed, and this fires once per mpv, so an
            -- uninstalled verdandi would pile them up. The OSD line carries
            -- the urgency; this is here for an mpv started from the desktop,
            -- where nobody sees the console.
            args = { "notify-send", "-a", "verdandi", "verdandi", what },
            detach = true,
            playback_only = false,
        }, function() end)
    end
end

-- Resolved with the same PATH the launcher uses, so the answer is the one that
-- matters. A detached `exec` of a missing binary fails inside the child, where
-- mpv cannot see it, so this has to be asked before launching rather than after.
local function installed()
    local probe = mp.command_native({
        name = "subprocess",
        args = { "sh", "-c", PATH .. "command -v verdandi >/dev/null" },
        playback_only = false,
    })
    return probe.status == 0
end

local function on_first_file()
    -- Once per mpv, not once per file: mediactl plays a whole folder in one mpv,
    -- and one watcher already follows every file over the one socket.
    mp.unregister_event(on_first_file)

    -- Read here rather than at script load: discord.lua re-sets
    -- input-ipc-server while the scripts load, so the path is final only now.
    local socket = mp.get_property("input-ipc-server", "")
    if socket == "" then
        fail("no input-ipc-server, so this play is not being recorded", false)
        return
    end
    socket = mp.command_native({ "expand-path", socket })

    if not installed() then
        fail("not installed or not on PATH, so nothing is being recorded", true)
        return
    end

    mp.command_native_async({
        name = "subprocess",
        args = { "sh", "-c", LAUNCH, "verdandi.lua", socket },
        -- The watcher records the last file when mpv closes the socket on its
        -- way out, so it must outlive mpv: a detached process is one mpv
        -- neither waits on nor kills.
        detach = true,
        -- On by default, playback_only kills the process when the first file
        -- ends, and the watcher is there for every file after it too.
        playback_only = false,
    }, function(ok, _, err)
        if not ok then
            fail("the watcher would not start: " .. tostring(err), true)
        end
    end)
end

if options.enabled then
    mp.register_event("file-loaded", on_first_file)
end
