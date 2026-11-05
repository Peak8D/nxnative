// Networking (out of scope: online play/friends/store/cloud saves): all hooked up as "network unavailable", the game takes the offline branch.
// Cuts: nifm reports no network; socket only hooks up Initialize (returns success, creates no session), the rest of socket/ssl/friends are hooked up as failures when they come into use.
#include "hle.h"
#include <stdatomic.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

HLE_FORCE("_ZN2nn6socket10InitializeERKNS0_6ConfigE", "connects to the bsd socket service; offline, creates no session (cut)", Result, h_socket_Initialize, const void *cfg) { (void)cfg; return RES_OK; }
HLE_FORCE("_ZN2nn4nifm10InitializeEv", "connects to the nifm network management service; offline (cut)", Result, h_nifm_Initialize, void) { return RES_OK; }
// The request is asynchronous on Horizon: SubmitNetworkRequest only hands it to the nifm service, and IsNetworkRequestOnHold stays true until the service has evaluated it, so a query
// right after the submission finds it on hold. Pikmin 4 (Unreal Engine 4.26, main+0x203f3b0) relies on that: it starts its BCAT thread, whose Run (main+0x203f510) sets its status
// to 2, submits, waits while on hold and sets 6 (done, offline), and only then stores status 1 itself; answered at once, a thread that ran first finished before that store, the 1 stayed,
// and the boot state machine (main+0x1f2f764, state 7) waited for the status forever at the black loading screen (about 1 boot in 6, 3 in 4 with four at once). Pikmin 4 sleeps 10 ms
// between its queries, so a request that is on hold at the first query is all it needs.
// Each query is an IPC to the nifm service on Horizon, which evaluates the request as it serves them (offline, a rejection), so the request is on hold for the first query after
// the submission and settled at the next. The hold used to be 50 ms of host time: Hades' NetworkPollThread (main+0x27aaa0) polls with SleepThread(0) and cancels after 2,000
// queries, 2,000 IPCs on the Switch but 0.1 ms here, so it cancelled and submitted again forever (1.17 billion queries a minute, a host core). Its stamp, now_ns() | 1, also read
// as settled at once whenever the next query came within the same 42 ns clock tick (30% of Hades' submissions)
static _Atomic int g_nifm_hold;   // queries the pending request still answers "on hold" (0 = none, or settled)
HLE_FORCE("_ZN2nn4nifm20SubmitNetworkRequestEv", "offline: the request is on hold for the first query, then settles as unavailable", void, h_nifm_Submit, void) { atomic_store(&g_nifm_hold, 1); }
HLE_FORCE("_ZN2nn4nifm20CancelNetworkRequestEv", "offline: drops the pending request", void, h_nifm_Cancel, void) { atomic_store(&g_nifm_hold, 0); }
HLE_FORCE("_ZN2nn4nifm22IsNetworkRequestOnHoldEv", "offline: on hold for the first query after the submission", int, h_nifm_OnHold, void) {
    int h = atomic_load(&g_nifm_hold);
    while (h > 0 && !atomic_compare_exchange_weak(&g_nifm_hold, &h, h - 1)) {}
    return h > 0;
}
HLE_FORCE("_ZN2nn4nifm18IsNetworkAvailableEv", "offline: always unavailable / DM_NET=1: the host's own connectivity", int, h_nifm_Available, void) { return dm_net_online() && dm_net_own_ip() ? 1 : 0; }
// Returns bool (`and w0, w0, #1`, Metroid Dread's sdk+0x336210): whether the system's network screen resolved the error.
// Neither socket mode presents that system screen, so neither can report it resolved.
HLE_FORCE("_ZN2nn4nifm31HandleNetworkRequestErrorResultEv", "not resolved, shows no system network screen (offline or DM_NET=1)", unsigned, h_nifm_HandleErr, void) { return 0; }
HLE_FORCE("_ZN2nn4nifm26GetCurrentPrimaryIpAddressEPNS_6socket6InAddrE", "offline: no IP / DM_NET=1: the probed interface", Result, h_nifm_GetIp, uint32_t *a) {
    if (!dm_net_online() || !dm_net_own_ip()) { if (a) *a = 0; return RES_FAIL; }
    *a = dm_net_own_ip(); return RES_OK;
}
HLE_FORCE("_ZN2nn4nifm22GetCurrentProxySettingEPNS0_12ProxySettingE", "offline: no proxy setting", Result, h_nifm_GetProxy, void *p) { (void)p; return RES_FAIL; }
HLE_FORCE("nnsslInitialize", "connects to the ssl service; the network library's global init needs it to succeed, offline creates no session (cut)", Result, h_ssl_Initialize, void) { return RES_OK; }
HLE_FORCE("nnsslFinalize", "pairs with nnsslInitialize (cut)", Result, h_ssl_Finalize, void) { return RES_OK; }

