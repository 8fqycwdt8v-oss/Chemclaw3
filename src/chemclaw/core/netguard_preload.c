/*
 * The compiled half of the egress guard: interpose libc's `connect`, `getaddrinfo`, `sendto` and
 * `sendmsg` so a destination outside the derived allowlist is refused even when the caller is a
 * compiled extension that never touches Python's `socket` module.
 *
 * WHY THIS FILE EXISTS, AND WHY IT IS C.
 *
 * `netguard.py` patches `socket.socket`'s methods and the `socket` module's resolvers. That is the
 * whole of the pure-Python surface and nothing more. Measured on this tree with the allowlist empty
 * and no proxy set, a listener on a non-loopback address: a `grpc.insecure_channel`, the OTLP gRPC
 * span exporter and `temporalio.Client.connect` each reached it with the Python guard's refusal
 * counter flat — seven TCP connections, zero refusals — while a raw `socket.create_connection` to
 * the same address was refused. grpc's C-core and Temporal's Rust sdk-core open their sockets
 * below the interpreter. They are also the two destinations the guard most wants to bound: with
 * `otel_include_sensitive_data` on, the OTLP exporter carries prompts and completions, so a wrong
 * or hostile `CHEMCLAW_OTEL_ENDPOINT` exports them anywhere.
 *
 * An `LD_PRELOAD` interposition is the layer that reaches them, and that was measured rather than
 * assumed before this file was written: driven against a *real* gRPC server (Temporal) over a
 * non-loopback route, grpc's own C-core reported `connect failed: ... error: Operation not
 * permitted` — this interposer's `EPERM` surfacing through the layer Python cannot reach. A probe
 * that cannot observe success proves nothing, so `tests/test_netguard_preload.py` keeps the
 * positive control (the same dial succeeds with no interposer) and the non-destructive control (a
 * loopback dial succeeds with it).
 *
 * WHAT IT DOES NOT COVER, STATED RATHER THAN IMPLIED.
 *
 *   - A **statically linked** binary, or one issuing the syscall directly (`syscall(SYS_connect)`,
 *     a Go binary, an `asm` stub). There is no dynamic symbol to interpose.
 *   - Anything not named below: `sendmmsg`, `io_uring`, a raw `AF_PACKET` socket, `write` on an
 *     already-connected descriptor this guard allowed.
 *   - A process this interposer is not loaded into. It arms from `LD_PRELOAD` in
 *     `deploy/entrypoint.sh`, so every component of the image carries it; the knowledge-sync
 *     containers set their own `command` and deliberately do not (they are `git`, whose remote is
 *     not on the settings object at all).
 *   - **Where** traffic goes once a proxy is in the environment. A proxied dial is a legitimate
 *     connection to the proxy; `netguard.refuse_proxied_egress` is the layer for that shape.
 *
 * The NetworkPolicy remains the layer that takes the network away instead of asking libc nicely.
 * This one catches what it cannot: a sidecar on loopback shares the pod's network namespace, so
 * its traffic never crosses a policy enforcement point.
 *
 * THE ALLOWLIST IS NOT DECLARED HERE.
 *
 * It arrives in `CHEMCLAW_NETGUARD_PRELOAD_ALLOW` as a comma-separated list, written by
 * `deploy/entrypoint.sh` from `chemclaw.cli.egress_preload`, which calls
 * `netguard.derive_allowed` — the *same* function the Python layer arms with. A second hand-written
 * list in C is the defect this family keeps finding, so there is not one. An **absent or empty**
 * variable is not "disabled": it means loopback only. Disabling is done by not preloading the
 * library, which is what `CHEMCLAW_EGRESS_GUARD_ENABLED=false` makes the entrypoint do, so there is
 * one knob rather than two.
 *
 * DNS IS EXEMPT AT THE ADDRESS AND ENFORCED AT THE NAME.
 *
 * glibc's resolver `connect`s a UDP socket to the nameserver in `/etc/resolv.conf`, which is
 * non-loopback in every cluster — refusing it would break every lookup including the allowlisted
 * ones. So a dial to port 53 is permitted **only** when its address is one of the nameservers that
 * file names, read from the same file the resolver reads; a name that is not on the allowlist never
 * gets that far, because `getaddrinfo` refuses it first. That mirrors the chart, which already
 * carries DNS as its own rule rather than inside the destination-scoped one.
 *
 * A **numeric** host passed to `getaddrinfo` is let through and judged at `connect` instead: no
 * query leaves the host for an IP literal, so charging it as a resolver refusal would misreport the
 * event. Nothing escapes either way — `connect` refuses it a moment later — and an operator can
 * tell a blocked lookup from a blocked dial, which is the whole reason the two counters are
 * separate.
 *
 * IMPLEMENTATION NOTES THAT ARE NOT STYLE.
 *
 *   - No mutex and no `stdio`. Both take locks, and a lock held across `fork()` deadlocks the
 *     child on its next call — this process forks (`kg/git_writer.py` shells out to `git`). The
 *     resolved-address table is append-only with atomic slot claims, and a refusal is one `write(2)`
 *     of a buffer filled by `snprintf`.
 *   - Parsed in a `constructor`, which runs single-threaded at load, so there is no lazy-init race
 *     to get wrong.
 *   - The table fails **closed** when full: further resolved addresses are not recorded, so dials
 *     to them are refused, and the overflow is logged once. 1024 entries is far beyond a
 *     deployment's own small set of gateway and infrastructure addresses.
 */

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#define CHEMCLAW_MAX_ALLOWED 256
#define CHEMCLAW_MAX_RESOLVED 1024
#define CHEMCLAW_MAX_NAMESERVERS 8
#define CHEMCLAW_HOST_MAX 256

