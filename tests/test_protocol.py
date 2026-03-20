#!/usr/bin/env python3
"""
Test client for the QEMU RTL co-simulation protocol.

Simulates the soc-simulator side: listens on a TCP port, waits for QEMU
to connect, performs the HELLO handshake, then exercises MMIO, SYNC, etc.

Usage:
    1. Start this test (listens on TCP):
       python3 test_protocol.py [--addr 0.0.0.0:2345]
    2. Start QEMU:
       qemu-system-riscv64 -machine rtl,rtl-sock=127.0.0.1:2345 -nographic

Tests:
    1. HELLO handshake
    2. MMIO write "OK\n" to UART (visible on QEMU console)
    3. MMIO read from UART status register
    4. SYNC message exchange
    5. Shutdown
"""

import argparse
import socket
import struct
import sys
import time

# Protocol constants
RTL_PROTOCOL_MAGIC = 0x51454D55524C5400
RTL_PROTOCOL_VERSION = 3
RTL_MAX_ADDR_RANGES = 16

# Message types
RTL_MSG_HELLO           = 0x01
RTL_MSG_HELLO_ACK       = 0x02
RTL_MSG_MMIO_READ       = 0x10
RTL_MSG_MMIO_READ_RESP  = 0x11
RTL_MSG_MMIO_WRITE      = 0x12
RTL_MSG_MMIO_WRITE_RESP = 0x13
RTL_MSG_DMA_READ        = 0x20
RTL_MSG_DMA_READ_RESP   = 0x21
RTL_MSG_DMA_WRITE       = 0x22
RTL_MSG_DMA_WRITE_RESP  = 0x23
RTL_MSG_IRQ_UPDATE      = 0x30
RTL_MSG_SYNC            = 0x40
RTL_MSG_SYNC_ACK        = 0x41
RTL_MSG_MEM_READ        = 0x50
RTL_MSG_MEM_READ_RESP   = 0x51
RTL_MSG_MEM_WRITE       = 0x52
RTL_MSG_MEM_WRITE_RESP  = 0x53
RTL_MSG_CPU_START       = 0x60
RTL_MSG_CPU_START_ACK   = 0x61
RTL_MSG_CPU_STOP        = 0x62
RTL_MSG_CPU_STOP_ACK    = 0x63
RTL_MSG_CPU_STATUS      = 0x64
RTL_MSG_CPU_STATUS_RESP = 0x65
RTL_MSG_SHUTDOWN        = 0xF0

# UART addresses (16550 registers, regshift=0)
UART_BASE = 0x60100000
UART_THR  = UART_BASE + 0  # Transmit Holding Register (write)
UART_RBR  = UART_BASE + 0  # Receive Buffer Register (read)
UART_IER  = UART_BASE + 1  # Interrupt Enable Register
UART_IIR  = UART_BASE + 2  # Interrupt Identification Register (read)
UART_FCR  = UART_BASE + 2  # FIFO Control Register (write)
UART_LCR  = UART_BASE + 3  # Line Control Register
UART_MCR  = UART_BASE + 4  # Modem Control Register
UART_LSR  = UART_BASE + 5  # Line Status Register
UART_MSR  = UART_BASE + 6  # Modem Status Register

# LSR bits
UART_LSR_DR   = 0x01  # Data Ready
UART_LSR_THRE = 0x20  # Transmit Holding Register Empty
UART_LSR_TEMT = 0x40  # Transmitter Empty


def recv_all(sock, n):
    """Receive exactly n bytes."""
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Connection closed")
        data += chunk
    return data


def send_all(sock, data):
    """Send all bytes."""
    sock.sendall(data)


def pack_header(msg_type, length):
    """Pack a message header (type: u32, length: u32)."""
    return struct.pack('<II', msg_type, length)


def unpack_header(data):
    """Unpack a message header."""
    return struct.unpack('<II', data)


