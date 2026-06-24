FIRRTL version 3.3.0
circuit Top :
  module Top :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>

    reg r : UInt<1>, clock with : (reset => (reset, UInt<1>(0h0)))
    y <= mux(r, a, UInt<1>(0h0))
    r <= a