// ---------- socket: offline, all calls that contact the service return -1 in BSD convention, error code ENETDOWN ----------
// Not cut: Inet* / nnsocketInet* byte order and address text conversion (pure computation).
#define SK_ENETDOWN 50                  // Horizon sockets use FreeBSD error codes
static __thread int t_skerr = SK_ENETDOWN;
static int sk_fail(void) { t_skerr = SK_ENETDOWN; return -1; }
#define SKF(m, cname, ...) HLE_FORCE(m, "bsd socket service (offline cut / DM_NET=1: the host's own sockets)", int, cname, __VA_ARGS__)
// Poll / Select with nothing to wait on: the host creates no socket, so a game's descriptors are all -1, or it passes none. POSIX and Horizon's bsd service ignore negative
// descriptors and wait out the timeout, returning 0. Failing at once made Dark Souls Remastered's network thread ("NexusRevolution Socket", main+0x92c7c4) spin a core at 100%
// on Poll(fds, 0, 33 ms), 11,000 calls a second. An endless wait on nothing never returns on hardware; here it returns 0 after 100 ms: a thread in a host hook pauses for a garbage
// collector (IL2CPP's, hle_thread.c) only once the hook returns, and the caller can still see a shutdown
void dm_guest_sleep_ns(int64_t ns);
static int sk_idle(int64_t ms) { dm_guest_sleep_ns((ms < 0 ? 100 : ms) * 1000000); return 0; }
typedef struct { int32_t fd; int16_t events, revents; } sk_pollfd;

// ---------- DM_NET=1: real sockets (the default stays offline: without the flag every game keeps the behaviour above) ----------
// The guest descriptor is a host descriptor. Horizon's SockAddrIn is FreeBSD's: u8 sin_len at 0, u8 family at 1 (AF_INET = 2, the host's
// own number), the port big-endian at 2, the address at 4. Addresses cross through an explicit conversion all the same, and a u16 family of 2
// at 0 (no sin_len) is taken too. Star Fox's libuv (0400E88025B1A000, main+0xbe32700) binds {16, 2, ...}: read as a u16, its family 0x210
// failed the bind, libuv had no wakeup pair, and its loop polled a watcher without a descriptor and aborted. PollFd and the fd_set bits match the host's layouts, and the host is BSD, so the errno numbering
// is the guest's. DM_NET_PEERS=ip,ip,… replicates a send to a broadcast-style address (255.255.255.255, the interface's own
// IP, or its /24's .255) as unicast to each peer: a Tailscale tailnet carries no L2 broadcast, so a game's LAN discovery
// rides on this. nifm (below) reports the probed interface with a /24 subnet, which puts a game's directed-broadcast
// address inside the same patterns.
// DM_NET unset: the profile's net key (DM_GAME_NET). Star Fox turns it on: its libuv needs a loopback socket pair to start, and offline there are no sockets at all. Loopback
// sockets offline, as Horizon's bsd service has them without a network, would serve it without a profile key (open)
int dm_net_online(void) { static int v = -1; if (v < 0) { const char *e = getenv("DM_NET"); v = e ? atoi(e) != 0 : DM_GAME_NET; } return v; }
enum { SK_MAX_PEERS = 16 };
static in_addr_t sk_peers[SK_MAX_PEERS];
static int sk_npeers = -1;
static in_addr_t sk_own_ip;          // the interface unicast leaves through (dm_net_own_ip); 0 = unknown
static uint8_t sk_own_logged;

static int sk_peer_count(void) {
    if (sk_npeers < 0) {
        sk_npeers = 0;
        const char *p = getenv("DM_NET_PEERS");
        while (p && *p && sk_npeers < SK_MAX_PEERS) {
            char buf[40]; const char *q = strchr(p, ',');
            size_t n = q ? (size_t)(q - p) : strlen(p);
            if (n < sizeof buf) {
                memcpy(buf, p, n); buf[n] = 0;
                struct in_addr a;
                if (inet_pton(AF_INET, buf, &a) == 1) sk_peers[sk_npeers++] = a.s_addr;
                else LOG("[RED] DM_NET_PEERS: %s is not an IPv4 address", buf);
            }
            p = q ? q + 1 : p + strlen(p);
        }
    }
    return sk_npeers;
}

uint32_t dm_net_own_ip(void) {   // a connected UDP socket's local address: the route unicast would take, no packets sent
    if (!sk_own_ip && dm_net_online()) {
        int s = (int)socket(AF_INET, SOCK_DGRAM, 0);
        if (s >= 0) {
            struct sockaddr_in a = {0}; a.sin_len = sizeof a; a.sin_family = AF_INET; a.sin_port = htons(53);   // connect needs a port; UDP connect sends nothing, it only picks the route
            const char *e = getenv("DM_NET_PROBE");   // what to route to: the first peer by default, else the system's own route (8.8.8.8)
            a.sin_addr.s_addr = e ? inet_addr(e) : (sk_peer_count() ? sk_peers[0] : inet_addr("8.8.8.8"));
            if (!connect(s, (struct sockaddr *)&a, sizeof a)) {
                socklen_t l = sizeof a; struct sockaddr_in l4 = {0};
                if (!getsockname(s, (struct sockaddr *)&l4, &l)) sk_own_ip = l4.sin_addr.s_addr;
            }
            close(s);
        }
        if (sk_own_ip && !sk_own_logged) {
            sk_own_logged = 1;
            struct in_addr i = {sk_own_ip}; char ip[INET_ADDRSTRLEN] = "";
            inet_ntop(AF_INET, &i, ip, sizeof ip);
            LOG("DM_NET: the game's sockets are the host's own (interface %s, %d peer(s))", ip, sk_peer_count());
        }
    }
    return sk_own_ip;
}

void dm_net_ipconfig(uint32_t *ip, uint32_t *mask, uint32_t *gw, uint32_t *d0, uint32_t *d1) {   // nifm's IpConfigInfo; the /24 mask lands a game's directed broadcast inside the replication patterns
    uint32_t own = dm_net_own_ip();
    if (ip) *ip = own;
    if (mask) *mask = own ? htonl(0xffffff00u) : 0;
    if (gw) *gw = 0;
    if (d0) *d0 = 0;
    if (d1) *d1 = 0;
}