/* Refusal kinds, mirrored by `netguard_preload.REFUSAL_CONNECT` / `REFUSAL_RESOLVE`. An operator
 * has to be able to tell a blocked dial from a blocked lookup: they are different events with
 * different causes, and one counter covering both cannot say which happened. */
#define CHEMCLAW_REFUSAL_CONNECT 0
#define CHEMCLAW_REFUSAL_RESOLVE 1

static char allowed[CHEMCLAW_MAX_ALLOWED][CHEMCLAW_HOST_MAX];
static unsigned int allowed_count;

static char nameservers[CHEMCLAW_MAX_NAMESERVERS][INET6_ADDRSTRLEN];
static unsigned int nameserver_count;

static char resolved[CHEMCLAW_MAX_RESOLVED][INET6_ADDRSTRLEN];
static unsigned char resolved_ready[CHEMCLAW_MAX_RESOLVED];
static unsigned int resolved_claimed;
static unsigned int overflow_reported;

static unsigned long refused_connect;
static unsigned long refused_resolve;

static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
static ssize_t (*real_sendmsg)(int, const struct msghdr *, int);
static int (*real_getaddrinfo)(const char *, const char *, const struct addrinfo *,
                               struct addrinfo **);

/* ------------------------------------------------------------------ logging */

static void emit(const char *message)
{
    char line[640];
    int n = snprintf(line, sizeof line, "chemclaw-netguard-preload: %s\n", message);
    if (n <= 0) {
        return;
    }
    if ((size_t)n > sizeof line) {
        n = (int)sizeof line;
    }
    ssize_t written = write(2, line, (size_t)n);
    (void)written; /* a refusal must never depend on stderr being writable */
}

/* ------------------------------------------------------------------ parsing */

static void lowercase(char *text)
{
    for (; *text; text++) {
        if (*text >= 'A' && *text <= 'Z') {
            *text = (char)(*text - 'A' + 'a');
        }
    }
}

