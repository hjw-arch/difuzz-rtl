FIRRTL version 3.3.0
circuit Top :
  module Top :
    input clock : Clock
    input reset : UInt<1>
    input in_a : UInt<1>
    input in_b : UInt<1>
    input d0 : UInt<1>
    input d1 : UInt<1>
    output y : UInt<1>

    reg r : {a : UInt<1>, b : UInt<1>}, clock
    r.a <= in_a
    r.b <= in_b
    y <= mux(r.a, d1, d0)
