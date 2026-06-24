import os
import re


MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
VERILOG_KEYWORDS = {
    "input", "output", "inout", "wire", "reg", "logic", "signed",
}

class tileSrcReader():
    def __init__(self, input_name):
        if not os.path.isfile(input_name):
            raise Exception('No file exists: {}'.format(input_name))

        name_file = open(input_name, 'r')

        self.name_map = {}

        while True:
            line = name_file.readline()
            if not line: break
            if line[0:2] != '  ':
                key = line[:-1]
                self.name_map[key] = []
                while True:
                    val_line = name_file.readline()
                    if not val_line: break
                    elif '  ' != val_line[0:2]: break

                    self.name_map[key].append(val_line[2:-1])

                if not val_line: break
                elif val_line != '\n':
                    raise Exception('Name file {} must contain new line between entries'.format(input_name))

        name_file.close()


    def return_map(self):
        return self.name_map


def _candidate_vfiles():
    out = []
    for key in ("DIFUZZRTL_VFILE", "VERILOG_SOURCE", "VERILOG_SOURCES"):
        value = os.getenv(key, "")
        if not value:
            continue
        for item in value.split():
            path = os.path.abspath(item)
            if path not in out and os.path.isfile(path):
                out.append(path)
    return out


def _strip_line_comment(text):
    return re.sub(r"//.*", "", text)


def _read_module_port_text(vfile, top_name):
    with open(vfile, "r") as fd:
        in_module = False
        chunks = []
        for line in fd:
            if not in_module:
                match = MODULE_RE.match(line)
                if match and match.group(1) == top_name:
                    in_module = True
                    chunks.append(line[line.find("(") + 1:])
                continue
            chunks.append(line)
            if line.strip() == ");":
                break
    return "".join(chunks) if chunks else ""


def infer_top_port_names(top_name):
    """Infer ANSI-style Verilog top ports for the current cocotb build.

    Modern Rocket/BOOM configs may rename the outer TileLink bundle prefix
    while preserving the channel field shape.  Reading the generated Verilog
    keeps the runtime adapter independent from a particular processor checkout.
    """
    for path in _candidate_vfiles():
        text = _read_module_port_text(path, top_name)
        if not text:
            continue
        text = _strip_line_comment(text)
        names = []
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.endswith(");"):
                chunk = chunk[:-2].strip()
            tokens = IDENT_RE.findall(chunk)
            tokens = [tok for tok in tokens if tok not in VERILOG_KEYWORDS]
            if tokens:
                names.append(tokens[-1])
        if names:
            return names
    return []
