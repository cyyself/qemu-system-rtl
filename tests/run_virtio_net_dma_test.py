#!/usr/bin/env python3
"""
Build and run the bare-metal virtio-net DMA + IRQ test.

The test passes only if:
    1. the guest firmware prints PASS after TX and RX completions,
    2. the host receives the guest TX frame over QEMU's UDP net backend, and
    3. the guest validates the inbound RX frame and the corresponding interrupt.
"""

from __future__ import annotations

import argparse
import pathlib
import socket
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SOCSIM = ROOT / "soc-simulator" / "obj_dir_qemu" / "VExampleRocketSystem"
QEMU = ROOT / "qemu" / "build" / "qemu-system-riscv64"
CC = "riscv64-linux-gnu-gcc"
OBJCOPY = "riscv64-linux-gnu-objcopy"

ELF = TESTS / "virtio_net_dma_test.elf"
BIN = TESTS / "virtio_net_dma_test.bin"
SOURCE = TESTS / "virtio_net_dma_test.c"
START = TESTS / "start.S"
LINKER = TESTS / "bare.ld"

TX_PAYLOAD_MARKER = b"RTL-VIRTIO-NET-DMA-TEST"
RX_PAYLOAD_MARKER = b"RTL-VIRTIO-NET-RX-IRQ-TEST"
GUEST_MAC = bytes.fromhex("525400123456")
HOST_MAC = bytes.fromhex("02aabbccddee")


def run(cmd: list[str], *, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def alloc_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_listen(log_path: pathlib.Path, proc: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"soc-simulator exited early with code {proc.returncode}:\n"
                f"{tail_text(log_path)}"
            )
        if "waiting for QEMU" in read_text(log_path):
                return
        time.sleep(0.2)
    raise TimeoutError(f"soc-simulator did not become ready:\n{tail_text(log_path)}")


def read_text(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")


def tail_text(path: pathlib.Path, lines: int = 40) -> str:
    text = read_text(path)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def build_firmware() -> None:
    run([
        CC,
        "-nostdlib",
        "-nostartfiles",
        "-fno-pic",
        "-no-pie",
        "-mcmodel=medany",
        "-msmall-data-limit=0",
        "-march=rv64imafdc",
        "-mabi=lp64d",
        "-O2",
        "-T",
        str(LINKER),
        "-o",
        str(ELF),
        str(START),
        str(SOURCE),
    ])
    run([OBJCOPY, "-O", "binary", str(ELF), str(BIN)])


def wait_for_pass(serial_log: pathlib.Path, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = read_text(serial_log)
        if "PASS\r\n" in text or "PASS\n" in text:
            return text
        if "FAIL " in text:
            raise RuntimeError(f"firmware reported failure:\n{text}")
        time.sleep(0.5)
    raise TimeoutError(
        f"timed out waiting for PASS; serial tail:\n{tail_text(serial_log)}"
    )


def wait_for_serial_token(serial_log: pathlib.Path, token: str, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = read_text(serial_log)
        if token in text:
            return text
        if "FAIL " in text:
            raise RuntimeError(f"firmware reported failure before {token!r}:\n{text}")
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {token!r}; serial tail:\n{tail_text(serial_log)}")


def recv_frame(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data, _addr = sock.recvfrom(4096)
    return data


def build_rx_frame() -> bytes:
    payload = RX_PAYLOAD_MARKER + b"-0123456789-HOST-RX!"
    payload = payload[:46].ljust(46, b"!")
    return GUEST_MAC + HOST_MAC + bytes((0x88, 0xb5)) + payload


def verify_tx_frame(frame: bytes) -> None:
    if TX_PAYLOAD_MARKER not in frame:
        raise RuntimeError("expected TX payload marker not found in host-received frame")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bare-metal virtio-net DMA test")
    parser.add_argument("--port", type=int, default=2364)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--memory-mode",
        choices=("local", "qemu"),
        default="local",
        help="Run with DRAM in soc-simulator (local, default) or QEMU (qemu)",
    )
    args = parser.parse_args()

    serial_log = pathlib.Path(f"/tmp/serial-{args.port}-vnet.log")
    qemu_log = pathlib.Path(f"/tmp/qemu-{args.port}-vnet.log")
    socsim_log = pathlib.Path(f"/tmp/socsim-{args.port}-vnet.log")

    for path in (serial_log, qemu_log, socsim_log):
        if path.exists():
            path.unlink()

    build_firmware()

    host_udp_port = alloc_udp_port()
    qemu_udp_port = alloc_udp_port()

    socsim_cmd = [
        str(SOCSIM),
        "-s",
        f"0.0.0.0:{args.port}",
        "-m",
        "256",
    ]
    if args.memory_mode == "qemu":
        socsim_cmd += ["-M", "qemu"]

    qemu_cmd = [
        str(QEMU),
        f"-machine",
        f"rtl,rtl-sock=127.0.0.1:{args.port}",
        "-global",
        "virtio-mmio.force-legacy=false",
        "-bios",
        "none",
        "-nographic",
        "-serial",
        f"file:{serial_log}",
        "-kernel",
        str(BIN),
        "-netdev",
        f"socket,id=net0,udp=127.0.0.1:{host_udp_port},localaddr=127.0.0.1:{qemu_udp_port}",
        "-device",
        "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56,tx=bh",
    ]

    socsim = subprocess.Popen(
        socsim_cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=socsim_log.open("w"),
    )
    qemu = None
    host_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    host_sock.bind(("127.0.0.1", host_udp_port))

    try:
        wait_for_listen(socsim_log, socsim, timeout=10.0)
        qemu = subprocess.Popen(
            qemu_cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=qemu_log.open("w"),
            stderr=subprocess.STDOUT,
        )

        tx_frame = recv_frame(host_sock, timeout=args.timeout)
        verify_tx_frame(tx_frame)

        wait_for_serial_token(serial_log, "TX DONE", timeout=args.timeout)

        host_sock.sendto(build_rx_frame(), ("127.0.0.1", qemu_udp_port))
        serial_text = wait_for_pass(serial_log, timeout=args.timeout)

        qemu.terminate()
        qemu.wait(timeout=10)
        qemu = None

        print("serial output:")
        print(serial_text)
        print(f"guest TX verified over UDP port {host_udp_port}")
        print(f"guest RX injected via UDP port {qemu_udp_port}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("--- serial tail ---", file=sys.stderr)
        print(tail_text(serial_log), file=sys.stderr)
        print("--- qemu tail ---", file=sys.stderr)
        print(tail_text(qemu_log), file=sys.stderr)
        print("--- soc-simulator tail ---", file=sys.stderr)
        print(tail_text(socsim_log), file=sys.stderr)
        return 1
    finally:
        host_sock.close()
        if qemu is not None:
            qemu.terminate()
            try:
                qemu.wait(timeout=5)
            except subprocess.TimeoutExpired:
                qemu.kill()
        socsim.terminate()
        try:
            socsim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            socsim.kill()


if __name__ == "__main__":
    sys.exit(main())