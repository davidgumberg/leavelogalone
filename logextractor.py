import json
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from ctypes import ArgumentError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from scanf import scanf_compile

import clang.cindex as ci
from clang.cindex import CompilationDatabase, CompilationDatabaseError, Index, TranslationUnit

@dataclass(frozen=True, slots=True)
class LogMessage:
    fmt: str                     # the format string (without surrounding quotes)
    file: Optional[str]         # source file name (None for built‑ins)
    line: int                    # line number of the macro call
    column: int                  # column number of the macro call
    macro: str                   # which Log* macro was used
    category: Optional[str] = None   # optional category argument


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
        self.root_dir_str = str(self.root_dir)
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

        self.lock = threading.Lock()
        self.log_messages: list[LogMessage] = []

        self.num_threads = os.cpu_count() or 4

        self.index_pool = queue.Queue()

        for _ in range(self.num_threads):
            self.index_pool.put(Index.create())



    def fmt_to_regex(self, fmt_str):
        """
        Converts a printf/scanf string to a Python Regex.
        Returns the regex string and a list of inferred types, just a wrapper
        for the python scanf package's scanf_compile.
        """

        return scanf_compile(fmt_str, collapseWhitespace=False)

    def visit_node(self, node):
        if node.location.file:
            fname = node.location.file.name
            # skip files outside of our dir
            if not fname.startswith(self.root_dir_str):
                return

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

        loc = node.location
        file_name = loc.file.name if loc.file else None

        msg = LogMessage(
            fmt=fmt_str,
            file=file_name,
            line=loc.line,
            column=loc.column,
            macro=macro,
            category=category,
        )
        with self.lock:
            self.log_messages.append(msg)

    def parse_worker(self, filename, args):
        """Worker method for the thread pool."""
        index = self.index_pool.get()

        try:
            tu = index.parse(
                str(filename),
                args=args,
                options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            )
            self.visit_node(tu.cursor)
            return filename
        except Exception as e:
            print(f"\nError parsing {filename}: {e}")
            return None
        finally:
            self.index_pool.put(index)

    @staticmethod
    def compile_args(basename, args):
        arglist = list(args)[1:]
        clean = []

        for a in arglist:
            # stop parsing at -- {filename.cpp}
            if a == '--':
                break
            # These break everything for some reason
            elif not a.startswith('-') and a.endswith(basename):
                continue
            clean.append(a)
        return clean

    def parse_file(self, filename):
        print(f"Parsing {filename}...")
        cmds = self.cdb.getCompileCommands(filename)
        if cmds is None:
            raise ArgumentError(f"{filename} not found in compilation database!")

        # We only want the first one if multiple exist.
        args = LogCompiler.compile_args(os.path.basename(filename.absolute()), cmds[0].arguments)

        try:
            tu = self.index.parse(
                filename,
                args=args,
                options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        except ci.TranslationUnitLoadError as e:
            print(e)
            print(f"Something went wrong parsing {filename} with args: {args}")
            raise

        self.visit_node(tu.cursor)

    def parse_all(self):
        exclude_dirs = [
            # No debug.log messages live in these places!
            self.root_dir / "src" / "bench",
            self.root_dir / "src" / "test",
            self.root_dir / "src" / "ipc" / "test",
            self.root_dir / "src" / "qt" / "test",
            self.root_dir / "src" / "wallet" / "test",
            # autogenerated stuff
            self.build_dir,
        ]

        exclude_dirs = [p.resolve() for p in exclude_dirs]

        all_cmds = self.cdb.getAllCompileCommands()
        if all_cmds is None:
            raise AssertionError

        seen: set[Path] = set()
        tasks = []

        for cmd in all_cmds:
            src_path = Path(cmd.filename).resolve()
            if src_path in seen:
                continue
            seen.add(src_path)
            if any(src_path.is_relative_to(d) for d in exclude_dirs):
                continue
            if src_path.suffix == ".cpp":
                args = self.compile_args(os.path.basename(src_path.absolute()), cmd.arguments)
                tasks.append((src_path, args))

        print(f"Parsing {len(tasks)} files using {os.cpu_count()} threads...")

        with ThreadPoolExecutor() as executor:
            # Submit all tasks
            futures = {
                executor.submit(self.parse_worker, f, a): f
                for f, a in tasks
            }

            # Watch for completion
            for i, future in enumerate(as_completed(futures)):
                try:
                    future.result() # Raises exceptions if the thread crashed
                    if i % 10 == 0:
                        print(f"Progress: {i}/{len(tasks)}", end='\r')
                except Exception as e:
                    print(f"\nTask failed: {e}")

    def dump_db(self, out_file):
        # Convert each LogMessage to a plain dict before dumping
        serialisable = [asdict(m) for m in self.log_messages]

        with open(out_file, 'w') as f:
            json.dump(serialisable, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage!!! logextractor.py {src_dir} {out_path}")
        sys.exit(-1)

    compiler = LogCompiler(sys.argv[1])
    compiler.parse_all()
    compiler.dump_db("log_defs.json")
