/*
 * virtio_net_dma_test.c - Bare-metal virtio-net DMA + IRQ test
 *
 * This firmware negotiates a modern virtio-mmio network device, configures
 * both the receive and transmit virtqueues, transmits one frame to the host,
 * waits for the TX used ring, then validates that an inbound host frame is
 * DMA-written into guest memory and signalled via a machine external interrupt.
 */

#include <stdint.h>

void *memcpy(void *dst, const void *src, unsigned long len)
{
    unsigned char *dst_bytes = (unsigned char *)dst;
    const unsigned char *src_bytes = (const unsigned char *)src;

    for (unsigned long index = 0; index < len; index++) {
        dst_bytes[index] = src_bytes[index];
    }

    return dst;
}

void *memset(void *dst, int value, unsigned long len)
{
    unsigned char *dst_bytes = (unsigned char *)dst;

    for (unsigned long index = 0; index < len; index++) {
        dst_bytes[index] = (unsigned char)value;
    }

    return dst;
}

#define UART_BASE               0x60100000UL
#define UART_THR                0x00
#define UART_LSR                0x05
#define UART_LSR_THRE           0x20

#define PLIC_BASE               0x0c000000UL
#define PLIC_PRIORITY(n)        (*(volatile uint32_t *)(PLIC_BASE + 4U * (n)))
#define PLIC_ENABLE             (*(volatile uint32_t *)(PLIC_BASE + 0x2000U))
#define PLIC_THRESHOLD          (*(volatile uint32_t *)(PLIC_BASE + 0x200000U))
#define PLIC_CLAIM              (*(volatile uint32_t *)(PLIC_BASE + 0x200004U))

#define PLIC_SOURCE_VIRTIO      2U

#define MIE_MEIE                (1UL << 11)
#define MSTATUS_MIE             (1UL << 3)

#define VIRTIO_BASE             0x60200000UL
#define VIRTIO_STRIDE           0x1000UL
#define VIRTIO_COUNT            8U
#define VIRTIO_DEVICE_NET       1U

#define VIRTIO_MMIO_MAGIC_VALUE         0x000
#define VIRTIO_MMIO_VERSION             0x004
#define VIRTIO_MMIO_DEVICE_ID           0x008
#define VIRTIO_MMIO_VENDOR_ID           0x00c
#define VIRTIO_MMIO_DEVICE_FEATURES     0x010
#define VIRTIO_MMIO_DEVICE_FEATURES_SEL 0x014
#define VIRTIO_MMIO_DRIVER_FEATURES     0x020
#define VIRTIO_MMIO_DRIVER_FEATURES_SEL 0x024
#define VIRTIO_MMIO_QUEUE_SEL           0x030
#define VIRTIO_MMIO_QUEUE_NUM_MAX       0x034
#define VIRTIO_MMIO_QUEUE_NUM           0x038
#define VIRTIO_MMIO_QUEUE_READY         0x044
#define VIRTIO_MMIO_QUEUE_NOTIFY        0x050
#define VIRTIO_MMIO_INTERRUPT_STATUS    0x060
#define VIRTIO_MMIO_INTERRUPT_ACK       0x064
#define VIRTIO_MMIO_STATUS              0x070
#define VIRTIO_MMIO_QUEUE_DESC_LOW      0x080
#define VIRTIO_MMIO_QUEUE_DESC_HIGH     0x084
#define VIRTIO_MMIO_QUEUE_AVAIL_LOW     0x090
#define VIRTIO_MMIO_QUEUE_AVAIL_HIGH    0x094
#define VIRTIO_MMIO_QUEUE_USED_LOW      0x0a0
#define VIRTIO_MMIO_QUEUE_USED_HIGH     0x0a4
#define VIRTIO_MMIO_CONFIG              0x100

#define VIRTIO_CONFIG_S_ACKNOWLEDGE     0x01
#define VIRTIO_CONFIG_S_DRIVER          0x02
#define VIRTIO_CONFIG_S_DRIVER_OK       0x04
#define VIRTIO_CONFIG_S_FEATURES_OK     0x08
#define VIRTIO_CONFIG_S_FAILED          0x80

