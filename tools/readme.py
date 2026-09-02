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
import configparser
import io
import pathlib
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import tokenize
import traceback
import zipfile

PACKAGE = pathlib.Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"(?:<!--\s*readme:\s*(\w+)\s*-->\s*\n)?```python\n(.*?)```", re.DOTALL)
MARKER = re.compile(r"->")
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


class _Ambiguous:
	"""A comment carrying more than one arrow. Nothing can say which is meant."""

	def __init__(self, text):
		self.text = text


def _comments(block: str):
	"""{line number: comment text} for every COMMENT TOKEN in the block.

	Tokenizing rather than scanning lines is what separates a comment from the
	characters `# ->` inside a string literal. A line regex failed both ways:
	it read `"# -> not a comment"` as a claim, and missed a marker written
	above the statement it described.
	"""
	found = {}
	try:
		for token in tokenize.generate_tokens(io.StringIO(block).readline):
			if token.type == tokenize.COMMENT:
				found[token.start[0]] = token.string
	except (tokenize.TokenError, IndentationError, SyntaxError):
		pass
	return found


def _marker_lines(comments):
	"""The comment lines that carry a `# ->` marker."""
	return {line for line, text in comments.items() if MARKER.search(text)}


def _marker_on(statement, source_lines, markers):
	"""Whether a real `# ->` comment is attached to this statement."""
	if statement.end_lineno in markers:
		return True
	for offset, line in enumerate(source_lines[statement.end_lineno:], start=statement.end_lineno + 1):
		if not line.strip().startswith("#"):
			return False
		if offset in markers:
			return True
	return False


def _claim_lines(statement, source_lines, markers):
	"""The line numbers a bare expression's claim occupies."""
	if statement.end_lineno in markers:
		return {statement.end_lineno}
	lines = set()
	for offset, line in enumerate(source_lines[statement.end_lineno:], start=statement.end_lineno + 1):
		if not line.strip().startswith("#"):
			break
		lines.add(offset)
	return lines if lines & markers else set()


def _claim_text(statement, source_lines, comments, markers):
	"""The raw text a bare expression claims, before it is parsed."""
	lines = sorted(_claim_lines(statement, source_lines, markers))
	joined = " ".join(comments[line].lstrip("#").strip() for line in lines if line in comments)
	return joined.split("->", 1)[1].strip() if "->" in joined else ""


def _claim_for(statement, source_lines, comments, markers):
	"""The value a bare expression claims, or _NO_CLAIM.

	The claim is read out of the COMMENT TOKEN, never the raw line, so a string
	containing an arrow is not mistaken for one. A comment holding two arrows
	is refused rather than resolved: `1  # -> 1  # -> 2` used to check only the
	text after the LAST arrow, and Python's own comment rules then swallowed
	the mismatch, so a false claim disappeared into a passing block.
	"""
	if not isinstance(statement, ast.Expr):
		return _NO_CLAIM
	lines = sorted(_claim_lines(statement, source_lines, markers))
	if not lines:
		return _NO_CLAIM
	texts = [comments[line].lstrip("#").strip() for line in lines if line in comments]
	joined = " ".join(texts)
	if joined.count("->") > 1:
		return _Ambiguous(joined)
	text = joined.split("->", 1)[1].strip() if "->" in joined else ""
	if not text:
		return _Unparseable(joined)
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


def _declared_name(package: pathlib.Path):
	"""The DISTRIBUTION name, which is not reliably the directory name."""
	for line in (package / "pyproject.toml").read_text(encoding="utf-8").splitlines():
		if line.startswith("name = "):
			return line.split("=", 1)[1].strip().strip('"\'')
	return package.name


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


def _pyproject_field(project: pathlib.Path, field: str):
	"""A top-level `[project]` value, as a list.

	Hand-parsing TOML got this wrong in the obvious way: it split
	`"cryptopos-core>=2,<3"` on the comma INSIDE the specifier and reported two
	dependencies. A checker that misreads what it is comparing invents failures
	as readily as it misses them, so this uses a real parser or declines to
	compare at all.
	"""
	data = _pyproject(project)
	value = data.get("project", {}).get(field)
	if value is None:
		return []
	return [value] if isinstance(value, str) else list(value)


