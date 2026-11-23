/* Verifier-only LD_PRELOAD shim: shifts this process's wall clock (CLOCK_REALTIME and friends) by
 * HOST_CLOCK_SHIFT_MS milliseconds, the way a host whose clock is off reads it. Monotonic clocks,
 * sleeps and timeouts are untouched. Built from this file with:
 *   gcc -O2 -shared -fPIC -Wall -o hostclock.so hostclock.c -ldl   (python:3.11.12-bookworm) */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

static long long shift_ns;
static int ready;
static int (*real_clock_gettime)(clockid_t, struct timespec *);

static void init(void) {
    const char *s = getenv("HOST_CLOCK_SHIFT_MS");
    shift_ns = s ? atoll(s) * 1000000LL : 0;
    real_clock_gettime = (int (*)(clockid_t, struct timespec *))dlsym(RTLD_NEXT, "clock_gettime");
    ready = 1;
}

static int is_wall(clockid_t c) {
    return c == CLOCK_REALTIME || c == CLOCK_REALTIME_COARSE || c == CLOCK_TAI;
}

int clock_gettime(clockid_t c, struct timespec *ts) {
    if (!ready) init();
    int rc = real_clock_gettime(c, ts);
    if (rc == 0 && ts && is_wall(c) && shift_ns) {
        long long ns = (long long)ts->tv_sec * 1000000000LL + ts->tv_nsec + shift_ns;
        ts->tv_sec = (time_t)(ns / 1000000000LL);
        ts->tv_nsec = (long)(ns % 1000000000LL);
        if (ts->tv_nsec < 0) { ts->tv_nsec += 1000000000L; ts->tv_sec -= 1; }
    }
    return rc;
}

int gettimeofday(struct timeval *tv, void *tz) {
    (void)tz;
    struct timespec ts;
    int rc = clock_gettime(CLOCK_REALTIME, &ts);
    if (rc == 0 && tv) { tv->tv_sec = ts.tv_sec; tv->tv_usec = ts.tv_nsec / 1000; }
    return rc;
}

time_t time(time_t *out) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    if (out) *out = ts.tv_sec;
    return ts.tv_sec;
}
