// Modern CIRCT/FIRRTL entry point for DifuzzRTL register coverage.
//
// This file is intentionally narrow at this stage: it provides a loadable
// Low-FIRRTL pass and checks the modern operations that must be covered by the
// full semantic port of legacy coverage.regCoverage.

#include "circt/Dialect/FIRRTL/FIRRTLOps.h"
#include "circt/Dialect/FIRRTL/FIRRTLTypes.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Diagnostics.h"
#include "mlir/IR/Verifier.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Tools/Plugins/PassPlugin.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/ADT/TypeSwitch.h"
#include "llvm/Support/Compiler.h"
#include "llvm/ADT/APSInt.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include <algorithm>
#include <cctype>
#include <fstream>
#include <map>
#include <optional>
#include <set>
#include <vector>

using namespace mlir;
using namespace circt;

namespace {

enum class NodeKind { Port, Wire, Register, Memory, Instance, Node };

enum class StatePlanMode { Compressed, LegacyLike };

static std::optional<StatePlanMode> parseStatePlanMode(StringRef value) {
  auto normalized = value.trim().lower();
  if (normalized == "compressed")
    return StatePlanMode::Compressed;
  if (normalized == "legacy-like" || normalized == "legacylike" ||
      normalized == "legacy")
    return StatePlanMode::LegacyLike;
  return std::nullopt;
}

static const char *statePlanModeName(StatePlanMode mode) {
  switch (mode) {
  case StatePlanMode::Compressed:
    return "compressed";
  case StatePlanMode::LegacyLike:
    return "legacy-like";
  }
  return "unknown";
}

struct DeclNode {
  NodeKind kind;
  std::string name;
  Operation *op = nullptr;
  Value value;
  std::optional<unsigned> width;
};

struct VectorGroup {
  unsigned size = 0;
  std::string prefix;
  std::set<std::string> bodies;
};

struct ModuleAudit {
  unsigned ports = 0;
  unsigned regs = 0;
  unsigned regResets = 0;
  unsigned muxes = 0;
  unsigned instances = 0;
  unsigned ctrlRegs = 0;
  unsigned directInputRegs = 0;
  unsigned excludedDirectInputRegs = 0;
  unsigned eligibleCtrlRegs = 0;
  unsigned largeCtrlRegs = 0;
  unsigned smallCtrlRegs = 0;
  unsigned vectorGroups = 0;
  unsigned vectorRegs = 0;
  unsigned optRegs = 0;
  unsigned uncoveredCtrlSigs = 0;
  unsigned totalStateBits = 0;
  unsigned regStateSize = 0;
  unsigned covMapSize = 0;
};

struct CoverageItem {
  Value value;
  unsigned width = 0;
  unsigned offset = 0;
  std::string name;
  bool isMuxCondition = false;
};

struct CoveragePlan {
  unsigned totalStateBits = 0;
  unsigned regStateSize = 0;
  unsigned covMapSize = 0;
  SmallVector<CoverageItem> items;
};

struct CircuitAudit {
  unsigned modules = 0;
  unsigned modulesWithCtrl = 0;
  unsigned modulesWithState = 0;
  unsigned regs = 0;
  unsigned regResets = 0;
  unsigned muxes = 0;
  unsigned instances = 0;
  unsigned ctrlRegs = 0;
  unsigned eligibleCtrlRegs = 0;
  unsigned totalStateBits = 0;
  unsigned cappedStateBits = 0;

