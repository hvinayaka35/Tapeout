# SPDX-FileCopyrightText: (c) 2026 H Vinayaka
# SPDX-License-Identifier: Apache-2.0

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

# scheme code -> (q, W, name)
SCHEMES = {
    0: (3329, 12, "ML-KEM"),
    1: (8380417, 23, "ML-DSA"),
    2: (12289, 14, "Falcon"),
}

WR, START, RD, MODE = 0x01, 0x02, 0x04, 0x08
BUSY_BIT, FAULT_BIT = 6, 7


def ctrl(mode=0, scheme=0):
    """Static part of the control word."""
    return (MODE if mode else 0) | ((scheme & 3) << 4)


async def strobe(dut, bit, base):
    """Pulse one control bit high for a single clock, then low."""
    dut.uio_in.value = base | bit
    await ClockCycles(dut.clk, 1)
    dut.uio_in.value = base
    await ClockCycles(dut.clk, 1)


async def write_bytes(dut, values, base):
    """Write 23-bit words as 3 little-endian bytes each, in load order."""
    for v in values:
        for shift in (0, 8, 16):
            dut.ui_in.value = (v >> shift) & 0xFF
            await strobe(dut, WR, base)


async def read_result(dut, base):
    """Read out0 and out1 as 3 bytes each."""
    words = []
    for _ in range(2):
        w = 0
        for shift in (0, 8, 16):
            w |= int(dut.uo_out.value) << shift
            await strobe(dut, RD, base)
        words.append(w & 0x7FFFFF)
    return words


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def butterfly(dut, a, b, zeta, zstep, scheme, mode):
    """Load, run, and read back one butterfly.  Returns (out0, out1, fault)."""
    base = ctrl(mode, scheme)
    dut.uio_in.value = base
    await ClockCycles(dut.clk, 1)

    await write_bytes(dut, [a, b, zeta, zstep], base)
    await strobe(dut, START, base)

    for _ in range(200):
        if not (int(dut.uio_out.value) >> BUSY_BIT) & 1:
            break
        await ClockCycles(dut.clk, 1)
    else:
        raise AssertionError("design never left busy")

    fault = (int(dut.uio_out.value) >> FAULT_BIT) & 1
    out0, out1 = await read_result(dut, base)
    return out0, out1, fault


def golden(a, b, zeta, q, mode):
    if mode == 0:  # Cooley-Tukey, forward
        t = (b * zeta) % q
        return (a + t) % q, (a - t) % q
    # Gentleman-Sande, inverse
    return (a + b) % q, ((a - b) * zeta) % q


@cocotb.test()
async def test_all_schemes(dut):
    """Directed vectors across all three moduli and both transform directions."""
    dut._log.info("start")
    cocotb.start_soon(Clock(dut.clk, 20, units="ns").start())
    await reset(dut)

    for scheme, (q, w, name) in SCHEMES.items():
        for mode in (0, 1):
            vectors = [
                (0, 0, 0),
                (1, 1, 1),
                (q - 1, q - 1, q - 1),
                (0, q - 1, 1),
                (q - 1, 0, q - 1),
                (q - 1, 1, q - 1),
                (q // 2, q // 3, q // 5),
            ]
            for a, b, z in vectors:
                o0, o1, f = await butterfly(dut, a, b, z, 1, scheme, mode)
                e0, e1 = golden(a, b, z, q, mode)
                assert (o0, o1) == (e0, e1), (
                    f"{name} mode={mode} a={a} b={b} z={z}: "
                    f"got ({o0},{o1}) expected ({e0},{e1})"
                )
                assert f == 0, f"{name}: spurious fault flag"
            dut._log.info(f"{name} W={w} mode={'GS' if mode else 'CT'}: OK")


@cocotb.test()
async def test_random(dut):
    """Randomised comparison against the golden model."""
    cocotb.start_soon(Clock(dut.clk, 20, units="ns").start())
    await reset(dut)

    rnd = random.Random(0xC0FFEE)
    for scheme, (q, _, name) in SCHEMES.items():
        for mode in (0, 1):
            for _ in range(25):
                a, b, z = (rnd.randrange(q) for _ in range(3))
                o0, o1, f = await butterfly(dut, a, b, z, 1, scheme, mode)
                e0, e1 = golden(a, b, z, q, mode)
                assert (o0, o1) == (e0, e1), (
                    f"{name} mode={mode} a={a} b={b} z={z}: "
                    f"got ({o0},{o1}) expected ({e0},{e1})"
                )
                assert f == 0
    dut._log.info("randomised: OK")


@cocotb.test()
async def test_twiddle_recurrence(dut):
    """zeta should advance by zstep after each butterfly."""
    cocotb.start_soon(Clock(dut.clk, 20, units="ns").start())

    for scheme, (q, _, name) in SCHEMES.items():
        # Reset per scheme: the write pointer auto-increments and is only
        # rewound by START or reset, so a partial 6-byte write at the end of
        # the previous scheme would otherwise leave it at offset 6.
        await reset(dut)

        zeta, zstep = 17 % q, 5 % q
        base = ctrl(0, scheme)
        dut.uio_in.value = base
        await ClockCycles(dut.clk, 1)
        await write_bytes(dut, [1, 1, zeta, zstep], base)

        for step in range(4):
            await strobe(dut, START, base)
            for _ in range(200):
                if not (int(dut.uio_out.value) >> BUSY_BIT) & 1:
                    break
                await ClockCycles(dut.clk, 1)
            else:
                raise AssertionError(f"{name} step {step}: never left busy")
            out0, out1 = await read_result(dut, base)
            # CT with a = b = 1: out0 = 1 + zeta, out1 = 1 - zeta
            assert out0 == (1 + zeta) % q, (
                f"{name} step {step}: out0 got {out0} expected {(1 + zeta) % q}"
            )
            assert out1 == (1 - zeta) % q, (
                f"{name} step {step}: out1 got {out1} expected {(1 - zeta) % q}"
            )
            zeta = (zeta * zstep) % q
            # START rewound the write pointer, so only a and b need rewriting;
            # zeta is now maintained on-chip by the recurrence.
            await write_bytes(dut, [1, 1], base)
        dut._log.info(f"{name} twiddle recurrence: OK")


@cocotb.test()
async def test_constant_time(dut):
    """Cycle count must depend only on the scheme, never on operand values."""
    cocotb.start_soon(Clock(dut.clk, 20, units="ns").start())
    await reset(dut)

    async def cycles_for(a, b, z, scheme, mode):
        base = ctrl(mode, scheme)
        dut.uio_in.value = base
        await ClockCycles(dut.clk, 1)
        await write_bytes(dut, [a, b, z, 1], base)
        await strobe(dut, START, base)
        assert (int(dut.uio_out.value) >> BUSY_BIT) & 1, "busy never asserted"
        n = 0
        while (int(dut.uio_out.value) >> BUSY_BIT) & 1:
            await ClockCycles(dut.clk, 1)
            n += 1
            assert n < 200
        return n

    for scheme, (q, _, name) in SCHEMES.items():
        counts = set()
        for mode in (0, 1):
            for a, b, z in [(0, 0, 0), (q - 1, q - 1, q - 1), (1, 2, 3), (7, q - 2, q // 2)]:
                counts.add(await cycles_for(a, b, z, scheme, mode))
        assert len(counts) == 1, f"{name}: timing varies with data or mode: {counts}"
        dut._log.info(f"{name}: constant {counts.pop()} cycles")