class RTLTestClient:
    def __init__(self, addr):
        self.addr = addr
        self.sock = None
        self.req_id = 0
        self.irq_level = 0
        self.irq_updates = 0
        self.cpu_running = False
        self.tick_count = 0
        self.memory = bytearray(256 * 1024 * 1024)  # 256MB simulated memory

    def connect(self):
        """Listen on TCP and wait for QEMU to connect."""
        host, _, port_str = self.addr.rpartition(':')
        if not host:
            host = '0.0.0.0'
        port = int(port_str)
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind((host, port))
        listen_sock.listen(1)
        print(f"[+] Listening on {host}:{port}, waiting for QEMU to connect...")
        self.sock, peer = listen_sock.accept()
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        listen_sock.close()
        print(f"[+] QEMU connected from {peer[0]}:{peer[1]}")

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def next_req_id(self):
        rid = self.req_id
        self.req_id += 1
        return rid

    def _recv_msg(self):
        """Receive a message header and return (type, full_raw_data)."""
        hdr_data = recv_all(self.sock, 8)
        msg_type, msg_len = unpack_header(hdr_data)
        body = b''
        if msg_len > 8:
            body = recv_all(self.sock, msg_len - 8)
        return msg_type, hdr_data + body

    def _handle_irq_update(self, raw):
        """Process an IRQ_UPDATE message."""
        irq_levels = struct.unpack('<Q', raw[8:16])[0]
        self.irq_level = irq_levels
        self.irq_updates += 1

    def _handle_cpu_start(self, raw):
        """Handle CPU_START from QEMU: send CPU_START_ACK."""
        start_addr = struct.unpack('<Q', raw[8:16])[0]
        print(f"    Received CPU_START (start_addr=0x{start_addr:x})")
        # Send CPU_START_ACK: header(8) + status(4) + reserved(4) = 16
        ack = pack_header(RTL_MSG_CPU_START_ACK, 16)
        ack += struct.pack('<II', 0, 0)  # status=OK, reserved=0
        send_all(self.sock, ack)
        self.cpu_running = True
        print("    Sent CPU_START_ACK")

    def _handle_cpu_stop(self, raw):
        """Handle CPU_STOP from QEMU: send CPU_STOP_ACK."""
        reason, reserved = struct.unpack('<II', raw[8:16])
        print(f"    Received CPU_STOP (reason={reason})")
        ack = pack_header(RTL_MSG_CPU_STOP_ACK, 16)
        ack += struct.pack('<II', 0, 0)
        send_all(self.sock, ack)
        self.cpu_running = False
        print("    Sent CPU_STOP_ACK")

    def _handle_mem_write(self, raw):
        """Handle MEM_WRITE from QEMU: store data and send MEM_WRITE_RESP."""
        addr, size, req_id = struct.unpack('<QII', raw[8:24])
        data = raw[24:24+size]
        print(f"    Received MEM_WRITE addr=0x{addr:x} size={size} req_id={req_id}")
        # Store in our simulated memory
        offset = addr - 0x80000000
        if 0 <= offset < len(self.memory):
            self.memory[offset:offset+size] = data
        # Send response: header(8) + req_id(4) + status(4) = 16
        resp = pack_header(RTL_MSG_MEM_WRITE_RESP, 16)
        resp += struct.pack('<II', req_id, 0)  # status=OK
        send_all(self.sock, resp)

    def _handle_mem_read(self, raw):
        """Handle MEM_READ from QEMU: return data from simulated memory."""
        addr, size, req_id = struct.unpack('<QII', raw[8:24])
        print(f"    Received MEM_READ addr=0x{addr:x} size={size} req_id={req_id}")
        offset = addr - 0x80000000
        if 0 <= offset and offset + size <= len(self.memory):
            data = bytes(self.memory[offset:offset+size])
        else:
            data = b'\x00' * size
        # Send response: header(8) + req_id(4) + status(4) + data
        total_len = 16 + len(data)
        resp = pack_header(RTL_MSG_MEM_READ_RESP, total_len)
        resp += struct.pack('<II', req_id, 0)  # status=OK
        resp += data
        send_all(self.sock, resp)

    def _handle_cpu_status(self, raw):
        """Handle CPU_STATUS from QEMU: send status response."""
        status_val = 1 if self.cpu_running else 0
        # header(8) + status(4) + tick_count(8) + reserved(4) = 24
        resp = pack_header(RTL_MSG_CPU_STATUS_RESP, 24)
        resp += struct.pack('<IQII', status_val, self.tick_count, 0, 0)[:16]
        # Actually the struct is: status(u32) + tick_count(u64) + reserved(u32)
        resp = pack_header(RTL_MSG_CPU_STATUS_RESP, 24)
        resp += struct.pack('<IQI', status_val, self.tick_count, 0)
        send_all(self.sock, resp)

    def _recv_expected(self, expected_type):
        """Receive messages until we get the expected type, handling async messages."""
        while True:
            msg_type, raw = self._recv_msg()
            if msg_type == RTL_MSG_IRQ_UPDATE:
                self._handle_irq_update(raw)
                continue
            if msg_type == RTL_MSG_CPU_START:
                self._handle_cpu_start(raw)
                continue
            if msg_type == RTL_MSG_CPU_STOP:
                self._handle_cpu_stop(raw)
                continue
            if msg_type == RTL_MSG_MEM_WRITE:
                self._handle_mem_write(raw)
                continue
            if msg_type == RTL_MSG_MEM_READ:
                self._handle_mem_read(raw)
                continue
            if msg_type == RTL_MSG_CPU_STATUS:
                self._handle_cpu_status(raw)
                continue
            if msg_type == expected_type:
                return raw
            raise RuntimeError(
                f"Expected msg type 0x{expected_type:x}, got 0x{msg_type:x}")

    def send_hello(self):
        """Send HELLO message with CPU capabilities."""
        # Build address ranges
        # Range 0: Memory (0x80000000, 256MB)
        # Range 1: MMIO (0x60000000, 512MB)
        ranges = []
        ranges.append(struct.pack('<QQII',
                                  0x80000000,    # base
                                  256*1024*1024, # size
                                  0,             # type=memory
                                  0))            # flags
        ranges.append(struct.pack('<QQII',
                                  0x60000000,    # base
                                  0x20000000,    # size=512MB
                                  1,             # type=MMIO
                                  0))            # flags
        # Pad to RTL_MAX_ADDR_RANGES (16)
        range_entry_size = 8 + 8 + 4 + 4  # 24 bytes each
        while len(ranges) < RTL_MAX_ADDR_RANGES:
            ranges.append(b'\x00' * range_entry_size)

        isa_string = b'rv64imafdc' + b'\x00' * (64 - len(b'rv64imafdc'))

        # HELLO body (after header): magic(8) + version(4) + num_cores(4) +
        #   xlen(4) + num_irq_lines(4) + isa_string(64) + num_addr_ranges(4) +
        #   flags(4) + memory_mode(4) + reserved2(4) + ranges(16*24)
        body = struct.pack('<QIIII',
                           RTL_PROTOCOL_MAGIC,
                           RTL_PROTOCOL_VERSION,
                           1,   # num_cores
                           64,  # xlen
                           64)  # num_irq_lines
        body += isa_string
        body += struct.pack('<IIII', 2, 0, 0, 0)  # num_addr_ranges, flags, memory_mode, reserved2
        for r in ranges:
            body += r

        total_len = 8 + len(body)  # header + body
        msg = pack_header(RTL_MSG_HELLO, total_len) + body
        send_all(self.sock, msg)

        # Receive HELLO_ACK (may get IRQ_UPDATEs first)
        ack_data = self._recv_expected(RTL_MSG_HELLO_ACK)
        status, memory_mode = struct.unpack('<II', ack_data[8:16])

        if status != 0:
            raise RuntimeError(f"HELLO_ACK failed with status {status}")

        print("[+] HELLO handshake complete")

        # After HELLO_ACK, QEMU may send MEM_WRITE(s) and CPU_START.
        # Drain any such messages until we see CPU_START.
        print("[+] Waiting for CPU_START from QEMU...")
        while not self.cpu_running:
            msg_type, raw = self._recv_msg()
            if msg_type == RTL_MSG_CPU_START:
                self._handle_cpu_start(raw)
            elif msg_type == RTL_MSG_MEM_WRITE:
                self._handle_mem_write(raw)
            elif msg_type == RTL_MSG_MEM_READ:
                self._handle_mem_read(raw)
            elif msg_type == RTL_MSG_IRQ_UPDATE:
                self._handle_irq_update(raw)
            else:
                print(f"    Unexpected message type 0x{msg_type:x} during setup")
        print("[+] CPU started, ready for tests")
        return True

    def mmio_write(self, addr, data, size):
        """Send an MMIO write request and wait for response."""
        req_id = self.next_req_id()

        # header(8) + addr(8) + data(8) + size(4) + req_id(4) = 32
        msg = pack_header(RTL_MSG_MMIO_WRITE, 32)
        msg += struct.pack('<QQII', addr, data, size, req_id)
        send_all(self.sock, msg)

        # Receive response (may get IRQ_UPDATEs first)
        resp_data = self._recv_expected(RTL_MSG_MMIO_WRITE_RESP)
        resp_req_id, status = struct.unpack('<II', resp_data[8:16])
        return status == 0

    def mmio_read(self, addr, size):
        """Send an MMIO read request and wait for response."""
        req_id = self.next_req_id()

        # header(8) + addr(8) + size(4) + req_id(4) = 24
        msg = pack_header(RTL_MSG_MMIO_READ, 24)
        msg += struct.pack('<QII', addr, size, req_id)
        send_all(self.sock, msg)

        # Receive response (may get IRQ_UPDATEs first)
        resp_data = self._recv_expected(RTL_MSG_MMIO_READ_RESP)
        data, resp_req_id, status = struct.unpack('<QII', resp_data[8:24])
        return data, status == 0

    def send_sync(self, tick):
        """Send a SYNC message and wait for ACK."""
        # header(8) + tick_count(8) = 16
        msg = pack_header(RTL_MSG_SYNC, 16)
        msg += struct.pack('<Q', tick)
        send_all(self.sock, msg)

        # Receive SYNC_ACK (may get IRQ_UPDATEs first)
        resp_data = self._recv_expected(RTL_MSG_SYNC_ACK)
        resp_tick = struct.unpack('<Q', resp_data[8:16])[0]
        return resp_tick

    def send_shutdown(self):
        """Send a SHUTDOWN message."""
        # header(8) + reason(4) + reserved(4) = 16
        msg = pack_header(RTL_MSG_SHUTDOWN, 16)
        msg += struct.pack('<II', 0, 0)  # reason=normal, reserved=0
        send_all(self.sock, msg)
        print("[+] Shutdown sent")