  void add(const ModuleAudit &audit) {
    ++modules;
    if (audit.ctrlRegs)
      ++modulesWithCtrl;
    if (audit.regStateSize)
      ++modulesWithState;
    regs += audit.regs;
    regResets += audit.regResets;
    muxes += audit.muxes;
    instances += audit.instances;
    ctrlRegs += audit.ctrlRegs;
    eligibleCtrlRegs += audit.eligibleCtrlRegs;
    totalStateBits += audit.totalStateBits;
    cappedStateBits += audit.regStateSize;
  }
};

struct ModuleCovSumInfo {
  firrtl::FModuleOp module;
  bool selected = true;
  unsigned oldPortCount = 0;
  unsigned covSumPortIndex = 0;
  unsigned metaAssertPortIndex = 0;
  unsigned metaResetPortIndex = 0;
  CoveragePlan plan;
  SmallVector<Value> childCovSums;
  SmallVector<Value> childMetaResets;
  SmallVector<Value> childMetaAsserts;
  SmallVector<std::pair<unsigned, firrtl::PortInfo>> insertedPorts;
  Value localCovSum;
  Value localMetaAssert;
};

static LogicalResult selectTargetModules(firrtl::CircuitOp circuit,
                                         StringRef targetModule,
                                         llvm::StringSet<> &selected) {
  llvm::StringMap<firrtl::FModuleOp> modules;
  circuit.walk([&](firrtl::FModuleOp module) {
    modules.try_emplace(module.getName(), module);
  });

  if (targetModule.empty()) {
    for (auto &entry : modules)
      selected.insert(entry.getKey());
    return success();
  }

  auto root = modules.find(targetModule);
  if (root == modules.end())
    return circuit.emitError() << "DifuzzRTL regCoverage target module `"
                               << targetModule << "` does not exist";

  SmallVector<firrtl::FModuleOp> worklist{root->second};
  while (!worklist.empty()) {
    auto module = worklist.pop_back_val();
    if (!selected.insert(module.getName()).second)
      continue;
    module.walk([&](firrtl::InstanceOp inst) {
      auto child = modules.find(inst.getModuleName());
      if (child != modules.end())
        worklist.push_back(child->second);
    });
  }

  for (auto &entry : modules) {
    if (selected.contains(entry.getKey()))
      continue;
    auto parent = entry.second;
    WalkResult result = parent.walk([&](firrtl::InstanceOp inst) {
      auto child = inst.getModuleName();
      if (!selected.contains(child) || child == targetModule)
        return WalkResult::advance();
      inst.emitError() << "DifuzzRTL regCoverage target module `"
                       << targetModule
                       << "` is not instance-exact: selected descendant `"
                       << child
                       << "` is also instantiated by unselected module `"
                       << parent.getName() << "`";
      return WalkResult::interrupt();
    });
    if (result.wasInterrupted())
      return failure();
  }
  return success();
}

static firrtl::UIntType getUInt(MLIRContext *ctx, unsigned width) {
  return firrtl::UIntType::get(ctx, width);
}

static firrtl::IntType getUIntAsInt(MLIRContext *ctx, unsigned width) {
  return cast<firrtl::IntType>(getUInt(ctx, width));
}

static Value constantUInt(OpBuilder &builder, Location loc, unsigned width,
                          uint64_t value) {
  auto type = getUIntAsInt(builder.getContext(), width);
  return builder.create<firrtl::ConstantOp>(loc, type,
                                            llvm::APInt(width, value))
      .getResult();
}

static firrtl::FIRRTLBaseType anonymousBaseType(Type type) {
  auto base = dyn_cast<firrtl::FIRRTLBaseType>(type);
  if (!base)
    return {};
  return base.getAllConstDroppedType().getAnonymousType();
}

static bool isClockType(Type type) {
  auto base = anonymousBaseType(type);
  return base && isa<firrtl::ClockType>(base);
}

static bool isUIntType(Type type) {
  auto base = anonymousBaseType(type);
  return base && isa<firrtl::UIntType>(base);
}

static std::optional<unsigned> bitWidth(Type type) {
  auto base = dyn_cast<firrtl::FIRRTLBaseType>(type);
  if (!base)
    return std::nullopt;

  base = base.getAllConstDroppedType().getAnonymousType();
  auto intType = dyn_cast<firrtl::IntType>(base);
  if (!intType)
    return std::nullopt;

  auto width = intType.getWidth();
  if (!width || *width < 0)
    return std::nullopt;
  return static_cast<unsigned>(*width);
}

static Value findClock(firrtl::FModuleOp module) {
  Value selected;
  auto ports = module.getPorts();
  for (auto [index, port] : llvm::enumerate(ports)) {
    Value arg = module.getArgument(index);
    auto name = port.getName();
    if ((isClockType(arg.getType()) || name == "clock" ||
         name == "gated_clock") &&
        isClockType(arg.getType()))
      selected = arg;
  }
  return selected;
}

static Value asUInt(OpBuilder &builder, Location loc, Value value,
                    unsigned width) {
  if (isUIntType(value.getType()))
    return value;
  return builder.create<firrtl::AsUIntPrimOp>(loc, getUInt(builder.getContext(), width),
                                              value)
      .getResult();
}

static Value truncateToUInt30(OpBuilder &builder, Location loc, Value value) {
  return builder.create<firrtl::BitsPrimOp>(loc, getUInt(builder.getContext(), 30),
                                            value, 29, 0)
      .getResult();
}

static firrtl::BundleType memoryReadPortType(MLIRContext *ctx,
                                             firrtl::UIntType addrType,
                                             firrtl::UIntType dataType) {
  auto oneBit = getUInt(ctx, 1);
  SmallVector<firrtl::BundleType::BundleElement> elems;
  elems.emplace_back(StringAttr::get(ctx, "addr"), false, addrType);
  elems.emplace_back(StringAttr::get(ctx, "en"), false, oneBit);
  elems.emplace_back(StringAttr::get(ctx, "clk"), false,
                     firrtl::ClockType::get(ctx));
  elems.emplace_back(StringAttr::get(ctx, "data"), true, dataType);
  return firrtl::BundleType::get(ctx, elems);
}

static firrtl::BundleType memoryWritePortType(MLIRContext *ctx,
                                              firrtl::UIntType addrType,
                                              firrtl::UIntType dataType) {
  auto oneBit = getUInt(ctx, 1);
  SmallVector<firrtl::BundleType::BundleElement> elems;
  elems.emplace_back(StringAttr::get(ctx, "addr"), false, addrType);
  elems.emplace_back(StringAttr::get(ctx, "en"), false, oneBit);
  elems.emplace_back(StringAttr::get(ctx, "clk"), false,
                     firrtl::ClockType::get(ctx));
  elems.emplace_back(StringAttr::get(ctx, "data"), false, dataType);
  elems.emplace_back(StringAttr::get(ctx, "mask"), false, oneBit);
  return firrtl::BundleType::get(ctx, elems);
}

static std::string zeroInitPath(StringRef directory, uint64_t depth) {
  llvm::SmallString<256> path(directory);
  llvm::sys::path::append(path,
                          ("zeros-" + std::to_string(depth) + ".hex"));
  return path.str().str();
}

static LogicalResult validateZeroInitFile(Operation *anchor, StringRef path,
                                          uint64_t depth) {
  std::ifstream input(path.str());
  if (!input)
    return anchor->emitError()
           << "missing DifuzzRTL zero-initialization file `" << path << "`";

  uint64_t entries = 0;
  std::string token;
  while (input >> token) {
    if (token != "0")
      return anchor->emitError()
             << "DifuzzRTL initialization file `" << path
             << "` contains a non-zero entry";
    if (++entries > depth)
      return anchor->emitError()
             << "DifuzzRTL initialization file `" << path
             << "` has more than " << depth << " entries";
  }
  if (entries != depth)
    return anchor->emitError()
           << "DifuzzRTL initialization file `" << path << "` has "
           << entries << " entries; expected " << depth;
  return success();
}

static LogicalResult ensureZeroInitFile(Operation *anchor, StringRef directory,
                                        uint64_t depth) {
  std::error_code error = llvm::sys::fs::create_directories(directory);
  if (error)
    return anchor->emitError()
           << "cannot create DifuzzRTL initialization directory `" << directory
           << "`: " << error.message();

  auto path = zeroInitPath(directory, depth);
  if (!llvm::sys::fs::exists(path)) {
    llvm::raw_fd_ostream output(path, error, llvm::sys::fs::OF_Text);
    if (error)
      return anchor->emitError()
             << "cannot create DifuzzRTL initialization file `" << path
             << "`: " << error.message();
    for (uint64_t i = 0; i < depth; ++i)
      output << "0\n";
    output.close();
    if (output.has_error())
      return anchor->emitError()
             << "cannot write DifuzzRTL initialization file `" << path << "`";
  }
  return validateZeroInitFile(anchor, path, depth);
}

struct MemoryPorts {
  Value readAddr;
  Value readEn;
  Value readClk;
  Value readData;
  Value writeAddr;
  Value writeEn;
  Value writeClk;
  Value writeData;
  Value writeMask;
};

static MemoryPorts getMemoryPorts(OpBuilder &builder, Location loc,
                                  firrtl::MemOp memory) {
  Value readPort = memory.getResult(0);
  Value writePort = memory.getResult(1);
  return {
      builder.create<firrtl::SubfieldOp>(loc, readPort, "addr").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, readPort, "en").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, readPort, "clk").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, readPort, "data").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, writePort, "addr").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, writePort, "en").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, writePort, "clk").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, writePort, "data").getResult(),
      builder.create<firrtl::SubfieldOp>(loc, writePort, "mask").getResult(),
  };
}

static firrtl::MemOp createZeroInitializedMemory(
    OpBuilder &builder, Location loc, firrtl::UIntType addrType,
    firrtl::UIntType dataType, uint64_t depth, StringRef name,
    StringRef initDirectory) {
  SmallVector<Type> portTypes{
      memoryReadPortType(builder.getContext(), addrType, dataType),
      memoryWritePortType(builder.getContext(), addrType, dataType)};
  SmallVector<Attribute> portNames{builder.getStringAttr("read"),
                                   builder.getStringAttr("write")};
  auto init = firrtl::MemoryInitAttr::get(
      builder.getContext(),
      builder.getStringAttr(zeroInitPath(initDirectory, depth)), false, true);
  auto empty = builder.getArrayAttr({});
  auto portAnnotations = builder.getArrayAttr({empty, empty});
  return builder.create<firrtl::MemOp>(
      loc, TypeRange(portTypes), 0, 1, depth, firrtl::RUWAttr::Undefined,
      builder.getArrayAttr(portNames), name,
      firrtl::NameKindEnum::InterestingName, empty, portAnnotations,
      circt::hw::InnerSymAttr(), init, StringAttr());
}

static void connectMemoryControl(OpBuilder &builder, Location loc,
                                 const MemoryPorts &ports, Value clock,
                                 Value readAddr, Value writeAddr) {
  auto one = constantUInt(builder, loc, 1, 1);
  builder.create<firrtl::StrictConnectOp>(loc, ports.readAddr, readAddr);
  builder.create<firrtl::StrictConnectOp>(loc, ports.readEn, one);
  builder.create<firrtl::StrictConnectOp>(loc, ports.readClk, clock);
  builder.create<firrtl::StrictConnectOp>(loc, ports.writeAddr, writeAddr);
  builder.create<firrtl::StrictConnectOp>(loc, ports.writeEn, one);
  builder.create<firrtl::StrictConnectOp>(loc, ports.writeClk, clock);
  builder.create<firrtl::StrictConnectOp>(loc, ports.writeMask, one);
}

