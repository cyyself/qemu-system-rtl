/*
 * uart_test.S - Minimal bare-metal RISC-V test
 *
 * Writes "OK\n" to a 16550 UART at 0x60100000, then loops forever.
 * This runs on Rocket-Chip with the RTL QEMU machine.
 *
 * Build:
 *   riscv64-linux-gnu-gcc -nostdlib -nostartfiles -Ttext=0x80000000 \
 *       -march=rv64imafdc -mabi=lp64d -o uart_test.elf uart_test.S
 *   riscv64-linux-gnu-objcopy -O binary uart_test.elf uart_test.bin
 */

#define UART_BASE  0x60100000
#define UART_THR   0          /* Transmit Holding Register */
#define UART_LSR   5          /* Line Status Register */
#define UART_LSR_THRE  0x20   /* THR Empty bit */

    .section .text
    .globl _start

_start:
    /* Load UART base address */
    li      a0, UART_BASE

    /* Print 'O' */
    li      a1, 'O'
    jal     uart_putc

    /* Print 'K' */
    li      a1, 'K'
    jal     uart_putc

    /* Print '\r' */
    li      a1, '\r'
    jal     uart_putc

    /* Print '\n' */
    li      a1, '\n'
    jal     uart_putc

    /* Infinite loop */
1:  wfi
    j       1b

/*
 * uart_putc: Write byte in a1 to UART at a0.
 * Polls LSR until THR is empty, then writes the byte.
 */
uart_putc:
    /* Wait for UART transmit buffer to be ready */
1:  lbu     t0, UART_LSR(a0)
    andi    t0, t0, UART_LSR_THRE
    beqz    t0, 1b
    /* Write the character */
    sb      a1, UART_THR(a0)
    ret