static void sk_host_addr(struct sockaddr_in *h, const void *g) {   // Horizon SockAddr → the host's (which has sin_len first)
    memset(h, 0, sizeof *h);
    h->sin_len = sizeof *h; h->sin_family = AF_INET;
    memcpy(&h->sin_port, (const char *)g + 2, 2);
    memcpy(&h->sin_addr, (const char *)g + 4, 4);
}
static void sk_guest_addr(void *g, unsigned *gl, const struct sockaddr_in *h) {   // and back (sin_len 16, family 2, as Horizon writes them)
    uint8_t o[16] = {0};
    o[0] = sizeof o; o[1] = 2;   // sin_len, AF_INET
    memcpy(o + 2, &h->sin_port, 2);
    memcpy(o + 4, &h->sin_addr, 4);
    unsigned capacity = gl ? *gl : 0;
    if (capacity) memcpy(g, o, capacity < sizeof o ? capacity : sizeof o);
    if (gl) *gl = sizeof o;
}
static int sk_is_broadcast(const struct sockaddr_in *d) {   // 255.255.255.255 / the interface's own IP / its /24's .255
    if (d->sin_addr.s_addr == INADDR_BROADCAST || !d->sin_addr.s_addr) return 1;
    uint32_t own = sk_own_ip, ip = d->sin_addr.s_addr;
    return own && (ip == own || ((ip & htonl(0xffffff00u)) == (own & htonl(0xffffff00u)) && (ip & htonl(0x000000ffu)) == htonl(0x000000ffu)));
}

int dm_sk_socket(int fam, int type, int proto) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    if (fam != 2) { t_skerr = 47; return -1; }   // EAFNOSUPPORT: AF_INET for now (a game's Inet6 is not wired)
    int s = (int)socket(AF_INET, type, proto ? proto : 0);
    if (s < 0) { t_skerr = errno; return -1; }
    if (type == 2) { int one = 1; setsockopt(s, SOL_SOCKET, SO_BROADCAST, &one, sizeof one); }   // a game sending to 255.255.255.255 expects that to work as on hardware
    t_skerr = 0; return s;
}
static int sk_addr_ok(const void *a, unsigned n, struct sockaddr_in *h) {
    const uint8_t *b = a;
    if (n < 8 || !(b[1] == 2 || (b[0] == 2 && b[1] == 0))) { t_skerr = 47; return 0; }   // FreeBSD's u8 family at 1, or a u16 family at 0
    sk_host_addr(h, a); return 1;
}
int dm_sk_bind(int s, const void *a, unsigned n) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in h; if (!sk_addr_ok(a, n, &h)) return -1;
    int rc = bind(s, (struct sockaddr *)&h, sizeof h);
    t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_connect(int s, const void *a, unsigned n) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in h; if (!sk_addr_ok(a, n, &h)) return -1;
    int rc = connect(s, (struct sockaddr *)&h, sizeof h);
    t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_listen(int s, int n) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    int rc = listen(s, n); t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_accept(int s, void *a, unsigned *n) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in h = {0}; socklen_t l = sizeof h;
    int r = (int)accept(s, (struct sockaddr *)&h, &l);
    if (r >= 0) { t_skerr = 0; if (a && n) sk_guest_addr(a, n, &h); }
    else t_skerr = errno;
    return r;
}