class ModuleGraph {
public:
  explicit ModuleGraph(firrtl::FModuleOp module,
                       StatePlanMode statePlanMode = StatePlanMode::Compressed)
      : module(module), statePlanMode(statePlanMode) {}

  LogicalResult run(ModuleAudit &audit, CoveragePlan *plan = nullptr) {
    if (failed(checkLowFormBoundary()))
      return failure();
    collectDecls(audit);
    collectEdges();
    reverseEdges();
    collectMuxSources(audit);
    findDirectInputRegs(audit);
    findVectorGroups(audit);
    if (failed(computeInstrCovSummary(audit, plan)))
      return failure();
    return success();
  }

private:
  firrtl::FModuleOp module;
  StatePlanMode statePlanMode;
  llvm::StringMap<DeclNode> nodes;
  llvm::DenseMap<Value, std::string> rootNames;
  llvm::StringMap<llvm::StringSet<>> edges;
  llvm::StringMap<llvm::StringSet<>> reverse;
  llvm::StringSet<> portNames;
  llvm::StringSet<> controlRegs;
  llvm::StringSet<> directInputRegs;
  llvm::StringSet<> excludedDirectInputRegs;
  llvm::StringSet<> eligibleCtrlRegs;
  llvm::DenseMap<Value, SmallVector<std::string>> valueNamesCache;
  llvm::StringMap<llvm::StringSet<>> firstSourcesCache;
  llvm::StringMap<SmallVector<Value>> muxSelsBySource;
  std::vector<VectorGroup> vectorGroups;
  llvm::StringSet<> vectorRegNames;

  void addNode(NodeKind kind, StringRef name, Operation *op, Value value,
               std::optional<unsigned> width = std::nullopt) {
    if (name.empty())
      return;
    std::string key = name.str();
    nodes[key] = DeclNode{kind, key, op, value, width};
    edges.try_emplace(key);
    reverse.try_emplace(key);
    if (value)
      rootNames[value] = key;
  }

  static bool isRegisterKind(NodeKind kind) { return kind == NodeKind::Register; }

  static bool isSourceBoundary(NodeKind kind) {
    return kind == NodeKind::Port || kind == NodeKind::Register ||
           kind == NodeKind::Memory || kind == NodeKind::Instance;
  }

  LogicalResult checkLowFormBoundary() {
    unsigned whens = 0;
    unsigned matches = 0;
    unsigned groups = 0;

    module.walk([&](firrtl::WhenOp) { ++whens; });
    module.walk([&](firrtl::MatchOp) { ++matches; });
    module.walk([&](firrtl::GroupOp) { ++groups; });

    if (whens || matches || groups)
      return module.emitError()
             << "DifuzzRTL regCoverage must run at the Low FIRRTL boundary; "
                "found high-level control structure(s): when="
             << whens << " match=" << matches << " group=" << groups
             << ". Use --low-firrtl-pass-plugin, not the high-FIRRTL "
                "plugin point.";

    return success();
  }

  static std::string locKey(Location loc) {
    std::string key;
    llvm::raw_string_ostream os(key);
    loc.print(os);
    return os.str();
  }

  static std::string longestCommonPrefix(ArrayRef<std::string> names) {
    if (names.empty())
      return {};

    std::string prefix = names.front();
    for (StringRef name : names.drop_front()) {
      size_t len = 0;
      while (len < prefix.size() && len < name.size() &&
             prefix[len] == name[len])
        ++len;
      prefix.resize(len);
    }
    return prefix;
  }

  static bool splitVectorBody(StringRef body, unsigned &index,
                              std::string &suffix) {
    if (body.empty() || !std::isdigit(static_cast<unsigned char>(body[0])))
      return false;

    auto end = body.find('_');
    auto indexText = body.substr(0, end == StringRef::npos ? body.size() : end);
    if (indexText.empty())
      return false;
    for (char c : indexText)
      if (!std::isdigit(static_cast<unsigned char>(c)))
        return false;

    unsigned long parsed = 0;
    if (indexText.getAsInteger(10, parsed))
      return false;

    index = static_cast<unsigned>(parsed);
    auto suffixStart = std::to_string(index).size();
    suffix = body.substr(std::min<size_t>(suffixStart, body.size())).str();
    return true;
  }

  void collectDecls(ModuleAudit &audit) {
    auto ports = module.getPorts();
    auto &block = module.getBody().front();
    audit.ports = ports.size();
    for (auto [index, port] : llvm::enumerate(ports)) {
      auto name = port.name.getValue();
      portNames.insert(name);
      addNode(NodeKind::Port, name, nullptr, block.getArgument(index));
    }

    module.walk([&](Operation *op) {
      TypeSwitch<Operation *>(op)
          .Case<firrtl::WireOp>([&](auto wire) {
            addNode(NodeKind::Wire, wire.getName(), op, wire.getResult());
          })
          .Case<firrtl::RegOp>([&](auto reg) {
            ++audit.regs;
            addNode(NodeKind::Register, reg.getName(), op, reg.getResult(),
                    bitWidth(reg.getResult().getType()));
          })
          .Case<firrtl::RegResetOp>([&](auto reg) {
            ++audit.regResets;
            addNode(NodeKind::Register, reg.getName(), op, reg.getResult(),
                    bitWidth(reg.getResult().getType()));
          })
          .Case<firrtl::NodeOp>([&](auto node) {
            addNode(NodeKind::Node, node.getName(), op, node.getResult());
          })
          .Case<firrtl::MemOp>([&](auto mem) {
            addNode(NodeKind::Memory, mem.getName(), op, {});
            for (auto result : mem->getResults())
              rootNames[result] = mem.getName().str();
          })
          .Case<firrtl::InstanceOp>([&](auto inst) {
            ++audit.instances;
            addNode(NodeKind::Instance, inst.getName(), op, {});
            for (auto result : inst.getResults())
              rootNames[result] = inst.getName().str();
          })
          .Case<firrtl::MuxPrimOp>([&](auto) { ++audit.muxes; });
    });
  }

  std::optional<StringRef> rootName(Value value) const {
    auto direct = rootNames.find(value);
    if (direct != rootNames.end())
      return StringRef(direct->second);

    auto *op = value.getDefiningOp();
    if (!op)
      return std::nullopt;

    return TypeSwitch<Operation *, std::optional<StringRef>>(op)
        .Case<firrtl::SubfieldOp>(
            [&](auto sub) { return rootName(sub.getInput()); })
        .Case<firrtl::SubindexOp>(
            [&](auto sub) { return rootName(sub.getInput()); })
        .Case<firrtl::SubaccessOp>(
            [&](auto sub) { return rootName(sub.getInput()); })
        .Default([](Operation *) -> std::optional<StringRef> {
          return std::nullopt;
        });
  }

  const SmallVector<std::string> &findNames(Value value) {
    auto cached = valueNamesCache.find(value);
    if (cached != valueNamesCache.end())
      return cached->second;

    SmallVector<std::string> names;
    if (auto name = rootName(value)) {
      names.push_back(name->str());
    } else if (auto *op = value.getDefiningOp()) {
      auto append = [&](Value source) {
        for (auto &name : findNames(source))
          if (std::find(names.begin(), names.end(), name) == names.end())
            names.push_back(name);
      };
      if (auto mux = dyn_cast<firrtl::MuxPrimOp>(op)) {
        // Legacy graphLedger.Node.findNames intentionally walks only the mux
        // data arms here. Nested mux conditions are handled when that mux
        // itself is visited as a mux.
        append(mux.getHigh());
        append(mux.getLow());
      } else {
        for (auto operand : op->getOperands())
          append(operand);
      }
    }
    return valueNamesCache.try_emplace(value, std::move(names)).first->second;
  }

