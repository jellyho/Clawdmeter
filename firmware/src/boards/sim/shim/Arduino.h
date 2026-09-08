#pragma once
// Minimal Arduino compatibility layer for the native simulator build.
// Covers only what the shared sources actually use: millis/delay, Serial,
// and the setup()/loop() entry points (driven by sim_main.cpp).
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
// MinGW's CRT has no strlcpy (on ESP32 it comes from newlib via Arduino.h).
static inline size_t strlcpy(char* dst, const char* src, size_t size) {
    size_t n = strlen(src);
    if (size) {
        size_t c = n < size - 1 ? n : size - 1;
        memcpy(dst, src, c);
        dst[c] = '\0';
    }
    return n;
}
#endif

unsigned long millis(void);
void delay(unsigned long ms);

class SimSerial {
public:
    void begin(unsigned long baud) { (void)baud; }
    int  available(void) { return 0; }
    int  read(void) { return -1; }
    size_t write(const uint8_t* buf, size_t len);
    void flush(void) { fflush(stdout); }
    void print(const char* s) { fputs(s, stdout); }
    void println(const char* s) { puts(s); }
    void println(void) { putchar('\n'); }
    void printf(const char* fmt, ...) __attribute__((format(printf, 2, 3)));
};
extern SimSerial Serial;

void setup(void);
void loop(void);