// Event descriptors (nn::socket::EventFd): the bsd service keeps them itself, network or not, and an engine wakes its own network thread with them. Failed as a socket, Star Fox's
// (0400E88025B1A000) network library started its wakeup watcher without a descriptor: the watcher's fd field still held a list pointer, the SDK's poll took it for no socket of
// its own (EFAULT), and the library aborted (main+0xbe2f3a0). Host: each one is a pipe whose read end is the guest's descriptor (so with DM_NET=1 it cannot take a socket's number,
// and the host's poll sees it), its count kept beside it; the pipe holds one byte while the count is not 0. Reads take the count (or 1 in semaphore mode), writes add to it
#define SK_EFD_SEMAPHORE 1
#define SK_EFD_NONBLOCK 4       // EventFdFlags: the flag the eventfd's own read honours
#define SK_O_NONBLOCK 0x800     // Horizon's O_NONBLOCK (dm_sk_fcntl)
typedef struct { int fd, wfd, flags; uint64_t v; } sk_efd;
static sk_efd g_efd[64];
static pthread_mutex_t g_efd_mu = PTHREAD_MUTEX_INITIALIZER;
static sk_efd *sk_efd_of(int fd) { if (fd >= 0) for (int i = 0; i < 64; i++) if (g_efd[i].wfd && g_efd[i].fd == fd) return &g_efd[i]; return NULL; }   // under g_efd_mu
static int sk_is_efd(int fd) { pthread_mutex_lock(&g_efd_mu); int r = sk_efd_of(fd) != NULL; pthread_mutex_unlock(&g_efd_mu); return r; }
static int sk_efd_new(uint64_t v, int flags) {
    int p[2]; if (pipe(p)) { t_skerr = errno; return -1; }
    for (int k = 0; k < 2; k++) { fcntl(p[k], F_SETFL, O_NONBLOCK); fcntl(p[k], F_SETFD, FD_CLOEXEC); }
    pthread_mutex_lock(&g_efd_mu);
    for (int i = 0; i < 64; i++) if (!g_efd[i].wfd) {
        g_efd[i] = (sk_efd){p[0], p[1], flags, v};
        if (v) (void)!write(p[1], "e", 1);
        pthread_mutex_unlock(&g_efd_mu); t_skerr = 0; return p[0];
    }
    pthread_mutex_unlock(&g_efd_mu); close(p[0]); close(p[1]);
    LOG("[RED] socket: more than 64 event descriptors"); t_skerr = 24; return -1;   // EMFILE
}
static int sk_efd_read(int fd, uint64_t *out) {   // 1 done, 0 not an event descriptor, -1 failed (t_skerr)
    pthread_mutex_lock(&g_efd_mu);
    for (;;) {
        sk_efd *e = sk_efd_of(fd);
        if (!e) { pthread_mutex_unlock(&g_efd_mu); return 0; }
        if (e->v) {
            uint64_t v = e->flags & SK_EFD_SEMAPHORE ? 1 : e->v; e->v -= v; *out = v;
            if (!e->v) { char b[8]; while (read(e->fd, b, sizeof b) > 0) {} }
            pthread_mutex_unlock(&g_efd_mu); t_skerr = 0; return 1;
        }
        if (e->flags & SK_EFD_NONBLOCK) { pthread_mutex_unlock(&g_efd_mu); t_skerr = 35; return -1; }   // EAGAIN
        pthread_mutex_unlock(&g_efd_mu);
        struct pollfd w = {fd, POLLIN, 0}; poll(&w, 1, 100);
        pthread_mutex_lock(&g_efd_mu);
    }
}
static int sk_efd_write(int fd, uint64_t v) {
    pthread_mutex_lock(&g_efd_mu);
    sk_efd *e = sk_efd_of(fd);
    if (e) { if (v && !e->v) (void)!write(e->wfd, "e", 1); e->v += v; }
    pthread_mutex_unlock(&g_efd_mu); t_skerr = 0; return e != NULL;
}
static int sk_efd_close(int fd) {
    pthread_mutex_lock(&g_efd_mu);
    sk_efd *e = sk_efd_of(fd); if (e) { close(e->fd); close(e->wfd); *e = (sk_efd){0}; }
    pthread_mutex_unlock(&g_efd_mu); return e != NULL;
}
static int sk_efd_fcntl(int fd, int c, long a, int *rc) {   // 1 when fd is an event descriptor (*rc the result)
    pthread_mutex_lock(&g_efd_mu);
    sk_efd *e = sk_efd_of(fd);
    if (e) {
        if (c == 3) *rc = e->flags & SK_EFD_NONBLOCK ? SK_O_NONBLOCK : 0;
        else if (c == 4) { e->flags = (e->flags & ~SK_EFD_NONBLOCK) | (a & SK_O_NONBLOCK ? SK_EFD_NONBLOCK : 0); *rc = 0; }
        else *rc = 0;
    }
    pthread_mutex_unlock(&g_efd_mu); return e != NULL;
}
int dm_sk_close(int s) {
    if (sk_efd_close(s)) { t_skerr = 0; return 0; }
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    int rc = close(s); t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_shutdown(int s, int how) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    int rc = shutdown(s, how); t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_name(int s, void *a, unsigned *n, int peer) {   // GetPeerName (1) / GetSockName (0)
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in h = {0}; socklen_t l = sizeof h;
    int rc = peer ? getpeername(s, (struct sockaddr *)&h, &l) : getsockname(s, (struct sockaddr *)&h, &l);
    if (rc >= 0) { t_skerr = 0; if (a && n) sk_guest_addr(a, n, &h); }
    else t_skerr = errno;
    return rc;
}
int dm_sk_sockopt(int s, int l, int o, const void *v, unsigned *n, int set) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    if (l != 0xffff) { t_skerr = 42; return -1; }   // ENOPROTOOPT: nn::socket::Level Socket = 0xffff = the host's SOL_SOCKET; the other levels (IP options) are not wired
    socklen_t hl = n ? *n : 0;
    int rc = set ? setsockopt(s, SOL_SOCKET, o, v, n ? *n : 0) : getsockopt(s, SOL_SOCKET, o, (void *)v, &hl);
    if (!set && n) *n = hl;
    t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_fcntl(int s, int c, long a) {
    int erc; if (sk_efd_fcntl(s, c, a, &erc)) { t_skerr = 0; return erc; }
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    if (c == 3) {   // F_GETFL: O_NONBLOCK is 0x800 on Horizon, 4 here
        int rc = fcntl(s, F_GETFL);
        if (rc < 0) { t_skerr = errno; return -1; }
        t_skerr = 0; return (rc & O_NONBLOCK) ? (rc & ~O_NONBLOCK) | 0x800 : rc;
    }
    if (c == 4) {   // F_SETFL
        int rc = fcntl(s, F_SETFL, (a & 0x800) ? O_NONBLOCK : 0);
        t_skerr = rc < 0 ? errno : 0; return rc;
    }
    t_skerr = 22; return -1;   // EINVAL: the other commands are not wired
}
long dm_sk_send(int s, const void *b, unsigned long n, int f) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    long r = send(s, b, n, f); t_skerr = r < 0 ? errno : 0; return r;
}
long dm_sk_recv(int s, void *b, unsigned long n, int f) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    long r = recv(s, b, n, f); t_skerr = r < 0 ? errno : 0; return r;
}
long dm_sk_sendto(int s, const void *b, unsigned long n, int f, const void *a, unsigned al) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in d; if (!sk_addr_ok(a, al, &d)) return -1;
    long r = sendto(s, b, n, f, (struct sockaddr *)&d, sizeof d);
    int err = r < 0 ? errno : 0;
    int peers = sk_peer_count();
    if (peers && sk_is_broadcast(&d)) {
        for (int i = 0; i < peers; i++) {
            if (sk_peers[i] == d.sin_addr.s_addr) continue;
            struct sockaddr_in p = {0}; p.sin_len = sizeof p; p.sin_family = AF_INET;
            p.sin_port = d.sin_port; p.sin_addr.s_addr = sk_peers[i];
            long copy = sendto(s, b, n, f, (struct sockaddr *)&p, sizeof p);
            if (r < 0) { r = copy; err = copy < 0 ? errno : 0; }
        }
        // A tailnet may have no broadcast route; a successful peer copy still delivers the datagram. Keep failures when none was sent.
    }
    t_skerr = err;
    return r;
}
long dm_sk_recvfrom(int s, void *b, unsigned long n, int f, void *a, unsigned *al) {
    if (!dm_net_online()) { t_skerr = SK_ENETDOWN; return -1; }
    struct sockaddr_in d = {0}; socklen_t l = sizeof d;
    long r = recvfrom(s, b, n, f, a ? (struct sockaddr *)&d : NULL, a ? &l : NULL);
    if (r >= 0) { t_skerr = 0; if (a && al) sk_guest_addr(a, al, &d); }
    else t_skerr = errno;
    return r;
}
int dm_sk_last_error(void) { return t_skerr; }