  void addEdge(StringRef source, StringRef sink) {
    if (source.empty() || sink.empty())
      return;
    if (!nodes.contains(source) || !nodes.contains(sink))
      return;
    edges[source].insert(sink);
  }

  void addSourceEdges(Value src, StringRef sink) {
    for (auto &source : findNames(src))
      addEdge(source, sink);
  }

  void collectEdges() {
    module.walk([&](Operation *op) {
      TypeSwitch<Operation *>(op)
          .Case<firrtl::RegResetOp>([&](auto reg) {
            addSourceEdges(reg.getResetSignal(), reg.getName());
          })
          .Case<firrtl::NodeOp>([&](auto node) {
            addSourceEdges(node.getInput(), node.getName());
          })
          .Case<firrtl::ConnectOp>([&](auto connect) {
            if (auto sink = rootName(connect.getDest()))
              addSourceEdges(connect.getSrc(), *sink);
          })
          .Case<firrtl::StrictConnectOp>([&](auto connect) {
            if (auto sink = rootName(connect.getDest()))
              addSourceEdges(connect.getSrc(), *sink);
          });
    });
  }

  void reverseEdges() {
    for (auto &node : nodes)
      reverse.try_emplace(node.getKey());
    for (auto &entry : edges)
      for (auto &sink : entry.getValue())
        reverse[sink.getKey()].insert(entry.getKey());
  }

  void findSrcs(StringRef sink, llvm::StringSet<> &visited,
                llvm::StringSet<> &sources) const {
    if (!nodes.contains(sink) || visited.contains(sink))
      return;
    visited.insert(sink);

    const auto &node = nodes.find(sink)->second;
    if (isSourceBoundary(node.kind))
      sources.insert(sink);

    if (node.kind != NodeKind::Wire && node.kind != NodeKind::Node)
      return;

    auto rev = reverse.find(sink);
    if (rev == reverse.end())
      return;
    for (auto &source : rev->second)
      findSrcs(source.getKey(), visited, sources);
  }

  void appendFirstSources(StringRef sink, llvm::StringSet<> &sources) {
    auto node = nodes.find(sink);
    if (node == nodes.end())
      return;
    if (isSourceBoundary(node->second.kind)) {
      sources.insert(sink);
      return;
    }

    auto cached = firstSourcesCache.find(sink);
    if (cached == firstSourcesCache.end()) {
      llvm::StringSet<> visited;
      llvm::StringSet<> resolved;
      findSrcs(sink, visited, resolved);
      cached = firstSourcesCache.try_emplace(sink, std::move(resolved)).first;
    }
    for (auto &source : cached->second)
      sources.insert(source.getKey());
  }

  void collectMuxSources(ModuleAudit &audit) {
    module.walk([&](firrtl::MuxPrimOp mux) {
      llvm::StringSet<> condNames;
      for (auto &name : findNames(mux.getSel()))
        condNames.insert(name);

      llvm::StringSet<> muxSources;
      for (auto &name : condNames)
        appendFirstSources(name.getKey(), muxSources);

      for (auto &source : muxSources) {
        auto it = nodes.find(source.getKey());
        if (it != nodes.end() && isRegisterKind(it->second.kind))
          controlRegs.insert(source.getKey());
        muxSelsBySource[source.getKey()].push_back(mux.getSel());
      }
    });

    audit.ctrlRegs = controlRegs.size();
  }

  void findDirectInputRegs(ModuleAudit &audit) {
    llvm::StringSet<> firstInputRegs;
    llvm::StringMap<llvm::StringSet<>> regSources;

    for (auto &reg : controlRegs) {
      auto rev = reverse.find(reg.getKey());
      if (rev == reverse.end())
        continue;

      llvm::StringSet<> srcs;
      for (auto &source : rev->second)
        appendFirstSources(source.getKey(), srcs);
      srcs.erase(reg.getKey());
      regSources[reg.getKey()] = std::move(srcs);
    }

    for (auto &entry : regSources) {
      bool allPorts = true;
      for (auto &source : entry.getValue())
        allPorts &= portNames.contains(source.getKey());
      if (allPorts)
        firstInputRegs.insert(entry.getKey());
    }

    for (auto &entry : regSources) {
      bool directInput = true;
      for (auto &source : entry.getValue()) {
        directInput &= portNames.contains(source.getKey()) ||
                       firstInputRegs.contains(source.getKey());
      }
      if (directInput)
        directInputRegs.insert(entry.getKey());
    }

    audit.directInputRegs = directInputRegs.size();

    for (auto &reg : directInputRegs) {
      auto it = nodes.find(reg.getKey());
      if (it != nodes.end() && it->second.width.value_or(0) > 3)
        excludedDirectInputRegs.insert(reg.getKey());
    }

    for (auto &reg : controlRegs) {
      if (!excludedDirectInputRegs.contains(reg.getKey()))
        eligibleCtrlRegs.insert(reg.getKey());
    }

    for (auto &reg : eligibleCtrlRegs) {
      auto it = nodes.find(reg.getKey());
      if (it == nodes.end())
        continue;
      if (it->second.width.value_or(0) >= 20)
        ++audit.largeCtrlRegs;
      else
        ++audit.smallCtrlRegs;
    }

    audit.excludedDirectInputRegs = excludedDirectInputRegs.size();
    audit.eligibleCtrlRegs = eligibleCtrlRegs.size();
  }

  LogicalResult requireControlRegWidths() {
    for (auto &reg : controlRegs) {
      auto it = nodes.find(reg.getKey());
      if (it == nodes.end() || !it->second.width)
        return module.emitError()
               << "DifuzzRTL regCoverage requires integer-width control "
                  "registers; unsupported control register `"
               << reg.getKey() << "`";
    }
    return success();
  }

  void findVectorGroups(ModuleAudit &audit) {
    llvm::StringMap<std::vector<std::string>> regsByLoc;

    for (auto &reg : controlRegs) {
      auto it = nodes.find(reg.getKey());
      if (it == nodes.end() || !it->second.op)
        continue;
      auto loc = it->second.op->getLoc();
      if (isa<UnknownLoc>(loc))
        continue;
      regsByLoc[locKey(loc)].push_back(reg.getKey().str());
    }

    constexpr unsigned minVectorSize = 2;
    for (auto &entry : regsByLoc) {
      auto &regs = entry.getValue();
      if (regs.size() < minVectorSize)
        continue;
      std::sort(regs.begin(), regs.end());

      auto prefix = longestCommonPrefix(regs);
      if (prefix.empty())
        continue;

      std::map<unsigned, std::set<std::string>> elements;
      bool valid = true;
      for (auto &reg : regs) {
        unsigned index = 0;
        std::string suffix;
        if (!splitVectorBody(StringRef(reg).drop_front(prefix.size()), index,
                             suffix)) {
          valid = false;
          break;
        }
        elements[index].insert(std::move(suffix));
      }

      if (!valid || elements.empty() || elements.begin()->first != 0)
        continue;

      unsigned prev = 0;
      bool first = true;
      const auto &firstBodies = elements.begin()->second;
      for (auto &element : elements) {
        if (!first && element.first != prev + 1) {
          valid = false;
          break;
        }
        if (element.second != firstBodies) {
          valid = false;
          break;
        }
        prev = element.first;
        first = false;
      }
      if (!valid)
        continue;

      VectorGroup group;
      group.size = elements.size();
      group.prefix = prefix;
      group.bodies = firstBodies;
      vectorGroups.push_back(group);

      for (unsigned index = 0; index < group.size; ++index)
        for (auto &body : group.bodies)
          vectorRegNames.insert(group.prefix + std::to_string(index) + body);
    }

    audit.vectorGroups = vectorGroups.size();
    audit.vectorRegs = vectorRegNames.size();
  }

