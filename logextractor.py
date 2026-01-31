from contextlib import chdir
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

from ctypes import ArgumentError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from scanf import scanf_compile

import clang.cindex as ci
from clang.cindex import CompilationDatabase, CompilationDatabaseError

LOG_FUNCS = {
    'LogDebug', 'LogTrace',
    'LogPrintf', 'LogPrint',
    'LogInfo', 'LogError', 'LogWarning',
    'LogPrintFormatInternal'
}

@dataclass(frozen=True, slots=True)
class LogMessage:
    fmt: str                     # the format string (without surrounding quotes)
    regex: str
    regex_types: list[str]
    file: Optional[str]         # source file name (None for built‑ins)
    line: int                    # line number of the macro call
    column: int                  # column number of the macro call
    macro: str                   # which Log* macro was used
    category: Optional[str] = None   # optional category argument


def fmt_to_regex(fmt_str):
    """
    Converts a printf/scanf string to a Python Regex.
    Returns the regex string and a list of inferred types, just a wrapper
    for the python scanf package's scanf_compile.
    """

    return scanf_compile(fmt_str, collapseWhitespace=False)

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


# Global that will be initialized once in each process
_PROCESS_LOCAL_INDEX = None


def worker_init():
    """
    Called once per process to init the global per-process index.
    """
    global _PROCESS_LOCAL_INDEX
    _PROCESS_LOCAL_INDEX = ci.Index.create()


def worker_entrypoint(filename: str, args: list[str], root_dir: str):
    """
    Wrapper that initializes / retrieves the global _PROCESS_LOCAL_INDEX and
    invokes parse_file.
    """
    global _PROCESS_LOCAL_INDEX
    if _PROCESS_LOCAL_INDEX is None:
        _PROCESS_LOCAL_INDEX = ci.Index.create()

    return parse_file(filename, args, root_dir, _PROCESS_LOCAL_INDEX)


def process_log(node: ci.Cursor, root_dir: str) -> LogMessage:
    macro = node.spelling
    args = get_macro_args(node)

    category: Optional[str] = None

    idx = 0
    if macro in ("LogDebug", "LogTrace"):
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
    regex, regex_types = fmt_to_regex(fmt_str)
    regex = regex.pattern
    regex_types = [getattr(t, '__name__', str(t)) for t in regex_types]

    loc = node.location
    file_name = Path(loc.file.name).relative_to(root_dir)

    return LogMessage(
        fmt=fmt_str,
        regex=regex,
        regex_types=regex_types,
        file=str(file_name),
        line=loc.line,
        column=loc.column,
        macro=macro,
        category=category,
    )


def parse_file(
    filename: str, args: list[str], root_dir: str, index: Optional[ci.Index] = None
    ) -> list[LogMessage]:
    """
    Parse file into LogMessages, optionally reuses an existing index if the
    caller has one.
    """
    def visit_node(node: ci.Cursor, root_dir: str, results: list[LogMessage]):
        if node.location.file:
            fname = node.location.file.name
            # skip files outside of our dir
            if not fname.startswith(root_dir):
                return

        if node.kind == ci.CursorKind.MACRO_INSTANTIATION:
            if node.spelling in LOG_FUNCS:
                results.append(process_log(node, root_dir))

        # Recurse
        for child in node.get_children():
            visit_node(child, root_dir, results)

    results: list[LogMessage] = []

    if not index:
        index = ci.Index.create()

    try:
        tu = index.parse(
            str(filename),
            args=args,
            options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except ci.TranslationUnitLoadError as e:
        print(f"Error parsing {filename}: {e}")
        print(f"Using args: {" ".join(args)}")
        return []

    assert isinstance(tu.cursor, ci.Cursor)

    visit_node(tu.cursor, root_dir, results)
    return results


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

        self.log_messages: list[LogMessage] = []

    @staticmethod
    def clean_args(args):
        arglist = list(args)[1:]
        clean = []
        skip_next = False
        # Flags that slow down parsing but aren't needed for AST generation
        bad_prefixes = ('-O', '-W', '-g', '-f')

        for a in arglist:
            # stop parsing at -- {filename.cpp}
            if a == '--':
                break
            elif skip_next:
                skip_next = False
                continue
            # These break everything for some reason
            elif a == '-c':
                skip_next = True
                continue
            elif a.startswith(bad_prefixes):
                continue
            clean.append(a)

        clean.append('-fsyntax-only')
        return clean

    def parse_file(self, filename):
        print(f"Parsing {filename}...")
        filename = Path(filename)
        cmds = self.cdb.getCompileCommands(filename)
        if cmds is None:
            raise ArgumentError(f"{filename} not found in compilation database!")

        # We only want the first one if multiple exist.
        args = LogCompiler.clean_args(cmds[0].arguments)

        index = ci.Index.create()
        self.log_messages.extend(parse_file(str(filename), args, str(self.root_dir), index))

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
                args = self.clean_args(cmd.arguments)
                tasks.append((src_path, args, str(self.root_dir)))

        print(f"Parsing {len(tasks)} files using {os.cpu_count()} threads...")

        workers = os.cpu_count()
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=worker_init
        ) as executor:
            # Submit all tasks
            futures = {
                executor.submit(worker_entrypoint, f, a, r): f
                for f, a, r in tasks
            }

            # Watch for completion
            for i, future in enumerate(as_completed(futures)):
                try:
                    self.log_messages.extend(future.result())
                    print(f"Progress: {i}/{len(tasks)}", end='\r')
                except Exception as e:
                    print(f"\nTask failed: {e}")

    def dump_db(self, out_file):
        serialisable = [asdict(m) for m in self.log_messages]

        with open(out_file, 'w') as f:
            json.dump(serialisable, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) not in [3, 4]:
        print(
            "Usage!!! logextractor.py {src_dir} {out_path}\n"
            "Or!!! logextractor.py {src_dir} {file} {out_path}\n"
            )
        sys.exit(-1)

    with chdir(sys.argv[1]):
        compiler = LogCompiler(sys.argv[1])
        if len(sys.argv) == 3:
            compiler.parse_all()
        elif len(sys.argv) == 4:
            compiler.parse_file(sys.argv[2])
        compiler.dump_db(sys.argv[-1])
