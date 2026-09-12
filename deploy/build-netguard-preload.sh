#!/usr/bin/env sh
# Compile the egress interposer (`src/chemclaw/core/netguard_preload.c`) into the shared object
# `LD_PRELOAD` names.
#
# **One declaration of the flags, because there are two callers.** `deploy/Containerfile` builds it
# into the image, and `tests/test_netguard_preload.py` builds it to drive the four-arm measurement.
# Writing `gcc …` twice is the shape this repository keeps finding: the test would then prove a
# binary the image does not ship. This script is the shared half, and the test invokes *it* rather
# than reproducing it.
#
#   -O2 -Wall -Wextra -Werror  the interposer runs in every process of the deployment; a warning
#                              here is a fault in the layer that is supposed to be the backstop.
#   -fPIC -shared              it is loaded into somebody else's address space.
#   -ldl                       `dlsym(RTLD_NEXT, …)` is how the real libc entry point is reached.
#
# Usage: build-netguard-preload.sh <source.c> <output.so>
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <source.c> <output.so>" >&2
    exit 64
fi

# The output directory is created here rather than by either caller, because the two callers
# disagree about whether it exists and only one of them said so. The image builds into `/app/lib`,
# which nothing in `deploy/Containerfile` had created -- `ld` answers that with
# "cannot open output file ... No such file or directory" and the whole build fails. The suite
# builds into a temp root that always exists, so it could not have caught it, and `docker build`
# does not run in the sandbox this was written in (a pre-existing TLS failure against the Red Hat
# CDN, in the `dnf -y update` line above it). CI caught it. Putting the `mkdir` in the shared
# script means neither caller has to remember, which is the same argument that put the flags here.
mkdir -p "$(dirname "$2")"

exec "${CC:-gcc}" -shared -fPIC -O2 -Wall -Wextra -Werror -o "$2" "$1" -ldl