  llvm::DenseSet<Value> muxSelsForReg(StringRef reg) const {
    llvm::DenseSet<Value> sels;
    auto found = muxSelsBySource.find(reg);
    if (found != muxSelsBySource.end())
      sels.insert(found->second.begin(), found->second.end());
    return sels;
  }

  static std::string valueKey(Value value) {
    std::string key;
    llvm::raw_string_ostream os(key);
    value.print(os);
    return os.str();
  }

  static unsigned stableOffset(StringRef key, unsigned width) {
    unsigned limit = 20 - width + 1;
    return static_cast<unsigned>(llvm::hash_value(key) % limit);
  }

  void assignOffsets(MutableArrayRef<CoverageItem> items,
                     unsigned totalStateBits, unsigned regStateSize) const {
    if (totalStateBits <= regStateSize) {
      unsigned offset = 0;
      for (auto &item : items) {
        item.offset = offset;
        offset += item.width;
      }
      return;
    }

    for (auto &item : items)
      item.offset = stableOffset(item.name, item.width);
  }

  LogicalResult computeLegacyLikeInstrCovSummary(ModuleAudit &audit,
                                                 CoveragePlan *plan) {
    if (failed(requireControlRegWidths()))
      return failure();

    unsigned fullCtrlBits = 0;
    for (auto &reg : controlRegs) {
      auto width = nodes.find(reg.getKey())->second.width.value();
      fullCtrlBits += width;
    }

    std::vector<std::string> selectedRegNames;
    unsigned totalStateBits = 0;
    for (auto &reg : controlRegs) {
      auto width = nodes.find(reg.getKey())->second.width.value();
      if (fullCtrlBits > 20 && width >= 20)
        continue;
      selectedRegNames.push_back(reg.getKey().str());
      totalStateBits += width;
    }
    std::sort(selectedRegNames.begin(), selectedRegNames.end());

    audit.optRegs = selectedRegNames.size();
    audit.uncoveredCtrlSigs = 0;
    audit.totalStateBits = totalStateBits;
    audit.regStateSize = totalStateBits == 0 ? 0 : std::min(totalStateBits, 20u);
    audit.covMapSize =
        audit.regStateSize == 0 ? 0 : (1u << audit.regStateSize);

    if (!plan)
      return success();

    plan->totalStateBits = audit.totalStateBits;
    plan->regStateSize = audit.regStateSize;
    plan->covMapSize = audit.covMapSize;
    plan->items.clear();

    SmallVector<CoverageItem> seedItems;
    for (auto &name : selectedRegNames) {
      const auto &node = nodes.find(name)->second;
      seedItems.push_back(
          CoverageItem{node.value, node.width.value(), 0, name, false});
    }

    assignOffsets(seedItems, audit.totalStateBits, audit.regStateSize);
    plan->items.append(seedItems.begin(), seedItems.end());

    return success();
  }

  LogicalResult computeCompressedInstrCovSummary(ModuleAudit &audit,
                                                 CoveragePlan *plan) {
    if (failed(requireControlRegWidths()))
      return failure();

    llvm::StringSet<> smallRegs;
    llvm::StringSet<> largeRegs;
    for (auto &reg : eligibleCtrlRegs) {
      auto width = nodes.find(reg.getKey())->second.width.value();
      if (width >= 20)
        largeRegs.insert(reg.getKey());
      else
        smallRegs.insert(reg.getKey());
    }

    llvm::DenseSet<Value> ctrlSigs;
    llvm::DenseSet<Value> coveredMuxSrcs;
    for (auto &reg : largeRegs) {
      auto sels = muxSelsForReg(reg.getKey());
      ctrlSigs.insert(sels.begin(), sels.end());
    }
    for (auto &reg : smallRegs) {
      auto sels = muxSelsForReg(reg.getKey());
      coveredMuxSrcs.insert(sels.begin(), sels.end());
    }

    llvm::StringSet<> firstVectorRegs;
    for (auto &group : vectorGroups) {
      for (auto &body : group.bodies) {
        auto name = group.prefix + "0" + body;
        if (smallRegs.contains(name))
          firstVectorRegs.insert(name);
      }
    }

    llvm::StringSet<> optRegs;
    for (auto &reg : smallRegs) {
      if (vectorRegNames.contains(reg.getKey()))
        continue;

      auto sels = muxSelsForReg(reg.getKey());
      auto width = nodes.find(reg.getKey())->second.width.value();
      if (sels.size() >= width) {
        optRegs.insert(reg.getKey());
      } else {
        ctrlSigs.insert(sels.begin(), sels.end());
      }
    }

    llvm::DenseSet<Value> uncoveredCtrlSigs;
    for (auto sel : ctrlSigs)
      if (!coveredMuxSrcs.contains(sel))
        uncoveredCtrlSigs.insert(sel);

    unsigned totalStateBits = uncoveredCtrlSigs.size();
    for (auto &reg : optRegs)
      totalStateBits += nodes.find(reg.getKey())->second.width.value();
    for (auto &reg : firstVectorRegs)
      totalStateBits += nodes.find(reg.getKey())->second.width.value();

    audit.optRegs = optRegs.size() + firstVectorRegs.size();
    audit.uncoveredCtrlSigs = uncoveredCtrlSigs.size();
    audit.totalStateBits = totalStateBits;
    audit.regStateSize = totalStateBits == 0 ? 0 : std::min(totalStateBits, 20u);
    audit.covMapSize =
        audit.regStateSize == 0 ? 0 : (1u << audit.regStateSize);

    if (!plan)
      return success();

    plan->totalStateBits = audit.totalStateBits;
    plan->regStateSize = audit.regStateSize;
    plan->covMapSize = audit.covMapSize;
    plan->items.clear();

    SmallVector<CoverageItem> seedItems;
    std::vector<std::string> optRegNames;
    for (auto &reg : optRegs)
      optRegNames.push_back(reg.getKey().str());
    std::sort(optRegNames.begin(), optRegNames.end());

    std::vector<std::string> firstVecNames;
    for (auto &reg : firstVectorRegs)
      firstVecNames.push_back(reg.getKey().str());
    std::sort(firstVecNames.begin(), firstVecNames.end());

    std::vector<std::pair<std::string, Value>> uncoveredSels;
    for (auto sel : uncoveredCtrlSigs)
      uncoveredSels.emplace_back(valueKey(sel), sel);
    std::sort(uncoveredSels.begin(), uncoveredSels.end(),
              [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });

    for (auto &name : optRegNames) {
      const auto &node = nodes.find(name)->second;
      seedItems.push_back(
          CoverageItem{node.value, node.width.value(), 0, name, false});
    }
    for (auto &name : firstVecNames) {
      const auto &node = nodes.find(name)->second;
      seedItems.push_back(
          CoverageItem{node.value, node.width.value(), 0, name, false});
    }
    for (auto &[name, sel] : uncoveredSels) {
      seedItems.push_back(
          CoverageItem{sel, 1, 0, std::move(name), true});
    }

    assignOffsets(seedItems, audit.totalStateBits, audit.regStateSize);

    llvm::StringMap<unsigned> firstVecOffsets;
    for (auto &item : seedItems) {
      if (firstVectorRegs.contains(item.name))
        firstVecOffsets[item.name] = item.offset;
      else
        plan->items.push_back(item);
    }

    for (auto &group : vectorGroups) {
      for (auto &body : group.bodies) {
        auto firstName = group.prefix + "0" + body;
        auto offsetIt = firstVecOffsets.find(firstName);
        if (offsetIt == firstVecOffsets.end())
          continue;

        for (unsigned index = 0; index < group.size; ++index) {
          auto name = group.prefix + std::to_string(index) + body;
          auto nodeIt = nodes.find(name);
          if (nodeIt == nodes.end() || !smallRegs.contains(name))
            continue;

          const auto &node = nodeIt->second;
          plan->items.push_back(CoverageItem{
              node.value, node.width.value(), offsetIt->second, name, false});
        }
      }
    }

    return success();
  }

