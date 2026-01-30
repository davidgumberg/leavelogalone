from ctypes import ArgumentError
from pathlib import Path
from pprint import pprint
import os
import sys
import re
import json
from typing import Optional, Tuple
import clang.cindex as ci
from clang.cindex import CompilationDatabase, CompilationDatabaseError, Index, TranslationUnit
from scanf import scanf, scanf_compile


def arg_str(arg: list[ci.Token]) -> str:
    return "".join(tok.spelling for tok in arg)

# Unfortunately have to parse macro args ourselves since clang won't do it :(
# there is probably a better way to do this using the args to
# LogPrintFormatInternal but afaict can't access the argumentizer for that from
# this translation unit.

def get_macro_args(cursor: ci.Cursor) -> list[list[ci.Token]]:
    macro_name = cursor.spelling
    toks = list(cursor.get_tokens())
    assert toks

    args = []
    current_arg = []
    paren_depth = 0
    for idx, tok in enumerate(toks):
        t = tok.spelling
        if idx == 0:
            assert t == macro_name
            continue
        elif idx == 1:
            assert t == "("
        elif t == "(":
            paren_depth += 1
        elif t == ")":
            # end of macro reached
            if paren_depth == 0:
                args.append(current_arg)
                return args
            else:
                paren_depth -= 1
                current_arg.append(tok)
        elif t == "," and paren_depth == 0:
            args.append(current_arg)
            current_arg = []
        else:
            current_arg.append(tok)

    return []


class LogCompiler:
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir)
        self.build_dir = self.root_dir / "build"

        compile_commands_path = self.build_dir / "compile_commands.json"
        if not compile_commands_path.is_file():
            raise ArgumentError(
                f"Expected to find {compile_commands_path.absolute()} but "
                f"didn't. Check that {self.build_dir.absolute()} exists, you "
                "are building with clang, and that "
                "CMAKE_EXPORT_COMPILE_COMMANDS=ON")

        try:
            self.cdb = CompilationDatabase.fromDirectory(os.path.join(root_dir, "build"))
        except CompilationDatabaseError:
            raise ArgumentError(
                f"Error: something went wrong loading {compile_commands_path}")

        # reusing an index is just for performance reasons, cached headers
        self.index = Index.create()

        self.log_funcs = {
            'LogDebug',
            'LogTrace',
            'LogPrintf',
            'LogInfo',
            'LogPrint',
            'LogWarning',
            'LogPrintFormatInternal'
        }
        self.log_messages = []

    def fmt_to_regex(self, fmt_str):
        """
        Converts a printf/scanf string to a Python Regex.
        Returns the regex string and a list of inferred types, just a wrapper
        for the python scanf package's scanf_compile.
        """

        return scanf_compile(fmt_str, collapseWhitespace=False)

    def visit_node(self, node):
        if node.kind.is_expression() or node.kind == ci.CursorKind.MACRO_INSTANTIATION:
            if node.spelling in self.log_funcs:
                self.process_log_call(node)

        # Recurse
        for child in node.get_children():
            self.visit_node(child)

    def process_log_call(self, node):
        macro = node.spelling
        args = get_macro_args(node)
        # Todo: include metadata:
        # category, source location

        # What to do about duplicate fmt_str's?
        # These have to be treated as one log message, maybe with multiple
        # locations, or just pick one out of a hat?
        category: Optional[str] = None

        idx = 0
        if macro == "LogDebug" or macro == "LogTrace":
            # First arg is category
            category = arg_str(args[idx])
            idx += 1

        fmt_str = arg_str(args[idx])
        if fmt_str.startswith('"') and fmt_str.endswith('"'):
            fmt_str = fmt_str[1:-1]
        else:
            # The format string is not a literal, probably not worth handling this
            print(f"Format string is not a literal, skipped: {fmt_str}")

        # on second thought, store the fmt strings in the text file,
        # the log parser can compile to regex's at load time?
        #regex = self.fmt_to_regex(fmt_str)
        self.log_messages.append(fmt_str)


    def parse_file(self, filename):
        def compile_args(args):
            # generator to list
            arglist = list(args)
            # first arg is compiler command
            arglist = arglist[1:]

            clean = []
            for a in arglist:
                # stop parsing at -- {filename.cpp}
                if a == '--':
                    break
                clean.append(a)
            return clean
        print(f"Parsing {filename}...")
        cmds = self.cdb.getCompileCommands(filename)
        if cmds is None:
            raise ArgumentError(f"{filename} not found in compilation database!")

        # We only want the first one if multiple exist.
        args = compile_args(cmds[0].arguments)
        TranslationUnit.PARSE_INCOMPLETE

        try:
            tu = self.index.parse(
                filename,
                args=args,
                options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        except ci.TranslationUnitLoadError as e:
            print(f"Something went wrong parsing {filename} with {args}")
            print(e)

        failed = False
        for diag in tu.diagnostics:
            # You can filter by severity.
            # Severity 3 is Error, 4 is Fatal.
            if diag.severity >= ci.Diagnostic.Error:
                print(f"DIAGNOSTIC: {diag.spelling}")
                print(f"  Location: {diag.location.file}:{diag.location.line}:{diag.location.column}")
                print(f"  Severity: {diag.severity}")
                failed = True

        if failed:
            print(f"Skipping {filename} due to parsing errors.")
            return

        self.visit_node(tu.cursor)

    def parse_all(self):
        exclude_dirs = [
            self.root_dir / "src" / "bench",
            self.root_dir / "src" / "test",
            self.root_dir / "src" / "ipc" / "test",
            self.build_dir / "src" / "qt",
            self.root_dir / "src" / "qt",
            self.build_dir / "src" / "qt" / "test",
            self.root_dir / "src" / "qt" / "test",
            self.root_dir / "src" / "wallet" / "test",
        ]

        exclude_dirs = [p.resolve() for p in exclude_dirs]

        all_cmds = self.cdb.getAllCompileCommands()
        if all_cmds is None:
            raise AssertionError

        seen: set[Path] = set()

        for cmd in all_cmds:
            src_path = Path(cmd.filename).resolve()
            if src_path in seen:
                continue
            seen.add(src_path)
            # skip test files, they cause trouble!!!
            if any(src_path.is_relative_to(d) for d in exclude_dirs):
                continue
            if src_path.suffix == ".cpp":
                self.parse_file(src_path)


    def dump_db(self, out_file):
        with open(out_file, 'w') as f:
            json.dump(self.log_messages, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage!!! logextractor.py {src_dir} {out_path}")
        sys.exit(-1)

    compiler = LogCompiler(sys.argv[1])
    compiler.parse_all()
    compiler.dump_db("log_defs.json")
