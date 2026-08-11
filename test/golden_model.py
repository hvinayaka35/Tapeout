"""Cycle-accurate model of tt_um_vinayaka_pqc_ntt, mirroring RTL structure exactly."""
import random

SCHEMES = {0: (3329, 12, 2), 1: (8380417, 23, 1), 2: (12289, 14, 1)}
M25 = (1 << 25) - 1
M26 = (1 << 26) - 1


def mod3f(v):
    """Mirrors the Verilog fold: sum 2-bit groups, then fold by 4, then correct."""
    a1 = 0
    for i in range(0, 26, 2):
        a1 += (v >> i) & 3
    assert a1 <= 39
    a2 = (a1 >> 2) + (a1 & 3)
    a3 = (a2 >> 2) + (a2 & 3)
    a4 = (a3 >> 2) + (a3 & 3)
    return a4 - 3 if a4 >= 3 else a4


def m3add(p, r):
    s = p + r
    return s - 3 if s >= 3 else s


def preshift(z, scheme):
    if scheme == 1:
        return z
    if scheme == 2:
        return (z << 9) & 0x7FFFFF
    return (z << 11) & 0x7FFFFF


class DUT:
    def __init__(self):
        self.areg = self.breg = self.zeta = 0
        self.zstep = 1
        self.r0 = self.r1 = 0
        self.acc = self.mcand = self.scan = 0
        self.cnt = 0
        self.rpred = self.rx3 = 0
        self.fault = 0
        self.state = 'IDLE'
        self.tw = 0
        self.scheme = 0
        self.mode = 0

    # ---- combinational ----
    def consts(self):
        return SCHEMES[self.scheme if self.scheme in SCHEMES else 0]

    def mul_step(self):
        q, _, _ = self.consts()
        zbit = (self.scan >> 22) & 1
        addend = self.mcand if zbit else 0
        tsum = ((self.acc << 1) + addend) & M25
        dq1 = (tsum - q) & M26
        dq2 = (tsum - 2 * q) & M26
        sub2 = not ((dq2 >> 25) & 1)
        sub1 = not ((dq1 >> 25) & 1)
        if sub2:
            acc_n, csub = dq2 & 0x7FFFFF, 2
        elif sub1:
            acc_n, csub = dq1 & 0x7FFFFF, 1
        else:
            acc_n, csub = tsum & 0x7FFFFF, 0
        return acc_n, csub, zbit

    def rpred_step(self, csub, zbit):
        _, _, rq3 = self.consts()
        rdbl = {0: 0, 1: 2, 2: 1}[self.rpred]
        radd = self.rx3 if zbit else 0
        rsum = m3add(rdbl, radd)
        rq2 = m3add(rq3, rq3)
        rsubv = {0: 0, 1: rq3, 2: rq2}[csub]
        rneg = {0: 0, 1: 2, 2: 1}[rsubv]
        return m3add(rsum, rneg)

    def addsub(self, do_sub, y):
        q, _, _ = self.consts()
        if do_sub:
            apre = (self.areg - y + q) & M25
        else:
            apre = (self.areg + y) & M25
        adq = (apre - q) & M26
        return (adq & 0x7FFFFF) if not ((adq >> 25) & 1) else (apre & 0x7FFFFF)

    def m3src(self):
        if self.state == 'MEND':
            return self.acc
        if self.state == 'TINIT':
            return self.zeta
        return self.breg

    # ---- sequential ----
    def tick(self):
        q, wlen, rq3 = self.consts()
        s = self.state
        if s == 'PADD':
            self.r0 = self.addsub(0, self.breg)
            self.state = 'PSUB'
        elif s == 'PSUB':
            self.breg = self.addsub(1, self.breg)
            self.state = 'MINIT'
        elif s == 'MINIT':
            self.acc = 0
            self.mcand = self.breg
            self.scan = preshift(self.zeta, self.scheme)
            self.cnt = wlen
            self.rpred = 0
            self.rx3 = mod3f(self.m3src())
            self.tw = 0
            self.state = 'MUL'
        elif s == 'MUL':
            acc_n, csub, zbit = self.mul_step()
            self.rpred = self.rpred_step(csub, zbit)
            self.acc = acc_n
            self.scan = (self.scan << 1) & 0x7FFFFF
            if self.cnt == 1:
                self.state = 'MEND'
            else:
                self.cnt -= 1
        elif s == 'MEND':
            if self.rpred != mod3f(self.acc):
                self.fault = 1
            if not self.tw:
                if self.mode:
                    self.r1 = self.acc
                    self.state = 'TINIT'
                else:
                    self.state = 'POADD'
            else:
                self.zeta = self.acc
                self.state = 'DONE'
        elif s == 'POADD':
            self.r0 = self.addsub(0, self.acc)
            self.state = 'POSUB'
        elif s == 'POSUB':
            self.r1 = self.addsub(1, self.acc)
            self.state = 'TINIT'
        elif s == 'TINIT':
            self.rx3 = mod3f(self.m3src())
            self.acc = 0
            self.mcand = self.zeta
            self.scan = preshift(self.zstep, self.scheme)
            self.cnt = wlen
            self.rpred = 0
            self.tw = 1
            self.state = 'MUL'
        elif s == 'DONE':
            self.state = 'IDLE'

    def start(self, scheme, mode):
        self.scheme, self.mode = scheme, mode
        self.state = 'PADD' if mode else 'MINIT'
        for _ in range(400):
            if self.state == 'IDLE':
                break
            self.tick()
        else:
            raise RuntimeError('hang')


