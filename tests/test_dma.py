#!/usr/bin/env python3
"""
test_dma.py - Test DMA and IRQ via L2 Frontend Bus

This script acts as QEMU: connects to soc-simulator via TCP,
performs handshake, sends CPU_START, handles MMIO (UART) messages,
then sends DMA_WRITE + IRQ_UPDATE and verifies the CPU reads the
DMA'd data correctly.

Usage:
    1. Start soc-simulator:
       cd soc-simulator && ./obj_dir_qemu/VExampleRocketSystem \
           -b ../tests/dma_irq_test.bin -s 0.0.0.0:2345
    2. Run this test:
       python3 test_dma.py [--addr 127.0.0.1:2345]

Expected output from CPU firmware:
    READY       (firmware initialized, waiting for interrupt)
    DMA_OK      (DMA'd data printed by interrupt handler)
    DONE        (interrupt handler finished)
"""

import argparse
import socket
import struct
import sys
import time

# Protocol constants
RTL_PROTOCOL_MAGIC   = 0x51454D55524C5400
RTL_PROTOCOL_VERSION = 3

# Message types
MSG_HELLO          = 0x01
MSG_HELLO_ACK      = 0x02
MSG_MMIO_READ      = 0x10
MSG_MMIO_READ_RESP = 0x11
MSG_MMIO_WRITE     = 0x12
MSG_MMIO_WRITE_RESP = 0x13
MSG_DMA_WRITE      = 0x22
MSG_DMA_WRITE_RESP = 0x23
MSG_IRQ_UPDATE     = 0x30
MSG_SYNC           = 0x40
MSG_SYNC_ACK       = 0x41
MSG_CPU_START      = 0x60
MSG_CPU_START_ACK  = 0x61
MSG_CPU_MEM_READ       = 0x70
MSG_CPU_MEM_READ_RESP  = 0x71
MSG_CPU_MEM_WRITE      = 0x72
MSG_CPU_MEM_WRITE_RESP = 0x73
MSG_SHUTDOWN       = 0xF0

# Memory mode
RTL_MEMMODE_LOCAL = 0

# UART addresses
UART_BASE   = 0x60100000
UART_THR    = UART_BASE + 0
UART_LSR    = UART_BASE + 5
UART_LSR_THRE = 0x20
UART_LSR_TEMT = 0x40

# DMA target
DMA_BUF_ADDR = 0x80010000
DMA_TEST_DATA = b"DMA_OK\r\n"