def _pyproject(project: pathlib.Path):
	"""The parsed pyproject, or a refusal to run this check at all.

	Returning "no opinion" when `tomllib` is missing meant the metadata
	comparison silently switched itself off on Python 3.9 and 3.10 -- both of
	which this package supports, so the check was absent exactly where someone
	would reasonably run it. A check that disables itself is worse than one
	that is not there, because the green line still gets printed.
	"""
	try:
		import tomllib
	except ModuleNotFoundError:                     # pragma: no cover - Python < 3.11
		sys.exit("readme: --wheel needs Python 3.11+ for tomllib, to compare the wheel's "
		         "metadata against pyproject.toml. Run the gate on a newer interpreter; "
		         "the package itself still supports 3.9.")
	return tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))


def _metadata_differences(project: pathlib.Path, archive):
	"""What the wheel PUBLISHES, against what this tree declares.

	The bytes of the modules can match while the wheel still installs a
	dependency the tree no longer declares, or pins a different Python. A
	reader's `pip install` obeys the metadata, not the source, so a gate that
	reads only `.py` can approve an artefact that behaves differently.
	"""
	name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
	if name is None:
		return ["the wheel has no METADATA"]
	metadata = archive.read(name).decode("utf-8", "replace")
	declared_list = _pyproject_field(project, "dependencies")
	problems = _entry_point_differences(project, archive)
	for field, key in (("Name", "name"), ("Version", "version")):
		built = next((line.split(":", 1)[1].strip()
		              for line in metadata.splitlines() if line.startswith(f"{field}:")), "")
		wanted = (_pyproject_field(project, key) or [""])[0]
		if not wanted and key == "version":
			# A DYNAMIC VERSION IS STILL A VERSION. `[project].version` is
			# absent here, and the `if wanted` guard used to skip the check
			# entirely -- so a wheel could carry METADATA saying 9.9.9 behind a
			# correctly named file and pass.
			wanted = _declared_version(project) or ""
		if wanted and built.replace("_", "-") != wanted.replace("_", "-"):
			problems.append(f"wheel {field} is {built!r}, the tree says {wanted!r}")
	built_requires = sorted(
		line.split(":", 1)[1].strip()
		for line in metadata.splitlines() if line.startswith("Requires-Dist:"))
	declared = sorted(declared_list)
	if built_requires != declared:
		problems.append(f"wheel requires {built_requires or 'nothing'} but pyproject declares "
		                f"{declared or 'nothing'}")
	# The wheel embeds the README as its long description, and that -- not the
	# working tree -- is what PyPI and `pip show` present. Executing the
	# working-tree README against the wheel while the wheel ships a different
	# document is the same staleness this guard exists to refuse.
	embedded = metadata.split("\n\n", 1)[1] if "\n\n" in metadata else ""
	on_disk = (project / "README.md").read_text(encoding="utf-8")
	# EXACT, apart from a trailing newline. Whitespace is executable content in
	# a file full of Python, and collapsing it let a wheel embed a README whose
	# output claim carried a doubled space while comparing equal to the correct
	# one. An ABSENT description is a difference too, not an exemption.
	if embedded.rstrip("\n") != on_disk.rstrip("\n"):
		problems.append("the README embedded in the wheel differs from README.md"
		                if embedded.strip() else "the wheel embeds no README")
	built_python = next((line.split(":", 1)[1].strip()
	                     for line in metadata.splitlines() if line.startswith("Requires-Python:")), "")
	declared_python = ((_pyproject_field(project, "requires-python") or [""]) or [""])[0]
	if declared_python and built_python != declared_python:
		problems.append(f"wheel requires-python {built_python!r}, pyproject says {declared_python!r}")
	return problems