#define VIRTIO_F_VERSION_1              32U
#define VIRTIO_NET_F_MAC                5U

#define VIRTQ_DESC_F_NEXT               1U
#define VIRTQ_DESC_F_WRITE              2U

#define RX_QUEUE_INDEX                  0U
#define TX_QUEUE_INDEX                  1U
#define QUEUE_SIZE                      8U

#define RX_DESC_ADDR                    0x80010000UL
#define RX_AVAIL_ADDR                   0x80011000UL
#define RX_USED_ADDR                    0x80012000UL
#define TX_DESC_ADDR                    0x80013000UL
#define TX_AVAIL_ADDR                   0x80014000UL
#define TX_USED_ADDR                    0x80015000UL
#define RX_BUF_ADDR                     0x80016000UL
#define TX_HDR_ADDR                     0x80017000UL
#define TX_FRAME_ADDR                   0x80018000UL
#define VIRTIO_BASE_HOLDER_ADDR         0x80019000UL
#define STATE_BASE_ADDR                 0x8001a000UL
#define CACHE_SCRUB_ADDR                0x80100000UL
#define CACHE_SCRUB_SIZE                (128U * 1024U)

#define ETH_HEADER_LEN                  14U
#define ETH_PAYLOAD_LEN                 46U
#define ETH_FRAME_LEN                   (ETH_HEADER_LEN + ETH_PAYLOAD_LEN)
#define RX_BUFFER_SIZE                  2048U

#define TX_MARKER_LEN                   24U
#define RX_MARKER_LEN                   23U

#define IRQ_COUNT                       (*(volatile uint32_t *)(STATE_BASE_ADDR + 0x00U))
#define IRQ_STATUS_ACCUM                (*(volatile uint32_t *)(STATE_BASE_ADDR + 0x04U))
#define IRQ_BAD_SOURCE                  (*(volatile uint32_t *)(STATE_BASE_ADDR + 0x08U))
#define LAST_IRQ_SOURCE                 (*(volatile uint32_t *)(STATE_BASE_ADDR + 0x0cU))
#define LAST_IRQ_STATUS                 (*(volatile uint32_t *)(STATE_BASE_ADDR + 0x10U))

struct virtq_desc {
    uint64_t addr;
    uint32_t len;
    uint16_t flags;
    uint16_t next;
};

struct virtq_avail {
    uint16_t flags;
    uint16_t idx;
    uint16_t ring[QUEUE_SIZE];
    uint16_t used_event;
};

struct virtq_used_elem {
    uint32_t id;
    uint32_t len;
};

struct virtq_used {
    uint16_t flags;
    uint16_t idx;
    struct virtq_used_elem ring[QUEUE_SIZE];
    uint16_t avail_event;
};

struct virtio_net_hdr {
    uint8_t flags;
    uint8_t gso_type;
    uint16_t hdr_len;
    uint16_t gso_size;
    uint16_t csum_start;
    uint16_t csum_offset;
};

struct virtio_net_rx_hdr {
    struct virtio_net_hdr hdr;
    uint16_t num_buffers;
};

static volatile uint8_t *const uart = (volatile uint8_t *)UART_BASE;
static volatile struct virtq_desc *const rx_desc =
    (volatile struct virtq_desc *)RX_DESC_ADDR;
static volatile struct virtq_avail *const rx_avail =
    (volatile struct virtq_avail *)RX_AVAIL_ADDR;
static volatile struct virtq_used *const rx_used =
    (volatile struct virtq_used *)RX_USED_ADDR;
static volatile uint8_t *const rx_buf = (volatile uint8_t *)RX_BUF_ADDR;
static volatile struct virtq_desc *const tx_desc =
    (volatile struct virtq_desc *)TX_DESC_ADDR;
static volatile struct virtq_avail *const tx_avail =
    (volatile struct virtq_avail *)TX_AVAIL_ADDR;
static volatile struct virtq_used *const tx_used =
    (volatile struct virtq_used *)TX_USED_ADDR;
static volatile struct virtio_net_hdr *const tx_hdr =
    (volatile struct virtio_net_hdr *)TX_HDR_ADDR;
