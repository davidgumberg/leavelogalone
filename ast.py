import sys
import clang.cindex as ci


def print_ast(node: ci.Cursor, indent=0):
    prefix = "  " * indent
    print(
        f"{prefix}{node.kind} : '{node.spelling}' "
        f"(Ref: '{node.referenced.spelling if node.referenced else 'None'}')")

    # Recurse
    for child in node.get_children():
        print_ast(child, indent + 2)


index = ci.Index.create()

if len(sys.argv) != 2:
    print("USAGE!")
    exit(-1)

tu = index.parse(sys.argv[1], args=['-fsyntax-only', '-std=c++17'])
print_ast(tu.cursor)
