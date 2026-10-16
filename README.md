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

## Terms

Mine, and free to use — MIT licensed, so do what you like with it.

Support and updates depend entirely on my mood. Issues and pull requests may get a
look, or may not. Nothing here is maintained, supported, or promised.
