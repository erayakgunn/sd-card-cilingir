/* sdbus_cid.c — Pi SD yuvasi uzerinden SD-bus ham komutlari (MMC_IOC_CMD)
 *
 * Build : gcc -O2 -o sdbus_cid sdbus_cid.c
 * Usage : sudo ./sdbus_cid /dev/mmcblk0 readcid
 *         sudo ./sdbus_cid /dev/mmcblk0 program <16 bayt hex CID>
 *
 * Not: karti mount edilmemis tut. Program sonrasi karti cikar-tak,
 * sonra: cat /sys/block/mmcblk0/device/cid  (kernel yeniden sayar)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/mmc/ioctl.h>

/* linux/mmc/core.h bayraklari */
#ifndef MMC_RSP_PRESENT
#define MMC_RSP_PRESENT (1 << 0)
#define MMC_RSP_136     (1 << 1)
#define MMC_RSP_CRC     (1 << 4)
#define MMC_CMD_AC      (0 << 5)
#define MMC_CMD_ADTC    (1 << 5)
#endif

static int send_cmd(int fd, unsigned opcode, unsigned arg, unsigned flags,
                    unsigned char *data, unsigned blksz, int is_write,
                    unsigned resp[4])
{
    struct mmc_ioc_cmd ic;
    memset(&ic, 0, sizeof(ic));
    ic.opcode = opcode;
    ic.arg = arg;
    ic.flags = flags;
    if (data) {
        ic.data_ptr = (uintptr_t)data;
        ic.blocks = 1;
        ic.blksz = blksz;
        ic.write_flag = is_write ? 1 : 0;
    }
    if (ioctl(fd, MMC_IOC_CMD, &ic) < 0) {
        fprintf(stderr, "CMD%d ioctl hatasi: %s\n", opcode, strerror(errno));
        return -1;
    }
    if (resp && (ic.flags & MMC_RSP_PRESENT)) {
        for (int i = 0; i < 4; i++)
            resp[i] = ic.response[i];
    }
    return 0;
}

static unsigned rca_from_sys(const char *dev)
{
    /* /dev/mmcblk0 -> /sys/block/mmcblk0/device -> mmc0:0001 (rca hex) */
    char path[256], link[256];
    snprintf(path, sizeof(path), "/sys/block/%s/device", strrchr(dev, '/') + 1);
    ssize_t n = readlink(path, link, sizeof(link) - 1);
    if (n <= 0) return 0;
    link[n] = 0;
    char *colon = strrchr(link, ':');
    if (!colon) return 0;
    return (unsigned)strtoul(colon + 1, NULL, 16);
}

static void print_resp136(const unsigned resp[4])
{
    /* R2 136-bit: linux response[3..0] = ilk gelen 32-bit sozcukler? iki yorum da bas */
    printf("resp raw: %08X %08X %08X %08X\n", resp[3], resp[2], resp[1], resp[0]);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "kullanim: %s /dev/mmcblkN readcid | program <hex16>\n", argv[0]);
        return 2;
    }
    int fd = open(argv[1], O_RDWR);
    if (fd < 0) {
        perror("open");
        return 1;
    }
    unsigned rca = rca_from_sys(argv[1]);
    unsigned resp[4] = {0};
    unsigned char *buf = NULL;
    if (posix_memalign((void **)&buf, 512, 512) != 0) return 1;

    if (strcmp(argv[2], "readcid") == 0) {
        /* sec (CMD7) sonra CID oku (CMD10, R2) */
        if (send_cmd(fd, 7, rca << 16, MMC_RSP_PRESENT | MMC_RSP_CRC | MMC_CMD_AC,
                     NULL, 0, 0, resp) < 0) return 1;
        if (send_cmd(fd, 10, rca << 16, MMC_RSP_PRESENT | MMC_RSP_136 | MMC_RSP_CRC | MMC_CMD_AC,
                     NULL, 0, 0, resp) < 0) return 1;
        printf("CMD10 (CID) ");
        print_resp136(resp);
    } else if (strcmp(argv[2], "program") == 0 && argc >= 4) {
        if (strlen(argv[3]) != 32) {
            fprintf(stderr, "CID 16 bayt (32 hex karakter) olmali\n");
            return 2;
        }
        unsigned char cid[16];
        for (int i = 0; i < 16; i++) {
            char h[3] = {argv[3][2 * i], argv[3][2 * i + 1], 0};
            cid[i] = (unsigned char)strtoul(h, NULL, 16);
        }
        /* sec (CMD7) */
        if (send_cmd(fd, 7, rca << 16, MMC_RSP_PRESENT | MMC_RSP_CRC | MMC_CMD_AC,
                     NULL, 0, 0, resp) < 0) return 1;
        /* CMD26 PROGRAM_CID: R1 + 16 bayt veri (DAT) — CRC16'yi SDHCI donanimi ekler */
        memset(buf, 0, 512);
        memcpy(buf, cid, 16);
        if (send_cmd(fd, 26, 0, MMC_RSP_PRESENT | MMC_RSP_CRC | MMC_CMD_ADTC,
                     buf, 16, 1, resp) < 0) {
            fprintf(stderr, "CMD26 basarisiz\n");
            return 1;
        }
        printf("CMD26 r1 = %08X\n", resp[0]);
        /* geri oku */
        if (send_cmd(fd, 10, rca << 16, MMC_RSP_PRESENT | MMC_RSP_136 | MMC_RSP_CRC | MMC_CMD_AC,
                     NULL, 0, 0, resp) < 0) return 1;
        printf("CMD10 (readback) ");
        print_resp136(resp);
    } else {
        fprintf(stderr, "bilinmeyen komut\n");
        return 2;
    }

    free(buf);
    close(fd);
    return 0;
}
