/*
 * solver.c - fast single-threaded SHA-256 PoW solver for rpow2.com.
 *
 * Usage: solver <prefix_hex> <difficulty_bits> <start_u64> <stride_u64>
 *
 * Finds u64 nonce (>= start, stepping by stride) such that:
 *   sha256(prefix || nonce.to_bytes(8, 'little'))
 * has at least <difficulty_bits> trailing zero bits (big-endian integer view).
 * On success prints "<nonce>\n<hashes>\n" to stdout and exits 0.
 * On SIGINT/SIGTERM (or exhaustion of u64) exits non-zero with no output.
 *
 * Single-block optimization: prefix + 8-byte nonce fits in one 64-byte
 * SHA-256 block (max prefix supported = 47 bytes; live prefix is 16). The
 * message length and padding are precomputed once.
 *
 * Public-domain SHA-256, styled after Brad Conte's reference code.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>

static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

static volatile sig_atomic_t g_stop = 0;
static void on_sig(int s) { (void)s; g_stop = 1; }

#define ROTR(x,n) (((x) >> (n)) | ((x) << (32 - (n))))
#define CH(x,y,z)  (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x,y,z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define BSIG0(x) (ROTR(x,2)  ^ ROTR(x,13) ^ ROTR(x,22))
#define BSIG1(x) (ROTR(x,6)  ^ ROTR(x,11) ^ ROTR(x,25))
#define SSIG0(x) (ROTR(x,7)  ^ ROTR(x,18) ^ ((x) >> 3))
#define SSIG1(x) (ROTR(x,17) ^ ROTR(x,19) ^ ((x) >> 10))

static inline void sha256_block(const uint8_t blk[64], uint32_t H[8]) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)blk[i*4]   << 24) |
               ((uint32_t)blk[i*4+1] << 16) |
               ((uint32_t)blk[i*4+2] <<  8) |
                (uint32_t)blk[i*4+3];
    }
    for (int i = 16; i < 64; i++) {
        w[i] = SSIG1(w[i-2]) + w[i-7] + SSIG0(w[i-15]) + w[i-16];
    }
    uint32_t a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + BSIG1(e) + CH(e,f,g) + K[i] + w[i];
        uint32_t t2 = BSIG0(a) + MAJ(a,b,c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }
    H[0]+=a; H[1]+=b; H[2]+=c; H[3]+=d; H[4]+=e; H[5]+=f; H[6]+=g; H[7]+=h;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "usage: %s <prefix_hex> <bits> <start> <stride>\n", argv[0]);
        return 2;
    }
    const char *hex = argv[1];
    size_t hlen = strlen(hex);
    if (hlen == 0 || hlen % 2 != 0) {
        fprintf(stderr, "bad prefix hex\n"); return 2;
    }
    size_t plen = hlen / 2;
    /* msg = prefix || 8-byte nonce, padded SHA-256 needs msg_len <= 55 to
     * fit in one 64-byte block. So plen <= 47. Live prefix is 16. */
    if (plen > 47) { fprintf(stderr, "prefix > 47 bytes unsupported\n"); return 2; }
    uint8_t prefix[48];
    for (size_t i = 0; i < plen; i++) {
        unsigned v;
        if (sscanf(hex + 2*i, "%2x", &v) != 1) {
            fprintf(stderr, "bad hex\n"); return 2;
        }
        prefix[i] = (uint8_t)v;
    }
    int bits = atoi(argv[2]);
    if (bits < 1 || bits > 255) { fprintf(stderr, "bad bits\n"); return 2; }
    uint64_t start  = strtoull(argv[3], NULL, 10);
    uint64_t stride = strtoull(argv[4], NULL, 10);
    if (stride == 0) stride = 1;

    signal(SIGINT,  on_sig);
    signal(SIGTERM, on_sig);

    /* Build the padded block once; only the 8 nonce bytes will be rewritten
     * on each iteration. */
    uint8_t block[64];
    memset(block, 0, 64);
    memcpy(block, prefix, plen);
    /* 0x80 immediately after the 8 nonce bytes */
    block[plen + 8] = 0x80;
    /* 64-bit big-endian length in bits at bytes 56..63 */
    uint64_t bit_len = (uint64_t)(plen + 8) * 8;
    block[56] = (uint8_t)(bit_len >> 56);
    block[57] = (uint8_t)(bit_len >> 48);
    block[58] = (uint8_t)(bit_len >> 40);
    block[59] = (uint8_t)(bit_len >> 32);
    block[60] = (uint8_t)(bit_len >> 24);
    block[61] = (uint8_t)(bit_len >> 16);
    block[62] = (uint8_t)(bit_len >>  8);
    block[63] = (uint8_t)(bit_len);

    static const uint32_t IV[8] = {
        0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
        0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19
    };

    /* Decompose difficulty into (full 32-bit words that must be zero)
     * + a mask on the next higher word. H[7] is the lowest 32 bits of the
     * big-endian integer (= digest bytes 28..31). */
    int full_words = bits / 32;
    int rem        = bits % 32;
    uint32_t rem_mask = (rem == 0) ? 0u : ((1u << rem) - 1u);

    uint64_t n = start;
    uint64_t hashes = 0;

    for (;;) {
        if ((hashes & 0x3ffffULL) == 0 && g_stop) return 143;

        block[plen+0] = (uint8_t)(n);
        block[plen+1] = (uint8_t)(n >> 8);
        block[plen+2] = (uint8_t)(n >> 16);
        block[plen+3] = (uint8_t)(n >> 24);
        block[plen+4] = (uint8_t)(n >> 32);
        block[plen+5] = (uint8_t)(n >> 40);
        block[plen+6] = (uint8_t)(n >> 48);
        block[plen+7] = (uint8_t)(n >> 56);

        uint32_t H[8];
        memcpy(H, IV, sizeof(H));
        sha256_block(block, H);

        /* Fast path for bits in (0, 32]: only inspect H[7]. */
        if (full_words == 0) {
            if ((H[7] & rem_mask) == 0) {
                hashes++;
                printf("%llu\n%llu\n", (unsigned long long)n, (unsigned long long)hashes);
                fflush(stdout);
                return 0;
            }
        } else {
            int ok = 1;
            int idx = 7;
            for (int k = 0; k < full_words; k++) {
                if (H[idx] != 0) { ok = 0; break; }
                idx--;
            }
            if (ok && rem > 0 && (H[idx] & rem_mask) != 0) ok = 0;
            if (ok) {
                hashes++;
                printf("%llu\n%llu\n", (unsigned long long)n, (unsigned long long)hashes);
                fflush(stdout);
                return 0;
            }
        }

        hashes++;
        n += stride;
        /* u64 wrap-around is astronomically unreachable at 25 bits, but
         * guard anyway. */
        if (n < stride) return 4;
    }
}
