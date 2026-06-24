FIRRTL version 3.3.0
circuit Top :
  module Top :
    input clock : Clock
    input reset : UInt<1>
    input in0 : UInt<2>
    input in1 : UInt<2>
    input d0 : UInt<1>
    input d1 : UInt<1>
    output y : UInt<1>

    reg lane_0_state : UInt<2>, clock with : (reset => (reset, UInt<2>(0h0))) @[Vec.scala 10:7]
    reg lane_1_state : UInt<2>, clock with : (reset => (reset, UInt<2>(0h0))) @[Vec.scala 10:7]

    lane_0_state <= in0
    lane_1_state <= in1

    node c0 = neq(lane_0_state, UInt<2>(0h0))
    node c1 = neq(lane_1_state, UInt<2>(0h0))
    node m0 = mux(c0, d1, d0)
    node m1 = mux(c1, d1, d0)
    y <= or(m0, m1)