  LogicalResult computeInstrCovSummary(ModuleAudit &audit, CoveragePlan *plan) {
    switch (statePlanMode) {
    case StatePlanMode::Compressed:
      return computeCompressedInstrCovSummary(audit, plan);
    case StatePlanMode::LegacyLike:
      return computeLegacyLikeInstrCovSummary(audit, plan);
    }
    return failure();
  }
};

static Value buildStateExpression(OpBuilder &builder, Location loc,
                                  const CoveragePlan &plan) {
  auto *ctx = builder.getContext();
  auto stateType = getUInt(ctx, plan.regStateSize);
  SmallVector<Value> paddedItems;

  for (const auto &item : plan.items) {
    Value itemValue = asUInt(builder, loc, item.value, item.width);
    Value shifted =
        builder
            .create<firrtl::ShlPrimOp>(loc,
                                        getUInt(ctx, item.width + item.offset),
                                        itemValue, item.offset)
            .getResult();
    Value padded = builder
                       .create<firrtl::PadPrimOp>(loc, stateType, shifted,
                                                  plan.regStateSize)
                       .getResult();
    paddedItems.push_back(padded);
  }

  if (paddedItems.empty())
    return constantUInt(builder, loc, plan.regStateSize, 0);

  while (paddedItems.size() > 1) {
    SmallVector<Value> next;
    for (unsigned i = 0; i < paddedItems.size(); i += 2) {
      if (i + 1 == paddedItems.size()) {
        next.push_back(paddedItems[i]);
        continue;
      }
      next.push_back(
          builder.create<firrtl::XorPrimOp>(loc, stateType, paddedItems[i],
                                            paddedItems[i + 1])
              .getResult());
    }
    paddedItems = std::move(next);
  }

  return paddedItems.front();
}

static Value insertLocalCoverage(firrtl::FModuleOp module,
                                 const CoveragePlan &plan,
                                 StringRef initDirectory) {
  OpBuilder builder = module.getBodyBuilder();
  auto loc = module.getLoc();
  auto *ctx = module.getContext();
  auto covSumType = getUInt(ctx, 30);
  auto moduleName = module.getName();
  auto zeroCovSum = constantUInt(builder, loc, 30, 0);

  Value clock = findClock(module);
  if (plan.regStateSize == 0 || !clock)
    return zeroCovSum;

  auto stateType = getUInt(ctx, plan.regStateSize);
  auto stateName = (moduleName + "_state").str();
  auto covMapName = (moduleName + "_cov").str();
  auto covSumName = (moduleName + "_covSum").str();
  auto zeroAddr = constantUInt(builder, loc, 1, 0);
  auto one = constantUInt(builder, loc, 1, 1);

  auto stateMemory = createZeroInitializedMemory(
      builder, loc, getUInt(ctx, 1), stateType, 1, stateName, initDirectory);
  auto statePorts = getMemoryPorts(builder, loc, stateMemory);
  connectMemoryControl(builder, loc, statePorts, clock, zeroAddr, zeroAddr);

  auto covMap = createZeroInitializedMemory(
      builder, loc, stateType, getUInt(ctx, 1), plan.covMapSize, covMapName,
      initDirectory);
  auto covPorts = getMemoryPorts(builder, loc, covMap);

  auto covSumMemory = createZeroInitializedMemory(
      builder, loc, getUInt(ctx, 1), covSumType, 1, covSumName,
      initDirectory);
  auto covSumPorts = getMemoryPorts(builder, loc, covSumMemory);
  connectMemoryControl(builder, loc, covSumPorts, clock, zeroAddr, zeroAddr);

  auto stateExpr = buildStateExpression(builder, loc, plan);
  builder.create<firrtl::StrictConnectOp>(loc, statePorts.writeData,
                                          stateExpr);
  connectMemoryControl(builder, loc, covPorts, clock, statePorts.readData,
                       statePorts.readData);
  builder.create<firrtl::StrictConnectOp>(loc, covPorts.writeData, one);

  auto plusOneWide =
      builder.create<firrtl::AddPrimOp>(loc, covSumPorts.readData,
                                        constantUInt(builder, loc, 1, 1));
  auto plusOne = truncateToUInt30(builder, loc, plusOneWide.getResult());
  auto nextCovSum = builder.create<firrtl::MuxPrimOp>(
      loc, covSumType, covPorts.readData, covSumPorts.readData, plusOne);
  builder.create<firrtl::StrictConnectOp>(loc, covSumPorts.writeData,
                                          nextCovSum.getResult());

  return covSumPorts.readData;
}

static Value orReduce(OpBuilder &builder, Location loc, ArrayRef<Value> values) {
  auto *ctx = builder.getContext();
  auto oneBit = getUInt(ctx, 1);
  if (values.empty())
    return constantUInt(builder, loc, 1, 0);

  SmallVector<Value> work(values.begin(), values.end());
  while (work.size() > 1) {
    SmallVector<Value> next;
    for (unsigned i = 0; i < work.size(); i += 2) {
      if (i + 1 == work.size()) {
        next.push_back(work[i]);
        continue;
      }
      next.push_back(builder.create<firrtl::OrPrimOp>(loc, oneBit, work[i],
                                                      work[i + 1])
                         .getResult());
    }
    work = std::move(next);
  }
  return work.front();
}

static Value insertMetaAssert(firrtl::FModuleOp module,
                              ArrayRef<Value> childMetaAsserts,
                              Value metaReset, bool includeLocal,
                              StringRef initDirectory) {
  OpBuilder builder = module.getBodyBuilder();
  auto loc = module.getLoc();
  auto *ctx = module.getContext();
  auto oneBit = getUInt(ctx, 1);
  Value clock = findClock(module);
  Value reset = {};
  auto ports = module.getPorts();
  for (auto [index, port] : llvm::enumerate(ports)) {
    if (port.getName().contains("reset")) {
      reset = module.getArgument(index);
      break;
    }
  }

  SmallVector<Value> terms;
  if (includeLocal)
    module.walk([&](firrtl::StopOp stop) { terms.push_back(stop.getCond()); });
  terms.append(childMetaAsserts.begin(), childMetaAsserts.end());
  if (terms.empty())
    return constantUInt(builder, loc, 1, 0);

  Value topOr = orReduce(builder, loc, terms);

  if (clock && reset) {
    auto zeroAddr = constantUInt(builder, loc, 1, 0);
    auto assertMemory = createZeroInitializedMemory(
        builder, loc, oneBit, oneBit, 1,
        (module.getName() + "_metaAssert").str(), initDirectory);
    auto assertPorts = getMemoryPorts(builder, loc, assertMemory);
    connectMemoryControl(builder, loc, assertPorts, clock, zeroAddr, zeroAddr);
    auto next = builder.create<firrtl::OrPrimOp>(loc, oneBit,
                                                assertPorts.readData, topOr);
    auto resetNext = builder.create<firrtl::MuxPrimOp>(
        loc, oneBit, metaReset, constantUInt(builder, loc, 1, 0),
        next.getResult());
    builder.create<firrtl::StrictConnectOp>(loc, assertPorts.writeData,
                                            resetNext.getResult());
    return assertPorts.readData;
  }

  return topOr;
}

