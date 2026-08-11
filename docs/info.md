<!---
This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.
-->

## How it works

This is a **Number Theoretic Transform (NTT) butterfly** — the innermost
arithmetic operation of lattice-based post-quantum cryptography. One
time-shared serial datapath serves all three moduli standardised by NIST:

| Scheme | Standard | q | Structure | Iterations |
|---|---|---|---|---|
| ML-KEM | FIPS 203 | 3329 | 13 · 2⁸ + 1 | 12 |
| ML-DSA | FIPS 204 | 8380417 | 1023 · 2¹³ + 1 | 23 |
| FN-DSA (Falcon) | draft | 12289 | 3 · 2¹² + 1 | 14 |

Both transform directions are supported:

```
Cooley-Tukey (forward)      Gentleman-Sande (inverse)
  t    = b · ζ  mod q         out0 = a + b     mod q
  out0 = a + t  mod q         t    = a − b     mod q
  out1 = a − t  mod q         out1 = t · ζ     mod q
```

**No general multiplier.** A parallel 23×23 multiplier would be roughly 1,500–2,500
cells in Sky130 — more than the whole budget. Instead the product is computed
bit-serially, most-significant-bit first:

```
acc ← 2·acc                 (a wired shift, zero gates)
acc ← acc + x  if ζᵢ         (an AND array plus one adder)
acc ← acc − q  if needed     (two conditional subtractions)
```

Because `acc < q` on entry, `2·acc + x < 3q`, so **two** conditional subtractions
always suffice — never one, never three. They run in parallel rather than in
series, so the critical path is add → subtract → select.

**One adder, not two.** Cooley-Tukey needs the modular adder only *after* the
multiply; Gentleman-Sande only *before*. They are never simultaneously active,
so a single add/subtract unit serves both, with a multiplexer on one input port.

**Concurrent fault detection.** A mod-3 residue code runs alongside the multiply
loop. Since every step has the form `acc' = 2·acc + x − c·q` with a known
subtraction count `c`, the residue is predictable:

```
r' = (2·r + ζᵢ·rₓ − c·r_q)  mod 3
```

Extracting a residue is cheap because 2 ≡ −1 (mod 3), making it an alternating
bit-sum rather than a division. The prediction is compared against a freshly
computed residue at the end of the loop; a mismatch raises `FAULT`. Simulated
single-bit injection into the accumulator is caught about 97% of the time.

**On-chip twiddle recurrence.** After each butterfly the design computes
ζ ← ζ · ζ_step, reusing the same serial multiplier. Successive twiddle factors
therefore do not have to be supplied over the pins. `ζ_step` resets to 1, which
makes the update a no-op unless you load something else.

**Constant time.** Execution takes 31 / 35 / 53 cycles for ML-KEM / Falcon /
ML-DSA — identical for both transform directions and independent of every
operand value. Skipping iterations when ζᵢ = 0 would nearly halve the average
latency, but it would leak the twiddle's Hamming weight through timing, so it is
deliberately not done.

## How to test

All values are 23-bit, sent as **3 little-endian bytes** (the top byte carries
only 7 bits).

**Control pins (`uio[5:0]`, inputs):**

| Pin | Name | Function |
|---|---|---|
| uio[0] | WR | Rising edge latches `ui_in` into the load chain |
| uio[1] | START | Rising edge begins a butterfly, resets both pointers |
| uio[2] | RD | Rising edge advances the result byte pointer |
| uio[3] | MODE | 0 = Cooley-Tukey, 1 = Gentleman-Sande |
| uio[5:4] | SCHEME | 00 = ML-KEM, 01 = ML-DSA, 10 = Falcon |

**Status pins (`uio[7:6]`, outputs):** `uio[6]` = BUSY, `uio[7]` = FAULT.

**Sequence:**

1. Set MODE and SCHEME.
2. Pulse WR twelve times with the bytes of `a`, `b`, `ζ`, `ζ_step` in that order.
3. Pulse START. Wait for BUSY to fall.
4. Pulse RD, reading `uo_out` before each pulse, to collect six bytes:
   `out0` then `out1`.
5. Check FAULT is low.

For subsequent butterflies you only need to rewrite `a` and `b` (six bytes),
since START resets the write pointer and ζ is maintained on-chip.

**Worked example (ML-KEM, forward, a = 1, b = 1, ζ = 17, ζ_step = 1):**
t = 17, so `out0` = 18 and `out1` = 3329 − 16 = 3313.

## External hardware

None. A microcontroller, the Tiny Tapeout demo board, or an FPGA driving the
control and data pins is sufficient.
