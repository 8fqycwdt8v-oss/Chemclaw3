#!/usr/bin/env bash
# A pid is not an identity, and this file is what makes the lane's bookkeeping mean something.
#
# `processes.sh` and `e2e-full-stack/up.sh` each carried their own three-line `running()`, and both
# read exactly `kill -0 "$(cat "$name.pid")"`. That answers "does *a* process with this number
# exist", never "is it the one this lane started". Measured on 2026-08-28: a `props` server was
# recorded as pid 3422 at 17:56 and killed at 18:07; a bring-up at 20:53 logged
# `props already running (pid 3422)`, skipped starting it, and then died at
# `props did not become ready` — pid 3422 by then belonged to something else. Pid reuse is not
# exotic here: a campaign session churns thousands of short-lived shells through a 4-million-wide
# pid space in two hours, and `soak.sh` reads the same file with `ps -o rss=` to build the front
# door's memory-drift series, so a recycled number does not just skip a start, it puts a stranger's
# resident set into a published measurement.
#
# The fix is to record what the kernel already knows: field 22 of `/proc/<pid>/stat` is the
# process's start time in clock ticks since boot, and it is exactly the discriminator a recycled
# pid cannot forge. It is the same technique the storm's Postgres-bounce check uses on the
# postmaster (`cli/live_storm.py::_postmaster_start_time`) for the same reason — an observation of
# the thing, rather than a number that used to name it.
#
# The pidfile keeps holding the bare pid, because a dozen readers `cat` it to signal or to measure;
# the start time goes in a sibling `<name>.start`. A pidfile with no sibling is treated as *ours*,
# so a lane brought up by an older revision of these scripts degrades to the old behaviour instead
# of refusing to see its own processes.

# The start time of a running process, or nothing when it is gone.
process_start_ticks() {
  local stat
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  # `comm` is parenthesised and may itself contain spaces and `)`, so the fields are counted from
  # after the *last* `)`: overall field 22 is field 20 of that remainder.
  stat="${stat##*) }"
  awk '{print $20}' <<<"$stat"
}

# Record a pid and the identity that outlives its number.
record_pid() {
  local name="$1" pid="$2" run_dir="$3"
  printf '%s' "$pid" >"$run_dir/$name.pid"
  process_start_ticks "$pid" >"$run_dir/$name.start" 2>/dev/null || rm -f "$run_dir/$name.start"
}

# Whether *this lane's* process for `name` is alive — the pid exists **and** is the one recorded.
pidfile_running() {
  # One name per `local`: every argument to the builtin is expanded before it runs, so a later
  # assignment in the same statement cannot see an earlier one. Written as one line this read
  # `pidfile=/probe.pid` with `run_dir` empty, the file never existed, and the predicate answered
  # "not running" for every live process — which would have started a second copy of each server
  # rather than skipping the start. Caught by the four-case test below this file's callers.
  local name="$1" run_dir="$2"
  local pidfile="$run_dir/$name.pid"
  local startfile="$run_dir/$name.start"
  local pid recorded now
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile")"
  kill -0 "$pid" 2>/dev/null || return 1
  # No sibling: a pidfile written before this file existed. Fall back rather than refuse.
  [ -f "$startfile" ] || return 0
  recorded="$(cat "$startfile")"
  now="$(process_start_ticks "$pid")" || return 1
  [ "$recorded" = "$now" ]
}

# Forget a process, both halves, so a stale sibling cannot outlive its pidfile.
forget_pid() {
  rm -f "$2/$1.pid" "$2/$1.start"
}
