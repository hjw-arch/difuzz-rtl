FIRRTL version 3.3.0
circuit Top :
  module TargetLeaf :
    input a : UInt<1>
    output y : UInt<1>
    y <= a

  module TargetRoot :
    input a : UInt<1>
    output y : UInt<1>
    inst leaf of TargetLeaf
    leaf.a <= a
    y <= leaf.y

  module Top :
    input a : UInt<1>
    output y : UInt<1>
    inst target of TargetRoot
    inst leaked of TargetLeaf
    target.a <= a
    leaked.a <= a
    y <= and(target.y, leaked.y)
