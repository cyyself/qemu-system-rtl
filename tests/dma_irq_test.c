/*
 * dma_irq_test.c - Test DMA write + interrupt via L2 Frontend Bus
 *
 * Test flow:
 *   1. Configure PLIC for external interrupt (source 1)
 *   2. Set up M-mode trap handler (mtvec → _trap_entry in start.S)
 *   3. Enable interrupts (mie.MEIE, mstatus.MIE)
 *   4. Print "READY\r\n" to UART
 *   5. Wait for interrupt (WFI loop)
 *   6. On interrupt: claim PLIC, read DMA'd data from buffer, print to UART
 *   7. Complete PLIC claim, print "DONE\r\n"
 *
 * Build:
 *   riscv64-linux-gnu-gcc -nostdlib -nostartfiles -fno-pic \
 *       -Ttext=0x80000000 -march=rv64imafdc -mabi=lp64d -O2 \
 *       -o dma_irq_test.elf start.S dma_irq_test.c
 *   riscv64-linux-gnu-objcopy -O binary -j .text dma_irq_test.elf dma_irq_test.bin
 */

#include <stdint.h>

/* ---- Hardware addresses ---- */
#define UART_BASE       ((volatile uint8_t *)0x60100000)
#define UART_THR        0   /* Transmit Holding Register */
#define UART_LSR        5   /* Line Status Register */
#define UART_LSR_THRE   0x20

#define PLIC_BASE           ((volatile uint32_t *)0x0C000000)
#define PLIC_PRIORITY(n)    (*(volatile uint32_t *)((uintptr_t)PLIC_BASE + 4 * (n)))
#define PLIC_ENABLE         (*(volatile uint32_t *)((uintptr_t)PLIC_BASE + 0x2000))
#define PLIC_THRESHOLD      (*(volatile uint32_t *)((uintptr_t)PLIC_BASE + 0x200000))
#define PLIC_CLAIM          (*(volatile uint32_t *)((uintptr_t)PLIC_BASE + 0x200004))

#define DMA_BUF     ((volatile char *)0x80010000)
#define DMA_BUF_SIZE    64

/* ---- CSR helpers (inline asm) ---- */
#define csrw(csr, val) asm volatile("csrw " #csr ", %0" :: "r"(val))
#define csrs(csr, val) asm volatile("csrs " #csr ", %0" :: "r"(val))
#define csrr(csr) ({ unsigned long __v; \
    asm volatile("csrr %0, " #csr : "=r"(__v)); __v; })
#define wfi() asm volatile("wfi")

#define MIE_MEIE    (1UL << 11)
#define MSTATUS_MIE (1UL << 3)

/* Defined in start.S */
extern void _trap_entry(void);

/* ---- UART helpers ---- */
static void uart_putc(char c)
{
    while (!(UART_BASE[UART_LSR] & UART_LSR_THRE))
        ;
    UART_BASE[UART_THR] = c;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

/* ---- Trap handler (called from start.S _trap_entry) ---- */
void trap_handler(void)
{
    unsigned long mcause = csrr(mcause);

    /* Check interrupt bit (MSB) */
    if ((long)mcause >= 0)
        return; /* exception, not interrupt — ignore */

    unsigned long code = mcause & 0x7FFFFFFFFFFFFFFFUL;
    if (code != 11) /* 11 = Machine External Interrupt */
        return;

    /* Claim interrupt from PLIC */
    uint32_t source = PLIC_CLAIM;
    if (source == 0)
        return; /* spurious */

    /* Read DMA buffer (null-terminated) and print to UART */
    for (int i = 0; i < DMA_BUF_SIZE; i++) {
        char c = DMA_BUF[i];
        if (c == '\0')
            break;
        uart_putc(c);
    }

    uart_puts("DONE\r\n");

    /* Complete PLIC claim */
    PLIC_CLAIM = source;
}

/* ---- Main ---- */
void main(void)
{
    /* Set up trap handler */
    csrw(mtvec, _trap_entry);

    /* Configure PLIC: source 1, priority 1, enabled, threshold 0 */
    PLIC_PRIORITY(1) = 1;
    PLIC_ENABLE      = (1 << 1); /* enable source 1 */
    PLIC_THRESHOLD    = 0;

    /* Enable machine external interrupt + global MIE */
    csrs(mie, MIE_MEIE);
    csrs(mstatus, MSTATUS_MIE);

    uart_puts("READY\r\n");

    /* Wait for interrupt forever */
    for (;;)
        wfi();
}
