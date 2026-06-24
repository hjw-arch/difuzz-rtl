FIRRTL version 3.3.0
circuit Top :
  module Top :
    input clock0 : Clock
    input clock1 : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>

    reg r : UInt<1>, clock1
    y <= mux(r, a, UInt<1>(0h0))
    r <= a