def recv_all(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Connection closed")
        data += chunk
    return data


def pack_hdr(msg_type, length):
    return struct.pack('<II', msg_type, length)


class QEMUSimulator:
    """Simulates the QEMU side of the RTL protocol."""

    def __init__(self, addr):
        self.addr = addr
        self.sock = None
        self.uart_output = bytearray()
        self.dma_write_resp_received = False
        self.dma_write_status = None

    def connect(self):
        host, _, port_str = self.addr.rpartition(':')
        if not host:
            host = '127.0.0.1'
        port = int(port_str)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[+] Connecting to soc-simulator at {host}:{port}...")
        self.sock.connect((host, port))
        print(f"[+] Connected")

    def close(self):
        if self.sock:
            self.sock.close()

    def recv_msg(self):
        """Receive a message: returns (type, raw_bytes)."""
        hdr = recv_all(self.sock, 8)
        msg_type, msg_len = struct.unpack('<II', hdr)
        body = b''
        if msg_len > 8:
            body = recv_all(self.sock, msg_len - 8)
        return msg_type, hdr + body

    def do_handshake(self):
        """Receive HELLO from soc-sim, send HELLO_ACK."""
        msg_type, raw = self.recv_msg()
        if msg_type != MSG_HELLO:
            raise RuntimeError(f"Expected HELLO (0x01), got 0x{msg_type:x}")

        # Parse HELLO
        magic = struct.unpack_from('<Q', raw, 8)[0]
        version = struct.unpack_from('<I', raw, 16)[0]
        num_cores = struct.unpack_from('<I', raw, 20)[0]
        xlen = struct.unpack_from('<I', raw, 24)[0]
        num_irq = struct.unpack_from('<I', raw, 28)[0]
        isa = raw[32:96].split(b'\x00')[0].decode()
        num_ranges = struct.unpack_from('<I', raw, 96)[0]
        flags = struct.unpack_from('<I', raw, 100)[0]
        memory_mode = struct.unpack_from('<I', raw, 104)[0]

        print(f"[+] HELLO: magic=OK version={version} cores={num_cores} "
              f"xlen={xlen} isa={isa} irqs={num_irq}")
        print(f"    flags=0x{flags:x} memory_mode={memory_mode} "
              f"ranges={num_ranges}")

        if magic != RTL_PROTOCOL_MAGIC:
            raise RuntimeError("Bad protocol magic")
        if version != RTL_PROTOCOL_VERSION:
            raise RuntimeError(f"Protocol version mismatch: {version}")

        # Send HELLO_ACK
        ack = pack_hdr(MSG_HELLO_ACK, 16)
        ack += struct.pack('<II', 0, memory_mode)  # status=OK
        self.sock.sendall(ack)
        print("[+] Sent HELLO_ACK")

    def send_cpu_start(self):
        """Send CPU_START to soc-simulator."""
        msg = pack_hdr(MSG_CPU_START, 16)
        msg += struct.pack('<Q', 0)  # start_addr=0 (use default)
        self.sock.sendall(msg)
        print("[+] Sent CPU_START")

        # Wait for CPU_START_ACK
        while True:
            msg_type, raw = self.recv_msg()
            if msg_type == MSG_CPU_START_ACK:
                status = struct.unpack_from('<I', raw, 8)[0]
                if status != 0:
                    raise RuntimeError(f"CPU_START_ACK failed: {status}")
                print("[+] CPU started")
                return
            else:
                self._handle_async(msg_type, raw)

    def send_dma_write(self, addr, data):
        """Send DMA_WRITE to soc-simulator."""
        size = len(data)
        hdr_size = 8 + 8 + 4 + 4  # header + addr + size + req_id
        msg_len = hdr_size + size
        msg = pack_hdr(MSG_DMA_WRITE, msg_len)
        msg += struct.pack('<QII', addr, size, 0)  # req_id=0
        msg += data
        self.sock.sendall(msg)
        print(f"[+] Sent DMA_WRITE addr=0x{addr:x} size={size}")

    def send_irq_update(self, irq_levels):
        """Send IRQ_UPDATE to soc-simulator."""
        msg = pack_hdr(MSG_IRQ_UPDATE, 16)
        msg += struct.pack('<Q', irq_levels)
        self.sock.sendall(msg)
        print(f"[+] Sent IRQ_UPDATE levels=0x{irq_levels:x}")

    def send_shutdown(self):
        """Send SHUTDOWN."""
        msg = pack_hdr(MSG_SHUTDOWN, 16)
        msg += struct.pack('<II', 0, 0)
        self.sock.sendall(msg)
        print("[+] Sent SHUTDOWN")

    def handle_mmio_read(self, raw):
        """Handle MMIO_READ from soc-sim (CPU reading a peripheral)."""
        addr = struct.unpack_from('<Q', raw, 8)[0]
        size = struct.unpack_from('<I', raw, 16)[0]
        req_id = struct.unpack_from('<I', raw, 20)[0]

        # Respond based on address
        data = 0
        if addr == UART_LSR:
            data = UART_LSR_THRE | UART_LSR_TEMT  # UART ready
        # Other UART registers: return 0

        resp = pack_hdr(MSG_MMIO_READ_RESP, 24)
        resp += struct.pack('<QII', data, req_id, 0)  # status=OK
        self.sock.sendall(resp)

    def handle_mmio_write(self, raw):
        """Handle MMIO_WRITE from soc-sim (CPU writing a peripheral)."""
        addr = struct.unpack_from('<Q', raw, 8)[0]
        data = struct.unpack_from('<Q', raw, 16)[0]
        size = struct.unpack_from('<I', raw, 24)[0]
        req_id = struct.unpack_from('<I', raw, 28)[0]

        if addr == UART_THR:
            ch = data & 0xFF
            self.uart_output.append(ch)
            if ch >= 0x20 or ch in (0x0a, 0x0d):
                sys.stdout.write(chr(ch))
                sys.stdout.flush()

        # Send MMIO_WRITE_RESP
        resp = pack_hdr(MSG_MMIO_WRITE_RESP, 16)
        resp += struct.pack('<II', req_id, 0)  # status=OK
        self.sock.sendall(resp)

    def handle_sync(self, raw):
        """Handle SYNC from soc-sim."""
        tick = struct.unpack_from('<Q', raw, 8)[0]
        ack = pack_hdr(MSG_SYNC_ACK, 16)
        ack += struct.pack('<Q', tick)
        self.sock.sendall(ack)

    def _handle_async(self, msg_type, raw):
        """Handle messages that can arrive at any time."""
        if msg_type == MSG_MMIO_READ:
            self.handle_mmio_read(raw)
        elif msg_type == MSG_MMIO_WRITE:
            self.handle_mmio_write(raw)
        elif msg_type == MSG_SYNC:
            self.handle_sync(raw)
        elif msg_type == MSG_DMA_WRITE_RESP:
            req_id = struct.unpack_from('<I', raw, 8)[0]
            status = struct.unpack_from('<I', raw, 12)[0]
            self.dma_write_resp_received = True
            self.dma_write_status = status
            print(f"[+] DMA_WRITE_RESP req_id={req_id} status={status}")
        elif msg_type == MSG_SHUTDOWN:
            raise ConnectionError("soc-sim sent SHUTDOWN")
        else:
            print(f"    [?] Unhandled message type 0x{msg_type:x}")

    def process_until(self, condition, timeout=60):
        """Process messages until condition() returns True or timeout."""
        self.sock.settimeout(1.0)
        start = time.time()
        while time.time() - start < timeout:
            try:
                msg_type, raw = self.recv_msg()
                self._handle_async(msg_type, raw)
            except socket.timeout:
                pass
            if condition():
                self.sock.settimeout(None)
                return True
        self.sock.settimeout(None)
        return False

    def uart_has(self, text):
        """Check if the UART output contains the given text."""
        return text.encode() in bytes(self.uart_output)


def main():
    parser = argparse.ArgumentParser(description='DMA+IRQ test (acts as QEMU)')
    parser.add_argument('--addr', '-a', default='127.0.0.1:2345',
                        help='soc-simulator TCP address (host:port)')
    args = parser.parse_args()

    sim = QEMUSimulator(args.addr)
    passed = True

    try:
        sim.connect()
        sim.do_handshake()
        sim.send_cpu_start()

        # Phase 1: Wait for "READY" from CPU firmware
        print("\n=== Phase 1: Waiting for READY ===")
        if not sim.process_until(lambda: sim.uart_has("READY\r\n"), timeout=120):
            print("FAIL: Timed out waiting for READY")
            passed = False
        else:
            print("OK: CPU printed READY")

        if passed:
            # Small delay to ensure CPU is in WFI
            time.sleep(0.5)

            # Phase 2: Send DMA_WRITE with test data
            print("\n=== Phase 2: Sending DMA_WRITE ===")
            # Pad test data with null terminator
            dma_data = DMA_TEST_DATA + b'\x00'
            # Align to 8 bytes for clean AXI burst
            while len(dma_data) % 8 != 0:
                dma_data += b'\x00'
            sim.send_dma_write(DMA_BUF_ADDR, dma_data)

            # Wait for DMA_WRITE_RESP
            if not sim.process_until(
                    lambda: sim.dma_write_resp_received, timeout=30):
                print("FAIL: Timed out waiting for DMA_WRITE_RESP")
                passed = False
            elif sim.dma_write_status != 0:
                print(f"FAIL: DMA_WRITE failed with status "
                      f"{sim.dma_write_status}")
                passed = False
            else:
                print("OK: DMA_WRITE completed successfully")

        if passed:
            # Phase 3: Send IRQ to trigger interrupt handler
            print("\n=== Phase 3: Sending IRQ_UPDATE ===")
            sim.send_irq_update(0x1)  # bit 0 → PLIC source 1

            # Wait for "DONE" in UART output
            if not sim.process_until(
                    lambda: sim.uart_has("DONE\r\n"), timeout=30):
                print("FAIL: Timed out waiting for DONE")
                passed = False
            else:
                print("OK: CPU printed DONE")

            # Check that DMA data was printed
            if b"DMA_OK" in bytes(sim.uart_output):
                print("OK: CPU correctly read DMA'd data")
            else:
                print("FAIL: DMA data not found in UART output")
                passed = False

            # Clear IRQ
            sim.send_irq_update(0x0)

            # Let it settle
            sim.process_until(lambda: False, timeout=2)

        # Summary
        print("\n" + "=" * 50)
        uart_str = bytes(sim.uart_output).decode('ascii', errors='replace')
        print(f"UART output: {uart_str!r}")
        if passed:
            print("=== ALL TESTS PASSED ===")
        else:
            print("=== TESTS FAILED ===")

    except ConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        passed = False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        passed = False
    finally:
        try:
            sim.send_shutdown()
        except Exception:
            pass
        sim.close()

    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
