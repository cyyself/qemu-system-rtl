# QEMU-System-RTL: Connecting RTL CPU Cores to QEMU

This project connects a CPU core implemented in RTL (Register Transfer Level) to
QEMU's system emulator via a TCP socket protocol. This enables running a
hardware CPU design (e.g., Rocket-Chip) with QEMU's full peripheral ecosystem
(UART, VirtIO networking, block devices, etc.).

## Architecture

```
┌─────────────────────────┐          ┌──────────────────────────────┐
│    soc-simulator        │          │         QEMU                 │
│                         │          │                              │
│  ┌───────────────────┐  │  Unix    │  ┌──────────────────────┐    │
│  │ RTL CPU Core      │  │  TCP     │  │  UART (16550)        │    │
│  │ (Rocket-Chip)     │──┼──Socket──┼──│  VirtIO MMIO ×8      │    │
│  └────────┬──────────┘  │          │  │  (net, blk, etc.)    │    │
│           │ AXI4        │          │  └──────────────────────┘    │
│  ┌────────┴──────────┐  │          │                              │
│  │ Memory (DRAM)     │  │  DMA     │  IRQ forwarding              │
│  │ 0x80000000+       │◄─┼──────────┼── QEMU → soc-simulator       │
│  └───────────────────┘  │          │                              │
└─────────────────────────┘          └──────────────────────────────┘
```

**soc-simulator** runs the RTL CPU via Verilator. It listens on a TCP port and
waits for QEMU to connect. The CPU's MMIO bus accesses (0x60000000–0x7FFFFFFF)
are forwarded to QEMU over the TCP socket. QEMU provides the peripheral devices.
Memory (0x80000000+) stays local in soc-simulator.

**QEMU** acts as the peripheral provider. It connects to soc-simulator's TCP
port. It creates UART, VirtIO, and other devices, handles MMIO requests from the
RTL CPU, can DMA into soc-simulator's memory, and forwards interrupt signals
back to the RTL CPU.

## Memory Map

| Address Range | Size | Owner | Description |
|---|---|---|---|
| `0x60000000–0x7FFFFFFF` | 512 MB | QEMU (MMIO) | Peripheral address space |
| `0x60100000–0x601000FF` | 256 B | QEMU | UART0 (16550A) |
| `0x60200000–0x60208FFF` | 32 KB | QEMU | VirtIO MMIO ×8 |
| `0x80000000+` | Configurable | soc-simulator | DRAM |

## IRQ Map

| IRQ | Device |
|---|---|
| 0 | UART0 |
| 1 | VirtIO MMIO device 0 |

## Protocol

Communication uses a binary protocol over a TCP stream socket. All messages
start with an 8-byte header:

```c
struct rtl_msg_header {
    uint32_t type;    // message type
    uint32_t length;  // total length including header
};
```

All multi-byte fields are **little-endian**.

### Message Flow

1. **Startup**: soc-simulator listens on TCP → QEMU connects
2. **Handshake**: soc-simulator sends `HELLO` (CPU info), QEMU responds `HELLO_ACK`
3. **Runtime**:
   - CPU MMIO → `MMIO_READ`/`MMIO_WRITE` (soc-sim → QEMU → response)
   - DMA → `DMA_READ`/`DMA_WRITE` (QEMU → soc-sim → response)
   - IRQ → `IRQ_UPDATE` (QEMU → soc-sim, async)
   - Time sync → `SYNC`/`SYNC_ACK` (soc-sim → QEMU, periodic)
4. **Shutdown**: Either side sends `SHUTDOWN`

### Message Types