static volatile uint8_t *const tx_frame = (volatile uint8_t *)TX_FRAME_ADDR;
static volatile uint64_t *const cache_scrub =
    (volatile uint64_t *)CACHE_SCRUB_ADDR;

static inline void fence_rw_rw(void)
{
    asm volatile("fence rw, rw" ::: "memory");
}

static inline void csrw_mtvec(uintptr_t value)
{
    asm volatile("csrw mtvec, %0" :: "r"(value));
}

static inline void csrs_mie(uint64_t value)
{
    asm volatile("csrs mie, %0" :: "r"(value));
}

static inline void csrs_mstatus(uint64_t value)
{
    asm volatile("csrs mstatus, %0" :: "r"(value));
}

static inline uint64_t csrr_mcause(void)
{
    uint64_t value;
    asm volatile("csrr %0, mcause" : "=r"(value));
    return value;
}

extern void _trap_entry(void);

#define virtio_base (*(volatile uintptr_t *)VIRTIO_BASE_HOLDER_ADDR)

static inline uint32_t mmio_read32(uintptr_t addr)
{
    return *(volatile uint32_t *)addr;
}

static inline void mmio_write32(uintptr_t addr, uint32_t value)
{
    *(volatile uint32_t *)addr = value;
}

static void uart_putc(char ch)
{
    while (!(uart[UART_LSR] & UART_LSR_THRE)) {
    }
    uart[UART_THR] = (uint8_t)ch;
}

static void uart_puts(const char *str)
{
    while (*str) {
        uart_putc(*str++);
    }
}

static void uart_put_hex32(uint32_t value)
{
    static const char hex[] = "0123456789abcdef";
    for (int shift = 28; shift >= 0; shift -= 4) {
        uart_putc(hex[(value >> shift) & 0xf]);
    }
}

static void uart_put_hex64(uint64_t value)
{
    uart_put_hex32((uint32_t)(value >> 32));
    uart_put_hex32((uint32_t)value);
}

static void fail(const char *reason, uint32_t value)
{
    uart_puts("FAIL ");
    uart_puts(reason);
    uart_putc('=');
    uart_put_hex32(value);
    uart_puts("\r\n");
    if (virtio_base != 0) {
        mmio_write32(virtio_base + VIRTIO_MMIO_STATUS,
                     VIRTIO_CONFIG_S_FAILED);
    }
    for (;;) {
    }
}

static uintptr_t find_virtio_net(void)
{
    for (uint32_t slot = 0; slot < VIRTIO_COUNT; slot++) {
        uintptr_t base = VIRTIO_BASE + slot * VIRTIO_STRIDE;
        if (mmio_read32(base + VIRTIO_MMIO_MAGIC_VALUE) != 0x74726976) {
            continue;
        }
        if (mmio_read32(base + VIRTIO_MMIO_DEVICE_ID) == VIRTIO_DEVICE_NET) {
            return base;
        }
    }
    return 0;
}

static uint32_t virtio_get_features_sel(uint32_t selector)
{
    mmio_write32(virtio_base + VIRTIO_MMIO_DEVICE_FEATURES_SEL, selector);
    return mmio_read32(virtio_base + VIRTIO_MMIO_DEVICE_FEATURES);
}

static void virtio_set_features_sel(uint32_t selector, uint32_t value)
{
    mmio_write32(virtio_base + VIRTIO_MMIO_DRIVER_FEATURES_SEL, selector);
    mmio_write32(virtio_base + VIRTIO_MMIO_DRIVER_FEATURES, value);
}

static void clear_region(volatile uint8_t *addr, uint32_t size)
{
    for (uint32_t index = 0; index < size; index++) {
        addr[index] = 0;
    }
}

static void cache_evict(void)
{
    volatile uint64_t sink = 0;
    uint32_t words = CACHE_SCRUB_SIZE / sizeof(uint64_t);

    for (uint32_t index = 0; index < words; index += 8) {
        cache_scrub[index] = (uint64_t)index;
        sink ^= cache_scrub[index];
    }

    asm volatile("" :: "r"(sink) : "memory");
}

