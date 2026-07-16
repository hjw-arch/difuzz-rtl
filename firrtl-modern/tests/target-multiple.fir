FIRRTL version 3.3.0
circuit Top :
  module TargetRoot :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    reg state : UInt<1>, clock with : (reset => (reset, UInt<1>(0h0)))
    state <= a
    y <= mux(state, a, UInt<1>(0h0))

  module Top :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    inst first of TargetRoot
    inst second of TargetRoot
    first.clock <= clock
    first.reset <= reset
    first.a <= a
    second.clock <= clock
    second.reset <= reset
    second.a <= a
    y <= or(first.y, second.y)
