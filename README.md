![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Crypto-Agile, Fault-Detecting NTT Butterfly

A single serial datapath computing the Number Theoretic Transform butterfly for
**all three NIST post-quantum standards** — ML-KEM (FIPS 203), ML-DSA (FIPS 204)
and FN-DSA / Falcon — with concurrent fault detection, on one Tiny Tapeout
1x2 tile in SkyWater 130 nm.

- [Read the full datasheet](docs/info.md)

## What makes it small

| Technique | Effect |
|---|---|
| Bit-serial double-and-add | Removes the 23×23 multiplier (~1,500–2,500 cells) |
| Two parallel conditional subtractions | Exact reduction with no correction factor |
| Time-shared modular adder | One add/sub unit instead of two combine stages |
| Write-back into register `b` | Removes the multiplier operand multiplexer |
| Shared mod-3 extractor | One residue network instead of two |

Only two multiplexers survive in the whole datapath, each with a single output:
one on the adder's Y port, one on `out1`.

## What makes it novel

- **Tri-modular.** All three moduli share the form q = k·2ᵐ + 1, so one reduction
  datapath serves them; adding Falcon to a two-scheme design costs ~35 cells.
- **Fault detecting.** A mod-3 residue code predicts the accumulator's residue
  through the serial loop and checks it at the end, in a single pass with no
  latency penalty. Fault-injection attacks on the NTT are an active threat to
  FIPS 203/204 implementations and almost no open hardware ships with detection.
- **Constant time by construction.** Latency depends only on the public scheme
  selector: 31 / 35 / 53 cycles, identical for forward and inverse transforms.

## Verification

`test/test.py` checks all three moduli and both transform directions against a
Python golden model, verifies the on-chip twiddle recurrence over successive
butterflies, and asserts that cycle count never varies with operand values.

```console
cd test
make            # RTL simulation
make GATES=yes  # gate-level simulation, after the GDS action has run
```

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper
than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit <https://tinytapeout.com>.

## License

Apache-2.0