/* Strip the brackets an IPv6 literal wears in a URL, exactly as `netguard._check` does. */
static void unbracket(char *text)
{
    size_t len = strlen(text);
    if (len >= 2 && text[0] == '[' && text[len - 1] == ']') {
        memmove(text, text + 1, len - 2);
        text[len - 2] = '\0';
    }
}

static void remember_allowed(const char *entry, size_t length)
{
    if (length == 0 || length >= CHEMCLAW_HOST_MAX || allowed_count >= CHEMCLAW_MAX_ALLOWED) {
        return;
    }
    char *slot = allowed[allowed_count];
    memcpy(slot, entry, length);
    slot[length] = '\0';
    lowercase(slot);
    unbracket(slot);
    if (slot[0] != '\0') {
        allowed_count++;
    }
}

static void parse_allowlist(const char *value)
{
    if (value == NULL) {
        return;
    }
    const char *start = value;
    for (const char *cursor = value;; cursor++) {
        if (*cursor == ',' || *cursor == '\0') {
            const char *head = start;
            const char *tail = cursor;
            while (head < tail && (*head == ' ' || *head == '\t')) {
                head++;
            }
            while (tail > head && (tail[-1] == ' ' || tail[-1] == '\t')) {
                tail--;
            }
            remember_allowed(head, (size_t)(tail - head));
            if (*cursor == '\0') {
                return;
            }
            start = cursor + 1;
        }
    }
}

/* The resolver's own addresses, from the file the resolver reads. Hand-parsed rather than taken
 * from `res_init`, because calling into the resolver from a constructor that interposes the
 * resolver is a reentrancy question nobody should have to answer. */
static void parse_nameservers(void)
{
    FILE *handle = fopen("/etc/resolv.conf", "re");
    if (handle == NULL) {
        return;
    }
    char line[512];
    while (fgets(line, sizeof line, handle) != NULL && nameserver_count < CHEMCLAW_MAX_NAMESERVERS) {
        if (strncmp(line, "nameserver", 10) != 0) {
            continue;
        }
        char *cursor = line + 10;
        while (*cursor == ' ' || *cursor == '\t') {
            cursor++;
        }
        char *end = cursor;
        while (*end != '\0' && *end != ' ' && *end != '\t' && *end != '\n' && *end != '\r'
               && *end != '%') {
            end++;
        }
        size_t length = (size_t)(end - cursor);
        if (length > 0 && length < INET6_ADDRSTRLEN) {
            memcpy(nameservers[nameserver_count], cursor, length);
            nameservers[nameserver_count][length] = '\0';
            nameserver_count++;
        }
    }
    fclose(handle);
}

__attribute__((constructor)) static void arm(void)
{
    parse_allowlist(getenv("CHEMCLAW_NETGUARD_PRELOAD_ALLOW"));
    parse_nameservers();
}

/* ------------------------------------------------------------- the allowlist */