| Type | Code | Direction | Description |
|---|---|---|---|
| `HELLO` | 0x01 | RTL → QEMU | CPU capabilities and address map |
| `HELLO_ACK` | 0x02 | QEMU → RTL | Acknowledgement |
| `MMIO_READ` | 0x10 | RTL → QEMU | Read peripheral register |
| `MMIO_READ_RESP` | 0x11 | QEMU → RTL | Read response with data |
| `MMIO_WRITE` | 0x12 | RTL → QEMU | Write peripheral register |
| `MMIO_WRITE_RESP` | 0x13 | QEMU → RTL | Write acknowledgement |
| `DMA_READ` | 0x20 | QEMU → RTL | Read from RTL memory |
| `DMA_READ_RESP` | 0x21 | RTL → QEMU | DMA read response with data |
| `DMA_WRITE` | 0x22 | QEMU → RTL | Write to RTL memory |
| `DMA_WRITE_RESP` | 0x23 | RTL → QEMU | DMA write acknowledgement |
| `IRQ_UPDATE` | 0x30 | QEMU → RTL | Interrupt level bitmask change |
| `SYNC` | 0x40 | RTL → QEMU | Clock synchronization heartbeat |
| `SYNC_ACK` | 0x41 | QEMU → RTL | Sync acknowledgement |
| `SHUTDOWN` | 0xF0 | Either | Graceful termination |

See [protocol/rtl_protocol.h](protocol/rtl_protocol.h) for complete struct
definitions.

## Quick Start

### 1. Build QEMU

```bash
cd qemu
mkdir -p build && cd build
../configure --target-list=riscv64-softmmu
make -j$(nproc)
```

### 2. Start soc-simulator (with Rocket-Chip RTL)

If you have the Rocket-Chip Verilog generated via Verilator:

```bash
cd soc-simulator
make sim_qemu VERILATOR_ROOT=/path/to/verilator
```

Start soc-simulator first (it listens for QEMU to connect):

```bash
cd soc-simulator
./sim_qemu -s 0.0.0.0:2345 -b firmware.bin -m 256
```

### 3. Start QEMU

QEMU connects to soc-simulator:

```bash
./qemu-system-riscv64 \
    -machine rtl,rtl-sock=127.0.0.1:2345 \
    -nographic \
    -serial mon:stdio
```

### 4. Run the test client

The test script simulates the soc-simulator side: it listens on a TCP port,
waits for QEMU to connect, performs the HELLO handshake, and exercises MMIO,
SYNC, and UART writes.

```bash
# Terminal 1: Start the test (listens on TCP port 2345)
python3 tests/test_protocol.py --addr 0.0.0.0:2345

# Terminal 2: Start QEMU (connects to the test)
cd qemu/build
./qemu-system-riscv64 \
    -machine rtl,rtl-sock=127.0.0.1:2345 \
    -nographic -serial mon:stdio \
    -kernel ../../tests/uart_test.bin
```

The test performs:
- HELLO handshake (protocol v3)
- Receives firmware via MEM_WRITE and CPU_START from QEMU
- UART write ("OK\n" and "Hello from RTL CPU!\n")
- UART register read (Line Status Register)
- SYNC message exchange
- Shutdown

### 5. Run with RTL CPU

```bash
# Terminal 1: Start soc-simulator (listens on TCP port 2345)
cd soc-simulator
./sim_qemu -s 0.0.0.0:2345 -b firmware.bin -m 256

# Terminal 2: Start QEMU (connects to soc-simulator)
cd qemu/build
./qemu-system-riscv64 -machine rtl,rtl-sock=127.0.0.1:2345 -nographic
```

## Project Structure

```
qemu-system-rtl/
├── README.md                          # This file
├── protocol/
│   └── rtl_protocol.h                 # Shared protocol definitions
├── qemu/
│   ├── hw/riscv/
│   │   ├── rtl.c                      # QEMU RTL machine backend
│   │   ├── Kconfig                    # Build config (RISCV_RTL)
│   │   └── meson.build                # Build integration
│   └── include/hw/riscv/
│       ├── rtl.h                      # Machine state header
│       └── rtl_protocol.h             # Protocol header (copy)
├── soc-simulator/
│   ├── src/
│   │   ├── qemu_bridge.hpp            # QEMU bridge (MMIO/DMA/IRQ)
│   │   ├── sim_qemu.cpp               # Main simulator with QEMU bridge
│   │   ├── axi4.hpp                   # AXI4 bus primitives
│   │   ├── axi4_master.hpp            # AXI4 master (DMA via L2 frontend)
│   │   ├── axi4_mem.hpp               # AXI4 memory controller
│   │   ├── axi4_xbar.hpp              # AXI4 crossbar switch
│   │   ├── rocket_top.hpp             # Rocket-Chip tie-off helpers
│   │   └── mmio_dev.hpp               # MMIO device base class
│   └── Makefile                       # Builds sim_qemu target
├── rocket-chip/                       # Rocket-Chip RISC-V CPU
└── tests/
    ├── bare.ld                        # Linker script for bare-metal tests
    ├── start.S                        # Minimal startup (stack + trap entry)
    ├── uart_test.S                    # UART write test ("OK")
    ├── dma_irq_test.c                 # DMA + interrupt test (C)
    ├── virtio_net_dma_test.c          # Bare-metal virtio-net DMA smoke test
    ├── run_virtio_net_dma_test.py     # Build + run helper for the test
    ├── test_protocol.py               # Protocol test (simulates soc-sim)
    └── test_dma.py                    # DMA+IRQ test (simulates QEMU)
```

