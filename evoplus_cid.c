/* Samsung Evo CID backdoor, native Linux SD/MMC host only.
 * Build: gcc -O2 -Wall -o evoplus_cid evoplus_cid.c
 * Use:   sudo ./evoplus_cid /dev/mmcblk0 <32-hex-CID>
 *
 * This is controller-specific. It is not a generic CID writer.
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/mmc/ioctl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define MMC_RSP_PRESENT (1 << 0)
#define MMC_RSP_136     (1 << 1)
#define MMC_RSP_CRC     (1 << 2)
#define MMC_RSP_BUSY    (1 << 3)
#define MMC_RSP_OPCODE  (1 << 4)
#define MMC_CMD_AC      (0 << 5)
#define MMC_CMD_ADTC    (1 << 5)
#define MMC_RSP_R1      (MMC_RSP_PRESENT | MMC_RSP_CRC | MMC_RSP_OPCODE)
#define MMC_RSP_R1B     (MMC_RSP_R1 | MMC_RSP_BUSY)
#define MMC_RSP_SPI_R1  (1 << 7)

static int acmd(int fd, unsigned opcode, unsigned arg, unsigned flags,
                unsigned char *data, unsigned blksz, int write_flag,
                struct mmc_ioc_cmd *out)
{
    struct mmc_ioc_cmd c;
    memset(&c, 0, sizeof(c));
    c.opcode = opcode;
    c.arg = arg;
    c.flags = flags;
    c.write_flag = write_flag;
    c.data_timeout_ns = 0x10000000;
    if (data) {
        c.blksz = blksz;
        c.blocks = 1;
        c.data_ptr = (uintptr_t)data;
    }
    if (ioctl(fd, MMC_IOC_CMD, &c) < 0) {
        fprintf(stderr, "CMD%u ioctl: %s\n", opcode, strerror(errno));
        return -1;
    }
    if (out) *out = c;
    return 0;
}

static int vendor(int fd, unsigned arg)
{
    struct mmc_ioc_cmd r;
    int rc = acmd(fd, 62, arg, MMC_RSP_R1B | MMC_CMD_AC, NULL, 0, 1, &r);
    if (rc == 0)
        printf("CMD62 %#010x response=%08x\n", arg, r.response[0]);
    return rc;
}

static unsigned char crc7(const unsigned char *data, int len)
{
    unsigned char crc = 0;
    for (int n = 0; n <= len; n++) {
        unsigned char d = n == len ? 0 : data[n];
        int bits = n == len ? 7 : 8;
        while (bits--) {
            crc = (unsigned char)((crc << 1) | ((d & 0x80) ? 1 : 0));
            if (crc & 0x80) crc ^= 0x09;
            d <<= 1;
        }
        crc &= 0x7f;
    }
    return (unsigned char)((crc << 1) | 1);
}

static int parse_cid(const char *s, unsigned char cid[16])
{
    size_t n = strlen(s);
    if (n != 30 && n != 32) return -1;
    memset(cid, 0, 16);
    for (int i = 0; i < (int)(n / 2); i++) {
        unsigned v;
        if (sscanf(s + i * 2, "%2x", &v) != 1) return -1;
        cid[i] = (unsigned char)v;
    }
    if (n == 30) cid[15] = crc7(cid, 15);
    return 0;
}

int main(int argc, char **argv)
{
    unsigned char cid[16];
    if (argc != 3 || parse_cid(argv[2], cid)) {
        fprintf(stderr, "usage: sudo %s /dev/mmcblkN <30-or-32-hex-CID>\n", argv[0]);
        return 2;
    }
    int fd = open(argv[1], O_RDWR);
    if (fd < 0) { perror("open"); return 1; }

    printf("target CID: ");
    for (int i = 0; i < 16; i++) printf("%02X", cid[i]);
    puts("");

    if (vendor(fd, 0xEFAC62EC) || vendor(fd, 0xEF50)) {
        fprintf(stderr, "Samsung vendor unlock reddedildi; CID yazilmadi.\n");
        close(fd);
        return 1;
    }

    /* Published Samsung sequence: confirm Smart Report with CMD17 first. */
    unsigned char *block = aligned_alloc(512, 512);
    if (!block) { perror("aligned_alloc"); close(fd); return 1; }
    struct mmc_ioc_cmd r;
    if (acmd(fd, 17, 0, MMC_RSP_R1 | MMC_CMD_ADTC, block, 512, 0, &r)) {
        fprintf(stderr, "CMD17 Smart Report basarisiz; CMD26 gonderilmedi.\n");
        free(block); close(fd); return 1;
    }
    printf("CMD17 Smart Report okundu.\n");

    unsigned char data[16];
    memcpy(data, cid, 16);
    if (acmd(fd, 26, 0, MMC_RSP_SPI_R1 | MMC_RSP_R1 | MMC_CMD_ADTC,
             data, 16, 1, &r)) {
        fprintf(stderr, "CMD26 basarisiz.\n");
        free(block); close(fd); return 1;
    }
    printf("CMD26 kabul edildi; karti cikarip takarak CID'i dogrula.\n");
    free(block);
    close(fd);
    return 0;
}