static int is_allowed_host(const char *host)
{
    char needle[CHEMCLAW_HOST_MAX];
    size_t length = strlen(host);
    if (length >= sizeof needle) {
        return 0;
    }
    memcpy(needle, host, length + 1);
    lowercase(needle);
    unbracket(needle);
    for (unsigned int i = 0; i < allowed_count; i++) {
        if (strcmp(needle, allowed[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static int is_resolved_address(const char *address)
{
    unsigned int claimed = __atomic_load_n(&resolved_claimed, __ATOMIC_ACQUIRE);
    if (claimed > CHEMCLAW_MAX_RESOLVED) {
        claimed = CHEMCLAW_MAX_RESOLVED;
    }
    for (unsigned int i = 0; i < claimed; i++) {
        if (__atomic_load_n(&resolved_ready[i], __ATOMIC_ACQUIRE)
            && strcmp(address, resolved[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static void remember_resolved(const char *address)
{
    if (is_resolved_address(address) || strlen(address) >= INET6_ADDRSTRLEN) {
        return;
    }
    unsigned int slot = __atomic_fetch_add(&resolved_claimed, 1u, __ATOMIC_SEQ_CST);
    if (slot >= CHEMCLAW_MAX_RESOLVED) {
        if (__atomic_exchange_n(&overflow_reported, 1u, __ATOMIC_SEQ_CST) == 0u) {
            emit("the resolved-address table is full; further resolved addresses are refused");
        }
        return;
    }
    strcpy(resolved[slot], address);
    __atomic_store_n(&resolved_ready[slot], (unsigned char)1, __ATOMIC_RELEASE);
}

static int is_nameserver(const char *address)
{
    for (unsigned int i = 0; i < nameserver_count; i++) {
        if (strcmp(address, nameservers[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

/* Render a socket address as (text, port, is_loopback). Returns 0 when the family carries no
 * destination that can leave this host — AF_UNIX is a path, and AF_UNSPEC on `connect` is the
 * idiom for un-connecting a datagram socket, which the resolver itself uses. */
static int describe(const struct sockaddr *address, char *text, size_t text_size, int *port,
                    int *loopback)
{
    if (address == NULL) {
        return 0;
    }
    if (address->sa_family == AF_INET) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)address;
        if (inet_ntop(AF_INET, &in->sin_addr, text, (socklen_t)text_size) == NULL) {
            return 0;
        }
        *port = ntohs(in->sin_port);
        *loopback = (ntohl(in->sin_addr.s_addr) >> 24) == 127u;
        return 1;
    }
    if (address->sa_family == AF_INET6) {
        const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)address;
        /* An IPv4-mapped address is an IPv4 destination wearing a v6 sockaddr, and reading it as
         * opaque v6 bytes would let `::ffff:1.2.3.4` past a check that refuses `1.2.3.4`. */
        if (IN6_IS_ADDR_V4MAPPED(&in6->sin6_addr)) {
            struct in_addr mapped;
            memcpy(&mapped, ((const unsigned char *)&in6->sin6_addr) + 12, sizeof mapped);
            if (inet_ntop(AF_INET, &mapped, text, (socklen_t)text_size) == NULL) {
                return 0;
            }
            *port = ntohs(in6->sin6_port);
            *loopback = (ntohl(mapped.s_addr) >> 24) == 127u;
            return 1;
        }
        if (inet_ntop(AF_INET6, &in6->sin6_addr, text, (socklen_t)text_size) == NULL) {
            return 0;
        }
        *port = ntohs(in6->sin6_port);
        *loopback = IN6_IS_ADDR_LOOPBACK(&in6->sin6_addr) ? 1 : 0;
        return 1;
    }
    return 0;
}

/* 0 when the address may be dialled, -1 when it is refused (errno already set, refusal counted). */
static int check_address(const struct sockaddr *address, const char *verb)
{
    char text[INET6_ADDRSTRLEN];
    int port = 0;
    int loopback = 0;
    if (!describe(address, text, sizeof text, &port, &loopback)) {
        return 0;
    }
    if (loopback || is_allowed_host(text) || is_resolved_address(text)
        || (port == 53 && is_nameserver(text))) {
        return 0;
    }
    __atomic_fetch_add(&refused_connect, 1ul, __ATOMIC_SEQ_CST);
    char message[512];
    snprintf(message, sizeof message,
             "refused %s to %s:%d - not the LLM gateway, declared infrastructure, or a host named "
             "in CHEMCLAW_EGRESS_ALLOW",
             verb, text, port);
    emit(message);
    errno = EPERM;
    return -1;
}

/* --------------------------------------------------------- the interpositions */

int connect(int fd, const struct sockaddr *address, socklen_t length)
{
    if (real_connect == NULL) {
        real_connect = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
    }
    if (check_address(address, "connect") != 0) {
        return -1;
    }
    return real_connect(fd, address, length);
}

ssize_t sendto(int fd, const void *buffer, size_t length, int flags,
               const struct sockaddr *address, socklen_t address_length)
{
    if (real_sendto == NULL) {
        real_sendto = (ssize_t (*)(int, const void *, size_t, int, const struct sockaddr *,
                                   socklen_t))dlsym(RTLD_NEXT, "sendto");
    }
    if (check_address(address, "sendto") != 0) {
        return -1;
    }
    return real_sendto(fd, buffer, length, flags, address, address_length);
}

ssize_t sendmsg(int fd, const struct msghdr *message, int flags)
{
    if (real_sendmsg == NULL) {
        real_sendmsg =
            (ssize_t (*)(int, const struct msghdr *, int))dlsym(RTLD_NEXT, "sendmsg");
    }
    if (message != NULL && message->msg_name != NULL
        && check_address((const struct sockaddr *)message->msg_name, "sendmsg") != 0) {
        return -1;
    }
    return real_sendmsg(fd, message, flags);
}

int getaddrinfo(const char *node, const char *service, const struct addrinfo *hints,
                struct addrinfo **result)
{
    if (real_getaddrinfo == NULL) {
        real_getaddrinfo = (int (*)(const char *, const char *, const struct addrinfo *,
                                    struct addrinfo **))dlsym(RTLD_NEXT, "getaddrinfo");
    }
    int numeric = 0;
    if (node != NULL && node[0] != '\0') {
        char host[CHEMCLAW_HOST_MAX];
        size_t length = strlen(node);
        if (length < sizeof host) {
            memcpy(host, node, length + 1);
            unbracket(host);
            struct in_addr v4;
            struct in6_addr v6;
            numeric = inet_pton(AF_INET, host, &v4) == 1 || inet_pton(AF_INET6, host, &v6) == 1;
        }
        int loopback_name = strcmp(node, "localhost") == 0 || strcmp(node, "localhost.") == 0;
        if (!numeric && !loopback_name && !is_allowed_host(node)) {
            __atomic_fetch_add(&refused_resolve, 1ul, __ATOMIC_SEQ_CST);
            char message[512];
            snprintf(message, sizeof message,
                     "refused resolve of %.180s - not the LLM gateway, declared infrastructure, or "
                     "a host named in CHEMCLAW_EGRESS_ALLOW",
                     node);
            emit(message);
            return EAI_NONAME;
        }
    }
    int status = real_getaddrinfo(node, service, hints, result);
    /* Record only what an **allowlisted name** resolved to. A loopback name is already exempt by
     * address, and a numeric host was never resolved at all — recording either would turn whatever
     * a second A record or a split-horizon resolver returns into a standing permission, which is
     * the narrowing `netguard.arm`'s own `getaddrinfo` hook already carries. */
    if (status == 0 && result != NULL && node != NULL && !numeric && is_allowed_host(node)) {
        for (const struct addrinfo *entry = *result; entry != NULL; entry = entry->ai_next) {
            char text[INET6_ADDRSTRLEN];
            int port = 0;
            int loopback = 0;
            if (describe(entry->ai_addr, text, sizeof text, &port, &loopback) && !loopback) {
                remember_resolved(text);
            }
        }
    }
    return status;
}

/* ------------------------------------------------------------ what Python reads */

/* Resolving this symbol is the *only* honest proof that the library is loaded into a process, which
 * is why `netguard_preload.is_armed()` asks for it through `ctypes` rather than reading the
 * environment variable that was supposed to cause it. An env var says what a launcher intended. */
int chemclaw_netguard_preload_armed(void)
{
    return 1;
}

unsigned long chemclaw_netguard_preload_refused(int kind)
{
    if (kind == CHEMCLAW_REFUSAL_CONNECT) {
        return __atomic_load_n(&refused_connect, __ATOMIC_SEQ_CST);
    }
    if (kind == CHEMCLAW_REFUSAL_RESOLVE) {
        return __atomic_load_n(&refused_resolve, __ATOMIC_SEQ_CST);
    }
    return 0ul;
}

unsigned int chemclaw_netguard_preload_allowed_count(void)
{
    return allowed_count;
}
