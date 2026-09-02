#!/usr/bin/env python3
"""Execute every example in a package README and report the ones that are not true.

	python3 tools/readme.py           # this package's README, against src/
	python3 tools/readme.py --wheel   # against dist/*.whl, as a stranger installs it
	python3 tools/readme.py --show    # print the blocks, run nothing

It lives in each package rather than beside them: these four are published
independently, and a check a cloner cannot run is a check that does not exist
for them.

Prose is the only part of these packages that no other gate reads, and this
project has four recorded incidents where a green suite sat beside a deployment
that could not take a payment. A cookbook whose recipes have never been run is
the same defect with better formatting.

Three kinds of ```python block, selected by an HTML comment on the line before
the fence so that GitHub still syntax-highlights every one of them:

    <!-- readme: new -->     start a fresh namespace; the recipe stands alone
    <!-- readme: skip -->    show it, never run it (needs a chain, funds, a key)
    <!-- readme: raises -->  every line is `expression  # ExceptionName - why`

An unmarked block continues the previous namespace, so a recipe may be told in
several fences. A bare expression whose line ends in `# -> value` is evaluated
and compared with that value.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
import tempfile
import traceback
import zipfile

PACKAGE = pathlib.Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"(?:<!--\s*readme:\s*(\w+)\s*-->\s*\n)?```python\n(.*?)```", re.DOTALL)
CLAIM = re.compile(r"#\s*->\s*(.+?)\s*$")
RAISES = re.compile(r"#\s*([A-Z]\w+)")
_NO_CLAIM = object()


class _Unparseable:
	"""A `# ->` marker whose value could not be read.

	It is a FAILURE and never a skip. The first version of this gate returned
	_NO_CLAIM here, which meant a claim it could not parse was silently not
	checked -- the precise failure mode the gate exists to prevent, hiding in
	the gate itself.
	"""

	def __init__(self, text):
		self.text = text


def blocks(readme: pathlib.Path):
	"""Return (mode, source) for every python fence, in document order."""
	return [(mode or "", body) for mode, body in BLOCK.findall(readme.read_text(encoding="utf-8"))]


def _claim_for(statement, source_lines):
	"""The `# -> value` a bare expression claims, or _NO_CLAIM.

	The marker may sit on the expression's own last line, or on the comment
	lines directly beneath it -- both styles read naturally, and a claim that
	is only checked in one of them is a claim that rots in the other. A claim
	spanning several comment lines is rejoined before it is read.
	"""
	if not isinstance(statement, ast.Expr):
		return _NO_CLAIM
	index = statement.end_lineno - 1
	if CLAIM.search(source_lines[index]):
		text = CLAIM.search(source_lines[index]).group(1)
	else:
		trailing = []
		for line in source_lines[index + 1:]:
			if not line.strip().startswith("#"):
				break
			trailing.append(line.strip().lstrip("#").strip())
		joined = " ".join(trailing)
		if "->" not in joined:
			return _NO_CLAIM
		text = joined.split("->", 1)[1].strip()
	try:
		return ast.literal_eval(text)
	except (ValueError, SyntaxError):
		return _Unparseable(text)


def _search_paths(package: pathlib.Path):
	"""Where the examples import from: this package's src, then its examples.

	A rail also needs `cryptopos_core`. A sibling checkout is used when there is
	one; otherwise the installed distribution is, which is what a cloner of a
	single repository has.
	"""
	paths = [package / "src", package.parent / "cryptopos-core" / "src", package / "examples"]
	return [p for p in paths if p.is_dir()]


def _declared_version(package: pathlib.Path):
	"""The version this working tree declares, from pyproject or the package init."""
	text = (package / "pyproject.toml").read_text(encoding="utf-8")
	for line in text.splitlines():
		if line.startswith("version = "):
			return line.split("=", 1)[1].strip().strip('"\'')
	for init in (package / "src").glob("*/__init__.py"):
		for line in init.read_text(encoding="utf-8").splitlines():
			if line.startswith("__version__"):
				return line.split("=", 1)[1].strip().strip('"\'')
	return None


def _unpack_wheels(package: pathlib.Path, destination: pathlib.Path):
	"""Install this package, and a sibling core if there is one, from the wheels."""
	wanted = [package, package.parent / "cryptopos-core"]
	for project in dict.fromkeys(p for p in wanted if (p / "dist").is_dir()):
		wheels = sorted((project / "dist").glob("*.whl"))
		if not wheels:
			sys.exit(f"readme: no wheel in {project / 'dist'} — run a build first")
		# A STALE WHEEL MUST NOT QUIETLY PASS. --wheel exists to check the
		# artefact a stranger installs; a dist/ left behind by an older version
		# would check code this tree no longer contains, and report it green.
		declared = _declared_version(project)
		built = wheels[-1].name.split("-")[1]
		if declared is not None and built != declared:
			sys.exit(f"readme: {project.name}/dist holds {built} but the tree declares "
			         f"{declared} — rebuild before checking against the wheel")
		zipfile.ZipFile(wheels[-1]).extractall(destination)
	if not any(destination.iterdir()):
		sys.exit(f"readme: no wheel found for {package.name} — run a build first")
	return destination


def run(package: pathlib.Path, wheel: bool, show: bool):
	readme = package / "README.md"
	if not readme.is_file():
		sys.exit(f"readme: no README.md in {package}")
	found = blocks(readme)
	if show:
		for index, (mode, block) in enumerate(found, 1):
			print(f"--- block {index} [{mode or 'continue'}] ---\n{block}")
		return 0

	temporary = None
	if wheel:
		temporary = tempfile.TemporaryDirectory()
		paths = [_unpack_wheels(package, pathlib.Path(temporary.name))]
		if (package / "examples").is_dir():
			paths.append(package / "examples")
	else:
		paths = _search_paths(package)
	for path in reversed(paths):
		sys.path.insert(0, str(path))

	failures, ran, skipped = [], 0, 0
	namespace: dict = {"__name__": "readme"}
	for index, (mode, block) in enumerate(found, 1):
		if mode == "skip":
			skipped += 1
			try:
				compile(block, str(readme), "exec")          # it must at least be Python
			except SyntaxError as exc:
				failures.append(f"block {index} (skipped) is not valid Python: {exc}")
			continue
		if mode == "new":
			namespace = {"__name__": "readme"}
		ran += 1
		if mode == "raises":
			for line in (l for l in block.strip().splitlines() if l.strip()):
				expression, _, _comment = line.partition("#")
				wanted = RAISES.search(line)
				if not wanted:
					failures.append(f"block {index}: `{line.strip()}` names no exception")
					continue
				try:
					eval(expression.strip(), namespace)      # noqa: S307 - the point
					failures.append(f"block {index}: {expression.strip()} did not raise {wanted.group(1)}")
				except Exception as exc:                     # noqa: BLE001
					if type(exc).__name__ != wanted.group(1):
						failures.append(f"block {index}: {expression.strip()} raised "
						                f"{type(exc).__name__}, README says {wanted.group(1)}")
			continue
		source_lines = block.splitlines()
		try:
			tree = ast.parse(block)
			for statement in tree.body:
				claim = _claim_for(statement, source_lines)
				if claim is _NO_CLAIM:
					exec(compile(ast.Module([statement], []), str(readme), "exec"), namespace)  # noqa: S102
					continue
				expression = ast.get_source_segment(block, statement.value) or "<expression>"
				actual = eval(compile(ast.Expression(statement.value), str(readme), "eval"), namespace)  # noqa: S307
				if isinstance(claim, _Unparseable):
					failures.append(f"block {index}: {expression} — the `->` claim "
					                f"{claim.text!r} is not a Python literal, so nothing "
					                f"checked it; actual value is {actual!r}")
					continue
				if actual != claim:
					failures.append(f"block {index}: {expression} — README says {claim!r}, "
					                f"actually produced {actual!r}")
		except Exception:                                    # noqa: BLE001
			failures.append(f"block {index} failed:\n{traceback.format_exc(limit=3)}")

	where = "the built wheel" if wheel else "src/"
	print(f"readme: {package.name} — {ran} example block(s) run against {where}, {skipped} shown only")
	if temporary is not None:
		temporary.cleanup()
	if failures:
		print(f"\nNOT TRUE — {len(failures)}:\n", file=sys.stderr)
		for failure in failures:
			print(f"  {failure}", file=sys.stderr)
		print("\nFix the prose or fix the code. A README that lies is worse than no README.",
		      file=sys.stderr)
		return 1
	print("readme: every example is true")
	return 0


def main():
	parser = argparse.ArgumentParser(description="Check that this package's README examples are true.")
	parser.add_argument("--wheel", action="store_true", help="import from dist/*.whl, not src/")
	parser.add_argument("--show", action="store_true", help="print the blocks and run nothing")
	return run(PACKAGE, parser.parse_args().wheel, parser.parse_args().show)


if __name__ == "__main__":
	sys.exit(main())