static void build_frame(const uint8_t mac[6])
{
    static const uint8_t payload[ETH_PAYLOAD_LEN] = {
        'R', 'T', 'L', '-', 'V', 'I', 'R', 'T', 'I', 'O', '-', 'N', 'E', 'T',
        '-', 'D', 'M', 'A', '-', 'T', 'E', 'S', 'T', '-', '0', '1', '2', '3',
        '4', '5', '6', '7', '8', '9', '-', 'A', 'B', 'C', 'D', 'E', 'F', 'G',
        'H', 'I', 'J', 'K'
    };

    for (uint32_t index = 0; index < 6; index++) {
        tx_frame[index] = 0xff;
        tx_frame[6 + index] = mac[index];
    }

    tx_frame[12] = 0x88;
    tx_frame[13] = 0xb5;

    for (uint32_t index = 0; index < ETH_PAYLOAD_LEN; index++) {
        tx_frame[ETH_HEADER_LEN + index] = payload[index];
    }
}

static void build_expected_rx_frame(const uint8_t mac[6], uint8_t *frame)
{
    static const uint8_t payload[ETH_PAYLOAD_LEN] = {
        'R', 'T', 'L', '-', 'V', 'I', 'R', 'T', 'I', 'O', '-', 'N', 'E', 'T',
        '-', 'R', 'X', '-', 'I', 'R', 'Q', '-', 'T', 'E', 'S', 'T', '-', '0',
        '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', 'H', 'O', 'S', 'T',
        '-', 'R', 'X', '!'
    };
    static const uint8_t src_mac[6] = { 0x02, 0xaa, 0xbb, 0xcc, 0xdd, 0xee };

    for (uint32_t index = 0; index < 6; index++) {
        frame[index] = mac[index];
        frame[6 + index] = src_mac[index];
    }

    frame[12] = 0x88;
    frame[13] = 0xb5;

    for (uint32_t index = 0; index < ETH_PAYLOAD_LEN; index++) {
        frame[ETH_HEADER_LEN + index] = payload[index];
    }
}

static void setup_queue(uint32_t queue_index,
                        uintptr_t desc_addr,
                        uintptr_t avail_addr,
                        uintptr_t used_addr)
{
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_SEL, queue_index);

    if (mmio_read32(virtio_base + VIRTIO_MMIO_QUEUE_NUM_MAX) < QUEUE_SIZE) {
        fail("QNUM", mmio_read32(virtio_base + VIRTIO_MMIO_QUEUE_NUM_MAX));
    }

    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_NUM, QUEUE_SIZE);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_DESC_LOW, desc_addr);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_DESC_HIGH, 0);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_AVAIL_LOW, avail_addr);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_AVAIL_HIGH, 0);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_USED_LOW, used_addr);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_USED_HIGH, 0);
    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_READY, 1);

    if (mmio_read32(virtio_base + VIRTIO_MMIO_QUEUE_READY) != 1) {
        fail("QREADY", mmio_read32(virtio_base + VIRTIO_MMIO_QUEUE_READY));
    }
}

static void setup_rx_queue(void)
{
    clear_region((volatile uint8_t *)RX_DESC_ADDR,
                 sizeof(struct virtq_desc) * QUEUE_SIZE);
    clear_region((volatile uint8_t *)RX_AVAIL_ADDR, sizeof(struct virtq_avail));
    clear_region((volatile uint8_t *)RX_USED_ADDR, sizeof(struct virtq_used));
    clear_region((volatile uint8_t *)RX_BUF_ADDR, RX_BUFFER_SIZE);

    setup_queue(RX_QUEUE_INDEX, RX_DESC_ADDR, RX_AVAIL_ADDR, RX_USED_ADDR);
}

static void setup_tx_queue(void)
{
    clear_region((volatile uint8_t *)TX_DESC_ADDR,
                 sizeof(struct virtq_desc) * QUEUE_SIZE);
    clear_region((volatile uint8_t *)TX_AVAIL_ADDR, sizeof(struct virtq_avail));
    clear_region((volatile uint8_t *)TX_USED_ADDR, sizeof(struct virtq_used));
    clear_region((volatile uint8_t *)TX_HDR_ADDR, sizeof(struct virtio_net_hdr));

    setup_queue(TX_QUEUE_INDEX, TX_DESC_ADDR, TX_AVAIL_ADDR, TX_USED_ADDR);
}

