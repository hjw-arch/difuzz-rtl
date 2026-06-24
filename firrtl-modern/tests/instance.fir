FIRRTL version 3.3.0
circuit Top :
  module Child :
    input in : UInt<1>
    output out : UInt<1>
    out <= in

  module Top :
    input a : UInt<1>
    output y : UInt<1>
    inst c of Child
    c.in <= a
    y <= c.out
