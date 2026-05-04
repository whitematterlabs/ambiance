---
name: diagnose-crash
description: Use when a `proc failed` event lands or `/proc/<slug>/status` is `failed`, to classify cause as transient vs structural before deciding to restart or surface.
---

# Diagnose a crashed proc

Goal: classify in under a minute. Output is a one-line cause + a
decision (`restart` | `surface`).

## Procedure

1. `tail -n 80 /proc/<slug>/log.md` — find the last traceback.
2. Identify the **innermost exception** type and message.
3. Classify:

   | Signal | Class | Decision |
   |---|---|---|
   | `sqlite3.OperationalError: database is locked` | transient | restart |
   | `BlockingIOError`, `FileLockError` | transient | restart |
   | `requests.ConnectionError` / DNS fail | transient | restart |
   | `ImportError`, `ModuleNotFoundError` | structural | surface |
   | `AttributeError` / `KeyError` on payload field | structural | surface |
   | `PermissionError` on a path you can't fix | structural | surface |
   | Same exception 3+ times after restart | looping | surface |

4. Cross-check `/sys/drivers/<slug>/` if relevant — a corrupt cursor
   file can present as a structural error but be fixed by deleting
   the cursor (operator decision; surface unless obvious).

5. Log to `/proc/root/log.md`:
   ```
   [HH:MM] <slug> failed: <innermost-exception> — <decision>
   ```

## Hand-off

- `restart` → invoke skill `restart-driver`.
- `surface` → append one line to
  `/var/spool/communication/messages/me/1/<today>.md` naming the
  exception and the file path of the relevant log.

## Anti-pattern

Do not paste full tracebacks into the operator's inbox. One line.
The traceback is in `/proc/<slug>/log.md` if they need it.