class ModernRegCoverageCovSumPass
    : public PassWrapper<ModernRegCoverageCovSumPass,
                         OperationPass<firrtl::CircuitOp>> {
public:
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ModernRegCoverageCovSumPass)

  ModernRegCoverageCovSumPass()
      : statePlan(*this, "state-plan",
                  llvm::cl::desc(
                      "State packing plan: compressed or legacy-like"),
                  llvm::cl::init("compressed")),
        targetModule(*this, "target-module",
                     llvm::cl::desc(
                         "Exact module whose definition subtree is covered"),
                     llvm::cl::init("")),
        coverageInitDir(
            *this, "coverage-init-dir",
            llvm::cl::desc(
                "Absolute directory containing zeros-<depth>.hex files"),
            llvm::cl::init("")) {}

  ModernRegCoverageCovSumPass(const ModernRegCoverageCovSumPass &other)
      : PassWrapper(other),
        statePlan(*this, "state-plan",
                  llvm::cl::desc(
                      "State packing plan: compressed or legacy-like"),
                  llvm::cl::init("compressed")),
        targetModule(*this, "target-module",
                     llvm::cl::desc(
                         "Exact module whose definition subtree is covered"),
                     llvm::cl::init("")),
        coverageInitDir(
            *this, "coverage-init-dir",
            llvm::cl::desc(
                "Absolute directory containing zeros-<depth>.hex files"),
            llvm::cl::init("")) {
    statePlan = other.statePlan.getValue();
    targetModule = other.targetModule.getValue();
    coverageInitDir = other.coverageInitDir.getValue();
  }

  Option<std::string> statePlan;
  Option<std::string> targetModule;
  Option<std::string> coverageInitDir;

  StringRef getArgument() const final {
    return "difuzzrtl-modern-regcoverage-covsum";
  }

  StringRef getDescription() const final {
    return "Insert the DifuzzRTL io_covSum port and child aggregation shell";
  }

  void runOnOperation() final {
    auto circuit = getOperation();
    auto statePlanMode = parseStatePlanMode(statePlan);
    if (!statePlanMode) {
      circuit.emitError() << "unsupported DifuzzRTL regCoverage state-plan `"
                          << statePlan
                          << "`; expected compressed or legacy-like";
      signalPassFailure();
      return;
    }
    MLIRContext *ctx = circuit.getContext();
    OpBuilder builder(ctx);
    auto covSumType = getUInt(ctx, 30);
    auto oneBitType = getUInt(ctx, 1);
    auto covSumName = builder.getStringAttr("io_covSum");
    auto metaAssertName = builder.getStringAttr("metaAssert");
    auto metaResetName = builder.getStringAttr("metaReset");

    unsigned whens = 0;
    unsigned matches = 0;
    unsigned groups = 0;
    circuit.walk([&](firrtl::WhenOp) { ++whens; });
    circuit.walk([&](firrtl::MatchOp) { ++matches; });
    circuit.walk([&](firrtl::GroupOp) { ++groups; });
    if (whens || matches || groups) {
      circuit.emitError()
          << "DifuzzRTL regCoverage must run at the Low FIRRTL boundary; "
             "found high-level control structure(s): when="
          << whens << " match=" << matches << " group=" << groups
          << ". Use --low-firrtl-pass-plugin, not the high-FIRRTL "
             "plugin point.";
      signalPassFailure();
      return;
    }

    llvm::StringSet<> selectedModules;
    if (failed(selectTargetModules(circuit, targetModule, selectedModules))) {
      signalPassFailure();
      return;
    }

    SmallVector<firrtl::FModuleOp> modules;
    llvm::StringMap<unsigned> moduleIndex;
    circuit.walk([&](firrtl::FModuleOp module) {
      moduleIndex[module.getName()] = modules.size();
      modules.push_back(module);
    });

    SmallVector<ModuleCovSumInfo, 0> infos;
    infos.reserve(modules.size());
    for (auto module : modules) {
      for (auto port : module.getPorts()) {
        if (port.getName() == "io_covSum") {
          module.emitError()
              << "DifuzzRTL regCoverage coverage port already exists";
          signalPassFailure();
          return;
        }
        if (port.getName() == "metaAssert" || port.getName() == "metaReset") {
          module.emitError()
              << "DifuzzRTL regCoverage meta port already exists";
          signalPassFailure();
          return;
        }
      }

      ModuleAudit audit;
      CoveragePlan plan;
      bool selected = selectedModules.contains(module.getName());
      if (selected &&
          failed(ModuleGraph(module, *statePlanMode).run(audit, &plan))) {
        signalPassFailure();
        return;
      }

      ModuleCovSumInfo info;
      info.module = module;
      info.selected = selected;
      info.oldPortCount = module.getNumPorts();
      info.covSumPortIndex = info.oldPortCount;
      info.metaAssertPortIndex = info.oldPortCount + 1;
      info.metaResetPortIndex = info.oldPortCount + 2;
      info.plan = std::move(plan);
      infos.push_back(info);
    }

    std::set<uint64_t> initDepths;
    for (auto &info : infos) {
      if (info.plan.covMapSize) {
        initDepths.insert(1);
        initDepths.insert(info.plan.covMapSize);
      }
      if (info.selected) {
        bool hasStop = false;
        info.module.walk([&](firrtl::StopOp) { hasStop = true; });
        if (hasStop)
          initDepths.insert(1);
      }
    }
    if (coverageInitDir.empty() ||
        !llvm::sys::path::is_absolute(coverageInitDir)) {
      circuit.emitError()
          << "DifuzzRTL regCoverage requires an absolute "
             "coverage-init-dir";
      signalPassFailure();
      return;
    }
    for (auto depth : initDepths) {
      if (failed(ensureZeroInitFile(circuit, coverageInitDir, depth))) {
        signalPassFailure();
        return;
      }
    }

    for (auto &info : infos) {
      SmallVector<std::pair<unsigned, firrtl::PortInfo>> inserts;
      unsigned nextPort = info.oldPortCount;
      auto addPort = [&](firrtl::PortInfo port) {
        unsigned index = nextPort++;
        info.insertedPorts.push_back({info.oldPortCount, port});
        return index;
      };
      info.covSumPortIndex = addPort(
          firrtl::PortInfo(covSumName, covSumType, firrtl::Direction::Out));
      info.metaAssertPortIndex = addPort(firrtl::PortInfo(
          metaAssertName, oneBitType, firrtl::Direction::Out));
      info.metaResetPortIndex = addPort(firrtl::PortInfo(
          metaResetName, oneBitType, firrtl::Direction::In));

      inserts.append(info.insertedPorts.begin(), info.insertedPorts.end());
      info.module.insertPorts(inserts);
    }

    for (auto &info : infos) {
      SmallVector<firrtl::InstanceOp> instances;
      info.module.walk([&](firrtl::InstanceOp inst) {
        if (moduleIndex.contains(inst.getModuleName()))
          instances.push_back(inst);
      });

      for (auto inst : instances) {
        auto childIt = moduleIndex.find(inst.getModuleName());
        if (childIt == moduleIndex.end())
          continue;

        auto &childInfo = infos[childIt->second];
        auto newInst = inst.cloneAndInsertPorts(childInfo.insertedPorts);

        for (unsigned i = 0; i < childInfo.oldPortCount; ++i)
          inst->getResult(i).replaceAllUsesWith(newInst->getResult(i));
        info.childCovSums.push_back(
            newInst->getResult(childInfo.covSumPortIndex));
        info.childMetaAsserts.push_back(
            newInst->getResult(childInfo.metaAssertPortIndex));
        info.childMetaResets.push_back(
            newInst->getResult(childInfo.metaResetPortIndex));
        inst.erase();
      }
    }

    for (auto &info : infos) {
      OpBuilder bodyBuilder = info.module.getBodyBuilder();
      auto loc = info.module.getLoc();
      info.localCovSum =
          insertLocalCoverage(info.module, info.plan, coverageInitDir);
      auto metaAssertPort = info.module.getArgument(info.metaAssertPortIndex);
      auto metaResetPort = info.module.getArgument(info.metaResetPortIndex);
      // metaReset is instrumentation-only; it must never rewrite DUT state.
      info.localMetaAssert =
          insertMetaAssert(info.module, info.childMetaAsserts, metaResetPort,
                           info.selected, coverageInitDir);

      Value sum = info.localCovSum ? info.localCovSum
                                   : constantUInt(bodyBuilder, loc, 30, 0);

      for (auto childCovSum : info.childCovSums) {
        auto wideSum =
            bodyBuilder.create<firrtl::AddPrimOp>(loc, sum, childCovSum);
        sum = truncateToUInt30(bodyBuilder, loc, wideSum.getResult());
      }

      auto covSumPort = info.module.getArgument(info.covSumPortIndex);
      bodyBuilder.create<firrtl::StrictConnectOp>(loc, covSumPort, sum);
      bodyBuilder.create<firrtl::StrictConnectOp>(loc, metaAssertPort,
                                                  info.localMetaAssert);

      for (auto childMetaReset : info.childMetaResets)
        bodyBuilder.create<firrtl::StrictConnectOp>(loc, childMetaReset,
                                                    metaResetPort);
    }

    if (failed(verify(circuit)))
      signalPassFailure();
  }
};