def golden(a, b, z, q, mode):
    if mode == 0:
        t = (b * z) % q
        return (a + t) % q, (a - t) % q
    return (a + b) % q, ((a - b) * z) % q


def run(trials=40000):
    rnd = random.Random(7)
    for scheme, (q, w, _) in SCHEMES.items():
        for mode in (0, 1):
            d = DUT()
            for i in range(trials // 6):
                a, b, z = (rnd.randrange(q) for _ in range(3))
                zs = rnd.randrange(q)
                d.areg, d.breg, d.zeta, d.zstep = a, b, z, zs
                d.fault = 0
                d.start(scheme, mode)
                e0, e1 = golden(a, b, z, q, mode)
                assert (d.r0, d.r1) == (e0, e1), (scheme, mode, a, b, z, d.r0, d.r1, e0, e1)
                assert d.zeta == (z * zs) % q, ('twiddle', scheme, z, zs, d.zeta)
                assert d.fault == 0, ('false fault', scheme, mode, a, b, z)
            # boundary vectors
            for a, b, z in [(0, 0, 0), (q - 1, q - 1, q - 1), (0, q - 1, 1),
                            (q - 1, 0, q - 1), (1, 1, 1), (q - 1, 1, q - 1)]:
                d.areg, d.breg, d.zeta, d.zstep = a, b, z, 1
                d.fault = 0
                d.start(scheme, mode)
                assert (d.r0, d.r1) == golden(a, b, z, q, mode), (scheme, mode, a, b, z)
                assert d.zeta == z and d.fault == 0
    print('functional: PASS')


def fault_check():
    """Inject a single-bit flip in acc mid-loop; confirm detection rate."""
    rnd = random.Random(3)
    caught = total = 0
    for scheme, (q, w, _) in SCHEMES.items():
        for _ in range(3000):
            d = DUT()
            d.areg, d.breg, d.zeta, d.zstep = (rnd.randrange(q) for _ in range(3)), 0, 0, 0
            d.areg, d.breg, d.zeta, d.zstep = rnd.randrange(q), rnd.randrange(q), rnd.randrange(q), 1
            d.scheme, d.mode = scheme, rnd.randrange(2)
            d.state = 'PADD' if d.mode else 'MINIT'
            inject = rnd.randrange(1, w)
            n = 0
            for _ in range(400):
                if d.state == 'IDLE':
                    break
                d.tick()
                if d.state == 'MUL' and not d.tw:
                    n += 1
                    if n == inject:
                        bit = rnd.randrange(q.bit_length())
                        d.acc ^= (1 << bit)
                        if d.acc >= q:
                            d.acc ^= (1 << bit)
                            continue
            total += 1
            caught += d.fault
    print(f'fault detection: {caught}/{total} = {100*caught/total:.1f}%')


def cycle_count():
    for scheme, (q, w, _) in SCHEMES.items():
        for mode in (0, 1):
            d = DUT()
            d.areg, d.breg, d.zeta, d.zstep = 5, 7, 11, 1
            d.scheme, d.mode = scheme, mode
            d.state = 'PADD' if mode else 'MINIT'
            n = 0
            while d.state != 'IDLE' and n < 400:
                d.tick()
                n += 1
            print(f'  scheme={scheme} (W={w}) mode={"GS" if mode else "CT"}: {n} cycles')


if __name__ == '__main__':
    run()
    fault_check()
    print('cycle counts:')
    cycle_count()