static void submit_rx(void)
{
    rx_desc[0].addr = RX_BUF_ADDR;
    rx_desc[0].len = RX_BUFFER_SIZE;
    rx_desc[0].flags = VIRTQ_DESC_F_WRITE;
    rx_desc[0].next = 0;

    rx_avail->ring[0] = 0;
    fence_rw_rw();
    rx_avail->idx = 1;
    fence_rw_rw();
    cache_evict();

    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_NOTIFY, RX_QUEUE_INDEX);
}

static void submit_tx(void)
{
    tx_desc[0].addr = TX_HDR_ADDR;
    tx_desc[0].len = sizeof(struct virtio_net_hdr);
    tx_desc[0].flags = VIRTQ_DESC_F_NEXT;
    tx_desc[0].next = 1;

    tx_desc[1].addr = TX_FRAME_ADDR;
    tx_desc[1].len = ETH_FRAME_LEN;
    tx_desc[1].flags = 0;
    tx_desc[1].next = 0;

    tx_avail->ring[0] = 0;
    fence_rw_rw();
    tx_avail->idx = 1;
    fence_rw_rw();
    cache_evict();

    mmio_write32(virtio_base + VIRTIO_MMIO_QUEUE_NOTIFY, TX_QUEUE_INDEX);
}

static void setup_interrupts(void)
{
    IRQ_COUNT = 0;
    IRQ_STATUS_ACCUM = 0;
    IRQ_BAD_SOURCE = 0;
    LAST_IRQ_SOURCE = 0;
    LAST_IRQ_STATUS = 0;

    csrw_mtvec((uintptr_t)_trap_entry);
    PLIC_PRIORITY(PLIC_SOURCE_VIRTIO) = 1;
    PLIC_ENABLE = (1U << PLIC_SOURCE_VIRTIO);
    PLIC_THRESHOLD = 0;
    csrs_mie(MIE_MEIE);
    csrs_mstatus(MSTATUS_MIE);
}

static void virtio_init(uint8_t mac[6])
{
    uint32_t dev_features0;
    uint32_t dev_features1;
    uint32_t status;

    virtio_base = find_virtio_net();
    if (virtio_base == 0) {
        fail("NODEV", 0);
    }

    if (mmio_read32(virtio_base + VIRTIO_MMIO_VERSION) != 2) {
        fail("VER", mmio_read32(virtio_base + VIRTIO_MMIO_VERSION));
    }

    if (mmio_read32(virtio_base + VIRTIO_MMIO_DEVICE_ID) != VIRTIO_DEVICE_NET) {
        fail("DEVID", mmio_read32(virtio_base + VIRTIO_MMIO_DEVICE_ID));
    }

    mmio_write32(virtio_base + VIRTIO_MMIO_STATUS, 0);
    status = VIRTIO_CONFIG_S_ACKNOWLEDGE | VIRTIO_CONFIG_S_DRIVER;
    mmio_write32(virtio_base + VIRTIO_MMIO_STATUS, status);

    dev_features0 = virtio_get_features_sel(0);
    dev_features1 = virtio_get_features_sel(1);

    if ((dev_features1 & 0x1U) == 0) {
        fail("FEAT32", dev_features1);
    }

    virtio_set_features_sel(0, 0);
    virtio_set_features_sel(1, 0x1U);

    if (dev_features0 & (1U << VIRTIO_NET_F_MAC)) {
        for (uint32_t index = 0; index < 6; index++) {
            mac[index] = *(volatile uint8_t *)(virtio_base + VIRTIO_MMIO_CONFIG + index);
        }
    } else {
        mac[0] = 0x02;
        mac[1] = 0x12;
        mac[2] = 0x34;
        mac[3] = 0x56;
        mac[4] = 0x78;
        mac[5] = 0x9a;
    }

    status |= VIRTIO_CONFIG_S_FEATURES_OK;
    mmio_write32(virtio_base + VIRTIO_MMIO_STATUS, status);
    status = mmio_read32(virtio_base + VIRTIO_MMIO_STATUS);
    if ((status & VIRTIO_CONFIG_S_FEATURES_OK) == 0) {
        fail("FEATOK", status);
    }

    setup_rx_queue();
    setup_tx_queue();

    status |= VIRTIO_CONFIG_S_DRIVER_OK;
    mmio_write32(virtio_base + VIRTIO_MMIO_STATUS, status);
}

