# Security policy

OC Deck handles agent permissions and launches agent processes, so please
report security problems privately.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** (Security tab → *Report a
vulnerability*) on this repository. If that is unavailable, open a minimal public
issue that says only that you have a security report and ask for a private
channel. Please do not include exploit details in public.

Helpful details: the version, the harness involved, and the steps to reproduce.
I aim to acknowledge reports within a week.

## What is in scope

- OC Deck approving, denying or starting something the user did not ask for.
- Secrets (tokens, keys, passwords) reaching a child process, a log, a state
  file or the terminal.
- Reading or writing outside the files OC Deck is documented to use.
- The optional GNOME extensions, plugins and agent browser in this repository.

## Design notes

OC Deck never approves a permission by itself; every approval is a keystroke from
the user. It removes server credentials from the environment of processes it
starts, bounds and validates each file it reads, and refuses to replace launchers
it did not create. The agent browser's grant file records a choice but is **not
a security boundary**; enforce browser access in the browser tool's own
configuration.
