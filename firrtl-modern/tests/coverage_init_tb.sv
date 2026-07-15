module tb;
  logic clock = 0;
  logic reset = 1;
  logic a = 0;
  logic metaReset = 1;
  wire y;
  wire [29:0] io_covSum;
  wire metaAssert;

  Top dut (.*);

  task automatic tick(input logic nextReset, nextA, nextMetaReset);
    reset = nextReset;
    a = nextA;
    metaReset = nextMetaReset;
    #1 clock = 1;
    #1 clock = 0;
    #1;
  endtask

  initial begin
    #1;
    $display("COV initial=%0d", io_covSum);
    if (io_covSum != 0)
      $fatal(1, "coverage metadata did not start at zero");

    tick(1, 0, 1);
    $display("COV first_state=%0d", io_covSum);
    if (io_covSum != 1)
      $fatal(1, "first control state did not increment coverage exactly once");
    tick(1, 0, 0);
    tick(0, 0, 0);
    tick(0, 0, 0);
    tick(0, 1, 0);
    tick(0, 1, 0);
    tick(0, 1, 0);
    $display("COV before_meta_reset=%0d", io_covSum);
    if (io_covSum != 2)
      $fatal(1, "expected exactly two distinct control states");

    tick(0, 1, 1);
    tick(0, 1, 0);
    $display("COV after_meta_reset=%0d", io_covSum);
    if (io_covSum != 2)
      $fatal(1, "metaReset cleared cumulative coverage metadata");
    $finish;
  end
endmodule
