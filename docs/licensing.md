# Licensing

Project-owned code is licensed under GPL-3.0-only. Source files carry SPDX notices;
LICENSE contains the complete GPLv3 text. Third-party components retain their own
licenses and notices. Previously distributed copies keep the permissions granted
with those copies.

## Dependency review

The direct dependencies pinned in pyproject.toml (deepagents, langchain-openai,
langchain-ollama, and langgraph-checkpoint) declare MIT licensing in their
installed distribution metadata. The installed transitive dependencies were also
reviewed through their metadata and selected license files. This is not a complete
review of every bundled native component or redistribution obligation.

The test-only dependencies pytest-xdist and execnet declare MIT licensing in their
distribution metadata and license files. These terms are compatible with GPLv3.
They belong to the test dependency group and are not runtime requirements of the
git-ez wheel.

### Open item: regex

The locked regex distribution declares `Apache-2.0 AND CNRI-Python`. Its LICENSE.txt
references CNRI's Python 1.6 license for code derived from CPython's re module.
That declaration must not be silently treated as the distinct
`CNRI-Python-GPL-Compatible` license. Historical Python 1.6 licensing has a documented
GPL compatibility concern.

Clarify the applicable terms with upstream or identify a compatible dependency path
before claiming that the complete runtime is GPL-compatible or redistributing a
bundle containing it. The git-ez wheel does not vendor these dependencies, but
installers resolve them separately; this is not a conclusion that all combined uses
are compatible. No dependency versions were changed as part of the license switch.

References:

- [regex metadata and license](https://github.com/mrabarnett/mrab-regex/blob/hg/pyproject.toml)
- [Python 1.6 license FAQ](https://www.python.org/download/releases/1.6.1/license-faq/)
- [CNRI GPL-compatible license](https://opensource.org/license/cnri-python-gpl-compatible)
- [Apache 2.0 and GPLv3 compatibility](https://apache.org/licenses/GPL-compatibility.html)