## Bare-Metal Virtio-Net DMA Test

For a much faster DMA sanity check than booting Linux, use the bare-metal
virtio-net DMA + interrupt test:

```bash
python3 tests/run_virtio_net_dma_test.py --port 2364
```

By default this runs in `local` memory mode, which exercises protocol DMA over
the L2 frontend bus.

To exercise the local-memory protocol DMA path explicitly:

```bash
python3 tests/run_virtio_net_dma_test.py --port 2364 --memory-mode local
```

The helper will:
- build `tests/virtio_net_dma_test.bin`
- start `soc-simulator` on the requested port
- start QEMU with `virtio-net-device`
- connect the guest NIC to a UDP socket backend
- verify the guest TX frame from the host side
- inject one RX frame back into the guest
- verify the guest printed `PASS`

This validates virtio-net TX DMA, RX DMA, and interrupt delivery in local-memory
mode without waiting for a Linux boot:
- QEMU DMA-reads guest descriptors and the transmit packet
- QEMU DMA-writes the used ring update back into guest memory
- QEMU DMA-writes the received frame into guest memory
- QEMU raises the shared VirtIO external interrupt and the guest claims it via the PLIC

Current status:
- `--memory-mode local`: verified passing, exercises protocol DMA over the L2 frontend bus
- `--memory-mode qemu`: verified passing, useful as a comparison path where DRAM resides in QEMU

## Design Notes

### MMIO Flow (CPU → Peripheral)

1. RTL CPU issues AXI4 read/write on MMIO port to address in 0x60000000 range
2. soc-simulator's `axi4_xbar` routes to `qemu_mmio_bridge`
3. Bridge sends `MMIO_READ`/`MMIO_WRITE` over socket (blocking)
4. QEMU receives, calls `address_space_rw()` to access device
5. Device responds (e.g., UART returns register value)
6. QEMU sends response back over socket
7. Bridge returns data to AXI4 bus

### DMA Flow (Peripheral → Memory)

1. QEMU device needs to DMA (e.g., VirtIO network receives a packet)
2. QEMU sends `DMA_WRITE` with address and data over socket
3. soc-simulator's bridge writes directly to `axi4_mem` backing memory
4. Bridge sends `DMA_WRITE_RESP` acknowledgement

### IRQ Flow

1. QEMU device asserts/deasserts an IRQ line
2. QEMU's IRQ handler sends `IRQ_UPDATE` with full bitmask over socket
3. soc-simulator's bridge stores the bitmask atomically
4. Simulation loop reads `bridge.get_irq_level()` and applies to CPU's
   external interrupt input

### Threading Model

On the soc-simulator side:
- **Main thread**: Verilator simulation loop, AXI4 bus processing
- **Recv thread**: Background thread receiving QEMU messages (DMA, IRQ, etc.)
- Synchronization via mutexes and condition variables

On the QEMU side:
- Everything runs in QEMU's main event loop
- `qemu_set_fd_handler()` for non-blocking socket I/O

## VirtIO Devices

The RTL machine provides 8 VirtIO MMIO transport slots. Use QEMU's standard
device options:

```bash
# Network
-device virtio-net-device,netdev=net0 -netdev user,id=net0

# Block device
-device virtio-blk-device,drive=hd0 -drive file=disk.img,id=hd0,if=none

# Random number generator
-device virtio-rng-device
```

## QEMU Machine Properties

| Property | Default | Description |
|---|---|---|
| `rtl-sock` | `127.0.0.1:2345` | TCP address of soc-simulator (host:port) |

Set via: `-machine rtl,rtl-sock=host:port`