int dm_sk_poll(void *fds, uint64_t n, int ms) {   // also hle_offline.c's plain pollfd form
    sk_pollfd *p = fds; int efds = 0;
    for (uint64_t i = 0; i < n; i++) if (p[i].fd >= 0) { if (sk_is_efd(p[i].fd)) efds++; else if (!dm_net_online()) return sk_fail(); }   // offline the only descriptors are event ones
    if (!efds && !dm_net_online()) {
        for (uint64_t i = 0; i < n; i++) p[i].revents = 0;
        return sk_idle(ms);
    }
    // An event descriptor is always writable: asked for, that alone makes the poll return at once (the pipe's read end never reports it)
    int wr = 0;
    for (uint64_t i = 0; i < n && efds; i++) if (p[i].fd >= 0 && (p[i].events & POLLOUT) && sk_is_efd(p[i].fd)) wr = 1;
    int rc = poll((struct pollfd *)p, (nfds_t)n, wr ? 0 : !dm_net_online() && ms < 0 ? 100 : ms);   // identical layouts; negative descriptors are ignored as on Horizon (offline an endless wait returns after 100 ms, as above)
    if (rc >= 0 && efds) {
        rc = 0;
        for (uint64_t i = 0; i < n; i++) {
            if (p[i].fd >= 0 && (p[i].events & POLLOUT) && sk_is_efd(p[i].fd)) p[i].revents |= POLLOUT;
            rc += p[i].revents != 0;
        }
    }
    t_skerr = rc < 0 ? errno : 0; return rc;
}
int dm_sk_select(int nfds, const uint64_t *r, const uint64_t *w, const uint64_t *e, const int64_t *tv) {   // fd_set: 1024 bits; timeval: {i64 seconds, i64 microseconds}
    const uint64_t *sets[3] = {r, w, e};
    int live = 0;
    if (dm_net_online())
        for (int k = 0; k < 3 && !live; k++)
            for (int fd = 0; sets[k] && fd < nfds && fd < 1024; fd++) if ((sets[k][fd >> 6] >> (fd & 63)) & 1) { live = 1; break; }
    if (!live) {   // offline (any descriptor fails), or nothing to wait on: the timeout passes (Dark Souls' net thread)
        for (int k = 0; k < 3; k++) for (int fd = 0; sets[k] && fd < nfds && fd < 1024; fd++) if ((sets[k][fd >> 6] >> (fd & 63)) & 1) return sk_fail();
        return sk_idle(tv ? tv[0] * 1000 + tv[1] / 1000 : -1);
    }
    fd_set R, W, E; fd_set *h[3] = {r ? &R : NULL, w ? &W : NULL, e ? &E : NULL};
    FD_ZERO(&R); FD_ZERO(&W); FD_ZERO(&E);
    for (int k = 0; k < 3; k++) for (int fd = 0; sets[k] && fd < nfds && fd < 1024; fd++) if ((sets[k][fd >> 6] >> (fd & 63)) & 1) FD_SET(fd, h[k]);
    struct timeval t = {0, 0};
    if (tv) { t.tv_sec = (time_t)tv[0]; t.tv_usec = (suseconds_t)tv[1]; }
    int rc = select(nfds > 0 ? nfds : 1, h[0], h[1], h[2], tv ? &t : NULL);
    t_skerr = rc < 0 ? errno : 0;
    if (rc >= 0) for (int k = 0; k < 3; k++) if (sets[k]) {   // the results go back into the guest's bit sets (Horizon's fd_set is 1024 bits)
        uint64_t *g = (uint64_t *)sets[k];
        memset(g, 0, 128);
        for (int fd = 0; fd < nfds && fd < 1024; fd++) if (FD_ISSET(fd, h[k])) g[fd >> 6] |= (uint64_t)1 << (fd & 63);
    }
    return rc;
}
SKF("_ZN2nn6socket6SocketENS0_6FamilyENS0_4TypeENS0_8ProtocolE", h_sk_Socket, int a, int b, int c) { return dm_sk_socket(a, b, c); }
SKF("_ZN2nn6socket4BindEiPKNS0_8SockAddrEj", h_sk_Bind, int s, const void *a, unsigned n) { return dm_sk_bind(s, a, n); }
SKF("_ZN2nn6socket7ConnectEiPKNS0_8SockAddrEj", h_sk_Connect, int s, const void *a, unsigned n) { return dm_sk_connect(s, a, n); }
SKF("_ZN2nn6socket6ListenEii", h_sk_Listen, int s, int n) { return dm_sk_listen(s, n); }
SKF("_ZN2nn6socket6AcceptEiPNS0_8SockAddrEPj", h_sk_Accept, int s, void *a, unsigned *n) { return dm_sk_accept(s, a, n); }
SKF("_ZN2nn6socket5CloseEi", h_sk_Close, int s) { return dm_sk_close(s); }
SKF("_ZN2nn6socket11GetPeerNameEiPNS0_8SockAddrEPj", h_sk_GetPeerName, int s, void *a, unsigned *n) { return dm_sk_name(s, a, n, 1); }
SKF("_ZN2nn6socket11GetSockNameEiPNS0_8SockAddrEPj", h_sk_GetSockName, int s, void *a, unsigned *n) { return dm_sk_name(s, a, n, 0); }
SKF("_ZN2nn6socket10GetSockOptEiNS0_5LevelENS0_6OptionEPvPj", h_sk_GetSockOpt, int s, int l, int o, void *v, unsigned *n) { return dm_sk_sockopt(s, l, o, v, n, 0); }
SKF("_ZN2nn6socket10SetSockOptEiNS0_5LevelENS0_6OptionEPKvj", h_sk_SetSockOpt, int s, int l, int o, const void *v, unsigned n) { return dm_sk_sockopt(s, l, o, v, &n, 1); }
SKF("_ZN2nn6socket5FcntlEiNS0_12FcntlCommandEz", h_sk_Fcntl, int s, int c, long a) { return dm_sk_fcntl(s, c, a); }
SKF("_ZN2nn6socket4PollEPNS0_6PollFdEmi", h_sk_Poll, void *f, unsigned long n, int t) { return dm_sk_poll(f, n, t); }
SKF("_ZN2nn6socket6SelectEiPNS0_5FdSetES2_S2_PNS0_7TimeValE", h_sk_Select, int n, void *r, void *w, void *e, void *t) { return dm_sk_select(n, r, w, e, t); }
HLE_FORCE("_ZN2nn6socket7EventFdEmNS0_12EventFdFlagsE", "an event descriptor the host keeps (the bsd service's, network or not)", int, h_sk_EventFd, unsigned long v, int f) { return sk_efd_new(v, f); }
HLE_FORCE("_ZN2nn6socket11EventFdReadEiPm", "takes an event descriptor's count", int, h_sk_EventFdRead, int s, uint64_t *v) { int r = sk_efd_read(s, v); return r > 0 ? 0 : r < 0 ? -1 : sk_fail(); }
HLE_FORCE("_ZN2nn6socket12EventFdWriteEim", "adds to an event descriptor's count", int, h_sk_EventFdWrite, int s, uint64_t v) { return sk_efd_write(s, v) ? 0 : sk_fail(); }
// Read and Write: an event descriptor's 8-byte count, as on the bsd service; a socket's data with DM_NET=1; offline they fail like the other socket calls
HLE_FORCE("_ZN2nn6socket4ReadEiPvm", "an event descriptor's count / DM_NET=1: the host socket's data", long, h_sk_Read, int s, void *b, unsigned long n) {
    if (sk_is_efd(s)) { uint64_t v; if (n < 8) { t_skerr = 22; return -1; } int r = sk_efd_read(s, &v); if (r > 0) { memcpy(b, &v, 8); return 8; } return -1; }   // EINVAL
    if (!dm_net_online()) return sk_fail();
    long r = read(s, b, n); t_skerr = r < 0 ? errno : 0; return r;
}
HLE_FORCE("_ZN2nn6socket5WriteEiPKvm", "adds to an event descriptor's count / DM_NET=1: the host socket's data", long, h_sk_Write, int s, const void *b, unsigned long n) {
    if (sk_is_efd(s)) { uint64_t v; if (n < 8) { t_skerr = 22; return -1; } memcpy(&v, b, 8); sk_efd_write(s, v); return 8; }
    if (!dm_net_online()) return sk_fail();
    long r = write(s, b, n); t_skerr = r < 0 ? errno : 0; return r;
}
SKF("_ZN2nn6socket17ResolverGetOptionEPNS0_14ResolverOptionENS0_17ResolverOptionKeyE", h_sk_ResGet, void *o, int k) { (void)o; (void)k; return sk_fail(); }
SKF("_ZN2nn6socket17ResolverSetOptionERKNS0_14ResolverOptionE", h_sk_ResSet, const void *o) { (void)o; return sk_fail(); }
HLE_FORCE("_ZN2nn6socket4RecvEiPvmNS0_7MsgFlagE", "bsd socket service (offline cut / DM_NET=1: the host's own)", long, h_sk_Recv, int s, void *b, unsigned long n, int f) { return dm_sk_recv(s, b, n, f); }
HLE_FORCE("_ZN2nn6socket8RecvFromEiPvmNS0_7MsgFlagEPNS0_8SockAddrEPj", "bsd socket service (offline cut / DM_NET=1: the host's own)", long, h_sk_RecvFrom, int s, void *b, unsigned long n, int f, void *a, unsigned *al) { return dm_sk_recvfrom(s, b, n, f, a, al); }
HLE_FORCE("_ZN2nn6socket4SendEiPKvmNS0_7MsgFlagE", "bsd socket service (offline cut / DM_NET=1: the host's own)", long, h_sk_Send, int s, const void *b, unsigned long n, int f) { return dm_sk_send(s, b, n, f); }
HLE_FORCE("_ZN2nn6socket11GetAddrInfoEPKcS2_PKNS0_8AddrInfoEPPS3_", "name resolution (cut): EAI_FAIL", int, h_sk_GetAddrInfo, const char *h, const char *s, const void *hint, void **res) {
    (void)h; (void)s; (void)hint; if (res) *res = NULL; return 4;   // EAI_FAIL
}
HLE_FORCE("_ZN2nn6socket12FreeAddrInfoEPNS0_8AddrInfoE", "pairs with GetAddrInfo (cut)", void, h_sk_FreeAddrInfo, void *ai) { (void)ai; }
HLE_FORCE("_ZN2nn6socket16GetHostEntByNameEPKcPKNS0_14ResolverOptionEm", "name resolution (cut): NULL", void *, h_sk_GetHostEnt, const char *n, const void *o, unsigned long c) { (void)n; (void)o; (void)c; t_skerr = SK_ENETDOWN; return NULL; }
HLE_FORCE("_ZN2nn6socket12GetLastErrorEv", "socket error code kept by the host", int, h_sk_GetLastError, void) { return t_skerr; }
// C interface
SKF("nnsocketSocket", h_nsk_Socket, int a, int b, int c) { return dm_sk_socket(a, b, c); }
SKF("nnsocketBind", h_nsk_Bind, int s, const void *a, unsigned n) { return dm_sk_bind(s, a, n); }
SKF("nnsocketConnect", h_nsk_Connect, int s, const void *a, unsigned n) { return dm_sk_connect(s, a, n); }
SKF("nnsocketClose", h_nsk_Close, int s) { return dm_sk_close(s); }
SKF("nnsocketGetPeerName", h_nsk_GetPeerName, int s, void *a, unsigned *n) { return dm_sk_name(s, a, n, 1); }
SKF("nnsocketGetSockName", h_nsk_GetSockName, int s, void *a, unsigned *n) { return dm_sk_name(s, a, n, 0); }
SKF("nnsocketGetSockOpt", h_nsk_GetSockOpt, int s, int l, int o, void *v, unsigned *n) { return dm_sk_sockopt(s, l, o, v, n, 0); }
SKF("nnsocketSetSockOpt", h_nsk_SetSockOpt, int s, int l, int o, const void *v, unsigned n) { return dm_sk_sockopt(s, l, o, v, &n, 1); }
SKF("nnsocketFcntl", h_nsk_Fcntl, int s, int c, long a) { return dm_sk_fcntl(s, c, a); }
SKF("nnsocketPoll", h_nsk_Poll, void *f, unsigned long n, int t) { return dm_sk_poll(f, n, t); }
SKF("nnsocketResolverGetOption", h_nsk_ResGet, void *o, int k) { (void)o; (void)k; return sk_fail(); }
SKF("nnsocketResolverSetOption", h_nsk_ResSet, const void *o) { (void)o; return sk_fail(); }
HLE_FORCE("nnsocketRecv", "bsd socket service (offline cut / DM_NET=1: the host's own)", long, h_nsk_Recv, int s, void *b, unsigned long n, int f) { return dm_sk_recv(s, b, n, f); }
HLE_FORCE("nnsocketSend", "bsd socket service (offline cut / DM_NET=1: the host's own)", long, h_nsk_Send, int s, const void *b, unsigned long n, int f) { return dm_sk_send(s, b, n, f); }
HLE_FORCE("nnsocketGetHostByNameWithOptions", "name resolution (cut): NULL", void *, h_nsk_GetHost, const char *n, const void *o, unsigned long c) { (void)n; (void)o; (void)c; t_skerr = SK_ENETDOWN; return NULL; }
HLE_FORCE("nnsocketGetLastErrno", "socket error code kept by the host", int, h_nsk_GetErrno, void) { return t_skerr; }
HLE_FORCE("nnsocketSetLastErrno", "socket error code kept by the host", void, h_nsk_SetErrno, int e) { t_skerr = e; }