def test_handshake(client):
    """Test 1: HELLO handshake."""
    print("\n=== Test 1: HELLO handshake ===")
    client.send_hello()
    print("    PASS")


def test_uart_write(client):
    """Test 2: Write 'OK\\n' to UART (visible on QEMU console)."""
    print("\n=== Test 2: UART write 'OK\\n' ===")

    # Write 'O'
    ok = client.mmio_write(UART_THR, ord('O'), 1)
    assert ok, "MMIO write 'O' failed"
    print("    Wrote 'O' to UART THR")

    # Write 'K'
    ok = client.mmio_write(UART_THR, ord('K'), 1)
    assert ok, "MMIO write 'K' failed"
    print("    Wrote 'K' to UART THR")

    # Write '\n'
    ok = client.mmio_write(UART_THR, ord('\n'), 1)
    assert ok, "MMIO write '\\n' failed"
    print("    Wrote '\\n' to UART THR")

    if client.irq_updates > 0:
        print(f"    IRQ updates received: {client.irq_updates}, "
              f"level=0x{client.irq_level:016x}")

    print("    PASS - Check QEMU console for 'OK' output")


def test_uart_read(client):
    """Test 3: Read UART Line Status Register."""
    print("\n=== Test 3: UART LSR read ===")

    data, ok = client.mmio_read(UART_LSR, 1)
    assert ok, "MMIO read LSR failed"
    print(f"    UART LSR = 0x{data:02x}")

    if data & UART_LSR_THRE:
        print("    THR is empty (ready for write)")
    if data & UART_LSR_TEMT:
        print("    Transmitter is empty")
    print("    PASS")


