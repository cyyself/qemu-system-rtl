OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS {
    . = 0x80000000;
    .text : {
        *(.init)
        *(.text .text.*)
        *(.rodata .rodata.*)
    }
    /DISCARD/ : { *(*) }
}