class ModernRegCoverageAuditPass
    : public PassWrapper<ModernRegCoverageAuditPass,
                         OperationPass<firrtl::CircuitOp>> {
public:
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ModernRegCoverageAuditPass)

  ModernRegCoverageAuditPass()
      : statePlan(*this, "state-plan",
                  llvm::cl::desc(
                      "State packing plan: compressed or legacy-like"),
                  llvm::cl::init("compressed")),
        targetModule(*this, "target-module",
                     llvm::cl::desc(
                         "Exact module whose definition subtree is audited"),
                     llvm::cl::init("")) {}

  ModernRegCoverageAuditPass(const ModernRegCoverageAuditPass &other)
      : PassWrapper(other),
        statePlan(*this, "state-plan",
                  llvm::cl::desc(
                      "State packing plan: compressed or legacy-like"),
                  llvm::cl::init("compressed")),
        targetModule(*this, "target-module",
                     llvm::cl::desc(
                         "Exact module whose definition subtree is audited"),
                     llvm::cl::init("")) {
    statePlan = other.statePlan.getValue();
    targetModule = other.targetModule.getValue();
  }

  Option<std::string> statePlan;
  Option<std::string> targetModule;

  StringRef getArgument() const final {
    return "difuzzrtl-modern-regcoverage-audit";
  }

  StringRef getDescription() const final {
    return "Audit modern FIRRTL operations needed by DifuzzRTL regCoverage";
  }

  void runOnOperation() final {
    auto circuit = getOperation();
    auto statePlanMode = parseStatePlanMode(statePlan);
    if (!statePlanMode) {
      circuit.emitError() << "unsupported DifuzzRTL regCoverage state-plan `"
                          << statePlan
                          << "`; expected compressed or legacy-like";
      signalPassFailure();
      return;
    }
    CircuitAudit circuitAudit;
    bool hadFailure = false;
    llvm::StringSet<> selectedModules;
    if (failed(selectTargetModules(circuit, targetModule, selectedModules))) {
      signalPassFailure();
      return;
    }

    circuit.walk([&](firrtl::FModuleOp module) {
      if (!selectedModules.contains(module.getName()))
        return;
      ModuleAudit audit;
      if (failed(ModuleGraph(module, *statePlanMode).run(audit))) {
        hadFailure = true;
        return;
      }

      circuitAudit.add(audit);

      if (audit.ctrlRegs)
        module.emitRemark()
            << "difuzzrtl modern regcoverage audit: state_plan="
            << statePlanModeName(*statePlanMode) << " ports=" << audit.ports
            << " regs=" << audit.regs << " regresets=" << audit.regResets
            << " muxes=" << audit.muxes << " instances=" << audit.instances
            << " ctrl_regs=" << audit.ctrlRegs
            << " direct_input_regs=" << audit.directInputRegs
            << " excluded_direct_input_regs=" << audit.excludedDirectInputRegs
            << " eligible_ctrl_regs=" << audit.eligibleCtrlRegs
            << " large_ctrl_regs=" << audit.largeCtrlRegs
            << " small_ctrl_regs=" << audit.smallCtrlRegs
            << " vector_groups=" << audit.vectorGroups
            << " vector_regs=" << audit.vectorRegs
            << " opt_regs=" << audit.optRegs
            << " uncovered_ctrl_sigs=" << audit.uncoveredCtrlSigs
            << " total_state_bits=" << audit.totalStateBits
            << " reg_state_size=" << audit.regStateSize
            << " cov_map_size=" << audit.covMapSize;
    });

    if (hadFailure) {
      signalPassFailure();
      return;
    }

    if (failed(verify(circuit)))
      signalPassFailure();

    circuit.emitRemark()
        << "difuzzrtl modern regcoverage circuit audit: state_plan="
        << statePlanModeName(*statePlanMode) << " target_module="
        << (targetModule.empty() ? StringRef("all")
                                 : StringRef(targetModule.getValue()))
        << " modules="
        << circuitAudit.modules
        << " modules_with_ctrl=" << circuitAudit.modulesWithCtrl
        << " modules_with_state=" << circuitAudit.modulesWithState
        << " regs=" << circuitAudit.regs
        << " regresets=" << circuitAudit.regResets
        << " muxes=" << circuitAudit.muxes
        << " instances=" << circuitAudit.instances
        << " ctrl_regs=" << circuitAudit.ctrlRegs
        << " eligible_ctrl_regs=" << circuitAudit.eligibleCtrlRegs
        << " total_state_bits=" << circuitAudit.totalStateBits
        << " capped_state_bits=" << circuitAudit.cappedStateBits;
  }
};

void registerModernRegCoveragePasses() {
  PassRegistration<ModernRegCoverageAuditPass>();
  PassRegistration<ModernRegCoverageCovSumPass>();
}

} // namespace

extern "C" PassPluginLibraryInfo LLVM_ATTRIBUTE_WEAK mlirGetPassPluginInfo() {
  return {MLIR_PLUGIN_API_VERSION, "DifuzzRTLModernRegCoverage", "0.1",
          []() { registerModernRegCoveragePasses(); }};
}