def _entry_point_differences(project: pathlib.Path, archive):
	"""The wheel's entry points against the ones pyproject declares.

	This is the whole installation story for a rail: `pip install` then
	`discover()`. A wheel whose `entry_points.txt` is missing or stale installs
	cleanly, matches every module byte, and provides no rail at all.
	"""
	declared = _pyproject(project).get("project", {}).get("entry-points", {}) or {}
	name = next((n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")), None)
	built = {}
	if name is not None:
		parser = configparser.ConfigParser()
		parser.read_string(archive.read(name).decode("utf-8", "replace"))
		built = {group: dict(parser.items(group)) for group in parser.sections()}
	wanted = {group: {k: v for k, v in points.items()} for group, points in declared.items()}
	if built != wanted:
		return [f"wheel entry points {built or 'none'} do not match pyproject {wanted or 'none'}"]
	return []


def _refuse_if_contents_differ(project: pathlib.Path, archive, wheel_name: str):
	"""Compare the wheel's modules against src/ byte for byte.

	A version string is not provenance. Editing a module without bumping
	`__version__` is ordinary during development, and left the previous guard
	accepting a wheel that no longer contained the code under test -- the exact
	silent-staleness this whole gate exists to refuse.
	"""
	members = {name for name in archive.namelist() if name.endswith(".py")}
	differences = []
	expected = set()
	for source in sorted((project / "src").rglob("*.py")):
		member = str(source.relative_to(project / "src"))
		expected.add(member)
		if member not in members:
			differences.append(f"{member} is missing from the wheel")
		elif archive.read(member) != source.read_bytes():
			differences.append(f"{member} differs from src/")
	for stale in sorted(members - expected):
		# A module deleted from src/ but still inside the wheel is importable
		# by a reader and absent from the maintainer's tree -- the worst
		# possible asymmetry for a gate that claims to check the artefact.
		differences.append(f"{stale} is in the wheel but no longer in src/")
	differences.extend(_metadata_differences(project, archive))
	if differences:
		sys.exit(f"readme: {wheel_name} does not match src/ — rebuild before checking "
		         f"against the wheel\n  " + "\n  ".join(differences[:6]))


def _unpack_wheels(package: pathlib.Path, destination: pathlib.Path):
	"""Install this package, and a sibling core if there is one, from the wheels."""
	wanted = [package, package.parent / "cryptopos-core"]
	for project in dict.fromkeys(p for p in wanted if (p / "dist").is_dir()):
		distribution = _declared_name(project).replace("-", "_")
		wheels = sorted((project / "dist").glob(f"{distribution}-*.whl"))
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
		archive = zipfile.ZipFile(wheels[-1])
		_refuse_if_contents_differ(project, archive, wheels[-1].name)
		archive.extractall(destination)
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
	if wheel and os.environ.get("READMEGATE_CHILD") != "1":
		# A FRESH, ISOLATED INTERPRETER. Rebuilding `sys.path` in this process
		# does not undo `sys.modules`, a package `__path__` already pointing
		# elsewhere, an import hook on `sys.meta_path`, or a `sitecustomize`
		# that ran before this file did. "Only the wheel" is a claim about the
		# whole interpreter, so it takes a new one: -I ignores the environment
		# and user site, -S skips site-packages entirely.
		child = subprocess.run(
			[sys.executable, "-I", "-S", str(pathlib.Path(__file__).resolve())],
			env={**os.environ, "READMEGATE_CHILD": "1", "READMEGATE_PACKAGE": str(package)},
			cwd=str(package))
		return child.returncode
	if wheel:
		# ONLY the wheel. Adding examples/ here made this a hybrid of installed
		# code and files that exist solely in a checkout, and reported a recipe
		# true that a `pip install` reader could not run.
		temporary = tempfile.TemporaryDirectory()
		paths = [_unpack_wheels(package, pathlib.Path(temporary.name))]
	else:
		paths = _search_paths(package)
	original_path = list(sys.path)
	if wheel:
		# BUILT, not filtered. Removing entries containing "site-packages" left
		# PYTHONPATH entries, .egg paths and editable-install finders in place,
		# so an undeclared import could still resolve from this machine and
		# fail for a reader. Start from the standard library and add the wheel.
		sys.path[:] = [sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"],
		               str(pathlib.Path(sysconfig.get_paths()["platstdlib"]) / "lib-dynload")]
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
			if _marker_lines(_comments(block)):
				failures.append(
					f"block {index} is skipped but carries a `# ->` claim, which nothing can "
					f"check. Use `# e.g.` for an illustrative value, or make the block runnable.")
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
		comments = _comments(block)
		markers = _marker_lines(comments)
		checked_lines: set = set()
		try:
			tree = ast.parse(block)
			for statement in tree.body:
				claim = _claim_for(statement, source_lines, comments, markers)
				if claim is _NO_CLAIM:
					# A marker on something that is not a bare expression cannot be
					# compared with anything. Silently ignoring it is how a claim
					# comes to look checked while nothing reads it.
					if not isinstance(statement, ast.Expr) and _marker_on(statement, source_lines, markers):
						failures.append(
							f"block {index}: line {statement.end_lineno} carries a `# ->` claim on "
							f"a {type(statement).__name__.lower()}, which the gate cannot evaluate. "
							f"Put the claim on a bare expression, or write `# e.g.`.")
					exec(compile(ast.Module([statement], []), str(readme), "exec"), namespace)  # noqa: S102
					continue
				expression = ast.get_source_segment(block, statement.value) or "<expression>"
				checked_lines.update(_claim_lines(statement, source_lines, markers))
				actual = eval(compile(ast.Expression(statement.value), str(readme), "eval"), namespace)  # noqa: S307
				if isinstance(claim, _Ambiguous):
					failures.append(f"block {index}: {expression} — the comment {claim.text!r} "
					                f"carries more than one `->`; nothing can tell which is the claim")
					continue
				if isinstance(claim, _Unparseable):
					failures.append(f"block {index}: {expression} — the `->` claim "
					                f"{claim.text!r} is not a Python literal, so nothing "
					                f"checked it; actual value is {actual!r}")
					continue
				# REPRESENTATION, not merely equality. Type-and-value still
				# passed `{"b": 2, "a": 1}  # -> {"a": 1, "b": 2}` and
				# `-0.0  # -> 0.0`: equal values, different text on the screen.
				# A claim in this file is what the reader will see, so it is
				# compared against `repr`.
				# EXACT. Collapsing runs of whitespace in both sides let
				# `"a  b"  # -> 'a b'` pass -- the claim and the output differ
				# by a space inside the string, which is exactly the sort of
				# thing a reader would copy and be wrong about.
				written = _claim_text(statement, source_lines, comments, markers)
				produced = repr(actual)
				if produced != written:
					failures.append(f"block {index}: {expression} — README shows {written}, "
					                f"actually produced {produced}")
			unaccounted = markers - checked_lines
			if unaccounted:
				failures.append(
					f"block {index}: `# ->` on line(s) {sorted(unaccounted)} were not checked by "
					f"anything. A marker only means something on a bare top-level expression; "
					f"write `# e.g.` for an illustration.")
		except Exception:                                    # noqa: BLE001
			failures.append(f"block {index} failed:\n{traceback.format_exc(limit=3)}")

	sys.path[:] = original_path
	where = "the built wheel, with site-packages off the path" if wheel else "src/"
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
	if os.environ.get("READMEGATE_CHILD") == "1":
		return run(pathlib.Path(os.environ["READMEGATE_PACKAGE"]), True, False)
	parser = argparse.ArgumentParser(description="Check that this package's README examples are true.")
	parser.add_argument("--wheel", action="store_true", help="import from dist/*.whl, not src/")
	parser.add_argument("--show", action="store_true", help="print the blocks and run nothing")
	return run(PACKAGE, parser.parse_args().wheel, parser.parse_args().show)


if __name__ == "__main__":
	sys.exit(main())