def test_sync(client):
    """Test 4: Sync message exchange."""
    print("\n=== Test 4: SYNC ===")

    tick = client.send_sync(10000)
    print(f"    SYNC ACK received, tick={tick}")
    assert tick == 10000, f"Expected tick 10000, got {tick}"

    tick = client.send_sync(20000)
    print(f"    SYNC ACK received, tick={tick}")
    assert tick == 20000, f"Expected tick 20000, got {tick}"

    print("    PASS")


def test_uart_string(client, text):
    """Write a string to UART character by character."""
    print(f"\n=== Writing to UART: {text!r} ===")
    for ch in text:
        ok = client.mmio_write(UART_THR, ord(ch), 1)
        if not ok:
            print(f"    FAILED to write '{ch}'")
            return False
    print("    PASS")
    return True


def main():
    parser = argparse.ArgumentParser(description='RTL protocol test client')
    parser.add_argument('--addr', '-a', default='0.0.0.0:2345',
                        help='TCP listen address (host:port)')
    # Keep --socket as hidden alias for backwards compat
    parser.add_argument('--socket', '-s', default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument('--test', '-t', choices=['all', 'handshake', 'uart',
                                                  'sync', 'uart-string'],
                        default='all', help='Test to run')
    parser.add_argument('--text', default='Hello from RTL CPU!\n',
                        help='Text to write for uart-string test')
    args = parser.parse_args()

    addr = args.socket if args.socket else args.addr
    client = RTLTestClient(addr)

    try:
        client.connect()
        test_handshake(client)

        if args.test in ('all', 'uart'):
            test_uart_write(client)
            test_uart_read(client)

        if args.test in ('all', 'sync'):
            test_sync(client)

        if args.test == 'uart-string':
            test_uart_string(client, args.text)

        if args.test == 'all':
            test_uart_string(client, "Hello from RTL CPU!\n")

        print("\n=== All tests passed ===")

    except ConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Test failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            client.send_shutdown()
        except Exception:
            pass
        client.close()


if __name__ == '__main__':
    main()
