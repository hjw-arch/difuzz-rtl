FIRRTL version 3.3.0
circuit Top :
  module Top :
    input clock : Clock
    input reset : UInt<1>
    input halt : UInt<1>
    output y : UInt<1>

    y <= halt
    stop(clock, halt, 1)