// ---------- ssl: offline, Initialize and a context's Create / Destroy succeed, everything else that contacts the service fails ----------
// A context is a session of the local ssl service, so on Horizon Create works with no network and only a connection needs one; DRAGON QUEST BUILDERS 2 ends its startup when Create fails (main+0x861260).
// Not cut: Context / Connection construction and destruction, Context::GetContextId (only read and write game objects).
#define SSF(m, cname, ...) HLE_FORCE(m, "ssl service (cut): fails", Result, cname, __VA_ARGS__)
HLE_FORCE("_ZN2nn3ssl10InitializeEv", "same as nnsslInitialize: succeeds, creates no session (cut)", Result, h_ssl_InitCpp, void) { return RES_OK; }
HLE_FORCE("_ZN2nn3ssl8FinalizeEv", "pairs with Initialize (cut)", Result, h_ssl_FinCpp, void) { return RES_OK; }
HLE_FORCE("_ZN2nn3ssl7Context6CreateENS1_10SslVersionE", "a session of the local ssl service: succeeds offline, connects to nothing (cut)", Result, h_ssl_CtxCreate, void *self, int v) { (void)self; (void)v; return RES_OK; }
HLE_FORCE("_ZN2nn3ssl7Context7DestroyEv", "pairs with Create (cut)", Result, h_ssl_CtxDestroy, void *self) { (void)self; return RES_OK; }
SSF("_ZN2nn3ssl7Context19RegisterInternalPkiEPmNS1_11InternalPkiE", h_ssl_CtxRegPki, void *self, uint64_t *id, int p) { (void)self; (void)id; (void)p; return RES_FAIL; }
SSF("_ZN2nn3ssl7Context9RemovePkiEm", h_ssl_CtxRemovePki, void *self, uint64_t id) { (void)self; (void)id; return RES_FAIL; }
SSF("_ZN2nn3ssl14BuiltInManager28GetBuiltInCertificateBufSizeEPjPNS0_15CaCertificateIdEj", h_ssl_BiBufSize, uint32_t *o, void *ids, unsigned n) { (void)ids; (void)n; if (o) *o = 0; return RES_FAIL; }
SSF("_ZN2nn3ssl14BuiltInManager22GetBuiltInCertificatesEPPNS1_22BuiltInCertificateInfoEPhjPNS0_15CaCertificateIdEj", h_ssl_BiCerts, void **o, uint8_t *b, unsigned bs, void *ids, unsigned n) { (void)o; (void)b; (void)bs; (void)ids; (void)n; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection6CreateEPNS0_7ContextE", h_ssl_CnCreate, void *self, void *ctx) { (void)self; (void)ctx; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection7DestroyEv", h_ssl_CnDestroy, void *self) { (void)self; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection11DoHandshakeEv", h_ssl_CnHandshake, void *self) { (void)self; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection11SetHostNameEPKcj", h_ssl_CnHost, void *self, const char *h, unsigned n) { (void)self; (void)h; (void)n; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection9SetIoModeENS1_6IoModeE", h_ssl_CnIoMode, void *self, int m) { (void)self; (void)m; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection19SetSessionCacheModeENS1_16SessionCacheModeE", h_ssl_CnCache, void *self, int m) { (void)self; (void)m; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection19SetSocketDescriptorEi", h_ssl_CnSock, void *self, int s) { (void)self; (void)s; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection12GetLastErrorEPNS_6ResultE", h_ssl_CnLastErr, void *self, uint32_t *r) { (void)self; if (r) *r = RES_FAIL; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection18GetVerifyCertErrorEPNS_6ResultE", h_ssl_CnVerifyErr, void *self, uint32_t *r) { (void)self; if (r) *r = RES_FAIL; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection4PeekEPcPij", h_ssl_CnPeek, void *self, char *b, int *o, unsigned n) { (void)self; (void)b; (void)n; if (o) *o = 0; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection4PollEPNS1_9PollEventES3_j", h_ssl_CnPoll, void *self, void *o, void *i, unsigned t) { (void)self; (void)o; (void)i; (void)t; return RES_FAIL; }
HLE_FORCE("_ZN2nn3ssl10Connection4ReadEPcj", "ssl service (cut): read returns -1", int, h_ssl_CnRead, void *self, char *b, unsigned n) { (void)self; (void)b; (void)n; return -1; }
HLE_FORCE("_ZN2nn3ssl10Connection5WriteEPKcj", "ssl service (cut): write returns -1", int, h_ssl_CnWrite, void *self, const char *b, unsigned n) { (void)self; (void)b; (void)n; return -1; }
// C interface: all return nnResult
#define SSC(m, cname) HLE_FORCE(m, "ssl service (cut): fails", Result, cname, void) { return RES_FAIL; }
SSC("nnsslConnectionCreate", h_nssl_Create) SSC("nnsslConnectionDestroy", h_nssl_Destroy)
SSC("nnsslConnectionDoHandshakeWithCertBuffer", h_nssl_Handshake) SSC("nnsslConnectionGetConnectionId", h_nssl_GetId)
SSC("nnsslConnectionGetNextAlpnProto", h_nssl_GetAlpn) SSC("nnsslConnectionGetVerifyCertError", h_nssl_VerifyErr)
SSC("nnsslConnectionPeek", h_nssl_Peek) SSC("nnsslConnectionPending", h_nssl_Pending) SSC("nnsslConnectionRead", h_nssl_Read)
SSC("nnsslConnectionSetHostName", h_nssl_SetHost) SSC("nnsslConnectionSetIoMode", h_nssl_SetIo)
SSC("nnsslConnectionSetNextAlpnProto", h_nssl_SetAlpn) SSC("nnsslConnectionSetOption", h_nssl_SetOpt)
SSC("nnsslConnectionSetSessionCacheMode", h_nssl_SetCache) SSC("nnsslConnectionSetSocketDescriptor", h_nssl_SetSock)
SSC("nnsslConnectionSetVerifyOption", h_nssl_SetVerify) SSC("nnsslConnectionWrite", h_nssl_Write)
SSC("nnsslContextDestroy", h_nssl_CtxDestroy) SSC("nnsslContextGetContextId", h_nssl_CtxGetId)
