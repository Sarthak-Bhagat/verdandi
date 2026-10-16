# verdandi

A media scrobbler that reads the id an \*arr wrote into the file path instead of
guessing from the filename. Python, with an offline resolver and no runtime dependencies.

## The name

Verðandi is the Norn of the present in Norse myth: *what is becoming*, from the verb *verða*, to
become. She records what happens as it plays. Her sister Urðr keeps it once it has come to pass,
which is the job of urd, the server this feeds.

It was called scrobd until 2026-10-16. Two reasons for the change:

- [Scrob](https://github.com/ellite/scrob) is an active self-hosted tracker in the same space, and
  "scrobd" was too easy to mistake for it.
- The `d` stood for daemon, and this is no longer one. A watcher starts with each mpv and exits
  when mpv closes its socket.

On the first command after the rename, state under `scrobd` in the XDG config and state
directories is moved to `verdandi`, once and without overwriting anything.

## Pushing to urd

Every session `verdandi watch` records is also sent to urd, if one is configured. Two files
in `~/.config/verdandi/` set it up:

```toml
# config.toml
server = "https://urd.example"
```

and `token`, holding the push token on one line, readable by you alone (`chmod 600`; a looser
file is used with a warning). The token lives in its own file so the config can be shown
without it, and verdandi never prints or logs it.

A row is sent as soon as it is written, and the place in the log moves only when urd has
answered for every line. If urd is unreachable when mpv closes, the watcher stays behind and
retries for up to 12 hours, then gives up until the next watch. More than three unsent watches
puts up one desktop notification. `verdandi push` sends whatever is still unsent, now.

Without `server`, nothing is pushed and nothing else changes.

## Terms

Mine, and free to use — MIT licensed, so do what you like with it.

Support and updates depend entirely on my mood. Issues and pull requests may get a
look, or may not. Nothing here is maintained, supported, or promised.
