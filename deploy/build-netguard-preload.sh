#!/usr/bin/env sh
# Compile the egress interposer (`src/chemclaw/core/netguard_preload.c`) into the shared object
# `LD_PRELOAD` names.
#
# **One declaration of the flags, because there are two callers.** `deploy/Containerfile` builds it
# into the image, and `tests/test_netguard_preload.py` builds it to drive the three-arm measurement.
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

exec "${CC:-gcc}" -shared -fPIC -O2 -Wall -Wextra -Werror -o "$2" "$1" -ldl