void trap_handler(void)
{
    uint64_t mcause = csrr_mcause();
    uint32_t source;
    uint32_t status = 0;

    if ((int64_t)mcause >= 0) {
        return;
    }

    if ((mcause & 0xfffU) != 11U) {
        return;
    }

    source = PLIC_CLAIM;
    LAST_IRQ_SOURCE = source;
    if (source == 0) {
        return;
    }

    if (source != PLIC_SOURCE_VIRTIO) {
        IRQ_BAD_SOURCE = source;
    } else if (virtio_base != 0) {
        status = mmio_read32(virtio_base + VIRTIO_MMIO_INTERRUPT_STATUS);
        if (status != 0) {
            mmio_write32(virtio_base + VIRTIO_MMIO_INTERRUPT_ACK, status);
        }
    }

    LAST_IRQ_STATUS = status;
    IRQ_STATUS_ACCUM |= status;
    IRQ_COUNT += 1;
    PLIC_CLAIM = source;
}

void main(void)
{
    uint8_t mac[6];
    uint8_t expected_rx[ETH_FRAME_LEN];
    uint32_t spins = 0;
    uint32_t tx_irq_count;

    virtio_base = 0;
    uart_puts("VNET DMA TEST\r\n");
    setup_interrupts();
    virtio_init(mac);
    build_expected_rx_frame(mac, expected_rx);
    submit_rx();
    uart_puts("RX READY\r\n");
    build_frame(mac);
    submit_tx();
    uart_puts("TX SUBMITTED\r\n");

    while (tx_used->idx == 0) {
        if ((spins & 0xfffU) == 0) {
            cache_evict();
        }
        if (++spins == 200000000U) {
            fail("TIMEOUT", tx_used->idx);
        }
    }

    fence_rw_rw();

    if (tx_used->ring[0].id != 0) {
        fail("USEDID", tx_used->ring[0].id);
    }

    uart_puts("USED LEN=");
    uart_put_hex32(tx_used->ring[0].len);
    uart_puts("\r\nTX DONE\r\n");

    tx_irq_count = IRQ_COUNT;
    spins = 0;
    while (rx_used->idx == 0) {
        if ((spins & 0xfffU) == 0) {
            cache_evict();
        }
        if (++spins == 200000000U) {
            fail("RXTMO", rx_used->idx);
        }
    }

    cache_evict();
    fence_rw_rw();

    if (IRQ_BAD_SOURCE != 0) {
        fail("IRQSRC", IRQ_BAD_SOURCE);
    }
    if (IRQ_COUNT <= tx_irq_count) {
        fail("NOIRQ", IRQ_COUNT);
    }
    if (LAST_IRQ_SOURCE != PLIC_SOURCE_VIRTIO) {
        fail("LASTSRC", LAST_IRQ_SOURCE);
    }
    if ((LAST_IRQ_STATUS & 0x1U) == 0) {
        fail("IRQSTS", LAST_IRQ_STATUS);
    }
    if (rx_used->ring[0].id != 0) {
        fail("RXUSEDID", rx_used->ring[0].id);
    }
    if (rx_used->ring[0].len < (sizeof(struct virtio_net_rx_hdr) + ETH_FRAME_LEN)) {
        fail("RXLEN", rx_used->ring[0].len);
    }

    volatile uint8_t *rx_frame = rx_buf + sizeof(struct virtio_net_rx_hdr);
    for (uint32_t index = 0; index < ETH_FRAME_LEN; index++) {
        if (rx_frame[index] != expected_rx[index]) {
            fail("RXDATA", index);
        }
    }

    uart_puts("RX LEN=");
    uart_put_hex32(rx_used->ring[0].len);
    uart_puts("\r\nIRQ COUNT=");
    uart_put_hex32(IRQ_COUNT);
    uart_puts("\r\nLAST MCAUSE=");
    uart_put_hex64(csrr_mcause());
    uart_puts("\r\nPASS\r\n");

    for (;;) {
    }
}