/*
 * Copyright (c) 2026 H Vinayaka
 * SPDX-License-Identifier: Apache-2.0
 *
 * Crypto-agile, fault-detecting NTT butterfly for ML-KEM, ML-DSA and FN-DSA.
 *
 * One time-shared serial datapath computes the Cooley-Tukey (forward) and
 * Gentleman-Sande (inverse) butterfly for three NIST post-quantum moduli:
 *
 *     ML-KEM   (FIPS 203)  q = 3329     = 13   * 2^8  + 1   W = 12
 *     ML-DSA   (FIPS 204)  q = 8380417  = 1023 * 2^13 + 1   W = 23
 *     FN-DSA   (Falcon)    q = 12289    = 3    * 2^12 + 1   W = 14
 *
 * Multiplication is bit-serial double-and-add with two parallel conditional
 * subtractions; there is no general multiplier anywhere in the design.
 * A mod-3 residue code runs alongside the multiply loop and raises `fault`
 * on mismatch.  Execution time depends only on the (public) scheme select,
 * never on operand values.
 */

`default_nettype none

module tt_um_vinayaka_pqc_ntt (
    input  wire [7:0] ui_in,    // Dedicated inputs  - data byte
    output wire [7:0] uo_out,   // Dedicated outputs - result byte
    input  wire [7:0] uio_in,   // IOs: Input path   - control
    output wire [7:0] uio_out,  // IOs: Output path  - status
    output wire [7:0] uio_oe,   // IOs: Enable path  (1 = output)
    input  wire       ena,      // always 1 when powered - ignored
    input  wire       clk,
    input  wire       rst_n     // active low reset
);

  // ---------------------------------------------------------------------
  // Control pins
  // ---------------------------------------------------------------------
  wire       wr_i     = uio_in[0];  // strobe: latch ui_in into load chain
  wire       start_i  = uio_in[1];  // strobe: begin a butterfly
  wire       rd_i     = uio_in[2];  // strobe: advance result byte pointer
  wire       mode_i   = uio_in[3];  // 0 = Cooley-Tukey, 1 = Gentleman-Sande
  wire [1:0] scheme_i = uio_in[5:4];

  // ---------------------------------------------------------------------
  // Scheme constants
  // ---------------------------------------------------------------------
  localparam [22:0] Q_KEM = 23'd3329;
  localparam [22:0] Q_DSA = 23'd8380417;
  localparam [22:0] Q_FAL = 23'd12289;

  reg [1:0] scheme_r;
  reg       mode_r;

  reg [22:0] q;
  reg [4:0]  wlen;
  reg [1:0]  rq3;  // q mod 3

  always @(*) begin
    case (scheme_r)
      2'd1: begin q = Q_DSA; wlen = 5'd23; rq3 = 2'd1; end
      2'd2: begin q = Q_FAL; wlen = 5'd14; rq3 = 2'd1; end
      default: begin q = Q_KEM; wlen = 5'd12; rq3 = 2'd2; end
    endcase
  end

  // ---------------------------------------------------------------------
  // Mod-3 residue extraction.
  //   4^k = 1 (mod 3), so the value is congruent to the sum of its 2-bit
  //   groups; the same identity folds that sum down to two bits.
  // ---------------------------------------------------------------------
  function automatic [1:0] mod3f;
    input [25:0] v;
    reg [6:0] a1, a2, a3, a4, a5;
    begin
      a1 = {5'b0, v[ 1: 0]} + {5'b0, v[ 3: 2]} + {5'b0, v[ 5: 4]} +
           {5'b0, v[ 7: 6]} + {5'b0, v[ 9: 8]} + {5'b0, v[11:10]} +
           {5'b0, v[13:12]} + {5'b0, v[15:14]} + {5'b0, v[17:16]} +
           {5'b0, v[19:18]} + {5'b0, v[21:20]} + {5'b0, v[23:22]} +
           {5'b0, v[25:24]};
      a2 = {2'b0, a1[6:2]} + {5'b0, a1[1:0]};
      a3 = {2'b0, a2[6:2]} + {5'b0, a2[1:0]};
      a4 = {2'b0, a3[6:2]} + {5'b0, a3[1:0]};
      a5 = (a4 >= 7'd3) ? (a4 - 7'd3) : a4;
      mod3f = a5[1:0];
    end
  endfunction

  function automatic [1:0] m3add;
    input [1:0] p;
    input [1:0] r;
    reg [2:0] s;
    begin
      s = {1'b0, p} + {1'b0, r};
      m3add = (s >= 3'd3) ? (s[1:0] - 2'd3) : s[1:0];
    end
  endfunction

  function automatic [1:0] m3neg;
    input [1:0] p;
    begin
      m3neg = (p == 2'd0) ? 2'd0 : (p == 2'd1) ? 2'd2 : 2'd1;
    end
  endfunction

  // Align bit W-1 of the serial multiplier to position 22 so the scan
  // register can be a plain shift-left.  All shifts are constant.
  function automatic [22:0] preshift;
    input [22:0] z;
    input [1:0]  sc;
    begin
      case (sc)
        2'd1:    preshift = z;
        2'd2:    preshift = {z[13:0], 9'b0};
        default: preshift = {z[11:0], 11'b0};
      endcase
    end
  endfunction

  // ---------------------------------------------------------------------
  // Architectural state
  // ---------------------------------------------------------------------
  reg [22:0] areg, breg, zeta, zstep;  // operands
  reg [22:0] r0, r1;                   // results
  reg [22:0] acc, mcand, scan;         // multiplier
  reg [4:0]  cnt;
  reg [1:0]  rpred, rx3;               // residue prediction
  reg        fault_q, tw;
  reg [3:0]  wptr;
  reg [2:0]  rptr;
  reg        wr_d, start_d, rd_d;

  localparam S_IDLE  = 4'd0,  S_PADD  = 4'd1,  S_PSUB = 4'd2,
             S_MINIT = 4'd3,  S_MUL   = 4'd4,  S_MEND = 4'd5,
             S_POADD = 4'd6,  S_POSUB = 4'd7,  S_TINIT = 4'd8,
             S_DONE  = 4'd9;
  reg [3:0] state;

  wire wr_p    = wr_i    & ~wr_d;
  wire start_p = start_i & ~start_d;
  wire rd_p    = rd_i    & ~rd_d;
  wire busy    = (state != S_IDLE);

  // ---------------------------------------------------------------------
  // Shared modular add / subtract.  X is always `areg`; only port Y is
  // multiplexed.  Reduction is one conditional subtraction, decided by the
  // subtractor's own borrow bit rather than a separate comparator.
  // ---------------------------------------------------------------------
  wire        do_sub = (state == S_PSUB) || (state == S_POSUB);
  wire [22:0] ymux   = ((state == S_POADD) || (state == S_POSUB)) ? acc : breg;

  wire [24:0] apre = do_sub ? ({2'b0, areg} - {2'b0, ymux} + {2'b0, q})
                            : ({2'b0, areg} + {2'b0, ymux});
  wire [25:0] adq    = {1'b0, apre} - {3'b0, q};
  wire [22:0] as_out = ~adq[25] ? adq[22:0] : apre[22:0];

  // ---------------------------------------------------------------------
  // Serial multiply-and-reduce.  acc < q on entry, so
  //   2*acc < 2q  and  2*acc + x < 3q,
  // hence at most two subtractions of q are ever needed.  They run in
  // parallel, so the path is add -> subtract -> select, not three adders.
  // ---------------------------------------------------------------------
  wire        zbit   = scan[22];
  wire [22:0] addend = {23{zbit}} & mcand;
  wire [24:0] tsum   = {1'b0, acc, 1'b0} + {2'b0, addend};

  wire [25:0] dq1 = {1'b0, tsum} - {3'b0, q};
  wire [25:0] dq2 = {1'b0, tsum} - {2'b0, q, 1'b0};
  wire        s2  = ~dq2[25];
  wire        s1  = ~dq1[25];

  wire [22:0] acc_next = s2 ? dq2[22:0] : (s1 ? dq1[22:0] : tsum[22:0]);
  wire [1:0]  csub     = s2 ? 2'd2 : (s1 ? 2'd1 : 2'd0);

  // Predicted residue:  r' = (2r + b.rx - c.rq) mod 3
  wire [1:0] rdbl  = (rpred == 2'd0) ? 2'd0 : (rpred == 2'd1) ? 2'd2 : 2'd1;
  wire [1:0] radd  = zbit ? rx3 : 2'd0;
  wire [1:0] rsum  = m3add(rdbl, radd);
  wire [1:0] rq2   = m3add(rq3, rq3);
  wire [1:0] rsubv = (csub == 2'd0) ? 2'd0 : (csub == 2'd1) ? rq3 : rq2;
  wire [1:0] rpred_next = m3add(rsum, m3neg(rsubv));

  // One shared mod-3 extractor, time-multiplexed between the two uses.
  wire [22:0] m3src = (state == S_MEND)  ? acc  :
                      (state == S_TINIT) ? zeta : breg;
  wire [1:0]  m3val = mod3f({3'b0, m3src});

  // ---------------------------------------------------------------------
  // Sequential logic
  // ---------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      areg <= 23'd0; breg <= 23'd0; zeta <= 23'd0; zstep <= 23'd1;
      r0   <= 23'd0; r1   <= 23'd0;
      acc  <= 23'd0; mcand <= 23'd0; scan <= 23'd0;
      cnt  <= 5'd0;  rpred <= 2'd0;  rx3  <= 2'd0;
      fault_q <= 1'b0; tw <= 1'b0;
      wptr <= 4'd0; rptr <= 3'd0;
      wr_d <= 1'b0; start_d <= 1'b0; rd_d <= 1'b0;
      scheme_r <= 2'd0; mode_r <= 1'b0;
      state <= S_IDLE;
    end else begin
      wr_d    <= wr_i;
      start_d <= start_i;
      rd_d    <= rd_i;

      // ---- host writes: 12 bytes, little-endian, a / b / zeta / zstep ----
      if (wr_p && !busy) begin
        case (wptr)
          4'd0:  areg[7:0]    <= ui_in;
          4'd1:  areg[15:8]   <= ui_in;
          4'd2:  areg[22:16]  <= ui_in[6:0];
          4'd3:  breg[7:0]    <= ui_in;
          4'd4:  breg[15:8]   <= ui_in;
          4'd5:  breg[22:16]  <= ui_in[6:0];
          4'd6:  zeta[7:0]    <= ui_in;
          4'd7:  zeta[15:8]   <= ui_in;
          4'd8:  zeta[22:16]  <= ui_in[6:0];
          4'd9:  zstep[7:0]   <= ui_in;
          4'd10: zstep[15:8]  <= ui_in;
          4'd11: zstep[22:16] <= ui_in[6:0];
          default: ;
        endcase
        wptr <= (wptr == 4'd11) ? 4'd0 : (wptr + 4'd1);
      end

      // ---- host reads: 6 bytes, r0 then r1 ----
      if (rd_p) rptr <= (rptr == 3'd5) ? 3'd0 : (rptr + 3'd1);

      // ---- datapath FSM ----
      case (state)
        S_IDLE: begin
          if (start_p) begin
            scheme_r <= scheme_i;
            mode_r   <= mode_i;
            fault_q  <= 1'b0;
            wptr     <= 4'd0;
            rptr     <= 3'd0;
            state    <= mode_i ? S_PADD : S_MINIT;
          end
        end

        // Gentleman-Sande: combine before multiplying.  The add must come
        // first, because the subtract overwrites breg with a - b.
        S_PADD: begin
          r0    <= as_out;
          state <= S_PSUB;
        end
        S_PSUB: begin
          breg  <= as_out;
          state <= S_MINIT;
        end

        S_MINIT: begin
          acc   <= 23'd0;
          mcand <= breg;
          scan  <= preshift(zeta, scheme_r);
          cnt   <= wlen;
          rpred <= 2'd0;
          rx3   <= m3val;
          tw    <= 1'b0;
          state <= S_MUL;
        end

        S_MUL: begin
          acc   <= acc_next;
          rpred <= rpred_next;
          scan  <= {scan[21:0], 1'b0};
          if (cnt == 5'd1) state <= S_MEND;
          else             cnt   <= cnt - 5'd1;
        end

        S_MEND: begin
          if (rpred != m3val) fault_q <= 1'b1;
          if (!tw) begin
            if (mode_r) begin
              r1    <= acc;        // GS: out1 is the product itself
              state <= S_TINIT;
            end else begin
              state <= S_POADD;
            end
          end else begin
            zeta  <= acc;          // twiddle recurrence result
            state <= S_DONE;
          end
        end

        // Cooley-Tukey: combine after multiplying.
        S_POADD: begin
          r0    <= as_out;
          state <= S_POSUB;
        end
        S_POSUB: begin
          r1    <= as_out;
          state <= S_TINIT;
        end

        // zeta <- zeta * zstep, reusing the same multiplier.
        // zstep resets to 1, which makes the update a no-op by default.
        S_TINIT: begin
          rx3   <= m3val;
          acc   <= 23'd0;
          mcand <= zeta;
          scan  <= preshift(zstep, scheme_r);
          cnt   <= wlen;
          rpred <= 2'd0;
          tw    <= 1'b1;
          state <= S_MUL;
        end

        S_DONE: state <= S_IDLE;

        default: state <= S_IDLE;
      endcase
    end
  end

  // ---------------------------------------------------------------------
  // Outputs
  // ---------------------------------------------------------------------
  reg [7:0] rbyte;
  always @(*) begin
    case (rptr)
      3'd0:    rbyte = r0[7:0];
      3'd1:    rbyte = r0[15:8];
      3'd2:    rbyte = {1'b0, r0[22:16]};
      3'd3:    rbyte = r1[7:0];
      3'd4:    rbyte = r1[15:8];
      3'd5:    rbyte = {1'b0, r1[22:16]};
      default: rbyte = 8'h00;
    endcase
  end

  assign uo_out  = rbyte;
  assign uio_out = {fault_q, busy, 6'b000000};
  assign uio_oe  = 8'b1100_0000;   // uio[7:6] outputs, uio[5:0] inputs

  wire _unused = &{ena, uio_in[7:6],
                   apre[24:23], adq[24:23],
                   tsum[24:23], dq1[24:23], dq2[24:23], 1'b0};

endmodule
