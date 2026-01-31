The ugly bit here is that your pip install clang==x.x.x version should match as
closely as possible your system's `clang-devel` or `clang-dev` or `libclang-dev`
or w/e. Might not have the exact release, but there should be a matching
MAJOR.MINOR pip package for your clang library package

### LLM disclosure

I used LLM's heavily (in chat format) while working on this, but I've touched
most and reviewed every line of code in this repo.
